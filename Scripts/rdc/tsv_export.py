# -*- coding: utf-8 -*-
"""TSV export: token-efficient tabular format for LLM analysis."""

from __future__ import annotations

import json
from pathlib import Path

from rpc import _unwrap
from shared import (
    classify_pass_stage, detect_bloom_chain, detect_fullscreen_quad,
    analyze_spirv_instructions, estimate_register_pressure, deduplicate_shaders,
)


def write_tsv(path: Path, headers: list[str], rows: list[list]) -> None:
    lines = ["\t".join(headers)]
    for row in rows:
        cells = []
        for v in row:
            if v is None:
                cells.append("")
            elif isinstance(v, bool):
                cells.append("1" if v else "0")
            elif isinstance(v, (dict, list)):
                cells.append(json.dumps(v, ensure_ascii=False, separators=(",", ":")))
            else:
                cells.append(str(v))
        lines.append("\t".join(cells))
    path.write_text("\n".join(lines) + "\n", encoding="utf-8")


def _build_passes(pass_details: list[dict]) -> tuple[list[str], list[list]]:
    headers = [
        "name", "begin_eid", "end_eid", "draws", "dispatches",
        "copies", "clears", "triangles",
        "n_color", "color_formats", "depth_format", "load_ops", "store_ops",
    ]
    rows = []
    for p in pass_details:
        cts = p.get("color_targets") or []
        dt = p.get("depth_target")
        rows.append([
            p.get("name", ""),
            p.get("begin_eid", 0),
            p.get("end_eid", 0),
            p.get("draws", 0),
            p.get("dispatches", 0),
            p.get("copies", 0),
            p.get("clears", 0),
            p.get("triangles", 0),
            len(cts),
            ";".join(ct.get("format", "") for ct in cts if isinstance(ct, dict)),
            dt.get("format", "") if isinstance(dt, dict) else "",
            ";".join(f"{op[0]}:{op[1]}" for op in (p.get("load_ops") or []) if len(op) >= 2),
            ";".join(f"{op[0]}:{op[1]}" for op in (p.get("store_ops") or []) if len(op) >= 2),
        ])
    return headers, rows


def _build_draws(summary: dict, pipelines: dict) -> tuple[list[str], list[list]]:
    headers = [
        "eid", "type", "triangles", "instances", "pass", "marker",
        "topology", "graphics_pipeline", "compute_pipeline",
    ]
    draws = _unwrap(summary.get("draws"), "draws") or []
    rows = []
    for d in draws:
        if not isinstance(d, dict):
            continue
        eid = d.get("eid", 0)
        pipe = pipelines.get(str(eid)) or {}
        rows.append([
            eid,
            d.get("type", ""),
            d.get("triangles", 0),
            d.get("instances", 0),
            d.get("pass", ""),
            d.get("marker", ""),
            pipe.get("topology", ""),
            pipe.get("graphics_pipeline", ""),
            pipe.get("compute_pipeline", ""),
        ])
    return headers, rows


def _build_bindings(bindings: dict) -> tuple[list[str], list[list]]:
    headers = ["eid", "stage", "kind", "set", "slot", "name"]
    rows = []
    for eid_str in sorted(bindings, key=lambda k: int(k)):
        for b in bindings[eid_str]:
            if not isinstance(b, dict):
                continue
            rows.append([
                b.get("eid", eid_str),
                b.get("stage", ""),
                b.get("kind", ""),
                b.get("set", ""),
                b.get("slot", ""),
                b.get("name", ""),
            ])
    return headers, rows


def _build_resources(resource_details: dict) -> tuple[list[str], list[list]]:
    headers = [
        "id", "name", "type", "format", "width", "height", "depth",
        "mips", "array_size", "byte_size", "length", "creation_flags", "gpu_address",
    ]
    rows = []
    for rid in sorted(resource_details, key=lambda k: int(k)):
        r = resource_details[rid]
        if not isinstance(r, dict) or "_error" in r:
            continue
        rows.append([
            r.get("id", rid),
            r.get("name", ""),
            r.get("type", ""),
            r.get("format", ""),
            r.get("width", ""),
            r.get("height", ""),
            r.get("depth", ""),
            r.get("mips", ""),
            r.get("array_size", ""),
            r.get("byte_size", ""),
            r.get("length", ""),
            r.get("creation_flags", ""),
            r.get("gpu_address", ""),
        ])
    return headers, rows


