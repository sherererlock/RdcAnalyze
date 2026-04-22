# RdcAnalyze 工具与文档改进计划

## Context

当前 `RdcAnalyze` 是一套基于 rdc-cli 的 GPU 帧分析工具链，文档体系经过 commit `78700cf` 重构后已分为三层 API 文档（infra / collect / visual）+ 流程图 + JSON Schema，覆盖较全。但仍存在文档缺口、能力短板、未完成的 stub。本计划梳理当前状态并提出**文档改进 + 功能补强**两类建议，便于后续按优先级逐项落地。

---

## A. 当前状态盘点

### 文档（`docs/` + `CLAUDE.md`）

| 文件 | 行数 | 用途 | 状态 |
|------|------|------|------|
| `CLAUDE.md` | — | 项目总览 + Claude 工作约束 | ✅ |
| `docs/scripts-api.md` | 70 | 模块总览 + 入口约束 | ✅ |
| `docs/flowchart.md` | 239 | 7 张 Mermaid 管线图 | ✅ |
| `docs/infra-api.md` | 207 | rpc + shared API | ✅ |
| `docs/collect-api.md` | 198 | collect/workers/computed API | ✅ |
| `docs/visual-api.md` | 386 | analyze/tsv/render_graph/export_assets | ✅ |
| `docs/json-schema.md` | 644 | JSON 文件结构 | ✅ |

### 已实现能力

- 采集：10 步串行/并行管线，10 类基础数据 + Pass/RT/Pipeline/Bindings/Shader/Resource 详情
- 分析：8 章节 HTML 报告 + 16 张 TSV
- 渲染图：5 级回退依赖边推断
- Shader：8 种模式识别（Fullscreen Blit / Dithering / FXAA / Bloom Threshold / Gaussian Blur / Tonemapping / Shadow Map / PBR IBL）+ 指令分布 + 寄存器压力 + 变体去重 + Pass 矩阵
- 资源导出：`--export-assets` → FBX Mesh + PNG 纹理 + MD5 去重

### 已知缺口（来自代码 TODO + 架构盘点）

1. `shared.py:749-765` 三个 stub：**SSAO / SSR / Bilateral Filter** 模式检测未实现
2. `analyze.py:747` **TBDR tile load/store 效率分析** 仅在 GLES 数据缺失时提示，未实现
3. 无根目录 `README.md`（GitHub 首页空白）
4. `assets/pbr_comparison.html` 未追踪、未在文档中说明用途（已加入 `.gitignore`，为独立 PBR 教学 demo）
5. 无 `CHANGELOG.md` / 版本历史
6. 无 troubleshooting / FAQ 文档

---

## B. 文档改进建议（按优先级）

### B1. 添加根目录 `README.md`（高）

GitHub 首页直接呈现项目，应包含：项目定位 / 一行 quickstart / 截图（HTML 报告 + render_graph）/ 文档索引（指向 `docs/`）/ 系统要求（Win + 嵌入式 Python）/ 许可证。
- **复用**：`CLAUDE.md` 的 "Project Overview" + "Common Commands" 段落直接搬过去并精简。

### B2. 文档统一索引页 `docs/README.md`（中）

`docs/` 下当前 6 个 .md 没有索引，新读者需自己摸索阅读顺序。建议：
- **新读者路径**：`README.md` → `flowchart.md`（看图） → `scripts-api.md`（结构）→ 三层 API
- **数据消费者路径**：`json-schema.md` + `visual-api.md` 的 TSV 部分

### B3. 补 `docs/troubleshooting.md`（中）

集中常见错误：
- "shader cache 构建超时"（已通过 RPC 直连解决，记录为 FAQ）
- "SIGINT 后 worker 残留" 排查
- "GLES capture 无 load/store ops"（已在 analyze 中提示，文档化）
- "mesh 顶点 < 300 被跳过" 的过滤阈值与覆盖
- 端口冲突 / `%LOCALAPPDATA%/rdc/sessions/` 清理

