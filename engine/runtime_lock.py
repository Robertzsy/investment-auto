from __future__ import annotations

import os
import time
from contextlib import contextmanager
from pathlib import Path
from typing import Generator, IO, Optional


class AtomicClaim:
    """An O_EXCL filesystem claim whose ownership can cross a thread start.

    ``atomic_claim`` remains the convenient context-manager API. Long-running
    worker launchers use this object directly so they can acquire the lease,
    update durable state only after acquisition succeeds, and then transfer
    release responsibility to the worker thread.
    """

    def __init__(self, lock_path: Path, *, stale_seconds: int = 7200) -> None:
        self.lock_path = lock_path
        self.stale_seconds = stale_seconds
        self.descriptor: Optional[int] = None

    def acquire(self) -> bool:
        if self.descriptor is not None:
            return True
        self.lock_path.parent.mkdir(parents=True, exist_ok=True)
        for _ in range(2):
            try:
                descriptor = os.open(str(self.lock_path), os.O_CREAT | os.O_EXCL | os.O_WRONLY)
                os.write(descriptor, f"pid={os.getpid()} time={time.time()}\n".encode("ascii"))
                self.descriptor = descriptor
                return True
            except FileExistsError:
                try:
                    if time.time() - self.lock_path.stat().st_mtime > self.stale_seconds:
                        self.lock_path.unlink()
                        continue
                except FileNotFoundError:
                    continue
                return False
        return False

    def release(self) -> None:
        descriptor = self.descriptor
        if descriptor is None:
            return
        self.descriptor = None
        os.close(descriptor)
        try:
            self.lock_path.unlink()
        except FileNotFoundError:
            pass


@contextmanager
def atomic_claim(lock_path: Path, *, stale_seconds: int = 7200) -> Generator[bool, None, None]:
    """Atomically claim a filesystem-backed job across threads and processes."""
    claim = AtomicClaim(lock_path, stale_seconds=stale_seconds)
    try:
        yield claim.acquire()
    finally:
        claim.release()


class ProcessLease:
    """Advisory single-process lease held for the lifetime of a scheduler."""

    def __init__(self, path: Path) -> None:
        self.path = path
        self.handle: Optional[IO[bytes]] = None

    def acquire(self) -> None:
        self.path.parent.mkdir(parents=True, exist_ok=True)
        handle: Optional[IO[bytes]] = None
        try:
            handle = self.path.open("a+b")
            handle.seek(0)
            if handle.read(1) == b"":
                handle.seek(0)
                handle.write(b"0")
                handle.flush()
            handle.seek(0)
            if os.name == "nt":
                import msvcrt

                msvcrt.locking(handle.fileno(), msvcrt.LK_NBLCK, 1)
            else:
                import fcntl

                fcntl.flock(handle.fileno(), fcntl.LOCK_EX | fcntl.LOCK_NB)
        except (OSError, BlockingIOError) as exc:
            if handle is not None:
                handle.close()
            raise RuntimeError("另一个 investment-auto 调度器已经在运行") from exc
        self.handle = handle

    def release(self) -> None:
        handle = self.handle
        if handle is None:
            return
        try:
            handle.seek(0)
            if os.name == "nt":
                import msvcrt

                msvcrt.locking(handle.fileno(), msvcrt.LK_UNLCK, 1)
            else:
                import fcntl

                fcntl.flock(handle.fileno(), fcntl.LOCK_UN)
        finally:
            handle.close()
            self.handle = None

    def __enter__(self) -> "ProcessLease":
        self.acquire()
        return self

    def __exit__(self, *_: object) -> None:
        self.release()
