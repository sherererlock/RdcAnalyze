# CLAUDE.md

This file provides guidance to Claude Code (claude.ai/code) when working with code in this repository.

## Project Overview

RdcAnalyze is a GPU frame analysis toolkit that wraps **rdc-cli** (a CLI for RenderDoc captures) with automated data collection and HTML report generation. It ships as a self-contained portable package on Windows: embedded Python 3.13, RenderDoc binaries, and all pip dependencies are checked in.

## Repository Layout

```
Scripts/rdc/          # Main analysis scripts (the code you'll edit most)
  collect.py          # Phase 1: automated data collection from .rdc captures via rdc-cli
  analyze.py          # Phase 2: generates interactive HTML performance report from collected JSON
  shared.py           # Shared utilities (BPP tables, format helpers, JSON I/O)
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

**Phase 1 — `collect.py`**: Opens an .rdc capture via rdc-cli's daemon (JSON-RPC over TCP), collects base data (info, stats, passes, draws, events, resources, counters), then per-draw pipeline/bindings, shader disassembly, and resource details. Supports parallel collection with `-j N` workers (each a separate rdc daemon session). Outputs JSON files + `render_graph.html` into a `*-analysis/` directory.

**Phase 2 — `analyze.py`**: Reads the `*-analysis/` JSON files and generates `performance_report.html` with sections: Frame Overview, Rendering Pipeline (Gantt + table), Triangle Hotspots, Bandwidth Estimation, Shader Complexity, Memory, and Optimization Suggestions.

### Key Patterns

- **rdc-cli daemon**: `collect.py` communicates with rdc-cli via subprocess calls (`run_rdc()` / `run_rdc_json()`) and direct JSON-RPC socket calls (`_rpc_call()`) for long-running operations like shader cache builds.
- **WorkerPool**: Manages parallel daemon sessions (`rdc-collect-w0..wN`) for concurrent per-draw and resource data collection.
- **Render Graph**: `_extract_subpasses()` builds fine-grained sub-pass nodes from event marker hierarchy, then `_build_dependency_edges()` infers RT data flow using multiple strategies (explicit deps → per-pass reads/writes → RT usage events → descriptors → name similarity → unconsumed RT propagation).
- **HTML reports** reference shared CSS from an `assets/` directory via relative path (`__ASSETS__` placeholder replaced at generation time).

## Important Constraints

- **Single RenderDoc call at a time**: Never invoke multiple rdc commands concurrently against the same session — RenderDoc will deadlock. Parallel collection uses separate named sessions.
- **Embedded Python is the runtime**: Always use `python\python.exe`, not system Python. The embedded interpreter has all dependencies (click, numpy, protobuf, etc.) pre-installed in `python\Lib\site-packages\`.
- **`python/` and `rdc-portable/` are read-only**: These directories contain checked-in binaries and installed packages. Edit only files under `Scripts/rdc/`.
- **.rdc files are gitignored**: Capture files are large binaries excluded via `.gitignore`.