### B4. 补 `docs/CHANGELOG.md`（低）

`collect.py` 已有 `version = "1.2.0"`，但无 changelog。从 git log 提取最近 5 条 feat/refactor 即可启动。

### B5. `docs/visual-api.md` 拆分（低）

386 行单文件偏长，可拆为 `analyze.md` + `render_graph.md` + `export_assets.md`，与脚本一一对应。

### B6. 说明并归档 `assets/pbr_comparison.html`（低）

已确认该文件为独立 WebGL PBR 光照方案对比 demo（双面板 WebGL Canvas，26KB 单文件），与 RDC 工具无数据依赖。已加入 `.gitignore`。

---

## C. 功能补强建议（按 ROI 排序）

---

### C0. 基础设施：analysis.json 持久化（前置，C2/C4/C8 共同依赖）

当前 `analyze.py` 的 `analysis` dict 在 `main()` 中组装后直接传给 `render_html()`，**不写盘**（`analyze.py:2152`）。C2 overdraw、C4 compare、C8 CI assert 都需要读取其中的派生数据，必须先持久化。

#### 实施细节

**改动位置**：`Scripts/rdc/analyze.py:main()`（约 2130 行），在 `render_html()` 调用后追加：

```python
analysis_path = analysis_dir / "json" / "analysis.json"
analysis_path.write_text(json.dumps(analysis, indent=2, ensure_ascii=False), encoding="utf-8")
```

**输出 Schema**（文档化进 `docs/json-schema.md` 末尾新章节）：

```
{
  "overview":        {...},   // analyze_frame_overview()  — GPU time、draw/pass counts
  "pipeline":        {...},   // analyze_pipeline()        — 每 pass 的 Gantt 数据
  "pipeline_stages": {...},   // analyze_pipeline_stages() — stage 分类 + GPU 时间分布
  "hotspots":        {...},   // analyze_hotspots()        — top tri/GPU-time draws
  "bandwidth":       {...},   // analyze_bandwidth()       — RT load/store MB 估算
  "shaders":         {...},   // analyze_shaders()         — 指令分布 + 变体统计
  "memory":          {...},   // analyze_memory()          — 纹理/缓冲区内存
  "suggestions":     [...]    // generate_suggestions()    — 建议列表
}
```

**关键文件**：`Scripts/rdc/analyze.py:2130+`；`docs/json-schema.md`（追加 schema）

---

### C1. 完成 `shared.py` 三个 Shader 模式 stub（中）

`shared.py:749-765` 已注册 detector 但返回 None。需准备 ground-truth capture 验证后实现：
- **SSAO**: ≥8 depth samples + 噪声纹理 + 半球采样
- **SSR**: ray-march loop + depth compare + 屏幕空间坐标
- **Bilateral Filter**: 加权采样 + 深度权重衰减

**复用**：现有 `ShaderContext` 已预解析 `sample_count` / `dref_count` / `has_log2_exp2` 等，新 detector 只需在 context 上添加少量字段（如 `loop_count`、`depth_sample_count`）。

**关键文件**：`Scripts/rdc/shared.py:705-770`（detector 注册区）

#### 实施细节

**Step 1：扩展 `ShaderContext`**（`shared.py:595-607`）—— 追加字段：

```python
loop_count: int          # re.findall(r'\bLoop\b', ps_text) 计数（RenderDoc SPIR-V 将 OpLoop 渲染为 "Loop"）
depth_sample_count: int  # ImageSampleExplicitLod.*[Dd]epth|res_depth|depthTex 命名匹配次数
noise_texture_hint: bool # ps_text 含 noise|hash|rand 命名，或含 Frac(Sin(Dot(
has_ray_march: bool      # loop_count >= 1 AND sample_count >= 4（派生字段）
```

**Step 2：在 `_build_shader_context()`**（`shared.py:610-645`）中补计算逻辑。

**Step 3：替换三个 stub**（`shared.py:749-765`）：

