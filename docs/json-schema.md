# JSON 数据文件结构说明

`collect.py` 运行后在 `{capture-stem}-analysis/` 目录下生成以下 JSON 文件。本文档描述每个文件的结构、字段含义，以及它们在管线中的作用。

---

## 目录总览

| 文件 | 阶段 | 用途 |
|------|------|------|
| `summary.json` | Step 2 | 帧的全局概要：API 信息、Draw Call 列表、Pass 列表、资源列表等 |
| `pass_details.json` | Step 3 | 每个 Pass 的 Render Target 附件详情 |
| `rt_usage.json` | Step 3.5 | 渲染目标资源的使用事件 + Descriptor 绑定，用于构建依赖图 |
| `pipelines.json` | Step 4 | 每个 Draw Call 的管线状态（光栅化、混合、深度模板等） |
| `bindings.json` | Step 4 | 每个 Draw Call 的资源绑定（Buffer、Texture、Sampler） |
| `shader_disasm.json` | Step 5 | Shader 对索引，关联 EID 列表 + 反汇编文件路径 |
| `resource_details.json` | Step 6 | 每个纹理/缓冲区的详细元数据（尺寸、格式、Mip 层级等） |
| `computed.json` | Step 7 | 计算分析结果：三角形分布、内存估算、管线去重、告警 |
| `_collection.json` | 结束 | 采集元数据：版本、耗时、错误统计 |
| `meshes.json` | Step 6.5a | 导出的 Mesh FBX 文件索引（`--export-assets` 时生成） |
| `exported_shaders.json` | Step 6.5a | 关联显著 Draw Call 的 Shader 对子集（`--export-assets` 时生成） |
| `textures.json` | Step 6.5c | 导出的纹理 PNG 文件索引（`--export-assets` 时生成） |

---

## 1. summary.json

**来源**: `collect_base()` 函数通过 `rdc-cli` 逐项采集基础数据。

**作用**: 整个管线的核心数据源。几乎所有后续步骤都从这里提取 Draw Call 列表、Pass 列表、资源列表等信息。`analyze.py` 的 Frame Overview、Hotspots 分析也依赖此文件。

