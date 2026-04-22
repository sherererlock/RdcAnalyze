# -*- coding: utf-8 -*-
"""rdc_analyze.py - GPU Frame Performance Analysis Report Generator.

Reads a *-analysis/ directory produced by rdc_collect.py and generates
an interactive HTML performance report.

Usage:
    python\\python.exe rdc_analyze.py <analysis-dir>

Output:
    {analysis-dir}/performance_report.html
"""

from __future__ import annotations

import argparse
import json
import os
import re
import sys
from collections import Counter, defaultdict
from pathlib import Path

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))

from shared import (
    guess_bpp, unwrap, fmt_number, fmt_mb, rt_bytes,
    classify_pass_stage, detect_bloom_chain, detect_fullscreen_quad,
    detect_shader_patterns,
    analyze_spirv_instructions, estimate_register_pressure, deduplicate_shaders,
    STAGE_COLORS, write_json,
)


# ─────────────────────────────────────────────────────────────────────
# Data Loading
# ─────────────────────────────────────────────────────────────────────

def _load_json(path: Path) -> dict | list | None:
    if not path.exists():
        return None
    with open(path, "r", encoding="utf-8") as f:
        return json.load(f)


def load_analysis(analysis_dir: Path) -> dict:
    """Load all JSON files from an analysis directory."""
    data: dict = {}
    files = {
        "summary": "summary.json",
        "pass_details": "pass_details.json",
        "computed": "computed.json",
        "shader_disasm": "shader_disasm.json",
        "resource_details": "resource_details.json",
        "pipelines": "pipelines.json",
        "bindings": "bindings.json",
        "collection": "_collection.json",
    }
    json_dir = analysis_dir / "json"
    if not json_dir.is_dir():
        json_dir = analysis_dir
    for key, fname in files.items():
        data[key] = _load_json(json_dir / fname)
    data["analysis_dir"] = str(analysis_dir)
    data["shaders_dir"] = analysis_dir / "shaders"
    return data


# ─────────────────────────────────────────────────────────────────────
# Helpers
# ─────────────────────────────────────────────────────────────────────

_unwrap = unwrap  # alias for local readability


_fmt_number = fmt_number  # alias for local readability
_fmt_mb = fmt_mb


def _classify_pass(name: str, *, draws: int = -1, dispatches: int = -1) -> str:
    """Classify a pass by its name into a category."""
    lower = name.lower()
    if any(k in lower for k in ("compute", "dispatch", "cs ")):
        return "Compute"
    if any(k in lower for k in ("shadow", "shadowmap")):
        return "Shadow"
    if any(k in lower for k in ("depth", "prepass", "pre-pass", "zprepass")):
        return "DepthPrepass"
    if any(k in lower for k in ("gbuffer", "g-buffer")):
        return "GBuffer"
    if any(k in lower for k in ("bloom",)):
        return "PostProcess"
    if any(k in lower for k in ("uberpost", "postprocess", "post process", "tonemap", "fxaa", "smaa")):
        return "PostProcess"
    if any(k in lower for k in ("present", "blit", "finalblit")):
        return "Present"
    if any(k in lower for k in ("transparent",)):
        return "Transparent"
    if any(k in lower for k in ("opaque", "forward", "main", "draw")):
        return "Geometry"
    if any(k in lower for k in ("hair",)):
        return "Hair"
    if any(k in lower for k in ("scriptablerenderer",)):
        return "Geometry"
    if draws == 0 and dispatches > 0:
        return "Compute"
    return "Other"


_PASS_CAT_COLORS = {
    "Shadow": "#6366f1",
    "DepthPrepass": "#8b5cf6",
    "GBuffer": "#3b82f6",
    "Geometry": "#22b07a",
    "Hair": "#f59e0b",
    "Transparent": "#06b6d4",
    "Compute": "#6366f1",
    "PostProcess": "#d4a017",
    "Present": "#64748b",
    "Other": "#555f78",
}


# ─────────────────────────────────────────────────────────────────────
# Analysis Modules
# ─────────────────────────────────────────────────────────────────────

def analyze_frame_overview(data: dict) -> dict:
    summary = data.get("summary") or {}
    info = summary.get("info") or {}
    draws_list = _unwrap(summary.get("draws"), "draws") or []
    passes = _unwrap(summary.get("passes"), "passes") or []

    total_tri = sum(d.get("triangles", 0) for d in draws_list if isinstance(d, dict))
    draw_types: Counter = Counter()
    for d in draws_list:
        if isinstance(d, dict):
            draw_types[d.get("type", "unknown")] += 1

    # Detect resolution from pass_details
    pass_details = data.get("pass_details") or []
    resolution = ""
    for p in pass_details:
        for ct in (p.get("color_targets") or []):
            w, h = ct.get("width", 0), ct.get("height", 0)
            if w > 0 and h > 0:
                resolution = f"{w}x{h}"
                break
        if resolution:
            break

    # Detect rendering architecture
    pass_names = [p.get("name", "").lower() for p in passes]
    has_gbuffer = any("gbuffer" in n or "g-buffer" in n for n in pass_names)
    arch = "Deferred" if has_gbuffer else "Forward"

    events_list = _unwrap(summary.get("events"), "events") or []
    total_dispatches = sum(
        1 for e in events_list
        if isinstance(e, dict) and e.get("type") == "Dispatch"
    )

    return {
        "api": info.get("API", "Unknown"),
        "platform": info.get("machine_ident", "Unknown").strip(),
        "resolution": resolution,
        "events": info.get("Events", 0),
        "draw_calls": info.get("Draw Calls", ""),
        "total_draws": len(draws_list),
        "total_dispatches": total_dispatches,
        "total_triangles": total_tri,
        "draw_types": dict(draw_types),
        "pass_count": len(passes),
        "architecture": arch,
        "clears": info.get("Clears", 0),
    }


def analyze_pipeline(data: dict) -> dict:
    pass_details = data.get("pass_details") or []
    result = []
    for p in pass_details:
        p_draws = p.get("draws", 0)
        p_dispatches = p.get("dispatches", 0)
        cat = _classify_pass(p.get("name", ""), draws=p_draws, dispatches=p_dispatches)
        color_targets = p.get("color_targets") or []
        depth_target = p.get("depth_target")
        rt_info = []
        for ct in color_targets:
            rt_info.append({
                "name": ct.get("name", ""),
                "format": ct.get("format", ""),
                "dims": f"{ct.get('width', 0)}x{ct.get('height', 0)}",
            })
        if depth_target:
            rt_info.append({
                "name": depth_target.get("name", ""),
                "format": depth_target.get("format", ""),
                "dims": f"{depth_target.get('width', 0)}x{depth_target.get('height', 0)}",
                "is_depth": True,
            })
        result.append({
            "name": p.get("name", ""),
            "category": cat,
            "color": _PASS_CAT_COLORS.get(cat, "#555f78"),
            "draws": p.get("draws", 0),
            "dispatches": p.get("dispatches", 0),
            "triangles": p.get("triangles", 0),
            "begin_eid": p.get("begin_eid", 0),
            "end_eid": p.get("end_eid", 0),
            "render_targets": rt_info,
        })
    return {"passes": result}


def analyze_hotspots(data: dict) -> dict:
    summary = data.get("summary") or {}
    draws = _unwrap(summary.get("draws"), "draws") or []
    computed = data.get("computed") or {}

    # Top draws by triangle count
    sorted_draws = sorted(
        [d for d in draws if isinstance(d, dict)],
        key=lambda d: d.get("triangles", 0),
        reverse=True,
    )
    top_draws = sorted_draws[:15]
    max_tri = top_draws[0]["triangles"] if top_draws else 1

    # Per-pass triangle distribution
    tri_dist = computed.get("triangle_distribution", {})
    per_pass = tri_dist.get("per_pass", [])

    # Detect repeated mesh draws (same triangle count appearing in multiple passes)
    tri_counts: dict[int, list] = defaultdict(list)
    for d in draws:
        if isinstance(d, dict) and d.get("triangles", 0) > 1000:
            tri_counts[d["triangles"]].append(d)
    repeated_meshes = []
    for tri, draw_list in tri_counts.items():
        if len(draw_list) >= 3:
            markers = list({d.get("marker", "") for d in draw_list})
            repeated_meshes.append({
                "triangles": tri,
                "count": len(draw_list),
                "markers": markers,
                "eids": [d["eid"] for d in draw_list],
            })

    return {
        "top_draws": top_draws,
        "max_tri": max_tri,
        "per_pass": per_pass,
        "total_triangles": tri_dist.get("total", 0),
        "repeated_meshes": repeated_meshes,
    }


def analyze_bandwidth(data: dict) -> dict:
    pass_details = data.get("pass_details") or []
    passes_bw = []
    total_bw = 0.0
    bloom_bw = 0.0

    for p in pass_details:
        name = p.get("name", "")
        # Each pass: load (read) all targets + store (write) all targets
        # Without load/store ops, assume worst case: load + store for each target
        read_bytes = 0.0
        write_bytes = 0.0
        for ct in (p.get("color_targets") or []):
            b = rt_bytes(ct)
            read_bytes += b   # load
            write_bytes += b  # store
        dt = p.get("depth_target")
        if dt:
            b = rt_bytes(dt)
            read_bytes += b
            write_bytes += b

        pass_bw = read_bytes + write_bytes
        pass_mb = pass_bw / (1024 * 1024)
        total_bw += pass_bw

        if "bloom" in name.lower():
            bloom_bw += pass_bw

        passes_bw.append({
            "name": name,
            "read_mb": round(read_bytes / (1024 * 1024), 2),
            "write_mb": round(write_bytes / (1024 * 1024), 2),
            "total_mb": round(pass_mb, 2),
        })

    return {
        "passes": passes_bw,
        "total_mb": round(total_bw / (1024 * 1024), 2),
        "bloom_mb": round(bloom_bw / (1024 * 1024), 2),
        "bloom_passes": sum(1 for p in pass_details if "bloom" in p.get("name", "").lower()),
    }


