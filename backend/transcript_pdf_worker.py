"""Lightweight transcript child; parser imports occur only after containment.

Beginner note:
    Execute this file with an isolated Python interpreter. It deliberately
    imports neither the fundamentals facade nor the application's main module.
    Both parsers share the same deadline and memory boundary.
"""

from __future__ import annotations

import json
import sys
from pathlib import Path

MAX_RESULT_BYTES = 256 * 1024
MEMORY_BYTES = 512 * 1024 * 1024


def _append_limited(
    chunks: list[str],
    page_text: str,
    *,
    max_chars: int | None,
) -> bool:
    """Append text and return False once the caller has enough characters.

    Args:
        chunks: Page fragments retained so far; modified in place.
        page_text: Text from the next page, produced inside the bounded child.
        max_chars: Maximum characters including separators. None is supported
            for pure helper tests; production always supplies a bounded integer.

    Returns:
        True when another page can contribute text, otherwise False.

    Beginner note:
        Both extractors join pages with blank lines. Counting those separators
        prevents individually valid pages from exceeding the combined prompt
        budget. Stopping here saves work, while the OS limits still protect the
        extraction of a single unusually expensive page.
    """
    if not page_text:
        return True
    if max_chars is None:
        chunks.append(page_text)
        return True
    current = "\n\n".join(chunks)
    separator_len = 2 if current else 0
    remaining = max_chars - len(current) - separator_len
    if remaining <= 0:
        return False
    chunks.append(page_text[:remaining])
    return len("\n\n".join(chunks)) < max_chars


def _extract_with_pdfplumber(
    pdf_path: Path,
    *,
    max_chars: int | None = None,
    max_pages: int | None = None,
) -> str:
    """Extract the bounded leading pages with pdfplumber inside the child.

    Args:
        pdf_path: Parent-selected local PDF path.
        max_chars: Retained character limit, including page separators.
        max_pages: Maximum leading pages to visit.

    Returns:
        Joined text, or an empty string if the library is absent or parsing fails.

    Beginner note:
        Importing the parser here lets the worker install OS limits first.
        Page selection alone cannot bound compressed-object expansion, which is
        why this helper must remain behind the process launcher in production.
    """
    try:
        import pdfplumber  # type: ignore[import-untyped, unused-ignore]
    except ImportError:
        pass
        return ""

    chunks: list[str] = []
    try:
        with pdfplumber.open(str(pdf_path)) as pdf:
            pages = pdf.pages[:max_pages] if max_pages is not None else pdf.pages
            for page in pages:
                page_text = page.extract_text(x_tolerance=2, y_tolerance=2) or ""
                if not _append_limited(chunks, page_text, max_chars=max_chars):
                    break
    except Exception:  # noqa: BLE001 — extractors throw odd errors on weird PDFs
        pass
        return ""
    return "\n\n".join(chunks).strip()


def _extract_with_pypdf(
    pdf_path: Path,
    *,
    max_chars: int | None = None,
    max_pages: int | None = None,
) -> str:
    """Try the optional pypdf reader under the same child resource limits.

    Args:
        pdf_path: Parent-selected local PDF path.
        max_chars: Retained character limit, including page separators.
        max_pages: Maximum leading pages to visit.

    Returns:
        Joined text, or an empty string if pypdf is absent or cannot parse it.

    Beginner note:
        A fallback is useful for differences between PDF libraries, but it must
        not escape containment. This call shares the first parser's process,
        remaining wall time, memory ceiling, and text/page limits.
    """
    try:
        from pypdf import PdfReader  # type: ignore[import-untyped, unused-ignore]
    except ImportError:
        return ""

    try:
        reader = PdfReader(str(pdf_path))
        pages = reader.pages[:max_pages] if max_pages is not None else reader.pages
        chunks: list[str] = []
        for page in pages:
            if not _append_limited(chunks, page.extract_text() or "", max_chars=max_chars):
                break
        return "\n\n".join(chunks).strip()
    except Exception:  # noqa: BLE001
        pass
        return ""


def extract_payload(pdf_path: Path, *, max_chars: int, max_pages: int) -> bytes:
    """Extract bounded text with both parsers inside the same worker.

    Args:
        pdf_path: Parent-selected PDF, opened only after the start grant.
        max_chars: Validated retained text ceiling, at most 40,000 characters.
        max_pages: Validated leading-page ceiling, at most 30 pages.

    Returns:
        UTF-8 JSON containing only text, bounded to 256 KiB. An oversized result
        is replaced by an empty-text receipt so it cannot become evidence.

    Beginner note:
        Pure unit tests may call this helper with fake parser modules. Production
        reaches it only through main after the operating-system limits succeed.
    """
    text = _extract_with_pdfplumber(pdf_path, max_chars=max_chars, max_pages=max_pages)
    if not text:
        text = _extract_with_pypdf(pdf_path, max_chars=max_chars, max_pages=max_pages)
    encoded = json.dumps({"text": text}, ensure_ascii=False).encode("utf-8")
    return encoded if len(encoded) <= MAX_RESULT_BYTES else b'{"text":""}'


def _install_memory_limit() -> None:
    """Fail closed unless this supported platform has its memory policy active.

    Raises:
        OSError: Resource limits cannot be installed or the OS is unsupported.
        ValueError: The OS rejects the requested resource-limit values.

    Beginner note:
        Linux sets RLIMIT_AS before pdfplumber/pypdf imports. Windows relies on
        the parent's Job Object and cannot proceed until the parent grants its
        token. The file-size limit also bounds a faulty result writer on Linux.
    """
    if sys.platform == "linux":
        import resource

        resource.setrlimit(resource.RLIMIT_AS, (MEMORY_BYTES, MEMORY_BYTES))
        resource.setrlimit(resource.RLIMIT_FSIZE, (MAX_RESULT_BYTES, MAX_RESULT_BYTES))
    elif sys.platform != "win32":
        raise OSError("PDF memory containment unavailable")


def main() -> None:
    """Wait for containment, parse, then write one bounded primitive receipt.

    Beginner note:
        EOF or an absent start token means the parent failed or could not attach
        the Windows job. The PDF is never opened on either path. A private
        result file avoids blocking the parent on a partial pipe message.
    """
    if sys.stdin.buffer.read(3) != b"GO\n":
        return
    try:
        _install_memory_limit()
        pdf_path, result_path, chars, pages = sys.argv[1:]
        max_chars, max_pages = int(chars), int(pages)
        if not (0 < max_chars <= 40000 and 0 < max_pages <= 30):
            return
        result = extract_payload(Path(pdf_path), max_chars=max_chars, max_pages=max_pages)
        Path(result_path).write_bytes(result)
    except Exception:  # noqa: BLE001 - no parser-controlled exception escapes
        return


if __name__ == "__main__":
    main()