```jsonc
{
  // ── 采集元信息 ──
  "_meta": {
    "capture": "E:/captures/scene.rdc",   // 源 .rdc 文件绝对路径
    "collected_at": "2026-04-18T22:15:30", // 采集时间
    "version": "1.2.0"                     // collect.py 版本
  },

  // ── 帧基本信息 (rdc info --json) ──
  "info": {
    "API": "Vulkan",                       // 图形 API: Vulkan / OpenGL ES / D3D11 等
    "machine_ident": "Qualcomm Adreno 740", // GPU 设备标识
    "Events": 3250,                        // 总事件数
    "Draw Calls": "428 (Indexed: 410)",    // Draw Call 统计文本
    "Clears": 15                           // Clear 操作次数
  },

  // ── GPU 计数器 (rdc stats --json) ──
  "stats": { /* rdc-cli 原始输出, 结构因 GPU 厂商而异 */ },

  // ── Pass 列表 (rdc passes --json) ──
  "passes": {
    "passes": [                            // 或直接为数组
      {
        "name": "DrawOpaqueObjects",       // Pass 名称 (来自 debug marker)
        "begin_eid": 100,                  // 起始事件 ID
        "end_eid": 450,                    // 结束事件 ID
        "draws": 85,                       // 包含的 Draw Call 数
        "dispatches": 0,                   // 包含的 Compute Dispatch 数
        "triangles": 125000                // 总三角形数
      }
      // ... 更多 Pass
    ]
  },

  // ── Pass 依赖 (rdc passes --deps --json) ──
  "pass_deps": {
    "edges": [                             // 显式依赖边 (部分 API 可用)
      {
        "src": "ShadowPass",              // 源 Pass 名称
        "dst": "DrawOpaqueObjects",       // 目标 Pass 名称
        "resources": [42, 43]             // 关联资源 ID
      }
    ],
    "per_pass": [                          // 每个 Pass 的读写资源 (部分 API 可用)
      {
        "name": "DrawOpaqueObjects",
        "reads": [42, 43],                // 读取的资源 ID 列表
        "writes": [10, 11, 12]            // 写入的资源 ID 列表
      }
    ]
  },

  // ── Draw Call 列表 (rdc draws --json) ──
  "draws": {
    "draws": [                             // 或直接为数组
      {
        "eid": 150,                        // 事件 ID (全局唯一)
        "type": "DrawIndexed",             // 类型: DrawIndexed / Draw / DrawInstanced 等
        "triangles": 5000,                 // 提交的三角形数
        "marker": "Opaque/Character",      // Debug marker 层级路径
        "pass": "DrawOpaqueObjects"        // 所属 Pass 名称
      }
      // ... 更多 Draw Call
    ]
  },

  // ── 事件列表 (rdc events --json) ──
  "events": [
    {
      "eid": 100,                          // 事件 ID
      "name": "DrawOpaqueObjects",         // 事件名称 (marker push/pop 或 API 调用)
      "type": "Other"                      // 事件类型
    }
    // ... 所有事件
  ],

  // ── 资源列表 (rdc resources --json) ──
  "resources": {
    "resources": [                         // 或直接为数组
      {
        "id": 42,                          // 资源 ID
        "name": "_CameraColorAttachmentA", // 资源名称
        "type": "Texture"                  // 类型: Texture / Buffer
      }
      // ... 更多资源
    ]
  },

  // ── 未使用的渲染目标 (rdc unused-targets --json) ──
  "unused_targets": { /* rdc-cli 原始输出 */ },

  // ── 验证日志 (rdc log --json) ──
  "log": {
    "messages": [                          // 或 "log" 键
      {
        "severity": "HIGH",               // 严重级别: HIGH / ERROR / WARNING / INFO
        "message": "Validation Error: ...", // 错误消息
        "eid": 150                         // 关联事件 ID (可选)
      }
    ]
  },

  // ── GPU 硬件计数器 (rdc counters --json) ──
  "counters": { /* rdc-cli 原始输出, 结构因 GPU 厂商而异 */ }
}
```

---

## 2. pass_details.json

**来源**: `collect_pass_details()` 对每个 Pass 调用 `rdc pass <index> --json`。

**作用**: 提供每个 Pass 的 Render Target 附件信息（颜色目标 + 深度目标的资源 ID、格式、尺寸）。这是带宽估算、Render Graph 构建、Pipeline 表格渲染的关键输入。

```jsonc
[
  {
    // ── Pass 基本信息 ──
    "name": "DrawOpaqueObjects",
    "draws": 85,
    "dispatches": 0,
    "triangles": 125000,
    "begin_eid": 100,
    "end_eid": 450,

    // ── 颜色目标列表 ──
    "color_targets": [
      {
        "id": 42,                          // 资源 ID (对应 resources 中的 id)
        "name": "_CameraColorAttachmentA", // 资源名称
        "format": "R16G16B16A16_SFLOAT",   // 像素格式
        "width": 2340,                     // 宽度 (像素)
        "height": 1080                     // 高度 (像素)
      }
      // ... 可能多个颜色附件 (MRT)
    ],

    // ── 深度/模板目标 (单个或 null) ──
    "depth_target": {
      "id": 43,
      "name": "_CameraDepthAttachment",
      "format": "D32_SFLOAT",
      "width": 2340,
      "height": 1080
    },

    // ── Load/Store 操作 (部分 API 可用, GLES 通常为空) ──
    "load_ops": [ /* ... */ ],             // 附件加载操作
    "store_ops": [ /* ... */ ]             // 附件存储操作
  }
  // ... 每个 Pass 一项
]
```

---

## 3. rt_usage.json

**来源**: `collect_rt_usage()` 对每个渲染目标资源调用 `_rpc_call("usage", {id})`, 并对每个子 Pass 的 Draw Call 调用 `_rpc_call("descriptors", {eid})`。

