# -*- coding: utf-8 -*-
"""Mesh and texture asset export from RDC captures."""

from __future__ import annotations

from pathlib import Path

from rpc import run_rdc, _unwrap, Progress, ErrorCollector


def collect_meshes(
    draw_eids: list[int],
    out_dir: Path,
    errors: ErrorCollector,
    *,
    session: str | None = None,
) -> dict:
    """Export OBJ mesh for each draw call.

    Writes to {out_dir}/meshes/mesh_{eid}.obj.
    Returns {eid_str: {"file": str, "size_bytes": int}}.
    """
    if not draw_eids:
        return {}

    meshes_dir = out_dir / "meshes"
    meshes_dir.mkdir(exist_ok=True)

    results: dict = {}
    prog = Progress(len(draw_eids), "Mesh export")
    for eid in draw_eids:
        prog.tick(f"EID {eid}")
        out_file = meshes_dir / f"mesh_{eid}.obj"
        _out, err, rc = run_rdc(
            "mesh", str(eid), "-o", str(out_file),
            session=session, timeout=60,
        )
        if rc == 0 and out_file.exists():
            results[str(eid)] = {
                "file": f"meshes/mesh_{eid}.obj",
                "size_bytes": out_file.stat().st_size,
            }
        else:
            errors.append({"phase": "mesh_export", "eid": eid, "error": err or "failed"})
    prog.done()
    return results


def collect_textures(
    summary: dict,
    out_dir: Path,
    errors: ErrorCollector,
    *,
    session: str | None = None,
) -> dict:
    """Export PNG for each texture resource.

    Writes to {out_dir}/textures/tex_{id}.png.
    Returns {id_str: {"file": str, "name": str, "size_bytes": int}}.
    """
    resources = _unwrap(summary.get("resources"), "resources")
    if not resources or not isinstance(resources, list):
        return {}

    tex_resources = [
        r for r in resources
        if isinstance(r, dict) and r.get("type") == "Texture"
    ]
    if not tex_resources:
        return {}

    textures_dir = out_dir / "textures"
    textures_dir.mkdir(exist_ok=True)

    results: dict = {}
    prog = Progress(len(tex_resources), "Texture export")
    for res in tex_resources:
        rid = res["id"]
        prog.tick(f"texture {rid}")
        out_file = textures_dir / f"tex_{rid}.png"
        _out, err, rc = run_rdc(
            "texture", str(rid), "-o", str(out_file),
            session=session, timeout=60,
        )
        if rc == 0 and out_file.exists():
            results[str(rid)] = {
                "file": f"textures/tex_{rid}.png",
                "name": res.get("name", ""),
                "size_bytes": out_file.stat().st_size,
            }
        else:
            errors.append({"phase": "texture_export", "id": rid, "error": err or "failed"})
    prog.done()
    return results


# ─────────────────────────────────────────────────────────────────────
# Parallel shard variants for WorkerPool integration
# ─────────────────────────────────────────────────────────────────────

def _collect_meshes_shard(
    session: str,
    eid_shard: list[int],
    out_dir: Path,
    progress: Progress,
    errors: ErrorCollector,
) -> dict:
    """Export meshes for a shard of draw EIDs (worker thread)."""
    meshes_dir = out_dir / "meshes"
    results: dict = {}
    for eid in eid_shard:
        progress.tick(f"EID {eid}")
        out_file = meshes_dir / f"mesh_{eid}.obj"
        _out, err, rc = run_rdc(
            "mesh", str(eid), "-o", str(out_file),
            session=session, timeout=60,
        )
        if rc == 0 and out_file.exists():
            results[str(eid)] = {
                "file": f"meshes/mesh_{eid}.obj",
                "size_bytes": out_file.stat().st_size,
            }
        else:
            errors.append({"phase": "mesh_export", "eid": eid, "error": err or "failed"})
    return results


def _collect_textures_shard(
    session: str,
    tex_tasks: list[tuple[int, str]],
    out_dir: Path,
    progress: Progress,
    errors: ErrorCollector,
) -> dict:
    """Export textures for a shard of resource IDs (worker thread)."""
    textures_dir = out_dir / "textures"
    results: dict = {}
    for rid, name in tex_tasks:
        progress.tick(f"texture {rid}")
        out_file = textures_dir / f"tex_{rid}.png"
        _out, err, rc = run_rdc(
            "texture", str(rid), "-o", str(out_file),
            session=session, timeout=60,
        )
        if rc == 0 and out_file.exists():
            results[str(rid)] = {
                "file": f"textures/tex_{rid}.png",
                "name": name,
                "size_bytes": out_file.stat().st_size,
            }
        else:
            errors.append({"phase": "texture_export", "id": rid, "error": err or "failed"})
    return results
