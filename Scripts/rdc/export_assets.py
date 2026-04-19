# -*- coding: utf-8 -*-
"""Mesh and texture asset export from RDC captures."""

from __future__ import annotations

import hashlib
from pathlib import Path

from rpc import run_rdc, run_rdc_json, _rpc_call, _unwrap, Progress, ErrorCollector
from fbx_writer import write_fbx

MIN_VERTEX_COUNT = 300

# vbuffer attribute name → FBX semantic name
_ATTR_ALIASES: dict[str, str] = {
    "TEXCOORD0": "UV",
    "TEXCOORD1": "UV2",
    "TEXCOORD": "UV",
    "COLOR0": "COLOR",
    "COLOR1": "COLOR",
}

# Known semantic name patterns (case-insensitive prefix match)
_SEMANTIC_PATTERNS: list[tuple[str, str]] = [
    ("position", "POSITION"),
    ("pos", "POSITION"),
    ("normal", "NORMAL"),
    ("tangent", "TANGENT"),
    ("binormal", "TANGENT"),
    ("texcoord", "UV"),
    ("uv", "UV"),
    ("color", "COLOR"),
]


def _infer_semantic(raw_attrs: dict[str, int]) -> dict[str, str]:
    """Map raw attribute names to FBX semantics using name patterns + component count heuristics.

    Args:
        raw_attrs: {attr_name: component_count}
    Returns:
        {raw_name: semantic_name}
    """
    mapping: dict[str, str] = {}
    used_semantics: set[str] = set()

    # Pass 1: match by known name patterns
    for raw_name, comp_count in raw_attrs.items():
        name_lower = raw_name.lower()
        if raw_name in _ATTR_ALIASES:
            sem = _ATTR_ALIASES[raw_name]
            mapping[raw_name] = sem
            used_semantics.add(sem)
            continue
        for pattern, semantic in _SEMANTIC_PATTERNS:
            if pattern in name_lower and semantic not in used_semantics:
                if semantic == "UV" and "UV" in used_semantics:
                    semantic = "UV2"
                mapping[raw_name] = semantic
                used_semantics.add(semantic)
                break

    # Pass 2: heuristic for generic names (_input0, ATTRIBUTE0, etc.)
    unmapped = [n for n in raw_attrs if n not in mapping]
    if unmapped:
        pos_assigned = "POSITION" in used_semantics
        normal_assigned = "NORMAL" in used_semantics
        uv_assigned = "UV" in used_semantics

        for raw_name in unmapped:
            comp_count = raw_attrs[raw_name]
            if comp_count >= 3 and not pos_assigned:
                mapping[raw_name] = "POSITION"
                pos_assigned = True
            elif comp_count >= 3 and not normal_assigned:
                mapping[raw_name] = "NORMAL"
                normal_assigned = True
            elif comp_count == 2 and not uv_assigned:
                mapping[raw_name] = "UV"
                uv_assigned = True
            elif comp_count == 2 and uv_assigned and "UV2" not in used_semantics:
                mapping[raw_name] = "UV2"
            elif comp_count >= 4 and "TANGENT" not in used_semantics:
                mapping[raw_name] = "TANGENT"
            elif comp_count >= 3 and "COLOR" not in used_semantics:
                mapping[raw_name] = "COLOR"
            used_semantics = set(mapping.values())

    return mapping


def _parse_vbuffer(vbuffer: dict) -> dict[str, list[list[float]]]:
    """Parse vbuffer_decode JSON into per-unique-vertex attribute dict.

    Handles both named attributes (POSITION, NORMAL) and GLES-style
    generic names (_input0, _input1) via heuristic mapping.

    Output: {"POSITION": [[x,y,z], ...], "NORMAL": [[nx,ny,nz], ...], ...}
    """
    columns = vbuffer.get("columns") or []
    vertices = vbuffer.get("vertices") or []
    if not columns or not vertices:
        return {}

    # Group column indices by raw attribute name
    raw_cols: dict[str, list[int]] = {}
    for i, col in enumerate(columns):
        attr_name = col.split(".")[0] if "." in col else col
        raw_cols.setdefault(attr_name, []).append(i)

    # Build component count map and infer semantics
    raw_comp_counts = {name: len(indices) for name, indices in raw_cols.items()}
    name_mapping = _infer_semantic(raw_comp_counts)

    result: dict[str, list[list[float]]] = {}
    for raw_name, col_indices in raw_cols.items():
        semantic = name_mapping.get(raw_name)
        if not semantic:
            continue
        result[semantic] = [
            [row[j] for j in col_indices if j < len(row)]
            for row in vertices
        ]
    return result