**作用**: 记录每个 Render Target 在帧内被哪些事件读写，以及每个子 Pass 绑定了哪些资源作为输入。这是 Render Graph 依赖边推断的核心数据。

```jsonc
{
  // ── 每个 RT 资源的使用记录 (键为资源 ID 字符串) ──
  "42": {
    "name": "_CameraColorAttachmentA",
    "entries": [
      {
        "eid": 100,                        // 使用该资源的事件 ID
        "usage": "ColorTarget"             // 使用类型:
                                           //   写入: RenderTarget / DepthStencil / StreamOut / Clear / Copy / ColorTarget / DepthStencilTarget
                                           //   读取: ShaderResource / Texture / InputAttachment 等
      },
      {
        "eid": 500,
        "usage": "ShaderResource"          // 在 EID 500 处被作为纹理读取
      }
      // ... 该资源的所有使用事件
    ]
  },
  // ... 更多资源

  // ── Descriptor 绑定映射 (特殊键) ──
  "_descriptors": {
    // 键 = 子 Pass 的 begin_eid, 值 = 该子 Pass 所有 Draw Call 绑定的资源 ID 集合
    "100": [42, 43, 55, 78],               // 从 EID 100 开始的子 Pass 绑定了这些资源
    "460": [10, 42, 90]
    // ... 每个子 Pass 一条
  }
}
```

---

## 4. pipelines.json

**来源**: 对每个 Draw Call 调用 `rdc pipeline <EID> --json`。

**作用**: 记录每个 Draw Call 时刻 GPU 管线的完整状态快照。`computed.py` 用它做管线状态去重分析（相同状态的 Draw Call 分组）。`analyze.py` 虽不直接读取此文件，但 computed.json 中的 `pipeline_dedup` 来源于此。

```jsonc
{
  // 键 = Draw Call 的 EID (字符串), 值 = rdc-cli 返回的管线状态
  "150": {
    // ── 光栅化状态 ──
    "rasterizer": {
      "cull_mode": "Back",                 // 剔除模式: None / Front / Back
      "front_ccw": true,                   // 正面是否为逆时针
      "depth_clamp": false,
      "polygon_mode": "Fill"               // Fill / Line / Point
    },

    // ── 深度/模板状态 ──
    "depth_stencil": {
      "depth_test_enable": true,
      "depth_write_enable": true,
      "depth_func": "LessEqual",           // 深度比较函数
      "stencil_enable": false
    },

    // ── 混合状态 ──
    "blend": {
      "targets": [
        {
          "blend_enable": false,
          "color_write_mask": "RGBA"        // 颜色写入掩码
        }
      ]
    },

    // ── Viewport / Scissor ──
    "viewport": { "x": 0, "y": 0, "width": 2340, "height": 1080 },
    "scissor": { "x": 0, "y": 0, "width": 2340, "height": 1080 }

    // ... 其他管线状态字段 (因 API 而异)
  }
  // ... 每个 Draw Call 一项
}
```

---

## 5. bindings.json

**来源**: 对每个 Draw Call 调用 `rdc bindings <EID> --json`。

**作用**: 记录每个 Draw Call 时刻绑定的所有 GPU 资源（Uniform Buffer、纹理、采样器等）。可用于分析 Draw Call 之间的资源共享关系、纹理使用热度等。

```jsonc
{
  // 键 = Draw Call 的 EID (字符串)
  "150": {
    // ── Vertex 阶段绑定 ──
    "vertex": {
      "uniform_buffers": [
        {
          "binding": 0,
          "resource_id": 100,              // 绑定的 Buffer 资源 ID
          "offset": 0,
          "size": 256                      // 绑定范围 (字节)
        }
      ],
      "textures": [],                      // VS 通常无纹理绑定
      "samplers": []
    },

    // ── Fragment/Pixel 阶段绑定 ──
    "fragment": {
      "uniform_buffers": [
        { "binding": 0, "resource_id": 101, "offset": 0, "size": 512 }
      ],
      "textures": [
        {
          "binding": 0,
          "resource_id": 55,               // 纹理资源 ID
          "name": "_MainTex"               // 纹理名称 (来自 Shader 反射)
        },
        { "binding": 1, "resource_id": 56, "name": "_NormalMap" }
      ],
      "samplers": [
        { "binding": 0, "filter": "Linear", "address_u": "Repeat" }
      ]
    }

    // ... 其他阶段 (Geometry, Compute 等, 因 API 而异)
  }
  // ... 每个 Draw Call 一项
}
```

