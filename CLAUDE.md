# CLAUDE.md

Work style: telegraph; noun-phrases ok; drop grammar; min tokens.

This file provides guidance to Claude Code (claude.ai/code) when working with code in this repository.

## Project Overview

RdcAnalyze is a GPU frame analysis toolkit that wraps **rdc-cli** (a CLI for RenderDoc captures) with automated data collection and HTML report generation. It ships as a self-contained portable package on Windows: embedded Python 3.13, RenderDoc binaries, and all pip dependencies are checked in.

## Repository Layout

```
Scripts/rdc/          # Main analysis scripts (the code you'll edit most)
  collect.py          # Phase 1: automated data collection from .rdc captures via rdc-cli
  analyze.py          # Phase 2: generates interactive HTML performance report from collected JSON
  tsv_export.py       # TSV table generation (called by collect.py)
  shared.py           # Shared utilities (BPP tables, format helpers, stage classification, shader pattern detection)
Scripts/rdc-report.bat  # One-command pipeline: collect → analyze

rdc-portable/         # Portable RenderDoc (binaries + Python bindings, checked in)
  rdc.bat             # Entry point: invokes `rdc.cli.entry()` via embedded Python
  rdc-shell.bat       # Interactive shell with `rdc` aliased
  renderdoc/          # renderdoc.dll, renderdoc.pyd, renderdoccmd.exe

python/               # Embedded Python 3.13 + site-packages (checked in, not editable)
  Lib/site-packages/rdc/  # rdc-cli package (installed via pip, treat as read-only)

rdc_captures/         # Working directory for .rdc files (gitignored via *.rdc)
```

## Common Commands

### Full pipeline (collect + report)
```bat
Scripts\rdc-report.bat <capture.rdc> [-j 8]
```

### Run phases individually
```bat
python\python.exe Scripts\rdc\collect.py <capture.rdc> [-j 8]
python\python.exe Scripts\rdc\analyze.py <capture-stem>-analysis/
```

### Use rdc-cli directly
```bat
rdc-portable\rdc.bat open <capture.rdc>
rdc-portable\rdc.bat info --json
rdc-portable\rdc.bat draws --json
rdc-portable\rdc.bat close
```

## Architecture

### Two-Phase Pipeline

**Phase 1 — `collect.py`**: Opens an .rdc capture via rdc-cli's daemon (JSON-RPC over TCP), collects base data (info, stats, passes, draws, events, resources, counters), then per-draw pipeline/bindings, shader disassembly, and resource details. Supports parallel collection with `-j N` workers (each a separate rdc daemon session). Outputs into a `*-analysis/` directory:

```
{capture}-analysis/
  json/             # Full JSON data (for scripts)
  tsv/              # TSV tables (for AI/LLM analysis — token-efficient)
    passes.tsv      # Pass overview: name, eid range, draw/dispatch counts, RT formats
    draws.tsv       # Per-draw: eid, type, triangles, pass, topology, pipeline IDs
    bindings.tsv    # Per-binding: eid, stage, kind, set, slot, resource name
    resources.tsv   # All resources: textures + buffers unified
    shaders.tsv          # Shader pairs: vs/ps/cs IDs, usage count, eid list
    shader_instructions.tsv  # Per-shader instruction mix (arithmetic/sample/logic/...) + register pressure
    shader_variants.tsv  # Shader variant deduplication groups (SpecId diff)
    shader_pass_matrix.tsv   # Shader × Pass usage matrix (draw counts)
    pipeline_stages.tsv  # Auto-classified stage per pass (Compute/ShadowMap/MainColor/Bloom/UI/...)
    stage_summary.tsv    # GPU time distribution by stage type
  shaders/          # .shader disassembly files
  render_graph.html
```

**Phase 2 — `analyze.py`**: Reads the `*-analysis/json/` files and generates `performance_report.html` with sections: Frame Overview, Rendering Pipeline (Gantt + table), Pipeline Stage Analysis (auto-classification + GPU time breakdown + bloom chain detection), Triangle Hotspots, Bandwidth Estimation, Shader Complexity, Memory, and Optimization Suggestions.

### Key Patterns

- **rdc-cli daemon**: `collect.py` communicates with rdc-cli via subprocess calls (`run_rdc()` / `run_rdc_json()`) and direct JSON-RPC socket calls (`_rpc_call()`) for long-running operations like shader cache builds.
- **WorkerPool**: Manages parallel daemon sessions (`rdc-collect-w0..wN`) for concurrent per-draw and resource data collection.
- **Render Graph**: `_extract_subpasses()` builds fine-grained sub-pass nodes from event marker hierarchy, then `_build_dependency_edges()` infers RT data flow using multiple strategies (explicit deps → per-pass reads/writes → RT usage events → descriptors → name similarity → unconsumed RT propagation).
- **Pipeline Stage Classification** (`shared.py`): Two-tier heuristic — (1) engine-provided pass name keywords (shadow, gbuffer, bloom, fxaa…), (2) metadata fallback for RenderDoc auto-generated names (RT format/size, load ops, draw characteristics). Includes `detect_bloom_chain()` (progressive ½ resolution downsample-upsample detection) and `detect_fullscreen_quad()` (tri≤2 + PS invocations ≈ RT pixels).
- **Shader Pattern Detection** (`shared.py`): Registry-based pattern recognition on RenderDoc SPIR-V disassembly. `ShaderContext` dataclass pre-parses shared signals (sample counts, Dref, Cube sampler, Log2/Exp2, Dot count, etc.); individual `@_register`-decorated detectors match against context. Supports both PS and CS. Current patterns: Fullscreen Blit, Dithering, FXAA, Bloom Threshold, Gaussian Blur, Tonemapping, Shadow Map, PBR IBL. TODO stubs: SSAO, SSR, Bilateral Filter.
- **Shader Analysis** (`shared.py` + `analyze.py`): Per-shader instruction distribution (`analyze_spirv_instructions`), register pressure estimation (`estimate_register_pressure`), variant deduplication via normalized content hash (`deduplicate_shaders`), shader→pass usage heatmap matrix.
- **HTML reports** reference shared CSS from an `assets/` directory via relative path (`__ASSETS__` placeholder replaced at generation time).

## Important Constraints

- **Single RenderDoc call at a time**: Never invoke multiple rdc commands concurrently against the same session — RenderDoc will deadlock. Parallel collection uses separate named sessions.
- **Embedded Python is the runtime**: Always use `python\python.exe`, not system Python. The embedded interpreter has all dependencies (click, numpy, protobuf, etc.) pre-installed in `python\Lib\site-packages\`.
- **.rdc files are gitignored**: Capture files are large binaries excluded via `.gitignore`.
- **AI 分析帧数据时必须从 `tsv/` 目录读取**：TSV 格式专为 LLM 设计，省 token、易解析。禁止直接读 `json/` 目录的 JSON 文件做分析——JSON 仅供脚本内部使用。
