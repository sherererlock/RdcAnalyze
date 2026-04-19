# -*- coding: utf-8 -*-
"""Data collection functions and parallel worker infrastructure."""

from __future__ import annotations

import bisect
import threading
from concurrent.futures import ThreadPoolExecutor, as_completed
from pathlib import Path

from rpc import (
    run_rdc, run_rdc_json, _rpc_call, _unwrap,
    Progress, ErrorCollector, SESSION_PREFIX,
)
from render_graph import _extract_subpasses
from shared import unwrap


# ─────────────────────────────────────────────────────────────────────
# Base collection
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


def _get_dispatch_eids(summary: dict) -> list[int]:
    """Extract compute dispatch EIDs from summary.events."""
    events = _unwrap(summary.get("events"), "events")
    if not events or not isinstance(events, list):
        return []
    return [e["eid"] for e in events if isinstance(e, dict) and e.get("type") == "Dispatch"]


# ─────────────────────────────────────────────────────────────────────
# Per-draw & per-resource collection
# ─────────────────────────────────────────────────────────────────────

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

    infos: dict[int, dict] = {}
    for sid in shader_ids:
        info = _rpc_call(sess_name, "shader_list_info", {"id": sid}, timeout=30)
        if info and isinstance(info, dict):
            infos[sid] = info

    eid_to_shaders: dict[int, dict[str, int]] = {}
    for sid, info in infos.items():
        stages = info.get("stages") or []
        eids = info.get("eids") or []
        for stage in stages:
            for eid in eids:
                eid_to_shaders.setdefault(eid, {})[stage] = sid

    # Separate graphics (VS/PS) pairs from compute (CS) shaders
    pair_map: dict[tuple[int, int], list[int]] = {}
    cs_map: dict[int, list[int]] = {}
    for eid, stage_map in sorted(eid_to_shaders.items()):
        cs = stage_map.get("cs", 0)
        vs = stage_map.get("vs", 0)
        ps = stage_map.get("ps", 0)
        if cs and not vs and not ps:
            cs_map.setdefault(cs, []).append(eid)
        else:
            pair_map.setdefault((vs, ps), []).append(eid)

    shaders_dir = out_dir / "shaders"
    shaders_dir.mkdir(exist_ok=True)

    def _fetch_disasm(sid: int) -> str | None:
        result = _rpc_call(sess_name, "shader_list_disasm", {"id": sid}, timeout=60)
        if result and isinstance(result, dict):
            return result.get("disasm", "")
        return None

    results: dict = {}
    total_items = len(pair_map) + len(cs_map)
    prog = Progress(total_items, "Shader files")

    # Graphics VS/PS pairs
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

            disasm_text = _fetch_disasm(sid)
            if disasm_text:
                lines.append(disasm_text)
            else:
                lines.append("// (disassembly unavailable)")
                errors.append({"phase": "shader_disasm", "shader_id": sid, "error": "disasm failed"})

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

    # Compute shaders
    for cs_id, eids in sorted(cs_map.items()):
        cs_name = f"shader_cs_{cs_id}"
        prog.tick(cs_name)

        info = infos.get(cs_id, {})
        entry_name = info.get("entry", "main")

        lines = [
            f"// {'=' * 60}",
            f"// Compute Shader (ID: {cs_id})  Entry: {entry_name}",
            f"// Used by {len(eids)} dispatch(es): EID {', '.join(str(e) for e in eids[:20])}",
        ]
        if len(eids) > 20:
            lines.append(f"//   ... +{len(eids) - 20} more")
        lines.append(f"// {'=' * 60}")
        lines.append("")

        disasm_text = _fetch_disasm(cs_id)
        if disasm_text:
            lines.append(disasm_text)
        else:
            lines.append("// (disassembly unavailable)")
            errors.append({"phase": "shader_disasm", "shader_id": cs_id, "error": "disasm failed"})

        shader_file = shaders_dir / f"{cs_name}.shader"
        shader_file.write_text("\n".join(lines), encoding="utf-8")

        rel_path = f"shaders/{cs_name}.shader"
        results[f"cs_{cs_id}"] = {
            "cs_id": cs_id,
            "eids": eids,
            "uses": len(eids),
            "file": rel_path,
        }

    prog.done()
    gfx_count = len(pair_map)
    cs_count = len(cs_map)
    print(f"    Saved {len(results)} .shader files ({gfx_count} graphics, {cs_count} compute) to {shaders_dir}/")
    return results


def collect_resource_details(summary: dict, errors: ErrorCollector) -> dict:
    """Collect per-resource metadata via VFS. Returns {res_id_str: data}."""
    resources = _unwrap(summary.get("resources"), "resources")
    if not resources or not isinstance(resources, list):
        print("  Skipping resource details (no resources data)")
        return {}

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
    """Collect resource usage events and per-subpass descriptor bindings."""
    sess_name = session or "default"

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
        draws_list = (summary.get("draws") or []) if summary else []
        all_draw_eids = sorted(d["eid"] for d in draws_list if isinstance(d, dict) and "eid" in d)

        desc_map: dict[str, list[int]] = {}
        sp_all_draws: list[tuple[int, list[int]]] = []
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
