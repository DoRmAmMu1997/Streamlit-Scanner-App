from __future__ import annotations

"""Tests for the concall transcript PDF downloader / extractor.

No live HTTP is involved. `requests.Session.get` is monkey-patched, and
`pdfplumber.open` is patched to return a fake document so the test does
not depend on having a real PDF on disk.
"""

from contextlib import contextmanager
from pathlib import Path
from types import SimpleNamespace
from typing import Iterable

import pytest

from backend.fundamentals import pdf_reader


# ---------------------------------------------------------------------------
# _safe_filename
# ---------------------------------------------------------------------------


def test_safe_filename_strips_query_strings_and_extensions():
    stem = pdf_reader._safe_filename("https://bse2gw.tatva.in/files/abc.pdf?token=xyz")
    assert stem.startswith("abc_")
    assert not stem.endswith(".pdf")


def test_safe_filename_replaces_unsafe_characters():
    stem = pdf_reader._safe_filename("https://example.com/path/with spaces & symbols!.pdf")
    # No unsafe characters in the resulting stem.
    assert " " not in stem
    assert "&" not in stem
    assert "!" not in stem


def test_safe_filename_digest_is_deterministic_hex():
    # The collision-avoidance suffix must be a stable hashlib hex digest, NOT
    # the builtin hash() (which is salted per-process and would silently break
    # the on-disk PDF cache across restarts).
    import re

    url = "https://bse.example.com/concall/q1fy26.pdf?token=abc"
    stem = pdf_reader._safe_filename(url)
    assert re.search(r"_[0-9a-f]{10}$", stem), stem
    assert pdf_reader._safe_filename(url) == stem


# ---------------------------------------------------------------------------
# download_pdf
# ---------------------------------------------------------------------------


class _FakeSession:
    """Minimal stand-in for requests.Session — records GETs and returns canned bytes."""

    def __init__(self, status: int = 200, body: bytes = b"%PDF-fake"):
        self.status = status
        self.body = body
        self.calls: list[str] = []

    def get(self, url, **kwargs):
        self.calls.append(url)
        return SimpleNamespace(status_code=self.status, content=self.body)

    def close(self):
        pass


def test_download_pdf_writes_file_and_is_idempotent(tmp_path: Path):
    session = _FakeSession(status=200, body=b"%PDF-1.4 fake bytes")
    url = "https://bse.example.com/concall/q1fy26.pdf"

    path1 = pdf_reader.download_pdf(url, cache_dir=tmp_path, session=session)
    assert path1 is not None
    assert path1.exists()
    assert path1.read_bytes().startswith(b"%PDF-1.4")
    assert len(session.calls) == 1

    # Second call: cache hit, no new HTTP request.
    path2 = pdf_reader.download_pdf(url, cache_dir=tmp_path, session=session)
    assert path2 == path1
    assert len(session.calls) == 1, "Second download_pdf call should hit the cache"


def test_download_pdf_returns_none_on_404(tmp_path: Path):
    session = _FakeSession(status=404, body=b"")
    path = pdf_reader.download_pdf(
        "https://example.com/missing.pdf", cache_dir=tmp_path, session=session
    )
    assert path is None


def test_download_pdf_returns_none_on_empty_body(tmp_path: Path):
    session = _FakeSession(status=200, body=b"")
    path = pdf_reader.download_pdf(
        "https://example.com/empty.pdf", cache_dir=tmp_path, session=session
    )
    assert path is None


def test_download_pdf_rejects_non_http_scheme(tmp_path: Path):
    # file:// (and any non-http scheme) must be refused before any fetch,
    # closing an SSRF / local-file-read avenue. Returns None, makes no GET,
    # and writes nothing.
    session = _FakeSession(status=200, body=b"should-not-be-read")
    assert (
        pdf_reader.download_pdf("file:///etc/passwd", cache_dir=tmp_path, session=session)
        is None
    )
    assert session.calls == []
    assert list(tmp_path.iterdir()) == []


# ---------------------------------------------------------------------------
# extract_text — pdfplumber and pypdf paths
# ---------------------------------------------------------------------------


class _FakePdfPage:
    def __init__(self, text: str):
        self._text = text

    def extract_text(self, **_kwargs):  # noqa: ANN001
        return self._text


class _FakePdfDoc:
    def __init__(self, pages_text: Iterable[str]):
        self.pages = [_FakePdfPage(t) for t in pages_text]

    def __enter__(self):
        return self

    def __exit__(self, *_args):
        return False


@contextmanager
def _patched_pdfplumber(monkeypatch, pages_text: Iterable[str] | None = None):
    """Make `pdfplumber.open` return a fake document with the given page text."""
    import pdfplumber  # type: ignore[import-untyped]

    def _fake_open(_path):
        if pages_text is None:
            raise RuntimeError("simulated pdfplumber failure")
        return _FakePdfDoc(pages_text)

    monkeypatch.setattr(pdfplumber, "open", _fake_open)
    yield


