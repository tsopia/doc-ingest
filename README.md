# Doc Ingest Service

基于 FastAPI + MarkItDown 的文档解析服务，支持多模态模型整理、实时流式输出和全链路追踪。

## ✨ 核心特性

- **多模态解析**：支持 URL 下载和文件上传，自动提取结构化数据（标题/段落/表格）
- **AI 智能整理**：集成 GPT-4o/Qwen-VL 等多模态模型，对文档内容进行深度整理
- **实时流式响应**：支持 SSE（Server-Sent Events）流式输出，大文件处理不超时，提供"打字机"体验
- **全链路可观测**：自动生成 `x-trace-id`，支持请求链路追踪和性能分析
- **智能重试**：内置模型调用重试机制，提升稳定性
- **对象存储集成**：支持将提取的图片上传至 S3/OSS/MinIO/腾讯云 COS 等兼容存储

---

## 🚀 快速开始

### 1. 安装依赖
```bash
uv sync
```

### 2. 启动服务
```bash
# 开发模式
uv run uvicorn app.main:app --reload --host 0.0.0.0 --port 8000

# 生产模式
uv run uvicorn app.main:app --host 0.0.0.0 --port 8000 --workers 4
```

### 3. Docker 部署
```bash
docker-compose up -d
```

### 4. 健康检查
```bash
curl http://localhost:8000/health
# {"status":"healthy","service":"doc-ingest","version":"0.1.0"}
```

---

## ⚙️ 配置说明

所有配置均支持 `.env` 文件，统一前缀 `DOC_INGEST__`。

### 核心配置
| 环境变量 | 默认值 | 说明 |
|---------|--------|------|
| `DOC_INGEST__LOG__LEVEL` | `INFO` | 日志级别 (DEBUG/INFO/WARNING/ERROR) |

### 模型配置 (AI 整理)
| 环境变量 | 默认值 | 说明 |
|---------|--------|------|
| `DOC_INGEST__MODEL__API_KEY` | - | **必填** 模型 API Key |
| `DOC_INGEST__MODEL__BASE_URL` | - | API Base URL (兼容 OpenAI 格式) |
| `DOC_INGEST__MODEL__MODEL_NAME` | `gpt-4o` | 模型名称 (如 qwen-plus, deepseek-chat) |
| `DOC_INGEST__MODEL__TIMEOUT_SECONDS` | `120` | 模型超时时间 |
| `DOC_INGEST__MODEL__MAX_INPUT_TOKENS` | `25000` | 最大输入 Token 限制 |
| `DOC_INGEST__MODEL__TEMPERATURE` | `0.1` | 模型温度 |
| `DOC_INGEST__MODEL__MAX_RETRIES` | `3` | 模型调用最大重试次数 |
| `DOC_INGEST__MODEL__DENOISE_PROMPT` | - | AI 去噪的 Prompt |

### SSE 流式配置
| 环境变量 | 默认值 | 说明 |
|---------|--------|------|
| `DOC_INGEST__SSE__HEARTBEAT_INTERVAL` | `10` | 心跳间隔（秒） |
| `DOC_INGEST__SSE__LONG_STAGE_THRESHOLD` | `10` | 触发心跳的阶段耗时阈值（秒） |

### 对象存储 (OSS/S3/MinIO/COS)
| 环境变量 | 说明 |
|---------|------|
| `DOC_INGEST__STORAGE__PROVIDER` | 存储提供商: `oss`, `s3`, `minio`, `cos` (默认 `oss`) |
| `DOC_INGEST__STORAGE__ENDPOINT` | 存储服务端点 |
| `DOC_INGEST__STORAGE__ACCESS_KEY_ID` | Access Key ID |
| `DOC_INGEST__STORAGE__ACCESS_KEY_SECRET` | Access Key Secret |
| `DOC_INGEST__STORAGE__BUCKET` | 存储桶名称 |
| `DOC_INGEST__STORAGE__REGION` | 区域（S3/COS需要） |
| `DOC_INGEST__STORAGE__PREFIX` | 对象前缀 (默认 `doc-ingest/`) |
| `DOC_INGEST__STORAGE__SECURE` | 是否使用HTTPS (默认 `true`) |
| `DOC_INGEST__STORAGE__URL_TTL_SECONDS` | 下载链接有效期（默认 1800s） |

### 可观测性 (Langfuse)
可选配置，配置密钥后自动启用 Langfuse 链路追踪。
| 环境变量 | 默认值 | 说明 |
|---------|--------|------|
| `DOC_INGEST__LANGFUSE__PUBLIC_KEY` | - | Langfuse Public Key |
| `DOC_INGEST__LANGFUSE__SECRET_KEY` | - | Langfuse Secret Key |
| `DOC_INGEST__LANGFUSE__HOST` | `https://cloud.langfuse.com` | Langfuse Host URL |

---

## 🔌 API 接口

### 1. 同步接口 (Standard API)

适用于不需要实时反馈的场景（如后台批处理）。支持**同步等待**和**异步回调**两种模式。

#### 1.1 文件转换

**POST** `/convert/file`

| 参数 | 类型 | 必选 | 说明 |
|------|------|------|------|
| `file` | File | 是 | 上传的文件 |
| `callback_url` | string | 否 | 回调地址。若提供，接口立即返回 `task_id`，处理完成后向该地址发送 POST 请求。 |
| `mode` | string | 否 | 处理模式: `ocr` 等 (默认 `ocr`) |
| `enable_ai_denoise` | string | 否 | AI去噪开关: `auto`/`true`/`false` (默认 `auto`) |

