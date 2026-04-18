# -*- coding: utf-8 -*-
"""rdc_collect.py - Full RDC capture data collection + computed analysis + Render Graph.

Two-phase RDC analysis workflow:
  Phase 1 (this script): Automated data collection & algorithmic analysis.
  Phase 2 (AI):          Semantic analysis of collected data by Claude.

Usage:
    python\\python.exe rdc_collect.py <capture.rdc> [-j WORKERS]

Output:
    {capture-stem}-analysis/ directory with JSON data files + render_graph.html
"""

from __future__ import annotations

import os
import sys

if sys.version_info < (3, 10):
    print("ERROR: Python 3.10+ required. Use the embedded Python:")
    print("  python\\python.exe rdc_collect.py <capture.rdc>")
    sys.exit(1)

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))

import argparse
import signal
import time
from concurrent.futures import ThreadPoolExecutor, as_completed
from pathlib import Path

from shared import write_json
from rpc import run_rdc, _unwrap, Progress, ErrorCollector, MAIN_SESSION
from workers import (
    collect_base, collect_pass_details, _get_draw_eids,
    collect_per_draw, collect_shaders_disasm,
    collect_resource_details, collect_rt_usage,
    _shard_list, _get_resource_tasks,
    _collect_per_draw_shard, _collect_resources_shard,
    WorkerPool,
)
from computed import compute_analysis
from render_graph import generate_render_graph_html
from export_assets import (
    collect_meshes, collect_textures,
    _collect_meshes_shard, _collect_textures_shard,
)

# ─────────────────────────────────────────────────────────────────────
# Configuration
# ─────────────────────────────────────────────────────────────────────

VERSION = "1.2.0"


# ─────────────────────────────────────────────────────────────────────
# Main
# ─────────────────────────────────────────────────────────────────────

