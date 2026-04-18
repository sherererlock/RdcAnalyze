# -*- coding: utf-8 -*-
"""rdc_collect.py - Full RDC capture data collection + computed analysis + Render Graph.

Two-phase RDC analysis workflow:
  Phase 1 (this script): Automated data collection & algorithmic analysis.
  Phase 2 (AI):          Semantic analysis of collected data by Claude.

Usage:
    python\\python.exe rdc_collect.py <capture.rdc> [-j WORKERS]

Output:
    {capture-stem}-analysis/ directory with JSON data files + render_graph.html
"""

from __future__ import annotations

import os
import sys

if sys.version_info < (3, 10):
    print("ERROR: Python 3.10+ required. Use the embedded Python:")
    print("  python\\python.exe rdc_collect.py <capture.rdc>")
    sys.exit(1)

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))

import argparse
import bisect
import hashlib
import json
import re
import signal
import socket
import subprocess
import threading
import time
from collections import Counter, defaultdict
from concurrent.futures import ThreadPoolExecutor, as_completed
from pathlib import Path

from shared import (
    BPP_TABLE, guess_bpp, unwrap, write_json, fmt_number, estimate_texture_mb,
)

# ─────────────────────────────────────────────────────────────────────
# Configuration
# ─────────────────────────────────────────────────────────────────────

VERSION = "1.1.0"
SESSION_PREFIX = "rdc-collect"
MAIN_SESSION = f"{SESSION_PREFIX}-main"
RDC_BAT = str(Path(__file__).resolve().parent.parent.parent / "rdc-portable" / "rdc.bat")

# Alert thresholds
ALERT_HIGH_TRI_DRAW = 10000
ALERT_LARGE_TEX_DIM = 2048
ALERT_LARGE_TEX_MB = 4.0


# ─────────────────────────────────────────────────────────────────────
# Helpers
# ─────────────────────────────────────────────────────────────────────

def run_rdc(*args: str, session: str | None = None, timeout: int = 120) -> tuple[str, str, int]:
    """Execute a single rdc command. Returns (stdout, stderr, returncode).

    If *session* is given, the command runs within that named daemon session
    (thread-safe for parallel workers).
    """
    cmd = [RDC_BAT]
    if session:
        cmd += ["--session", session]
    cmd.extend(args)
    try:
        r = subprocess.run(cmd, capture_output=True, text=True, timeout=timeout)
        return r.stdout.strip(), r.stderr.strip(), r.returncode
    except subprocess.TimeoutExpired:
        return "", "TIMEOUT after {}s".format(timeout), -1


def run_rdc_json(*args: str, session: str | None = None, timeout: int = 120) -> dict | list | None:
    """Execute rdc command with --json flag and return parsed JSON, or None on error."""
    stdout, stderr, rc = run_rdc(*args, "--json", session=session, timeout=timeout)
    if rc != 0 or not stdout:
        return None
    try:
        return json.loads(stdout)
    except json.JSONDecodeError:
        return None


def _session_file(session: str) -> Path:
    """Return session JSON path for a named rdc session."""
    if os.name == "nt":
        base = Path(os.environ.get("LOCALAPPDATA", Path.home() / "AppData" / "Local"))
    else:
        base = Path.home() / ".local" / "share"
    return base / "rdc" / "sessions" / f"{session}.json"


def _rpc_call(session: str, method: str, params: dict | None = None, timeout: float = 30.0) -> dict | None:
    """Send JSON-RPC request directly to daemon, bypassing CLI's 30s socket timeout.

    Returns the 'result' dict on success, None on error.
    """
    sf = _session_file(session)
    if not sf.exists():
        return None
    try:
        sdata = json.loads(sf.read_text())
        host, port, token = sdata["host"], int(sdata["port"]), sdata["token"]
    except (json.JSONDecodeError, KeyError, ValueError):
        return None

    payload = {
        "jsonrpc": "2.0",
        "method": method,
        "id": 1,
        "params": {"_token": token, **(params or {})},
    }
    data = (json.dumps(payload) + "\n").encode("utf-8")
    try:
        with socket.create_connection((host, port), timeout=timeout) as sock:
            sock.sendall(data)
            # recv_line: read until newline
            chunks: list[bytes] = []
            while True:
                chunk = sock.recv(65536)
                if not chunk:
                    break
                chunks.append(chunk)
                if b"\n" in chunk:
                    break
            if not chunks:
                return None
            line = b"".join(chunks).split(b"\n", 1)[0].decode("utf-8")
            resp = json.loads(line)
            if "error" in resp:
                return None
            return resp.get("result")
    except (OSError, json.JSONDecodeError, ValueError):
        return None


def _unwrap(data: dict | list | None, key: str) -> list | dict | None:
    """Unwrap rdc JSON output: {'draws': [...]} -> [...], or return as-is if already a list.

    Thin wrapper around shared.unwrap for single-key call pattern used in collect.
    """
    return unwrap(data, key)


class Progress:
    """Thread-safe progress printer."""

    def __init__(self, total: int, phase: str) -> None:
        self.total = total
        self.current = 0
        self.phase = phase
        self.t0 = time.time()
        self._lock = threading.Lock()

    def tick(self, label: str = "") -> None:
        with self._lock:
            self.current += 1
            current = self.current
        elapsed = time.time() - self.t0
        rate = current / elapsed if elapsed > 0 else 0
        eta = (self.total - current) / rate if rate > 0 else 0
        pct = 100 * current / self.total if self.total else 100
        msg = f"\r  [{current}/{self.total}] {self.phase}: {label}"
        msg += f" ({pct:.0f}%, ~{eta:.0f}s left)    "
        print(msg, end="", flush=True)

    def done(self) -> float:
        elapsed = time.time() - self.t0
        print(f"\n  Done: {self.phase} - {elapsed:.1f}s")
        return elapsed


class ErrorCollector:
    """Thread-safe error accumulator."""

    def __init__(self) -> None:
        self._errors: list[dict] = []
        self._lock = threading.Lock()

    def append(self, error: dict) -> None:
        with self._lock:
            self._errors.append(error)

    @property
    def errors(self) -> list[dict]:
        with self._lock:
            return list(self._errors)

    def __len__(self) -> int:
        with self._lock:
            return len(self._errors)


# ─────────────────────────────────────────────────────────────────────
# Phase 1a: Data Collection
# ─────────────────────────────────────────────────────────────────────

BASE_STEPS: list[tuple[str, list[str], int]] = [
    ("info",           ["info"],           120),
    ("stats",          ["stats"],          120),
    ("passes",         ["passes"],         120),
    ("pass_deps",      ["passes", "--deps"], 180),
    ("draws",          ["draws"],          120),
    ("events",         ["events"],         120),
    ("resources",      ["resources"],      120),
    ("unused_targets", ["unused-targets"], 120),
    ("log",            ["log"],            120),
    ("counters",       ["counters"],       180),
    # NOTE: "shaders" removed — triggers expensive full-replay cache build.
    # Collected separately in Step 5 with longer timeout.
]


def collect_base(errors: ErrorCollector, *, session: str | None = None) -> dict:
    """Collect base data items. Returns dict for summary.json."""
    summary: dict = {}
    prog = Progress(len(BASE_STEPS), "Base data")
    for key, cmd_args, tout in BASE_STEPS:
        prog.tick(key)
        data = run_rdc_json(*cmd_args, session=session, timeout=tout)
        if data is not None:
            summary[key] = data
        else:
            summary[key] = None
            errors.append({"phase": "base", "key": key, "error": "failed or empty"})
    prog.done()
    return summary


def collect_pass_details(
    summary: dict, errors: ErrorCollector, *, session: str | None = None,
) -> list:
    """Collect per-pass attachment details."""
    passes = _unwrap(summary.get("passes"), "passes")
    if not passes or not isinstance(passes, list):
        print("  Skipping pass details (no passes data)")
        return []

    results: list = []
    prog = Progress(len(passes), "Pass details")
    for i in range(len(passes)):
        prog.tick(f"pass {i}")
        data = run_rdc_json("pass", str(i), session=session)
        if data is not None:
            results.append(data)
        else:
            results.append({"_index": i, "_error": "failed"})
            errors.append({"phase": "pass_details", "index": i, "error": "failed"})
    prog.done()
    return results


def _get_draw_eids(summary: dict) -> list[int]:
    """Extract draw call EIDs from summary.draws."""
    draws = _unwrap(summary.get("draws"), "draws")
    if not draws or not isinstance(draws, list):
        return []
    return [d["eid"] for d in draws if isinstance(d, dict) and "eid" in d]


