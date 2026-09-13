"""Regression coverage for the transcript parser's process trust boundary."""

from __future__ import annotations

import ctypes
import io
import json
from pathlib import Path
from types import SimpleNamespace

import pytest

from backend import transcript_pdf_process as launcher
from backend import transcript_pdf_worker as worker
from backend.fundamentals import pdf_reader


def _fake_worker(tmp_path: Path, monkeypatch, body: str) -> list:
    """Capture real child lifetimes while substituting a benign worker script.

    Args:
        tmp_path: Private pytest directory for the replacement script.
        monkeypatch: Fixture that restores the production launch seams afterward.
        body: Trusted test source describing a bounded failure scenario.

    Returns:
        A list filled with the actual Popen objects as the launcher creates them.

    Beginner note:
        Saving the real constructor before patching avoids recursive fakes. We
        retain the real process lifecycle so cleanup assertions prove termination,
        rather than merely observing that a mock kill method was called.
    """
    script = tmp_path / "fake_worker.py"
    script.write_text("import sys, time\nfrom pathlib import Path\n" + body, encoding="utf-8")
    monkeypatch.setattr(launcher, "_WORKER_PATH", script)
    processes: list = []
    original = launcher.subprocess.Popen

    def launch(*args, **kwargs):
        process = original(*args, **kwargs)
        processes.append(process)
        return process

    monkeypatch.setattr(launcher.subprocess, "Popen", launch)
    return processes


def test_stalled_child_is_killed_and_private_directory_removed(tmp_path: Path, monkeypatch):
    """A blocked parser must not outlive the request or retain private files.

    Beginner note:
        The original in-process parser could stall indefinitely. A harmless
        sleeping child reproduces that failure without a hostile PDF; the test
        requires both an exited process and removal of its private result path.
    """
    processes = _fake_worker(tmp_path, monkeypatch, "sys.stdin.buffer.read(3)\ntime.sleep(30)\n")
    monkeypatch.setattr(launcher, "WALL_TIME_SECONDS", 0.2)
    with pytest.raises(launcher.subprocess.TimeoutExpired):
        launcher.run_transcript_worker(tmp_path / "unused.pdf", max_chars=40, max_pages=1)
    assert processes[0].poll() is not None
    assert not Path(processes[0].args[4]).parent.exists()


def test_parent_rejects_large_result_file_and_cleans_up(tmp_path: Path, monkeypatch):
    """A buggy child cannot make the parent read more than the receipt cap.

    Beginner note:
        A child-side check alone does not protect the receiving app from a
        malformed result. One byte beyond the cap must trigger rejection while
        still closing the child and deleting the temporary output directory.
    """
    processes = _fake_worker(tmp_path, monkeypatch,
                             "sys.stdin.buffer.read(3)\nPath(sys.argv[2]).write_bytes(b'x' * 262145)\n")
    with pytest.raises(OverflowError):
        launcher.run_transcript_worker(tmp_path / "unused.pdf", max_chars=40, max_pages=1)
    assert processes[0].poll() == 0
    assert not Path(processes[0].args[4]).parent.exists()


def test_windows_assignment_failure_never_releases_child(tmp_path: Path, monkeypatch):
    """Job setup failure leaves the waiting child unable to open its PDF.

    Beginner note:
        Starting a parser before attaching its job creates an unbounded race.
        The marker represents parsing: failed assignment must leave it absent
        and the waiting process must be reaped, rather than granted a fallback.
    """
    if launcher.sys.platform != "win32":
        pytest.skip("Windows Job Object integration")
    marker = tmp_path / "parsed"
    processes = _fake_worker(tmp_path, monkeypatch,
        f"token = sys.stdin.buffer.read(3)\nif token == b'GO\\n': Path({str(marker)!r}).touch()\n")

    def fail_assignment(self, pid):
        raise OSError("bounded setup failure")

    monkeypatch.setattr(launcher.WindowsPdfJob, "assign", fail_assignment)
    with pytest.raises(OSError, match="bounded setup failure"):
        launcher.run_transcript_worker(tmp_path / "unused.pdf", max_chars=40, max_pages=1)
    assert not marker.exists()
    assert processes[0].poll() is not None