def _expand_by_indices(
    attr_data: dict[str, list[list[float]]],
    indices: list[int],
) -> dict[str, list[list[float]] | list[int]]:
    """Expand per-unique-vertex data to per-polygon-vertex using indices.

    Returns FBX-ready data dict with "IDX" + all attributes.
    """
    data: dict[str, list[list[float]] | list[int]] = {"IDX": indices}
    for attr_name, unique_verts in attr_data.items():
        expanded = []
        for idx in indices:
            if idx < len(unique_verts):
                expanded.append(unique_verts[idx])
            else:
                comp_count = len(unique_verts[0]) if unique_verts else 3
                expanded.append([0.0] * comp_count)
        data[attr_name] = expanded
    return data


def _fbx_content_hash(path: Path, eid: int) -> str:
    """Hash FBX file content with model name normalized out for dedup."""
    content = path.read_bytes()
    content = content.replace(f"draw_{eid}".encode(), b"draw__dedup__")
    return hashlib.md5(content).hexdigest()


def _export_one_mesh(
    eid: int,
    meshes_dir: Path,
    errors: ErrorCollector,
    *,
    session: str | None = None,
) -> dict | None:
    """Export a single draw call as FBX. Returns result dict or None if skipped."""
    mesh_info = run_rdc_json("mesh", str(eid), session=session, timeout=60)
    if not mesh_info or mesh_info.get("vertex_count", 0) < MIN_VERTEX_COUNT:
        return None

    indices = mesh_info.get("indices") or list(range(mesh_info.get("vertex_count", 0)))

    vbuffer = run_rdc_json("cat", f"/draws/{eid}/vbuffer", session=session, timeout=60)
    if not vbuffer:
        errors.append({"phase": "mesh_export", "eid": eid, "error": "vbuffer decode failed"})
        return None

    attr_data = _parse_vbuffer(vbuffer)
    if not attr_data.get("POSITION"):
        errors.append({"phase": "mesh_export", "eid": eid, "error": "no POSITION in vbuffer"})
        return None

    fbx_data = _expand_by_indices(attr_data, indices)
    attrs = [k for k in fbx_data if k != "IDX"]

    out_file = meshes_dir / f"mesh_{eid}.fbx"
    try:
        write_fbx(out_file, f"draw_{eid}", fbx_data)
    except Exception as exc:
        errors.append({"phase": "mesh_export", "eid": eid, "error": str(exc)})
        return None

    if not out_file.exists():
        return None

    return {
        "file": f"meshes/mesh_{eid}.fbx",
        "vertex_count": mesh_info["vertex_count"],
        "attributes": attrs,
        "size_bytes": out_file.stat().st_size,
        "_eid": eid,
    }


def _dedup_meshes(results: dict, meshes_dir: Path) -> int:
    """Post-process: deduplicate exported FBX files by content hash.

    Modifies results in-place: duplicate entries get 'dedup_of' and point
    to the original file. Duplicate files are deleted from disk.
    Returns number of duplicates removed.
    """
    hash_to_eid: dict[str, str] = {}
    deduped = 0
    for eid_str in sorted(results, key=int):
        result = results[eid_str]
        fbx_path = meshes_dir / f"mesh_{eid_str}.fbx"
        if not fbx_path.exists():
            continue
        fh = _fbx_content_hash(fbx_path, int(eid_str))
        if fh in hash_to_eid:
            orig_eid = hash_to_eid[fh]
            orig = results[orig_eid]
            fbx_path.unlink()
            result["file"] = orig["file"]
            result["size_bytes"] = orig["size_bytes"]
            result["dedup_of"] = orig["_eid"]
            deduped += 1
        else:
            hash_to_eid[fh] = eid_str
    return deduped