SSAO detector 判定条件：
```
ctx.depth_sample_count >= 8
AND ctx.dot_count >= 4           # 半球采样 N·V 投影
AND (ctx.noise_texture_hint OR ctx.sample_count >= 12)
AND not ctx.has_cube_sampler
```

SSR detector 判定条件：
```
ctx.has_ray_march
AND ctx.depth_sample_count >= 1
AND re.search(r'FOrdLessThan|FOrdGreaterThan', ctx.ps_text)  # depth compare
AND ctx.sample_count >= 4
```

Bilateral Filter detector 判定条件：
```
ctx.sample_count >= 5
AND ctx.depth_sample_count >= 2
AND re.search(r'\bExp\b|Pow.*FAbs', ctx.ps_text)             # 权重衰减
AND ctx.has_fclamp_01
```

**测试**：用含已知效果的 capture（Unity URP/HDRP 或 UE5 demo），跑 `collect.py` 后检查 `shaders.tsv` 的 `patterns` 列。

---

### C2. **Overdraw 估算**（高）

当前 `analyze_bandwidth()` 只算 RT load/store，未估 overdraw。可基于：`PS invocations / RT 像素数` per pass，输出每 pass 的 overdraw 倍数（hardware counter 已在 `counters.tsv` / `draw_timing.tsv` 中）。

**关键文件**：`Scripts/rdc/analyze.py` 新增 `analyze_overdraw()` 模块；TSV 加 `overdraw.tsv`。

#### 实施细节

**Step 1：新函数 `compute_overdraw()`** 放进 `Scripts/rdc/computed.py`，集成进 `compute_analysis()`：

```python
def compute_overdraw(summary: dict, pass_details: list) -> dict:
    """
    Returns:
      {
        "available": bool,           # False 若 PS Invocations counter 不存在
        "reason": str,               # 仅 available=False 时有
        "per_pass": [
          {"pass": str, "eid_range": [int, int], "rt_size": "WxH",
           "rt_pixels": int, "ps_invocations": int,
           "overdraw": float, "severity": "high"|"warn"|"ok"}
        ],
        "frame_avg_overdraw": float,
        "worst_pass": str
      }
    """
```

**算法**：
1. 从 `summary["counters"]["rows"]` 过滤 `counter == "PS Invocations"`，构建 `{eid: value}` 字典
2. 若该 counter 不存在 → 返回 `{"available": False, "reason": "PS Invocations counter not exposed"}`
3. 对每个 `pass_details[i]`，累加 `[begin_eid, end_eid]` 范围内的 PS Invocations → `ps_inv`；取主 color target `size` → `rt_pixels = w * h`；`overdraw = ps_inv / rt_pixels`（`rt_pixels==0` 时跳过）
4. `severity = "high" if overdraw > 4 else "warn" if overdraw > 2 else "ok"`

> **Counter 命名确认**：已实测 Adreno 样本中 counter 名为 `"PS Invocations"`（unit `Absolute`）。其他厂商（NVIDIA/AMD/Mali）通常同名；若不存在则 `available=False`，下游静默跳过。

**Step 2：集成进 `compute_analysis()`**（`computed.py:19+`）：
```python
result["overdraw"] = compute_overdraw(summary, pass_details)
```

**Step 3：TSV**，在 `tsv_export.py` 新增 `_build_overdraw(computed)` → `overdraw.tsv`：
```
pass    eid_range    rt_size    rt_pixels    ps_invocations    overdraw    severity
```

**Step 4：HTML**，在 `analyze.py` 新增 `analyze_overdraw(data)` section，颜色编码柱状图（red ≥4、amber 2-4、green <2）。

**关键文件**：`Scripts/rdc/computed.py:19+`；`Scripts/rdc/tsv_export.py`；`Scripts/rdc/analyze.py`（新 section）

---

### C3. **Mipmap 使用率检查**（中）

