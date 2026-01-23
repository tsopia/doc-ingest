import gc
import json
import re
from typing import Any, Callable, Mapping, Optional
from urllib.parse import urlparse, urlunparse

from loguru import logger
from openai import OpenAIError
from tenacity import retry, stop_after_attempt, wait_exponential, retry_if_exception_type

from app.config import get_settings
from app.utils.observability import observe, get_openai_client
from app.utils.text_splitter import TextSplitter, TextChunk, need_chunking


class ModelService:
    IMAGE_MD_RE = re.compile(r"!\[[^\]]*]\((image://img_\d+)\)")

    def __init__(self) -> None:
        self._settings = get_settings().model
        self._client = None
        if self._settings.api_key:
            try:
                self._client = get_openai_client(
                    api_key=self._settings.api_key,
                    base_url=self._settings.base_url or None,
                )
            except Exception as e:
                logger.error(f"Failed to initialize OpenAI client: {e}")

        # 初始化文本切分器
        self._splitter = TextSplitter(
            max_tokens=getattr(self._settings, 'chunk_max_tokens', 8000),
            overlap_tokens=getattr(self._settings, 'chunk_overlap_tokens', 200)
        )

    def _clear_images_base64(self, images: list[dict]) -> None:
        """清理图片的 base64 数据以释放内存"""
        for img in images:
            if "base64" in img:
                del img["base64"]

    def _image_url_map(self, images: list[dict]) -> dict[str, str]:
        mapping: dict[str, str] = {}
        for item in images:
            image_id = item.get("id")
            if not image_id:
                continue
            url = item.get("url")
            if url:
                # Remove query parameters for cleaner logging/usage if needed,
                # but for access we usually need them if signature is there.
                # However, consistent with the script logic:
                # The script REMOVED query params. Wait, if it's OSS with signature, removing query param makes it invalid if private.
                # Let's re-read the script logic.
                # Script logic:
                # parsed = urlparse(url)
                # url_without_query = urlunparse(...)
                # This seems aggressive if the URL requires a signature.
                # BUT, if the model can access it (public bucket), it's fine.
                # If it's a private bucket with signed URL, removing query breaks it.
                # Re-evaluating: The script WAS doing it.
                # "https://synocodes-qa.oss-cn-shanghai.aliyuncs.com/.../xxx.jpg?OSSAccessKeyId=..."
                # If I strip it, the model won't be able to fetch it if it's private.
                # I will KEEP the original URL for safety, unless I know for sure.
                # Actually, in the script prompt it showed sample results, maybe the user *wanted* clean URLs in the FINAL output?
                # But here we are passing to the MODEL to READ.
                # The model needs the ACCESS token.
                # So I should use the FULL URL for the model input.
                mapping[image_id] = url
                continue

            # Fallback to base64
            base64_data = item.get("base64")
            if base64_data:
                mime = item.get("mime") or "application/octet-stream"
                mapping[image_id] = f"data:{mime};base64,{base64_data}"
        return mapping

    def _build_content(
        self, markdown: str, images: list[dict], task_prompt: str
    ) -> list[dict[str, Any]]:
        image_map = self._image_url_map(images)
        content: list[dict[str, Any]] = []
        task_prompt = task_prompt.strip()
        if task_prompt:
            content.append({"type": "text", "text": task_prompt + "\n\n"})

        last = 0
        for match in self.IMAGE_MD_RE.finditer(markdown):
            start, end = match.span()
            if start > last:
                text = markdown[last:start]
                if text.strip():
                    content.append({"type": "text", "text": text})

            placeholder = match.group(1)
            # placeholder is "image://img_n", we need "img_n"
            if "://" in placeholder:
                image_id = placeholder.split("://", 1)[1]
            else:
                image_id = placeholder

            url = image_map.get(image_id)
            if url:
                content.append({"type": "image_url", "image_url": {"url": url}})
            else:
                content.append({"type": "text", "text": f"[missing image {image_id}]"})
            last = end

        tail = markdown[last:]
        if tail.strip():
            content.append({"type": "text", "text": tail})
        return content

    def _extract_chunk_content(self, chunk: Any) -> str:
        """
        从流式响应 chunk 中提取内容

        Args:
            chunk: 流式响应的单个 chunk

        Returns:
            提取的文本内容，如果无内容则返回空字符串
        """
        try:
            # 标准 OpenAI 流式格式：chunk.choices[0].delta.content
            if hasattr(chunk, 'choices') and len(chunk.choices) > 0:
                delta = chunk.choices[0].delta
                if hasattr(delta, 'content') and delta.content:
                    return delta.content
        except (AttributeError, IndexError) as e:
            logger.warning(f"Failed to extract content from streaming chunk: {e}")

        return ""

    def _call_model_stream(self, messages: list[dict[str, Any]]):
        """
        流式调用模型 API，yield 每个文本 chunk

        Args:
            messages: 消息列表

        Yields:
            str: 模型生成的文本片段
        """
        logger.debug(f"Calling model (stream) with timeout={self._settings.timeout_seconds}s")

        try:
            response = self._client.chat.completions.create(
                model=self._settings.model_name,
                temperature=self._settings.temperature,
                messages=messages,
                timeout=self._settings.timeout_seconds,
                stream=True,  # 启用流式
            )

            for chunk in response:
                content = self._extract_chunk_content(chunk)
                if content:
                    yield content

        except OpenAIError as e:
            logger.error(f"Model streaming request failed: {e}")
            raise e
        except Exception as e:
            logger.exception(f"Unexpected error during model streaming: {e}")
            raise e

    def _call_model(self, messages: list[dict[str, Any]]) -> str:
        """
        调用模型 API，带重试机制和超时控制

        内部使用流式 API，拼接完整结果后返回。
        这样即使处理时间长，也不会超时（因为持续有数据流）。

        Args:
            messages: 消息列表

        Returns:
            完整的模型响应文本
        """
        # 动态创建重试装饰器
        retry_decorator = retry(
            stop=stop_after_attempt(self._settings.max_retries),
            wait=wait_exponential(
                min=self._settings.retry_min_wait,
                max=self._settings.retry_max_wait
            ),
            retry=retry_if_exception_type(OpenAIError),
            reraise=True,
        )

        @retry_decorator
        def _do_call():
            # 使用流式 API，内部拼接完整结果
            full_result = ""
            for chunk in self._call_model_stream(messages):
                full_result += chunk
            return full_result

        return _do_call()

    @observe(name="process_document_chunked")
    def process_document_chunked(
        self,
        markdown: str,
        images: list[dict],
        on_token: Optional[Callable[[str], None]] = None,
        on_heartbeat: Optional[Callable[[], None]] = None
    ) -> str:
        """
        分块处理文档（如果需要）

        Args:
            markdown: 文本内容（含占位符）
            images: 图片列表（可能是 URL 或 base64）
            on_token: SSE 流式输出回调（暂不使用）
            on_heartbeat: SSE 心跳回调（暂不使用）

        Returns:
            处理后的文本
        """
        # 判断是否需要切分
        if not need_chunking(markdown, images, self._settings.max_input_tokens):
            logger.info("Document is small, processing without chunking")
            result = self.process_document(markdown, images)
            # 非分块处理完成后也清理 base64
            self._clear_images_base64(images)
            return result

        logger.info("Document is large, processing with chunking")
        chunks = self._splitter.split(markdown)
        logger.info(f"Split into {len(chunks)} chunks")

        if len(chunks) == 1:
            return self.process_document(
                self._build_chunk_prompt(chunks[0]),
                self._filter_chunk_images(chunks[0].text, images)
            )

        # 串行处理每个 chunk
        results = []
        for chunk in chunks:
            # 筛选该 chunk 引用的图片
            chunk_images = self._filter_chunk_images(chunk.text, images)

            logger.info(
                f"Processing chunk {chunk.index + 1}/{chunk.total} "
                f"with {len(chunk_images)} images"
            )

            # 构建 prompt 并处理
            prompt = self._build_chunk_prompt(chunk)
            result = self.process_document(prompt, chunk_images)
            results.append(result)

            # 及时释放该 chunk 的图片内存
            self._clear_images_base64(chunk_images)
            gc.collect()

        return self._merge_results(results)

    def _filter_chunk_images(self, chunk_text: str, all_images: list[dict]) -> list[dict]:
        """
        筛选 chunk 中引用的图片

        Args:
            chunk_text: chunk 文本（含占位符）
            all_images: 全部图片列表

        Returns:
            该 chunk 引用的图片列表
        """
        # 提取 chunk 中的图片占位符 ID
        referenced_ids = set(re.findall(r'image://(img_\d+)', chunk_text))

        # 筛选图片
        chunk_images = [img for img in all_images if img.get('id') in referenced_ids]
        return chunk_images

    def _build_chunk_prompt(self, chunk: TextChunk) -> str:
        """
        构建 chunk 的 prompt

        包含位置信息，强调保留原文
        """
        if chunk.total == 1:
            # 单个 chunk，直接返回
            return chunk.text

        # 多个 chunk，添加明确的保留原文指令
        parts = [
            f"【这是文档的第 {chunk.index + 1}/{chunk.total} 部分。"
            f"请严格保持原文内容和结构，仅将图片占位符替换为图片内容描述，不要改写或重新组织内容】\n\n"
        ]

        parts.append(chunk.text)

        return "".join(parts)

    def _merge_results(self, results: list[str]) -> str:
        """
        合并多个 chunk 的结果

        简单策略：用换行拼接
        """
        # 去除可能的开头套话
        cleaned = []
        for result in results:
            # 简单去除常见套话模式
            result = result.strip()
            if result:
                cleaned.append(result)

        return "\n\n".join(cleaned)

    def process_document(
        self, markdown: str, images: list[dict]
    ) -> str:
        import time

        overall_start = time.monotonic()
        if not self._client:
            logger.debug("Model processing skipped: no client configured")
            return ""

        # Token 预估：防止超过模型上下文限制
        # 文本: ~3 字符/token (中英文混合)
        # 图片: ~170 tokens/张 (高分辨率)
        token_start = time.monotonic()
        estimated_tokens = len(markdown) // 3 + len(images) * 170
        token_elapsed = time.monotonic() - token_start

        if estimated_tokens > self._settings.max_input_tokens:
            logger.warning(
                f"Document too large: estimated {estimated_tokens} tokens, "
                f"limit {self._settings.max_input_tokens}"
            )
            raise ValueError(
                f"文档过大（预估 {estimated_tokens} tokens），"
                f"超过限制 {self._settings.max_input_tokens} tokens。"
                f"建议：拆分文档或减少图片数量"
            )

        logger.info(
            "Model processing START: estimated_tokens={} (text: {}, images: {}) token_estimation_ms={}",
            estimated_tokens,
            len(markdown)//3,
            len(images)*170,
            int(token_elapsed * 1000),
        )

        # 构建内容
        content_start = time.monotonic()
        content = self._build_content(
            markdown, images, self._settings.task_prompt
        )
        content_elapsed = time.monotonic() - content_start
        logger.debug(
            "Model content build done: content_items={} elapsed_ms={}",
            len(content),
            int(content_elapsed * 1000),
        )

        messages = []
        if self._settings.system_prompt:
            messages.append({"role": "system", "content": self._settings.system_prompt})
        messages.append({"role": "user", "content": content})

        try:
            logger.info("Sending request to model: {}", self._settings.model_name)

            # Debug log for model input messages
            debug_messages = []
            for msg in messages:
                content_copy = msg["content"]
                if isinstance(content_copy, list):
                    content_list = []
                    for item in content_copy:
                        item_copy = item.copy()
                        if item_copy.get("type") == "image_url":
                            url = item_copy["image_url"]["url"]
                            if url.startswith("data:"):
                                item_copy["image_url"]["url"] = url[:50] + "...[truncated]"
                        content_list.append(item_copy)
                    debug_messages.append({"role": msg["role"], "content": content_list})
                else:
                    debug_messages.append(msg)

            logger.debug("Model request messages: {}", json.dumps(debug_messages, ensure_ascii=False))

            # 实际调用模型 API
            api_start = time.monotonic()
            answer = self._call_model(messages)
            api_elapsed = time.monotonic() - api_start

            overall_elapsed = time.monotonic() - overall_start
            logger.info(
                "Model processing COMPLETE: response_length={} api_elapsed_ms={} total_elapsed_ms={}",
                len(answer),
                int(api_elapsed * 1000),
                int(overall_elapsed * 1000),
            )
            logger.debug("Model response content: {}", answer)
            return answer
        except OpenAIError as e:
            logger.error(f"Model request failed after retries: {e}")
            raise e
        except Exception as e:
            logger.exception(f"Unexpected error during model processing: {e}")
            raise e
