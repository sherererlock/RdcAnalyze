# -*- coding: utf-8 -*-
"""Render Graph: sub-pass extraction, dependency edge building, HTML generation."""

from __future__ import annotations

import json
import re
from collections import Counter
from pathlib import Path

from rpc import _unwrap
from shared import classify_pass_stage, detect_bloom_chain, STAGE_COLORS


# ─────────────────────────────────────────────────────────────────────
# Constants
# ─────────────────────────────────────────────────────────────────────

_NOISE_PATTERNS = ("GUI.Repaint", "UIR.DrawChain", "EditorLoop",
                   "UGUI.Rendering", "GUITexture", "PlayerEndOfFrame")

_BATCH_NAMES = frozenset({
    "RenderLoop.DrawSRPBatcher", "RenderLoop.Draw",
    "Canvas.RenderSubBatch", "Canvas.RenderOverlays",
})

_RT_STOPWORDS = frozenset({
    "tex2d", "tex3d", "texcube", "texture", "srgb", "unorm", "sfloat",
    "linear", "attachment", "rt", "buffer",
})
_DIM_RE = re.compile(r"^\d+x\d+$")
_FMT_RE = re.compile(r"^[RGBAD]\d+", re.IGNORECASE)

_TEMPLATE_DIR = Path(__file__).resolve().parent.parent.parent / "assets"


