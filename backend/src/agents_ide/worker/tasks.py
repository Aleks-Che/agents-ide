"""One thread per admitted task, with no fixed concurrency ceiling or hidden queue."""

import logging
import threading
from collections.abc import Callable

logger = logging.getLogger("agents_ide.worker")


class DispatchThreads:
    def __init__(self) -> None:
        self.threads: list[threading.Thread] = []

    def start(self, name: str, dispatch: Callable[[], bool]) -> None:
        def execute() -> None:
            try:
                dispatch()
            except Exception:
                logger.exception("worker.dispatch_error", extra={"task": name})

        thread = threading.Thread(target=execute, name=name)
        thread.start()
        self.threads.append(thread)

    def reap(self) -> None:
        self.threads = [thread for thread in self.threads if thread.is_alive()]

    def shutdown(self) -> None:
        for thread in self.threads:
            thread.join()
        self.threads.clear()
