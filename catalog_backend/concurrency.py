from __future__ import annotations

import fcntl
from contextlib import contextmanager
from pathlib import Path
from time import monotonic, sleep


class TaskCapacityError(RuntimeError):
    """Raised when a bounded background-style task cannot obtain a slot."""


class FileSlotPool:
    """Small cross-process task limiter backed by advisory file locks."""

    def __init__(
        self,
        directory: str | Path,
        name: str,
        slots: int,
        *,
        wait_seconds: float = 900,
        poll_seconds: float = 0.05,
    ) -> None:
        self.directory = Path(directory)
        self.name = str(name).strip() or "task"
        self.slots = max(1, int(slots))
        self.wait_seconds = max(0.0, float(wait_seconds))
        self.poll_seconds = max(0.01, float(poll_seconds))

    @contextmanager
    def acquire(self):
        self.directory.mkdir(parents=True, exist_ok=True)
        deadline = monotonic() + self.wait_seconds
        while True:
            for slot_number in range(1, self.slots + 1):
                lock_file = (self.directory / f"{self.name}-{slot_number}.lock").open("a+")
                try:
                    fcntl.flock(lock_file.fileno(), fcntl.LOCK_EX | fcntl.LOCK_NB)
                except BlockingIOError:
                    lock_file.close()
                    continue
                try:
                    yield slot_number
                finally:
                    fcntl.flock(lock_file.fileno(), fcntl.LOCK_UN)
                    lock_file.close()
                return
            if monotonic() >= deadline:
                raise TaskCapacityError(f"{self.name} task capacity is currently full")
            sleep(self.poll_seconds)