---

## 6. shader_disasm.json

**来源**: `collect_shaders_disasm()` 通过 JSON-RPC 直连 daemon 获取 Shader 列表和反汇编。

**作用**: 索引所有唯一的 VS+PS Shader 对，记录每对被多少个 Draw Call 使用。`analyze.py` 的 Shader Complexity 分析模块解析关联的 `.shader` 反汇编文件，提取 SPIR-V Bound、纹理采样次数、UBO 大小等复杂度指标。

```jsonc
{
  // 键 = "VS_ID_PS_ID" 格式的 Shader 对标识
  "1024_2048": {
    "vs_id": 1024,                         // Vertex Shader 资源 ID
    "ps_id": 2048,                         // Pixel/Fragment Shader 资源 ID
    "eids": [150, 200, 205, 210, 350],     // 使用此 Shader 对的 Draw Call EID 列表
    "uses": 5,                             // 使用次数 (= eids 长度)
    "file": "shaders/shader_1024_2048.shader"  // 反汇编文件的相对路径
  },
  "1025_2049": {
    "vs_id": 1025,
    "ps_id": 2049,
    "eids": [155, 160],
    "uses": 2,
    "file": "shaders/shader_1025_2049.shader"
  }
  // ... 每个唯一 Shader 对一项
}
```

**关联文件**: `shaders/shader_{vs}_{ps}.shader` 文本文件包含 VS 和 PS 的 SPIR-V 反汇编内容。

---

## 7. resource_details.json

**来源**: `collect_resource_details()` 对每个 Texture/Buffer 通过 VFS 路径 (`rdc cat /textures/{id}/info` 或 `/buffers/{id}/info`) 获取详细信息。

**作用**: 提供纹理和缓冲区的完整元数据。`computed.py` 用于内存估算；`analyze.py` 用于内存分析章节的格式分布统计和最大资源排行。

```jsonc
{
  // 键 = 资源 ID (字符串)

  // ── 纹理资源 ──
  "42": {
    "id": 42,
    "name": "_CameraColorAttachmentA",
    "type": "Texture",                     // 资源类型
    "format": "R16G16B16A16_SFLOAT",       // 像素格式
    "width": 2340,                         // 宽度
    "height": 1080,                        // 高度
    "depth": 1,                            // 深度 (3D 纹理 > 1)
    "mips": 1,                             // Mipmap 层级数
    "array_size": 1                        // 纹理数组大小 (Cubemap = 6)
    // ... 其他字段因 API 而异
  },

  // ── 缓冲区资源 ──
  "100": {
    "id": 100,
    "name": "UnityPerMaterial",
    "type": "Buffer",
    "length": 65536,                       // 缓冲区大小 (字节), 也可能叫 "size"
    "usage": "UniformBuffer"               // 用途标记 (因 API 而异)
  }
  // ... 每个 Texture/Buffer 一项
}
```

---

## 8. computed.json

**来源**: `compute_analysis()` 函数读取 summary、pass_details、pipelines、resource_details 四项数据进行离线计算。

**作用**: 汇总分析结果，为 `analyze.py` 报告生成提供预计算数据。包含三角形分布、内存估算、管线状态去重、告警列表等。