`resource_details.json` 已有纹理的 `mips` 总数，但未检查实际 view 覆盖的 mip 范围。新增一次采集步骤，收集每个 draw 的 descriptor view 信息，输出"纹理 X 的 view 仅覆盖 mip 0–1，剩余 N 层浪费显存"之类建议。

#### Spike 结果（2026-04-22）

- `rdc-cli bindings --json` 输出固定为 `{eid, stage, kind, set, slot, name}`，**不暴露 view / mip 信息**，无法通过扩展 bindings 命令解决。
- `rdc-cli script` 在 daemon 内可调用 RenderDoc Python API `controller.GetDescriptors(descriptorSetResourceId, [DescriptorRange])` 拿到完整 `Descriptor` 对象，字段含：
  ```
  firstMip    ← view 起始 mip 层
  numMips     ← view 覆盖的 mip 层数（上界，非实际采样层）
  resource    ← ResourceId，对应 resource_details 中的纹理 ID
  view        ← view ResourceId
  ```
  实测（ls.rdc EID 214，set 2）：`firstMip=0, numMips=9, resource=9631199`，该纹理总 mips=9 → 全量覆盖，无浪费。

**精度说明**：`numMips` 是 view 可见范围，硬件 LOD 选择在 `[firstMip, firstMip+numMips)` 内自动决定，无法从静态 view 得知具体采样了哪层。因此本方案检测的是 **view 级别的 mip 浪费**（view 未覆盖的层必然不被采样），而非采样级别的精确 LOD 分布。

#### 实施细节

**Step 1：新增采集脚本 `Scripts/rdc/collect_mip_views.py`**（或内联进 `collect.py` 作为新步骤），通过 `rdc-cli script` 按 pass 采样 descriptor set，输出 `json/binding_views.json`。

**采样策略**：只取每个 pass 的 `begin_eid`（来自已有 `passes.json`），而非遍历全部 draw。Pass 内 descriptor set 绑定通常在 pass 级别统一更新，per-draw 切换同一 slot 纹理极罕见，精度损失可接受，`SetFrameEvent` 调用次数从数百次降至 ~20-50 次。

```python
# 脚本在 daemon 内执行，rd / controller 已注入
import json, pathlib

passes_path = pathlib.Path(__ANALYSIS_DIR__) / "json" / "passes.json"
passes = json.loads(passes_path.read_text(encoding="utf-8"))
sample_eids = [p["begin_eid"] for p in passes if p.get("begin_eid") is not None]

result = {}  # {str(eid): [{set, bind, resource_id, first_mip, num_mips}]}
for eid in sample_eids:
    controller.SetFrameEvent(eid, False)
    state = controller.GetVulkanPipelineState()
    entries = []
    for set_idx, ds in enumerate(state.graphics.descriptorSets):
        dr = rd.DescriptorRange()
        dr.offset = 0
        dr.count = 64  # 每组最多取 64 个 descriptor
        descs = controller.GetDescriptors(ds.descriptorSetResourceId, [dr])
        for d in descs:
            rid = int(str(d.resource).split("::")[-1])
            if rid == 0:
                continue
            entries.append({
                "set": set_idx, "bind": int(d.byteOffset),
                "resource_id": rid,
                "first_mip": d.firstMip, "num_mips": d.numMips,
            })
    if entries:
        result[str(eid)] = entries
```

**输出 `json/binding_views.json` schema**：
```json
{
  "214": [
    {"set": 2, "bind": 0, "resource_id": 9631199, "first_mip": 0, "num_mips": 9}
  ]
}
```

**Step 2：新函数 `compute_mipmap_usage(binding_views, resource_details)`**，放 `computed.py`：

```python
def compute_mipmap_usage(binding_views: dict, resource_details: dict) -> dict:
    """
    Returns:
      {
        "per_texture": [
          {"resource_id": int, "name": str, "total_mips": int,
           "viewed_mip_range": [first, last],   # 所有 binding 中 view 覆盖的最大范围
           "unviewed_mips": [k, ...],            # total_mips 中从未被任何 view 覆盖的层
           "wasted_bytes": int,
           "recommendation": "Reduce mips from 9 to 5"}
        ],
        "total_wasted_mb": float
      }
    """
```

