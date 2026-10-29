import re

import markdown as markdown_lib
import nh3

_SENTENCE_END = re.compile(r"[.!?…][\"')\]]*\s")


def render_markdown(value: str | None) -> str:
    """Markdown to HTML, sanitised. Model output is never trusted."""
    if not value:
        return ""
    html = markdown_lib.markdown(value, extensions=["tables", "sane_lists", "nl2br"])
    return nh3.clean(html)


def split_text(text: str, chunk_chars: int) -> list[str]:
    """Split on paragraph boundaries into chunks no larger than chunk_chars."""
    if chunk_chars <= 0 or len(text) <= chunk_chars:
        return [text]

    chunks: list[str] = []
    current = ""
    for paragraph in text.split("\n\n"):
        block = paragraph if len(paragraph) <= chunk_chars else None
        if block is None:
            if current:
                chunks.append(current)
                current = ""
            chunks.extend(_wrap(paragraph, chunk_chars))
            continue
        candidate = f"{current}\n\n{block}" if current else block
        if len(candidate) > chunk_chars:
            chunks.append(current)
            current = block
        else:
            current = candidate
    if current:
        chunks.append(current)
    return chunks


def _wrap(paragraph: str, limit: int) -> list[str]:
    """Cut one oversized paragraph at sentence ends, else at spaces.

    A single-speaker transcript arrives as one paragraph, so a plain slice cut
    names and figures in half at every chunk boundary. A cut is only moved back
    as far as half the limit, so a chunk never shrinks to a sliver.
    """
    parts = []
    while len(paragraph) > limit:
        window = paragraph[:limit + 1]
        sentence_ends = [m.end() for m in _SENTENCE_END.finditer(window, limit // 2)]
        cut = sentence_ends[-1] if sentence_ends else window.rfind(" ", limit // 2) + 1 or limit
        parts.append(paragraph[:cut].rstrip())
        paragraph = paragraph[cut:].lstrip()
    if paragraph:
        parts.append(paragraph)
    return parts


def format_bytes(size: int | float | None) -> str:
    if not size:
        return "—"
    for unit in ("B", "KB", "MB", "GB"):
        if size < 1024 or unit == "GB":
            return f"{size:.0f} {unit}" if unit == "B" else f"{size:.1f} {unit}"
        size /= 1024
    return f"{size:.1f} GB"