def collect_per_draw(draw_eids: list[int], errors: ErrorCollector) -> tuple[dict, dict]:
    """Collect pipeline + bindings for every draw in one pass. Returns (pipelines, bindings)."""
    if not draw_eids:
        print("  Skipping per-draw data (no draw EIDs)")
        return {}, {}

    pipelines: dict = {}
    bindings: dict = {}
    prog = Progress(len(draw_eids), "Per-draw (pipeline+bindings)")
    for eid in draw_eids:
        prog.tick(f"EID {eid}")
        eid_str = str(eid)
        data = run_rdc_json("pipeline", eid_str)
        if data is not None:
            pipelines[eid_str] = data
        else:
            errors.append({"phase": "pipelines", "eid": eid, "error": "failed"})

        data = run_rdc_json("bindings", eid_str)
        if data is not None:
            bindings[eid_str] = data
        else:
            errors.append({"phase": "bindings", "eid": eid, "error": "failed"})
    prog.done()
    return pipelines, bindings


def collect_shaders_disasm(
    out_dir: Path,
    errors: ErrorCollector,
    *,
    session: str | None = None,
) -> dict:
    """Enumerate shaders, group VS+PS pairs, save .shader files.

    Uses direct JSON-RPC calls to bypass CLI's 30s socket timeout —
    _build_shader_cache can take several minutes on large captures.

    Returns {pair_key: {vs_id, ps_id, eids, uses, file}} for shader_disasm.json.
    """
    sess_name = session or "default"

    # Step A: trigger shader cache build via 'shaders' RPC (long timeout)
    print("    Building shader cache (this may take several minutes) ...")
    result = _rpc_call(sess_name, "shaders", {}, timeout=900)
    if result is None:
        print("  Skipping shader disasm (shaders RPC failed)")
        errors.append({"phase": "shader_disasm", "error": "shaders RPC failed or timed out"})
        return {}

    shaders = result.get("rows", [])
    if not shaders:
        print("  Skipping shader disasm (no shaders found)")
        errors.append({"phase": "shader_disasm", "error": "no shaders found"})
        return {}

    shader_ids = [s["shader"] for s in shaders if isinstance(s, dict) and "shader" in s]
    print(f"    Found {len(shader_ids)} shaders, fetching info ...")

    # Step B: get info for each shader (fast, cache already built)
    infos: dict[int, dict] = {}
    for sid in shader_ids:
        info = _rpc_call(sess_name, "shader_list_info", {"id": sid}, timeout=30)
        if info and isinstance(info, dict):
            infos[sid] = info

    # Step C: group VS+PS pairs by shared draw EIDs
    eid_to_shaders: dict[int, dict[str, int]] = {}
    for sid, info in infos.items():
        stages = info.get("stages") or []
        eids = info.get("eids") or []
        for stage in stages:
            for eid in eids:
                eid_to_shaders.setdefault(eid, {})[stage] = sid

    pair_map: dict[tuple[int, int], list[int]] = {}
    for eid, stage_map in sorted(eid_to_shaders.items()):
        vs = stage_map.get("vs", 0)
        ps = stage_map.get("ps", 0)
        pair_map.setdefault((vs, ps), []).append(eid)

    # Step D: fetch disasm and save .shader files
    shaders_dir = out_dir / "shaders"
    shaders_dir.mkdir(exist_ok=True)

    results: dict = {}
    prog = Progress(len(pair_map), "Shader files")
    for (vs_id, ps_id), eids in sorted(pair_map.items()):
        pair_name = f"shader_{vs_id}_{ps_id}"
        prog.tick(pair_name)

        lines: list[str] = []
        lines.append(f"// {'=' * 60}")
        lines.append(f"// Shader Pair: VS={vs_id}  PS={ps_id}")
        lines.append(f"// Used by {len(eids)} draw(s): EID {', '.join(str(e) for e in eids[:20])}")
        if len(eids) > 20:
            lines.append(f"//   ... +{len(eids) - 20} more")
        lines.append(f"// {'=' * 60}")

        for stage, sid in [("Vertex", vs_id), ("Pixel", ps_id)]:
            if sid == 0:
                continue
            info = infos.get(sid, {})
            entry_name = info.get("entry", "main")
            lines.append("")
            lines.append(f"// {'─' * 40}")
            lines.append(f"// {stage} Shader (ID: {sid})  Entry: {entry_name}")
            lines.append(f"// {'─' * 40}")
            lines.append("")

            # Fetch disasm directly (cache already built, should be fast)
            disasm_result = _rpc_call(sess_name, "shader_list_disasm", {"id": sid}, timeout=60)
            disasm_text = None
            if disasm_result and isinstance(disasm_result, dict):
                disasm_text = disasm_result.get("disasm", "")

            if disasm_text:
                lines.append(disasm_text)
            else:
                lines.append("// (disassembly unavailable)")
                errors.append({"phase": "shader_disasm", "shader_id": sid, "error": "disasm failed"})

        # Write .shader file
        shader_file = shaders_dir / f"{pair_name}.shader"
        shader_file.write_text("\n".join(lines), encoding="utf-8")

        rel_path = f"shaders/{pair_name}.shader"
        results[f"{vs_id}_{ps_id}"] = {
            "vs_id": vs_id,
            "ps_id": ps_id,
            "eids": eids,
            "uses": len(eids),
            "file": rel_path,
        }

    prog.done()
    print(f"    Saved {len(results)} .shader files to {shaders_dir}/")
    return results


def collect_resource_details(summary: dict, errors: ErrorCollector) -> dict:
    """Collect per-resource metadata via VFS. Returns {res_id_str: data}.

    Uses /textures/<id>/info and /buffers/<id>/info for rich detail
    (dimensions, format, byte_size for textures; length for buffers).
    """
    resources = _unwrap(summary.get("resources"), "resources")
    if not resources or not isinstance(resources, list):
        print("  Skipping resource details (no resources data)")
        return {}

    # Only collect Texture and Buffer resources (skip StateObject, Shader, etc.)
    targets = [
        r for r in resources
        if isinstance(r, dict) and r.get("type") in ("Texture", "Buffer")
    ]
    results: dict = {}
    prog = Progress(len(targets), "Resource details")
    for res in targets:
        rid = res["id"]
        rtype = res["type"]
        prog.tick(f"{rtype.lower()} {rid}")
        vfs_path = f"/textures/{rid}/info" if rtype == "Texture" else f"/buffers/{rid}/info"
        data = run_rdc_json("cat", vfs_path)
        if data is not None:
            data["type"] = rtype
            results[str(rid)] = data
        else:
            # Fallback to basic info
            results[str(rid)] = res
            errors.append({"phase": "resource_details", "id": rid, "error": "vfs failed"})
    prog.done()
    return results


def collect_rt_usage(
    pass_details: list,
    errors: ErrorCollector,
    *,
    session: str | None = None,
    summary: dict | None = None,
) -> dict:
    """Collect resource usage events and per-subpass descriptor bindings.

    For each unique RT resource ID in pass_details, queries the daemon's
    ``usage`` handler to find which events read/write that resource.

    Also queries ``descriptors`` at each subpass's first draw call to get
    actual texture resource IDs (critical for GLES where usage API misses reads).

    Returns {
        resource_id_str: {"name": str, "entries": [{"eid": int, "usage": str}]},
        ...,
        "_descriptors": {subpass_begin_eid_str: [resource_id, ...]},
    }.
    """
    sess_name = session or "default"

    # Extract unique RT resource IDs
    rt_ids: dict[int, str] = {}
    for pd in pass_details:
        if not isinstance(pd, dict):
            continue
        for ct in pd.get("color_targets") or []:
            if isinstance(ct, dict) and ct.get("id"):
                rt_ids[ct["id"]] = ct.get("name", "")
        dt = pd.get("depth_target")
        if isinstance(dt, dict) and dt.get("id"):
            rt_ids[dt["id"]] = dt.get("name", "")

    if not rt_ids:
        return {}

    results: dict = {}
    prog = Progress(len(rt_ids), "RT usage")
    for rid, rname in rt_ids.items():
        prog.tick(f"resource {rid}")
        result = _rpc_call(sess_name, "usage", {"id": rid}, timeout=30)
        if result and isinstance(result, dict):
            results[str(rid)] = {
                "name": result.get("name", rname),
                "entries": result.get("entries", []),
            }
        else:
            errors.append({"phase": "rt_usage", "id": rid, "error": "usage query failed"})
    prog.done()

    # ── Collect descriptors per subpass (for GLES read detection) ──
    subpasses = _extract_subpasses(summary, pass_details) if summary else []
    if subpasses:
        # Find the first draw EID within each subpass (not the marker push EID)
        draws_list = (summary.get("draws") or []) if summary else []
        all_draw_eids = sorted(d["eid"] for d in draws_list if isinstance(d, dict) and "eid" in d)

        desc_map: dict[str, list[int]] = {}
        # Collect ALL draw EIDs per subpass (not just the first) for
        # exhaustive descriptor sampling — different draws may bind
        # different textures that the usage API misses on GLES.
        sp_all_draws: list[tuple[int, list[int]]] = []  # (begin_eid, [draw_eids])
        for sp in subpasses:
            if sp.get("draws", 0) <= 0:
                continue
            begin = sp.get("begin_eid", 0)
            end = sp.get("end_eid", begin)
            idx = bisect.bisect_left(all_draw_eids, begin)
            draw_list: list[int] = []
            while idx < len(all_draw_eids) and all_draw_eids[idx] <= end:
                draw_list.append(all_draw_eids[idx])
                idx += 1
            if draw_list:
                sp_all_draws.append((begin, draw_list))

        total_queries = sum(len(draws) for _, draws in sp_all_draws)
        prog2 = Progress(total_queries, "Subpass descriptors")
        for begin_eid, draw_list in sp_all_draws:
            rid_set: set[int] = set()
            for draw_eid in draw_list:
                prog2.tick(f"EID {draw_eid}")
                result = _rpc_call(sess_name, "descriptors", {"eid": draw_eid}, timeout=30)
                if result and isinstance(result, dict):
                    for d in (result.get("descriptors") or []):
                        rid = d.get("resource_id", 0)
                        if rid:
                            rid_set.add(rid)
                else:
                    errors.append({"phase": "descriptors", "eid": draw_eid, "error": "query failed"})
            if rid_set:
                desc_map[str(begin_eid)] = sorted(rid_set)
        prog2.done()
        if desc_map:
            results["_descriptors"] = desc_map

    return results