def main() -> None:
    parser = argparse.ArgumentParser(
        description="RDC capture data collection + computed analysis",
    )
    parser.add_argument("capture", type=Path, help="Path to .rdc capture file")
    parser.add_argument(
        "-j", "--workers", type=int, default=1,
        help="Number of parallel workers (default: 1, max: 8)",
    )
    parser.add_argument(
        "--export-assets", action="store_true",
        help="Export mesh OBJ and texture PNG files for each draw/resource",
    )
    args = parser.parse_args()

    capture = args.capture.resolve()
    if not capture.exists():
        print(f"ERROR: File not found: {capture}")
        sys.exit(1)

    num_workers = max(1, min(args.workers, 8))
    parallel = num_workers > 1

    out_dir = capture.parent / f"{capture.stem}-analysis"
    out_dir.mkdir(exist_ok=True)
    print(f"RDC Collect v{VERSION}")
    print(f"Capture: {capture}")
    print(f"Output:  {out_dir}")
    if parallel:
        print(f"Workers: {num_workers}")
    print()

    errors = ErrorCollector()
    timings: dict[str, float] = {}
    t_start = time.time()
    worker_pool: WorkerPool | None = None

    def _cleanup(signum=None, frame=None):
        print("\n\nInterrupted! Cleaning up sessions...")
        if worker_pool is not None:
            worker_pool.close_all()
        run_rdc("close", session=(MAIN_SESSION if parallel else None), timeout=10)
        sys.exit(1)

    signal.signal(signal.SIGINT, _cleanup)

    try:
        # ═══════════════════════════════════════════════════════════════
        # SERIAL PHASE (main session)
        # ═══════════════════════════════════════════════════════════════

        # ── Step 1: Open ──
        print("[Step 1] Opening capture ...")
        sess = MAIN_SESSION if parallel else None
        run_rdc("close", session=sess, timeout=10)
        out, err, rc = run_rdc("open", str(capture), session=sess)
        if rc != 0:
            print(f"  ERROR: rdc open failed: {err}")
            print("  Try running 'rdc doctor' to diagnose issues.")
            sys.exit(1)
        print(f"  Opened successfully.")

        # ── Step 2: Base data ──
        print("\n[Step 2] Collecting base data ...")
        t0 = time.time()
        sess = MAIN_SESSION if parallel else None
        summary = collect_base(errors, session=sess)
        summary["_meta"] = {
            "capture": str(capture),
            "collected_at": time.strftime("%Y-%m-%dT%H:%M:%S"),
            "version": VERSION,
        }
        write_json(out_dir / "summary.json", summary)
        timings["base"] = time.time() - t0

        # ── Step 3: Pass details ──
        print("\n[Step 3] Collecting pass details ...")
        t0 = time.time()
        pass_details = collect_pass_details(summary, errors, session=sess)
        write_json(out_dir / "pass_details.json", pass_details)
        timings["pass_details"] = time.time() - t0

        # ── Step 3.5: RT resource usage (for dependency graph) ──
        print("\n[Step 3.5] Collecting render target usage ...")
        t0 = time.time()
        rt_sess = MAIN_SESSION if parallel else "default"
        rt_usage = collect_rt_usage(
            pass_details, errors, session=rt_sess, summary=summary,
        )
        write_json(out_dir / "rt_usage.json", rt_usage)
        timings["rt_usage"] = time.time() - t0

        # ═══════════════════════════════════════════════════════════════
        # PARALLEL PHASE (worker sessions)
        # ═══════════════════════════════════════════════════════════════

        draw_eids = _get_draw_eids(summary)

        if parallel:
            print(f"\n  Starting {num_workers} worker sessions ...")
            t0 = time.time()
            worker_pool = WorkerPool(num_workers, str(capture))
            active_workers = worker_pool.open_all()
            timings["worker_open"] = time.time() - t0

            if not active_workers:
                print("  ERROR: No workers started, falling back to serial")
                parallel = False

        if parallel and active_workers:
            # ── Step 4: Per-draw pipeline+bindings (parallel) ──
            print(f"\n[Step 4] Collecting pipeline+bindings for {len(draw_eids)} draws ({len(active_workers)} workers) ...")
            t0 = time.time()
            shards = _shard_list(draw_eids, len(active_workers))
            progress = Progress(len(draw_eids), "Per-draw (pipeline+bindings)")
            all_pipelines: dict = {}
            all_bindings: dict = {}

            with ThreadPoolExecutor(max_workers=len(active_workers)) as executor:
                futures = {}
                for session, shard in zip(active_workers, shards):
                    if shard:
                        futures[executor.submit(
                            _collect_per_draw_shard, session, shard, progress, errors
                        )] = session

                for future in as_completed(futures):
                    try:
                        pipe_part, bind_part = future.result()
                        all_pipelines.update(pipe_part)
                        all_bindings.update(bind_part)
                    except Exception as exc:
                        errors.append({"phase": "per_draw", "error": str(exc)})

            progress.done()
            pipelines, bindings = all_pipelines, all_bindings
            write_json(out_dir / "pipelines.json", pipelines)
            write_json(out_dir / "bindings.json", bindings)
            timings["per_draw"] = time.time() - t0

            # ── Step 5: Shader disassembly (on main session — cache is per-session) ──
            print(f"\n[Step 5] Collecting shader disassembly (main session) ...")
            t0 = time.time()
            shader_disasm = collect_shaders_disasm(
                out_dir, errors, session=MAIN_SESSION,
            )
            write_json(out_dir / "shader_disasm.json", shader_disasm)
            timings["shader_disasm"] = time.time() - t0

            # ── Step 6: Resource details (parallel) ──
            resource_tasks = _get_resource_tasks(summary)
            print(f"\n[Step 6] Collecting resource details for {len(resource_tasks)} resources ({len(active_workers)} workers) ...")
            t0 = time.time()
            res_shards = _shard_list(resource_tasks, len(active_workers))
            res_progress = Progress(len(resource_tasks), "Resource details")
            all_resource_details: dict = {}

            with ThreadPoolExecutor(max_workers=len(active_workers)) as executor:
                futures = {}
                for session, shard in zip(active_workers, res_shards):
                    if shard:
                        futures[executor.submit(
                            _collect_resources_shard, session, shard, res_progress, errors
                        )] = session

                for future in as_completed(futures):
                    try:
                        all_resource_details.update(future.result())
                    except Exception as exc:
                        errors.append({"phase": "resource_details", "error": str(exc)})

            res_progress.done()
            resource_details = all_resource_details
            write_json(out_dir / "resource_details.json", resource_details)
            timings["resource_details"] = time.time() - t0

            # ── Step 6.5: Mesh & Texture export (parallel, opt-in) ──
            if args.export_assets:
                # Meshes
                print(f"\n[Step 6.5a] Exporting meshes for {len(draw_eids)} draws ({len(active_workers)} workers) ...")
                t0 = time.time()
                (out_dir / "meshes").mkdir(exist_ok=True)
                mesh_shards = _shard_list(draw_eids, len(active_workers))
                mesh_progress = Progress(len(draw_eids), "Mesh export")
                all_meshes: dict = {}

                with ThreadPoolExecutor(max_workers=len(active_workers)) as executor:
                    futures = {}
                    for session, shard in zip(active_workers, mesh_shards):
                        if shard:
                            futures[executor.submit(
                                _collect_meshes_shard, session, shard, out_dir, mesh_progress, errors
                            )] = session

                    for future in as_completed(futures):
                        try:
                            all_meshes.update(future.result())
                        except Exception as exc:
                            errors.append({"phase": "mesh_export", "error": str(exc)})

                mesh_progress.done()
                write_json(out_dir / "meshes.json", all_meshes)
                timings["mesh_export"] = time.time() - t0

                # Textures
                tex_tasks = [
                    (r["id"], r.get("name", ""))
                    for r in (_unwrap(summary.get("resources"), "resources") or [])
                    if isinstance(r, dict) and r.get("type") == "Texture"
                ]
                print(f"\n[Step 6.5b] Exporting {len(tex_tasks)} textures ({len(active_workers)} workers) ...")
                t0 = time.time()
                (out_dir / "textures").mkdir(exist_ok=True)
                tex_shards = _shard_list(tex_tasks, len(active_workers))
                tex_progress = Progress(len(tex_tasks), "Texture export")
                all_textures: dict = {}

                with ThreadPoolExecutor(max_workers=len(active_workers)) as executor:
                    futures = {}
                    for session, shard in zip(active_workers, tex_shards):
                        if shard:
                            futures[executor.submit(
                                _collect_textures_shard, session, shard, out_dir, tex_progress, errors
                            )] = session

                    for future in as_completed(futures):
                        try:
                            all_textures.update(future.result())
                        except Exception as exc:
                            errors.append({"phase": "texture_export", "error": str(exc)})

                tex_progress.done()
                write_json(out_dir / "textures.json", all_textures)
                timings["texture_export"] = time.time() - t0

            # Close worker sessions
            print("\n  Closing worker sessions ...")
            worker_pool.close_all()
            worker_pool = None

        else:
            # ── Serial fallback (workers=1) ──
            print(f"\n[Step 4] Collecting pipeline+bindings for {len(draw_eids)} draws ...")
            t0 = time.time()
            pipelines, bindings = collect_per_draw(draw_eids, errors)
            write_json(out_dir / "pipelines.json", pipelines)
            write_json(out_dir / "bindings.json", bindings)
            timings["per_draw"] = time.time() - t0

            print("\n[Step 5] Collecting shader disassembly ...")
            t0 = time.time()
            shader_disasm = collect_shaders_disasm(out_dir, errors)
            write_json(out_dir / "shader_disasm.json", shader_disasm)
            timings["shader_disasm"] = time.time() - t0

            print("\n[Step 6] Collecting resource details ...")
            t0 = time.time()
            resource_details = collect_resource_details(summary, errors)
            write_json(out_dir / "resource_details.json", resource_details)
            timings["resource_details"] = time.time() - t0

            # ── Step 6.5: Mesh & Texture export (serial, opt-in) ──
            if args.export_assets:
                print(f"\n[Step 6.5a] Exporting meshes for {len(draw_eids)} draws ...")
                t0 = time.time()
                meshes = collect_meshes(draw_eids, out_dir, errors)
                write_json(out_dir / "meshes.json", meshes)
                timings["mesh_export"] = time.time() - t0

                print(f"\n[Step 6.5b] Exporting textures ...")
                t0 = time.time()
                textures = collect_textures(summary, out_dir, errors)
                write_json(out_dir / "textures.json", textures)
                timings["texture_export"] = time.time() - t0

        # ═══════════════════════════════════════════════════════════════
        # POST-MERGE PHASE
        # ═══════════════════════════════════════════════════════════════

        # ── Step 7: Computed analysis ──
        print("\n[Step 7] Running computed analysis ...")
        t0 = time.time()
        computed = compute_analysis(summary, pass_details, pipelines, resource_details)
        write_json(out_dir / "computed.json", computed)
        timings["computed"] = time.time() - t0
        print(f"  Done: computed analysis")

        # ── Step 8: Render Graph HTML ──
        print("\n[Step 8] Generating Render Graph ...")
        t0 = time.time()
        res_list = _unwrap(summary.get("resources"), "resources") or []
        res_names: dict[int, str] = {}
        for r in res_list:
            if isinstance(r, dict) and "id" in r:
                res_names[r["id"]] = r.get("name", f"Resource {r['id']}")
        html_assets_dir = Path(__file__).resolve().parent.parent.parent / "assets"
        assets_rel = os.path.relpath(html_assets_dir, out_dir).replace("\\", "/")
        html = generate_render_graph_html(summary, pass_details, res_names, rt_usage, assets_rel=assets_rel)
        (out_dir / "render_graph.html").write_text(html, encoding="utf-8")
        timings["render_graph"] = time.time() - t0
        print(f"  Done: render_graph.html")

        # ── Step 9: Close main session ──
        print("\n[Step 9] Closing session ...")
        run_rdc("close", session=sess)

    finally:
        if worker_pool is not None:
            worker_pool.close_all()

    # ── Collection metadata ──
    t_total = time.time() - t_start
    collection_meta = {
        "version": VERSION,
        "capture": str(capture),
        "workers": num_workers,
        "parallelized": num_workers > 1,
        "started_at": time.strftime(
            "%Y-%m-%dT%H:%M:%S",
            time.localtime(t_start),
        ),
        "completed_at": time.strftime("%Y-%m-%dT%H:%M:%S"),
        "total_seconds": round(t_total, 1),
        "timings": {k: round(v, 1) for k, v in timings.items()},
        "error_count": len(errors),
        "errors": errors.errors[:100],
    }
    write_json(out_dir / "_collection.json", collection_meta)

    # ── Summary ──
    print(f"\n{'='*60}")
    print(f"  Collection complete!")
    print(f"  Output:     {out_dir}")
    print(f"  Total time: {t_total:.1f}s")
    if num_workers > 1:
        print(f"  Workers:    {num_workers}")
    print(f"  Errors:     {len(errors)}")
    print(f"  Files:")
    for f in sorted(out_dir.iterdir()):
        size = f.stat().st_size
        if size > 1024 * 1024:
            s = f"{size / 1024 / 1024:.1f} MB"
        elif size > 1024:
            s = f"{size / 1024:.1f} KB"
        else:
            s = f"{size} B"
        print(f"    {f.name:30s} {s:>10s}")
    print(f"{'='*60}")


if __name__ == "__main__":
    main()