**算法**：
1. 遍历 `binding_views`，按 `resource_id` 累计各 view 的 `[first_mip, first_mip + num_mips)` 集合
2. 对每张 mips > 1 的纹理，`unviewed = set(0..total_mips-1) - union(viewed_ranges)`
3. `wasted_bytes ≈ byte_size × Σ(0.25^k for k in unviewed) / Σ(0.25^k for k in 0..total-1)`（mip 金字塔几何级数，精确比例）
4. 仅输出 `unviewed_mips` 非空且 `wasted_bytes > 0` 的纹理

**Step 3：TSV**，在 `tsv_export.py` 新增 `_build_mipmap_usage(computed)` → `mipmap_usage.tsv`：
```
resource    total_mips    viewed_range    unviewed_mips    wasted_mb    recommendation
```

**Step 4：`compute_analysis()` 集成**（`computed.py:compute_analysis()`）：
```python
binding_views = load_json(json_dir / "binding_views.json") or {}
result["mipmap_usage"] = compute_mipmap_usage(binding_views, resource_details)
```

**Step 5：HTML**，在 `analyze.py` 新增 `analyze_mipmap_usage(data)` section，表格列出浪费超过 1 MB 的纹理，加入 Optimization Suggestions。

**关键文件**：
- `Scripts/rdc/collect.py`（新 Step：调用 `rdc-cli script` 收集 binding_views）
- `Scripts/rdc/collect_mip_views.py`（新文件：在 daemon 内执行的采集脚本）
- `Scripts/rdc/computed.py`（新增 `compute_mipmap_usage()`）
- `Scripts/rdc/tsv_export.py`（新增 `mipmap_usage.tsv`）
- `Scripts/rdc/analyze.py`（新增 HTML section）

---

### C4. **多 capture 对比 / 回归基线**（高，对 CI 价值大）

新增 `Scripts/rdc/compare.py`：读两个 `*-analysis/` 目录，对比 frame_overview / stage_summary / shader_complexity / total_bandwidth，输出 diff HTML（pass 增删、shader 变体 diff、GPU time delta）。

**复用**：所有数据已在 `tsv/` 下，TSV diff 即可起步。

#### 实施细节

**依赖**：C0（`analysis.json` 持久化）。

**新文件 `Scripts/rdc/compare.py`**，约 300 行：

```python
def compare(baseline_dir: Path, current_dir: Path, out_html: Path,
            threshold_gpu: float = 10.0, threshold_bw: float = 20.0) -> int:
    """
    Returns: 0=无回归, 1=有回归（超过任一 error 级阈值）
    """
```

**CLI**：
```
python Scripts/rdc/compare.py <baseline-analysis/> <current-analysis/> [-o diff.html] [--threshold-gpu 10] [--threshold-bw 20]
```

**比较维度**（数据源 → diff 形式）：

| 维度 | 数据源 | diff 形式 |
|------|--------|-----------|
| Frame GPU time | `analysis.json → overview.gpu_time_ms` | delta 数值 + % |
| Pass 数量 | `analysis.json → pipeline.passes` | 增删 pass 列表 |
| Stage 时间分布 | `analysis.json → pipeline_stages.summary` | 各 stage delta 柱状对比 |
| Shader 变体 | `tsv/shader_variants.tsv` hash 集合 diff | 新增/移除 hash 列表 |
| Bandwidth | `analysis.json → bandwidth.total_mb` | delta + bloom_mb 对比 |
| Top-10 hotspot draws | `analysis.json → hotspots.draws[:10]` | 三角形数 delta |

**HTML 样式**：左右双列对比，红/绿 delta 颜色编码，超阈值项标 `⚠ regression`。复用 `analyze.py` 的辅助函数 `_esc()` / `_fmt_number()` / `_fmt_mb()` 和 `assets/` 中的共享 CSS。