def test_ipo_worker_waits_for_parent_before_parsing(monkeypatch):
    """IPO parsing must share the attach-before-parse ordering on Windows.

    Beginner note:
        A child that starts immediately could parse before the memory policy
        exists. A denied start event must prevent the parser call entirely and
        close its pipe so the parent can observe a safe failure.
    """
    from backend import ipo_pdf_worker

    class ClosedGate:
        def wait(self, timeout):
            return False

    class Connection:
        closed = False

        def close(self):
            self.closed = True

    def forbidden(*args, **kwargs):
        pytest.fail("Parser ran without a parent start grant")

    monkeypatch.setattr(ipo_pdf_worker, "extract_payload", forbidden)
    connection = Connection()
    ipo_pdf_worker.worker_entry("unused.pdf", {}, connection, ClosedGate())
    assert connection.closed


def test_memory_setup_failure_prevents_both_parsers(tmp_path: Path, monkeypatch):
    """A failed limit installation must not fall through to either PDF library.

    Beginner note:
        Treating OS setup as best-effort would recreate the original unbounded
        parser path. The failing setup seam therefore must produce no result
        file and must never call the helper that selects either parser.
    """
    def fail_limit():
        raise OSError("limit unavailable")

    def forbidden(*args, **kwargs):
        pytest.fail("Parser ran after memory containment failed")

    monkeypatch.setattr(worker, "_install_memory_limit", fail_limit)
    monkeypatch.setattr(worker, "extract_payload", forbidden)
    monkeypatch.setattr(worker.sys, "stdin", SimpleNamespace(buffer=io.BytesIO(b"GO\n")))
    result = tmp_path / "result.json"
    monkeypatch.setattr(worker.sys, "argv", ["worker", "unused.pdf", str(result), "40", "1"])
    worker.main()
    assert not result.exists()


def _write_pdf(path: Path, texts: list[str]) -> None:
    """Build a tiny valid PDF fixture without optional authoring dependencies.

    Args:
        path: Private fixture output path.
        texts: Trusted short ASCII labels, one per generated page.

    Beginner note:
        Explicit object offsets keep this a real parseable PDF while avoiding a
        new document-authoring dependency. This helper is limited to controlled
        labels; it is not an encoder for arbitrary external PDF content.
    """
    objects = [b"<< /Type /Catalog /Pages 2 0 R >>", b""]
    objects.append(b"<< /Type /Font /Subtype /Type1 /BaseFont /Helvetica >>")
    page_ids = []
    for text in texts:
        page_id = len(objects) + 1
        page_ids.append(page_id)
        objects.append((f"<< /Type /Page /Parent 2 0 R /MediaBox [0 0 612 792] "
                        f"/Resources << /Font << /F1 3 0 R >> >> /Contents {page_id + 1} 0 R >>").encode())
        content = f"BT /F1 12 Tf 10 750 Td ({text}) Tj ET".encode("ascii")
        objects.append(f"<< /Length {len(content)} >>\nstream\n".encode() + content + b"\nendstream")
    kids = " ".join(f"{number} 0 R" for number in page_ids)
    objects[1] = f"<< /Type /Pages /Kids [{kids}] /Count {len(page_ids)} >>".encode()
    data = bytearray(b"%PDF-1.4\n")
    offsets = [0]
    for number, body in enumerate(objects, 1):
        offsets.append(len(data))
        data.extend(f"{number} 0 obj\n".encode() + body + b"\nendobj\n")
    xref = len(data)
    data.extend(f"xref\n0 {len(offsets)}\n0000000000 65535 f \n".encode())
    for offset in offsets[1:]:
        data.extend(f"{offset:010d} 00000 n \n".encode())
    data.extend(f"trailer\n<< /Size {len(offsets)} /Root 1 0 R >>\nstartxref\n{xref}\n%%EOF\n".encode())
    path.write_bytes(data)


