import base64
import json
import mimetypes
import os
import re
from typing import Mapping, Optional, Sequence, Tuple
from urllib.parse import unquote, urlparse

from markitdown import StreamInfo


ALLOWED_STRUCTURED_TARGETS = {"titles", "paragraphs", "tables"}
IMAGE_EXTENSIONS = {".jpg", ".jpeg", ".png", ".gif", ".webp", ".bmp", ".tif", ".tiff"}
IMAGE_DATA_URI_RE = re.compile(
    r'!\[(?P<alt>[^\]]*)\]\('
    r'(?P<src>data:(?P<mime>[^;]+);base64,(?P<b64>[A-Za-z0-9+/=]+))'
    r'(?:\s+"(?P<title>[^"]*)")?\)'
)
_PAGE_PATTERNS = [
    re.compile(r"^page\s+\d+(\s+of\s+\d+)?$", re.IGNORECASE),
    re.compile(r"^\d+\s*/\s*\d+$"),
    re.compile(r"^\d+\s+of\s+\d+$", re.IGNORECASE),
    re.compile(r"^[-_]{1,3}\s*\d+\s*[-_]{1,3}$"),
    re.compile("^\u2014\\s*\\d+\\s*\u2014$"),
    re.compile("^\u7b2c\\s*\\d+\\s*\u9875(?:\\s*/\\s*\u5171?\\s*\\d+\\s*\u9875)?$"),
]
_TABLE_SEPARATOR_RE = re.compile(
    r"^\s*\|?\s*:?-{3,}:?\s*(\|\s*:?-{3,}:?\s*)+\|?\s*$"
)


def parse_content_type(value: Optional[str]) -> Tuple[Optional[str], Optional[str]]:
    if not value:
        return None, None
    parts = [part.strip() for part in value.split(";") if part.strip()]
    if not parts:
        return None, None
    mimetype = parts[0].lower()
    charset = None
    for part in parts[1:]:
        if part.lower().startswith("charset="):
            charset = part.split("=", 1)[1].strip().strip('"')
            break
    return mimetype, charset


def filename_from_content_disposition(value: Optional[str]) -> Optional[str]:
    if not value:
        return None
    match = re.search(r"filename\*=([^']*)''([^;]+)", value, flags=re.IGNORECASE)
    if match:
        return unquote(match.group(2))
    match = re.search(r'filename="([^"]+)"', value, flags=re.IGNORECASE)
    if match:
        return match.group(1)
    match = re.search(r"filename=([^;]+)", value, flags=re.IGNORECASE)
    if match:
        return match.group(1).strip().strip('"')
    return None


def stream_info_from_url(url: str, headers: Mapping[str, str]) -> StreamInfo:
    content_type = headers.get("Content-Type")
    mimetype, charset = parse_content_type(content_type)

    filename = filename_from_content_disposition(headers.get("Content-Disposition"))
    parsed = urlparse(url)
    if not filename and parsed.path:
        filename = os.path.basename(parsed.path)

    extension = None
    if filename:
        extension = os.path.splitext(filename)[1] or None
    elif parsed.path:
        extension = os.path.splitext(parsed.path)[1] or None

    return StreamInfo(
        url=url,
        mimetype=mimetype,
        charset=charset,
        filename=filename,
        extension=extension,
    )


def parse_structured(raw: object) -> Tuple[Optional[list[str]], Optional[str]]:
    if raw is None:
        return None, None
    if isinstance(raw, list):
        candidates = raw
    elif isinstance(raw, str):
        raw = raw.strip()
        if not raw:
            return None, None
        try:
            parsed = json.loads(raw)
        except json.JSONDecodeError:
            return None, "structured must be a JSON array"
        if parsed is None:
            return None, None
        if not isinstance(parsed, list):
            return None, "structured must be a JSON array"
        candidates = parsed
    else:
        return None, "structured must be a list of strings"

    normalized = []
    seen = set()
    invalid = False
    for item in candidates:
        if not isinstance(item, str):
            invalid = True
            continue
        value = item.strip()
        if value not in ALLOWED_STRUCTURED_TARGETS:
            invalid = True
            continue
        if value not in seen:
            normalized.append(value)
            seen.add(value)

    if invalid:
        return None, "structured supports only: titles, paragraphs, tables"
    if not normalized:
        return None, None
    return normalized, None