def _build_frame_info(summary: dict) -> tuple[list[str], list[list]]:
    info = summary.get("info") or {}
    headers = ["key", "value"]
    rows = [[k, v] for k, v in info.items()]
    return headers, rows


def _build_counters(summary: dict) -> tuple[list[str], list[list]]:
    counters = summary.get("counters") or {}
    raw_rows = counters.get("rows") or (counters if isinstance(counters, list) else [])
    if not raw_rows:
        return [], []
    headers = ["eid", "counter", "value", "unit"]
    rows = []
    for r in raw_rows:
        if not isinstance(r, dict):
            continue
        rows.append([
            r.get("eid", ""),
            r.get("counter", ""),
            r.get("value", ""),
            r.get("unit", ""),
        ])
    return headers, rows


def _build_events(summary: dict) -> tuple[list[str], list[list]]:
    events = _unwrap(summary.get("events"), "events") or []
    if not events:
        return [], []
    headers = ["eid", "type", "name"]
    rows = []
    for e in events:
        if not isinstance(e, dict):
            continue
        rows.append([e.get("eid", ""), e.get("type", ""), e.get("name", "")])
    return headers, rows


def _build_deps(summary: dict) -> tuple[list[str], list[list]]:
    pass_deps = summary.get("pass_deps") or {}
    edges = pass_deps.get("edges") or []
    if not edges:
        return [], []
    headers = ["src", "dst", "resources"]
    rows = []
    for e in edges:
        if not isinstance(e, dict):
            continue
        rids = e.get("resources") or []
        rows.append([
            e.get("src", ""),
            e.get("dst", ""),
            ",".join(str(r) for r in rids),
        ])
    return headers, rows


def _build_pass_rw(summary: dict) -> tuple[list[str], list[list]]:
    pass_deps = summary.get("pass_deps") or {}
    per_pass = pass_deps.get("per_pass") or []
    if not per_pass:
        return [], []
    headers = ["pass", "reads", "writes"]
    rows = []
    for pp in per_pass:
        if not isinstance(pp, dict):
            continue
        rows.append([
            pp.get("name", ""),
            ",".join(str(r) for r in (pp.get("reads") or [])),
            ",".join(str(r) for r in (pp.get("writes") or [])),
        ])
    return headers, rows


def _build_overdraw(computed: dict) -> tuple[list[str], list[list]]:
    od = (computed or {}).get("overdraw") or {}
    if not od.get("available"):
        return [], []
    headers = ["pass", "eid_range", "rt_size", "rt_pixels", "ps_invocations", "overdraw", "severity"]
    rows = []
    for p in od.get("per_pass") or []:
        eids = p.get("eid_range") or [0, 0]
        rows.append([
            p.get("pass", ""),
            f"{eids[0]}-{eids[1]}",
            p.get("rt_size", ""),
            p.get("rt_pixels", 0),
            p.get("ps_invocations", 0),
            p.get("overdraw", 0.0),
            p.get("severity", ""),
        ])
    return headers, rows


def _build_mipmap_usage(computed: dict) -> tuple[list[str], list[list]]:
    mu = (computed or {}).get("mipmap_usage") or {}
    per_texture = mu.get("per_texture") or []
    if not per_texture:
        return [], []
    headers = [
        "resource_id", "name", "total_mips", "viewed_range",
        "unviewed_mips", "wasted_mb", "recommendation",
    ]
    rows = []
    for t in per_texture:
        vr = t.get("viewed_mip_range") or [0, 0]
        unviewed = t.get("unviewed_mips") or []
        rows.append([
            t.get("resource_id", ""),
            t.get("name", ""),
            t.get("total_mips", 0),
            f"{vr[0]}-{vr[1]}",
            ",".join(str(k) for k in unviewed),
            t.get("wasted_mb", 0.0),
            t.get("recommendation", ""),
        ])
    return headers, rows


