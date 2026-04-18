# -*- coding: utf-8 -*-
"""Computed analysis: triangle distribution, memory estimation, alerts, dedup."""

from __future__ import annotations

import hashlib
import json
from collections import Counter, defaultdict

from rpc import _unwrap
from shared import estimate_texture_mb

# Alert thresholds
ALERT_HIGH_TRI_DRAW = 10000
ALERT_LARGE_TEX_DIM = 2048
ALERT_LARGE_TEX_MB = 4.0


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
    for d in draws:
        if isinstance(d, dict) and d.get("triangles", 0) > ALERT_HIGH_TRI_DRAW:
            alerts.append({
                "severity": "warning",
                "type": "high_triangle_draw",
                "eid": d["eid"],
                "triangles": d["triangles"],
                "pass": d.get("pass"),
            })
    for p in passes:
        if isinstance(p, dict) and p.get("draws", 0) == 0 and p.get("dispatches", 0) == 0:
            alerts.append({
                "severity": "info",
                "type": "empty_pass",
                "pass": p.get("name", "unknown"),
            })
    for r in largest:
        if r.get("size_mb", 0) > ALERT_LARGE_TEX_MB:
            alerts.append({
                "severity": "warning",
                "type": "large_resource",
                "id": r["id"],
                "name": r.get("name", ""),
                "size_mb": r["size_mb"],
            })
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
    sigs = []
    for p in passes:
        if isinstance(p, dict):
            sigs.append((p.get("draws", 0), p.get("dispatches", 0), p.get("triangles", 0)))
        else:
            sigs.append((0, 0, 0))
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
