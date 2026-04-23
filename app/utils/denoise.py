"""代码版 Markdown 去噪模块。

规则清洗：删除 OCR 噪声、提示词泄漏、空 block、多余空行。
仅用于 OCR 模式的后处理。
"""

import re
from dataclasses import dataclass, field

from loguru import logger


@dataclass
class CleanResult:
    """去噪结果"""

    markdown: str
    stats: dict = field(default_factory=dict)


# ---------------------------------------------------------------------------
# 噪声模式定义
# ---------------------------------------------------------------------------

# 固定无效 OCR 输出（整行匹配）
_NOISE_SENTENCES: list[str] = [
    "There is no text in the image.",
    "There is no text in the image",
    "There is no extractable text in the image.",
    "There is no extractable text in the image",
    "There is no visible text in the image.",
    "There is no visible text in the image",
    "No text found in the image.",
    "No text found in the image",
    "No text found.",
    "No text found",
    "None",
    "N/A",
    "图片中没有文字",
    "图片中没有可提取的文字",
    "该图片中没有文字内容",
    "此图片不包含文字",
]

# 提示词泄漏片段（部分匹配）
_PROMPT_LEAK_PATTERNS: list[str] = [
    "Extract all text from this image",
    "Return ONLY the extracted text",
    "maintaining the original layout and order",
    "Do not add any commentary or description",
]

# 编译为正则（不区分大小写，整行匹配噪声句子）
_NOISE_RE = re.compile(
    r"^\s*(" + "|".join(re.escape(s) for s in _NOISE_SENTENCES) + r")\s*\.?\s*$",
    re.IGNORECASE,
)

_PROMPT_LEAK_RE = re.compile(
    "|".join(re.escape(p) for p in _PROMPT_LEAK_PATTERNS),
    re.IGNORECASE,
)

# 空 OCR block 模式（如果插件输出包装块）
# 匹配类似 *[Image OCR] ... [End OCR]* 中内容为空的情况
_EMPTY_OCR_BLOCK_RE = re.compile(
    r"\*?\[Image\s+OCR\]\*?\s*\n?\s*\*?\[End\s+OCR\]\*?",
    re.IGNORECASE,
)

# 连续重复分隔线（3 个以上 --- / === / *** 连续出现）
_REPEATED_SEPARATOR_RE = re.compile(
    r"(?:^[ \t]*(?:[-]{3,}|[=]{3,}|[*]{3,})[ \t]*\n){2,}",
    re.MULTILINE,
)


def clean_markdown_v1(markdown: str) -> CleanResult:
    """代码版去噪（规则清洗）。

    Args:
        markdown: OCR 模式输出的原始 markdown

    Returns:
        CleanResult: 去噪后的 markdown 和统计信息
    """
    if not markdown:
        return CleanResult(markdown="", stats={})

    noise_removed = 0
    prompt_leaks_removed = 0
    empty_blocks_removed = 0

    # 1. 删除空 OCR block
    markdown, count = _EMPTY_OCR_BLOCK_RE.subn("", markdown)
    empty_blocks_removed += count

    # 2. 逐行处理
    lines = markdown.split("\n")
    cleaned_lines: list[str] = []
    in_code_block = False

    for line in lines:
        stripped = line.lstrip()

        # 保护代码块内容
        if stripped.startswith("```"):
            in_code_block = not in_code_block
            cleaned_lines.append(line)
            continue

        if in_code_block:
            cleaned_lines.append(line)
            continue

        # 检查噪声句子（整行匹配）
        if _NOISE_RE.match(line):
            noise_removed += 1
            continue

        # 检查提示词泄漏（整行包含泄漏片段则删除整行）
        if _PROMPT_LEAK_RE.search(line):
            prompt_leaks_removed += 1
            continue

        cleaned_lines.append(line)

    markdown = "\n".join(cleaned_lines)

    # 3. 清理重复分隔线（保留一个）
    def _keep_one_separator(m: re.Match) -> str:
        # 保留匹配中的第一行
        first_line = m.group(0).strip().split("\n")[0]
        return first_line + "\n"

    markdown = _REPEATED_SEPARATOR_RE.sub(_keep_one_separator, markdown)

    # 4. 压缩多余空行（连续 >1 个空行 → 1 个）
    markdown = re.sub(r"\n{3,}", "\n\n", markdown)

    # 5. 去除首尾空白
    markdown = markdown.strip()

    stats = {
        "noise_sentences_removed": noise_removed,
        "prompt_leaks_removed": prompt_leaks_removed,
        "empty_blocks_removed": empty_blocks_removed,
    }

    return CleanResult(markdown=markdown, stats=stats)


# ---------------------------------------------------------------------------
# AI 去噪触发逻辑
# ---------------------------------------------------------------------------

# 二次扫描残留的疑似噪声模式（松散匹配，用于决定是否触发 AI 去噪）
# 1. 包含 image 和 text 的短句（疑似 OCR 空图说明变体）
# 2. 包含 extract/return 和 text 的短句（疑似提示词泄漏变体）
_SUSPICIOUS_NOISE_RE = re.compile(
    r"(?:image.*text|text.*image|extract.*text|return.*text)",
    re.IGNORECASE
)


def should_ai_denoise(stats: dict, markdown: str) -> tuple[bool, str]:
    """判断是否需要触发 AI 去噪。
    
    Args:
        stats: clean_markdown_v1 返回的统计信息
        markdown: v1 去噪后的 markdown 文本
        
    Returns:
        tuple[bool, str]: (是否触发, 触发原因)
    """
    # 规则 1：v1 删了较多噪声句子（说明原始 OCR 质量很差，可能有漏网之鱼）
    noise_removed = stats.get("noise_sentences_removed", 0)
    if noise_removed >= 3:
        logger.info(f"Triggering AI denoise: {noise_removed} noise sentences removed in v1")
        return True, f"noise_sentences_removed >= 3 ({noise_removed})"
        
    # 规则 2：存在提示词泄漏（模型行为异常）
    leaks_removed = stats.get("prompt_leaks_removed", 0)
    if leaks_removed > 0:
        logger.info(f"Triggering AI denoise: {leaks_removed} prompt leaks removed in v1")
        return True, f"prompt_leaks_removed > 0 ({leaks_removed})"
        
    # 规则 3：文本中残留疑似噪声模式
    lines = markdown.split("\n")
    short_suspicious_lines = 0
    short_line_streak = 0
    max_short_line_streak = 0
    
    for line in lines:
        stripped = line.strip()
        if not stripped:
            short_line_streak = 0
            continue
            
        # 检测类似 "There is no text in this image" 的变体（一般比较短）
        if len(stripped) < 80 and _SUSPICIOUS_NOISE_RE.search(stripped):
            short_suspicious_lines += 1
            if short_suspicious_lines >= 1:  # 只要发现1行疑似噪声就触发
                logger.info(f"Triggering AI denoise: suspicious noise pattern found: '{stripped[:30]}...'")
                return True, "suspicious noise pattern found"
                
        # 检测连续极短行（可能是一堆乱码或碎裂的单词），排除列表和标题
        if len(stripped) < 10 and not stripped.startswith(("- ", "* ", "#", ">")):
            short_line_streak += 1
            max_short_line_streak = max(max_short_line_streak, short_line_streak)
        else:
            short_line_streak = 0
            
    if max_short_line_streak >= 3:
        logger.info(f"Triggering AI denoise: max_short_line_streak = {max_short_line_streak}")
        return True, f"max_short_line_streak >= 3 ({max_short_line_streak})"
        
    return False, ""