def _build_tbdr(computed: dict) -> tuple[list[str], list[list]]:
    tbdr = (computed or {}).get("tbdr") or {}
    if not tbdr.get("available"):
        return [], []
    issues = tbdr.get("issues") or []
    if not issues:
        return [], []
    headers = ["pass", "attachment", "type", "format", "rt_size", "tile_mb", "load_op", "store_op", "issue", "recommendation"]
    rows = []
    for a in issues:
        rows.append([
            a.get("pass", ""),
            a.get("attachment", ""),
            a.get("attachment_type", ""),
            a.get("format", ""),
            a.get("rt_size", ""),
            a.get("tile_mb", 0.0),
            a.get("load_op", ""),
            a.get("store_op", ""),
            a.get("issue", ""),
            a.get("recommendation", ""),
        ])
    return headers, rows


def _build_vertex_efficiency(computed: dict) -> tuple[list[str], list[list]]:
    ve = (computed or {}).get("vertex_efficiency") or {}
    if not ve.get("available"):
        return [], []
    issues = ve.get("issues") or []
    if not issues:
        return [], []
    headers = ["eid", "file", "vertex_count", "index_count", "reuse_ratio",
               "stride_bytes", "issue_type", "semantic", "format", "recommendation"]
    rows = []
    for item in issues:
        for issue in (item.get("issues") or []):
            rows.append([
                item.get("eid", ""),
                item.get("file", ""),
                item.get("vertex_count", 0),
                item.get("index_count", 0),
                item.get("reuse_ratio", 0.0),
                item.get("vertex_stride_bytes", 0),
                issue.get("type", ""),
                issue.get("semantic", ""),
                issue.get("format", ""),
                issue.get("recommendation", ""),
            ])
    return headers, rows


def _build_alerts(computed: dict) -> tuple[list[str], list[list]]:
    alerts = computed.get("alerts") or []
    if not alerts:
        return [], []
    headers = ["severity", "type", "eid", "id", "name", "detail"]
    rows = []
    for a in alerts:
        if not isinstance(a, dict):
            continue
        detail = ""
        if "triangles" in a:
            detail = f"triangles={a['triangles']}"
        elif "size_mb" in a:
            detail = f"size_mb={a['size_mb']}"
        rows.append([
            a.get("severity", ""),
            a.get("type", ""),
            a.get("eid", ""),
            a.get("id", ""),
            a.get("name", a.get("pass", "")),
            detail,
        ])
    return headers, rows


def _build_shaders(shader_disasm: dict) -> tuple[list[str], list[list]]:
    headers = ["key", "vs_id", "ps_id", "cs_id", "uses", "eids", "file"]
    rows = []
    for key, s in sorted(shader_disasm.items(), key=lambda kv: -kv[1].get("uses", 0)):
        if not isinstance(s, dict):
            continue
        eids = s.get("eids") or []
        rows.append([
            key,
            s.get("vs_id", ""),
            s.get("ps_id", ""),
            s.get("cs_id", ""),
            s.get("uses", 0),
            ",".join(str(e) for e in eids),
            s.get("file", ""),
        ])
    return headers, rows


def _build_pipeline_stages(
    summary: dict,
    pass_details: list[dict],
    counters_by_eid: dict[int, dict] | None = None,
) -> tuple[list[str], list[list], list[str], list[list]]:
    """Build pipeline_stages.tsv and stage_summary.tsv data."""
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

    draws_list = _unwrap(summary.get("draws"), "draws") or []
    pass_deps = summary.get("pass_deps") or {}
    per_pass_rw = pass_deps.get("per_pass") or []
    rw_by_name: dict[str, dict] = {}
    for pp in per_pass_rw:
        if isinstance(pp, dict):
            rw_by_name[pp.get("name", "")] = pp

    stage_headers = [
        "pass_name", "stage", "reason", "begin_eid", "end_eid",
        "draws", "dispatches", "triangles",
        "gpu_time_us", "ps_invocations", "is_fullscreen", "overdraw",
        "rt_width", "rt_height", "rt_format",
        "writes_to", "reads_from",
    ]
    stage_rows = []
    stage_times: dict[str, float] = {}
    stage_counts: dict[str, int] = {}

    for p in pass_details:
        name = p.get("name", "")
        begin_eid = p.get("begin_eid", 0)
        end_eid = p.get("end_eid", 0)

        stage, reason = classify_pass_stage(
            p,
            all_passes=pass_details,
            bloom_pass_names=bloom_names,
            max_rt_area=max_rt_area,
        )

        gpu_time_us = 0.0
        ps_inv = 0
        if counters_by_eid:
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
        writes = rw.get("writes") or []
        reads = rw.get("reads") or []

        stage_rows.append([
            name, stage, reason, begin_eid, end_eid,
            p.get("draws", 0), p.get("dispatches", 0), p.get("triangles", 0),
            round(gpu_time_us, 1), ps_inv, is_fs, overdraw,
            rt_w, rt_h, rt_fmt,
            ",".join(str(r) for r in writes),
            ",".join(str(r) for r in reads),
        ])

        stage_times[stage] = stage_times.get(stage, 0.0) + gpu_time_us
        stage_counts[stage] = stage_counts.get(stage, 0) + 1

    total_time = sum(stage_times.values())
    summary_headers = ["stage", "passes", "gpu_time_us", "pct"]
    summary_rows = []
    for stage in sorted(stage_times, key=lambda s: -stage_times[s]):
        pct = (stage_times[stage] / total_time * 100) if total_time > 0 else 0
        summary_rows.append([
            stage, stage_counts[stage], round(stage_times[stage], 1), round(pct, 1),
        ])

    return stage_headers, stage_rows, summary_headers, summary_rows