# ─────────────────────────────────────────────────────────────────────
# Parallel collection helpers
# ─────────────────────────────────────────────────────────────────────

def _shard_list(items: list, num_shards: int) -> list[list]:
    """Split items round-robin into N roughly equal shards."""
    if num_shards <= 1:
        return [items]
    shards: list[list] = [[] for _ in range(num_shards)]
    for i, item in enumerate(items):
        shards[i % num_shards].append(item)
    return shards




def _get_resource_tasks(summary: dict) -> list[tuple[int, str]]:
    """Extract (resource_id, type) for Texture and Buffer resources."""
    resources = _unwrap(summary.get("resources"), "resources")
    if not resources or not isinstance(resources, list):
        return []
    return [
        (r["id"], r["type"])
        for r in resources
        if isinstance(r, dict) and r.get("type") in ("Texture", "Buffer")
    ]


def _collect_per_draw_shard(
    session: str,
    eid_shard: list[int],
    progress: Progress,
    errors: ErrorCollector,
) -> tuple[dict, dict]:
    """Collect pipeline + bindings for a shard of draw EIDs."""
    pipelines: dict = {}
    bindings: dict = {}
    for eid in eid_shard:
        progress.tick(f"EID {eid}")
        eid_str = str(eid)
        data = run_rdc_json("pipeline", eid_str, session=session)
        if data is not None:
            pipelines[eid_str] = data
        else:
            errors.append({"phase": "pipelines", "eid": eid, "error": "failed"})
        data = run_rdc_json("bindings", eid_str, session=session)
        if data is not None:
            bindings[eid_str] = data
        else:
            errors.append({"phase": "bindings", "eid": eid, "error": "failed"})
    return pipelines, bindings




def _collect_resources_shard(
    session: str,
    resource_tasks: list[tuple[int, str]],
    progress: Progress,
    errors: ErrorCollector,
) -> dict:
    """Collect resource details via VFS for a shard of (id, type) pairs."""
    results: dict = {}
    for rid, rtype in resource_tasks:
        progress.tick(f"{rtype.lower()} {rid}")
        vfs_path = f"/textures/{rid}/info" if rtype == "Texture" else f"/buffers/{rid}/info"
        data = run_rdc_json("cat", vfs_path, session=session)
        if data is not None:
            data["type"] = rtype
            results[str(rid)] = data
        else:
            results[str(rid)] = {"id": rid, "type": rtype}
            errors.append({"phase": "resource_details", "id": rid, "error": "vfs failed"})
    return results


class WorkerPool:
    """Manages parallel rdc daemon sessions."""

    def __init__(self, num_workers: int, capture_path: str) -> None:
        self.num_workers = num_workers
        self.capture_path = capture_path
        self.session_names = [f"{SESSION_PREFIX}-w{i}" for i in range(num_workers)]
        self._opened: list[str] = []
        self._lock = threading.Lock()

    def open_all(self) -> list[str]:
        """Open all worker sessions in parallel. Returns successfully opened names."""
        def _open_one(name: str) -> tuple[str, bool, str]:
            # Close any stale session first
            run_rdc("close", session=name, timeout=10)
            out, err, rc = run_rdc("open", self.capture_path, session=name)
            return name, rc == 0, err

        with ThreadPoolExecutor(max_workers=self.num_workers) as executor:
            futures = {executor.submit(_open_one, n): n for n in self.session_names}
            for future in as_completed(futures):
                name, ok, err = future.result()
                if ok:
                    with self._lock:
                        self._opened.append(name)
                else:
                    print(f"  WARNING: worker {name} failed to open: {err}")

        return list(self._opened)

    def close_all(self) -> None:
        """Close all opened worker sessions in parallel."""
        with self._lock:
            to_close = list(self._opened)
        if not to_close:
            return

        def _close_one(name: str) -> None:
            run_rdc("close", session=name, timeout=15)

        with ThreadPoolExecutor(max_workers=len(to_close)) as executor:
            list(executor.map(_close_one, to_close))

        with self._lock:
            self._opened.clear()


# ─────────────────────────────────────────────────────────────────────
# Phase 1b: Computed Analysis
# ─────────────────────────────────────────────────────────────────────

def compute_analysis(
    summary: dict,
    pass_details: list,
    pipelines: dict,
    resource_details: dict,
) -> dict:
    """Run computed analysis on collected data. Returns dict for computed.json."""
    result: dict = {}

    # ── Triangle distribution ──
    draws = _unwrap(summary.get("draws"), "draws") or []
    total_tri = sum(d.get("triangles", 0) for d in draws if isinstance(d, dict))
    per_pass_tri: dict[str, int] = defaultdict(int)
    for d in draws:
        if isinstance(d, dict):
            pname = d.get("pass") or "(no pass)"
            per_pass_tri[pname] += d.get("triangles", 0)
    per_pass_list = []
    for name, tri in sorted(per_pass_tri.items(), key=lambda x: -x[1]):
        pct = (tri / total_tri * 100) if total_tri > 0 else 0
        per_pass_list.append({"name": name, "triangles": tri, "percent": round(pct, 1)})
    result["triangle_distribution"] = {"total": total_tri, "per_pass": per_pass_list}

    # ── Draw type distribution ──
    type_counts: Counter = Counter()
    for d in draws:
        if isinstance(d, dict):
            type_counts[d.get("type", "unknown")] += 1
    result["draw_type_distribution"] = dict(type_counts.most_common())

    # ── Memory estimate ──
    tex_total = 0.0
    buf_total = 0.0
    largest: list[dict] = []
    for rid_str, rdata in resource_details.items():
        if not isinstance(rdata, dict) or "_error" in rdata:
            continue
        rtype = (rdata.get("type") or "").lower()
        if "texture" in rtype or "image" in rtype:
            mb = estimate_texture_mb(rdata)
            tex_total += mb
            largest.append({
                "id": rdata.get("id", rid_str),
                "name": rdata.get("name", ""),
                "type": rdata.get("type", ""),
                "size_mb": round(mb, 2),
            })
        elif "buffer" in rtype:
            bsize = rdata.get("length", 0) or rdata.get("size", 0) or 0
            mb = bsize / (1024 * 1024)
            buf_total += mb
            largest.append({
                "id": rdata.get("id", rid_str),
                "name": rdata.get("name", ""),
                "type": rdata.get("type", ""),
                "size_mb": round(mb, 2),
            })
    largest.sort(key=lambda x: -x["size_mb"])
    result["memory_estimate"] = {
        "total_textures_mb": round(tex_total, 2),
        "total_buffers_mb": round(buf_total, 2),
        "largest_resources": largest[:20],
    }

    # ── Symmetric pass detection ──
    passes = _unwrap(summary.get("passes"), "passes") or []
    result["symmetric_passes"] = _detect_symmetric_passes(passes)

    # ── Pipeline dedup ──
    result["pipeline_dedup"] = _dedup_pipelines(pipelines)

    # ── Alerts ──
    alerts: list[dict] = []
    # High triangle draws
    for d in draws:
        if isinstance(d, dict) and d.get("triangles", 0) > ALERT_HIGH_TRI_DRAW:
            alerts.append({
                "severity": "warning",
                "type": "high_triangle_draw",
                "eid": d["eid"],
                "triangles": d["triangles"],
                "pass": d.get("pass"),
            })
    # Empty passes
    for p in passes:
        if isinstance(p, dict) and p.get("draws", 0) == 0 and p.get("dispatches", 0) == 0:
            alerts.append({
                "severity": "info",
                "type": "empty_pass",
                "pass": p.get("name", "unknown"),
            })
    # Large textures
    for r in largest:
        if r.get("size_mb", 0) > ALERT_LARGE_TEX_MB:
            alerts.append({
                "severity": "warning",
                "type": "large_resource",
                "id": r["id"],
                "name": r.get("name", ""),
                "size_mb": r["size_mb"],
            })
    # Validation errors from log
    log_data = _unwrap(summary.get("log"), "messages") or _unwrap(summary.get("log"), "log")
    if isinstance(log_data, list):
        for entry in log_data:
            if isinstance(entry, dict):
                sev = (entry.get("severity") or "").upper()
                if sev in ("HIGH", "ERROR", "CRITICAL"):
                    alerts.append({
                        "severity": "error",
                        "type": "validation_error",
                        "message": entry.get("message", ""),
                        "eid": entry.get("eid"),
                    })
    result["alerts"] = alerts
    return result