**关键文件**：`Scripts/rdc/compare.py`（新文件）；`Scripts/rdc/analyze.py`（复用辅助函数）

---

### C5. **TBDR tile 效率分析**（中，移动端高价值）

`analyze.py:747` 已有提示但未实现。在 load/store ops 数据可用时，统计每 pass 的 tile load/store 字节，标记可消除的 store（如 RT 仅在本 pass 内使用）。

#### 实施细节

**替换位置**：`analyze.py:738-751`（当前 info 提示），改为实际分析。

**新函数 `analyze_tbdr(pass_details, resources)`** 放 `analyze.py`，**仅当** `pass_details` 中至少一个 pass 含 `load_op`/`store_op` 字段时启用：

```python
def analyze_tbdr(pass_details: list, resources: dict) -> dict:
    """
    Returns:
      {
        "available": bool,
        "per_rt": [
          {"pass": str, "rt_name": str, "format": str,
           "load_op": "Load"|"Clear"|"DontCare",
           "store_op": "Store"|"DontCare",
           "tile_mb": float,
           "issue": "unnecessary_load"|"unnecessary_store"|None,
           "recommendation": str}
        ],
        "wasted_mb": float
      }
    """
```

**判定逻辑**：
- `unnecessary_load`：`load_op == "Load"` 且 `_rt_has_prior_writer(ct, p, pass_details)` 返回 False（即没有上游写出此 RT 的 pass）
- `unnecessary_store`：`store_op == "Store"` 且 `_rt_consumed_after(ct, p, pass_details)` 返回 False（即后续无 pass 读此 RT ≈ transient RT）

辅助 `_rt_consumed_after()` 可复用 `render_graph._build_dependency_edges()` 的边结果（`render_graph.py:410`）判断。

`tile_mb = w × h × bpp / (1024²)`，`bpp` 由 `shared.FORMAT_BPP` 表查找（已有）。

**TSV**，新增 `tbdr_efficiency.tsv`：
```
pass    rt_name    format    load_op    store_op    tile_mb    issue    recommendation
```

**关键文件**：`Scripts/rdc/analyze.py:738+`；`Scripts/rdc/tsv_export.py`；`Scripts/rdc/render_graph.py:410`（edges 复用）

---

### C6. **Pass 合并建议**（中）

利用 `render_graph` 已构建的依赖边，检测形如 "A → B 且 B 仅消费 A 的 RT 且分辨率/格式相同" 的链，建议合并为 sub-pass（Vulkan multi-subpass / Metal tile shader）。

#### 实施细节

**新函数 `detect_mergeable_passes(subpasses, edges, pass_details)`**，放 `render_graph.py` 末尾：

**算法**：
1. 遍历 `edges`（schema `{src, dst, type, label}`），找 `type in ("rt_flow", "inferred")` 且 `dst` 节点入度 == 1 的边 `(A → B)`
2. 对每对 `(A, B)` 检查：
   - 同一 RT 分辨率 + 格式
   - B 的所有读 RT 均来自 A 写出
   - A 的所有写 RT 均被 B 消费且不被其他节点读
   - `(A, B)` 之间无 compute/transfer sub-pass 节点介入
3. 满足条件 → 加入 mergeable group

**输出**：
```python
{
  "mergeable_groups": [
    {
      "passes": ["GBufferOpaque", "ShadingDeferred"],
      "reason": "Linear RT flow, same 1920x1080 RGBA8, single consumer",
      "recommendation": "Merge as Vulkan multi-subpass / Metal tile shader",
      "estimated_bandwidth_saved_mb": 8.4
    }
  ]
}
```

**TSV**，新增 `pass_merge_suggestions.tsv`：
```
pass_a    pass_b    rt_size    rt_format    bandwidth_saved_mb    reason
```

**HTML**：作为 `analyze.py` Optimization Suggestions section 的子项，附 render_graph 高亮对应边（JS 交互）。