```jsonc
{
  // ── 三角形分布 ──
  "triangle_distribution": {
    "total": 580000,                       // 全帧总三角形数
    "per_pass": [                          // 按 Pass 分组 (降序排列)
      {
        "name": "DrawOpaqueObjects",       // Pass 名称
        "triangles": 350000,               // 该 Pass 的三角形总数
        "percent": 60.3                    // 占比 (%)
      },
      { "name": "ShadowPass", "triangles": 125000, "percent": 21.6 }
      // ...
    ]
  },

  // ── Draw Call 类型分布 ──
  "draw_type_distribution": {
    "DrawIndexed": 410,                    // 索引绘制次数
    "Draw": 18                             // 非索引绘制次数
  },

  // ── 内存估算 ──
  "memory_estimate": {
    "total_textures_mb": 256.5,            // 纹理总内存 (MB)
    "total_buffers_mb": 12.3,              // 缓冲区总内存 (MB)
    "largest_resources": [                 // 最大资源排行 (前 20)
      {
        "id": 55,                          // 资源 ID
        "name": "_MainLightShadowmap",     // 资源名称
        "type": "Texture",                 // 类型
        "size_mb": 16.0                    // 估算大小 (MB)
      }
      // ...
    ]
  },

  // ── 对称 Pass 检测 (VR 双眼渲染) ──
  "symmetric_passes": {
    "detected": false,                     // 是否检测到对称模式
    "groups": [                            // 对称分组 (detected=true 时有值)
      {
        "passes_a": [0, 1, 2],             // 前半段 Pass 索引
        "passes_b": [3, 4, 5],             // 后半段 Pass 索引
        "similarity": 0.95                 // 签名相似度 (0~1)
      }
    ]
  },

  // ── 管线状态去重 ──
  "pipeline_dedup": {
    "unique_states": 28,                   // 唯一管线状态数
    "total_draws": 428,                    // 总 Draw Call 数
    "state_groups": [                      // 按使用次数降序
      {
        "hash": "a1b2c3d4e5f6",           // 管线状态内容哈希 (MD5 前 12 位)
        "count": 85,                       // 共享此状态的 Draw Call 数
        "eids": [150, 155, 160]            // 代表性 EID (最多 10 个)
      }
      // ... 最多 30 组
    ]
  },

  // ── 告警列表 ──
  "alerts": [
    // 高三角形 Draw Call 告警
    {
      "severity": "warning",
      "type": "high_triangle_draw",        // 告警类型
      "eid": 150,                          // 关联 EID
      "triangles": 50000,                  // 三角形数
      "pass": "DrawOpaqueObjects"          // 所属 Pass
    },
    // 空 Pass 告警
    {
      "severity": "info",
      "type": "empty_pass",
      "pass": "UnusedPass"
    },
    // 大资源告警 (>4 MB)
    {
      "severity": "warning",
      "type": "large_resource",
      "id": 55,
      "name": "_MainLightShadowmap",
      "size_mb": 16.0
    },
    // 验证错误 (来自 log)
    {
      "severity": "error",
      "type": "validation_error",
      "message": "Validation Error: ...",
      "eid": 150
    }
  ]
}
```

---

## 9. _collection.json

**来源**: `collect.py` 在采集完成后生成。

**作用**: 采集过程的元数据记录。用于排查采集问题、统计耗时、判断数据完整性。

```jsonc
{
  "version": "1.2.0",                      // collect.py 版本号
  "capture": "E:/captures/scene.rdc",      // 源文件路径
  "workers": 4,                            // 使用的 worker 数
  "parallelized": true,                    // 是否启用了并行采集
  "started_at": "2026-04-18T22:15:30",     // 开始时间
  "completed_at": "2026-04-18T22:18:45",   // 结束时间
  "total_seconds": 195.2,                  // 总耗时 (秒)
  "timings": {                             // 各阶段耗时 (秒)
    "base": 8.5,
    "pass_details": 12.3,
    "rt_usage": 25.1,
    "worker_open": 6.2,
    "per_draw": 45.0,
    "shader_disasm": 68.4,
    "resource_details": 18.7,
    "computed": 0.3,
    "render_graph": 0.1
  },
  "error_count": 3,                        // 总错误数
  "errors": [                              // 错误详情 (最多 100 条)
    {
      "phase": "resource_details",         // 出错阶段
      "id": 99,                            // 关联资源/EID
      "error": "vfs failed"               // 错误描述
    }
  ]
}
```

