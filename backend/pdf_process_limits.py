"""Parent-owned Windows memory containment for untrusted PDF children.

Beginner note:
    A Job Object limits committed process memory and kills its members when its
    last handle closes. The child must wait for an explicit parent handshake;
    starting the parser before assignment would leave a race without a limit.
"""

from __future__ import annotations

import ctypes
from ctypes import wintypes
from typing import Any


class _BasicLimits(ctypes.Structure):
    """Mirror the fixed Win32 basic-limit layout, including pointer-sized sizes.

    Beginner note:
        Field order and widths are part of the OS ABI. A Python-looking size
        substitution could move every later field and silently change policy.
    """
    _fields_ = [
        ("PerProcessUserTimeLimit", ctypes.c_longlong),
        ("PerJobUserTimeLimit", ctypes.c_longlong),
        ("LimitFlags", wintypes.DWORD),
        ("MinimumWorkingSetSize", ctypes.c_size_t),
        ("MaximumWorkingSetSize", ctypes.c_size_t),
        ("ActiveProcessLimit", wintypes.DWORD),
        ("Affinity", ctypes.c_size_t),
        ("PriorityClass", wintypes.DWORD),
        ("SchedulingClass", wintypes.DWORD),
    ]


class _IoCounters(ctypes.Structure):
    """Reserve the OS I/O counters embedded before the extended memory fields.

    Beginner note:
        We do not use these counters, but omitting them would put the memory
        ceiling at the wrong byte offset in the structure sent to Windows.
    """
    _fields_ = [(name, ctypes.c_ulonglong) for name in (
        "ReadOperationCount", "WriteOperationCount", "OtherOperationCount",
        "ReadTransferCount", "WriteTransferCount", "OtherTransferCount",
    )]


class _ExtendedLimits(ctypes.Structure):
    """Match JOBOBJECT_EXTENDED_LIMIT_INFORMATION for the Windows API call.

    Beginner note:
        Memory fields use SIZE_T rather than a fixed 32-bit integer so the same
        declaration preserves the contract on 32-bit and 64-bit Python.
    """
    _fields_ = [
        ("BasicLimitInformation", _BasicLimits),
        ("IoInfo", _IoCounters),
        ("ProcessMemoryLimit", ctypes.c_size_t),
        ("JobMemoryLimit", ctypes.c_size_t),
        ("PeakProcessMemoryUsed", ctypes.c_size_t),
        ("PeakJobMemoryUsed", ctypes.c_size_t),
    ]


class WindowsPdfJob:
    """Own one fail-closed memory-limited Windows Job Object.

    Args:
        maximum_bytes: Maximum committed bytes for each assigned PDF child.

    Raises:
        OSError: Windows refuses job creation or the process memory policy.

    Beginner note:
        Handle argument and return types are explicit because Windows handles
        are pointer-sized on 64-bit Python. ctypes' default integer return type
        would truncate a valid handle and silently destroy this protection.
    """

    def __init__(self, maximum_bytes: int) -> None:
        """Create the owned job and install its memory and cleanup policy.

        Args:
            maximum_bytes: Trusted, positive process commit limit in bytes.

        Raises:
            OSError: A required Windows job operation fails.

        Beginner note:
            Configure the job before assigning a waiting child, then release
            that child only after assignment. Callers own closing this handle.
        """
        # Linux's ctypes/type stubs omit WinDLL. This Windows-only constructor
        # is never called there; accidental use still raises fail-closed.
        self._api: Any = ctypes.WinDLL("kernel32", use_last_error=True)  # type: ignore[attr-defined, unused-ignore]
        self._api.CreateJobObjectW.argtypes = [ctypes.c_void_p, wintypes.LPCWSTR]
        self._api.CreateJobObjectW.restype = wintypes.HANDLE
        self._api.SetInformationJobObject.argtypes = [wintypes.HANDLE, ctypes.c_int, ctypes.c_void_p, wintypes.DWORD]
        self._api.SetInformationJobObject.restype = wintypes.BOOL
        self._api.OpenProcess.argtypes = [wintypes.DWORD, wintypes.BOOL, wintypes.DWORD]
        self._api.OpenProcess.restype = wintypes.HANDLE
        self._api.AssignProcessToJobObject.argtypes = [wintypes.HANDLE, wintypes.HANDLE]
        self._api.AssignProcessToJobObject.restype = wintypes.BOOL
        self._api.CloseHandle.argtypes = [wintypes.HANDLE]
        self._api.CloseHandle.restype = wintypes.BOOL
        self._handle = self._api.CreateJobObjectW(None, None)
        if not self._handle:
            raise OSError("PDF job creation failed")
        limits = _ExtendedLimits()
        # PROCESS_MEMORY | KILL_ON_JOB_CLOSE. Do not permit breakaway children.
        limits.BasicLimitInformation.LimitFlags = 0x100 | 0x2000
        limits.ProcessMemoryLimit = maximum_bytes
        if not self._api.SetInformationJobObject(self._handle, 9, ctypes.byref(limits), ctypes.sizeof(limits)):
            self.close()
            raise OSError("PDF job memory limit failed")

    def assign(self, pid: int) -> None:
        """Attach a waiting child, or raise before its start signal is sent.

        Args:
            pid: Identifier of the caller's newly spawned, waiting PDF child.

        Raises:
            OSError: The process cannot be opened or assigned to this job.

        Beginner note:
            The temporary process handle is closed on every path. The job
            retains the association, so releasing that handle does not release
            the memory restriction or the job's kill-on-close behavior.
        """
        process_handle = self._api.OpenProcess(0x100 | 0x1, False, pid)  # SET_QUOTA | TERMINATE
        if not process_handle:
            raise OSError("PDF process handle unavailable")
        try:
            if not self._api.AssignProcessToJobObject(self._handle, process_handle):
                raise OSError("PDF job assignment failed")
        finally:
            self._api.CloseHandle(process_handle)

    def close(self) -> None:
        """Release the job and terminate any remaining assigned processes.

        Beginner note:
            Clearing our handle makes repeated cleanup calls harmless. The
            parent still explicitly reaps its child to collect the exit status;
            job closure protects against surviving assigned descendants.
        """
        if self._handle:
            self._api.CloseHandle(self._handle)
            self._handle = None