def analyze_shaders(data: dict) -> dict:
    shader_disasm = data.get("shader_disasm") or {}
    shaders_dir: Path = data.get("shaders_dir", Path("."))

    shader_list = []
    for key, info in shader_disasm.items():
        if not isinstance(info, dict):
            continue

        is_compute = "cs_id" in info
        vs_id = info.get("vs_id", 0)
        ps_id = info.get("ps_id", 0)
        cs_id = info.get("cs_id", 0)
        uses = info.get("uses", 0)
        fname = info.get("file", "")

        spirv_bound = 0
        tex_samples = 0
        ubo_size = 0
        ps_lines = 0
        buffer_accesses = 0

        shader_path = shaders_dir / os.path.basename(fname) if fname else None
        content = ""
        if shader_path and shader_path.exists():
            try:
                content = shader_path.read_text(encoding="utf-8", errors="replace")
                if is_compute:
                    cs_lines = content.count("\n")
                    ps_lines = cs_lines
                    bound_match = re.search(r"<id> bound of (\d+)", content)
                    if bound_match:
                        spirv_bound = int(bound_match.group(1))
                    tex_samples = content.count("ImageSampleImplicitLod")
                    tex_samples += content.count("ImageSampleExplicitLod")
                    buffer_accesses = content.count("OpAccessChain")
                    buffer_accesses += content.count("StorageBuffer")
                else:
                    ps_section = content.split("Pixel Shader")
                    if len(ps_section) > 1:
                        ps_text = ps_section[1]
                        ps_lines = ps_text.count("\n")
                        bound_match = re.search(r"<id> bound of (\d+)", ps_text)
                        if bound_match:
                            spirv_bound = int(bound_match.group(1))
                        tex_samples = ps_text.count("ImageSampleImplicitLod")
                        tex_samples += ps_text.count("ImageSampleDrefImplicitLod")
                        tex_samples += ps_text.count("ImageSampleExplicitLod")
                        ubo_match = re.search(r"UnityPerMaterial.*?Offset\((\d+)\)", ps_text, re.DOTALL)
                        if ubo_match:
                            offsets = re.findall(r"Offset\((\d+)\)", ps_text[:ps_text.find("void main()")] if "void main()" in ps_text else ps_text)
                            if offsets:
                                ubo_size = max(int(o) for o in offsets) + 16
            except Exception:
                pass

        instructions = analyze_spirv_instructions(content, is_compute) if content else {
            "arithmetic": 0, "sample": 0, "logic": 0, "load_store": 0,
            "dot_matrix": 0, "intrinsic": 0, "barrier": 0, "total": 0,
        }
        reg_pressure = estimate_register_pressure(content, is_compute) if content else {
            "temp_vars": 0, "input_vars": 0, "output_vars": 0,
            "uniform_vars": 0, "spirv_bound": 0,
            "estimated_vgprs": 0, "pressure_level": "low",
        }

        entry = {
            "key": key,
            "is_compute": is_compute,
            "uses": uses,
            "eids": info.get("eids", []),
            "spirv_bound": spirv_bound,
            "tex_samples": tex_samples,
            "ubo_size": ubo_size,
            "ps_lines": ps_lines,
            "instructions": instructions,
            "register_pressure": reg_pressure,
        }
        if is_compute:
            entry["cs_id"] = cs_id
            entry["buffer_accesses"] = buffer_accesses
        else:
            entry["vs_id"] = vs_id
            entry["ps_id"] = ps_id
        shader_list.append(entry)

    shader_list.sort(key=lambda s: s["uses"] * max(s["spirv_bound"], 1), reverse=True)

    # Shader variant deduplication
    variants = deduplicate_shaders(shader_disasm, shaders_dir)
    variant_count_by_key: dict[str, int] = {}
    for g in variants["groups"]:
        for vk in g["variant_keys"]:
            variant_count_by_key[vk] = g["variant_count"]
    for s in shader_list:
        s["variant_count"] = variant_count_by_key.get(s["key"], 1)

    # Shader → Pass matrix
    pass_details = data.get("pass_details") or []
    pass_names = [p.get("name", f"Pass {i}") for i, p in enumerate(pass_details)]
    pass_ranges = [(p.get("begin_eid", 0), p.get("end_eid", 0)) for p in pass_details]

    matrix: list[list[int]] = []
    multi_pass_indices: list[int] = []
    for si, s in enumerate(shader_list):
        row = [0] * len(pass_details)
        for eid in s.get("eids", []):
            for pi, (beg, end) in enumerate(pass_ranges):
                if beg <= eid <= end:
                    row[pi] += 1
                    break
        matrix.append(row)
        if sum(1 for c in row if c > 0) >= 2:
            multi_pass_indices.append(si)

    shader_pass_matrix = {
        "shaders": [
            {"key": s["key"],
             "label": f'CS {s.get("cs_id", 0)}' if s.get("is_compute")
             else f'VS {s.get("vs_id", 0)} / PS {s.get("ps_id", 0)}'}
            for s in shader_list
        ],
        "passes": pass_names,
        "matrix": matrix,
        "multi_pass_indices": multi_pass_indices,
    }

    return {
        "shaders": shader_list,
        "total_unique": len(shader_list),
        "total_compute": sum(1 for s in shader_list if s.get("is_compute")),
        "variants": variants,
        "shader_pass_matrix": shader_pass_matrix,
    }


def analyze_memory(data: dict) -> dict:
    computed = data.get("computed") or {}
    resource_details = data.get("resource_details") or {}
    mem = computed.get("memory_estimate", {})

    # Format distribution
    fmt_counts: Counter = Counter()
    fmt_bytes: dict[str, float] = defaultdict(float)
    for rid, rdata in resource_details.items():
        if not isinstance(rdata, dict):
            continue
        rtype = (rdata.get("type") or "").lower()
        if "texture" not in rtype and "image" not in rtype:
            continue
        fmt = rdata.get("format", "Unknown")
        # Simplify format name for grouping
        fmt_group = fmt.split("_")[0] if "_" in fmt else fmt
        if "ASTC" in fmt.upper():
            fmt_group = fmt  # keep ASTC block size
        fmt_counts[fmt_group] += 1
        w = rdata.get("width", 0)
        h = rdata.get("height", 0)
        bpp = guess_bpp(fmt)
        fmt_bytes[fmt_group] += w * h * (bpp / 8) / (1024 * 1024)

    return {
        "total_textures_mb": mem.get("total_textures_mb", 0),
        "total_buffers_mb": mem.get("total_buffers_mb", 0),
        "largest_resources": mem.get("largest_resources", [])[:20],
        "format_distribution": [
            {"format": fmt, "count": cnt, "size_mb": round(fmt_bytes.get(fmt, 0), 2)}
            for fmt, cnt in fmt_counts.most_common()
        ],
    }


def analyze_overdraw(data: dict) -> dict:
    """Return overdraw estimation from computed.json, or unavailable sentinel."""
    computed = data.get("computed") or {}
    od = computed.get("overdraw")
    if od:
        return od
    return {"available": False, "reason": "No overdraw data — re-run collect.py to generate computed.json"}


def analyze_mipmap_usage(data: dict) -> dict:
    """Return mipmap view-level waste analysis from computed.json."""
    computed = data.get("computed") or {}
    mu = computed.get("mipmap_usage")
    if mu is not None:
        return mu
    return {"per_texture": [], "total_wasted_mb": 0.0}