**关键文件**：`Scripts/rdc/render_graph.py:410+`（复用 edges 和 subpasses）；`Scripts/rdc/tsv_export.py`；`Scripts/rdc/analyze.py`

---

### C7. **Vertex/Index Buffer 详细分析**（低）

当前 `--export-assets` 导出 FBX 但未分析顶点数据效率：顶点格式（half vs float）、属性数量、index buffer 重用率。可加 `vertex_efficiency.tsv`。

#### 实施细节

**前置**：当前 `meshes.json` 每项只有 `{file, vertex_count, attributes, size_bytes}`，**缺 `index_count` / `vertex_stride_bytes` / `vertex_format`**。需先扩展 `export_assets.py:_export_one_mesh()`（`line 155+/192`），在写 JSON entry 时补充：

```json
{
  "file": "meshes/mesh_5308.fbx",
  "vertex_count": 12345,
  "index_count": 36000,
  "vertex_stride_bytes": 32,
  "vertex_format": [
    {"semantic": "POSITION", "format": "R32G32B32_FLOAT", "size_bytes": 12},
    {"semantic": "NORMAL",   "format": "R8G8B8A8_SNORM",  "size_bytes": 4}
  ],
  "attributes": ["POSITION", "NORMAL", "TANGENT", "UV"],
  "size_bytes": 438720
}
```

需同步修改 `_parse_vbuffer()` 不丢弃列类型信息。

**新函数 `analyze_vertex_efficiency(meshes)`** 放 `computed.py`：

检测项：
1. **过大格式**：POSITION 用 `R32G32B32_FLOAT` 但可以 half → 建议 `R16G16B16_FLOAT`（节省 33%）
2. **冗余属性**：shader disasm 不含 `NORMAL`/`TANGENT` 用法但 buffer 传递
3. **Index 重用率低**：`index_count / vertex_count < 1.5` → 网格过度细分
4. **Stride padding**：`vertex_stride_bytes > sum(attr.size_bytes) + 4` → 有浪费

**TSV**，新增 `vertex_efficiency.tsv`：
```
mesh_file    vertex_count    index_count    stride_bytes    reuse_ratio    issues    potential_savings_kb
```

**关键文件**：`Scripts/rdc/export_assets.py:155+/192`；`Scripts/rdc/computed.py`；`Scripts/rdc/tsv_export.py`；`Scripts/rdc/analyze.py`

---

### C8. **CI 集成 + 阈值断言**（中）

新增 `Scripts/rdc/assert.py`：从 yaml/json 读阈值（如 `total_triangles < 500K`、`bloom_passes <= 6`、`bandwidth_mb < 80`），对 `*-analysis/` 校验，非 0 退出码用于 CI gate。

**复用**：`computed.json` + 各 TSV 已有所有需要的指标。

#### 实施细节

**依赖**：C0（`analysis.json` 持久化）；C2（`overdraw` 字段，可选）。

**新文件 `Scripts/rdc/assert.py`**，约 200 行：

```python
def assert_thresholds(analysis_dir: Path, thresholds_path: Path) -> int:
    """
    Returns: 0=全部通过, 1=有 error 级违规
    打印每条结果：✓ / ✗ metric: actual op expected (severity)
    """
```

**CLI**：
```
python Scripts/rdc/assert.py <analysis-dir>/ --thresholds thresholds.yaml [--junit out.xml]
```

**Thresholds YAML 格式**（工程团队维护 `thresholds.yaml`）：
```yaml
gpu_time_ms:
  max: 16.6
  severity: error        # error → 退出码 1；warn → 仅打印警告
total_triangles:
  max: 500000
  severity: warn
bandwidth_mb:
  max: 80
  severity: error
bloom_passes:
  max: 6
  severity: warn
shader_unique_count:
  max: 200
  severity: warn
overdraw_frame_avg:
  max: 3.5
  severity: error
overdraw_per_pass.MainColor:
  max: 5.0
  severity: warn
```