@pytest.mark.parametrize("fallback", [False, True])
def test_real_child_preserves_first_thirty_pages_and_character_limit(tmp_path: Path, monkeypatch, fallback: bool):
    """Both parser paths return useful text while obeying the same child boundary.

    Beginner note:
        Only the fallback test replaces the primary parser, inside the child.
        pypdf is optional, so its reader is a benign fake in that child.
        Its extraction path and the OS containment remain real.
    """
    pdf = tmp_path / "good.pdf"
    _write_pdf(pdf, [f"Page{i:02}" for i in range(1, 32)])
    if fallback:
        driver = tmp_path / "fallback_driver.py"
        driver.write_text(
            f"import runpy\ng = runpy.run_path({str(launcher._WORKER_PATH)!r})\n"
            "g['main'].__globals__['_extract_with_pdfplumber'] = lambda *a, **kw: ''\n"
            "import sys, types\n"
            "pages = [types.SimpleNamespace(extract_text=lambda i=i: f'Page{i:02}') for i in range(1, 32)]\n"
            "sys.modules['pypdf'] = types.SimpleNamespace(PdfReader=lambda p: types.SimpleNamespace(pages=pages))\n"
            "g['main']()\n", encoding="utf-8")
        monkeypatch.setattr(launcher, "_WORKER_PATH", driver)
    result = json.loads(launcher.run_transcript_worker(pdf, max_chars=40000, max_pages=30))
    assert "Page01" in result["text"] and "Page30" in result["text"]
    assert "Page31" not in result["text"]
    result = json.loads(launcher.run_transcript_worker(pdf, max_chars=10, max_pages=30))
    assert result["text"] == "Page01\n\nPa"


def test_windows_job_enforces_policy_and_kills_child_on_close(tmp_path: Path):
    """Read back the OS limit and prove close kills an attached benign sleeper.

    Beginner note:
        Mocking a successful API call could miss a wrong ctypes field offset.
        Reading the real configured memory value and observing actual process
        exit protect both the binary layout and kill-on-close invariants.
    """
    if launcher.sys.platform != "win32":
        pytest.skip("Windows Job Object integration")
    from backend.pdf_process_limits import WindowsPdfJob, _ExtendedLimits

    process = launcher.subprocess.Popen(
        [launcher.sys.executable, "-I", "-c", "import time; time.sleep(30)"],
        creationflags=launcher._WINDOWS_NO_WINDOW,
    )
    job = WindowsPdfJob(512 * 1024 * 1024)
    try:
        job.assign(process.pid)
        query = job._api.QueryInformationJobObject
        query.argtypes = [ctypes.c_void_p, ctypes.c_int, ctypes.c_void_p, ctypes.c_ulong, ctypes.c_void_p]
        query.restype = ctypes.c_int
        limits = _ExtendedLimits()
        assert query(job._handle, 9, ctypes.byref(limits), ctypes.sizeof(limits), None)
        assert limits.ProcessMemoryLimit == 536870912
        assert limits.BasicLimitInformation.LimitFlags & 0x2100 == 0x2100
        job.close()
        process.wait(timeout=5)
        assert process.poll() is not None
    finally:
        job.close()
        if process.poll() is None:
            process.kill()
        process.wait(timeout=5)


def test_real_ipo_child_keeps_existing_page_budget(tmp_path: Path):
    """Adding Windows containment must preserve IPO's all-or-review page rule.

    Beginner note:
        Transcript parsing retains an initial page slice, whereas IPO evidence
        requires a complete receipt within its own budget. A two-page fixture
        under a one-page IPO limit must still require review with no pages.
    """
    from backend.ipo.documents.table_extractor import PdfExtractionBudget, PdfParseStatus, parse_document_pages

    path = tmp_path / "ipo.pdf"
    _write_pdf(path, ["first", "second"])
    receipt = parse_document_pages(path, budget=PdfExtractionBudget(max_pages=1))
    assert receipt.status is PdfParseStatus.REVIEW_REQUIRED
    assert receipt.error_code == "page_limit_exceeded"
    assert receipt.pages == ()