def analyze_pipeline_stages(data: dict) -> dict:
    """Classify each pass into a pipeline stage using metadata heuristics."""
    summary = data.get("summary") or {}
    pass_details = data.get("pass_details") or []
    draws_list = _unwrap(summary.get("draws"), "draws") or []

    counters_raw = summary.get("counters") or {}
    counter_rows = counters_raw.get("rows") or (
        counters_raw if isinstance(counters_raw, list) else []
    )
    counters_by_eid: dict[int, dict] = {}
    for r in counter_rows:
        if not isinstance(r, dict):
            continue
        eid = r.get("eid")
        if eid is None:
            continue
        eid = int(eid)
        if eid not in counters_by_eid:
            counters_by_eid[eid] = {}
        counters_by_eid[eid][r.get("counter", "")] = r.get("value", 0)

    bloom = detect_bloom_chain(pass_details)
    bloom_names: set[str] = set()
    if bloom:
        bloom_names = set(bloom["passes"])

    max_rt_area = 0
    for p in pass_details:
        for ct in (p.get("color_targets") or []):
            area = ct.get("width", 0) * ct.get("height", 0)
            if area > max_rt_area:
                max_rt_area = area

    pass_deps = summary.get("pass_deps") or {}
    per_pass_rw = pass_deps.get("per_pass") or []
    rw_by_name: dict[str, dict] = {}
    for pp in per_pass_rw:
        if isinstance(pp, dict):
            rw_by_name[pp.get("name", "")] = pp

    # Pre-load shader content for pattern detection
    shader_disasm = data.get("shader_disasm") or {}
    shaders_dir: Path = data.get("shaders_dir", Path("."))
    _shader_content_cache: dict[str, str] = {}
    _shader_patterns_cache: dict[str, list[str]] = {}

    def _get_shader_patterns(shader_key: str, info: dict) -> list[str]:
        if shader_key in _shader_patterns_cache:
            return _shader_patterns_cache[shader_key]
        if shader_key not in _shader_content_cache:
            fname = info.get("file", "")
            shader_path = shaders_dir / os.path.basename(fname) if fname else None
            if shader_path and shader_path.exists():
                try:
                    _shader_content_cache[shader_key] = shader_path.read_text(
                        encoding="utf-8", errors="replace"
                    )
                except Exception:
                    _shader_content_cache[shader_key] = ""
            else:
                _shader_content_cache[shader_key] = ""
        content = _shader_content_cache[shader_key]
        is_cs = "cs_id" in info
        patterns = detect_shader_patterns(content, is_compute=is_cs) if content else []
        _shader_patterns_cache[shader_key] = patterns
        return patterns

    stages = []
    stage_times: dict[str, float] = {}
    stage_counts: dict[str, int] = {}

    for p in pass_details:
        name = p.get("name", "")
        begin_eid = p.get("begin_eid", 0)
        end_eid = p.get("end_eid", 0)

        is_auto_name = bool(re.match(
            r"^(Colour|Depth-only|Compute|Copy/Clear) Pass #\d+", name
        ))
        if is_auto_name:
            stage, reason = classify_pass_stage(
                p,
                all_passes=pass_details,
                bloom_pass_names=bloom_names,
                max_rt_area=max_rt_area,
            )
        else:
            cat = _classify_pass(name, draws=p.get("draws", 0), dispatches=p.get("dispatches", 0))
            if cat == "Other":
                stage, reason = classify_pass_stage(
                    p,
                    all_passes=pass_details,
                    bloom_pass_names=bloom_names,
                    max_rt_area=max_rt_area,
                )
            else:
                stage = cat
                reason = f"name keyword: '{name}'"

        gpu_time_us = 0.0
        ps_inv = 0
        for eid_key, cdata in counters_by_eid.items():
            if begin_eid <= eid_key <= end_eid:
                gpu_time_us += cdata.get("GPU Duration", 0.0) * 1e6
                ps_inv += int(cdata.get("PS Invocations", 0))

        cts = p.get("color_targets") or []
        rt_w = cts[0].get("width", 0) if cts else 0
        rt_h = cts[0].get("height", 0) if cts else 0
        rt_fmt = cts[0].get("format", "") if cts else ""
        if not cts:
            dt = p.get("depth_target")
            if isinstance(dt, dict):
                rt_w = dt.get("width", 0)
                rt_h = dt.get("height", 0)
                rt_fmt = dt.get("format", "")

        draws_in_pass = [
            d for d in draws_list
            if isinstance(d, dict) and begin_eid <= d.get("eid", 0) <= end_eid
        ]
        is_fs = detect_fullscreen_quad(draws_in_pass, rt_w, rt_h, counters_by_eid)

        rt_area = rt_w * rt_h
        overdraw = round(ps_inv / rt_area, 2) if rt_area > 0 and ps_inv > 0 else 0.0

        rw = rw_by_name.get(name, {})

        # Detect shader patterns for draws in this pass
        pass_eids = set(d.get("eid", 0) for d in draws_in_pass)
        shader_patterns: list[str] = []
        seen_patterns: set[str] = set()
        for skey, sinfo in shader_disasm.items():
            if not isinstance(sinfo, dict):
                continue
            shader_eids = set(sinfo.get("eids", []))
            if shader_eids & pass_eids:
                for pat in _get_shader_patterns(skey, sinfo):
                    if pat not in seen_patterns:
                        shader_patterns.append(pat)
                        seen_patterns.add(pat)

        stages.append({
            "pass_name": name,
            "stage": stage,
            "reason": reason,
            "color": STAGE_COLORS.get(stage, "#555f78"),
            "begin_eid": begin_eid,
            "end_eid": end_eid,
            "draws": p.get("draws", 0),
            "dispatches": p.get("dispatches", 0),
            "triangles": p.get("triangles", 0),
            "gpu_time_us": round(gpu_time_us, 1),
            "ps_invocations": ps_inv,
            "is_fullscreen": is_fs,
            "overdraw": overdraw,
            "rt_width": rt_w,
            "rt_height": rt_h,
            "rt_format": rt_fmt,
            "shader_patterns": shader_patterns,
            "writes_to": rw.get("writes") or [],
            "reads_from": rw.get("reads") or [],
        })

        stage_times[stage] = stage_times.get(stage, 0.0) + gpu_time_us
        stage_counts[stage] = stage_counts.get(stage, 0) + 1

    total_time = sum(stage_times.values())
    stage_groups = []
    for stg in sorted(stage_times, key=lambda s: -stage_times[s]):
        pct = (stage_times[stg] / total_time * 100) if total_time > 0 else 0
        stage_groups.append({
            "stage": stg,
            "passes": stage_counts[stg],
            "gpu_time_us": round(stage_times[stg], 1),
            "pct": round(pct, 1),
            "color": STAGE_COLORS.get(stg, "#555f78"),
        })

    return {
        "stages": stages,
        "stage_groups": stage_groups,
        "bloom_chain": bloom,
        "total_gpu_time_us": round(total_time, 1),
    }


def generate_suggestions(analysis: dict, data: dict) -> list[dict]:
    suggestions = []

    # 1. Repeated mesh draws
    for mesh in analysis["hotspots"].get("repeated_meshes", []):
        if mesh["triangles"] > 5000:
            suggestions.append({
                "severity": "warning",
                "title": f"Mesh ({_fmt_number(mesh['triangles'])} tri) drawn {mesh['count']}x across passes",
                "detail": (
                    f"A {_fmt_number(mesh['triangles'])}-triangle mesh is drawn {mesh['count']} times "
                    f"in markers: {', '.join(mesh['markers'][:4])}. "
                    "Consider using a simplified LOD for depth-only and shadow passes."
                ),
                "category": "Geometry",
            })

    # 2. Bloom pass count
    bloom_passes = analysis["bandwidth"].get("bloom_passes", 0)
    bloom_mb = analysis["bandwidth"].get("bloom_mb", 0)
    if bloom_passes > 8:
        suggestions.append({
            "severity": "info",
            "title": f"Bloom uses {bloom_passes} subpasses ({_fmt_mb(bloom_mb)} bandwidth)",
            "detail": (
                f"The Bloom effect uses {bloom_passes} render passes for its mip chain. "
                "Consider reducing mip levels (e.g., 4 instead of 5 downsample levels) "
                "or using compute shaders for the downsample/upsample chain."
            ),
            "category": "PostProcess",
        })

    # 3. Large UBO
    for s in analysis["shaders"].get("shaders", []):
        if s["ubo_size"] > 400 and s["uses"] > 5:
            suggestions.append({
                "severity": "info",
                "title": f"Large UnityPerMaterial UBO ({s['ubo_size']} bytes) used {s['uses']}x",
                "detail": (
                    f"Shader pair {s['key']} has a ~{s['ubo_size']}-byte material UBO with many "
                    "unused fields (Xhlslcc_UnusedX_*). This wastes uniform buffer bandwidth. "
                    "Consider stripping unused material properties."
                ),
                "category": "Shader",
            })
            break  # only report once

    # 4. Large textures
    computed = data.get("computed") or {}
    for alert in computed.get("alerts", []):
        if alert.get("type") == "large_resource" and alert.get("size_mb", 0) > 8:
            suggestions.append({
                "severity": "warning",
                "title": f"Large texture: {alert.get('name', '')} ({alert['size_mb']:.1f} MB)",
                "detail": (
                    "This texture consumes significant memory. Check if it's fully utilized "
                    "or if it can use a more compressed format."
                ),
                "category": "Memory",
            })

    # 5. Heavy draw calls
    for d in analysis["hotspots"].get("top_draws", [])[:3]:
        if d.get("triangles", 0) > 10000:
            suggestions.append({
                "severity": "warning",
                "title": f"Heavy draw: EID {d['eid']} — {_fmt_number(d['triangles'])} triangles",
                "detail": (
                    f"Draw call at EID {d['eid']} ({d.get('marker', '')}) submits "
                    f"{_fmt_number(d['triangles'])} triangles in a single call. "
                    "Consider mesh LODs or culling to reduce vertex load."
                ),
                "category": "Geometry",
            })

    # 6. No load/store ops
    pass_details = data.get("pass_details") or []
    has_load_store = any(p.get("load_ops") or p.get("store_ops") for p in pass_details)
    if not has_load_store and len(pass_details) > 0:
        suggestions.append({
            "severity": "info",
            "title": "No load/store ops data (GLES limitation)",
            "detail": (
                "This capture doesn't contain load/store operation data. "
                "TBDR optimization analysis (tile-based load/store efficiency) "
                "is not available. Bandwidth estimates assume worst-case full load+store."
            ),
            "category": "Data",
        })

    # 7. Total bandwidth
    total_mb = analysis["bandwidth"].get("total_mb", 0)
    if total_mb > 100:
        suggestions.append({
            "severity": "warning",
            "title": f"High estimated frame bandwidth: {_fmt_mb(total_mb)}",
            "detail": (
                f"Total render target bandwidth is ~{_fmt_mb(total_mb)} per frame. "
                "On mobile GPUs with limited bandwidth, this can be a major bottleneck. "
                "Focus on reducing RT resolution or merging passes where possible."
            ),
            "category": "Bandwidth",
        })

    # 8–12. Stage-aware suggestions
    pstages = analysis.get("pipeline_stages") or {}
    stage_groups = pstages.get("stage_groups") or []
    stage_list = pstages.get("stages") or []
    bloom_info = pstages.get("bloom_chain")
    total_gpu = pstages.get("total_gpu_time_us", 0)

    for g in stage_groups:
        pct = g.get("pct", 0)
        if pct > 35 and g["stage"] == "Compute":
            suggestions.append({
                "severity": "warning",
                "title": f"Compute dominates GPU time ({pct:.0f}%)",
                "detail": (
                    f"Compute dispatches consume {g['gpu_time_us']:.0f} us ({pct:.0f}% of frame). "
                    "Check workgroup size, occupancy, and whether all dispatches are necessary."
                ),
                "category": "Compute",
            })
        if pct > 25 and g["stage"] == "Compositing" and g["passes"] >= 3:
            suggestions.append({
                "severity": "info",
                "title": f"Compositing uses {g['passes']} separate passes ({pct:.0f}%)",
                "detail": (
                    f"{g['passes']} compositing passes across multiple queue submits. "
                    "If not required by synchronization, merging into fewer passes "
                    "reduces submit overhead and render pass transitions."
                ),
                "category": "Compositing",
            })

    if bloom_info and bloom_info.get("detected"):
        n_bloom = len(bloom_info.get("passes", []))
        bloom_gpu = sum(
            s["gpu_time_us"] for s in stage_list if s["stage"] == "Bloom"
        )
        bloom_pct = (bloom_gpu / total_gpu * 100) if total_gpu > 0 else 0
        if bloom_pct > 15:
            suggestions.append({
                "severity": "info",
                "title": f"Bloom chain: {n_bloom} passes, {bloom_gpu:.0f} us ({bloom_pct:.0f}%)",
                "detail": (
                    f"Bloom uses {bloom_info.get('levels', 0)} downsample levels. "
                    "Consider compute-shader bloom (single dispatch per level) "
                    "or reducing levels to cut render pass overhead."
                ),
                "category": "PostProcess",
            })

    for s in stage_list:
        od = s.get("overdraw", 0)
        if od > 4.0 and s["stage"] not in ("Bloom", "PostProcess", "Compositing"):
            suggestions.append({
                "severity": "warning",
                "title": f"High overdraw {od:.1f}x in {s['pass_name']}",
                "detail": (
                    f"Pass '{s['pass_name']}' ({s['stage']}) has {od:.1f}x overdraw. "
                    "Check draw order (front-to-back), early-Z effectiveness, "
                    "or whether alpha-tested/transparent draws are mixed with opaques."
                ),
                "category": "Overdraw",
            })

    # Sort: warning first, then info
    severity_order = {"critical": 0, "warning": 1, "info": 2}
    suggestions.sort(key=lambda s: severity_order.get(s["severity"], 9))

    return suggestions


