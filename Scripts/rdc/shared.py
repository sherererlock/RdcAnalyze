# -*- coding: utf-8 -*-
"""Shared helpers for rdc collect/analyze scripts.

Provides format tables, memory estimation, JSON utilities,
and other functions used by both rdc_collect.py and rdc_analyze.py.
"""

from __future__ import annotations

import json
import re
from collections import defaultdict
from dataclasses import dataclass
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


# ─────────────────────────────────────────────────────────────────────
# Pipeline stage classification (metadata-driven heuristics)
# ─────────────────────────────────────────────────────────────────────

STAGE_COLORS: dict[str, str] = {
    "Compute": "#6366f1",
    "ShadowMap": "#8b5cf6",
    "DepthPrepass": "#a78bfa",
    "GBuffer": "#3b82f6",
    "MainColor": "#22b07a",
    "Geometry": "#22b07a",
    "Transparent": "#06b6d4",
    "PostProcess": "#d4a017",
    "Bloom": "#f59e0b",
    "UI": "#ec4899",
    "Compositing": "#64748b",
    "Present": "#475569",
    "Hair": "#f59e0b",
    "Other": "#555f78",
}


def _is_power_of_two(n: int) -> bool:
    return n > 0 and (n & (n - 1)) == 0


def _has_depth_format(fmt: str) -> bool:
    return any(k in fmt.upper() for k in ("D16", "D24", "D32"))


def _has_srgb_format(fmt: str) -> bool:
    return "SRGB" in fmt.upper()


def _get_pass_rt_info(p: dict) -> dict:
    cts = p.get("color_targets") or []
    dt = p.get("depth_target")
    color_fmt = cts[0].get("format", "") if cts else ""
    color_w = cts[0].get("width", 0) if cts else 0
    color_h = cts[0].get("height", 0) if cts else 0
    depth_fmt = dt.get("format", "") if isinstance(dt, dict) else ""
    depth_w = dt.get("width", 0) if isinstance(dt, dict) else 0
    depth_h = dt.get("height", 0) if isinstance(dt, dict) else 0
    return {
        "n_color": len(cts), "color_fmt": color_fmt,
        "color_w": color_w, "color_h": color_h,
        "has_depth": bool(dt), "depth_fmt": depth_fmt,
        "depth_w": depth_w, "depth_h": depth_h,
    }


def _load_ops_set(p: dict) -> set[str]:
    ops = p.get("load_ops") or []
    return {op[1] for op in ops if len(op) >= 2}


def classify_pass_stage(
    p: dict,
    *,
    all_passes: list[dict] | None = None,
    bloom_pass_names: set[str] | None = None,
    max_rt_area: int = 0,
    max_rt_resource_id: int | None = None,
) -> tuple[str, str]:
    """Classify a render pass into a pipeline stage using metadata heuristics.

    Returns (stage_name, reason).
    """
    name = p.get("name", "")
    draws = p.get("draws", 0)
    dispatches = p.get("dispatches", 0)
    triangles = p.get("triangles", 0)
    rt = _get_pass_rt_info(p)
    load_ops = _load_ops_set(p)

    if bloom_pass_names and name in bloom_pass_names:
        return "Bloom", "bloom chain member"

    if dispatches > 0 and draws == 0:
        return "Compute", "dispatches only"

    if rt["n_color"] == 0 and rt["has_depth"]:
        dw, dh = rt["depth_w"], rt["depth_h"]
        if _has_depth_format(rt["depth_fmt"]) and _is_power_of_two(dw) and _is_power_of_two(dh):
            return "ShadowMap", f"depth-only {rt['depth_fmt']} {dw}x{dh} (power-of-2)"
        return "DepthPrepass", f"depth-only {rt['depth_fmt']} {dw}x{dh}"

    if draws > 0 and rt["n_color"] > 0:
        avg_tri = triangles / max(draws, 1)
        if _has_srgb_format(rt["color_fmt"]) and "Clear" in load_ops and draws >= 3 and avg_tri < 500:
            return "UI", f"SRGB+Clear, {draws} draws, avg {avg_tri:.0f} tri"

        cw, ch = rt["color_w"], rt["color_h"]
        rt_area = cw * ch
        if max_rt_area > 0 and rt_area >= max_rt_area * 0.9:
            if draws <= 2 and triangles <= 4 and not rt["has_depth"]:
                if not _has_srgb_format(rt["color_fmt"]) and "UNORM" in rt["color_fmt"].upper():
                    return "Compositing", f"blit to large UNORM RT {cw}x{ch}"

        if rt["has_depth"] and "Clear" in load_ops and draws >= 5:
            return "MainColor", f"color+depth, Clear, {draws} draws"

        if draws == 1 and triangles <= 2:
            return "PostProcess", f"fullscreen quad to {rt['color_fmt']}"

    return "Other", "no heuristic matched"


