# 可视化 + 导出层 API

HTML 报告 (`analyze.py`)、TSV 导出 (`tsv_export.py`)、渲染依赖图 (`render_graph.py`)、资源导出 (`export_assets.py`, `fbx_writer.py`) 的详细文档。

---

## analyze.py — HTML 性能报告生成

### 命令行

```
python\python.exe Scripts\rdc\analyze.py <analysis-dir>
```

### 输出

`{analysis-dir}/performance_report.html` — 包含 8 个可折叠章节的交互式性能报告。

### 分析模块

每个模块独立运行，返回一个 dict 供 HTML 模板渲染：

#### `analyze_frame_overview(summary, computed) → dict`

帧级概要：API、分辨率、事件数、Draw Call 数、总三角形、Draw 类型分布、渲染架构检测（Forward / Deferred，基于 GBuffer Pass 是否存在）。

| 返回键 | 类型 | 说明 |
|--------|------|------|
| `api` | str | 图形 API (Vulkan / OpenGL ES 等) |
| `platform` | str | GPU 设备标识 |
| `resolution` | str | 渲染分辨率 |
| `events` | int | 总事件数 |
| `draw_calls` | int | Draw Call 统计 |
| `total_triangles` | int | 总三角形数 |
| `draw_types` | dict | 各 Draw 类型计数 |
| `pass_count` | int | Pass 数量 |
| `architecture` | str | 渲染架构 (Forward / Deferred) |

#### `analyze_pipeline(summary, pass_details) → dict`

Pass 分类与时间线数据。每个 Pass 标记类别（Shadow / DepthPrepass / GBuffer / Geometry / PostProcess 等），生成 Gantt 图和 Pipeline 表格数据。

**Pass 分类颜色**:

| 类别 | 色值 | 触发关键词 |
|------|------|------------|
| Shadow | `#6366f1` | shadow |
| DepthPrepass | `#8b5cf6` | prepass, depth pre |
| GBuffer | `#3b82f6` | gbuffer |
| Geometry | `#22b07a` | opaque, geometry |
| Hair | `#f59e0b` | hair |
| Transparent | `#06b6d4` | transparent, translucent |
| PostProcess | `#d4a017` | post, bloom, fxaa, motion |
| Present | `#64748b` | present, blit |
| Other | `#555f78` | 未匹配时的默认类别 |

#### `analyze_pipeline_stages(data) → dict`

管线阶段自动分类。采用两级启发式策略：

1. **名称关键词匹配**（引擎命名的 Pass，如 "ShadowPass"、"GBuffer"）
2. **元数据启发式**（RenderDoc 自动生成名称如 "Colour Pass #1" 时 fallback）

元数据分类规则（按优先级）:

| 规则 | 判定条件 | 阶段 |
|------|----------|------|
| Compute | dispatches > 0, draws == 0 | Compute |
| ShadowMap | depth-only + D16/D32 + 2^n 尺寸 | ShadowMap |
| DepthPrepass | depth-only + 非 2^n 尺寸 | DepthPrepass |
| Bloom | `detect_bloom_chain()` 识别的连续 pass | Bloom |
| UI | SRGB + Clear + draws ≥ 3 + avg tri < 500 | UI |
| Compositing | 写入最大 UNORM RT + tri ≤ 2 | Compositing |
| PostProcess | 单全屏 quad (tri ≤ 2) | PostProcess |
| MainColor | color+depth + Clear + draws ≥ 5 | MainColor |

附加检测:

- **Bloom 金字塔检测** (`detect_bloom_chain()`): 扫描连续同格式 pass，检测 ½ 降采样 / ×2 升采样 / 回到原始分辨率的序列
- **全屏 Quad 检测** (`detect_fullscreen_quad()`): 所有 draw 的 tri ≤ 2 且 PS invocations ≈ RT 像素数
- **GPU 时间聚合**: 从 counters 的 `GPU Duration` 按 EID 范围归属 pass 后求和

| 返回键 | 类型 | 说明 |
|--------|------|------|
| `stages` | list[dict] | 每个 pass 的分类结果 (stage, reason, gpu_time_us, is_fullscreen 等) |
| `stage_groups` | list[dict] | 按 stage 分组汇总 (gpu_time_us, pct) |
| `bloom_chain` | dict\|None | Bloom 金字塔检测结果 (levels, resolutions, directions) |
| `total_gpu_time_us` | float | 全帧 GPU 时间 |

#### `analyze_hotspots(summary, pass_details) → dict`

三角形热点分析：

