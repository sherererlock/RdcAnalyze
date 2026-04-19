# Scripts/rdc 模块文档

本文档描述 `Scripts/rdc/` 下各 Python 模块的职责和分层架构。各层的详细 API 文档见子页面。

---

## 模块总览

```
Scripts/
├── rdc-report.bat          # 一键管线: collect → analyze
└── rdc/
    ├── collect.py          # 主控: 解析参数, 编排采集步骤, 输出 JSON
    ├── analyze.py          # 读取 JSON, 生成 performance_report.html
    ├── tsv_export.py       # TSV 表生成 (collect.py 调用)
    ├── shared.py           # BPP 表, 格式化, JSON I/O, 管线阶段分类, Shader 模式识别
    ├── rpc.py              # rdc-cli 子进程调用 + JSON-RPC 直连
    ├── workers.py          # 数据采集函数 + WorkerPool 并行框架
    ├── computed.py         # 三角形分布 / 内存估算 / 管线去重 / 告警
    ├── render_graph.py     # 子 Pass 提取, 依赖边推断, 交互式 HTML 图
    ├── export_assets.py    # FBX Mesh 导出 + PNG 纹理导出
    └── fbx_writer.py       # write_fbx() — ASCII FBX 7.3 写入
```

## 分层架构

| 层 | 模块 | 职责 | 详细文档 |
|----|------|------|----------|
| 通信 | `rpc.py` | 封装 rdc-cli 子进程调用和 JSON-RPC socket 直连 | [infra-api.md](infra-api.md) |
| 公共 | `shared.py` | 格式化、BPP 估算、JSON I/O、管线阶段分类、Shader 模式识别 | [infra-api.md](infra-api.md) |
| 采集 | `collect.py`, `workers.py` | 主控编排 + 各阶段采集函数 + 并行 Worker 框架 | [collect-api.md](collect-api.md) |
| 计算 | `computed.py` | 离线分析算法 (无 I/O) | [collect-api.md](collect-api.md) |
| 可视化 | `analyze.py`, `render_graph.py` | HTML 报告 / 依赖图生成 | [visual-api.md](visual-api.md) |
| TSV 导出 | `tsv_export.py` | token 高效的 TSV 表生成（供 LLM/脚本消费） | [visual-api.md](visual-api.md) |
| 导出 | `export_assets.py`, `fbx_writer.py` | Mesh FBX + 纹理 PNG 导出 | [visual-api.md](visual-api.md) |

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

## 关键设计约束

1. **单会话不可并发**: RenderDoc daemon 不支持同一会话内的并发调用（会死锁）。并行采集通过独立命名会话（`rdc-collect-w0..wN`）实现。

2. **嵌入式 Python**: 必须使用 `python/python.exe`，不使用系统 Python。所有依赖已预装在 `python/Lib/site-packages/`。

3. **只读目录**: `python/` 和 `rdc-portable/` 是签入的二进制目录，不可编辑。可编辑代码仅限 `Scripts/rdc/`。

4. **JSON-RPC 直连**: 部分操作（shader cache、usage、descriptors）绕过 CLI 的 30 秒超时限制，直接通过 TCP socket 与 daemon 通信。会话连接信息存储在 `%LOCALAPPDATA%/rdc/sessions/{session}.json`。

5. **错误容忍**: 单个 Draw Call 或资源的采集失败不会中断整体流程。错误被收集到 `ErrorCollector`，最终记录在 `_collection.json` 中。