def _build_draw_timing(
    summary: dict,
    pass_details: list[dict],
    counters_by_eid: dict[int, dict] | None = None,
) -> tuple[list[str], list[list]]:
    """Build draw_timing.tsv: per-draw GPU timing sorted by duration descending."""
    headers = ["eid", "gpu_duration_us", "ps_invocations", "vs_invocations", "triangles", "type", "pass"]
    draws = _unwrap(summary.get("draws"), "draws") or []
    rows = []
    for d in draws:
        if not isinstance(d, dict):
            continue
        eid = d.get("eid", 0)
        cdata = (counters_by_eid or {}).get(eid, {})
        gpu_dur_us = round(cdata.get("GPU Duration", 0.0) * 1e6, 2)
        ps_inv = int(cdata.get("PS Invocations", 0))
        vs_inv = int(cdata.get("VS Invocations", 0))
        rows.append([
            eid,
            gpu_dur_us,
            ps_inv,
            vs_inv,
            d.get("triangles", 0),
            d.get("type", ""),
            d.get("pass", ""),
        ])
    rows.sort(key=lambda r: r[1], reverse=True)
    return headers, rows


def _build_counters_by_eid(summary: dict) -> dict[int, dict]:
    """Build a dict mapping eid -> {counter_name: value} from counters data."""
    counters = summary.get("counters") or {}
    raw = counters.get("rows") or (counters if isinstance(counters, list) else [])
    result: dict[int, dict] = {}
    for r in raw:
        if not isinstance(r, dict):
            continue
        eid = r.get("eid")
        if eid is None:
            continue
        eid = int(eid)
        if eid not in result:
            result[eid] = {}
        result[eid][r.get("counter", "")] = r.get("value", 0)
    return result


def _build_shader_instructions(
    shader_disasm: dict, shaders_dir: Path,
) -> tuple[list[str], list[list]]:
    headers = [
        "key", "arithmetic", "sample", "logic", "load_store",
        "dot_matrix", "intrinsic", "barrier", "total",
        "pressure_level", "estimated_vgprs",
    ]
    rows = []
    for key, info in sorted(shader_disasm.items(), key=lambda kv: -kv[1].get("uses", 0)):
        if not isinstance(info, dict):
            continue
        fname = info.get("file", "")
        if not fname:
            continue
        shader_path = shaders_dir / Path(fname).name
        if not shader_path.exists():
            continue
        try:
            content = shader_path.read_text(encoding="utf-8", errors="replace")
        except Exception:
            continue
        is_compute = "cs_id" in info
        inst = analyze_spirv_instructions(content, is_compute)
        rp = estimate_register_pressure(content, is_compute)
        rows.append([
            key,
            inst["arithmetic"], inst["sample"], inst["logic"], inst["load_store"],
            inst["dot_matrix"], inst["intrinsic"], inst["barrier"], inst["total"],
            rp["pressure_level"], rp["estimated_vgprs"],
        ])
    return headers, rows


