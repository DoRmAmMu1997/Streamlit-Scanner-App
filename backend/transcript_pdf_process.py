"""Killable, bounded transcript worker launcher shared by both PDF parsers."""

from __future__ import annotations

# Required killable parser boundary; subprocess never executes a shell.
import subprocess  # nosec B404
import sys
import tempfile
import time
from pathlib import Path

from backend.pdf_process_limits import WindowsPdfJob

MAX_RESULT_BYTES = 256 * 1024
MEMORY_BYTES = 512 * 1024 * 1024
WALL_TIME_SECONDS = 60.0
_WORKER_PATH = Path(__file__).with_name("transcript_pdf_worker.py")
_WINDOWS_NO_WINDOW = 0x08000000  # Win32 CREATE_NO_WINDOW; absent from Linux subprocess stubs


def run_transcript_worker(pdf_path: Path, *, max_chars: int, max_pages: int) -> bytes:
    """Return a bounded JSON receipt from a fresh, resource-limited interpreter.

    Args:
        pdf_path: Local downloaded PDF selected by the parent.
        max_chars: Retained text ceiling, at most 40,000 characters.
        max_pages: Retained page ceiling, at most the first 30 pages.

    Returns:
        JSON bytes; failures propagate to the caller's unavailable-text policy.

    Beginner note:
        The deadline includes child startup and assignment. The child receives
        its GO token only after Windows memory containment succeeds. Its stdout
        and stderr are discarded so parser diagnostics cannot become an
        unbounded output buffer or leak hostile text into application logs.
    """
    if sys.platform not in {"win32", "linux"}:
        raise OSError("PDF memory containment unavailable")
    deadline = time.monotonic() + WALL_TIME_SECONDS
    job = WindowsPdfJob(MEMORY_BYTES) if sys.platform == "win32" else None
    process: subprocess.Popen[bytes] | None = None
    try:
        with tempfile.TemporaryDirectory(prefix="scanner-transcript-") as directory:
            result_path = Path(directory) / "result.json"
            try:
                # The executable and script are fixed by the application. PDF
                # paths are separate argv values, never command-line code.
                process = subprocess.Popen(  # nosec B603
                    [sys.executable, "-I", str(_WORKER_PATH), str(pdf_path.resolve()),
                     str(result_path), str(max_chars), str(max_pages)],
                    stdin=subprocess.PIPE,
                    stdout=subprocess.DEVNULL,
                    stderr=subprocess.DEVNULL,
                    creationflags=_WINDOWS_NO_WINDOW if sys.platform == "win32" else 0,
                    close_fds=True,
                )
                if job is not None:
                    job.assign(process.pid)
                if process.stdin is None:
                    raise ChildProcessError("PDF handshake unavailable")
                process.stdin.write(b"GO\n")
                process.stdin.close()
                process.wait(timeout=max(0.001, deadline - time.monotonic()))
                if process.returncode != 0:
                    raise ChildProcessError("PDF worker failed")
                # Do not trust worker-side limits. Reading at most cap+1 bytes
                # keeps even a buggy oversized result bounded in parent memory.
                with result_path.open("rb") as stream:
                    result = stream.read(MAX_RESULT_BYTES + 1)
                if len(result) > MAX_RESULT_BYTES:
                    raise OverflowError("PDF result exceeded its budget")
                return result
            finally:
                if process is not None:
                    if process.poll() is None:
                        process.kill()
                    process.wait(timeout=5)
                    if process.stdin is not None:
                        process.stdin.close()
                # Stop any surviving job members before removing private files.
                if job is not None:
                    job.close()
    finally:
        if job is not None:
            job.close()
