# -*- coding: utf-8 -*-
"""TSV export: token-efficient tabular format for LLM analysis."""

from __future__ import annotations

import json
from pathlib import Path

from rpc import _unwrap


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
    written = 0
    for name, (headers, rows) in tables.items():
        if headers:
            write_tsv(tsv_dir / f"{name}.tsv", headers, rows)
            written += 1
    print(f"  TSV: {written} files")
