# AGENTS.md - AI 开发规范

> 本文件用于指导 AI 模型理解项目架构和开发规范，防止错误修改。

## 项目概述

doc-ingest 是一个基于 FastAPI 的文档解析服务，核心功能是将各类文档（PDF、Word、PPT 等）转换为 Markdown，并可选地调用多模态模型进行内容整理。

## 目录结构

```
app/
├── main.py              # FastAPI 应用入口
├── config.py            # 配置定义（pydantic-settings）
├── api/
│   ├── routes.py        # 同步接口 (/convert/file, /convert/url)
│   └── sse_routes.py    # 流式接口 (/convert/file/stream)
├── services/
│   ├── parser_service.py   # 文档解析核心逻辑
│   └── model_service.py    # 模型调用封装
├── infra/
│   ├── downloader.py       # URL 下载
│   ├── storage_client.py   # 对象存储客户端
│   └── storage/            # 多厂商存储实现
├── middleware/
│   └── trace.py            # x-trace-id 中间件
└── utils/
    ├── parse_utils.py      # Markdown 解析工具
    ├── sse_generator.py    # SSE 事件生成器
    ├── sse_events.py       # SSE 事件类型定义
    └── trace.py            # trace_id 上下文管理
```

## 核心设计原则

### 1. 配置管理
- **所有配置**通过 `app/config.py` 的 `Settings` 类管理
- 环境变量统一使用 `DOC_INGEST__` 前缀
- 使用 `get_settings()` 获取配置（带 lru_cache）
- **禁止**在代码中硬编码配置值

### 2. SSE 流式实现
- SSE 事件通过 `SSEEventGenerator` 生成
- 每个处理阶段使用 `async with gen.stage(...)` 包裹
- **所有阶段必须启用心跳**：`enable_heartbeat=True`
- 模型输出通过 `model_chunk` 事件透传
- `complete` 事件不携带业务数据，仅作结束标记

### 3. 内存管理 ⚠️ 关键
文档处理涉及大量内存操作，必须遵守严格的 GC 策略：
```python
import gc

# 1. 转换完成后立即释放 MarkItDown 结果
markdown = normalize_markdown(result.text_content or "")
del result
del md

# 2. 图片 base64 清理策略
# A. OSS 上传成功后（storage_client.py）
#    -> 立即 pop("base64")
# B. OSS 上传失败或未启用 OSS
#    -> 保留 base64 作为 fallback 供模型处理
# C. 模型处理完成后（model_service.py）
#    -> 立即清理所有图片的 base64 (del img["base64"])
#    -> 如为分块处理，每个 chunk 完成后即清理

# 3. 模型处理后释放中间变量
del chunks, content, messages, full_result

# 4. 请求结束时主动 GC
gc.collect()
```

### 4. 日志规范
- 使用 `loguru.logger`
- trace_id 必须包含在日志中：`trace_id={gen.trace_id}`
- 阶段日志格式：`{method}: {stage} {key}={value} elapsed_ms={ms}`
- 禁止在 INFO 级别打印 base64 或大文本

### 5. 错误处理
- 同步接口：返回 `{"code": 1, "msg": "...", "data": {}}`
- SSE 接口：发送 `error` 事件后结束流
- 所有异常必须 catch 并记录日志

## 禁止的操作 🚫

1. **禁止删除 gc.collect() 调用** - 会导致内存泄漏
2. **禁止在 SSE 事件中传递大数据**（images/structured）- 会超出响应大小限制
3. **禁止将 enable_heartbeat 改为 False** - 会导致客户端超时
4. **禁止修改 trace_id 中间件逻辑** - 会破坏链路追踪
5. **禁止在配置类外定义环境变量读取** - 破坏配置统一管理

## 修改须知

### 添加新的 SSE 阶段
```python
async with gen.stage("new_stage", "阶段描述", start_progress, end_progress, enable_heartbeat=True):
    # 阶段逻辑
    result = await loop.run_in_executor(None, sync_function)

# 阶段完成后释放大对象
del result
```

### 添加新的配置项
1. 在 `config.py` 对应的 `*Settings` 类中添加字段
2. 使用 `Field()` 定义默认值和描述
3. 更新 `README.md` 配置说明表格

### 添加新的存储提供商
1. 在 `app/infra/storage/providers/` 创建新文件
2. 继承 `StorageProvider` 基类
3. 在 `storage/__init__.py` 注册

## 测试命令

```bash
# 本地运行
uv run uvicorn app.main:app --reload

# Docker 构建
docker-compose up -d --build

# SSE 测试
python scripts/test_sse.py test_files/sample.pdf
```

## 版本信息

- Python: 3.11+
- 依赖管理: uv (pyproject.toml + uv.lock)
- 框架: FastAPI
- 模型 SDK: OpenAI (兼容格式)