| 返回键 | 说明 |
|--------|------|
| `top_draws` | 三角形数最多的前 15 个 Draw Call |
| `per_pass` | 每个 Pass 的三角形分布 |
| `repeated_meshes` | 检测跨 Pass 重复提交的 Mesh（相同 marker 在 3+ Pass 出现 且 >5000 tri） |

#### `analyze_bandwidth(summary, pass_details) → dict`

帧级带宽估算：

- 逐 Pass 估算 load + store 字节数（基于 RT 格式 / 尺寸 / BPP）
- 特别追踪 Bloom 相关 Pass 的带宽
- 结果包含 `total_mb` 和 `bloom_mb`

#### `analyze_shaders(data) → dict`

Shader 综合分析：

- 解析 `.shader` 反汇编文件，提取 SPIR-V Bound、纹理采样次数、UBO 大小
- 按影响度排序（`uses × spirv_bound`）
- 每个 Shader 附加指令分布 (`instructions`) 和寄存器压力 (`register_pressure`)
- Shader 变体去重 (`variants`): 基于规范化内容 hash 分组，提取 SpecId 差异
- Shader → Pass 使用矩阵 (`shader_pass_matrix`): 统计每个 Shader 在各 Pass 中的 draw 次数

| 返回键 | 类型 | 说明 |
|--------|------|------|
| `shaders` | list[dict] | 每个 Shader 的详细分析（含 instructions, register_pressure, variant_count） |
| `total_unique` | int | 唯一 Shader 对总数 |
| `total_compute` | int | Compute Shader 数量 |
| `variants` | dict | 变体去重结果（groups, unique_shaders, variant_groups） |
| `shader_pass_matrix` | dict | Shader × Pass 使用热力图数据 |

#### `analyze_memory(summary, resource_details) → dict`

内存分析：

- 纹理总内存 / 缓冲区总内存
- 最大资源排行
- 格式分布统计

#### `generate_suggestions(...) → list[dict]`

综合所有分析结果生成优化建议卡片。

| 条件 | 严重度 | 建议 |
|------|--------|------|
| 同一 Mesh 在 3+ Pass 出现且 >5000 tri | warning | 考虑 GPU Instancing / Mesh 合批 |
| Bloom >8 subpass | warning | 考虑降低 Bloom 迭代次数 |
| UnityPerMaterial UBO >400 bytes 且 >5 uses | warning | 考虑精简材质参数 |
| 单纹理 >8 MB | warning | 考虑降低分辨率或压缩 |
| 单 Draw >10,000 tri | warning | 考虑 LOD 或裁剪 |
| 全帧 BW >100 MB | warning | 考虑减少 RT 格式精度 |
| 无 load/store ops 数据 | info | GLES 限制，无法精确带宽分析 |

### HTML 报告结构

报告包含 8 个可折叠章节：

1. **Frame Overview** — 信息卡片（API、分辨率、Draw Call、三角形等）
2. **Rendering Pipeline** — Gantt 时间线图 + Pass 详情表
3. **Pipeline Stage Analysis** — GPU 时间按阶段分布条形图 + Bloom 金字塔可视化 + Pass 分类表（stage / reason / GPU time / fullscreen 标记）
4. **Triangle Hotspots** — 热点条形图 + 重复 Mesh 表
5. **Bandwidth Estimation** — 逐 Pass 带宽条形图
6. **Shader Complexity** — Shader 对排行表格（含寄存器压力标签、变体数）+ Instruction Mix 堆叠条形图 + Shader Variants 去重展示 + Shader Usage Heatmap（Shader × Pass 使用矩阵）
7. **Memory** — 资源大小排行 + 格式分布
8. **Optimization Suggestions** — 带严重度图标的建议卡片

CSS 从 `assets/rdc-common.css` 加载，通过 `__ASSETS__` 占位符替换为相对路径。

---

## tsv_export.py — TSV 表生成

被 `collect.py` 在 Step 7.5 调用，将采集数据导出为 token 高效的 TSV 格式（供 LLM/AI 分析）。

### 主函数

#### `export_tsv(tsv_dir, summary, pass_details, pipelines, bindings, resource_details, shader_disasm, computed, shaders_dir=None) → None`

导出最多 16 张 TSV 表到 `tsv/` 目录。`shaders_dir` 非空时额外生成 shader 分析表。

### 输出文件

