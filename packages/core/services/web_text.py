"""Convert fetched web content (HTML, PDF bytes) into readable text.

Service-level so both runtime tools (``web_fetch``) and services (workspace
draft link sources) share one conversion path without services importing
tool modules.
"""
from __future__ import annotations

import re
from html import unescape

_RE_SCRIPT_STYLE = re.compile(
    r"<(script|style|nav|footer|header|noscript)[^>]*>.*?</\1>",
    re.DOTALL | re.IGNORECASE,
)
_RE_COMMENT = re.compile(r"<!--.*?-->", re.DOTALL)
_RE_HEADINGS = {i: re.compile(rf"<h{i}[^>]*>(.*?)</h{i}>", re.DOTALL | re.IGNORECASE) for i in range(1, 7)}
_RE_BOLD = re.compile(r"<(b|strong)[^>]*>(.*?)</\1>", re.DOTALL | re.IGNORECASE)
_RE_ITALIC = re.compile(r"<(i|em)[^>]*>(.*?)</\1>", re.DOTALL | re.IGNORECASE)
_RE_CODE_BLOCK = re.compile(r"<pre[^>]*><code[^>]*>(.*?)</code></pre>", re.DOTALL | re.IGNORECASE)
_RE_CODE_INLINE = re.compile(r"<code[^>]*>(.*?)</code>", re.DOTALL | re.IGNORECASE)
_RE_LINK = re.compile(r'<a[^>]+href="([^"]*)"[^>]*>(.*?)</a>', re.DOTALL | re.IGNORECASE)
_RE_IMG = re.compile(r'<img[^>]+alt="([^"]*)"[^>]+src="([^"]*)"[^>]*/?\s*>', re.IGNORECASE)
_RE_LI = re.compile(r"<li[^>]*>(.*?)</li>", re.DOTALL | re.IGNORECASE)
_RE_BR = re.compile(r"<br\s*/?>", re.IGNORECASE)
_RE_P_OPEN = re.compile(r"<p[^>]*>", re.IGNORECASE)
_RE_P_CLOSE = re.compile(r"</p>", re.IGNORECASE)
_RE_HR = re.compile(r"<hr[^>]*/?>", re.IGNORECASE)
_RE_TAG = re.compile(r"<[^>]+>")
_RE_BLANK_LINES = re.compile(r"\n{3,}")


def html_to_text(html: str, as_markdown: bool = True) -> str:
    """Convert HTML to clean text or markdown using pre-compiled regexes."""
    text = _RE_SCRIPT_STYLE.sub("", html)
    text = _RE_COMMENT.sub("", text)

    if as_markdown:
        for i in range(1, 7):
            text = _RE_HEADINGS[i].sub(rf"\n{'#' * i} \1\n", text)
        text = _RE_BOLD.sub(r"**\2**", text)
        text = _RE_ITALIC.sub(r"*\2*", text)
        text = _RE_CODE_BLOCK.sub(r"\n```\n\1\n```\n", text)
        text = _RE_CODE_INLINE.sub(r"`\1`", text)
        text = _RE_LINK.sub(r"[\2](\1)", text)
        text = _RE_IMG.sub(r"![\1](\2)", text)
        text = _RE_LI.sub(r"\n- \1", text)
        text = _RE_BR.sub("\n", text)
        text = _RE_P_OPEN.sub("\n\n", text)
        text = _RE_P_CLOSE.sub("", text)
        text = _RE_HR.sub("\n---\n", text)

    text = _RE_TAG.sub("", text)
    text = unescape(text)
    text = _RE_BLANK_LINES.sub("\n\n", text)
    return text.strip()


def extract_pdf_text(content: bytes, max_pages: int = 50) -> str:
    """Extract text from PDF bytes."""
    try:
        import io
        from pypdf import PdfReader

        reader = PdfReader(io.BytesIO(content))
        pages = []
        for i, page in enumerate(reader.pages[:max_pages]):
            page_text = page.extract_text() or ""
            if page_text.strip():
                pages.append(f"--- Page {i + 1} ---\n{page_text.strip()}")
        return "\n\n".join(pages) if pages else "(no text extracted from PDF)"
    except ImportError:
        return "(pypdf not installed — cannot extract PDF text)"
    except Exception as e:
        return f"(PDF extraction failed: {e})"
