# 采集层 API

数据采集主控 (`collect.py`)、采集函数与并行框架 (`workers.py`)、离线计算 (`computed.py`) 的详细文档。

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