def _detect_symmetric_passes(passes: list) -> dict:
    """Detect symmetric/mirror pass patterns (e.g., VR stereo rendering)."""
    if not passes or len(passes) < 4:
        return {"detected": False, "groups": []}
    # Build a signature for each pass: (draws, dispatches, triangles)
    sigs = []
    for p in passes:
        if isinstance(p, dict):
            sigs.append((p.get("draws", 0), p.get("dispatches", 0), p.get("triangles", 0)))
        else:
            sigs.append((0, 0, 0))
    # Try splitting in half and comparing
    n = len(sigs)
    groups = []
    half = n // 2
    if half >= 2:
        a_sigs = sigs[:half]
        b_sigs = sigs[half:half + len(a_sigs)]
        if len(a_sigs) == len(b_sigs):
            matches = sum(1 for x, y in zip(a_sigs, b_sigs) if x == y)
            similarity = matches / len(a_sigs)
            if similarity > 0.7:
                groups.append({
                    "passes_a": list(range(half)),
                    "passes_b": list(range(half, half + len(a_sigs))),
                    "similarity": round(similarity, 3),
                })
    return {"detected": len(groups) > 0, "groups": groups}


def _dedup_pipelines(pipelines: dict) -> dict:
    """Deduplicate pipeline states by content hash."""
    hash_to_eids: dict[str, list[int]] = defaultdict(list)
    for eid_str, data in pipelines.items():
        if not isinstance(data, dict) or "_error" in data:
            continue
        h = hashlib.md5(json.dumps(data, sort_keys=True).encode()).hexdigest()[:12]
        hash_to_eids[h].append(int(eid_str))
    state_groups = []
    for h, eids in sorted(hash_to_eids.items(), key=lambda x: -len(x[1])):
        state_groups.append({"hash": h, "count": len(eids), "eids": eids[:10]})
    return {
        "unique_states": len(hash_to_eids),
        "total_draws": len(pipelines),
        "state_groups": state_groups[:30],
    }


# ─────────────────────────────────────────────────────────────────────
# Phase 1c: Render Graph HTML — Sub-pass extraction + RT flow
# ─────────────────────────────────────────────────────────────────────

# Noise markers to skip (including children)
_NOISE_PATTERNS = ("GUI.Repaint", "UIR.DrawChain", "EditorLoop",
                   "UGUI.Rendering", "GUITexture", "PlayerEndOfFrame")
# Batch container names — not meaningful as pass names
_BATCH_NAMES = frozenset({
    "RenderLoop.DrawSRPBatcher", "RenderLoop.Draw",
    "Canvas.RenderSubBatch", "Canvas.RenderOverlays",
})


def _extract_subpasses(
    summary: dict,
    pass_details: list,
) -> list[dict]:
    """Extract fine-grained sub-passes from event marker hierarchy.

    Returns a list of dicts with keys:
      name, begin_eid, end_eid, draws, triangles, pass_idx,
      color_targets, depth_target
    """
    events = summary.get("events") or []
    draws_list = summary.get("draws") or []
    coarse_passes = _unwrap(summary.get("passes"), "passes") or []

    if not events:
        return _coarse_as_subpasses(coarse_passes, pass_details)

    draw_map: dict[int, dict] = {dr["eid"]: dr for dr in draws_list}

    # ── Step 1: parse events into marker list ──
    stack: list[dict] = []
    markers: list[dict] = []
    noise_depth: int | None = None

    for e in events:
        eid, name, typ = e["eid"], e["name"], e["type"]

        is_pop = name in ("glPopDebugGroup()", "vkCmdEndDebugUtilsLabelEXT()")
        is_push = (
            typ == "Other"
            and not name.startswith(("gl", "egl", "vk", "dx"))
        )

        if is_pop:
            if stack:
                m = stack.pop()
                m["end_eid"] = eid
                if noise_depth is not None and len(stack) <= noise_depth:
                    noise_depth = None
                if not m.get("_noise"):
                    markers.append(m)
        elif is_push:
            is_noise = any(pat in name for pat in _NOISE_PATTERNS)
            if noise_depth is not None or is_noise:
                if is_noise and noise_depth is None:
                    noise_depth = len(stack)
                stack.append({"_noise": True, "begin_eid": eid})
            else:
                stack.append({
                    "name": name,
                    "begin_eid": eid,
                    "end_eid": None,
                    "depth": len(stack),
                    "draws": 0,
                    "triangles": 0,
                    "_noise": False,
                })

        if eid in draw_map and noise_depth is None:
            dr = draw_map[eid]
            for m in stack:
                if not m.get("_noise"):
                    m["draws"] += 1
                    m["triangles"] += dr.get("triangles", 0)

    # Close any unclosed markers (e.g. Present.BlitToCurrentFB before eglSwapBuffers)
    last_eid = events[-1]["eid"] if events else 0
    while stack:
        m = stack.pop()
        m["end_eid"] = last_eid
        if not m.get("_noise"):
            markers.append(m)

    # ── Step 2: filter to meaningful candidates ──
    candidates = [
        m for m in markers
        if m["draws"] > 0 and m["name"] not in _BATCH_NAMES
    ]

    if not candidates:
        return _coarse_as_subpasses(coarse_passes, pass_details)

    candidates.sort(key=lambda m: (m["begin_eid"], -(m.get("end_eid") or 99999999)))

    # ── Step 3: find leaf passes (no child candidate inside them) ──
    leaf_passes: list[dict] = []
    for i, c in enumerate(candidates):
        c_end = c.get("end_eid") or c["begin_eid"]
        has_child = any(
            other["begin_eid"] > c["begin_eid"]
            and (other.get("end_eid") or other["begin_eid"]) <= c_end
            for j, other in enumerate(candidates) if j != i
        )
        if not has_child:
            leaf_passes.append(c)

    if not leaf_passes:
        return _coarse_as_subpasses(coarse_passes, pass_details)

    # ── Step 4: assign RT info from coarse passes ──
    for lp in leaf_passes:
        pi = _find_coarse_pass_idx(lp, coarse_passes)
        lp["pass_idx"] = pi
        _attach_rt_info(lp, pi, pass_details)
        lp.pop("_noise", None)
        lp.pop("depth", None)

    # ── Step 5: deduplicate names with suffixes ──
    name_counts: dict[str, int] = Counter(lp["name"] for lp in leaf_passes)
    name_seen: dict[str, int] = {}
    for lp in leaf_passes:
        n = lp["name"]
        if name_counts[n] > 1:
            idx = name_seen.get(n, 0) + 1
            name_seen[n] = idx
            lp["display_name"] = f"{n} ({idx})"
        else:
            lp["display_name"] = n

    return leaf_passes


def _coarse_as_subpasses(coarse_passes: list, pass_details: list) -> list[dict]:
    """Fallback: convert coarse passes to sub-pass format."""
    result = []
    for i, p in enumerate(coarse_passes):
        if not isinstance(p, dict):
            continue
        sp = {
            "name": p.get("name", f"Pass #{i}"),
            "display_name": p.get("name", f"Pass #{i}"),
            "begin_eid": p.get("begin_eid", 0),
            "end_eid": p.get("end_eid", 0),
            "draws": p.get("draws", 0),
            "triangles": p.get("triangles", 0),
            "dispatches": p.get("dispatches", 0),
            "pass_idx": i,
        }
        _attach_rt_info(sp, i, pass_details)
        result.append(sp)
    return result