def collect_meshes(
    draw_eids: list[int],
    out_dir: Path,
    errors: ErrorCollector,
    *,
    session: str | None = None,
) -> tuple[dict, set[int]]:
    """Export FBX mesh for each draw call.

    Writes to {out_dir}/meshes/mesh_{eid}.fbx.
    Returns (results_dict, significant_eids_set).
    """
    if not draw_eids:
        return {}, set()

    meshes_dir = out_dir / "meshes"
    meshes_dir.mkdir(exist_ok=True)

    results: dict = {}
    skipped = 0
    prog = Progress(len(draw_eids), "Mesh export")
    for eid in draw_eids:
        prog.tick(f"EID {eid}")
        result = _export_one_mesh(eid, meshes_dir, errors, session=session)
        if result:
            results[str(eid)] = result
        else:
            skipped += 1
    prog.done()

    # Post-process dedup: hash all files, delete duplicates
    deduped = _dedup_meshes(results, meshes_dir)

    exported = len(results) - deduped
    if skipped or deduped:
        print(f"    Exported {exported} unique meshes, {deduped} deduplicated, {skipped} skipped")
    significant_eids = {int(eid) for eid in results}
    return results, significant_eids


def collect_draw_texture_ids(
    significant_eids: set[int],
    errors: ErrorCollector,
    *,
    session: str | None = None,
) -> set[int]:
    """Query descriptors for significant draws to find bound texture resource IDs."""
    sess_name = session or "default"
    texture_ids: set[int] = set()
    for eid in significant_eids:
        result = _rpc_call(sess_name, "descriptors", {"eid": eid}, timeout=30)
        if not result or not isinstance(result, dict):
            continue
        for d in result.get("descriptors") or []:
            if "Image" in d.get("type", "") or "Texture" in d.get("type", ""):
                rid = d.get("resource_id", 0)
                if rid:
                    texture_ids.add(rid)
    return texture_ids


def collect_textures(
    summary: dict,
    out_dir: Path,
    errors: ErrorCollector,
    *,
    session: str | None = None,
    resource_ids: set[int] | None = None,
) -> dict:
    """Export PNG for each texture resource.

    If resource_ids is given, only exports textures in that set.
    Writes to {out_dir}/textures/tex_{id}.png.
    Returns {id_str: {"file": str, "name": str, "size_bytes": int}}.
    """
    resources = _unwrap(summary.get("resources"), "resources")
    if not resources or not isinstance(resources, list):
        return {}

    tex_resources = [
        r for r in resources
        if isinstance(r, dict) and r.get("type") == "Texture"
        and (resource_ids is None or r.get("id") in resource_ids)
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


def filter_shader_disasm(
    shader_disasm: dict,
    significant_eids: set[int],
) -> dict:
    """Filter shader_disasm to only shaders used by significant draws/dispatches.

    CS shaders (key starts with 'cs_') are always included since compute
    dispatches have no mesh significance metric.
    """
    if not shader_disasm or not significant_eids:
        return {}
    filtered: dict = {}
    for pair_key, info in shader_disasm.items():
        if not isinstance(info, dict):
            continue
        if "cs_id" in info:
            filtered[pair_key] = info
            continue
        eids = info.get("eids") or []
        if set(eids) & significant_eids:
            filtered[pair_key] = info
    return filtered


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
    """Export FBX meshes for a shard of draw EIDs (worker thread)."""
    meshes_dir = out_dir / "meshes"
    results: dict = {}
    for eid in eid_shard:
        progress.tick(f"EID {eid}")
        result = _export_one_mesh(eid, meshes_dir, errors, session=session)
        if result:
            results[str(eid)] = result
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
