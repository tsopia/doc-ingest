## Doc Ingest Service

基于 FastAPI + MarkItDown 的文档解析服务，支持：
- URL 下载解析
- 文件上传解析
- 可选结构化结果（titles/paragraphs/tables）
- 可选图片抽取（data URI -> 占位符 + images 列表）

---

## 接口

### 1) URL 解析
`POST /convert/url`

请求体（JSON）：
```json
{
  "url": "https://example.com/file.docx",
  "structured": ["titles", "paragraphs", "tables"],
  "keep_data_uris": true,
  "extract_images": true
}
```

字段说明：
- `url`：必填，http/https 可下载地址
- `structured`：可选，结构化目标数组，仅支持 `titles/paragraphs/tables`
- `keep_data_uris`：可选，默认 `true`，保留 data URI（图片 base64）
- `extract_images`：可选，默认 `true`，提取图片并替换为占位符

### 2) 文件上传解析
`POST /convert/file`

表单字段（multipart/form-data）：
- `file`：必填，上传文件
- `structured`：可选，JSON 数组字符串，例如 `["titles","tables"]`
- `keep_data_uris`：可选，默认 `true`
- `extract_images`：可选，默认 `true`

curl 示例：
```bash
curl -X POST "http://localhost:8000/convert/file" \
  -F "file=@/path/to/sample.docx" \
  -F 'structured=["titles","paragraphs"]' \
  -F "keep_data_uris=true" \
  -F "extract_images=true"
```

---

## 返回结构

统一返回格式：
```json
{
  "code": 0,
  "data": {},
  "msg": ""
}
```

`data` 字段内容：
- `markdown`：MarkItDown 解析后的 Markdown 文本
- `structured`（可选）：结构化结果对象（由 `structured` 控制）
- `images`（可选）：图片列表（由 `extract_images` 控制）

### structured 结构示例
```json
{
  "titles": ["标题1", "标题2"],
  "paragraphs": ["段落1", "段落2"],
  "tables": ["|a|b|", "|1|2|"]
}
```

### images 结构示例
```json
[
  {
    "id": "img_1",
    "mime": "image/jpeg",
    "base64": "...",
    "url": "https://oss-example/xxx?Expires=...",
    "url_expires_in": 1800,
    "alt": "logo",
    "title": null,
    "position": { "line": 12, "column": 5 },
    "placeholder": "image://img_1"
  }
]
```

说明：
- 文本中的图片会被替换为 `![alt](image://img_n)` 占位符
- `images` 列表中包含图片的位置信息，未启用 OSS 时包含 `base64`
- 若配置 OSS，会返回 `url`（30 分钟有效），并移除 `base64`
- `base64` 会显著增大响应体积（约 +33%），大文件建议关闭 `extract_images`

---

## 多模态示例（顺序交替）

当你要把结果交给多模态模型时，推荐“文本 + 图片交替输入”的方式：

```json
{
  "messages": [
    {
      "role": "user",
      "content": [
        { "type": "text", "text": "第一段文本... image://img_1 ..." },
        { "type": "image_url", "image_url": { "url": "data:image/jpeg;base64,BASE64_1" } },
        { "type": "text", "text": "第二段文本... image://img_2 ..." },
        { "type": "image_url", "image_url": { "url": "data:image/png;base64,BASE64_2" } },
        { "type": "text", "text": "第三段文本... image://img_3 ..." },
        { "type": "image_url", "image_url": { "url": "data:image/jpeg;base64,BASE64_3" } }
      ]
    }
  ]
}
```

要点：
- `markdown` 里用占位符标记图片位置（例如 `image://img_1`）
- `images` 列表提供 `id -> base64/url` 的对应关系
- 发送给模型时按顺序交替插入即可

---

## 缓存策略

为了避免短时间内重复解析相同文件，服务端提供内存缓存：
- URL 请求：以 `url + 参数` 作为缓存键（TTL 到期自动失效）
- 文件上传：对小文件计算内容哈希作为缓存键
- 仅缓存小文件（默认 `<= 5MB`），超出则不缓存以避免内存压力
- 多进程部署时为“每个 worker 独立缓存”

可通过环境变量调整（支持 `.env`，使用统一前缀 `DOC_INGEST__`）：
- `DOC_INGEST__CACHE__ENABLED`：是否启用缓存（默认 `true`）
- `DOC_INGEST__CACHE__TTL_SECONDS`：缓存 TTL 秒数（默认 `300`）
- `DOC_INGEST__CACHE__MAX_ENTRIES`：缓存最大条目数（默认 `256`）
- `DOC_INGEST__CACHE__MAX_BYTES`：缓存最大文件字节数（默认 `5242880`）
示例：`DOC_INGEST__CACHE__TTL_SECONDS=300`

下载配置：
- `DOC_INGEST__DOWNLOAD__TIMEOUT_SECONDS`：下载超时秒数（默认 `30`）

---

## OSS 图片上传

当启用图片抽取时，服务可将图片上传至 OSS 并返回临时下载地址（默认 30 分钟）。

环境变量（支持 `.env`，使用统一前缀 `DOC_INGEST__`）：
- `DOC_INGEST__OSS__ENDPOINT`：OSS Endpoint，例如 `https://oss-cn-hangzhou.aliyuncs.com`
- `DOC_INGEST__OSS__ACCESS_KEY_ID`：AccessKey ID
- `DOC_INGEST__OSS__ACCESS_KEY_SECRET`：AccessKey Secret
- `DOC_INGEST__OSS__BUCKET`：Bucket 名称
- `DOC_INGEST__OSS__PREFIX`：对象前缀（默认 `doc-ingest/`）
- `DOC_INGEST__OSS__URL_TTL_SECONDS`：下载地址有效期秒数（默认 `1800`）
- `DOC_INGEST__OSS__SECURE`：是否使用 https（默认 `true`）
示例：`DOC_INGEST__OSS__BUCKET=your-bucket`

说明：
- 若未配置 OSS 相关环境变量，则不上传，仍返回 `base64`
- 若 `CACHE_TTL_SECONDS` 大于 `OSS_URL_TTL_SECONDS`，缓存会自动跳过以避免返回过期链接

---

## 运行

```bash
uvicorn app.main:app --reload
```
