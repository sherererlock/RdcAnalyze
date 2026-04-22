# Troubleshooting

Common errors and fixes for RdcAnalyze.

---

## Shader cache build timeout

**Symptom**: `collect.py` hangs or prints a timeout error during the "Building shader cache" step. The default rdc-cli timeout is 30 seconds; large captures with hundreds of shaders can exceed this.

**Fix**: Already handled transparently. `collect.py` uses JSON-RPC direct socket calls (`_rpc_call()`) for shader cache construction, bypassing the 30-second CLI timeout. If you still see timeouts:

1. Check that the rdc daemon session started correctly (`%LOCALAPPDATA%\rdc\sessions\rdc-collect.json` should exist while the job runs).
2. Try a lower parallelism: `Scripts\rdc-report.bat capture.rdc -j 2` to reduce concurrent daemon load.
3. For captures > 500 shaders, expect the cache step to take 2–5 minutes.

---

## Worker daemon processes left running after Ctrl-C

**Symptom**: After interrupting `collect.py` with Ctrl-C, `rdc.exe` / `renderdoccmd.exe` processes remain in Task Manager.

**Fix**: Kill them manually:

```bat
taskkill /F /IM renderdoccmd.exe
```

Or clear stale session files:

```bat
del "%LOCALAPPDATA%\rdc\sessions\rdc-collect*.json"
```

Session files are named `rdc-collect.json`, `rdc-collect-w0.json`, … `rdc-collect-wN.json` for N parallel workers.

**Why it happens**: Python's `KeyboardInterrupt` may not propagate cleanly to subprocess children on Windows. A future improvement would add a `SIGTERM` handler to the worker pool.

---

## "mesh vertex count < 300, skipping" — assets not exported

**Symptom**: After running `collect.py --export-assets`, some meshes are missing from `meshes/`.

**Cause**: `export_assets.py` skips meshes with fewer than 300 vertices to avoid exporting screen-quad or debug primitives.

**Override**: The threshold is defined in `export_assets.py`. If you need smaller meshes, lower `MIN_VERTEX_COUNT` at the top of that file.

---

## GLES capture: no load/store ops in TBDR section

**Symptom**: The TBDR Tile Efficiency section in `performance_report.html` shows "load/store op data not available for this capture."

**Cause**: OpenGL ES captures do not expose explicit load/store op metadata via rdc-cli — this information is Vulkan-only (comes from `VkAttachmentDescription`). The TBDR analysis requires Vulkan or Metal captures.

**Workaround**: None for GLES. Use the Bandwidth Estimation section instead, which estimates RT bandwidth from texture format and dimensions regardless of API.

---

## Port conflict: daemon fails to start

**Symptom**: `collect.py` fails with a connection error immediately, before any data is collected. Log shows something like "connection refused" or "address already in use."

**Cause**: rdc-cli daemon uses a random port stored in `%LOCALAPPDATA%\rdc\sessions\{session}.json`. A stale session file from a previous crashed run points to a port that is now used by another process.

**Fix**:

```bat
del "%LOCALAPPDATA%\rdc\sessions\rdc-collect*.json"
```

Then rerun. The daemon will pick a new port.

---

## "No passes found" in report

**Symptom**: `performance_report.html` shows 0 passes or the pipeline section is empty.

**Cause**: The capture has no draw calls at the top level, or the `.rdc` file is corrupt / not from a supported API.

**Checks**:
1. Open the capture in RenderDoc GUI and confirm it has frames with draw calls.
2. Run `rdc-portable\rdc.bat open capture.rdc` then `rdc-portable\rdc.bat info --json` to verify the session opens.
3. Check `{capture}-analysis/json/_collection.json` for error entries — any per-step failures are recorded there.

---

## OpenGL capture shows too many passes (one per draw)

**Symptom**: The pipeline table shows hundreds of auto-named passes like `Pass #0`, `Pass #1`, … one per draw call.

**Cause**: RenderDoc detects OpenGL pass boundaries at `glBindFramebuffer()` calls, so each draw that binds a framebuffer creates a new "pass." If the engine uses `glPushDebugGroup` / `glPopDebugGroup` markers, `analyze.py` will automatically collapse these into the correct named groups.

**Fix**: Ensure your OpenGL engine emits debug group markers. If it does and the report still shows per-draw passes, check that the capture was taken with RenderDoc's "Capture child processes" option disabled (to avoid marker hierarchy corruption).

---

## Analysis directory not found

**Symptom**: `analyze.py` exits with "analysis directory not found" or similar.

**Cause**: `collect.py` must run successfully before `analyze.py`. The analysis directory (`{capture}-analysis/`) and its `json/` subdirectory must exist.

**Fix**: Run the full pipeline:

```bat
Scripts\rdc-report.bat capture.rdc
```

Or run phases in order:

```bat
python\python.exe Scripts\rdc\collect.py capture.rdc
python\python.exe Scripts\rdc\analyze.py capture-analysis/
```

---

## Slow collection on large captures

**Tip**: Use `-j N` to collect per-draw data in parallel. The optimal value depends on available RAM and CPU:

```bat
Scripts\rdc-report.bat capture.rdc -j 8
```

Each worker runs a separate rdc daemon session. More than 8–12 workers rarely helps and increases peak memory usage.
