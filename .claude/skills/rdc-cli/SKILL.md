---
name: rdc-cli
description: >
  Use this skill when working with RenderDoc capture files (.rdc), analyzing GPU frames,
  tracing shaders, inspecting draw calls, or running CI assertions against GPU captures.
  Trigger phrases: "open capture", "rdc file", ".rdc", "renderdoc", "shader debug",
  "pixel trace", "draw calls", "GPU frame", "assert pixel", "export render target".
---

# rdc-cli Skill

## Overview

rdc-cli is a Unix-friendly command-line interface for RenderDoc GPU captures. It provides a daemon-backed architecture using JSON-RPC over TCP, a virtual filesystem (VFS) path namespace for navigating capture internals, and composable commands designed for shell pipelines, scripting, and CI assertions.

Install: `pip install rdc-cli` (requires a local RenderDoc build with Python bindings).
Check setup: `rdc doctor`.

## Core Workflow

Follow this session lifecycle for any capture analysis task:

1. **Open** a capture: `rdc open path/to/capture.rdc`
2. **Inspect** metadata: `rdc info`, `rdc stats`, `rdc events`
3. **Navigate** the VFS: `rdc ls /`, `rdc ls /textures`, `rdc cat /pipelines/0`
4. **Analyze** specifics: `rdc shaders`, `rdc pipeline`, `rdc resources`, `rdc bindings`
5. **Debug** shaders: `rdc debug pixel X Y`, `rdc debug vertex EID VTXID`, `rdc debug thread EID GX GY GZ`
6. **Export** data: `rdc texture EID -o out.png`, `rdc rt EID`, `rdc buffer EID -o buf.bin`, `rdc log`
7. **Close** the session: `rdc close`

### Session Management

- Default session name: `default` (or value of `$RDC_SESSION`).
- Override per-command: `rdc --session myname open capture.rdc`.
- Check active session: `rdc status`.
- Navigate to a specific event: `rdc goto EID`.

## Output Formats

All list/table commands default to TSV (tab-separated values) with a header row, suitable for `cut`, `awk`, and `sort`.

| Flag | Format | Use Case |
|------|--------|----------|
| *(default)* | TSV with header | Human reading, shell pipelines |
| `--no-header` | TSV without header | Piping to `awk`/`cut` without stripping |
| `--json` | JSON array | Structured processing with `jq` |
| `--jsonl` | Newline-delimited JSON | Streaming processing, large datasets |
| `-q` / `--quiet` | Minimal (single column) | Extracting IDs for loops |

Example -- get all draw call EIDs as a plain list:

```bash
rdc draws -q
```

Example -- JSON pipeline with jq:

```bash
rdc events --json | jq '.[] | select(.type == "DrawIndexed")'
```

## Render Pass Analysis

### List passes (Phase 8 columns)

`rdc passes` outputs 6 columns: NAME, DRAWS, DISPATCHES, TRIANGLES, BEGIN_EID, END_EID.

```bash
rdc passes                          # TSV table
rdc passes --json                   # includes load_ops/store_ops per pass
rdc passes --deps --table           # per-pass READS/WRITES/LOAD/STORE
```

### Inspect a single pass

`rdc pass <name>` shows enriched attachments: resource name, format, dimensions, and load/store ops.

```bash
rdc pass GBuffer
rdc pass GBuffer --json
rdc pass 0                          # by 0-based index
```

### Detect dead render targets

`rdc unused-targets` finds render targets written but never consumed by visible output. Columns: ID, NAME, WRITTEN_BY, WAVE.

```bash
rdc unused-targets                  # TSV
rdc unused-targets --json           # structured
rdc unused-targets -q               # one resource ID per line (for scripting)
```

### Frame statistics

`rdc stats` outputs three sections: Per-Pass Breakdown, Top Draws by Triangle Count, and Largest Resources.

```bash
rdc stats                           # all three sections
rdc stats --json                    # includes largest_resources array
```

