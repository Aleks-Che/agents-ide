"""Read Windows Job PIDs without pywin32's partially initialized tuple."""

import ctypes
from ctypes import wintypes
from functools import cache
from typing import Any


def process_exited(pid: int) -> bool | None:
    """Check exit through a handle, including terminated processes retained by Popen.

    psutil's creation-time fallback scans system process data for those retained
    PIDs. Access denial is still unknown, never permission to reclaim their work.
    """
    import pywintypes
    import win32api
    import win32con
    import win32event

    if pid <= 0:
        return None
    try:
        handle = win32api.OpenProcess(win32con.SYNCHRONIZE, False, pid)
    except pywintypes.error as exc:
        return True if exc.winerror == 87 else None  # ERROR_INVALID_PARAMETER: no such PID
    try:
        state = win32event.WaitForSingleObject(handle, 0)
        if state == win32event.WAIT_OBJECT_0:
            return True
        return False if state == win32event.WAIT_TIMEOUT else None
    except pywintypes.error:
        return None
    finally:
        win32api.CloseHandle(handle)


class _ProcessListHeader(ctypes.Structure):
    _fields_ = [
        ("assigned", wintypes.DWORD),
        ("returned", wintypes.DWORD),
    ]


@cache
def _query_api() -> Any:
    query = ctypes.WinDLL("kernel32", use_last_error=True).QueryInformationJobObject
    query.argtypes = [
        wintypes.HANDLE,
        ctypes.c_int,
        wintypes.LPVOID,
        wintypes.DWORD,
        ctypes.POINTER(wintypes.DWORD),
    ]
    query.restype = wintypes.BOOL
    return query


def job_process_ids(handle: int) -> list[int]:
    """Return only populated PID slots, retrying if the native buffer is too small.

    Processes can exit during a successful query, so assigned and returned
    counts can differ. pywin32 allocates a tuple using the first count and
    fills it using the second, leaving NULL Python objects that crash the
    interpreter during iteration. Read the native buffer directly instead.
    """
    if not handle:
        # NULL queries the caller's Job, which is never an owned child Job.
        raise ValueError("An explicit owned Job handle is required")
    query = _query_api()
    capacity = 64
    offset = ctypes.sizeof(_ProcessListHeader)
    while True:
        size = offset + capacity * ctypes.sizeof(ctypes.c_size_t)
        buffer = ctypes.create_string_buffer(size)
        header = _ProcessListHeader.from_buffer(buffer)
        if query(handle, 3, buffer, size, None):  # JobObjectBasicProcessIdList
            if header.returned > capacity:
                raise OSError("Invalid process count returned for Windows Job")
            pids = (ctypes.c_size_t * header.returned).from_buffer(buffer, offset)
            return list(pids)
        error = ctypes.get_last_error()
        if error != 234:  # ERROR_MORE_DATA
            raise ctypes.WinError(error)
        capacity = max(capacity * 2, header.assigned)
