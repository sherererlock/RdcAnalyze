# 通信 + 公共层 API

通信层 (`rpc.py`) 和公共工具层 (`shared.py`) 的详细文档。

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

## shared.py — 公共工具函数

被所有模块引用的基础设施函数。

### 格式化与估算

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

### 管线阶段分类

#### `classify_pass_stage(p, *, all_passes, bloom_pass_names, max_rt_area) → tuple[str, str]`

元数据驱动的 Pass 分类。根据 RT 格式/尺寸、load ops、draw 特征判断管线阶段。返回 `(stage_name, reason)`。

#### `detect_bloom_chain(pass_details) → dict | None`

检测连续同格式 pass 中的降采样-升采样金字塔结构。要求 ≥2 个 down + ≥1 个 up，支持最后一步直接跳回原始分辨率的 composite。

#### `detect_fullscreen_quad(draws_in_pass, rt_width, rt_height, counters_by_eid) → bool`

判断 pass 内所有 draw 是否为全屏 quad（tri ≤ 2 且 PS invocations ≈ RT 像素数，容差 20%）。

### Shader 模式识别

基于 Registry 的 SPIR-V 反汇编模式检测系统。支持 PS 和 CS 两种 Shader 类型。

#### `ShaderContext` (dataclass)

预解析的 Shader 信号，每个 Shader 只构建一次：

| 字段 | 类型 | 说明 |
|------|------|------|
| `ps_text` | str | PS 段文本（CS 则为全文） |
| `is_compute` | bool | 是否 Compute Shader |
| `spirv_bound` | int | SPIR-V ID 上界 |
| `sample_count` | int | 所有 ImageSample* 操作计数 |
| `dref_count` | int | ImageSampleDref* 操作计数 |
| `has_cube_sampler` | bool | 存在 `SampledImage<float, Cube>` |
| `has_log2_exp2` | bool | Log2 + Exp2 同时存在（pow 仿真） |
| `dot_count` | int | `Dot(` 调用次数 |
| `has_inversesqrt` | bool | 存在 InverseSqrt |
| `has_fclamp_01` | bool | 存在 `FClamp(x, 0, 1)` |

#### `detect_shader_patterns(shader_content, is_compute=False) → list[str]`

构建 `ShaderContext`，遍历注册的 detector 返回匹配的模式名列表。

**已实现的模式**:

| 模式 | 判据 | exclusive |
|------|------|-----------|
| Fullscreen Blit | sample_count == 1, spirv_bound < 50 | 是 |
| Dithering | SpecId + FragCoord + bit ops (& 1, << 1) + Round | 否 |
| FXAA | samples ≥ 5 + FMax + FMin + spirv_bound > 400 | 否 |
| Bloom Threshold | FMax .xyz + subtract + clamp + multiply | 否 |
| Gaussian Blur | samples ≥ 3 + offset arithmetic + repeated sample ops | 否 |
| Tonemapping | Pow + (samples ≥ 2 or matrix multiply) | 否 |
| Shadow Map | Dref ≥ 1 + `SampledImage<float, 2D>` + factor arithmetic | 否 |
| PBR IBL | Cube sampler (必需) + ≥2/4 信号 (Log2/Exp2, InverseSqrt+Dot≥3, FClamp01, ExplicitLod) | 否 |

**TODO stub** (已注册，返回 None):
- SSAO: ≥8 depth samples + noise + hemisphere sampling
- SSR: ray march loop + depth compare + screen-space coords
- Bilateral Filter: weighted sampling + depth-based weight falloff

#### `_register(name, *, exclusive=False)`

装饰器，将 detector 函数注册到 `_PATTERN_REGISTRY`。`exclusive=True` 表示该模式匹配后跳过后续检测（互斥模式）。

### Shader 分析函数

#### `analyze_spirv_instructions(shader_content, is_compute=False) → dict`

统计 7 类指令分布: arithmetic, sample, logic, load_store, dot_matrix, intrinsic, barrier。返回各类计数 + total。

#### `estimate_register_pressure(shader_content, is_compute=False) → dict`

估算寄存器压力: temp_vars (Private 声明), input/output/uniform_vars, spirv_bound, estimated_vgprs (temp_vars × 4), pressure_level (low/medium/high)。

#### `deduplicate_shaders(shader_disasm, shaders_dir) → dict`

Shader 变体去重: 规范化内容（去头部注释, SpecId 值→占位符）→ MD5 hash → 分组。返回 groups, total_shaders, unique_shaders, variant_groups。

### 常量

- **`BPP_TABLE`**: 57 条 GPU 格式 → BPP 映射表，覆盖 RGBA、BC 压缩、ASTC、D/S 等格式。
- **`STAGE_COLORS`**: 管线阶段 → 色值映射，用于 HTML 报告中的阶段标签和条形图。