def detect_bloom_chain(pass_details: list[dict]) -> dict | None:
    """Detect a bloom downsample-upsample pyramid in a sequence of passes.

    Returns bloom info dict or None.
    """
    if len(pass_details) < 3:
        return None

    for start in range(len(pass_details)):
        p0 = pass_details[start]
        cts0 = p0.get("color_targets") or []
        if not cts0 or p0.get("draws", 0) != 1:
            continue
        base_fmt = cts0[0].get("format", "")
        if not base_fmt:
            continue

        chain = [start]
        base_w = cts0[0].get("width", 0)
        base_h = cts0[0].get("height", 0)
        prev_w, prev_h = base_w, base_h
        directions = ["threshold"]
        downs = 0
        ups = 0

        for j in range(start + 1, len(pass_details)):
            pj = pass_details[j]
            ctsj = pj.get("color_targets") or []
            if not ctsj or pj.get("draws", 0) != 1:
                break
            fmt_j = ctsj[0].get("format", "")
            if fmt_j != base_fmt:
                break
            wj = ctsj[0].get("width", 0)
            hj = ctsj[0].get("height", 0)

            ratio_w = prev_w / max(wj, 1)
            ratio_h = prev_h / max(hj, 1)

            if 1.8 <= ratio_w <= 2.2 and 1.8 <= ratio_h <= 2.2:
                directions.append("down")
                downs += 1
            elif 0.45 <= ratio_w <= 0.55 and 0.45 <= ratio_h <= 0.55:
                directions.append("up")
                ups += 1
            elif 0.9 <= ratio_w <= 1.1 and 0.9 <= ratio_h <= 1.1:
                directions.append("same")
            elif (ups > 0 and
                  0.9 <= wj / max(base_w, 1) <= 1.1 and
                  0.9 <= hj / max(base_h, 1) <= 1.1):
                directions.append("composite")
                ups += 1
            else:
                break

            chain.append(j)
            prev_w, prev_h = wj, hj

        if downs >= 2 and ups >= 1 and len(chain) >= 4:
            resolutions = []
            pass_names = []
            for idx in chain:
                pc = pass_details[idx]
                ct = (pc.get("color_targets") or [{}])[0]
                resolutions.append(f"{ct.get('width', 0)}x{ct.get('height', 0)}")
                pass_names.append(pc.get("name", ""))
            return {
                "detected": True,
                "levels": downs,
                "passes": pass_names,
                "pass_indices": chain,
                "resolutions": resolutions,
                "directions": directions,
            }

    return None


def detect_fullscreen_quad(
    draws_in_pass: list[dict],
    rt_width: int,
    rt_height: int,
    counters_by_eid: dict[int, dict] | None = None,
) -> bool:
    """Check if all draws in a pass are fullscreen quads."""
    if not draws_in_pass:
        return False
    if any(d.get("triangles", 0) > 2 for d in draws_in_pass):
        return False
    if counters_by_eid:
        total_ps = sum(
            counters_by_eid.get(d.get("eid", 0), {}).get("PS Invocations", 0)
            for d in draws_in_pass
        )
        rt_pixels = rt_width * rt_height
        if rt_pixels > 0 and total_ps > 0:
            return total_ps >= rt_pixels * 0.8
    return len(draws_in_pass) <= 2


# ─────────────────────────────────────────────────────────────────────
# Shader pattern recognition (SPIR-V disassembly heuristics)
# ─────────────────────────────────────────────────────────────────────