def test_extract_text_uses_pdfplumber_when_available(tmp_path: Path, monkeypatch):
    pdf_path = tmp_path / "demo.pdf"
    pdf_path.write_bytes(b"%PDF-fake")

    with _patched_pdfplumber(monkeypatch, ["Page one text.", "Page two text."]):
        text = pdf_reader.extract_text(pdf_path)

    assert "Page one text." in text
    assert "Page two text." in text


def test_extract_text_caches_alongside_pdf(tmp_path: Path, monkeypatch):
    pdf_path = tmp_path / "cached.pdf"
    pdf_path.write_bytes(b"%PDF-fake")

    with _patched_pdfplumber(monkeypatch, ["Hello, transcript."]):
        text1 = pdf_reader.extract_text(pdf_path)
    text_cache_path = pdf_path.with_suffix(".txt")
    assert text_cache_path.exists()
    assert "Hello, transcript." in text_cache_path.read_text(encoding="utf-8")

    # Second call should read the .txt cache and NOT call pdfplumber again.
    with _patched_pdfplumber(monkeypatch, pages_text=None):
        # pages_text=None would raise inside pdfplumber.open — but since we
        # have a cached .txt, extract_text must short-circuit and not call it.
        text2 = pdf_reader.extract_text(pdf_path)
    assert text1 == text2


def test_extract_text_returns_empty_on_total_failure(tmp_path: Path, monkeypatch):
    pdf_path = tmp_path / "broken.pdf"
    pdf_path.write_bytes(b"%PDF-fake")

    # pdfplumber raises, pypdf is unavailable / would also fail.
    with _patched_pdfplumber(monkeypatch, pages_text=None):
        # Patch pypdf to behave as if installed but broken.
        import sys

        fake_pypdf = SimpleNamespace(PdfReader=lambda _p: (_ for _ in ()).throw(RuntimeError("nope")))
        monkeypatch.setitem(sys.modules, "pypdf", fake_pypdf)
        text = pdf_reader.extract_text(pdf_path)

    assert text == ""


def test_extract_text_for_missing_file_returns_empty(tmp_path: Path):
    assert pdf_reader.extract_text(tmp_path / "no_such.pdf") == ""


# ---------------------------------------------------------------------------
# read_recent_concall_text
# ---------------------------------------------------------------------------


def _concalls_fixture() -> list[dict]:
    return [
        # The most recent quarter is missing a transcript URL — orchestrator
        # must skip it and walk forward to the next entry.
        {
            "month": "Apr 2026",
            "transcript_url": None,
            "ai_summary_url": None,
            "ppt_url": "https://example.com/ppt-apr.pdf",
            "rec_url": None,
        },
        {
            "month": "Jan 2026",
            "transcript_url": "https://example.com/concall-jan.pdf",
            "ai_summary_url": None,
            "ppt_url": None,
            "rec_url": None,
        },
    ]


def test_read_recent_concall_text_skips_rows_without_transcript_url(monkeypatch, tmp_path):
    captured_urls: list[str] = []

    def fake_download(url, **kwargs):  # noqa: ANN001
        captured_urls.append(url)
        path = tmp_path / "stub.pdf"
        path.write_bytes(b"%PDF-fake")
        return path

    def fake_extract(_path):
        return "Management commentary text..."

    monkeypatch.setattr(pdf_reader, "download_pdf", fake_download)
    monkeypatch.setattr(pdf_reader, "extract_text", fake_extract)

    text = pdf_reader.read_recent_concall_text(_concalls_fixture(), cache_dir=tmp_path)

    # We expect exactly ONE download (the Jan 2026 row), since Apr 2026 has
    # no transcript_url and must be skipped.
    assert captured_urls == ["https://example.com/concall-jan.pdf"]
    assert text.startswith("Management commentary text")


def test_read_recent_concall_text_returns_empty_when_no_transcript_available(monkeypatch, tmp_path):
    text = pdf_reader.read_recent_concall_text(
        [
            {"month": "Apr 2026", "transcript_url": None},
            {"month": "Jan 2026", "transcript_url": None},
        ],
        cache_dir=tmp_path,
    )
    assert text == ""


def test_read_recent_concall_text_returns_empty_when_concalls_is_empty(tmp_path):
    assert pdf_reader.read_recent_concall_text([], cache_dir=tmp_path) == ""
    assert pdf_reader.read_recent_concall_text(None, cache_dir=tmp_path) == ""


def test_read_recent_concall_text_truncates_long_transcripts(monkeypatch, tmp_path):
    big_text = "X" * 100_000

    monkeypatch.setattr(
        pdf_reader,
        "download_pdf",
        lambda url, **kwargs: tmp_path / "stub.pdf",  # noqa: ARG005
    )
    # Make sure the stub file exists so extract_text doesn't short-circuit.
    (tmp_path / "stub.pdf").write_bytes(b"%PDF-fake")
    monkeypatch.setattr(pdf_reader, "extract_text", lambda _p: big_text)

    text = pdf_reader.read_recent_concall_text(
        [{"month": "Jan 2026", "transcript_url": "https://example.com/big.pdf"}],
        cache_dir=tmp_path,
        max_chars=1000,
    )

    assert len(text) <= 1000 + len("\n\n[... transcript truncated ...]") + 10
    assert "[... transcript truncated ...]" in text
