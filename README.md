# RdcAnalyze

GPU frame analysis toolkit for RenderDoc captures — automated collection, HTML performance reports, and TSV data export for LLM-assisted analysis.

## What it does

1. **Collect** — opens an `.rdc` capture via rdc-cli, extracts per-draw pipeline data, shader disassembly, resource details, and GPU counters
2. **Analyze** — generates an interactive HTML performance report with pass timeline, bandwidth estimation, shader complexity, overdraw, mipmap usage, and optimization suggestions
3. **Render graph** — builds an interactive dependency graph showing RT data flow between passes

## Quickstart

```bat
Scripts\rdc-report.bat path\to\capture.rdc
```

Output: `path\to\capture-analysis\performance_report.html`

Parallel collection (faster for large captures):

```bat
Scripts\rdc-report.bat path\to\capture.rdc -j 8
```

## System Requirements

- Windows 10/11 (x64)
- No installation required — Python 3.13, RenderDoc binaries, and all dependencies are bundled

## Report Sections

| Section | Description |
|---------|-------------|
| Frame Overview | GPU time, draw/dispatch counts, resource summary |
| Rendering Pipeline | Per-pass Gantt timeline with RT formats and GPU time |
| Pipeline Stage Analysis | Auto-classified stages (Shadow / GBuffer / Bloom / UI …) with time breakdown |
| Triangle Hotspots | Top draws by triangle count and GPU time |
| Bandwidth Estimation | RT load/store MB per pass, bloom chain detection |
| Overdraw Estimation | PS invocations / RT pixel ratio per pass |
| TBDR Tile Efficiency | Unnecessary load/store ops (mobile GLES/Vulkan) |
| Shader Complexity | Instruction mix, register pressure, variant deduplication, pass matrix |
| Memory | Texture and buffer footprint breakdown |
| Mipmap Usage | View-level mip waste per texture |
| Vertex Buffer Efficiency | Index reuse ratio, oversized attributes, stride padding |
| Optimization Suggestions | Actionable items derived from all sections |

## Running phases individually

```bat
python\python.exe Scripts\rdc\collect.py capture.rdc [-j 8]
python\python.exe Scripts\rdc\analyze.py capture-analysis/
```

## Output layout

```
capture-analysis/
  performance_report.html   # Main interactive report
  render_graph.html          # RT dependency graph
  json/                      # Raw JSON (script input)
  tsv/                       # TSV tables (LLM / spreadsheet friendly)
  shaders/                   # Shader disassembly (.shader)
```

## TSV tables (for AI analysis)

The `tsv/` directory contains token-efficient tables designed for LLM consumption:

| File | Contents |
|------|----------|
| `passes.tsv` | Pass name, EID range, draw/dispatch counts, RT formats |
| `draws.tsv` | Per-draw EID, type, triangles, topology, pipeline IDs |
| `shaders.tsv` | Shader pairs, usage counts, detected patterns |
| `shader_instructions.tsv` | Per-shader instruction mix + register pressure |
| `pipeline_stages.tsv` | Auto-classified stage per pass |
| `stage_summary.tsv` | GPU time distribution by stage type |
| `overdraw.tsv` | Per-pass overdraw ratio and severity |
| `mipmap_usage.tsv` | Texture mip waste |
| `vertex_efficiency.tsv` | Mesh index reuse and attribute format issues |
| … | 20 tables total |

## Using rdc-cli directly

```bat
rdc-portable\rdc.bat open capture.rdc
rdc-portable\rdc.bat info --json
rdc-portable\rdc.bat draws --json
rdc-portable\rdc.bat close
```

## Documentation

| Document | Contents |
|----------|----------|
| [docs/scripts-api.md](docs/scripts-api.md) | Module overview and architecture |
| [docs/flowchart.md](docs/flowchart.md) | Pipeline flow diagrams (Mermaid) |
| [docs/infra-api.md](docs/infra-api.md) | RPC and shared utilities API |
| [docs/collect-api.md](docs/collect-api.md) | Collect / workers / computed API |
| [docs/visual-api.md](docs/visual-api.md) | Analyze / render_graph / export_assets API |
| [docs/json-schema.md](docs/json-schema.md) | JSON file schemas |
| [docs/troubleshooting.md](docs/troubleshooting.md) | Common errors and fixes |

## Version

v1.2.0