def _find_coarse_pass_idx(subpass: dict, coarse_passes: list) -> int | None:
    """Find which coarse pass best contains this sub-pass (tightest fit)."""
    sp_begin = subpass["begin_eid"]
    sp_end = subpass.get("end_eid") or sp_begin

    best_idx: int | None = None
    best_span = float("inf")

    # Find the tightest containing pass (with slack for marker boundaries)
    for i, cp in enumerate(coarse_passes):
        if not isinstance(cp, dict):
            continue
        cp_begin = cp.get("begin_eid", 0)
        cp_end = cp.get("end_eid", 0)
        if sp_begin >= cp_begin - 20 and sp_end <= cp_end + 20:
            span = cp_end - cp_begin
            if span < best_span:
                best_span = span
                best_idx = i

    if best_idx is not None:
        return best_idx

    # Overlap fallback
    for i, cp in enumerate(coarse_passes):
        if not isinstance(cp, dict):
            continue
        cp_begin = cp.get("begin_eid", 0)
        cp_end = cp.get("end_eid", 0)
        if sp_begin <= cp_end and sp_end >= cp_begin:
            return i

    return None


def _attach_rt_info(subpass: dict, pass_idx: int | None, pass_details: list) -> None:
    """Attach color_targets and depth_target from pass_details."""
    if pass_idx is not None and pass_idx < len(pass_details):
        pd = pass_details[pass_idx]
        if isinstance(pd, dict):
            subpass["color_targets"] = pd.get("color_targets") or []
            subpass["depth_target"] = pd.get("depth_target")
            return
    subpass["color_targets"] = []
    subpass["depth_target"] = None


def _short_rt_name(name: str) -> str:
    """Shorten a resource name for display: '_CameraColorA_2340x1080_...' -> 'CameraColorA'."""
    if not name:
        return ""
    # Strip leading underscore
    n = name.lstrip("_")
    # Take first segment before dimension pattern
    for sep in ("_2", "_1", "_Tex", "_R8", "_R1", "_D3", "_D2", "_B1"):
        idx = n.find(sep)
        if idx > 0:
            n = n[:idx]
            break
    return n


def _find_subpass_for_eid(subpasses: list[dict], eid: int) -> int | None:
    """Find which subpass contains the given EID (by EID range).

    Falls back to proximity matching: if the EID falls in a gap between
    subpasses, returns the nearest subsequent subpass within 200 EIDs.
    This handles cases like UberPostProcess reads landing between Bloom
    end and the next pass begin.
    """
    for i, sp in enumerate(subpasses):
        begin = sp.get("begin_eid", 0)
        end = sp.get("end_eid", begin)
        if begin <= eid <= end:
            return i

    # Proximity fallback: find nearest subpass with begin > eid
    best_idx: int | None = None
    best_gap = 201  # threshold
    for i, sp in enumerate(subpasses):
        begin = sp.get("begin_eid", 0)
        if begin > eid:
            gap = begin - eid
            if gap < best_gap:
                best_gap = gap
                best_idx = i
    return best_idx


def _add_edges_from_descriptors(
    subpasses: list[dict],
    rt_usage: dict,
    edges: list[dict],
    edge_set: set[tuple[int, int]],
) -> None:
    """Build edges by matching descriptor resource IDs against RT write sets.

    For each subpass's first draw call, the descriptors tell us which actual
    resource IDs are bound as textures.  If any of those resource IDs matches
    a render target written by an earlier subpass, that's a read dependency.

    This catches dependencies that the GLES ``usage`` API misses.
    """
    desc_map = rt_usage.get("_descriptors")
    if not desc_map or not isinstance(desc_map, dict):
        return

    write_sets = _get_write_sets(subpasses)

    # resource_id -> list of subpass indices that write it (as RT)
    rid_to_writers: dict[int, list[int]] = {}
    for i, writes in enumerate(write_sets):
        for rid in writes:
            rid_to_writers.setdefault(rid, []).append(i)

    for sp_idx, sp in enumerate(subpasses):
        eid_key = str(sp.get("begin_eid", 0))
        bound_rids = desc_map.get(eid_key)
        if not bound_rids:
            continue

        for rid in bound_rids:
            writers = rid_to_writers.get(rid)
            if not writers:
                continue
            # Find the latest writer before this subpass
            writer = None
            for w in reversed(writers):
                if w < sp_idx:
                    writer = w
                    break
            if writer is None:
                continue
            if (writer, sp_idx) in edge_set:
                continue
            # Skip if same coarse pass
            if (subpasses[writer].get("pass_idx") is not None
                    and subpasses[writer]["pass_idx"] == subpasses[sp_idx].get("pass_idx")):
                continue
            # Skip if reader also writes to this resource — RT reuse, not a
            # real texture read.  GLES descriptors often return wrong resource
            # IDs (e.g., depth buffer ID leaking into the descriptor set).
            if rid in write_sets[sp_idx]:
                continue

            rt_name = _short_rt_name(write_sets[writer].get(rid, ""))
            edge_set.add((writer, sp_idx))
            edges.append({"src": writer, "dst": sp_idx, "type": "rt_flow", "label": rt_name})


def _add_edges_from_rt_usage(
    subpasses: list[dict],
    rt_usage: dict,
    edges: list[dict],
    edge_set: set[tuple[int, int]],
) -> None:
    """Build edges by finding which subpasses read render targets written by others.

    For each RT resource:
      1. Find subpasses that write to it (from render target assignments).
      2. Find subpasses that read from it (from usage events, excluding write-type usage).
      3. Connect the latest writer before each reader.
    """
    write_sets = _get_write_sets(subpasses)

    # resource_id -> sorted list of subpass indices that write to it
    rid_to_writers: dict[int, list[int]] = {}
    for i, writes in enumerate(write_sets):
        for rid in writes:
            rid_to_writers.setdefault(rid, []).append(i)

    _WRITE_USAGE = ("RenderTarget", "DepthStencil", "StreamOut", "Clear", "Copy")
    # Also discover writers from usage entries' ColorTarget events.
    # This catches RTs like _BloomMipUp0 that aren't in any subpass's
    # color_targets because Bloom's 13 passes are merged into one node.
    _WRITE_USAGE_TYPES = ("ColorTarget", "DepthStencilTarget")

    for rid_str, usage_data in rt_usage.items():
        try:
            rid = int(rid_str)
        except (ValueError, TypeError):
            continue

        entries = usage_data.get("entries") or []
        rt_name = _short_rt_name(usage_data.get("name", ""))

        # Augment rid_to_writers from usage entries with write-type events
        for entry in entries:
            usage_type = entry.get("usage", "")
            if any(wt in usage_type for wt in _WRITE_USAGE_TYPES):
                sp_idx = _find_subpass_for_eid(subpasses, entry.get("eid", 0))
                if sp_idx is not None and sp_idx not in (rid_to_writers.get(rid) or []):
                    rid_to_writers.setdefault(rid, []).append(sp_idx)

        writers = rid_to_writers.get(rid)
        if not writers:
            continue

        # Find reader subpasses (events with non-write usage type)
        reader_subpasses: set[int] = set()
        for entry in entries:
            usage_type = entry.get("usage", "")
            if any(w in usage_type for w in _WRITE_USAGE):
                continue
            sp_idx = _find_subpass_for_eid(subpasses, entry.get("eid", 0))
            if sp_idx is not None:
                reader_subpasses.add(sp_idx)

        # For each reader, find the latest writer before it
        for reader in reader_subpasses:
            writer = None
            for w in reversed(writers):
                if w < reader:
                    writer = w
                    break
            if writer is None:
                continue
            if (writer, reader) in edge_set:
                continue
            # Skip if same coarse pass (already connected by sequential edges)
            if (subpasses[writer].get("pass_idx") is not None
                    and subpasses[writer]["pass_idx"] == subpasses[reader].get("pass_idx")):
                continue
            # Skip if reader also writes to this resource — indicates RT reuse
            # (e.g., depth buffer shared across independent rendering stages),
            # not a data dependency. Real cross-pass reads show up in descriptors.
            if rid in write_sets[reader]:
                continue

            edge_set.add((writer, reader))
            edges.append({"src": writer, "dst": reader, "type": "rt_flow", "label": rt_name})


