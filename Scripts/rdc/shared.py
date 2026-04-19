# -*- coding: utf-8 -*-
"""Shared helpers for rdc collect/analyze scripts.

Provides format tables, memory estimation, JSON utilities,
and other functions used by both rdc_collect.py and rdc_analyze.py.
"""

from __future__ import annotations

import json
import re
from collections import defaultdict
from pathlib import Path

# ─────────────────────────────────────────────────────────────────────
# Bits-per-pixel lookup for common GPU formats
# ─────────────────────────────────────────────────────────────────────

BPP_TABLE: dict[str, float] = {
    # RGBA 8-bit
    "R8G8B8A8": 32, "B8G8R8A8": 32,
    "R8G8B8A8_UNORM": 32, "R8G8B8A8_SRGB": 32,
    "B8G8R8A8_UNORM": 32, "B8G8R8A8_SRGB": 32,
    # RGB 8-bit
    "R8G8B8_UNORM": 24,
    # R8
    "R8_UNORM": 8, "R8_UINT": 8,
    # 10-bit
    "R10G10B10A2_UNORM": 32, "A2B10G10R10_UNORM": 32,
    # R11G11B10
    "R11G11B10_FLOAT": 32,
    # 16-bit float
    "R16_FLOAT": 16, "R16_SFLOAT": 16,
    "R16G16_FLOAT": 32, "R16G16_SFLOAT": 32,
    "R16G16B16A16_FLOAT": 64, "R16G16B16A16_SFLOAT": 64,
    # 32-bit float
    "R32_FLOAT": 32, "R32_SFLOAT": 32,
    "R32G32_FLOAT": 64, "R32G32_SFLOAT": 64,
    "R32G32B32A32_FLOAT": 128, "R32G32B32A32_SFLOAT": 128,
    # Depth/stencil
    "D16_UNORM": 16, "D16": 16,
    "D24_UNORM": 24, "D24_UNORM_S8_UINT": 32, "D24S8": 32,
    "D32_FLOAT": 32, "D32_SFLOAT": 32, "D32S8": 40,
    # BC compressed
    "BC1_UNORM": 4, "BC1_SRGB": 4,
    "BC1_RGB_UNORM": 4, "BC1_RGB_SRGB": 4,
    "BC1_RGBA_UNORM": 4, "BC1_RGBA_SRGB": 4,
    "BC2_UNORM": 8, "BC2_SRGB": 8,
    "BC3_UNORM": 8, "BC3_SRGB": 8,
    "BC4_UNORM": 4, "BC4_SNORM": 4,
    "BC5_UNORM": 8, "BC5_SNORM": 8,
    "BC6H_UFLOAT": 8, "BC6H_SFLOAT": 8,
    "BC7_UNORM": 8, "BC7_SRGB": 8,
    # ASTC compressed
    "ASTC_4x4_UNORM": 8, "ASTC_4x4_SRGB": 8,
    "ASTC_5x5_UNORM": 5.12, "ASTC_5x5_SRGB": 5.12,
    "ASTC_6x6_UNORM": 3.56, "ASTC_6x6_SRGB": 3.56,
    "ASTC_8x8_UNORM": 2, "ASTC_8x8_SRGB": 2,
}


def guess_bpp(fmt_str: str) -> float:
    """Guess bits-per-pixel from a format string. Returns 32 as fallback."""
    if not fmt_str:
        return 32.0
    upper = fmt_str.upper().replace("_BLOCK", "").strip()
    if upper in BPP_TABLE:
        return BPP_TABLE[upper]
    for key, val in BPP_TABLE.items():
        if key in upper or upper in key:
            return val
    return 32.0


# ─────────────────────────────────────────────────────────────────────
# Data helpers
# ─────────────────────────────────────────────────────────────────────

def unwrap(obj, *keys):
    """Unwrap rdc JSON output — e.g., unwrap(data, 'passes') extracts inner list."""
    if obj is None:
        return None
    if isinstance(obj, list):
        return obj
    if isinstance(obj, dict):
        for k in keys:
            if k in obj:
                return obj[k]
    return obj


def write_json(path: Path, data: object) -> None:
    """Write data as indented JSON."""
    with open(path, "w", encoding="utf-8") as f:
        json.dump(data, f, indent=2, ensure_ascii=False)


def fmt_number(n: int | float) -> str:
    """Format number with comma separators."""
    if isinstance(n, float):
        if n >= 100:
            return f"{n:,.0f}"
        return f"{n:,.2f}"
    return f"{n:,}"


def fmt_mb(mb: float) -> str:
    """Format megabytes with appropriate unit."""
    if mb >= 1:
        return f"{mb:.1f} MB"
    return f"{mb * 1024:.0f} KB"


# ─────────────────────────────────────────────────────────────────────
# Memory estimation
# ─────────────────────────────────────────────────────────────────────

