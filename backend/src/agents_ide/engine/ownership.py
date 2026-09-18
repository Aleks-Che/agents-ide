"""Local process identity, independent of delayed database heartbeats."""

import psutil


def owner_may_be_alive(pid: int | None, created_at: float | None) -> bool:
    if pid is None or created_at is None:
        return False
    try:
        process = psutil.Process(pid)
        return (
            process.create_time() == created_at
            and process.is_running()
            and process.status() != psutil.STATUS_ZOMBIE
        )
    except psutil.NoSuchProcess:
        return False
    except psutil.AccessDenied:
        # Failure to inspect a process does not authorize taking its work.
        return True