def _build_dependency_edges(
    subpasses: list[dict],
    nodes: list[dict],
    summary: dict,
    rt_usage: dict | None = None,
) -> list[dict]:
    """Build edges based on actual resource dependencies, not sequential order.

    Strategies (in preference order):
      1. pass_deps.edges — explicit dependency edges from rdc (Vulkan)
      2. pass_deps.per_pass reads/writes — resource flow analysis
      3. RT resource usage — per-resource usage events from daemon
      4. Shared render target resource IDs — heuristic fallback

    Strategy 3 is the most reliable for GLES captures where pass_deps
    is empty: it queries the daemon for which events read/write each RT.
    """
    edges: list[dict] = []
    edge_set: set[tuple[int, int]] = set()

    # ── A: Sequential edges within same coarse pass ──
    for i in range(len(subpasses) - 1):
        a, b = subpasses[i], subpasses[i + 1]
        if (a.get("pass_idx") is not None
                and a["pass_idx"] == b.get("pass_idx")):
            edge_set.add((i, i + 1))
            edges.append({"src": i, "dst": i + 1, "type": "sequential", "label": ""})

    # ── B: Cross-pass edges ──
    pass_deps = summary.get("pass_deps")
    coarse_passes = _unwrap(summary.get("passes"), "passes") or []

    dep_edges: list = []
    per_pass: list = []
    if isinstance(pass_deps, dict):
        dep_edges = pass_deps.get("edges") or []
        per_pass = pass_deps.get("per_pass") or []

    if dep_edges:
        _add_edges_from_dep_edges(subpasses, coarse_passes, dep_edges, edges, edge_set)
    elif per_pass:
        _add_edges_from_per_pass(subpasses, coarse_passes, per_pass, edges, edge_set)
    elif rt_usage:
        # B3: usage-based and descriptor-based detection complement each other
        _add_edges_from_rt_usage(subpasses, rt_usage, edges, edge_set)
        _add_edges_from_descriptors(subpasses, rt_usage, edges, edge_set)
        # C: RT name similarity matching (catches edges invisible to usage/descriptors)
        _add_edges_from_rt_name_similarity(subpasses, edges, edge_set)
        # D: unconsumed RT forward propagation (catches SetGlobalTexture bindings)
        _add_edges_from_unconsumed_rts(subpasses, edges, edge_set)
    else:
        _add_edges_from_shared_rts(subpasses, edges, edge_set)

    return edges


def _get_write_sets(subpasses: list[dict]) -> list[dict[int, str]]:
    """Collect {resource_id: resource_name} for each subpass's render targets."""
    write_sets: list[dict[int, str]] = []
    for sp in subpasses:
        writes: dict[int, str] = {}
        for ct in (sp.get("color_targets") or []):
            if isinstance(ct, dict) and ct.get("id"):
                writes[ct["id"]] = ct.get("name", "")
        dt = sp.get("depth_target")
        if isinstance(dt, dict) and dt.get("id"):
            writes[dt["id"]] = dt.get("name", "")
        write_sets.append(writes)
    return write_sets


def _add_edges_from_dep_edges(
    subpasses: list[dict],
    coarse_passes: list,
    dep_edges: list,
    edges: list[dict],
    edge_set: set[tuple[int, int]],
) -> None:
    """Strategy 1: Use explicit dependency edges from pass_deps."""
    # Map coarse pass name -> index
    name_to_idx: dict[str, int] = {}
    for i, cp in enumerate(coarse_passes):
        if isinstance(cp, dict):
            name_to_idx[cp.get("name", "")] = i

    # Map coarse pass index -> subpass indices
    coarse_to_subs: dict[int, list[int]] = {}
    for i, sp in enumerate(subpasses):
        pi = sp.get("pass_idx")
        if pi is not None:
            coarse_to_subs.setdefault(pi, []).append(i)

    write_sets = _get_write_sets(subpasses)

    for dep in dep_edges:
        if not isinstance(dep, dict):
            continue
        src_ci = name_to_idx.get(dep.get("src", ""))
        dst_ci = name_to_idx.get(dep.get("dst", ""))
        if src_ci is None or dst_ci is None:
            continue
        src_subs = coarse_to_subs.get(src_ci, [])
        dst_subs = coarse_to_subs.get(dst_ci, [])
        if not src_subs or not dst_subs:
            continue

        # Connect last subpass of source to first subpass of destination
        src_sub = max(src_subs)
        dst_sub = min(dst_subs)
        if (src_sub, dst_sub) in edge_set:
            continue
        edge_set.add((src_sub, dst_sub))

        # Label from shared resource names
        res_ids = dep.get("resources") or []
        label = ""
        for rid in res_ids:
            name = write_sets[src_sub].get(rid, "")
            if name:
                label = _short_rt_name(name)
                break
        if not label and res_ids:
            label = f"{len(res_ids)} res"

        edges.append({"src": src_sub, "dst": dst_sub, "type": "rt_flow", "label": label})


def _add_edges_from_per_pass(
    subpasses: list[dict],
    coarse_passes: list,
    per_pass: list,
    edges: list[dict],
    edge_set: set[tuple[int, int]],
) -> None:
    """Strategy 2: Build edges from per_pass reads/writes data."""
    # Map coarse pass name -> index
    name_to_idx: dict[str, int] = {}
    for i, cp in enumerate(coarse_passes):
        if isinstance(cp, dict):
            name_to_idx[cp.get("name", "")] = i

    # Map coarse pass index -> subpass indices
    coarse_to_subs: dict[int, list[int]] = {}
    for i, sp in enumerate(subpasses):
        pi = sp.get("pass_idx")
        if pi is not None:
            coarse_to_subs.setdefault(pi, []).append(i)

    write_sets = _get_write_sets(subpasses)

    # Build resource_id -> writer coarse pass index
    res_writers: dict[int, int] = {}
    for pp in per_pass:
        if not isinstance(pp, dict):
            continue
        ci = name_to_idx.get(pp.get("name", ""))
        if ci is None:
            continue
        for rid in (pp.get("writes") or []):
            res_writers[rid] = ci

    # For each pass, check if it reads a resource written by another pass
    for pp in per_pass:
        if not isinstance(pp, dict):
            continue
        dst_ci = name_to_idx.get(pp.get("name", ""))
        if dst_ci is None:
            continue
        for rid in (pp.get("reads") or []):
            src_ci = res_writers.get(rid)
            if src_ci is None or src_ci == dst_ci:
                continue
            # Connect last subpass of writer to first subpass of reader
            src_subs = coarse_to_subs.get(src_ci, [])
            dst_subs = coarse_to_subs.get(dst_ci, [])
            if not src_subs or not dst_subs:
                continue
            src_sub = max(src_subs)
            dst_sub = min(dst_subs)
            if (src_sub, dst_sub) in edge_set:
                continue
            edge_set.add((src_sub, dst_sub))

            label = ""
            name = write_sets[src_sub].get(rid, "")
            if name:
                label = _short_rt_name(name)

            edges.append({"src": src_sub, "dst": dst_sub, "type": "rt_flow", "label": label})


_RT_STOPWORDS = frozenset({
    "tex2d", "tex3d", "texcube", "texture", "srgb", "unorm", "sfloat",
    "linear", "attachment", "rt", "buffer",
})
_DIM_RE = re.compile(r"^\d+x\d+$")
_FMT_RE = re.compile(r"^[RGBAD]\d+", re.IGNORECASE)


def _tokenize_rt_name(name: str) -> set[str]:
    """Split an RT resource name into semantic tokens for similarity matching.

    Strips leading underscores, splits on underscores and CamelCase boundaries,
    filters out dimension patterns (1059x489), format tokens (R8G8B8A8), and
    common stopwords (Tex2D, SRGB, etc.).
    """
    if not name:
        return set()
    n = name.lstrip("_")
    # Split on underscores first
    parts: list[str] = []
    for seg in n.split("_"):
        # CamelCase split
        tokens = re.findall(r"[A-Z]?[a-z]+|[A-Z]+(?=[A-Z][a-z]|\d|\b)", seg)
        if tokens:
            parts.extend(tokens)
        elif seg:
            parts.append(seg)

    result: set[str] = set()
    for p in parts:
        low = p.lower()
        if _DIM_RE.match(low):
            continue
        if _FMT_RE.match(p) and len(p) >= 4:
            continue
        if low in _RT_STOPWORDS:
            continue
        if len(low) <= 1:
            continue
        result.add(low)
    return result


