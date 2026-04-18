# -*- coding: utf-8 -*-
"""ASCII FBX writer for mesh export.

Based on renderdoc2fbx (https://github.com/FXTD-ODYSSEY/renderdoc2fbx),
adapted for standalone use without GUI dependencies.
"""

from __future__ import annotations

from pathlib import Path
from textwrap import dedent

FBX_ASCII_TEMPLATE = """\
; FBX 7.3.0 project file
; ----------------------------------------------------

Definitions:  {

    ObjectType: "Geometry" {
        Count: 1
        PropertyTemplate: "FbxMesh" {
            Properties70:  {
                P: "Primary Visibility", "bool", "", "",1
            }
        }
    }

    ObjectType: "Model" {
        Count: 1
        PropertyTemplate: "FbxNode" {
            Properties70:  {
                P: "Visibility", "Visibility", "", "A",1
            }
        }
    }
}

Objects:  {
    Geometry: 2035541511296, "Geometry::", "Mesh" {
        Vertices: *%(vertices_num)s {
            a: %(vertices)s
        }
        PolygonVertexIndex: *%(polygons_num)s {
            a: %(polygons)s
        }
        GeometryVersion: 124
        %(LayerElementNormal)s
        %(LayerElementTangent)s
        %(LayerElementColor)s
        %(LayerElementUV)s
        %(LayerElementUV2)s
        Layer: 0 {
            Version: 100
            %(LayerElementNormalInsert)s
            %(LayerElementTangentInsert)s
            %(LayerElementColorInsert)s
            %(LayerElementUVInsert)s
        }
        Layer: 1 {
            Version: 100
            %(LayerElementUV2Insert)s
        }
    }
    Model: 2035615390896, "Model::%(model_name)s", "Mesh" {
        Properties70:  {
            P: "DefaultAttributeIndex", "int", "Integer", "",0
        }
    }
}

Connections:  {
    C: "OO",2035615390896,0
    C: "OO",2035541511296,2035615390896
}
"""

_LAYER_INSERT = {
    "Normal": 'LayerElement:  {\n            Type: "LayerElementNormal"\n            TypedIndex: 0\n        }',
    "Tangent": 'LayerElement:  {\n            Type: "LayerElementTangent"\n            TypedIndex: 0\n        }',
    "Color": 'LayerElement:  {\n            Type: "LayerElementColor"\n            TypedIndex: 0\n        }',
    "UV": 'LayerElement:  {\n            Type: "LayerElementUV"\n            TypedIndex: 0\n        }',
    "UV2": 'LayerElement:  {\n            Type: "LayerElementUV"\n            TypedIndex: 1\n        }',
}


def _fmt_normals(per_poly_vertex: list[list[float]]) -> str:
    normals = ",".join(str(v) for values in per_poly_vertex for v in values[:3])
    count = sum(min(len(v), 3) for v in per_poly_vertex)
    return (
        f'LayerElementNormal: 0 {{\n'
        f'    Version: 101\n'
        f'    Name: ""\n'
        f'    MappingInformationType: "ByPolygonVertex"\n'
        f'    ReferenceInformationType: "Direct"\n'
        f'    Normals: *{count} {{\n'
        f'        a: {normals}\n'
        f'    }}\n'
        f'}}'
    )


def _fmt_tangents(per_poly_vertex: list[list[float]]) -> str:
    tangents = ",".join(str(v) for values in per_poly_vertex for v in values[:3])
    count = sum(min(len(v), 3) for v in per_poly_vertex)
    return (
        f'LayerElementTangent: 0 {{\n'
        f'    Version: 101\n'
        f'    Name: "map1"\n'
        f'    MappingInformationType: "ByPolygonVertex"\n'
        f'    ReferenceInformationType: "Direct"\n'
        f'    Tangents: *{count} {{\n'
        f'        a: {tangents}\n'
        f'    }}\n'
        f'}}'
    )


def _fmt_colors(per_poly_vertex: list[list[float]], idx_len: int) -> str:
    colors = ",".join(str(v) for values in per_poly_vertex for v in values)
    count = sum(len(v) for v in per_poly_vertex)
    indices = ",".join(str(i) for i in range(idx_len))
    return (
        f'LayerElementColor: 0 {{\n'
        f'    Version: 101\n'
        f'    Name: "colorSet1"\n'
        f'    MappingInformationType: "ByPolygonVertex"\n'
        f'    ReferenceInformationType: "IndexToDirect"\n'
        f'    Colors: *{count} {{\n'
        f'        a: {colors}\n'
        f'    }}\n'
        f'    ColorIndex: *{idx_len} {{\n'
        f'        a: {indices}\n'
        f'    }}\n'
        f'}}'
    )