def estimate_texture_mb(res: dict) -> float:
    """Estimate texture memory in MB from resource detail dict."""
    w = res.get("width", 0)
    h = res.get("height", 0)
    d = res.get("depth", 1) or 1
    mips = res.get("mips", 1) or 1
    arrays = res.get("array_size", 1) or res.get("arrays", 1) or 1
    fmt = res.get("format", "")
    bpp = guess_bpp(fmt)
    total_pixels = 0
    mw, mh = w, h
    for _ in range(mips):
        total_pixels += max(mw, 1) * max(mh, 1)
        mw //= 2
        mh //= 2
    total_bytes = total_pixels * d * arrays * (bpp / 8)
    return total_bytes / (1024 * 1024)


def rt_bytes(target: dict) -> float:
    """Estimate bytes for a render target (single read or write)."""
    if not target:
        return 0.0
    w = target.get("width", 0)
    h = target.get("height", 0)
    fmt = target.get("format", "")
    bpp = guess_bpp(fmt)
    return w * h * (bpp / 8)


# ─────────────────────────────────────────────────────────────────────
# Frame deduplication
# ─────────────────────────────────────────────────────────────────────

_DEDUP_THRESHOLD = 0.05


def _within_threshold(a: int, b: int) -> bool:
    m = max(a, b, 1)
    return abs(a - b) / m <= _DEDUP_THRESHOLD


def _pass_shape(pd: dict) -> tuple:
    """Structural fingerprint independent of resource IDs."""
    name = pd.get("name", "")
    m = re.search(r"#\d+\s*(.*)", name)
    qualifier = m.group(1).strip() if m else name

    color_fmts = tuple(sorted(
        (t.get("format", ""), t.get("width", 0), t.get("height", 0))
        for t in (pd.get("color_targets") or [])
    ))
    dt = pd.get("depth_target") or {}
    depth_fmt = (dt.get("format", ""), dt.get("width", 0), dt.get("height", 0)) if dt else None

    return (qualifier, pd.get("draws", 0), pd.get("dispatches", 0),
            color_fmts, depth_fmt)


def _find_present_cut(summary: dict) -> int | None:
    """Strategy 1: frame boundary from Present events."""
    events = unwrap(summary.get("events"), "events")
    if not events or not isinstance(events, list):
        return None
    presents = [
        e["eid"] for e in events
        if isinstance(e, dict) and "eid" in e
        and "present" in e.get("name", "").lower()
    ]
    if len(presents) >= 2:
        return presents[-2]
    return None


def _find_swapchain_cut(pass_details: list) -> int | None:
    """Strategy 2: frame boundary from Swapchain/Backbuffer targets."""
    sc_indices: list[int] = []
    for i, pd in enumerate(pass_details):
        for ct in (pd.get("color_targets") or []):
            nm = (ct.get("name") or "").lower()
            if "swapchain" in nm or "backbuffer" in nm:
                sc_indices.append(i)
                break
    if len(sc_indices) >= 2:
        return pass_details[sc_indices[-2]].get("end_eid", 0)
    return None


def _find_rt_reuse_cut(pass_details: list) -> int | None:
    """Strategy 3: frame boundary from render target reuse + EID gap.

    When the same render target ID appears in well-separated passes with
    similar draw counts, the earlier occurrence is from a previous frame.
    The largest EID gap near the duplicate marks the frame boundary.
    """
    if len(pass_details) < 4:
        return None

    rt_uses: dict[int, list[int]] = defaultdict(list)
    for i, pd in enumerate(pass_details):
        for ct in (pd.get("color_targets") or []):
            if ct.get("id"):
                rt_uses[ct["id"]].append(i)
        dt = pd.get("depth_target")
        if isinstance(dt, dict) and dt.get("id"):
            rt_uses[dt["id"]].append(i)

    dup_indices: set[int] = set()
    for indices in rt_uses.values():
        if len(indices) < 2:
            continue
        for j in range(len(indices) - 1):
            ia, ib = indices[j], indices[j + 1]
            if ib - ia < 3:
                continue
            pa, pb = pass_details[ia], pass_details[ib]
            between = sum(
                pass_details[k].get("draws", 0) for k in range(ia + 1, ib)
            )
            if between < 5:
                continue
            if _within_threshold(pa.get("draws", 0), pb.get("draws", 0)):
                dup_indices.add(ia)

    if not dup_indices:
        return None

    last_dup = max(dup_indices)
    search_end = min(last_dup + 4, len(pass_details) - 1)
    best_gap = 0
    best_cut_after = None
    for i in range(search_end):
        end_eid = pass_details[i].get("end_eid", 0)
        next_begin = pass_details[i + 1].get("begin_eid", 0)
        gap = next_begin - end_eid
        if gap > best_gap:
            best_gap = gap
            best_cut_after = i

    if best_cut_after is None:
        best_cut_after = last_dup
    if best_cut_after + 1 >= len(pass_details):
        return None
    return pass_details[best_cut_after + 1].get("begin_eid", 0) - 1


