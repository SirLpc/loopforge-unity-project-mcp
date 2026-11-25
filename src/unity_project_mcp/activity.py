import hashlib
import os
from contextlib import contextmanager
from pathlib import Path

from .errors import ProjectError


def activity_name(repository_root: Path) -> str:
    identity = os.path.normcase(str(repository_root)).encode("utf-8")
    return "Global\\unity-project-mcp-" + hashlib.sha256(identity).hexdigest()


@contextmanager
def activity_lock(repository_root: Path, state: Path):
    busy = "The reference service or reference sync already holds the activity lock"
    if os.name == "nt":
        import pywintypes
        import win32event

        try:
            mutex = win32event.CreateMutex(None, False, activity_name(repository_root))
        except pywintypes.error:
            raise ProjectError("SERVICE_BUSY", busy) from None
        try:
            acquired = win32event.WaitForSingleObject(mutex, 0)
            if acquired not in (win32event.WAIT_OBJECT_0, win32event.WAIT_ABANDONED):
                raise ProjectError("SERVICE_BUSY", busy)
            try:
                yield
            finally:
                win32event.ReleaseMutex(mutex)
        finally:
            mutex.Close()
        return

    import fcntl

    state.mkdir(parents=True, exist_ok=True)
    with (state / "activity.lock").open("a+b") as handle:
        try:
            fcntl.flock(handle, fcntl.LOCK_EX | fcntl.LOCK_NB)
        except OSError:
            raise ProjectError("SERVICE_BUSY", busy) from None
        yield