| 文件 | 说明 |
|------|------|
| `frame_info.tsv` | 帧概要键值对 |
| `passes.tsv` | Pass 概览 (name, eid, draws, RT formats, load/store ops) |
| `draws.tsv` | 逐 Draw Call (eid, type, triangles, pipeline IDs) |
| `bindings.tsv` | 逐绑定 (eid, stage, kind, set, slot, name) |
| `resources.tsv` | 所有纹理+缓冲区 (id, format, size) |
| `shaders.tsv` | Shader 对 (vs/ps/cs IDs, uses, eids) |
| `counters.tsv` | GPU 硬件计数器 (eid, counter, value, unit) |
| `events.tsv` | 事件列表 (eid, type, name) |
| `deps.tsv` | Pass 依赖边 (src, dst, resources) |
| `pass_rw.tsv` | Pass 读写资源 (pass, reads, writes) |
| `alerts.tsv` | 告警 (severity, type, eid, detail) |
| `pipeline_stages.tsv` | 管线阶段分类 (pass, stage, reason, gpu_time, fullscreen) |
| `stage_summary.tsv` | 阶段汇总 (stage, passes, gpu_time_us, pct) |
| `draw_timing.tsv` | 逐 Draw GPU 耗时排行 (eid, gpu_duration_us, ps/vs_invocations) |
| `shader_instructions.tsv` | 每 Shader 指令分布 (arithmetic/sample/logic/...) + 寄存器压力 |
| `shader_variants.tsv` | Shader 变体去重组 (group_key, variant_key, spec_ids, uses) |
| `shader_pass_matrix.tsv` | Shader × Pass 使用矩阵（列名=Pass 名，值=draw count） |

### 格式约定

- Tab 分隔，UTF-8 编码
- 数组值逗号分隔（如 `eids: "1,2,3"`）
- None → 空字符串，bool → `"1"/"0"`
- 复杂值用 compact JSON

---

## render_graph.py — 渲染依赖图可视化

生成交互式 HTML 渲染依赖图，展示 Pass 间的数据流。

### 核心流程

1. **子 Pass 提取** → 2. **节点构建** → 3. **依赖边推断** → 4. **HTML 生成**

### 函数

#### `generate_render_graph_html(summary, pass_details, resource_names, rt_usage, assets_rel) → str`

主入口。返回完整 HTML 字符串。

#### `_extract_subpasses(summary, pass_details) → list[dict]`

从事件标记层级中提取细粒度子 Pass：

- 解析 `glPopDebugGroup` / `vkCmdEndDebugUtilsLabelEXT` 事件
- 过滤噪声模式（`GUI.Repaint`、`UIR.DrawChain` 等）
- 检测叶子 Pass（内部无子标记）
- 同名子 Pass 追加数字后缀去重

#### `_build_dependency_edges(subpasses, nodes, summary, rt_usage) → list[dict]`

五级回退策略推断依赖边：

| 优先级 | 策略 | 数据源 | 说明 |
|--------|------|--------|------|
| 1 | 显式依赖 | `pass_deps.edges` | 直接使用 rdc-cli 报告的依赖边 |
| 2 | 读写匹配 | `pass_deps.per_pass` | 一个 Pass 的 writes ∩ 另一个 Pass 的 reads |
| 3 | RT 使用事件 | `rt_usage` (不含 `_descriptors`) | 按 EID 排序追踪 RT 从写入到读取的流向 |
| C | RT 名称相似度 | 节点 RT 名称 | Token 化匹配（如 `CameraColor` → `CameraColorA`） |
| D | 共享 RT | 节点 RT ID | 共享 RT 资源 ID 的 Pass 间建边（最后兜底） |

子策略 C 和 D 在策略 3 之后作为补充运行。Descriptor 绑定（`_descriptors` 键）也参与匹配。

**同一 coarse pass 内的子 Pass 按顺序自动串联**。

#### `_short_rt_name(name: str) → str`

缩短资源名用于显示:

```
"_CameraColorAttachmentA_2340x1080_R16G16B16A16_SFLOAT" → "CameraColorA"
```

#### `_tokenize_rt_name(name: str) → set[str]`

将 RT 名称拆分为语义 token 集合，用于相似度匹配。

### 输出数据格式

HTML 模板内嵌 JSON:

```json
{
  "nodes": [
    {
      "id": 0,
      "name": "DrawOpaqueObjects",
      "triangles": 125000,
      "draws": 85,
      "color_targets": [{"name": "CameraColor", "format": "R16G16B16A16_SFLOAT"}],
      "depth_target": {"name": "DepthAttachment", "format": "D32_SFLOAT"}
    }
  ],
  "edges": [
    {"src": 0, "dst": 1, "type": "rt_flow", "label": "CameraColor"}
  ]
}
```

HTML 模板位于 `assets/render_graph_template.html`。

---

## export_assets.py — Mesh 与纹理导出