def _find_sequence_cut(pass_details: list) -> int | None:
    """Strategy 4: frame boundary from structural sequence repetition."""
    if len(pass_details) < 6:
        return None

    shapes = [_pass_shape(pd) for pd in pass_details]
    n = len(shapes)

    for length in range(n // 2, 2, -1):
        tail = shapes[n - length:]
        search = shapes[: n - length]
        for start in range(len(search) - length + 1):
            if search[start : start + length] == tail:
                return pass_details[n - length].get("begin_eid", 0) - 1

    def _fuzzy_match(a: tuple, b: tuple) -> bool:
        if a[0] != b[0] or a[3] != b[3] or a[4] != b[4]:
            return False
        if not _within_threshold(a[1], b[1]):
            return False
        return a[2] == b[2]

    for length in range(n // 2, 2, -1):
        tail = shapes[n - length:]
        search = shapes[: n - length]
        for start in range(len(search) - length + 1):
            candidate = search[start : start + length]
            if all(_fuzzy_match(candidate[i], tail[i]) for i in range(length)):
                return pass_details[n - length].get("begin_eid", 0) - 1

    return None


def dedup_frames(summary: dict, pass_details: list) -> tuple[dict, list]:
    """Detect and remove duplicate frames from a multi-frame capture.

    Four-strategy cascade:
      1. Present events (vkQueuePresentKHR / IDXGISwapChain::Present)
      2. Swapchain/Backbuffer render targets
      3. Render target reuse + EID gap
      4. Structural sequence matching (repeated pass shapes)
    """
    if not pass_details or len(pass_details) < 2:
        return summary, pass_details

    strategy = None
    cut_eid = _find_present_cut(summary)
    if cut_eid is not None:
        strategy = "vkQueuePresentKHR"
    else:
        cut_eid = _find_swapchain_cut(pass_details)
        if cut_eid is not None:
            strategy = "Swapchain Image target"
        else:
            cut_eid = _find_rt_reuse_cut(pass_details)
            if cut_eid is not None:
                strategy = "RT reuse + EID gap"
            else:
                cut_eid = _find_sequence_cut(pass_details)
                if cut_eid is not None:
                    strategy = "structural sequence match"

    if cut_eid is None:
        return summary, pass_details

    passes_raw = unwrap(summary.get("passes"), "passes") or []
    kept_pd: list[dict] = []
    kept_passes_raw: list = []
    for i, pd in enumerate(pass_details):
        if pd.get("begin_eid", 0) > cut_eid:
            kept_pd.append(pd)
            if i < len(passes_raw):
                kept_passes_raw.append(passes_raw[i])

    if not kept_pd:
        return summary, pass_details

    removed_passes = len(pass_details) - len(kept_pd)
    if removed_passes == 0:
        return summary, pass_details

    def _filter_eid(data, key: str):
        items = unwrap(data, key)
        if not items or not isinstance(items, list):
            return data
        filtered = [x for x in items if isinstance(x, dict) and x.get("eid", 0) > cut_eid]
        if isinstance(data, dict) and key in data:
            return {**data, key: filtered}
        return filtered

    orig_passes = summary.get("passes")
    if isinstance(orig_passes, dict) and "passes" in orig_passes:
        new_passes = {**orig_passes, "passes": kept_passes_raw}
    else:
        new_passes = kept_passes_raw

    new_summary = {**summary}
    new_summary["passes"] = new_passes
    new_summary["draws"] = _filter_eid(summary.get("draws"), "draws")
    new_summary["events"] = _filter_eid(summary.get("events"), "events")

    new_draws = unwrap(new_summary["draws"], "draws") or []
    new_events = unwrap(new_summary["events"], "events") or []
    print(f"  [Dedup] Strategy: {strategy}")
    print(f"  [Dedup] Removed {removed_passes} old-frame passes "
          f"(cut EID <= {cut_eid}, "
          f"kept {len(kept_pd)} passes, {len(new_draws)} draws, {len(new_events)} events)")

    _renumber_deduped(kept_pd)
    coarse = unwrap(new_summary.get("passes"), "passes") or []
    for sp, pd in zip(coarse, kept_pd):
        if isinstance(sp, dict) and isinstance(pd, dict):
            sp["name"] = pd["name"]

    return new_summary, kept_pd


def _renumber_deduped(passes: list[dict]) -> None:
    """Renumber passes per-type after dedup removed old-frame passes."""
    counters: dict[str, int] = {}
    _PREFIX_RE = re.compile(r"^(Compute Pass|Depth-only Pass|Copy/Clear Pass|Colour Pass) #\d+(.*)")
    for p in passes:
        name = p.get("name", "")
        m = _PREFIX_RE.match(name)
        if not m:
            continue
        prefix, suffix = m.group(1), m.group(2)
        counters[prefix] = counters.get(prefix, 0) + 1
        p["name"] = f"{prefix} #{counters[prefix]}{suffix}"