def _add_edges_from_rt_name_similarity(
    subpasses: list[dict],
    edges: list[dict],
    edge_set: set[tuple[int, int]],
) -> None:
    """Strategy C: Connect passes whose RT names show a subset relationship.

    Example: _HairShadowTexture tokens {hair, shadow} is a strict subset of
    _BlurredHairShadowTexture tokens {blurred, hair, shadow}, so
    Hair Shadow (1) -> Hair Shadow (2).

    Guard: equal sets (like Bloom's 13 RTs all tokenizing to {bloom}) do NOT
    trigger — only strict subsets.
    """
    # Build per-subpass token sets from color targets
    sp_tokens: list[set[str]] = []
    for sp in subpasses:
        merged: set[str] = set()
        for ct in (sp.get("color_targets") or []):
            if isinstance(ct, dict):
                merged |= _tokenize_rt_name(ct.get("name", ""))
        sp_tokens.append(merged)

    for i in range(len(subpasses)):
        if not sp_tokens[i]:
            continue
        for j in range(i + 1, len(subpasses)):
            if not sp_tokens[j]:
                continue
            if (i, j) in edge_set:
                continue
            # Skip same coarse pass
            pi_a = subpasses[i].get("pass_idx")
            pi_b = subpasses[j].get("pass_idx")
            if pi_a is not None and pi_a == pi_b:
                continue
            # Strict subset check (not equal)
            if sp_tokens[i] < sp_tokens[j]:
                edge_set.add((i, j))
                edges.append({
                    "src": i, "dst": j, "type": "rt_flow",
                    "label": "+".join(sorted(sp_tokens[j] - sp_tokens[i])),
                })
            elif sp_tokens[j] < sp_tokens[i]:
                edge_set.add((j, i))
                edges.append({
                    "src": j, "dst": i, "type": "rt_flow",
                    "label": "+".join(sorted(sp_tokens[i] - sp_tokens[j])),
                })


def _add_edges_from_unconsumed_rts(
    subpasses: list[dict],
    edges: list[dict],
    edge_set: set[tuple[int, int]],
) -> None:
    """Strategy D: Forward-propagate unconsumed render targets.

    Finds subpasses that write to color targets but have no outgoing rt_flow
    edge (their output is not consumed by any detected reader). Connects them
    to the nearest geometry-like pass within the next 5 subpasses.

    This catches globally-bound textures (SetGlobalTexture) that shaders read
    without named binding, invisible to both usage and descriptor queries.
    """
    _GEOM_KEYWORDS = ("draw", "opaque", "transparent", "forward", "main")

    # Find subpasses that are sources of existing rt_flow edges
    consumed_writers: set[int] = set()
    for e in edges:
        if e.get("type") in ("rt_flow",):
            consumed_writers.add(e["src"])

    for i, sp in enumerate(subpasses):
        if i in consumed_writers:
            continue
        if not (sp.get("color_targets") or []):
            continue

        # Search next 5 subpasses for a geometry pass
        for j in range(i + 1, min(i + 6, len(subpasses))):
            if (i, j) in edge_set:
                break  # already connected
            cand = subpasses[j]
            cand_name = (cand.get("name") or "").lower()
            if cand.get("draws", 0) >= 2 and any(k in cand_name for k in _GEOM_KEYWORDS):
                edge_set.add((i, j))
                ct_names = [
                    _short_rt_name(ct.get("name", ""))
                    for ct in (sp.get("color_targets") or [])
                    if isinstance(ct, dict)
                ]
                label = ct_names[0] if ct_names else ""
                edges.append({
                    "src": i, "dst": j, "type": "inferred",
                    "label": label,
                })
                break


def _add_edges_from_shared_rts(
    subpasses: list[dict],
    edges: list[dict],
    edge_set: set[tuple[int, int]],
) -> None:
    """Strategy 3: Heuristic — connect passes that share render target resource IDs."""
    write_sets = _get_write_sets(subpasses)

    for i in range(len(subpasses)):
        for j in range(i + 1, len(subpasses)):
            if (i, j) in edge_set:
                continue
            # Skip if same coarse pass (already handled by sequential edges)
            pi_a = subpasses[i].get("pass_idx")
            pi_b = subpasses[j].get("pass_idx")
            if pi_a is not None and pi_a == pi_b:
                continue
            # Check for shared resource IDs
            shared = set(write_sets[i].keys()) & set(write_sets[j].keys())
            if not shared:
                continue
            # Use first shared resource for label
            rid = next(iter(shared))
            label = _short_rt_name(write_sets[i].get(rid, "") or write_sets[j].get(rid, ""))
            edge_set.add((i, j))
            edges.append({"src": i, "dst": j, "type": "rt_flow", "label": label})


_TEMPLATE_DIR = Path(__file__).resolve().parent.parent.parent / "assets"


def _load_render_graph_template() -> str:
    """Load render graph HTML template from assets directory."""
    path = _TEMPLATE_DIR / "render_graph_template.html"
    return path.read_text(encoding="utf-8")


def generate_render_graph_html(
    summary: dict,
    pass_details: list,
    resource_names: dict[int, str],
    rt_usage: dict | None = None,
    assets_rel: str = "../html",
) -> str:
    """Generate interactive Render Graph HTML with sub-pass nodes and RT flow edges."""
    template = _load_render_graph_template()
    subpasses = _extract_subpasses(summary, pass_details)

    if not subpasses:
        graph_json = json.dumps({"nodes": [], "edges": []})
        return template.replace("/*GRAPH_DATA*/", graph_json).replace("__ASSETS__", assets_rel)

    # Build node data
    max_tri = max((sp.get("triangles", 0) for sp in subpasses), default=1) or 1
    nodes = []
    for i, sp in enumerate(subpasses):
        tri = sp.get("triangles", 0)
        draws = sp.get("draws", 0)
        dispatches = sp.get("dispatches", 0)

        # RT info for display
        color_targets = sp.get("color_targets") or []
        depth_target = sp.get("depth_target")
        ct_list = []
        for ct in color_targets:
            if isinstance(ct, dict):
                ct_list.append({
                    "name": _short_rt_name(ct.get("name", "")),
                    "format": ct.get("format", ""),
                })
        dt = None
        if isinstance(depth_target, dict):
            dt = {
                "name": _short_rt_name(depth_target.get("name", "")),
                "format": depth_target.get("format", ""),
            }

        nodes.append({
            "id": i,
            "name": sp.get("display_name") or sp.get("name", f"Pass #{i}"),
            "draws": draws,
            "dispatches": dispatches,
            "triangles": tri,
            "pass_idx": sp.get("pass_idx"),
            "begin_eid": sp.get("begin_eid", 0),
            "color_targets": ct_list,
            "depth_target": dt,
        })

    # Build edges based on actual resource dependencies (not linear chain)
    edge_data = _build_dependency_edges(subpasses, nodes, summary, rt_usage)

    graph_json = json.dumps({"nodes": nodes, "edges": edge_data}, ensure_ascii=False)
    return template.replace("/*GRAPH_DATA*/", graph_json).replace("__ASSETS__", assets_rel)


# ─────────────────────────────────────────────────────────────────────
# Main
# ─────────────────────────────────────────────────────────────────────