def normalize_markdown(markdown_text: str) -> str:
    if not markdown_text:
        return ""
    normalized = markdown_text.replace("\r\n", "\n").replace("\r", "\n")
    normalized = normalized.replace("\x00", "").replace("\f", "\n")
    if normalized.startswith("\ufeff"):
        normalized = normalized.lstrip("\ufeff")
    lines = normalized.split("\n")
    lines = _strip_page_artifacts(lines)
    lines = _normalize_tables(lines)
    lines = _compress_blank_lines(lines)
    return "\n".join(lines)


def _line_col(text: str, index: int) -> Tuple[int, int]:
    line = text.count("\n", 0, index) + 1
    last_newline = text.rfind("\n", 0, index)
    if last_newline == -1:
        column = index + 1
    else:
        column = index - last_newline
    return line, column


def _strip_page_artifacts(lines: Sequence[str]) -> list[str]:
    cleaned: list[str] = []
    in_code_block = False
    for line in lines:
        stripped = line.lstrip()
        if stripped.startswith("```"):
            in_code_block = not in_code_block
            cleaned.append(line)
            continue
        if in_code_block:
            cleaned.append(line)
            continue
        if _is_page_artifact(line):
            continue
        cleaned.append(line)
    return cleaned


def _is_page_artifact(line: str) -> bool:
    if "|" in line:
        return False
    stripped = line.strip()
    if not stripped:
        return False
    for pattern in _PAGE_PATTERNS:
        if pattern.match(stripped):
            return True
    return False


def _normalize_tables(lines: Sequence[str]) -> list[str]:
    normalized: list[str] = []
    i = 0
    in_code_block = False
    total = len(lines)
    while i < total:
        line = lines[i]
        stripped = line.lstrip()
        if stripped.startswith("```"):
            in_code_block = not in_code_block
            normalized.append(line)
            i += 1
            continue
        if in_code_block:
            normalized.append(line)
            i += 1
            continue
        if i + 1 < total and "|" in line and _is_table_separator(lines[i + 1]):
            header_cells = _split_table_cells(line)
            sep_cells = _split_table_cells(lines[i + 1])
            col_count = max(len(header_cells), len(sep_cells))
            normalized.append(_normalize_table_row(line, col_count))
            normalized.append(_normalize_table_separator(lines[i + 1], col_count))
            i += 2
            while i < total and lines[i].strip() and "|" in lines[i]:
                if lines[i].lstrip().startswith("```"):
                    break
                normalized.append(_normalize_table_row(lines[i], col_count))
                i += 1
            continue
        normalized.append(line)
        i += 1
    return normalized


def _is_table_separator(line: str) -> bool:
    return bool(_TABLE_SEPARATOR_RE.match(line))


def _split_table_cells(line: str) -> list[str]:
    return [cell.strip() for cell in line.strip().strip("|").split("|")]


def _normalize_table_row(line: str, col_count: int) -> str:
    cells = _split_table_cells(line)
    if len(cells) < col_count:
        cells.extend([""] * (col_count - len(cells)))
    return "| " + " | ".join(cells) + " |"


def _normalize_table_separator(line: str, col_count: int) -> str:
    raw_cells = _split_table_cells(line)
    new_cells = []
    for i in range(col_count):
        cell = raw_cells[i] if i < len(raw_cells) else "---"
        cell = cell.strip()
        left = cell.startswith(":")
        right = cell.endswith(":")
        core = "-" * 3
        new_cells.append(f"{':' if left else ''}{core}{':' if right else ''}")
    return "| " + " | ".join(new_cells) + " |"


def _compress_blank_lines(lines: Sequence[str]) -> list[str]:
    cleaned: list[str] = []
    blank_count = 0
    in_code_block = False
    for line in lines:
        stripped = line.lstrip()
        if stripped.startswith("```"):
            in_code_block = not in_code_block
            cleaned.append(line)
            blank_count = 0
            continue
        if in_code_block:
            cleaned.append(line)
            continue
        if line.strip() == "":
            blank_count += 1
            if blank_count == 1:
                cleaned.append("")
            continue
        blank_count = 0
        cleaned.append(line)
    return cleaned