`--export-assets` 选项启用时执行。导出 Draw Call 的几何数据（FBX）和关联纹理（PNG）。

### Mesh 导出

#### `collect_meshes(draw_eids, out_dir, errors, session=None) → tuple[dict, set[int]]`

批量导出 Mesh：

1. 对每个 Draw Call 调用 `_export_one_mesh()`
2. 导出后执行 `_dedup_meshes()` 去重
3. 返回 `(results_dict, significant_eids)`

`significant_eids` 是成功导出的 Draw Call EID 集合，后续用于过滤纹理和 Shader。

#### `_export_one_mesh(eid, meshes_dir, errors, session=None) → dict | None`

单个 Draw Call 的 Mesh 导出:

1. `rdc mesh <EID>` 获取 mesh_info（顶点数、索引）
2. `rdc cat /draws/<EID>/vbuffer` 获取顶点缓冲区数据
3. `_parse_vbuffer()` 解析属性并推断语义
4. `_expand_by_indices()` 按索引展开
5. `write_fbx()` 写入 ASCII FBX

**过滤**: 顶点数 < 300 (`MIN_VERTEX_COUNT`) 的 Draw Call 被跳过。

#### `_infer_semantic(raw_attrs: dict) → dict[str, str]`

属性语义推断：

| 模式 | 推断为 |
|------|--------|
| `POSITION`, `in_POSITION` | POSITION |
| `NORMAL`, `in_NORMAL` | NORMAL |
| `TEXCOORD0`, `in_TEXCOORD` | UV |
| `TEXCOORD1` | UV2 |
| `COLOR0`, `in_COLOR` | COLOR |
| `TANGENT` | TANGENT |
| 3 分量 float（名称含 norm/nml） | NORMAL |
| 2 分量 float（名称含 tex/uv） | UV |

#### `_dedup_meshes(results, meshes_dir) → int`

内容去重：对每个 FBX 文件计算 MD5 哈希，相同内容的文件只保留一份，重复项指向原始文件并标记 `dedup_of`。

### 纹理导出

#### `collect_textures(summary, out_dir, errors, session=None, resource_ids=None) → dict`

导出纹理 PNG:
- 调用 `rdc texture <RID> -o tex_{id}.png`
- 可通过 `resource_ids` 限定只导出与 significant draw 关联的纹理

#### `collect_draw_texture_ids(significant_eids, errors, session=None) → set[int]`

查询 Descriptor 绑定获取关联纹理 ID：对 significant draw 调用 `_rpc_call("descriptors", {eid})`，收集所有绑定的纹理资源 ID。

### Shader 过滤

#### `filter_shader_disasm(shader_disasm, significant_eids) → dict`

从完整 `shader_disasm` 中过滤出与 significant draw 关联的 Shader 对子集。

### 并行 Shard 函数

| 函数 | 说明 |
|------|------|
| `_collect_meshes_shard(session, eid_shard, meshes_dir, progress, errors)` | Worker: 导出一组 Draw Call 的 Mesh |
| `_collect_textures_shard(session, resource_tasks, tex_dir, progress, errors)` | Worker: 导出一组纹理 |

---

## fbx_writer.py — ASCII FBX 序列化

将顶点数据写入 ASCII FBX 7.3 格式文件，兼容主流建模工具。

### 函数

#### `write_fbx(path: str | Path, model_name: str, data: dict) → None`

| 参数 | 说明 |
|------|------|
| `path` | 输出 FBX 文件路径 |
| `model_name` | 模型节点名称 |
| `data` | 顶点数据字典 |

**`data` 字典键**:

| 键 | 类型 | 映射方式 | 说明 |
|----|------|----------|------|
| `IDX` | `list[int]` | — | 三角形索引（flat list） |
| `POSITION` | `list[list[float]]` | per-unique-vertex | 顶点位置 [x, y, z] |
| `NORMAL` | `list[list[float]]` | per-polygon-vertex | 法线 |
| `TANGENT` | `list[list[float]]` | per-polygon-vertex | 切线 |
| `COLOR` | `list[list[float]]` | per-polygon-vertex | 顶点色 [r, g, b, a] |
| `UV` | `list[list[float]]` | per-unique-vertex | 纹理坐标 [u, v] |
| `UV2` | `list[list[float]]` | per-unique-vertex | 第二套 UV |

**FBX 特殊处理**:
- 每 3 个索引末尾做 XOR `-1` 标记三角形结束
- UV 的 V 分量翻转（`1 - v`）
- per-unique-vertex 数据自动展开为 per-polygon-vertex