@dataclass
class ShaderContext:
    """Pre-parsed shader signals, computed once per shader."""
    ps_text: str
    is_compute: bool
    spirv_bound: int
    sample_count: int
    dref_count: int
    has_cube_sampler: bool
    has_log2_exp2: bool
    dot_count: int
    has_inversesqrt: bool
    has_fclamp_01: bool


def _build_shader_context(shader_content: str, is_compute: bool = False) -> ShaderContext | None:
    if is_compute:
        text = shader_content
    else:
        parts = shader_content.split("Pixel Shader")
        if len(parts) < 2:
            return None
        text = parts[1]

    bound_match = re.search(r"<id> bound of (\d+)", text)
    spirv_bound = int(bound_match.group(1)) if bound_match else 0

    sample_count = (
        text.count("ImageSampleImplicitLod")
        + text.count("ImageSampleExplicitLod")
        + text.count("ImageSampleDrefImplicitLod")
        + text.count("ImageSampleDrefExplicitLod")
    )
    dref_count = (
        text.count("ImageSampleDrefImplicitLod")
        + text.count("ImageSampleDrefExplicitLod")
    )

    return ShaderContext(
        ps_text=text,
        is_compute=is_compute,
        spirv_bound=spirv_bound,
        sample_count=sample_count,
        dref_count=dref_count,
        has_cube_sampler="SampledImage<float, Cube>" in text,
        has_log2_exp2="Log2" in text and "Exp2" in text,
        dot_count=text.count("Dot("),
        has_inversesqrt="InverseSqrt" in text,
        has_fclamp_01=bool(re.search(r"FClamp\([^,]+,\s*0\.0+,\s*1\.0+\)", text)),
    )


_PATTERN_REGISTRY: list[tuple[str, bool, object]] = []


def _register(name: str, *, exclusive: bool = False):
    def wrapper(fn):
        _PATTERN_REGISTRY.append((name, exclusive, fn))
        return fn
    return wrapper


@_register("Fullscreen Blit", exclusive=True)
def _detect_fullscreen_blit(ctx: ShaderContext) -> str | None:
    if ctx.sample_count == 1 and ctx.spirv_bound < 50:
        return "Fullscreen Blit"
    return None


@_register("Dithering")
def _detect_dithering(ctx: ShaderContext) -> str | None:
    t = ctx.ps_text
    if ("SpecId" in t and "FragCoord" in t
            and re.search(r"& 1\b", t) and re.search(r"<< 1\b", t)
            and "Round" in t):
        return "Dithering"
    return None


@_register("FXAA")
def _detect_fxaa(ctx: ShaderContext) -> str | None:
    t = ctx.ps_text
    has_fmax = "FMax" in t
    has_fmin = "FMin" in t
    if ctx.sample_count >= 5 and has_fmax and has_fmin and ctx.spirv_bound > 400:
        return "FXAA"
    return None


@_register("Bloom Threshold")
def _detect_bloom_threshold(ctx: ShaderContext) -> str | None:
    t = ctx.ps_text
    if (ctx.sample_count >= 1
            and re.search(r"FMax.*\.(xy|yz|xz|xyz)", t)
            and (re.search(r"\bFSub\b", t) or " - " in t)
            and (re.search(r"FMax\(.*0\.0", t) or "FClamp" in t)
            and ("FMul" in t or " * " in t)):
        return "Bloom Threshold"
    return None


@_register("Gaussian Blur")
def _detect_gaussian_blur(ctx: ShaderContext) -> str | None:
    t = ctx.ps_text
    if ctx.sample_count < 3:
        return None
    offset_count = len(re.findall(r"(?:FAdd|FSub).*(?:float2|float)", t[:4000]))
    mul_count = t.count("FMul") + t.count(" * ")
    if offset_count >= 2 and mul_count >= 3:
        explicit = t.count("ImageSampleExplicitLod")
        implicit = t.count("ImageSampleImplicitLod")
        if explicit >= 3 or implicit >= 3:
            return "Gaussian Blur"
    return None


