# -*- coding: utf-8 -*-
"""TSV export: token-efficient tabular format for LLM analysis."""

from __future__ import annotations

import json
from pathlib import Path

from rpc import _unwrap
from shared import classify_pass_stage, detect_bloom_chain, detect_fullscreen_quad


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


def export_tsv(
    tsv_dir: Path,
    summary: dict,
    pass_details: list,
    pipelines: dict,
    bindings: dict,
    resource_details: dict,
    shader_disasm: dict,
    computed: dict | None = None,
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

    counters_by_eid = _build_counters_by_eid(summary)
    sh, sr, sumh, sumr = _build_pipeline_stages(summary, pass_details, counters_by_eid)
    if sh:
        tables["pipeline_stages"] = (sh, sr)
    if sumh:
        tables["stage_summary"] = (sumh, sumr)

    dth, dtr = _build_draw_timing(summary, pass_details, counters_by_eid)
    if dth:
        tables["draw_timing"] = (dth, dtr)

    written = 0
    for name, (headers, rows) in tables.items():
        if headers:
            write_tsv(tsv_dir / f"{name}.tsv", headers, rows)
            written += 1
    print(f"  TSV: {written} files")