---

## 10. meshes.json (--export-assets)

**来源**: `collect_meshes()` / `_collect_meshes_shard()` 导出每个 Draw Call 的顶点数据为 FBX 文件。

**作用**: 导出的 Mesh FBX 文件索引。记录每个 Draw Call 对应的 FBX 文件路径、顶点数、属性列表。支持内容去重（相同几何体只保留一份文件）。

```jsonc
{
  // 键 = Draw Call 的 EID (字符串)
  "150": {
    "file": "meshes/mesh_150.fbx",         // FBX 文件相对路径
    "vertex_count": 12500,                 // 顶点数
    "attributes": ["POSITION", "NORMAL", "UV", "TANGENT"],  // 导出的顶点属性
    "size_bytes": 1048576,                 // FBX 文件大小 (字节)
    "_eid": 150                            // 原始 EID (内部使用)
  },
  // 去重的 Mesh 指向原始文件
  "200": {
    "file": "meshes/mesh_150.fbx",         // 复用 EID 150 的文件
    "vertex_count": 12500,
    "attributes": ["POSITION", "NORMAL", "UV", "TANGENT"],
    "size_bytes": 1048576,
    "_eid": 200,
    "dedup_of": 150                        // 标记为 EID 150 的副本
  }
  // ...
}
```

**过滤规则**: 顶点数 < 300 的 Draw Call 会被跳过 (`MIN_VERTEX_COUNT`)。

---

## 11. exported_shaders.json (--export-assets)

**来源**: `filter_shader_disasm()` 从 `shader_disasm.json` 中过滤出与"显著 Draw Call"（即成功导出 Mesh 的 Draw Call）关联的 Shader 对。

**作用**: 仅保留有对应 Mesh 导出的 Shader 子集，减少需要分析的 Shader 数量。结构与 `shader_disasm.json` 完全相同。

```jsonc
{
  // 结构同 shader_disasm.json, 但仅包含 eids 中有 significant_eids 交集的条目
  "1024_2048": {
    "vs_id": 1024,
    "ps_id": 2048,
    "eids": [150, 200, 205, 210, 350],
    "uses": 5,
    "file": "shaders/shader_1024_2048.shader"
  }
}
```

---

## 12. textures.json (--export-assets)

**来源**: `collect_textures()` / `_collect_textures_shard()` 将纹理资源导出为 PNG 文件。

**作用**: 导出的纹理 PNG 文件索引。仅导出被"显著 Draw Call"通过 Descriptor 绑定的纹理（而非全部纹理），减少导出量。

```jsonc
{
  // 键 = 纹理资源 ID (字符串)
  "55": {
    "file": "textures/tex_55.png",         // PNG 文件相对路径
    "name": "_MainTex",                    // 纹理名称
    "size_bytes": 524288                   // PNG 文件大小 (字节)
  },
  "56": {
    "file": "textures/tex_56.png",
    "name": "_NormalMap",
    "size_bytes": 262144
  }
  // ...
}
```

---

## 数据流向关系

```
summary.json ──┬──→ pass_details.json ──→ rt_usage.json
               │                              │
               ├──→ pipelines.json ────────────┤
               │                              │
               ├──→ bindings.json              │
               │                              │
               ├──→ shader_disasm.json         │
               │                              │
               ├──→ resource_details.json      │
               │         │                    │
               │         ▼                    ▼
               └──→ computed.json ──→ analyze.py ──→ performance_report.html
                                         │
                    rt_usage.json ────────┘──→ render_graph.html
```

| 消费者 | 输入文件 |
|--------|----------|
| `compute_analysis()` | summary, pass_details, pipelines, resource_details |
| `generate_render_graph_html()` | summary, pass_details, rt_usage |
| `analyze.py` (报告生成) | summary, pass_details, computed, shader_disasm, resource_details, pipelines, bindings |