@_register("Tonemapping")
def _detect_tonemapping(ctx: ShaderContext) -> str | None:
    t = ctx.ps_text
    has_pow = "Pow" in t
    has_matrix = bool(re.search(r"float[34]x[34]|MatrixTimesVector|VectorTimesMatrix", t))
    if has_pow and (ctx.sample_count >= 2 or has_matrix):
        return "Tonemapping"
    return None


@_register("Shadow Map")
def _detect_shadow_map(ctx: ShaderContext) -> str | None:
    if ctx.dref_count < 1:
        return None
    t = ctx.ps_text
    if "SampledImage<float, 2D>" not in t:
        return None
    if ctx.has_fclamp_01 or "FMax" in t or " * " in t:
        return "Shadow Map"
    return None


@_register("PBR IBL")
def _detect_pbr_ibl(ctx: ShaderContext) -> str | None:
    if not ctx.has_cube_sampler:
        return None
    t = ctx.ps_text
    signals = sum([
        ctx.has_log2_exp2,
        ctx.has_inversesqrt and ctx.dot_count >= 3,
        ctx.has_fclamp_01,
        "ImageSampleExplicitLod" in t,
    ])
    if signals >= 2:
        return "PBR IBL"
    return None


# TODO stubs — no ground truth captures available yet

@_register("SSAO")
def _detect_ssao(ctx: ShaderContext) -> str | None:
    # TODO: ≥8 depth samples + noise texture + hemisphere sampling
    return None


@_register("SSR")
def _detect_ssr(ctx: ShaderContext) -> str | None:
    # TODO: ray march loop + depth compare + screen-space coords
    return None


@_register("Bilateral Filter")
def _detect_bilateral(ctx: ShaderContext) -> str | None:
    # TODO: weighted sampling + depth-based weight falloff
    return None


def detect_shader_patterns(shader_content: str, is_compute: bool = False) -> list[str]:
    """Detect shader algorithm patterns from RenderDoc SPIR-V disassembly."""
    ctx = _build_shader_context(shader_content, is_compute)
    if ctx is None:
        return []
    detected: list[str] = []
    for _name, exclusive, fn in _PATTERN_REGISTRY:
        result = fn(ctx)
        if result:
            detected.append(result)
            if exclusive:
                return detected
    return detected


def analyze_spirv_instructions(shader_content: str, is_compute: bool = False) -> dict:
    """Count instruction categories from RenderDoc SPIR-V disassembly."""
    if is_compute:
        text = shader_content
    else:
        parts = shader_content.split("Pixel Shader")
        text = parts[1] if len(parts) >= 2 else ""

    if not text:
        return {"arithmetic": 0, "sample": 0, "logic": 0, "load_store": 0,
                "dot_matrix": 0, "intrinsic": 0, "barrier": 0, "total": 0}

    lines = text.split("\n")
    arith = 0
    sample = 0
    logic = 0
    load_store = 0
    dot_matrix = 0
    intrinsic = 0
    barrier = 0

    for line in lines:
        stripped = line.strip()
        if not stripped or stripped.startswith("//") or stripped.startswith("struct "):
            continue

        if "ImageSampleImplicitLod" in stripped or "ImageSampleExplicitLod" in stripped:
            sample += 1
        elif "ImageSampleDrefImplicitLod" in stripped:
            sample += 1
        elif "ImageFetch" in stripped:
            sample += 1

        if "Dot(" in stripped:
            dot_matrix += 1
        elif "MatrixTimesVector" in stripped or "MatrixTimesMatrix" in stripped:
            dot_matrix += 1
        elif "VectorTimesMatrix" in stripped:
            dot_matrix += 1

        if "GLSL.std.450::" in stripped:
            intrinsic += stripped.count("GLSL.std.450::")

        if "ControlBarrier" in stripped or "MemoryBarrier" in stripped:
            barrier += 1

        if stripped.startswith("if (") or stripped.startswith("} else"):
            logic += 1
        elif stripped.startswith("switch (") or stripped.startswith("for (") or stripped.startswith("while ("):
            logic += 1

        if "= *" in stripped:
            load_store += 1
        if re.match(r"^\*_\d+", stripped):
            load_store += 1

        for op in (" + ", " - ", " * ", " / "):
            if op in stripped:
                arith += stripped.count(op)
                break

    total = arith + sample + logic + load_store + dot_matrix + intrinsic + barrier
    return {
        "arithmetic": arith, "sample": sample, "logic": logic,
        "load_store": load_store, "dot_matrix": dot_matrix,
        "intrinsic": intrinsic, "barrier": barrier, "total": total,
    }