```bash
# 模式 A: 同步等待 (默认)
curl -X POST "http://localhost:8000/convert/file" \
  -F "file=@/path/to/doc.pdf"

# 模式 B: 异步回调
curl -X POST "http://localhost:8000/convert/file" \
  -F "file=@/path/to/doc.pdf" \
  -F "callback_url=http://your-server.com/webhook"
# 响应: {"code": 0, "msg": "Task accepted", "data": {"trace_id": "..."}}
```

#### 1.2 URL 转换

**POST** `/convert/url`

| 参数 | 类型 | 必选 | 说明 |
|------|------|------|------|
| `url` | string | 是 | 文档 URL |
| `callback_url` | string | 否 | 回调地址 |
| `mode` | string | 否 | 处理模式: `ocr` 等 (默认 `ocr`) |
| `enable_ai_denoise` | string | 否 | AI去噪开关: `auto`/`true`/`false` (默认 `auto`) |

```bash
# 模式 A: 同步等待
curl -X POST "http://localhost:8000/convert/url" \
  -H "Content-Type: application/json" \
  -d '{"url": "https://example.com/doc.pdf"}'

# 模式 B: 异步回调
curl -X POST "http://localhost:8000/convert/url" \
  -H "Content-Type: application/json" \
  -d '{"url": "...", "callback_url": "..."}'
```

### 2. 流式接口 (Streaming/SSE API) 🔥

**推荐使用**。适用于 Web 应用，提供实时进度和模型输出的"打字机"效果，彻底解决大文件超时问题。

#### 2.1 文件流式转换

**POST** `/convert/file/stream`

| 参数 | 类型 | 必选 | 说明 |
|------|------|------|------|
| `file` | File | 是 | 上传的文件 |
| `include_result` | bool | 否 | 是否在流结束时计算并返回完整 markdown 结果 (默认 `false`)。开启会增加服务端内存消耗。 |
| `mode` | string | 否 | 处理模式: `ocr` 等 (默认 `ocr`) |
| `enable_ai_denoise` | string | 否 | AI去噪开关: `auto`/`true`/`false` (默认 `auto`) |

```bash
# 默认模式 (省流/省内存)
curl -N -X POST "http://localhost:8000/convert/file/stream" \
  -F "file=@/path/to/doc.pdf"

# 开启结果汇总 (调试用)
curl -N -X POST "http://localhost:8000/convert/file/stream" \
  -F "file=@/path/to/doc.pdf" \
  -F "include_result=true"
```

#### 2.2 URL 流式转换

**POST** `/convert/url/stream`

| 参数 | 类型 | 必选 | 说明 |
|------|------|------|------|
| `url` | string | 是 | 文档 URL |
| `include_result` | bool | 否 | 是否在流结束时计算并返回完整 markdown 结果 (默认 `false`) |
| `mode` | string | 否 | 处理模式: `ocr` 等 (默认 `ocr`) |
| `enable_ai_denoise` | string | 否 | AI去噪开关: `auto`/`true`/`false` (默认 `auto`) |

```bash
curl -N -X POST "http://localhost:8000/convert/url/stream" \
  -H "Content-Type: application/json" \
  -d '{"url": "https://example.com/doc.pdf", "include_result": false}'
```

**响应格式 (Server-Sent Events)**:

```
data: {"type": "started", "progress": 0, "message": "开始处理"}

data: {"type": "stage:converting", "progress": 5, "message": "文档转换中"}

data: {"type": "stage:structuring_done", "progress": 20, "message": "结构化处理完成"}

data: {"type": "model_chunk", "data": {"content": "AI 解析的"}}

data: {"type": "model_chunk", "data": {"content": "内容片段..."}}

data: {"type": "complete", "progress": 100, "message": "处理完成"}
```
*(注：图片提取、上传等阶段也会有相应的进度事件)*

**前端集成示例 (JavaScript)**:

```javascript
const response = await fetch('/convert/file/stream', { method: 'POST', body: formData });
const reader = response.body.getReader();
const decoder = new TextDecoder();

while (true) {
  const { done, value } = await reader.read();
  if (done) break;

  const events = decoder.decode(value).split('\n\n');
  for (const event of events) {
    if (event.startsWith('data: ')) {
      const data = JSON.parse(event.slice(6));
      if (data.type === 'model_chunk') {
        console.log(data.data.content); // 实时渲染 markdown
      }
    }
  }
}
```

---

## 🔍 可观测性 (Observability)

### Trace ID 追踪

每个请求都会自动分配一个唯一的 `trace_id`，贯穿整个处理链路。

- **请求头**: `x-trace-id` (支持自定义传入)
- **响应头**: `x-trace-id` (返回分配的 ID)
- **日志**: 所有日志条目包含 `[trace_id=...]`

### 性能监控

日志中会自动记录关键阶段的性能指标，包括模型处理详情：

```log
# 示例日志
INFO | [trace_id=abc123] | Model performance: model=qwen-plus ttfb_ms=2345 total_ms=89745 output_chars=1650 chars_per_sec=18.40 chunks=42
```

- **ttfb_ms**: 首字时间 (Time To First Byte)
- **chars_per_sec**: 生成速度 (吞吐量)
- **total_ms**: 总处理耗时

---

## 📚 返回参数说明

| 字段 | 类型 | 说明 |
|----------|----------|----------|
| `markdown` | string | 解析后的 Markdown 文本（包含图片占位符） |
| `images` | list | 图片列表 (根据配置返回 URL 或 Base64 或不返回) |

**输出策略**:
1. **有模型**: 返回模型处理后的 markdown，`images` 列表为空（图片已由模型消费）。
2. **无模型 + OSS**: 返回 markdown 和包含 URL 的 `images` 列表。
3. **无模型 + 无 OSS**: 返回 markdown 和包含 Base64 的 `images` 列表。

---

## 🧪 开发与测试

运行 SSE 测试脚本：
```bash
python scripts/test_sse.py test_files/sample.pdf
```