def _build_shader_variants(
    shader_disasm: dict, shaders_dir: Path,
) -> tuple[list[str], list[list]]:
    variants = deduplicate_shaders(shader_disasm, shaders_dir)
    headers = ["group_key", "variant_key", "spec_ids", "uses"]
    rows = []
    for g in variants.get("groups", []):
        for vk in g["variant_keys"]:
            spec_str = ",".join(
                f'{sid}={vals[g["variant_keys"].index(vk)]}'
                for sid, vals in g.get("spec_diffs", {}).items()
                if g["variant_keys"].index(vk) < len(vals)
            )
            uses = (shader_disasm.get(vk) or {}).get("uses", 0)
            rows.append([g["canonical_key"], vk, spec_str, uses])
    return headers, rows


def _build_shader_pass_matrix(
    shader_disasm: dict, pass_details: list[dict],
) -> tuple[list[str], list[list]]:
    if not pass_details or not shader_disasm:
        return [], []
    pass_names = [p.get("name", f"Pass {i}") for i, p in enumerate(pass_details)]
    pass_ranges = [(p.get("begin_eid", 0), p.get("end_eid", 0)) for p in pass_details]

    headers = ["shader_key"] + pass_names
    rows = []
    for key in sorted(shader_disasm, key=lambda k: -(shader_disasm[k] or {}).get("uses", 0)):
        info = shader_disasm.get(key)
        if not isinstance(info, dict):
            continue
        row = [0] * len(pass_details)
        for eid in info.get("eids", []):
            for pi, (beg, end) in enumerate(pass_ranges):
                if beg <= eid <= end:
                    row[pi] += 1
                    break
        rows.append([key] + row)
    return headers, rows


def export_tsv(
    tsv_dir: Path,
    summary: dict,
    pass_details: list,
    pipelines: dict,
    bindings: dict,
    resource_details: dict,
    shader_disasm: dict,
    computed: dict | None = None,
    shaders_dir: Path | None = None,
) -> None:
    tsv_dir.mkdir(parents=True, exist_ok=True)
    tables = {
        "frame_info": _build_frame_info(summary),
        "passes": _build_passes(pass_details),
        "draws": _build_draws(summary, pipelines),
        "bindings": _build_bindings(bindings),
        "resources": _build_resources(resource_details),
        "shaders": _build_shaders(shader_disasm),
        "counters": _build_counters(summary),
        "events": _build_events(summary),
        "deps": _build_deps(summary),
        "pass_rw": _build_pass_rw(summary),
        "alerts": _build_alerts(computed or {}),
    }

    odh, odr = _build_overdraw(computed or {})
    if odh:
        tables["overdraw"] = (odh, odr)

    muh, mur = _build_mipmap_usage(computed or {})
    if muh:
        tables["mipmap_usage"] = (muh, mur)

    tbh, tbr = _build_tbdr(computed or {})
    if tbh:
        tables["tbdr_efficiency"] = (tbh, tbr)

    veh, ver = _build_vertex_efficiency(computed or {})
    if veh:
        tables["vertex_efficiency"] = (veh, ver)

    counters_by_eid = _build_counters_by_eid(summary)
    sh, sr, sumh, sumr = _build_pipeline_stages(summary, pass_details, counters_by_eid)
    if sh:
        tables["pipeline_stages"] = (sh, sr)
    if sumh:
        tables["stage_summary"] = (sumh, sumr)

    dth, dtr = _build_draw_timing(summary, pass_details, counters_by_eid)
    if dth:
        tables["draw_timing"] = (dth, dtr)

    # Shader analysis tables (require shaders_dir)
    if shaders_dir and shaders_dir.exists():
        sih, sir = _build_shader_instructions(shader_disasm, shaders_dir)
        if sih:
            tables["shader_instructions"] = (sih, sir)
        svh, svr = _build_shader_variants(shader_disasm, shaders_dir)
        if svh:
            tables["shader_variants"] = (svh, svr)

    spmh, spmr = _build_shader_pass_matrix(shader_disasm, pass_details)
    if spmh:
        tables["shader_pass_matrix"] = (spmh, spmr)

    written = 0
    for name, (headers, rows) in tables.items():
        if headers:
            write_tsv(tsv_dir / f"{name}.tsv", headers, rows)
            written += 1
    print(f"  TSV: {written} files")
