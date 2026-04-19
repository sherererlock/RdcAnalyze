# Scripts/rdc 模块文档

本文档描述 `Scripts/rdc/` 下各 Python 模块的职责、公开 API 和关键设计，以及 `Scripts/rdc-report.bat` 批处理入口。

---

## 目录

- [模块总览](#模块总览)
- [rdc-report.bat — 一键管线入口](#rdc-reportbat--一键管线入口)
- [collect.py — 数据采集主控](#collectpy--数据采集主控)
- [analyze.py — HTML 性能报告生成](#analyzepy--html-性能报告生成)
- [shared.py — 公共工具函数](#sharedpy--公共工具函数)
- [rpc.py — RenderDoc 通信层](#rpcpy--renderdoc-通信层)
- [workers.py — 数据采集与并行基础设施](#workerspy--数据采集与并行基础设施)
- [computed.py — 离线计算分析](#computedpy--离线计算分析)
- [render_graph.py — 渲染依赖图可视化](#render_graphpy--渲染依赖图可视化)
- [export_assets.py — Mesh 与纹理导出](#export_assetspy--mesh-与纹理导出)
- [fbx_writer.py — ASCII FBX 序列化](#fbx_writerpy--ascii-fbx-序列化)

---

## 模块总览

```
Scripts/
├── rdc-report.bat          # 一键管线: collect → analyze
└── rdc/
    ├── collect.py          # 主控: 解析参数, 编排采集步骤, 输出 JSON
    ├── analyze.py          # 读取 JSON, 生成 performance_report.html
    ├── shared.py           # BPP 表, 格式化, JSON I/O
    ├── rpc.py              # rdc-cli 子进程调用 + JSON-RPC 直连
    ├── workers.py          # 数据采集函数 + WorkerPool 并行框架
    ├── computed.py         # 三角形分布 / 内存估算 / 管线去重 / 告警
    ├── render_graph.py     # 子 Pass 提取, 依赖边推断, 交互式 HTML 图
    ├── export_assets.py    # FBX Mesh 导出 + PNG 纹理导出
    └── fbx_writer.py       # write_fbx() — ASCII FBX 7.3 写入
```

**分层架构**:

| 层 | 模块 | 职责 |
|----|------|------|
| 通信 | `rpc.py` | 封装 rdc-cli 子进程调用和 JSON-RPC socket 直连 |
| 公共 | `shared.py` | 格式化、BPP 估算、JSON I/O |
| 采集 | `workers.py` | 各阶段采集函数 + 并行 Worker 框架 |
| 计算 | `computed.py` | 离线分析算法 (无 I/O) |
| 可视化 | `analyze.py`, `render_graph.py` | HTML 报告 / 依赖图生成 |
| 导出 | `export_assets.py`, `fbx_writer.py` | Mesh FBX + 纹理 PNG 导出 |
| 主控 | `collect.py` | 编排全流程 |

---

## rdc-report.bat — 一键管线入口

一条命令完成「采集 + 报告」两阶段。

**用法**:
```bat
Scripts\rdc-report.bat <capture.rdc> [-j WORKERS]
```

**行为**:
1. 自动定位 Python（优先 `../python/python.exe`，其次系统 PATH）
2. 校验 `.rdc` 文件存在
3. 推导输出目录 `{stem}-analysis`
4. 调用 `collect.py`（默认 `-j 8`）
5. 调用 `analyze.py`
6. 打印报告路径

任一阶段失败则 `exit /b 1`。

---

## collect.py — 数据采集主控

**版本**: 1.2.0

### 命令行

```
python\python.exe Scripts\rdc\collect.py <capture.rdc> [-j WORKERS] [--export-assets]
```

| 参数 | 默认 | 说明 |
|------|------|------|
| `capture` | 必填 | `.rdc` 捕获文件路径 |
| `-j, --workers` | 1 | 并行 worker 数 (1–8) |
| `--export-assets` | 否 | 导出 Mesh FBX + 纹理 PNG |

### 输出目录

`{capture-stem}-analysis/` 下生成以下文件（详细结构见 [json-schema.md](json-schema.md)）:

| 文件 | 采集步骤 | 说明 |
|------|----------|------|
| `summary.json` | Step 2 | 帧概要 (info/passes/draws/resources 等) |
| `pass_details.json` | Step 3 | 每个 Pass 的 RT 附件 |
| `rt_usage.json` | Step 4 | RT 使用事件 + Descriptor 绑定 |
| `pipelines.json` | Step 5 | 每个 Draw Call 的管线状态 |
| `bindings.json` | Step 5 | 每个 Draw Call 的资源绑定 |
| `shader_disasm.json` | Step 6 | Shader 对索引 + 反汇编文件路径 |
| `resource_details.json` | Step 7 | 纹理/缓冲区详细元数据 |
| `computed.json` | Step 9 | 计算分析结果 |
| `render_graph.html` | Step 10 | 交互式渲染依赖图 |
| `_collection.json` | 结束 | 采集元数据 (版本/耗时/错误) |

可选 `--export-assets`:

| 文件 | 说明 |
|------|------|
| `meshes/mesh_{eid}.fbx` | 导出的 Mesh 几何体 |
| `meshes.json` | Mesh 文件索引 |
| `textures/tex_{id}.png` | 导出的纹理图像 |
| `textures.json` | 纹理文件索引 |
| `exported_shaders.json` | 关联 Shader 子集 |

### 采集流程 (10 步)

```
Step 1   打开 .rdc         → rdc open
Step 2   基础数据           → collect_base()
Step 3   Pass 详情          → collect_pass_details()
Step 4   RT 使用            → collect_rt_usage()
Step 5   管线 + 绑定        → collect_per_draw() 或并行 shard
Step 6   Shader 反汇编      → collect_shaders_disasm()
Step 7   资源详情           → collect_resource_details() 或并行 shard
Step 8   资源导出 (可选)     → collect_meshes() + collect_textures()
Step 9   计算分析           → compute_analysis()
Step 10  渲染图             → generate_render_graph_html()
```

**并行模式** (`-j > 1`): 使用 `WorkerPool` 开启 N 个独立 daemon 会话，Step 5/7/8 按 shard 分发到 worker。Shader 反汇编始终在主会话上执行（cache 不跨会话）。

**信号处理**: 捕获 `SIGINT`，安全关闭所有 worker 会话后退出。

---

## analyze.py — HTML 性能报告生成

### 命令行

```
python\python.exe Scripts\rdc\analyze.py <analysis-dir>
```

### 输出

`{analysis-dir}/performance_report.html` — 包含 7 个可折叠章节的交互式性能报告。

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

#### `analyze_shaders(shader_disasm) → dict`

Shader 复杂度排名：

- 解析 `.shader` 反汇编文件，提取 SPIR-V Bound、纹理采样次数、UBO 大小
- 按影响度排序（`uses × spirv_bound`）
- 返回 `shaders[]` 和 `total_unique` 统计

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

报告包含 7 个可折叠章节：

1. **Frame Overview** — 信息卡片（API、分辨率、Draw Call、三角形等）
2. **Rendering Pipeline** — Gantt 时间线图 + Pass 详情表
3. **Triangle Hotspots** — 热点条形图 + 重复 Mesh 表
4. **Bandwidth Estimation** — 逐 Pass 带宽条形图
5. **Shader Complexity** — Shader 对排行表格
6. **Memory** — 资源大小排行 + 格式分布
7. **Optimization Suggestions** — 带严重度图标的建议卡片

CSS 从 `assets/rdc-common.css` 加载，通过 `__ASSETS__` 占位符替换为相对路径。

---

## shared.py — 公共工具函数

被所有模块引用的基础设施函数。

### 函数

#### `guess_bpp(fmt_str: str) → float`

从 GPU 格式字符串估算每像素位数。

- 优先查 `BPP_TABLE`（57 条预定义格式）
- 未命中则按正则启发推断
- 兜底返回 `32.0`

```python
guess_bpp("R16G16B16A16_SFLOAT")  # → 64.0
guess_bpp("BC3_UNORM")            # → 8.0
guess_bpp("R8G8B8A8_UNORM")      # → 32.0
```

#### `unwrap(obj, *keys)`

解包 rdc-cli 返回的嵌套 JSON。若 `obj` 是 dict 且含 `keys` 中的某个键，则取出对应值。

```python
unwrap({"draws": [...]}, "draws")  # → [...]
unwrap([...], "draws")             # → [...]（已经是数组则直接返回）
```

#### `write_json(path: Path, data: object)`

UTF-8 写入带缩进的 JSON 文件。

#### `fmt_number(n: int | float) → str`

千分位格式化数字。

```python
fmt_number(1234567)  # → "1,234,567"
```

#### `fmt_mb(mb: float) → str`

格式化内存大小，自动选择 MB/KB 单位。

```python
fmt_mb(1.5)    # → "1.5 MB"
fmt_mb(0.3)    # → "307 KB"
```

#### `estimate_texture_mb(res: dict) → float`

从纹理元数据（width, height, depth, mips, array_size, format）估算内存占用。

#### `rt_bytes(target: dict) → float`

估算单个 Render Target 一次读写的字节数（`width × height × bpp / 8`）。

### 常量

- **`BPP_TABLE`**: 57 条 GPU 格式 → BPP 映射表，覆盖 RGBA、BC 压缩、ASTC、D/S 等格式。

---

## rpc.py — RenderDoc 通信层

封装与 rdc-cli daemon 的两种通信方式。

### 函数

#### `run_rdc(*args, session=None, timeout=120) → tuple[str, str, int]`

通过子进程调用 `rdc.bat`。

| 参数 | 说明 |
|------|------|
| `*args` | rdc-cli 命令参数（如 `"open"`, `"capture.rdc"`） |
| `session` | 会话名称（`--session NAME`），None 使用默认会话 |
| `timeout` | 超时秒数 |

返回 `(stdout, stderr, returncode)`。

#### `run_rdc_json(*args, session=None, timeout=120) → object`

`run_rdc()` 的 JSON 解析封装。stdout 解析为 Python 对象后返回。

#### `_rpc_call(session, method, params=None, timeout=30) → object`

JSON-RPC socket 直连 daemon，绕过 CLI 的超时限制。用于长耗时操作（shader cache 构建可达 15 分钟）。

**工作方式**:
1. 读取 `%LOCALAPPDATA%/rdc/sessions/{session}.json` 获取 host/port/token
2. 建立 TCP socket 连接
3. 发送 `{"method", "params", "_token"}` JSON-RPC 请求
4. 接收并解析响应

### 辅助类

#### `Progress`

线程安全的进度显示器，带 ETA 计算。

```python
p = Progress(total=100, label="draws")
p.tick("processing draw 42")  # 打印进度条
elapsed = p.done()             # 完成，返回耗时秒数
```

#### `ErrorCollector`

线程安全的错误收集器。

```python
ec = ErrorCollector()
ec.append({"phase": "per_draw", "eid": 150, "error": "timeout"})
print(len(ec.errors))  # 已收集的错误数
```

### 常量

| 常量 | 值 | 说明 |
|------|----|------|
| `SESSION_PREFIX` | `"rdc-collect"` | 会话名前缀 |
| `MAIN_SESSION` | `"rdc-collect-main"` | 主会话名 |
| `RDC_BAT` | `rdc-portable/rdc.bat` | rdc-cli 入口路径 |

---

## workers.py — 数据采集与并行基础设施

所有数据采集函数的定义模块，同时包含并行 Worker 管理。

### 采集函数

#### `collect_base(errors, session=None) → dict`

采集 10 项基础数据，返回字典包含:

| 键 | rdc-cli 命令 | 说明 |
|----|-------------|------|
| `info` | `rdc info --json` | API / GPU 设备 / 事件统计 |
| `stats` | `rdc stats --json` | GPU 计数器 |
| `passes` | `rdc passes --json` | Pass 列表 |
| `pass_deps` | `rdc passes --deps --json` | Pass 依赖关系 |
| `draws` | `rdc draws --json` | Draw Call 列表 |
| `events` | `rdc events --json` | 事件列表 |
| `resources` | `rdc resources --json` | 资源列表 |
| `unused_targets` | `rdc unused-targets --json` | 未使用的 RT |
| `log` | `rdc log --json` | 验证日志 |
| `counters` | `rdc counters --json` | 硬件计数器 |

#### `collect_pass_details(summary, errors, session=None) → list`

逐个 Pass 调用 `rdc pass <index> --json`，获取 color_targets、depth_target、load/store ops。

#### `collect_per_draw(draw_eids, errors) → tuple[dict, dict]`

串行版：逐个 Draw Call 调用 `rdc pipeline <EID>` + `rdc bindings <EID>`。返回 `(pipelines, bindings)`。

#### `collect_shaders_disasm(out_dir, errors, session=None) → dict`

Shader 采集流程:

1. 通过 JSON-RPC 调用 `shaders` 方法列举所有 Shader
2. 调用 `build_shader_cache` 构建反汇编缓存（可能耗时 15 分钟）
3. 分组 VS+PS 对，调用 `shader_disasm` 获取反汇编
4. 保存 `.shader` 文件到 `shaders/` 目录

#### `collect_resource_details(summary, errors) → dict`

逐个 Texture/Buffer 通过 VFS 路径获取详细元数据:
- 纹理: `rdc cat /textures/{id}/info`
- 缓冲区: `rdc cat /buffers/{id}/info`

#### `collect_rt_usage(pass_details, errors, session=None, summary=None) → dict`

采集 RT 使用事件和 Descriptor 绑定:
- 对每个 RT 资源调用 `_rpc_call("usage", {id})`
- 对每个子 Pass 的首个 Draw Call 调用 `_rpc_call("descriptors", {eid})`
- 结果包含 `_descriptors` 特殊键

### 并行 Shard 函数

用于 `-j > 1` 模式，每个函数处理任务列表的一个分片:

| 函数 | 说明 |
|------|------|
| `_collect_per_draw_shard(session, eid_shard, progress, errors)` | 在指定 worker 会话上采集一组 Draw Call 的 pipeline + bindings |
| `_collect_resources_shard(session, resource_tasks, progress, errors)` | 在指定 worker 上采集一组资源的详细信息 |

### 并行基础设施

#### `_shard_list(items, num_shards) → list[list]`

轮询分片：将列表均匀分成 N 份。

```python
_shard_list([1,2,3,4,5], 2)  # → [[1,3,5], [2,4]]
```

#### `WorkerPool`

管理多个 rdc-cli daemon 会话的生命周期。

```python
pool = WorkerPool(capture_path, num_workers=4)
sessions = pool.open_all()  # → ["rdc-collect-w0", ..., "rdc-collect-w3"]
# ... 在各 session 上并行执行采集
pool.close_all()
```

每个 worker 独立打开 `.rdc` 文件（RenderDoc 不允许同一会话并发调用）。

---

## computed.py — 离线计算分析

纯计算模块，无 I/O 操作。读取已采集的数据生成分析结果。

### 主函数

#### `compute_analysis(summary, pass_details, pipelines, resource_details) → dict`

返回字典包含以下分析结果:

### 分析项

#### 三角形分布 (`triangle_distribution`)

按 Pass 统计三角形数量和占比，降序排列。

#### Draw 类型分布 (`draw_type_distribution`)

统计各 Draw 类型（DrawIndexed / Draw / DrawInstanced / DispatchCompute 等）的出现次数。

#### 内存估算 (`memory_estimate`)

- 纹理总内存 / 缓冲区总内存
- 最大资源排行（前 20）

#### 对称 Pass 检测 (`symmetric_passes`)

检测 VR 双眼渲染模式：将 Pass 序列分前后两半，比较签名（draws / triangles / RT 数量）相似度。

#### 管线状态去重 (`pipeline_dedup`)

对所有 Draw Call 的管线状态做 MD5 哈希，分组统计。高复用率说明 batching 良好。

#### 告警 (`alerts`)

| 类型 | 阈值 | 严重度 |
|------|------|--------|
| `high_triangle_draw` | 单 Draw >10,000 tri | warning |
| `empty_pass` | Pass 无 Draw Call | info |
| `large_resource` | 资源 >4.0 MB | warning |
| `validation_error` | 验证日志中的错误 | error |

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

---

## 关键设计约束

1. **单会话不可并发**: RenderDoc daemon 不支持同一会话内的并发调用（会死锁）。并行采集通过独立命名会话（`rdc-collect-w0..wN`）实现。

2. **嵌入式 Python**: 必须使用 `python/python.exe`，不使用系统 Python。所有依赖已预装在 `python/Lib/site-packages/`。

3. **只读目录**: `python/` 和 `rdc-portable/` 是签入的二进制目录，不可编辑。可编辑代码仅限 `Scripts/rdc/`。

4. **JSON-RPC 直连**: 部分操作（shader cache、usage、descriptors）绕过 CLI 的 30 秒超时限制，直接通过 TCP socket 与 daemon 通信。会话连接信息存储在 `%LOCALAPPDATA%/rdc/sessions/{session}.json`。

5. **错误容忍**: 单个 Draw Call 或资源的采集失败不会中断整体流程。错误被收集到 `ErrorCollector`，最终记录在 `_collection.json` 中。