**取值逻辑**（dotted path 解析）：
- `gpu_time_ms` → `analysis.json → overview.gpu_time_ms`
- `total_triangles` → `computed.json → triangle_distribution.total`
- `bandwidth_mb` → `analysis.json → bandwidth.total_mb`
- `bloom_passes` → `analysis.json → bandwidth.bloom_passes`
- `overdraw_frame_avg` → `computed.json → overdraw.frame_avg_overdraw`
- `overdraw_per_pass.MainColor` → `computed.json → overdraw.per_pass[]` 中查 `pass == "MainColor"`

**输出样例**：
```
✗ gpu_time_ms: 18.2 > max 16.6 (error)
✓ bandwidth_mb: 64.3 ≤ max 80 (ok)
⚠ total_triangles: 520000 > max 500000 (warn)
```

可选 `--junit out.xml` 输出 JUnit XML，供 Jenkins/GitHub Actions 消费。

**关键文件**：`Scripts/rdc/assert.py`（新文件）；依赖 C0 的 `json/analysis.json` + 现有 `json/computed.json`

---

## D. 推荐落地顺序

| 阶段 | 任务 | 预计成本 |
|------|------|----------|
| Sprint 1 | **C0** analysis.json + **B1** README + **B3** troubleshooting + **C2** overdraw | 1–2 天 |
| Sprint 2 | **C4** 多 capture 对比 + **C8** CI 断言 | 2–3 天 |
| Sprint 3 | **C1** SSAO/SSR/Bilateral 检测 + **C5** TBDR | 3–5 天（需 ground-truth capture） |
| 长期 | B2 索引 / B4 changelog / C3 mipmap / C6 pass 合并 / C7 vbuffer | 按需 |

---

## E. 关键文件参考

- 文档新增：`README.md`（根目录）、`docs/README.md`、`docs/troubleshooting.md`、`docs/CHANGELOG.md`
- 代码扩展：
  - `Scripts/rdc/analyze.py:2130+` —— C0：analysis.json 持久化
  - `Scripts/rdc/shared.py:595-607` —— C1：扩 ShaderContext（loop_count / depth_sample_count）
  - `Scripts/rdc/shared.py:749-765` —— C1：注册新 shader detector（SSAO / SSR / Bilateral）
  - `Scripts/rdc/computed.py:19+` `compute_analysis()` —— C2/C3/C7 派生指标统一入口
  - `Scripts/rdc/analyze.py` —— 新增 `analyze_overdraw()` / `analyze_tbdr()` section
  - `Scripts/rdc/tsv_export.py` —— 新增 `overdraw.tsv` / `mipmap_usage.tsv` / `tbdr_efficiency.tsv` / `pass_merge_suggestions.tsv` / `vertex_efficiency.tsv`
  - `Scripts/rdc/render_graph.py:410` `_build_dependency_edges()` —— C5/C6 复用
  - `Scripts/rdc/compare.py`（新文件）—— C4：多 capture diff
  - `Scripts/rdc/assert.py`（新文件）—— C8：CI gate
  - `Scripts/rdc/collect.py` —— C3：新增 rdc-cli script 步骤采集 binding_views.json；C7：_parse_vbuffer 保留列类型
  - `Scripts/rdc/collect_mip_views.py`（新文件）—— C3：daemon 内执行脚本，调用 GetDescriptors 收集 view/mip 范围
  - `Scripts/rdc/export_assets.py:155+/192` —— C7：_export_one_mesh 扩字段

## F. 验证方式

- 文档：在 GitHub 网页端查看 README 渲染、链接跳转
- shader detector：用包含已知效果的 capture 跑 `collect.py` → 检查 `shaders.tsv` 的 patterns 列
- overdraw / TBDR：对比同一帧在 `analyze.py` 输出的报告与 RenderDoc UI 中的 overdraw view
- compare：对同一项目的两个 commit 各采集一次，跑 `compare.py` 看 diff
- CI assert：构造一个超阈值的 capture，确认 `assert.py` 返回非 0