def is_image_stream(stream_info: StreamInfo) -> bool:
    mimetype = (stream_info.mimetype or "").lower()
    extension = (stream_info.extension or "").lower()
    if mimetype.startswith("image/"):
        return True
    return extension in IMAGE_EXTENSIONS


def extract_images_from_markdown(markdown_text: str) -> Tuple[str, list[dict]]:
    import time
    from loguru import logger

    start = time.monotonic()
    images: list[dict] = []

    def replace(match: re.Match) -> str:
        base64_data = match.group("b64")
        if base64_data.endswith("..."):
            return match.group(0)
        image_id = f"img_{len(images) + 1}"
        line, column = _line_col(markdown_text, match.start())
        alt = match.group("alt") or ""
        title = match.group("title")
        mime = match.group("mime").lower()
        placeholder = f"image://{image_id}"
        images.append(
            {
                "id": image_id,
                "mime": mime,
                "base64": base64_data,
                "alt": alt,
                "title": title,
                "position": {"line": line, "column": column},
                "placeholder": placeholder,
            }
        )
        title_part = f' "{title}"' if title else ""
        return f"![{alt}]({placeholder}{title_part})"

    replaced = IMAGE_DATA_URI_RE.sub(replace, markdown_text)
    elapsed = time.monotonic() - start
    logger.debug(
        "extract_images_from_markdown: images={} elapsed_ms={}",
        len(images),
        int(elapsed * 1000),
    )
    return replaced, images


def append_image_from_bytes(
    markdown_text: str,
    image_bytes: bytes,
    stream_info: StreamInfo,
) -> Tuple[str, list[dict]]:
    image_id = "img_1"
    alt = stream_info.filename or "image"
    mime = stream_info.mimetype
    if not mime:
        mime, _ = mimetypes.guess_type(alt)
    if not mime:
        mime = "application/octet-stream"
    base64_data = base64.b64encode(image_bytes).decode("utf-8")
    placeholder = f"image://{image_id}"
    image_markdown = f"![{alt}]({placeholder})"
    if markdown_text:
        markdown_text = f"{image_markdown}\n\n{markdown_text}"
    else:
        markdown_text = image_markdown
    image = {
        "id": image_id,
        "mime": mime,
        "base64": base64_data,
        "alt": alt,
        "title": None,
        "position": {"line": 1, "column": 1},
        "placeholder": placeholder,
    }
    return markdown_text, [image]


def build_structured(markdown_text: str, targets: Sequence[str]) -> dict:
    import time
    from loguru import logger

    start = time.monotonic()
    titles: list[str] = []
    paragraphs: list[str] = []
    tables: list[str] = []
    lines = markdown_text.splitlines()
    paragraph_buffer = []
    table_buffer = []
    in_table = False

    for line in lines:
        if line.startswith("#"):
            if paragraph_buffer:
                paragraphs.append(" ".join(paragraph_buffer).strip())
                paragraph_buffer = []
            titles.append(line.strip("# ").strip())
        elif re.match(r"^\|.*\|$", line):
            in_table = True
            table_buffer.append(line)
        elif in_table and not line.strip():
            in_table = False
            if table_buffer:
                tables.append("\n".join(table_buffer))
                table_buffer = []
        else:
            if line.strip():
                paragraph_buffer.append(line.strip())

    if paragraph_buffer:
        paragraphs.append(" ".join(paragraph_buffer).strip())
    if table_buffer:
        tables.append("\n".join(table_buffer))

    wanted = set(targets)
    structured = {}
    if "titles" in wanted:
        structured["titles"] = titles
    if "paragraphs" in wanted:
        structured["paragraphs"] = paragraphs
    if "tables" in wanted:
        structured["tables"] = tables

    elapsed = time.monotonic() - start
    logger.debug(
        "build_structured: targets={} titles={} paragraphs={} tables={} elapsed_ms={}",
        targets,
        len(titles),
        len(paragraphs),
        len(tables),
        int(elapsed * 1000),
    )
    return structured