def main() -> None:
    parser = argparse.ArgumentParser(
        description="RDC capture data collection + computed analysis",
    )
    parser.add_argument("capture", type=Path, help="Path to .rdc capture file")
    parser.add_argument(
        "-j", "--workers", type=int, default=1,
        help="Number of parallel workers (default: 1, max: 8)",
    )
    args = parser.parse_args()

    capture = args.capture.resolve()
    if not capture.exists():
        print(f"ERROR: File not found: {capture}")
        sys.exit(1)

    num_workers = max(1, min(args.workers, 8))
    parallel = num_workers > 1

    out_dir = capture.parent / f"{capture.stem}-analysis"
    out_dir.mkdir(exist_ok=True)
    print(f"RDC Collect v{VERSION}")
    print(f"Capture: {capture}")
    print(f"Output:  {out_dir}")
    if parallel:
        print(f"Workers: {num_workers}")
    print()

    errors = ErrorCollector()
    timings: dict[str, float] = {}
    t_start = time.time()
    worker_pool: WorkerPool | None = None

    # Cleanup handler for Ctrl+C
    def _cleanup(signum=None, frame=None):
        print("\n\nInterrupted! Cleaning up sessions...")
        if worker_pool is not None:
            worker_pool.close_all()
        run_rdc("close", session=(MAIN_SESSION if parallel else None), timeout=10)
        sys.exit(1)

    signal.signal(signal.SIGINT, _cleanup)

    try:
        # ═══════════════════════════════════════════════════════════════
        # SERIAL PHASE (main session)
        # ═══════════════════════════════════════════════════════════════

        # ── Step 1: Open ──
        print("[Step 1] Opening capture ...")
        sess = MAIN_SESSION if parallel else None
        run_rdc("close", session=sess, timeout=10)
        out, err, rc = run_rdc("open", str(capture), session=sess)
        if rc != 0:
            print(f"  ERROR: rdc open failed: {err}")
            print("  Try running 'rdc doctor' to diagnose issues.")
            sys.exit(1)
        print(f"  Opened successfully.")

        # ── Step 2: Base data ──
        print("\n[Step 2] Collecting base data ...")
        t0 = time.time()
        sess = MAIN_SESSION if parallel else None
        summary = collect_base(errors, session=sess)
        summary["_meta"] = {
            "capture": str(capture),
            "collected_at": time.strftime("%Y-%m-%dT%H:%M:%S"),
            "version": VERSION,
        }
        write_json(out_dir / "summary.json", summary)
        timings["base"] = time.time() - t0

        # ── Step 3: Pass details ──
        print("\n[Step 3] Collecting pass details ...")
        t0 = time.time()
        pass_details = collect_pass_details(summary, errors, session=sess)
        write_json(out_dir / "pass_details.json", pass_details)
        timings["pass_details"] = time.time() - t0

        # ── Step 3.5: RT resource usage (for dependency graph) ──
        print("\n[Step 3.5] Collecting render target usage ...")
        t0 = time.time()
        rt_sess = MAIN_SESSION if parallel else "default"
        rt_usage = collect_rt_usage(
            pass_details, errors, session=rt_sess, summary=summary,
        )
        write_json(out_dir / "rt_usage.json", rt_usage)
        timings["rt_usage"] = time.time() - t0

        # ═══════════════════════════════════════════════════════════════
        # PARALLEL PHASE (worker sessions)
        # ═══════════════════════════════════════════════════════════════

        draw_eids = _get_draw_eids(summary)

        if parallel:
            # Start worker pool
            print(f"\n  Starting {num_workers} worker sessions ...")
            t0 = time.time()
            worker_pool = WorkerPool(num_workers, str(capture))
            active_workers = worker_pool.open_all()
            timings["worker_open"] = time.time() - t0

            if not active_workers:
                print("  ERROR: No workers started, falling back to serial")
                parallel = False

        if parallel and active_workers:
            # ── Step 4: Per-draw pipeline+bindings (parallel) ──
            print(f"\n[Step 4] Collecting pipeline+bindings for {len(draw_eids)} draws ({len(active_workers)} workers) ...")
            t0 = time.time()
            shards = _shard_list(draw_eids, len(active_workers))
            progress = Progress(len(draw_eids), "Per-draw (pipeline+bindings)")
            all_pipelines: dict = {}
            all_bindings: dict = {}

            with ThreadPoolExecutor(max_workers=len(active_workers)) as executor:
                futures = {}
                for session, shard in zip(active_workers, shards):
                    if shard:
                        futures[executor.submit(
                            _collect_per_draw_shard, session, shard, progress, errors
                        )] = session

                for future in as_completed(futures):
                    try:
                        pipe_part, bind_part = future.result()
                        all_pipelines.update(pipe_part)
                        all_bindings.update(bind_part)
                    except Exception as exc:
                        errors.append({"phase": "per_draw", "error": str(exc)})

            progress.done()
            pipelines, bindings = all_pipelines, all_bindings
            write_json(out_dir / "pipelines.json", pipelines)
            write_json(out_dir / "bindings.json", bindings)
            timings["per_draw"] = time.time() - t0

            # ── Step 5: Shader disassembly (on main session — cache is per-session) ──
            print(f"\n[Step 5] Collecting shader disassembly (main session) ...")
            t0 = time.time()
            shader_disasm = collect_shaders_disasm(
                out_dir, errors, session=MAIN_SESSION,
            )
            write_json(out_dir / "shader_disasm.json", shader_disasm)
            timings["shader_disasm"] = time.time() - t0

            # ── Step 6: Resource details (parallel) ──
            resource_tasks = _get_resource_tasks(summary)
            print(f"\n[Step 6] Collecting resource details for {len(resource_tasks)} resources ({len(active_workers)} workers) ...")
            t0 = time.time()
            res_shards = _shard_list(resource_tasks, len(active_workers))
            res_progress = Progress(len(resource_tasks), "Resource details")
            all_resource_details: dict = {}

            with ThreadPoolExecutor(max_workers=len(active_workers)) as executor:
                futures = {}
                for session, shard in zip(active_workers, res_shards):
                    if shard:
                        futures[executor.submit(
                            _collect_resources_shard, session, shard, res_progress, errors
                        )] = session

                for future in as_completed(futures):
                    try:
                        all_resource_details.update(future.result())
                    except Exception as exc:
                        errors.append({"phase": "resource_details", "error": str(exc)})

            res_progress.done()
            resource_details = all_resource_details
            write_json(out_dir / "resource_details.json", resource_details)
            timings["resource_details"] = time.time() - t0

            # Close worker sessions
            print("\n  Closing worker sessions ...")
            worker_pool.close_all()
            worker_pool = None

        else:
            # ── Serial fallback (workers=1) ──
            print(f"\n[Step 4] Collecting pipeline+bindings for {len(draw_eids)} draws ...")
            t0 = time.time()
            pipelines, bindings = collect_per_draw(draw_eids, errors)
            write_json(out_dir / "pipelines.json", pipelines)
            write_json(out_dir / "bindings.json", bindings)
            timings["per_draw"] = time.time() - t0

            print("\n[Step 5] Collecting shader disassembly ...")
            t0 = time.time()
            shader_disasm = collect_shaders_disasm(out_dir, errors)
            write_json(out_dir / "shader_disasm.json", shader_disasm)
            timings["shader_disasm"] = time.time() - t0

            print("\n[Step 6] Collecting resource details ...")
            t0 = time.time()
            resource_details = collect_resource_details(summary, errors)
            write_json(out_dir / "resource_details.json", resource_details)
            timings["resource_details"] = time.time() - t0

        # ═══════════════════════════════════════════════════════════════
        # POST-MERGE PHASE
        # ═══════════════════════════════════════════════════════════════

        # ── Step 7: Computed analysis ──
        print("\n[Step 7] Running computed analysis ...")
        t0 = time.time()
        computed = compute_analysis(summary, pass_details, pipelines, resource_details)
        write_json(out_dir / "computed.json", computed)
        timings["computed"] = time.time() - t0
        print(f"  Done: computed analysis")

        # ── Step 8: Render Graph HTML ──
        print("\n[Step 8] Generating Render Graph ...")
        t0 = time.time()
        res_list = _unwrap(summary.get("resources"), "resources") or []
        res_names: dict[int, str] = {}
        for r in res_list:
            if isinstance(r, dict) and "id" in r:
                res_names[r["id"]] = r.get("name", f"Resource {r['id']}")
        html_assets_dir = Path(__file__).resolve().parent.parent.parent / "assets"
        assets_rel = os.path.relpath(html_assets_dir, out_dir).replace("\\", "/")
        html = generate_render_graph_html(summary, pass_details, res_names, rt_usage, assets_rel=assets_rel)
        (out_dir / "render_graph.html").write_text(html, encoding="utf-8")
        timings["render_graph"] = time.time() - t0
        print(f"  Done: render_graph.html")

        # ── Step 9: Close main session ──
        print("\n[Step 9] Closing session ...")
        run_rdc("close", session=sess)

    finally:
        # Safety net: close any remaining sessions
        if worker_pool is not None:
            worker_pool.close_all()

    # ── Collection metadata ──
    t_total = time.time() - t_start
    collection_meta = {
        "version": VERSION,
        "capture": str(capture),
        "workers": num_workers,
        "parallelized": num_workers > 1,
        "started_at": time.strftime(
            "%Y-%m-%dT%H:%M:%S",
            time.localtime(t_start),
        ),
        "completed_at": time.strftime("%Y-%m-%dT%H:%M:%S"),
        "total_seconds": round(t_total, 1),
        "timings": {k: round(v, 1) for k, v in timings.items()},
        "error_count": len(errors),
        "errors": errors.errors[:100],
    }
    write_json(out_dir / "_collection.json", collection_meta)

    # ── Summary ──
    print(f"\n{'='*60}")
    print(f"  Collection complete!")
    print(f"  Output:     {out_dir}")
    print(f"  Total time: {t_total:.1f}s")
    if num_workers > 1:
        print(f"  Workers:    {num_workers}")
    print(f"  Errors:     {len(errors)}")
    print(f"  Files:")
    for f in sorted(out_dir.iterdir()):
        size = f.stat().st_size
        if size > 1024 * 1024:
            s = f"{size / 1024 / 1024:.1f} MB"
        elif size > 1024:
            s = f"{size / 1024:.1f} KB"
        else:
            s = f"{size} B"
        print(f"    {f.name:30s} {s:>10s}")
    print(f"{'='*60}")


if __name__ == "__main__":
    main()
