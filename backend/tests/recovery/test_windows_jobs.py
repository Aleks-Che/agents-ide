import ctypes
import os

import pytest

from agents_ide.worker import windows_jobs

pytestmark = pytest.mark.skipif(os.name != "nt", reason="Windows Job API")


@pytest.mark.parametrize("assigned,returned", [(8, 7), (1, 0), (3, 3)])
def test_process_ids_use_populated_slots(monkeypatch, assigned, returned):
    def query(handle, info_class, buffer, size, length):
        header = windows_jobs._ProcessListHeader.from_buffer(buffer)
        header.assigned, header.returned = assigned, returned
        slots = (ctypes.c_size_t * returned).from_buffer(buffer, ctypes.sizeof(header))
        for i in range(returned):
            slots[i] = 100 + i
        return True

    monkeypatch.setattr(windows_jobs, "_query_api", lambda: query)
    assert windows_jobs.job_process_ids(123) == list(range(100, 100 + returned))


def test_process_ids_grow_buffer_and_propagate_native_error(monkeypatch):
    sizes = []

    def query(handle, info_class, buffer, size, length):
        sizes.append(size)
        header = windows_jobs._ProcessListHeader.from_buffer(buffer)
        header.assigned = 200
        if len(sizes) == 1:
            ctypes.set_last_error(234)
            return False
        assert size >= ctypes.sizeof(header) + 200 * ctypes.sizeof(ctypes.c_size_t)
        ctypes.set_last_error(6)  # ERROR_INVALID_HANDLE must not mean an empty Job.
        return False

    monkeypatch.setattr(windows_jobs, "_query_api", lambda: query)
    with pytest.raises(OSError) as caught:
        windows_jobs.job_process_ids(123)
    assert caught.value.winerror == 6
    assert len(sizes) == 2


def test_process_ids_reject_null_handle():
    with pytest.raises(ValueError, match="owned Job"):
        windows_jobs.job_process_ids(0)