def estimate_register_pressure(shader_content: str, is_compute: bool = False) -> dict:
    """Estimate register pressure from RenderDoc SPIR-V disassembly."""
    if is_compute:
        text = shader_content
    else:
        parts = shader_content.split("Pixel Shader")
        text = parts[1] if len(parts) >= 2 else ""

    if not text:
        return {"temp_vars": 0, "input_vars": 0, "output_vars": 0,
                "uniform_vars": 0, "spirv_bound": 0,
                "estimated_vgprs": 0, "pressure_level": "low"}

    temp_vars = len(re.findall(r"^Private\s+\S+\*", text, re.MULTILINE))
    input_vars = len(re.findall(r"^Input\s+\S+\*", text, re.MULTILINE))
    output_vars = len(re.findall(r"^Output\s+\S+\*", text, re.MULTILINE))
    uniform_vars = len(re.findall(r"^(?:Uniform|UniformConstant)\s+\S+\*", text, re.MULTILINE))

    bound_match = re.search(r"<id> bound of (\d+)", text)
    spirv_bound = int(bound_match.group(1)) if bound_match else 0

    estimated_vgprs = temp_vars * 4
    if estimated_vgprs > 64:
        pressure_level = "high"
    elif estimated_vgprs > 32:
        pressure_level = "medium"
    else:
        pressure_level = "low"

    return {
        "temp_vars": temp_vars, "input_vars": input_vars,
        "output_vars": output_vars, "uniform_vars": uniform_vars,
        "spirv_bound": spirv_bound, "estimated_vgprs": estimated_vgprs,
        "pressure_level": pressure_level,
    }


def deduplicate_shaders(shader_disasm: dict, shaders_dir: Path) -> dict:
    """Group shader variants that differ only by specialization constants."""
    import hashlib

    _SPEC_VALUE_RE = re.compile(
        r"(const\s+\w+\s+_\d+\s*=\s*)([^:]+?)(\s*:\s*\[\[SpecId\(\d+\)\]\])"
    )
    _HEADER_RE = re.compile(r"^//.*$", re.MULTILINE)

    shader_hashes: dict[str, list[str]] = {}
    shader_specs: dict[str, dict[int, str]] = {}

    for key, info in shader_disasm.items():
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

        stripped = _HEADER_RE.sub("", content).strip()
        normalized = _SPEC_VALUE_RE.sub(r"\1<SPEC>\3", stripped)
        h = hashlib.md5(normalized.encode("utf-8")).hexdigest()

        shader_hashes.setdefault(h, []).append(key)

        specs: dict[int, str] = {}
        for m in _SPEC_VALUE_RE.finditer(content):
            sid_match = re.search(r"SpecId\((\d+)\)", m.group(3))
            if sid_match:
                specs[int(sid_match.group(1))] = m.group(2).strip()
        shader_specs[key] = specs

    groups = []
    variant_group_count = 0
    for h, keys in sorted(shader_hashes.items(), key=lambda kv: -len(kv[1])):
        canonical = keys[0]
        total_uses = sum(
            (shader_disasm.get(k) or {}).get("uses", 0) for k in keys
        )
        spec_diffs: dict[int, list[str]] = {}
        all_spec_ids: set[int] = set()
        for k in keys:
            all_spec_ids.update(shader_specs.get(k, {}).keys())
        for sid in sorted(all_spec_ids):
            vals = [shader_specs.get(k, {}).get(sid, "") for k in keys]
            if len(set(vals)) > 1:
                spec_diffs[sid] = vals

        if len(keys) > 1:
            variant_group_count += 1

        groups.append({
            "canonical_key": canonical,
            "variant_keys": keys,
            "variant_count": len(keys),
            "spec_diffs": spec_diffs,
            "total_uses": total_uses,
        })

    return {
        "groups": groups,
        "total_shaders": len(shader_disasm),
        "unique_shaders": len(groups),
        "variant_groups": variant_group_count,
    }


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