# ─────────────────────────────────────────────────────────────────────
# HTML Generation
# ─────────────────────────────────────────────────────────────────────

def _esc(s: str) -> str:
    """HTML-escape a string."""
    return (
        str(s)
        .replace("&", "&amp;")
        .replace("<", "&lt;")
        .replace(">", "&gt;")
        .replace('"', "&quot;")
    )


def _bar_color(ratio: float) -> str:
    """Return a heat color based on ratio 0..1."""
    if ratio < 0.25:
        return "var(--heat-cold)"
    if ratio < 0.5:
        return "var(--heat-cool)"
    if ratio < 0.75:
        return "var(--heat-warm)"
    if ratio < 0.9:
        return "var(--heat-hot)"
    return "var(--heat-fire)"


def _severity_icon(sev: str) -> str:
    if sev == "critical":
        return '<span class="sev-icon sev-critical">!!</span>'
    if sev == "warning":
        return '<span class="sev-icon sev-warning">!</span>'
    return '<span class="sev-icon sev-info">i</span>'


def render_html(analysis: dict, capture_name: str, assets_rel: str = "../html") -> str:
    overview = analysis["overview"]
    pipeline = analysis["pipeline"]
    hotspots = analysis["hotspots"]
    bandwidth = analysis["bandwidth"]
    shaders = analysis["shaders"]
    memory = analysis["memory"]
    suggestions = analysis["suggestions"]
    overdraw_data = analysis.get("overdraw") or {}
    mipmap_data = analysis.get("mipmap_usage") or {}

    # ── Build HTML sections ──

    # Section 1: Frame Overview cards
    overview_cards = f"""
    <div class="cards-grid">
      <div class="card">
        <div class="card-label">API</div>
        <div class="card-value">{_esc(overview['api'])}</div>
      </div>
      <div class="card">
        <div class="card-label">Platform</div>
        <div class="card-value small">{_esc(overview['platform'])}</div>
      </div>
      <div class="card">
        <div class="card-label">Resolution</div>
        <div class="card-value">{_esc(overview['resolution'])}</div>
      </div>
      <div class="card">
        <div class="card-label">Draw Calls</div>
        <div class="card-value">{_fmt_number(overview['total_draws'])}</div>
        <div class="card-sub">{_esc(str(overview['draw_calls']))}</div>
      </div>
      <div class="card">
        <div class="card-label">Dispatches</div>
        <div class="card-value">{_fmt_number(overview.get('total_dispatches', 0))}</div>
      </div>
      <div class="card">
        <div class="card-label">Triangles</div>
        <div class="card-value">{_fmt_number(overview['total_triangles'])}</div>
      </div>
      <div class="card">
        <div class="card-label">Passes</div>
        <div class="card-value">{overview['pass_count']}</div>
      </div>
      <div class="card">
        <div class="card-label">Architecture</div>
        <div class="card-value">{_esc(overview['architecture'])}</div>
      </div>
      <div class="card">
        <div class="card-label">Events</div>
        <div class="card-value">{_fmt_number(overview['events'])}</div>
      </div>
    </div>
    """

    # Section 2: Rendering Pipeline
    passes = pipeline["passes"]
    min_eid = min((p["begin_eid"] for p in passes), default=0)
    max_eid = max((p["end_eid"] for p in passes), default=1)
    eid_range = max(max_eid - min_eid, 1)

    gantt_bars = []
    for i, p in enumerate(passes):
        left_pct = (p["begin_eid"] - min_eid) / eid_range * 100
        width_pct = max((p["end_eid"] - p["begin_eid"]) / eid_range * 100, 0.5)
        short_name = p["name"]
        if len(short_name) > 30:
            short_name = short_name[:27] + "..."
        gantt_bars.append(
            f'<div class="gantt-bar" style="left:{left_pct:.2f}%;width:{width_pct:.2f}%;'
            f'background:{p["color"]}" title="{_esc(p["name"])} (EID {p["begin_eid"]}-{p["end_eid"]}, '
            f'{_fmt_number(p["triangles"])} tri)">'
            f'<span class="gantt-label">{_esc(short_name)}</span></div>'
        )

    pass_table_rows = []
    for i, p in enumerate(passes):
        rt_strs = []
        for rt in p["render_targets"]:
            prefix = '<span class="rt-depth">D</span> ' if rt.get("is_depth") else '<span class="rt-color">C</span> '
            rt_strs.append(f'{prefix}{_esc(rt["format"])} {_esc(rt["dims"])}')
        rt_cell = "<br>".join(rt_strs) if rt_strs else '<span class="text-muted">none</span>'
        pass_table_rows.append(
            f'<tr>'
            f'<td>{i}</td>'
            f'<td><span class="cat-dot" style="background:{p["color"]}"></span>{_esc(p["name"])}</td>'
            f'<td>{_esc(p["category"])}</td>'
            f'<td class="num">{p["draws"]}</td>'
            f'<td class="num">{p["dispatches"]}</td>'
            f'<td class="num">{_fmt_number(p["triangles"])}</td>'
            f'<td class="num">{p["begin_eid"]}-{p["end_eid"]}</td>'
            f'<td class="rt-cell">{rt_cell}</td>'
            f'</tr>'
        )

    pipeline_html = f"""
    <div class="gantt-container">
      <div class="gantt-track">
        {"".join(gantt_bars)}
      </div>
      <div class="gantt-axis">
        <span>EID {min_eid}</span>
        <span>EID {(min_eid + max_eid) // 2}</span>
        <span>EID {max_eid}</span>
      </div>
    </div>
    <div class="table-wrap">
    <table class="data-table sortable">
      <thead><tr>
        <th>#</th><th>Pass Name</th><th>Category</th>
        <th>Draws</th><th>Dispatches</th><th>Triangles</th><th>EID Range</th><th>Render Targets</th>
      </tr></thead>
      <tbody>{"".join(pass_table_rows)}</tbody>
    </table>
    </div>
    """

    # Section 2b: Pipeline Stage Analysis
    pstages = analysis.get("pipeline_stages") or {}
    stage_groups = pstages.get("stage_groups") or []
    stage_list = pstages.get("stages") or []
    bloom_chain = pstages.get("bloom_chain")
    total_gpu_us = pstages.get("total_gpu_time_us", 0)

    # Stage distribution bars
    stage_dist_bars = []
    max_stage_time = max((g["gpu_time_us"] for g in stage_groups), default=1)
    for g in stage_groups:
        pct = g["gpu_time_us"] / max(max_stage_time, 1) * 100
        stage_dist_bars.append(
            f'<div class="bar-row">'
            f'<div class="bar-label"><span class="cat-dot" style="background:{g["color"]}"></span>'
            f'{_esc(g["stage"])}'
            f'<span class="bar-sub">{g["passes"]} pass{"es" if g["passes"] != 1 else ""}</span></div>'
            f'<div class="bar-track">'
            f'<div class="bar-fill" style="width:{pct:.1f}%;background:{g["color"]}">'
            f'</div></div>'
            f'<div class="bar-value">{g["gpu_time_us"]:.0f} us ({g["pct"]:.0f}%)</div>'
            f'</div>'
        )

    # Stage-annotated pass table
    stage_table_rows = []
    for i, s in enumerate(stage_list):
        fs_tag = '<span class="tag tag-fs">FS</span>' if s.get("is_fullscreen") else ""
        pattern_tags = "".join(
            f'<span class="tag tag-pattern">{_esc(p)}</span>'
            for p in s.get("shader_patterns", [])
        )
        od = s.get("overdraw", 0.0)
        if od > 8.0:
            od_style = 'color:#ef4444;font-weight:600'
        elif od > 4.0:
            od_style = 'color:#f97316;font-weight:600'
        elif od > 2.0:
            od_style = 'color:#eab308'
        else:
            od_style = ''
        od_cell = f'<td class="num" style="{od_style}">{od:.2f}</td>' if od > 0 else '<td class="num">-</td>'
        stage_table_rows.append(
            f'<tr>'
            f'<td>{i}</td>'
            f'<td><span class="cat-dot" style="background:{s["color"]}"></span>{_esc(s["pass_name"])}</td>'
            f'<td><span class="stage-tag" style="background:{s["color"]}20;color:{s["color"]};'
            f'border:1px solid {s["color"]}40">{_esc(s["stage"])}</span></td>'
            f'<td>{_esc(s["reason"])}</td>'
            f'<td class="num">{s["gpu_time_us"]:.0f}</td>'
            f'<td class="num">{_fmt_number(s["ps_invocations"])}</td>'
            f'<td class="num">{s["rt_width"]}x{s["rt_height"]}</td>'
            f'<td>{_esc(s["rt_format"])}{fs_tag}{pattern_tags}</td>'
            f'{od_cell}'
            f'</tr>'
        )

    # Bloom chain visualization
    bloom_html = ""
    if bloom_chain and bloom_chain.get("detected"):
        bloom_steps = []
        for k, (res, direction) in enumerate(zip(
            bloom_chain.get("resolutions", []),
            bloom_chain.get("directions", []),
        )):
            arrow = ""
            if direction == "down":
                arrow = '<span class="bloom-arrow">&#8595;</span>'
            elif direction == "up":
                arrow = '<span class="bloom-arrow" style="color:#22b07a">&#8593;</span>'
            elif direction == "threshold":
                arrow = '<span class="bloom-arrow" style="color:#d4a017">&#9670;</span>'
            elif direction == "same":
                arrow = '<span class="bloom-arrow">&#8594;</span>'
            bloom_steps.append(
                f'<div class="bloom-step">'
                f'{arrow}<div class="bloom-res">{_esc(res)}</div>'
                f'<div class="bloom-label">{_esc(direction)}</div></div>'
            )
        bloom_html = f"""
        <h4>Bloom Chain ({bloom_chain.get("levels", 0)} levels)</h4>
        <div class="bloom-chain">{"".join(bloom_steps)}</div>
        """

    pipeline_stages_html = f"""
    <div class="mini-cards">
      <div class="mini-card">
        <div class="mini-label">Total GPU Time</div>
        <div class="mini-value">{total_gpu_us:.0f} us</div>
      </div>
      <div class="mini-card">
        <div class="mini-label">Stage Types</div>
        <div class="mini-value">{len(stage_groups)}</div>
      </div>
    </div>
    <h4>GPU Time by Stage</h4>
    <div class="chart-area">{"".join(stage_dist_bars)}</div>
    {bloom_html}
    <h4>Pass Classification</h4>
    <div class="table-wrap">
    <table class="data-table sortable">
      <thead><tr>
        <th>#</th><th>Pass Name</th><th>Stage</th><th>Reason</th>
        <th>GPU (us)</th><th>PS Invocations</th><th>RT Size</th><th>Format</th><th>OD</th>
      </tr></thead>
      <tbody>{"".join(stage_table_rows)}</tbody>
    </table>
    </div>
    """

    # Section 3: Hotspots
    top_draws_bars = []
    max_tri = hotspots["max_tri"] or 1
    for d in hotspots["top_draws"]:
        tri = d.get("triangles", 0)
        pct = tri / max_tri * 100
        ratio = tri / max_tri
        top_draws_bars.append(
            f'<div class="bar-row">'
            f'<div class="bar-label">EID {d["eid"]}'
            f'<span class="bar-sub">{_esc(d.get("marker", ""))}</span></div>'
            f'<div class="bar-track">'
            f'<div class="bar-fill" style="width:{pct:.1f}%;background:{_bar_color(ratio)}">'
            f'</div></div>'
            f'<div class="bar-value">{_fmt_number(tri)}</div>'
            f'</div>'
        )

    per_pass_bars = []
    total_tri = hotspots["total_triangles"] or 1
    for pp in hotspots["per_pass"]:
        pct = pp.get("percent", 0)
        per_pass_bars.append(
            f'<div class="bar-row">'
            f'<div class="bar-label">{_esc(pp["name"])}</div>'
            f'<div class="bar-track">'
            f'<div class="bar-fill" style="width:{pct:.1f}%;background:var(--accent-cyan)">'
            f'</div></div>'
            f'<div class="bar-value">{_fmt_number(pp["triangles"])} ({pct:.1f}%)</div>'
            f'</div>'
        )

    repeated_html = ""
    if hotspots["repeated_meshes"]:
        rows = []
        for m in hotspots["repeated_meshes"]:
            rows.append(
                f'<tr><td class="num">{_fmt_number(m["triangles"])}</td>'
                f'<td class="num">{m["count"]}</td>'
                f'<td>{_esc(", ".join(m["markers"][:4]))}</td></tr>'
            )
        repeated_html = f"""
        <h4>Repeated Mesh Draws</h4>
        <table class="data-table compact">
          <thead><tr><th>Triangles</th><th>Draw Count</th><th>Markers</th></tr></thead>
          <tbody>{"".join(rows)}</tbody>
        </table>
        """

    hotspots_html = f"""
    <div class="two-col">
      <div>
        <h4>Top Draw Calls by Triangle Count</h4>
        <div class="bar-chart">{"".join(top_draws_bars)}</div>
      </div>
      <div>
        <h4>Per-Pass Triangle Distribution</h4>
        <div class="bar-chart">{"".join(per_pass_bars)}</div>
      </div>
    </div>
    {repeated_html}
    """

    # Section 4: Bandwidth
    bw_passes = bandwidth["passes"]
    max_bw = max((p["total_mb"] for p in bw_passes), default=1) or 1
    bw_bars = []
    for p in bw_passes:
        pct = p["total_mb"] / max_bw * 100
        ratio = p["total_mb"] / max_bw
        bw_bars.append(
            f'<div class="bar-row">'
            f'<div class="bar-label">{_esc(p["name"])}</div>'
            f'<div class="bar-track">'
            f'<div class="bar-fill" style="width:{pct:.1f}%;background:{_bar_color(ratio)}">'
            f'</div></div>'
            f'<div class="bar-value">{p["total_mb"]:.2f} MB</div>'
            f'</div>'
        )

    bandwidth_html = f"""
    <div class="cards-grid mini">
      <div class="card">
        <div class="card-label">Total Frame BW</div>
        <div class="card-value">{_fmt_mb(bandwidth['total_mb'])}</div>
      </div>
      <div class="card">
        <div class="card-label">Bloom Chain BW</div>
        <div class="card-value">{_fmt_mb(bandwidth['bloom_mb'])}</div>
        <div class="card-sub">{bandwidth['bloom_passes']} subpasses</div>
      </div>
    </div>
    <h4>Per-Pass Bandwidth (Load + Store)</h4>
    <div class="bar-chart">{"".join(bw_bars)}</div>
    """

    # Section 5: Shaders
    shader_rows = []
    top_shaders = shaders["shaders"][:20]
    max_bound = max((s["spirv_bound"] for s in top_shaders), default=1) or 1
    for s in top_shaders:
        bound_pct = s["spirv_bound"] / max_bound * 100
        if s.get("is_compute"):
            shader_label = f'CS {s["cs_id"]}'
        else:
            shader_label = f'VS {s.get("vs_id", 0)} / PS {s.get("ps_id", 0)}'
        # Pressure tag
        rp = s.get("register_pressure", {})
        plevel = rp.get("pressure_level", "low")
        pressure_colors = {"low": "#22c55e", "medium": "#eab308", "high": "#ef4444"}
        pc = pressure_colors.get(plevel, "#888")
        pressure_tag = (
            f'<span class="stage-tag" style="background:{pc}20;color:{pc};'
            f'border:1px solid {pc}40">{plevel}</span>'
        )
        # Variant count
        vc = s.get("variant_count", 1)
        variant_cell = f'{vc}' if vc > 1 else '-'
        shader_rows.append(
            f'<tr>'
            f'<td class="mono">{shader_label}</td>'
            f'<td class="num">{s["uses"]}</td>'
            f'<td class="num">{_fmt_number(s["spirv_bound"])}'
            f'<div class="inline-bar" style="width:{bound_pct:.0f}%"></div></td>'
            f'<td class="num">{s["tex_samples"]}</td>'
            f'<td class="num">{_fmt_number(s["ps_lines"])}</td>'
            f'<td class="num">{s["ubo_size"] if s["ubo_size"] > 0 else "-"}</td>'
            f'<td>{pressure_tag}</td>'
            f'<td class="num">{variant_cell}</td>'
            f'</tr>'
        )

    cs_note = ""
    if shaders.get("total_compute", 0) > 0:
        cs_note = f' ({shaders["total_compute"]} compute)'

    # 5a: Instruction mix stacked bars
    INSTR_COLORS = {
        "arithmetic": "#3b82f6", "sample": "#22c55e", "logic": "#f59e0b",
        "load_store": "#8b5cf6", "dot_matrix": "#ec4899",
        "intrinsic": "#06b6d4", "barrier": "#ef4444",
    }
    instr_bars = []
    for s in top_shaders[:15]:
        inst = s.get("instructions", {})
        total = inst.get("total", 0) or 1
        if s.get("is_compute"):
            label = f'CS {s.get("cs_id", 0)}'
        else:
            label = f'VS {s.get("vs_id", 0)} / PS {s.get("ps_id", 0)}'
        segments = []
        for cat in ("arithmetic", "sample", "logic", "load_store", "dot_matrix", "intrinsic", "barrier"):
            v = inst.get(cat, 0)
            if v > 0:
                pct = v / total * 100
                segments.append(
                    f'<div class="stack-seg" style="width:{pct:.1f}%;background:{INSTR_COLORS[cat]}"'
                    f' title="{cat}: {v}"></div>'
                )
        instr_bars.append(
            f'<div class="bar-row">'
            f'<div class="bar-label mono" style="min-width:160px">{label}</div>'
            f'<div class="bar-track" style="display:flex">{"".join(segments)}</div>'
            f'<div class="bar-value">{total}</div>'
            f'</div>'
        )
    instr_legend = " ".join(
        f'<span style="display:inline-flex;align-items:center;gap:3px;margin-right:10px">'
        f'<span style="width:10px;height:10px;border-radius:2px;background:{c};display:inline-block"></span>'
        f'{n}</span>'
        for n, c in INSTR_COLORS.items()
    )

    # 5b: Shader variants
    variants = shaders.get("variants", {})
    variant_groups = [g for g in variants.get("groups", []) if g["variant_count"] > 1]
    variant_rows = []
    for g in variant_groups:
        spec_desc = ", ".join(f'SpecId({sid})' for sid in g.get("spec_diffs", {}))
        variant_rows.append(
            f'<tr>'
            f'<td class="mono">{_esc(g["canonical_key"])}</td>'
            f'<td class="num">{g["variant_count"]}</td>'
            f'<td class="num">{g["total_uses"]}</td>'
            f'<td>{_esc(spec_desc) if spec_desc else "-"}</td>'
            f'</tr>'
        )
    variants_html = ""
    if variant_groups:
        variants_html = f"""
        <h4>Shader Variants</h4>
        <div class="mini-cards">
          <div class="mini-card">
            <div class="mini-label">Total Shaders</div>
            <div class="mini-value">{variants.get("total_shaders", 0)}</div>
          </div>
          <div class="mini-card">
            <div class="mini-label">Unique (after dedup)</div>
            <div class="mini-value">{variants.get("unique_shaders", 0)}</div>
          </div>
          <div class="mini-card">
            <div class="mini-label">Variant Groups</div>
            <div class="mini-value">{variants.get("variant_groups", 0)}</div>
          </div>
        </div>
        <div class="table-wrap">
        <table class="data-table compact">
          <thead><tr><th>Canonical Shader</th><th>Variants</th><th>Total Uses</th><th>SpecId Diffs</th></tr></thead>
          <tbody>{"".join(variant_rows)}</tbody>
        </table>
        </div>
        """

    # 5c: Shader × Pass heatmap
    spm = shaders.get("shader_pass_matrix", {})
    heatmap_html = ""
    multi_idx = spm.get("multi_pass_indices", [])
    if multi_idx and spm.get("passes"):
        spm_shaders = spm["shaders"]
        spm_passes = spm["passes"]
        spm_matrix = spm["matrix"]
        max_count = max(
            (spm_matrix[si][pi] for si in multi_idx for pi in range(len(spm_passes))),
            default=1,
        ) or 1
        # Truncate pass names for column headers
        pass_headers = []
        for pn in spm_passes:
            short = pn[:18] + ".." if len(pn) > 20 else pn
            pass_headers.append(f'<th class="hm-th" title="{_esc(pn)}">{_esc(short)}</th>')
        hm_rows = []
        for si in multi_idx:
            cells = []
            for pi in range(len(spm_passes)):
                v = spm_matrix[si][pi]
                if v == 0:
                    cells.append('<td class="hm-cell"></td>')
                else:
                    opacity = max(0.15, v / max_count)
                    cells.append(
                        f'<td class="hm-cell" style="background:rgba(56,189,248,{opacity:.2f})"'
                        f' title="{v} draws">{v}</td>'
                    )
            hm_rows.append(
                f'<tr><td class="mono hm-label">{_esc(spm_shaders[si]["label"])}</td>'
                f'{"".join(cells)}</tr>'
            )
        heatmap_html = f"""
        <h4>Shader Usage Heatmap</h4>
        <div class="table-wrap">
        <table class="data-table compact heatmap-table">
          <thead><tr><th>Shader</th>{"".join(pass_headers)}</tr></thead>
          <tbody>{"".join(hm_rows)}</tbody>
        </table>
        </div>
        """

    shaders_html = f"""
    <div class="card-inline">Total unique shaders: <strong>{shaders['total_unique']}</strong>{cs_note}</div>
    <div class="table-wrap">
    <table class="data-table sortable">
      <thead><tr>
        <th>Shader</th><th>Uses</th><th>SPIR-V Bound</th>
        <th>Tex Samples</th><th>Lines</th><th>UBO Size</th><th>Pressure</th><th>Variants</th>
      </tr></thead>
      <tbody>{"".join(shader_rows)}</tbody>
    </table>
    </div>
    <h4>Instruction Mix</h4>
    <div style="margin-bottom:8px;font-size:12px">{instr_legend}</div>
    <div class="bar-chart">{"".join(instr_bars)}</div>
    {variants_html}
    {heatmap_html}
    """

    # Section 6: Memory
    largest = memory["largest_resources"]
    max_size = max((r["size_mb"] for r in largest), default=1) or 1
    mem_bars = []
    for r in largest[:15]:
        pct = r["size_mb"] / max_size * 100
        ratio = r["size_mb"] / max_size
        name = r.get("name", "")
        if len(name) > 40:
            name = name[:37] + "..."
        mem_bars.append(
            f'<div class="bar-row">'
            f'<div class="bar-label" title="{_esc(r.get("name", ""))}">{_esc(name)}'
            f'<span class="bar-sub">ID {r.get("id", "")}</span></div>'
            f'<div class="bar-track">'
            f'<div class="bar-fill" style="width:{pct:.1f}%;background:{_bar_color(ratio)}">'
            f'</div></div>'
            f'<div class="bar-value">{r["size_mb"]:.2f} MB</div>'
            f'</div>'
        )

    fmt_rows = []
    for fd in memory["format_distribution"]:
        fmt_rows.append(
            f'<tr><td>{_esc(fd["format"])}</td>'
            f'<td class="num">{fd["count"]}</td>'
            f'<td class="num">{fd["size_mb"]:.2f} MB</td></tr>'
        )

    memory_html = f"""
    <div class="cards-grid mini">
      <div class="card">
        <div class="card-label">Texture Memory</div>
        <div class="card-value">{_fmt_mb(memory['total_textures_mb'])}</div>
      </div>
      <div class="card">
        <div class="card-label">Buffer Memory</div>
        <div class="card-value">{_fmt_mb(memory['total_buffers_mb'])}</div>
      </div>
    </div>
    <div class="two-col">
      <div>
        <h4>Largest Resources</h4>
        <div class="bar-chart">{"".join(mem_bars)}</div>
      </div>
      <div>
        <h4>Format Distribution</h4>
        <div class="table-wrap">
        <table class="data-table compact">
          <thead><tr><th>Format</th><th>Count</th><th>Est. Size</th></tr></thead>
          <tbody>{"".join(fmt_rows)}</tbody>
        </table>
        </div>
      </div>
    </div>
    """

    # Section 8: Overdraw Estimation
    if overdraw_data.get("available"):
        od_per_pass = overdraw_data.get("per_pass") or []
        od_frame_avg = overdraw_data.get("frame_avg_overdraw", 0.0)
        od_worst = overdraw_data.get("worst_pass", "")
        od_max = max((p["overdraw"] for p in od_per_pass), default=1.0)

        od_bars = []
        for p in od_per_pass:
            od = p["overdraw"]
            pct = od / max(od_max, 0.01) * 100
            color = "#ef4444" if od >= 4 else "#f97316" if od >= 2 else "#22c55e"
            od_bars.append(
                f'<div class="bar-row">'
                f'<div class="bar-label">{_esc(p["pass"])}'
                f'<span class="bar-sub">{_esc(p["rt_size"])}</span></div>'
                f'<div class="bar-track">'
                f'<div class="bar-fill" style="width:{pct:.1f}%;background:{color}"></div></div>'
                f'<div class="bar-value" style="color:{color}">{od:.2f}x</div>'
                f'</div>'
            )

        od_table_rows = []
        for p in od_per_pass:
            od = p["overdraw"]
            sev = p.get("severity", "ok")
            sev_color = "#ef4444" if sev == "high" else "#f97316" if sev == "warn" else "#22c55e"
            od_table_rows.append(
                f'<tr>'
                f'<td>{_esc(p["pass"])}</td>'
                f'<td class="num">{p["eid_range"][0]}-{p["eid_range"][1]}</td>'
                f'<td class="num">{_esc(p["rt_size"])}</td>'
                f'<td class="num">{_fmt_number(p["ps_invocations"])}</td>'
                f'<td class="num" style="color:{sev_color};font-weight:600">{od:.2f}x</td>'
                f'<td><span style="color:{sev_color}">{_esc(sev)}</span></td>'
                f'</tr>'
            )

        overdraw_html = f"""
    <div class="mini-cards">
      <div class="mini-card">
        <div class="mini-label">Frame Avg Overdraw</div>
        <div class="mini-value">{od_frame_avg:.2f}x</div>
      </div>
      <div class="mini-card">
        <div class="mini-label">Worst Pass</div>
        <div class="mini-value small">{_esc(od_worst)}</div>
      </div>
      <div class="mini-card">
        <div class="mini-label">Passes Analyzed</div>
        <div class="mini-value">{len(od_per_pass)}</div>
      </div>
    </div>
    <h4>Overdraw per Pass <span style="font-size:11px;color:var(--text-muted);font-weight:400">(red ≥4x &nbsp; orange ≥2x &nbsp; green &lt;2x)</span></h4>
    <div class="chart-area">{"".join(od_bars)}</div>
    <div class="table-wrap">
    <table class="data-table sortable">
      <thead><tr>
        <th>Pass</th><th>EID Range</th><th>RT Size</th><th>PS Invocations</th><th>Overdraw</th><th>Severity</th>
      </tr></thead>
      <tbody>{"".join(od_table_rows)}</tbody>
    </table>
    </div>
    """
    else:
        od_reason = overdraw_data.get("reason", "No overdraw data available")
        overdraw_html = f'<p class="text-muted">{_esc(od_reason)}</p>'

    # Section 9: Mipmap Usage
    mu_per_texture = mipmap_data.get("per_texture") or []
    mu_total_wasted = mipmap_data.get("total_wasted_mb", 0.0)
    if mu_per_texture:
        mu_table_rows = []
        for t in mu_per_texture:
            vr = t.get("viewed_mip_range") or [0, 0]
            unviewed = t.get("unviewed_mips") or []
            wasted = t.get("wasted_mb", 0.0)
            color = "#ef4444" if wasted >= 1.0 else "#f97316" if wasted >= 0.1 else "#6b7280"
            mu_table_rows.append(
                f'<tr>'
                f'<td>{_esc(t.get("name", str(t.get("resource_id", ""))))}</td>'
                f'<td class="num">{t.get("total_mips", 0)}</td>'
                f'<td class="num">{vr[0]}–{vr[1]}</td>'
                f'<td class="num">{", ".join(str(k) for k in unviewed)}</td>'
                f'<td class="num" style="color:{color};font-weight:600">{wasted:.3f}</td>'
                f'<td>{_esc(t.get("recommendation", ""))}</td>'
                f'</tr>'
            )
        mipmap_html = f"""
    <div class="mini-cards">
      <div class="mini-card">
        <div class="mini-label">Total Wasted</div>
        <div class="mini-value">{mu_total_wasted:.2f} MB</div>
      </div>
      <div class="mini-card">
        <div class="mini-label">Affected Textures</div>
        <div class="mini-value">{len(mu_per_texture)}</div>
      </div>
    </div>
    <p style="font-size:12px;color:var(--text-muted);margin:4px 0 12px">
      View-level waste: mip layers outside all VkImageView ranges — never accessible by any shader.
    </p>
    <div class="table-wrap">
    <table class="data-table sortable">
      <thead><tr>
        <th>Texture</th><th>Total Mips</th><th>Viewed Range</th><th>Unviewed Mips</th><th>Wasted MB</th><th>Recommendation</th>
      </tr></thead>
      <tbody>{"".join(mu_table_rows)}</tbody>
    </table>
    </div>
    """
    else:
        mipmap_html = '<p class="text-muted">No mipmap view-level waste detected (or binding_views.json not available — re-run collect.py).</p>'

    # Section 10: Suggestions
    suggestion_cards = []
    for s in suggestions:
        suggestion_cards.append(
            f'<div class="suggestion-card sev-{s["severity"]}">'
            f'{_severity_icon(s["severity"])}'
            f'<div class="suggestion-body">'
            f'<div class="suggestion-title">{_esc(s["title"])}</div>'
            f'<div class="suggestion-detail">{_esc(s["detail"])}</div>'
            f'<span class="suggestion-cat">{_esc(s.get("category", ""))}</span>'
            f'</div></div>'
        )

    suggestions_html = "".join(suggestion_cards) if suggestion_cards else '<div class="text-muted">No optimization suggestions.</div>'

    # ── Assemble full HTML ──
    html = f"""<!DOCTYPE html>
<html lang="en">
<head>
<meta charset="UTF-8">
<meta name="viewport" content="width=device-width, initial-scale=1.0">
<title>GPU Performance Report - {_esc(capture_name)}</title>
<link rel="stylesheet" href="__ASSETS__/rdc-common.css">
<style>
body {{
  line-height: 1.5;
  overflow-y: auto;
}}

body::before {{
  opacity: 0.4;
}}

/* ── Top bar ── */
#topbar {{
  position: sticky;
  top: 0;
  height: 52px;
  background: rgba(13,16,23,0.92);
  backdrop-filter: blur(16px) saturate(1.3);
  -webkit-backdrop-filter: blur(16px) saturate(1.3);
  border-bottom: 1px solid var(--border-subtle);
  display: flex;
  align-items: center;
  padding: 0 28px;
  z-index: 50;
  gap: 16px;
}}

#topbar .logo {{
  display: flex;
  align-items: center;
  gap: 10px;
  font-family: var(--font-mono);
  font-weight: 700;
  font-size: 15px;
  color: var(--accent-cyan);
  letter-spacing: -0.02em;
}}

#topbar .logo::before {{
  content: '';
  width: 8px; height: 8px;
  background: var(--accent-cyan);
  border-radius: 50%;
  box-shadow: 0 0 8px var(--accent-cyan);
}}

#topbar .capture-name {{
  font-family: var(--font-mono);
  font-size: 13px;
  color: var(--text-secondary);
}}

/* ── Main layout ── */
.main-content {{
  position: relative;
  z-index: 1;
  max-width: 1400px;
  margin: 0 auto;
  padding: 24px 28px 80px;
}}

/* ── Sections ── */
.section {{
  margin-bottom: 32px;
}}

.section-header {{
  display: flex;
  align-items: center;
  gap: 12px;
  padding: 14px 0 10px;
  border-bottom: 1px solid var(--border-subtle);
  margin-bottom: 18px;
  cursor: pointer;
  user-select: none;
}}

.section-header h2 {{
  font-family: var(--font-sans);
  font-size: 18px;
  font-weight: 600;
  color: var(--text-primary);
  letter-spacing: -0.01em;
}}

.section-header .section-num {{
  font-family: var(--font-mono);
  font-size: 12px;
  color: var(--accent-cyan);
  background: var(--accent-cyan-dim);
  padding: 2px 8px;
  border-radius: 4px;
}}

.section-header .toggle {{
  margin-left: auto;
  font-size: 14px;
  color: var(--text-muted);
  transition: transform 0.2s;
}}

.section.collapsed .section-body {{ display: none; }}
.section.collapsed .toggle {{ transform: rotate(-90deg); }}

/* ── Cards grid ── */
.cards-grid {{
  display: grid;
  grid-template-columns: repeat(auto-fill, minmax(150px, 1fr));
  gap: 12px;
  margin-bottom: 20px;
}}

.cards-grid.mini {{
  grid-template-columns: repeat(auto-fill, minmax(180px, 1fr));
  margin-bottom: 16px;
}}

.card {{
  background: var(--bg-card);
  border: 1px solid var(--border-subtle);
  border-radius: 8px;
  padding: 14px 16px;
  transition: border-color 0.15s;
}}

.card:hover {{
  border-color: var(--border-mid);
}}

.card-label {{
  font-family: var(--font-mono);
  font-size: 11px;
  color: var(--text-muted);
  text-transform: uppercase;
  letter-spacing: 0.05em;
  margin-bottom: 6px;
}}

.card-value {{
  font-family: var(--font-mono);
  font-size: 22px;
  font-weight: 600;
  color: var(--text-primary);
  line-height: 1.2;
}}

.card-value.small {{
  font-size: 14px;
}}

.card-sub {{
  font-size: 11px;
  color: var(--text-secondary);
  margin-top: 4px;
}}

.card-inline {{
  font-size: 14px;
  color: var(--text-secondary);
  margin-bottom: 14px;
}}

.card-inline strong {{
  color: var(--accent-cyan);
}}

/* ── Tables ── */
.table-wrap {{
  overflow-x: auto;
}}

.data-table {{
  width: 100%;
  border-collapse: collapse;
  font-size: 13px;
}}

.data-table th {{
  text-align: left;
  font-family: var(--font-mono);
  font-size: 11px;
  font-weight: 500;
  color: var(--text-muted);
  text-transform: uppercase;
  letter-spacing: 0.04em;
  padding: 8px 12px;
  border-bottom: 1px solid var(--border-mid);
  position: sticky;
  top: 0;
  background: var(--bg-deep);
  cursor: pointer;
  user-select: none;
  white-space: nowrap;
}}

.data-table th:hover {{
  color: var(--text-secondary);
}}

.data-table th::after {{
  content: '';
  margin-left: 4px;
  opacity: 0.3;
}}

.data-table td {{
  padding: 7px 12px;
  border-bottom: 1px solid var(--border-subtle);
  color: var(--text-secondary);
  vertical-align: top;
}}

.data-table tr:hover td {{
  background: rgba(0,212,255,0.03);
  color: var(--text-primary);
}}

.data-table .num {{
  text-align: right;
  font-family: var(--font-mono);
  font-size: 12px;
}}

.data-table .mono {{
  font-family: var(--font-mono);
  font-size: 12px;
}}

.data-table.compact {{
  font-size: 12px;
}}

.data-table.compact td,
.data-table.compact th {{
  padding: 5px 10px;
}}

.cat-dot {{
  display: inline-block;
  width: 8px;
  height: 8px;
  border-radius: 50%;
  margin-right: 8px;
  vertical-align: middle;
}}

.rt-cell {{
  font-family: var(--font-mono);
  font-size: 11px;
  line-height: 1.6;
}}

.rt-color {{ color: var(--heat-cool); font-weight: 600; }}
.rt-depth {{ color: var(--heat-warm); font-weight: 600; }}

.inline-bar {{
  height: 3px;
  background: var(--accent-cyan);
  opacity: 0.3;
  border-radius: 2px;
  margin-top: 3px;
}}

/* ── Stage tags ── */
.stage-tag {{
  display: inline-block;
  padding: 2px 8px;
  border-radius: 4px;
  font-size: 11px;
  font-weight: 600;
  font-family: var(--font-mono);
}}
.tag-fs {{
  display: inline-block;
  padding: 1px 5px;
  border-radius: 3px;
  font-size: 10px;
  font-weight: 600;
  background: var(--accent-cyan);
  color: var(--bg-base);
  margin-left: 6px;
}}
.tag-pattern {{
  display: inline-block;
  background: #6366f120;
  color: #6366f1;
  border: 1px solid #6366f140;
  padding: 1px 5px;
  border-radius: 3px;
  font-size: 10px;
  font-weight: 600;
  margin-left: 4px;
}}

/* ── Bloom chain ── */
.bloom-chain {{
  display: flex;
  align-items: center;
  gap: 4px;
  padding: 16px;
  overflow-x: auto;
  background: var(--bg-card);
  border-radius: 8px;
  margin-bottom: 20px;
}}
.bloom-step {{
  display: flex;
  flex-direction: column;
  align-items: center;
  min-width: 80px;
}}
.bloom-arrow {{
  font-size: 20px;
  color: var(--heat-warm);
  margin-bottom: 4px;
}}
.bloom-res {{
  font-family: var(--font-mono);
  font-size: 12px;
  color: var(--text-primary);
  font-weight: 600;
}}
.bloom-label {{
  font-size: 10px;
  color: var(--text-muted);
  text-transform: uppercase;
}}

/* ── Instruction stacked bar segments ── */
.stack-seg {{
  height: 18px;
  min-width: 2px;
}}

/* ── Heatmap table ── */
.heatmap-table th, .heatmap-table td {{ text-align: center; padding: 4px 6px; }}
.hm-th {{ font-size: 10px; writing-mode: vertical-lr; transform: rotate(180deg); max-width: 30px; height: 100px; }}
.hm-label {{ text-align: left !important; white-space: nowrap; font-size: 11px; }}
.hm-cell {{ font-size: 11px; min-width: 28px; color: var(--text-primary); }}

/* ── Bar charts ── */
.bar-chart {{
  display: flex;
  flex-direction: column;
  gap: 6px;
}}

.bar-row {{
  display: grid;
  grid-template-columns: 200px 1fr 80px;
  align-items: center;
  gap: 12px;
}}

.bar-label {{
  font-size: 12px;
  font-family: var(--font-mono);
  color: var(--text-secondary);
  white-space: nowrap;
  overflow: hidden;
  text-overflow: ellipsis;
}}

.bar-sub {{
  display: block;
  font-size: 10px;
  color: var(--text-muted);
  overflow: hidden;
  text-overflow: ellipsis;
}}

.bar-track {{
  height: 14px;
  background: var(--bg-elevated);
  border-radius: 3px;
  overflow: hidden;
}}

.bar-fill {{
  height: 100%;
  border-radius: 3px;
  transition: width 0.3s ease;
  min-width: 2px;
}}

.bar-value {{
  font-family: var(--font-mono);
  font-size: 12px;
  color: var(--text-primary);
  text-align: right;
  white-space: nowrap;
}}

/* ── Gantt chart ── */
.gantt-container {{
  margin-bottom: 20px;
}}

.gantt-track {{
  position: relative;
  height: 36px;
  background: var(--bg-card);
  border: 1px solid var(--border-subtle);
  border-radius: 6px;
  overflow: hidden;
}}

.gantt-bar {{
  position: absolute;
  top: 4px;
  height: 28px;
  border-radius: 4px;
  display: flex;
  align-items: center;
  padding: 0 6px;
  overflow: hidden;
  opacity: 0.85;
  transition: opacity 0.15s;
  cursor: default;
}}

.gantt-bar:hover {{
  opacity: 1;
  z-index: 2;
}}

.gantt-label {{
  font-family: var(--font-mono);
  font-size: 10px;
  color: #fff;
  white-space: nowrap;
  overflow: hidden;
  text-overflow: ellipsis;
  text-shadow: 0 1px 3px rgba(0,0,0,0.5);
}}

.gantt-axis {{
  display: flex;
  justify-content: space-between;
  padding: 4px 2px 0;
  font-family: var(--font-mono);
  font-size: 10px;
  color: var(--text-muted);
}}

/* ── Two column layout ── */
.two-col {{
  display: grid;
  grid-template-columns: 1fr 1fr;
  gap: 24px;
  margin-bottom: 16px;
}}

@media (max-width: 900px) {{
  .two-col {{ grid-template-columns: 1fr; }}
  .bar-row {{ grid-template-columns: 140px 1fr 60px; }}
}}

h4 {{
  font-size: 14px;
  font-weight: 500;
  color: var(--text-secondary);
  margin-bottom: 12px;
}}

/* ── Suggestions ── */
.suggestion-card {{
  display: flex;
  gap: 14px;
  padding: 14px 18px;
  background: var(--bg-card);
  border: 1px solid var(--border-subtle);
  border-radius: 8px;
  margin-bottom: 10px;
  align-items: flex-start;
  transition: border-color 0.15s;
}}

.suggestion-card:hover {{
  border-color: var(--border-mid);
}}

.suggestion-card.sev-warning {{
  border-left: 3px solid var(--heat-warm);
}}

.suggestion-card.sev-critical {{
  border-left: 3px solid var(--heat-fire);
}}

.suggestion-card.sev-info {{
  border-left: 3px solid var(--heat-cold);
}}

.sev-icon {{
  display: flex;
  align-items: center;
  justify-content: center;
  width: 24px;
  height: 24px;
  border-radius: 50%;
  font-family: var(--font-mono);
  font-size: 12px;
  font-weight: 700;
  flex-shrink: 0;
}}

.sev-icon.sev-warning {{
  background: rgba(212,160,23,0.15);
  color: var(--heat-warm);
}}

.sev-icon.sev-critical {{
  background: rgba(212,37,80,0.15);
  color: var(--heat-fire);
}}

.sev-icon.sev-info {{
  background: rgba(26,140,170,0.15);
  color: var(--heat-cold);
}}

.suggestion-body {{
  flex: 1;
}}

.suggestion-title {{
  font-size: 14px;
  font-weight: 600;
  color: var(--text-primary);
  margin-bottom: 4px;
}}

.suggestion-detail {{
  font-size: 13px;
  color: var(--text-secondary);
  line-height: 1.5;
}}

.suggestion-cat {{
  display: inline-block;
  margin-top: 6px;
  font-family: var(--font-mono);
  font-size: 10px;
  color: var(--text-muted);
  background: var(--bg-elevated);
  padding: 1px 8px;
  border-radius: 3px;
}}

.text-muted {{ color: var(--text-muted); }}
</style>
</head>
<body>

<div id="topbar">
  <div class="logo">GPU PERF REPORT</div>
  <div class="capture-name">{_esc(capture_name)}</div>
</div>

<div class="main-content">

  <div class="section" id="sec-overview">
    <div class="section-header" onclick="toggleSection('sec-overview')">
      <span class="section-num">01</span>
      <h2>Frame Overview</h2>
      <span class="toggle">&#9660;</span>
    </div>
    <div class="section-body">{overview_cards}</div>
  </div>

  <div class="section" id="sec-pipeline">
    <div class="section-header" onclick="toggleSection('sec-pipeline')">
      <span class="section-num">02</span>
      <h2>Rendering Pipeline</h2>
      <span class="toggle">&#9660;</span>
    </div>
    <div class="section-body">{pipeline_html}</div>
  </div>

  <div class="section" id="sec-stages">
    <div class="section-header" onclick="toggleSection('sec-stages')">
      <span class="section-num">03</span>
      <h2>Pipeline Stage Analysis</h2>
      <span class="toggle">&#9660;</span>
    </div>
    <div class="section-body">{pipeline_stages_html}</div>
  </div>

  <div class="section" id="sec-hotspots">
    <div class="section-header" onclick="toggleSection('sec-hotspots')">
      <span class="section-num">04</span>
      <h2>Triangle &amp; Draw Call Hotspots</h2>
      <span class="toggle">&#9660;</span>
    </div>
    <div class="section-body">{hotspots_html}</div>
  </div>

  <div class="section" id="sec-bandwidth">
    <div class="section-header" onclick="toggleSection('sec-bandwidth')">
      <span class="section-num">05</span>
      <h2>Bandwidth Estimation</h2>
      <span class="toggle">&#9660;</span>
    </div>
    <div class="section-body">{bandwidth_html}</div>
  </div>

  <div class="section" id="sec-shaders">
    <div class="section-header" onclick="toggleSection('sec-shaders')">
      <span class="section-num">06</span>
      <h2>Shader Complexity</h2>
      <span class="toggle">&#9660;</span>
    </div>
    <div class="section-body">{shaders_html}</div>
  </div>

  <div class="section" id="sec-memory">
    <div class="section-header" onclick="toggleSection('sec-memory')">
      <span class="section-num">07</span>
      <h2>Texture &amp; Memory</h2>
      <span class="toggle">&#9660;</span>
    </div>
    <div class="section-body">{memory_html}</div>
  </div>

  <div class="section" id="sec-overdraw">
    <div class="section-header" onclick="toggleSection('sec-overdraw')">
      <span class="section-num">08</span>
      <h2>Overdraw Estimation</h2>
      <span class="toggle">&#9660;</span>
    </div>
    <div class="section-body">{overdraw_html}</div>
  </div>

  <div class="section" id="sec-mipmap">
    <div class="section-header" onclick="toggleSection('sec-mipmap')">
      <span class="section-num">09</span>
      <h2>Mipmap Usage</h2>
      <span class="toggle">&#9660;</span>
    </div>
    <div class="section-body">{mipmap_html}</div>
  </div>

  <div class="section" id="sec-suggestions">
    <div class="section-header" onclick="toggleSection('sec-suggestions')">
      <span class="section-num">10</span>
      <h2>Optimization Suggestions</h2>
      <span class="toggle">&#9660;</span>
    </div>
    <div class="section-body">{suggestions_html}</div>
  </div>

</div>

<script>
function toggleSection(id) {{
  document.getElementById(id).classList.toggle('collapsed');
}}

// ── Sortable tables ──
document.querySelectorAll('.data-table.sortable').forEach(table => {{
  const headers = table.querySelectorAll('th');
  headers.forEach((th, colIdx) => {{
    th.addEventListener('click', () => {{
      const tbody = table.querySelector('tbody');
      const rows = Array.from(tbody.querySelectorAll('tr'));
      const asc = th.dataset.sort !== 'asc';
      headers.forEach(h => delete h.dataset.sort);
      th.dataset.sort = asc ? 'asc' : 'desc';
      rows.sort((a, b) => {{
        let av = a.children[colIdx]?.textContent.trim() || '';
        let bv = b.children[colIdx]?.textContent.trim() || '';
        const an = parseFloat(av.replace(/,/g, ''));
        const bn = parseFloat(bv.replace(/,/g, ''));
        if (!isNaN(an) && !isNaN(bn)) {{
          return asc ? an - bn : bn - an;
        }}
        return asc ? av.localeCompare(bv) : bv.localeCompare(av);
      }});
      rows.forEach(r => tbody.appendChild(r));
    }});
  }});
}});
</script>

</body>
</html>"""

    return html.replace("__ASSETS__", assets_rel)


