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


def compute_overdraw(summary: dict, pass_details: list) -> dict:
    """Estimate per-pass overdraw from PS Invocations / RT pixel count."""
    counters = summary.get("counters") or {}
    raw = counters.get("rows") or (counters if isinstance(counters, list) else [])

    ps_inv_by_eid: dict[int, int] = {}
    for r in raw:
        if isinstance(r, dict) and r.get("counter") == "PS Invocations":
            eid = r.get("eid")
            if eid is not None:
                ps_inv_by_eid[int(eid)] = int(r.get("value", 0) or 0)

    if not ps_inv_by_eid:
        return {"available": False, "reason": "PS Invocations counter not exposed"}

    per_pass = []
    total_ps_inv = 0
    total_rt_pixels = 0
    worst_od = 0.0
    worst_pass = ""

    for p in pass_details:
        if not isinstance(p, dict):
            continue
        name = p.get("name", "")
        begin_eid = p.get("begin_eid", 0)
        end_eid = p.get("end_eid", 0)

        cts = p.get("color_targets") or []
        rt_w = cts[0].get("width", 0) if cts else 0
        rt_h = cts[0].get("height", 0) if cts else 0
        if not cts:
            dt = p.get("depth_target")
            if isinstance(dt, dict):
                rt_w = dt.get("width", 0)
                rt_h = dt.get("height", 0)
        rt_pixels = rt_w * rt_h

        ps_inv = sum(v for eid, v in ps_inv_by_eid.items() if begin_eid <= eid <= end_eid)

        if rt_pixels == 0 or ps_inv == 0:
            continue

        overdraw = round(ps_inv / rt_pixels, 2)
        severity = "high" if overdraw > 4 else "warn" if overdraw > 2 else "ok"

        per_pass.append({
            "pass": name,
            "eid_range": [begin_eid, end_eid],
            "rt_size": f"{rt_w}x{rt_h}",
            "rt_pixels": rt_pixels,
            "ps_invocations": ps_inv,
            "overdraw": overdraw,
            "severity": severity,
        })

        total_ps_inv += ps_inv
        total_rt_pixels += rt_pixels
        if overdraw > worst_od:
            worst_od = overdraw
            worst_pass = name

    frame_avg = round(total_ps_inv / total_rt_pixels, 2) if total_rt_pixels > 0 else 0.0
    return {
        "available": True,
        "per_pass": per_pass,
        "frame_avg_overdraw": frame_avg,
        "worst_pass": worst_pass,
    }


def compute_mipmap_usage(binding_views: dict, resource_details: dict) -> dict:
    """Detect textures with mip levels outside any view range (view-level waste).

    binding_views: {str(eid): [{resource_id, first_mip, num_mips, ...}]}
    Samples one EID per pass, so view ranges represent pass-level bindings.
    """
    viewed_ranges: dict[int, list[tuple[int, int]]] = defaultdict(list)
    for entries in binding_views.values():
        for e in entries:
            rid = e.get("resource_id")
            first = e.get("first_mip", 0)
            num = e.get("num_mips", 0)
            if rid and num > 0:
                viewed_ranges[rid].append((first, first + num))

    per_texture: list[dict] = []
    total_wasted_mb = 0.0

    for rid_str, rdata in resource_details.items():
        if not isinstance(rdata, dict) or "_error" in rdata:
            continue
        rtype = (rdata.get("type") or "").lower()
        if "texture" not in rtype and "image" not in rtype:
            continue
        total_mips = rdata.get("mips", 1) or 1
        if total_mips <= 1:
            continue

        rid = rdata.get("id")
        if rid is None:
            try:
                rid = int(rid_str)
            except (ValueError, TypeError):
                continue

        ranges = viewed_ranges.get(rid)
        if not ranges:
            continue

        viewed: set[int] = set()
        for (first, end) in ranges:
            viewed.update(range(first, min(end, total_mips)))

        unviewed = [k for k in range(total_mips) if k not in viewed]
        if not unviewed:
            continue

        # Mip k occupies 0.25^k relative to mip 0 (area quarters each level)
        total_weight = sum(0.25 ** k for k in range(total_mips))
        wasted_weight = sum(0.25 ** k for k in unviewed)
        tex_mb = estimate_texture_mb(rdata)
        wasted_mb = tex_mb * (wasted_weight / total_weight) if total_weight > 0 else 0.0
        if wasted_mb <= 0:
            continue

        first_viewed = min(viewed)
        last_viewed = max(viewed)
        per_texture.append({
            "resource_id": rid,
            "name": rdata.get("name", ""),
            "total_mips": total_mips,
            "viewed_mip_range": [first_viewed, last_viewed],
            "unviewed_mips": unviewed,
            "wasted_mb": round(wasted_mb, 3),
            "recommendation": f"Reduce mips from {total_mips} to {last_viewed + 1}",
        })
        total_wasted_mb += wasted_mb

    per_texture.sort(key=lambda x: -x["wasted_mb"])
    return {
        "per_texture": per_texture,
        "total_wasted_mb": round(total_wasted_mb, 3),
    }


def compute_analysis(
    summary: dict,
    pass_details: list,
    pipelines: dict,
    resource_details: dict,
    binding_views: dict | None = None,
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

    # ── Overdraw estimation ──
    result["overdraw"] = compute_overdraw(summary, pass_details)

    # ── Mipmap usage (view-level waste) ──
    result["mipmap_usage"] = compute_mipmap_usage(binding_views or {}, resource_details)

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
