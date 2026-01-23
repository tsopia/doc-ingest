"""文本切分工具，用于将大文档按语义边界切分为多个 chunk"""

import re
from dataclasses import dataclass
from typing import Optional

from loguru import logger


@dataclass
class TextChunk:
    """文本块"""
    text: str           # chunk 主体文本（含占位符）
    context: str        # 前一个 chunk 的尾部上下文（overlap）
    index: int          # chunk 索引（从 0 开始）
    total: int          # 总 chunk 数


def need_chunking(markdown: str, images: list[dict], max_tokens: int) -> bool:
    """
    判断是否需要切分

    估算规则：
    - 文本: ~3 字符/token（中英文混合）
    - 图片: ~170 tokens/张

    Args:
        markdown: 待处理的 markdown 文本（可能含占位符）
        images: 图片列表
        max_tokens: 最大 token 限制

    Returns:
        True 表示需要切分
    """
    text_tokens = len(markdown) // 3
    image_tokens = len(images) * 170
    estimated_tokens = text_tokens + image_tokens

    # 留 30% 余量给 system_prompt 和 task_prompt
    threshold = int(max_tokens * 0.7)

    logger.debug(
        f"Token estimation: text={text_tokens}, images={image_tokens}, "
        f"total={estimated_tokens}, threshold={threshold}"
    )

    return estimated_tokens > threshold


class TextSplitter:
    """
    文本切分器

    切分规则：
    1. 优先按 Heading 边界（#、##、###）
    2. 其次按段落边界（空行）
    3. 强制切分时回退到上一个完整段落
    4. 图片占位符必须完整在 chunk 内，不能切开
    5. overlap 只含纯文本，不含图片占位符
    """

    # 匹配 heading（行首的 # 标记）
    HEADING_RE = re.compile(r'^(#{1,6})\s+.+$', re.MULTILINE)

    # 匹配图片占位符（MarkItDown 生成或手动添加）
    IMAGE_PLACEHOLDER_RE = re.compile(r'!\[([^\]]*)\]\(image://[^)]+\)')

    def __init__(self, max_tokens: int = 8000, overlap_tokens: int = 200):
        """
        Args:
            max_tokens: 每个 chunk 的最大 token 数
            overlap_tokens: chunk 间的重叠 token 数（用于上下文）
        """
        self.max_tokens = max_tokens
        self.overlap_tokens = overlap_tokens

    def split(self, markdown: str) -> list[TextChunk]:
        """
        切分 markdown 文本

        Args:
            markdown: 待切分的文本（可能含图片占位符）

        Returns:
            TextChunk 列表，至少返回一个 chunk
        """
        if not markdown or not markdown.strip():
            return [TextChunk(text="", context="", index=0, total=1)]

        # 按 heading 切分
        sections = self._split_by_headings(markdown)

        # 对每个 section 按 token 限制进一步切分
        chunks = []
        for section in sections:
            chunks.extend(self._split_section(section))

        # 如果没有任何切分，返回整个文本
        if not chunks:
            chunks = [markdown]

        # 构建 TextChunk 对象，添加 overlap
        result = []
        for i, chunk_text in enumerate(chunks):
            context = ""
            if i > 0:
                # 从前一个 chunk 尾部提取 overlap（纯文本，不含图片）
                context = self._extract_overlap(chunks[i - 1])

            result.append(TextChunk(
                text=chunk_text,
                context=context,
                index=i,
                total=len(chunks)
            ))

        logger.info(f"Split document into {len(result)} chunks")
        return result

    def _split_by_headings(self, markdown: str) -> list[str]:
        """
        按 heading 边界切分

        Returns:
            section 列表，每个 section 是从一个 heading 到下一个 heading 之前的内容
        """
        matches = list(self.HEADING_RE.finditer(markdown))
        if not matches:
            return [markdown]

        sections = []
        for i, match in enumerate(matches):
            start = match.start()
            end = matches[i + 1].start() if i + 1 < len(matches) else len(markdown)
            section = markdown[start:end].strip()
            if section:
                sections.append(section)

        # 如果第一个 heading 之前有内容，加入
        if matches[0].start() > 0:
            prefix = markdown[:matches[0].start()].strip()
            if prefix:
                sections.insert(0, prefix)

        return sections

    def _split_section(self, section: str) -> list[str]:
        """
        对单个 section 按 token 限制切分

        如果 section 本身小于限制，直接返回；
        否则按段落边界切分
        """
        estimated_tokens = len(section) // 3
        if estimated_tokens <= self.max_tokens:
            return [section]

        # 按段落切分
        paragraphs = self._split_by_paragraphs(section)

        chunks = []
        current_chunk = []
        current_tokens = 0

        for para in paragraphs:
            para_tokens = len(para) // 3

            # 检查段落是否包含图片占位符（图片段落不能切开）
            has_image = bool(self.IMAGE_PLACEHOLDER_RE.search(para))

            if current_tokens + para_tokens <= self.max_tokens:
                current_chunk.append(para)
                current_tokens += para_tokens
            else:
                # 当前段落加入会超限
                if current_chunk:
                    chunks.append("\n\n".join(current_chunk))

                # 单个段落超限的情况
                if para_tokens > self.max_tokens and not has_image:
                    # 强制按句子切分（仅限纯文本段落）
                    sub_chunks = self._split_by_sentences(para)
                    chunks.extend(sub_chunks)
                    current_chunk = []
                    current_tokens = 0
                else:
                    # 包含图片或段落可接受，作为单独 chunk
                    current_chunk = [para]
                    current_tokens = para_tokens

        # 最后一个 chunk
        if current_chunk:
            chunks.append("\n\n".join(current_chunk))

        return chunks

    def _split_by_paragraphs(self, text: str) -> list[str]:
        """按空行切分段落"""
        paragraphs = [p.strip() for p in text.split("\n\n") if p.strip()]
        return paragraphs

    def _split_by_sentences(self, paragraph: str) -> list[str]:
        """
        强制按句子切分（最后手段）

        用于单个段落超限且不含图片的情况
        """
        # 简单按句号、问号、感叹号切分
        sentences = re.split(r'([。！？\.\!\?])', paragraph)

        chunks = []
        current = []
        current_tokens = 0

        for i in range(0, len(sentences), 2):
            sentence = sentences[i]
            if i + 1 < len(sentences):
                sentence += sentences[i + 1]  # 加上标点

            sentence = sentence.strip()
            if not sentence:
                continue

            sent_tokens = len(sentence) // 3
            if current_tokens + sent_tokens <= self.max_tokens:
                current.append(sentence)
                current_tokens += sent_tokens
            else:
                if current:
                    chunks.append("".join(current))
                current = [sentence]
                current_tokens = sent_tokens

        if current:
            chunks.append("".join(current))

        return chunks if chunks else [paragraph]

    def _extract_overlap(self, previous_chunk: str) -> str:
        """
        从前一个 chunk 尾部提取 overlap

        规则：
        1. 只提取纯文本
        2. 不包含图片占位符
        3. 大约 overlap_tokens 的长度
        """
        # 估算字符数
        overlap_chars = self.overlap_tokens * 3

        if len(previous_chunk) <= overlap_chars:
            tail = previous_chunk
        else:
            tail = previous_chunk[-overlap_chars:]

        # 去除图片占位符
        tail_clean = self.IMAGE_PLACEHOLDER_RE.sub("", tail)

        # 从第一个完整句子开始（避免截断句子）
        # 简单处理：找第一个句号/换行后的位置
        match = re.search(r'[。\.\n]', tail_clean)
        if match:
            tail_clean = tail_clean[match.end():].strip()

        return tail_clean.strip()