# ─────────────────────────────────────────────────────────────────────
# Main
# ─────────────────────────────────────────────────────────────────────

def main():
    parser = argparse.ArgumentParser(
        description="GPU Frame Performance Analysis Report Generator",
    )
    parser.add_argument(
        "analysis_dir", type=Path,
        help="Path to *-analysis/ directory produced by rdc_collect.py",
    )
    args = parser.parse_args()

    analysis_dir = args.analysis_dir
    if not analysis_dir.is_dir():
        print(f"ERROR: {analysis_dir} is not a directory")
        sys.exit(1)

    print(f"Loading analysis data from {analysis_dir} ...")
    data = load_analysis(analysis_dir)

    if not data.get("summary"):
        print(f"ERROR: summary.json not found in {analysis_dir}")
        sys.exit(1)

    print("Analyzing ...")
    analysis = {
        "overview": analyze_frame_overview(data),
        "pipeline": analyze_pipeline(data),
        "pipeline_stages": analyze_pipeline_stages(data),
        "hotspots": analyze_hotspots(data),
        "bandwidth": analyze_bandwidth(data),
        "shaders": analyze_shaders(data),
        "memory": analyze_memory(data),
        "overdraw": analyze_overdraw(data),
        "mipmap_usage": analyze_mipmap_usage(data),
    }
    analysis["suggestions"] = generate_suggestions(analysis, data)

    capture_name = analysis_dir.stem.replace("-analysis", "")
    html_assets_dir = Path(__file__).resolve().parent.parent.parent / "assets"
    assets_rel = os.path.relpath(html_assets_dir, analysis_dir).replace("\\", "/")
    print("Generating HTML report ...")
    html = render_html(analysis, capture_name, assets_rel=assets_rel)

    analysis_json_path = analysis_dir / "json" / "analysis.json"
    analysis_json_path.parent.mkdir(parents=True, exist_ok=True)
    write_json(analysis_json_path, analysis)
    print(f"[OK] Analysis JSON: {analysis_json_path}")

    out_path = analysis_dir / "performance_report.html"
    out_path.write_text(html, encoding="utf-8")
    print(f"Report saved: {out_path}")


if __name__ == "__main__":
    main()