GL/GLES/D3D11 captures without native BeginPass/EndPass markers get synthetic pass inference automatically — no extra flags needed.

## Common Tasks

### Find all draw calls

```bash
rdc draws
rdc draws --pass "GBuffer" --json
```

### Trace a pixel

```bash
rdc debug pixel 512 384
rdc debug pixel 512 384 --json    # structured output
rdc debug pixel 512 384 --trace   # full step-by-step trace
```

### Search shaders by name or source

```bash
rdc search "main" --type shader
rdc shaders --name "GBuffer*"
```

### Export render targets

```bash
rdc rt EID -o output.png
rdc texture EID --format png -o tex.png
```

### Browse VFS paths

```bash
rdc ls /
rdc ls /textures -l
rdc tree /pipelines --depth 2
rdc cat /events/42
```

### Inspect pipeline state at a draw call

```bash
rdc goto EID
rdc pipeline --json
rdc bindings --json
```

### Compare state before/after a pass

```bash
rdc goto 100 && rdc pipeline --json > before.json
rdc goto 200 && rdc pipeline --json > after.json
diff before.json after.json
```

## CI Assertions

rdc-cli provides assertion commands that exit non-zero on failure, designed for automated testing pipelines:

| Command | Purpose |
|---------|---------|
| `rdc assert-pixel X Y --expect R,G,B,A` | Assert pixel color at coordinates |
| `rdc assert-clean` | Assert no validation errors in capture |
| `rdc assert-count --type DrawIndexed --min N` | Assert minimum draw call count |
| `rdc assert-state FIELD VALUE` | Assert pipeline state field matches value |
| `rdc assert-image EID --ref reference.png` | Assert render target matches reference image |

Example CI script:

```bash
#!/bin/bash
set -e
rdc open test_capture.rdc
rdc assert-clean
rdc assert-count --type DrawIndexed --min 10
rdc assert-pixel 256 256 --expect 1.0,0.0,0.0,1.0
rdc close
```

## Shader Edit-Replay

Modify and replay shaders without recompiling the application:

```bash
rdc shader-encodings EID          # list available encodings
rdc shader EID --source > s.frag  # extract shader source
# ... edit s.frag ...
rdc shader-build s.frag --encoding glsl  # compile edited shader
rdc shader-replace EID s.frag     # hot-swap into capture
rdc shader-restore EID            # revert single shader
rdc shader-restore-all            # revert all modifications
```

## Command Reference

For the complete list of all commands with their arguments, options, types, and defaults, see [references/commands-quick-ref.md](references/commands-quick-ref.md).


### 关键标记说明表

| 标记类型 | 标记名称 (包含部分匹配)      | 说明                       | 建议操作                  |
| -------- | ---------------------------- | -------------------------- | ------------------------- |
| **噪声** | `GUI.Repaint`                | IMGUI (旧版编辑器 UI)      | **排除**                  |
| **噪声** | `UIR.DrawChain`              | UI Toolkit (新版编辑器 UI) | **排除**                  |
| **噪声** | `EditorLoop`                 | 编辑器内部循环             | **排除**                  |
| **重要** | `UniversalRenderPipeline`    | URP 管线入口               | **关注**                  |
| **重要** | `RenderSingleCameraInternal` | 单个相机的完整渲染流程     | **关注** (配合相机名过滤) |
| **重要** | `Camera.Render`              | 传统的相机渲染标记         | **关注**                  |
| **重要** | `Render.OpaqueGeometry`      | 不透明物体绘制             | **关注**                  |
| **重要** | `Render.TransparentGeometry` | 半透明物体绘制             | **关注**                  |
| **重要** | `Shadows.RenderShadowMap`    | 阴影贴图生成               | **关注**                  |


## 重要注意事项

**每次只执行一个Renderdoc调用，禁止调用多个以防Renderdoc卡死**，