def _fmt_uvs(unique_vertex_data: dict[int, list[float]], idx_list: list[int], layer_idx: int = 0) -> str:
    name = "map1" if layer_idx == 0 else "map2"
    uvs = ",".join(
        str(1 - v if i else v)
        for _idx, values in sorted(unique_vertex_data.items())
        for i, v in enumerate(values[:2])
    )
    uv_count = sum(min(len(v), 2) for v in unique_vertex_data.values())
    uv_indices = ",".join(str(idx) for idx in idx_list)
    return (
        f'LayerElementUV: {layer_idx} {{\n'
        f'    Version: 101\n'
        f'    Name: "{name}"\n'
        f'    MappingInformationType: "ByPolygonVertex"\n'
        f'    ReferenceInformationType: "IndexToDirect"\n'
        f'    UV: *{uv_count} {{\n'
        f'        a: {uvs}\n'
        f'    }}\n'
        f'    UVIndex: *{len(idx_list)} {{\n'
        f'        a: {uv_indices}\n'
        f'    }}\n'
        f'}}'
    )


def write_fbx(
    path: str | Path,
    model_name: str,
    data: dict[str, list[list[float]] | list[int]],
) -> None:
    """Write mesh data as ASCII FBX file.

    Args:
        path: Output file path.
        model_name: Model name in FBX scene.
        data: Dict with keys:
            "IDX": list[int] — triangle indices (flat)
            "POSITION": list[list[float]] — per-unique-vertex positions [x,y,z]
            Plus optional per-polygon-vertex attributes:
            "NORMAL": list[list[float]], "TANGENT": list[list[float]],
            "COLOR": list[list[float]], "UV": list[list[float]], "UV2": list[list[float]]
    """
    idx_dict = data["IDX"]
    position_data = data.get("POSITION", [])
    if not idx_dict or not position_data:
        return

    min_poly = min(idx_dict)
    idx_list = [idx - min_poly for idx in idx_dict]
    idx_len = len(idx_list)

    # Build per-polygon-vertex value_dict and per-unique-vertex vertex_data
    attr_names = [k for k in data if k not in ("IDX", "POSITION")]
    value_dict: dict[str, list[list[float]]] = {k: [] for k in attr_names}
    vertex_data: dict[str, dict[int, list[float]]] = {k: {} for k in attr_names}

    for i, idx in enumerate(idx_dict):
        for attr in attr_names:
            attr_values = data[attr]
            if i < len(attr_values):
                value = attr_values[i]
                value_dict[attr].append(value)
                if idx not in vertex_data[attr]:
                    vertex_data[attr][idx] = value

    # Also build unique vertex_data for POSITION
    pos_unique: dict[int, list[float]] = {}
    for i, idx in enumerate(idx_dict):
        if idx not in pos_unique and i < len(position_data):
            pos_unique[idx] = position_data[i]

    args: dict[str, str] = {
        "model_name": model_name,
        "LayerElementNormal": "",
        "LayerElementNormalInsert": "",
        "LayerElementTangent": "",
        "LayerElementTangentInsert": "",
        "LayerElementColor": "",
        "LayerElementColorInsert": "",
        "LayerElementUV": "",
        "LayerElementUVInsert": "",
        "LayerElementUV2": "",
        "LayerElementUV2Insert": "",
    }

    # Vertices (unique, sorted by index)
    vertices = ",".join(str(v) for _idx, values in sorted(pos_unique.items()) for v in values[:3])
    args["vertices"] = vertices
    args["vertices_num"] = str(sum(min(len(v), 3) for v in pos_unique.values()))

    # Polygon indices (XOR -1 on every 3rd for FBX triangle end marker)
    polygons = ",".join(str(idx ^ -1 if i % 3 == 2 else idx) for i, idx in enumerate(idx_list))
    args["polygons"] = polygons
    args["polygons_num"] = str(len(idx_list))

    # Optional attributes
    if value_dict.get("NORMAL"):
        args["LayerElementNormal"] = _fmt_normals(value_dict["NORMAL"])
        args["LayerElementNormalInsert"] = _LAYER_INSERT["Normal"]

    if value_dict.get("TANGENT"):
        args["LayerElementTangent"] = _fmt_tangents(value_dict["TANGENT"])
        args["LayerElementTangentInsert"] = _LAYER_INSERT["Tangent"]

    if value_dict.get("COLOR"):
        args["LayerElementColor"] = _fmt_colors(value_dict["COLOR"], idx_len)
        args["LayerElementColorInsert"] = _LAYER_INSERT["Color"]

    if vertex_data.get("UV"):
        args["LayerElementUV"] = _fmt_uvs(vertex_data["UV"], idx_list, layer_idx=0)
        args["LayerElementUVInsert"] = _LAYER_INSERT["UV"]

    if vertex_data.get("UV2"):
        args["LayerElementUV2"] = _fmt_uvs(vertex_data["UV2"], idx_list, layer_idx=1)
        args["LayerElementUV2Insert"] = _LAYER_INSERT["UV2"]

    fbx_text = FBX_ASCII_TEMPLATE % args
    Path(path).write_text(fbx_text, encoding="utf-8")
