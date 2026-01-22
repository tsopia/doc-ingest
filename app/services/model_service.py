import json
import re
from typing import Any, Mapping
from urllib.parse import urlparse, urlunparse

from loguru import logger
from openai import OpenAI, OpenAIError
from tenacity import retry, stop_after_attempt, wait_exponential, retry_if_exception_type

from app.config import get_settings


class ModelService:
    IMAGE_MD_RE = re.compile(r"!\[[^\]]*]\((image://img_\d+)\)")

    def __init__(self) -> None:
        self._settings = get_settings().model
        self._client = None
        if self._settings.api_key:
            try:
                self._client = OpenAI(
                    api_key=self._settings.api_key,
                    base_url=self._settings.base_url or None,
                )
            except Exception as e:
                logger.error(f"Failed to initialize OpenAI client: {e}")

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