@pytest.mark.skipif(launcher.sys.platform != "linux", reason="Linux RLIMIT integration")
def test_linux_real_child_installs_limits_before_extraction(tmp_path: Path, monkeypatch):
    """Read the actual child rlimits without allocating hostile amounts of memory.

    Beginner note:
        Platform-specific code can pass Windows tests while doing nothing on
        Linux. The extraction seam observes its own active address-space limit,
        proving installation happened before the parser would begin.
    """
    driver = tmp_path / "limits_driver.py"
    driver.write_text(
        f"import runpy, resource\ng = runpy.run_path({str(launcher._WORKER_PATH)!r})\n"
        "g['main'].__globals__['_extract_with_pdfplumber'] = "
        "lambda *a, **kw: str(resource.getrlimit(resource.RLIMIT_AS))\n"
        "g['main']()\n", encoding="utf-8")
    monkeypatch.setattr(launcher, "_WORKER_PATH", driver)
    result = json.loads(launcher.run_transcript_worker(tmp_path / "unused.pdf", max_chars=100, max_pages=1))
    assert result["text"] == "(536870912, 536870912)"


def test_extract_text_uses_only_bounded_worker(tmp_path: Path, monkeypatch):
    """A child failure must never trigger a parser inside the app process.

    Beginner note:
        A fallback in the parent would remove the memory and timeout protection
        precisely when a hostile document made the first attempt fail.
    """
    path = tmp_path / "document.pdf"
    path.write_bytes(b"%PDF-fake")
    monkeypatch.setattr(pdf_reader, "_extract_with_pdfplumber", lambda *a, **kw: "unsafe parent text", raising=False)
    monkeypatch.setattr(pdf_reader, "_extract_with_pypdf", lambda *a, **kw: "unsafe fallback text", raising=False)
    monkeypatch.setattr(pdf_reader, "_run_transcript_worker", lambda *a, **kw: b'{"text":""}', raising=False)
    assert pdf_reader.extract_text(path) == ""


def test_extract_text_rejects_oversized_child_text(tmp_path: Path, monkeypatch):
    """The parent independently rejects a worker that violates its text budget.

    Beginner note:
        A stale or faulty worker may return valid JSON with too much text. The
        caller must return unavailable evidence, and must not accept the text
        or invoke a parent-process parser after rejecting the receipt.
    """
    path = tmp_path / "document.pdf"
    path.write_bytes(b"%PDF-fake")
    monkeypatch.setattr(pdf_reader, "_extract_with_pdfplumber", lambda *a, **kw: "unsafe parent text", raising=False)
    monkeypatch.setattr(pdf_reader, "_run_transcript_worker", lambda *a, **kw: b'{"text":"123456"}', raising=False)
    assert pdf_reader.extract_text(path, max_chars=5) == ""


@pytest.mark.parametrize("encoded", [b"not json", b'{"text":4}', b'{"text":"ok","extra":1}', b"\xff"])
def test_parent_rejects_malformed_primitive_receipt(tmp_path: Path, monkeypatch, encoded: bytes):
    """Malformed and wrongly typed child data must not become transcript evidence.

    Beginner note:
        Successful child exit does not establish a valid result. Invalid UTF-8,
        invalid JSON, wrong field types, and unexpected fields must all produce
        empty evidence at the parent boundary instead of reaching the AI prompt.
    """
    path = tmp_path / "document.pdf"
    path.write_bytes(b"%PDF-fake")
    monkeypatch.setattr(pdf_reader, "_run_transcript_worker", lambda *a, **kw: encoded)
    assert pdf_reader.extract_text(path) == ""


def test_large_requested_limits_cannot_override_transcript_ceiling(tmp_path: Path, monkeypatch):
    """The parent caps user-supplied limits and ignores legacy full-text caches.

    Beginner note:
        Old caches cannot establish that only the first 30 pages were used.
        Neither an old cache nor an oversized caller request may bypass the
        new hard page/text ceilings, including when defaults are requested.
    """
    path = tmp_path / "document.pdf"
    path.write_bytes(b"%PDF-fake")
    path.with_suffix(".txt").write_text("legacy unbounded text", encoding="utf-8")

    def bounded(_path, *, max_chars, max_pages):
        assert max_chars == 40000 and max_pages == 30
        return json.dumps({"text": "x" * 40000}).encode()

    monkeypatch.setattr(pdf_reader, "_run_transcript_worker", bounded)
    assert len(pdf_reader.extract_text(path, max_chars=90000, max_pages=90)) == 40000
    assert len(pdf_reader.extract_text(path)) == 40000
