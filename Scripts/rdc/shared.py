# -*- coding: utf-8 -*-
"""Shared helpers for rdc collect/analyze scripts.

Provides format tables, memory estimation, JSON utilities,
and other functions used by both rdc_collect.py and rdc_analyze.py.
"""

from __future__ import annotations

import json
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