# ─────────────────────────────────────────────────────────────────────
# Sub-pass extraction
# ─────────────────────────────────────────────────────────────────────

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
    dispatch_eids: set[int] = {
        e["eid"] for e in events
        if isinstance(e, dict) and e.get("type") == "Dispatch"
    }

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
                    "dispatches": 0,
                    "triangles": 0,
                    "_noise": False,
                })

        if noise_depth is None:
            if eid in draw_map:
                dr = draw_map[eid]
                for m in stack:
                    if not m.get("_noise"):
                        m["draws"] += 1
                        m["triangles"] += dr.get("triangles", 0)
            elif eid in dispatch_eids:
                for m in stack:
                    if not m.get("_noise"):
                        m["dispatches"] = m.get("dispatches", 0) + 1

    # Close any unclosed markers
    last_eid = events[-1]["eid"] if events else 0
    while stack:
        m = stack.pop()
        m["end_eid"] = last_eid
        if not m.get("_noise"):
            markers.append(m)

    # ── Step 2: filter to meaningful candidates ──
    candidates = [
        m for m in markers
        if (m["draws"] > 0 or m.get("dispatches", 0) > 0) and m["name"] not in _BATCH_NAMES
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
    n = name.lstrip("_")
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
    """
    for i, sp in enumerate(subpasses):
        begin = sp.get("begin_eid", 0)
        end = sp.get("end_eid", begin)
        if begin <= eid <= end:
            return i

    best_idx: int | None = None
    best_gap = 201
    for i, sp in enumerate(subpasses):
        begin = sp.get("begin_eid", 0)
        if begin > eid:
            gap = begin - eid
            if gap < best_gap:
                best_gap = gap
                best_idx = i
    return best_idx


# ─────────────────────────────────────────────────────────────────────
# Dependency edge building
# ─────────────────────────────────────────────────────────────────────

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


def _add_edges_from_descriptors(
    subpasses: list[dict],
    rt_usage: dict,
    edges: list[dict],
    edge_set: set[tuple[int, int]],
) -> None:
    """Build edges by matching descriptor resource IDs against RT write sets."""
    desc_map = rt_usage.get("_descriptors")
    if not desc_map or not isinstance(desc_map, dict):
        return

    write_sets = _get_write_sets(subpasses)

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
            writer = None
            for w in reversed(writers):
                if w < sp_idx:
                    writer = w
                    break
            if writer is None:
                continue
            if (writer, sp_idx) in edge_set:
                continue
            if (subpasses[writer].get("pass_idx") is not None
                    and subpasses[writer]["pass_idx"] == subpasses[sp_idx].get("pass_idx")):
                continue
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
    """Build edges by finding which subpasses read render targets written by others."""
    write_sets = _get_write_sets(subpasses)

    rid_to_writers: dict[int, list[int]] = {}
    for i, writes in enumerate(write_sets):
        for rid in writes:
            rid_to_writers.setdefault(rid, []).append(i)

    _WRITE_USAGE = ("RenderTarget", "DepthStencil", "StreamOut", "Clear", "Copy")
    _WRITE_USAGE_TYPES = ("ColorTarget", "DepthStencilTarget")

    for rid_str, usage_data in rt_usage.items():
        try:
            rid = int(rid_str)
        except (ValueError, TypeError):
            continue

        entries = usage_data.get("entries") or []
        rt_name = _short_rt_name(usage_data.get("name", ""))

        for entry in entries:
            usage_type = entry.get("usage", "")
            if any(wt in usage_type for wt in _WRITE_USAGE_TYPES):
                sp_idx = _find_subpass_for_eid(subpasses, entry.get("eid", 0))
                if sp_idx is not None and sp_idx not in (rid_to_writers.get(rid) or []):
                    rid_to_writers.setdefault(rid, []).append(sp_idx)

        writers = rid_to_writers.get(rid)
        if not writers:
            continue

        reader_subpasses: set[int] = set()
        for entry in entries:
            usage_type = entry.get("usage", "")
            if any(w in usage_type for w in _WRITE_USAGE):
                continue
            sp_idx = _find_subpass_for_eid(subpasses, entry.get("eid", 0))
            if sp_idx is not None:
                reader_subpasses.add(sp_idx)

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
            if (subpasses[writer].get("pass_idx") is not None
                    and subpasses[writer]["pass_idx"] == subpasses[reader].get("pass_idx")):
                continue
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
    """Build edges based on actual resource dependencies, not sequential order."""
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

    seq_count = len(edges)

    if dep_edges:
        _add_edges_from_dep_edges(subpasses, coarse_passes, dep_edges, edges, edge_set)
    if len(edges) == seq_count and per_pass:
        _add_edges_from_per_pass(subpasses, coarse_passes, per_pass, edges, edge_set)
    if len(edges) == seq_count and rt_usage:
        _add_edges_from_rt_usage(subpasses, rt_usage, edges, edge_set)
        _add_edges_from_descriptors(subpasses, rt_usage, edges, edge_set)
        _add_edges_from_rt_name_similarity(subpasses, edges, edge_set)
        _add_edges_from_unconsumed_rts(subpasses, edges, edge_set)
    if len(edges) == seq_count:
        _add_edges_from_shared_rts(subpasses, edges, edge_set)

    return edges


def _add_edges_from_dep_edges(
    subpasses: list[dict],
    coarse_passes: list,
    dep_edges: list,
    edges: list[dict],
    edge_set: set[tuple[int, int]],
) -> None:
    """Strategy 1: Use explicit dependency edges from pass_deps."""
    name_to_idx: dict[str, int] = {}
    for i, cp in enumerate(coarse_passes):
        if isinstance(cp, dict):
            name_to_idx[cp.get("name", "")] = i

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

        src_sub = max(src_subs)
        dst_sub = min(dst_subs)
        if (src_sub, dst_sub) in edge_set:
            continue
        edge_set.add((src_sub, dst_sub))

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
    name_to_idx: dict[str, int] = {}
    for i, cp in enumerate(coarse_passes):
        if isinstance(cp, dict):
            name_to_idx[cp.get("name", "")] = i

    coarse_to_subs: dict[int, list[int]] = {}
    for i, sp in enumerate(subpasses):
        pi = sp.get("pass_idx")
        if pi is not None:
            coarse_to_subs.setdefault(pi, []).append(i)

    write_sets = _get_write_sets(subpasses)

    res_writers: dict[int, int] = {}
    for pp in per_pass:
        if not isinstance(pp, dict):
            continue
        ci = name_to_idx.get(pp.get("name", ""))
        if ci is None:
            continue
        for rid in (pp.get("writes") or []):
            res_writers[rid] = ci

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


def _tokenize_rt_name(name: str) -> set[str]:
    """Split an RT resource name into semantic tokens for similarity matching."""
    if not name:
        return set()
    n = name.lstrip("_")
    parts: list[str] = []
    for seg in n.split("_"):
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
    """Strategy C: Connect passes whose RT names show a subset relationship."""
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
            pi_a = subpasses[i].get("pass_idx")
            pi_b = subpasses[j].get("pass_idx")
            if pi_a is not None and pi_a == pi_b:
                continue
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
    """Strategy D: Forward-propagate unconsumed render targets."""
    _GEOM_KEYWORDS = ("draw", "opaque", "transparent", "forward", "main")

    consumed_writers: set[int] = set()
    for e in edges:
        if e.get("type") in ("rt_flow",):
            consumed_writers.add(e["src"])

    for i, sp in enumerate(subpasses):
        if i in consumed_writers:
            continue
        if not (sp.get("color_targets") or []):
            continue

        for j in range(i + 1, min(i + 6, len(subpasses))):
            if (i, j) in edge_set:
                break
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
            pi_a = subpasses[i].get("pass_idx")
            pi_b = subpasses[j].get("pass_idx")
            if pi_a is not None and pi_a == pi_b:
                continue
            shared = set(write_sets[i].keys()) & set(write_sets[j].keys())
            if not shared:
                continue
            rid = next(iter(shared))
            label = _short_rt_name(write_sets[i].get(rid, "") or write_sets[j].get(rid, ""))
            edge_set.add((i, j))
            edges.append({"src": i, "dst": j, "type": "rt_flow", "label": label})


# ─────────────────────────────────────────────────────────────────────
# HTML generation
# ─────────────────────────────────────────────────────────────────────

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

    bloom = detect_bloom_chain(pass_details)
    bloom_names: set[str] = set(bloom["passes"]) if bloom else set()
    max_rt_area = 0
    for p in pass_details:
        for ct in (p.get("color_targets") or []):
            area = ct.get("width", 0) * ct.get("height", 0)
            if area > max_rt_area:
                max_rt_area = area

    stage_by_pass_idx: dict[int, str] = {}
    for pi, p in enumerate(pass_details):
        stage, _ = classify_pass_stage(
            p, all_passes=pass_details,
            bloom_pass_names=bloom_names, max_rt_area=max_rt_area,
        )
        stage_by_pass_idx[pi] = stage

    max_tri = max((sp.get("triangles", 0) for sp in subpasses), default=1) or 1
    nodes = []
    for i, sp in enumerate(subpasses):
        tri = sp.get("triangles", 0)
        draws = sp.get("draws", 0)
        dispatches = sp.get("dispatches", 0)

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

        pass_idx = sp.get("pass_idx")
        stage = stage_by_pass_idx.get(pass_idx, "Other") if pass_idx is not None else "Other"

        nodes.append({
            "id": i,
            "name": sp.get("display_name") or sp.get("name", f"Pass #{i}"),
            "draws": draws,
            "dispatches": dispatches,
            "triangles": tri,
            "pass_idx": pass_idx,
            "begin_eid": sp.get("begin_eid", 0),
            "color_targets": ct_list,
            "depth_target": dt,
            "stage": stage,
            "stageColor": STAGE_COLORS.get(stage, "#555f78"),
        })

    edge_data = _build_dependency_edges(subpasses, nodes, summary, rt_usage)

    graph_json = json.dumps({"nodes": nodes, "edges": edge_data}, ensure_ascii=False)
    return template.replace("/*GRAPH_DATA*/", graph_json).replace("__ASSETS__", assets_rel)
