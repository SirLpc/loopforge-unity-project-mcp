import os
import signal
import subprocess
from collections.abc import Mapping, Sequence
from contextvars import ContextVar
from dataclasses import dataclass
from pathlib import Path
from threading import Thread
from time import monotonic

from .errors import ProjectError

if os.name == "nt":
    import pywintypes
    import win32job

QUERY_DEADLINE: ContextVar[float | None] = ContextVar("query_deadline", default=None)


@dataclass(frozen=True)
class Output:
    stdout: bytes
    returncode: int
    truncated: bool


def command_environment(exe: Path) -> dict[str, str]:
    env = {key: os.environ[key] for key in ("SystemRoot", "WINDIR", "SYSTEMDRIVE", "COMSPEC") if key in os.environ}
    env.update(PATH=str(exe.parent), LC_ALL="C.UTF-8", LANG="C.UTF-8")
    return env


def windows_job(process: subprocess.Popen):
    job = None
    try:
        job = win32job.CreateJobObject(None, "")
        info = win32job.QueryInformationJobObject(job, win32job.JobObjectExtendedLimitInformation)
        info["BasicLimitInformation"]["LimitFlags"] |= win32job.JOB_OBJECT_LIMIT_KILL_ON_JOB_CLOSE
        win32job.SetInformationJobObject(job, win32job.JobObjectExtendedLimitInformation, info)
        win32job.AssignProcessToJobObject(job, int(process._handle))
        return job
    except pywintypes.error:
        if job is not None:
            job.Close()
        if process.poll() is None:
            raise ProjectError("DEPENDENCY_UNAVAILABLE", "Could not contain the command process tree") from None
        return None


def run_bounded(
    argv: Sequence[str], cwd: Path, env: Mapping[str, str], *, timeout: float,
    cap: int, input_bytes: bytes | None = None,
) -> Output:
    if query_deadline := QUERY_DEADLINE.get():
        timeout = min(timeout, query_deadline - monotonic())
    if timeout <= 0:
        raise ProjectError("SEARCH_TIMEOUT", "Query exceeded its time limit")
    deadline = monotonic() + timeout
    try:
        process = subprocess.Popen(
            argv, cwd=cwd, env=env, shell=False, stdout=subprocess.PIPE, stderr=subprocess.DEVNULL,
            stdin=subprocess.DEVNULL if input_bytes is None else subprocess.PIPE,
            start_new_session=os.name != "nt",
        )
    except OSError:
        raise ProjectError("DEPENDENCY_UNAVAILABLE", "Could not start a required executable") from None

    try:
        job = windows_job(process) if os.name == "nt" else None
    except ProjectError:
        process.kill()
        process.wait()
        process.stdout.close()
        if process.stdin:
            process.stdin.close()
        raise

    captured = bytearray()
    read_error = False

    def stop_tree() -> None:
        try:
            if job is not None:
                win32job.TerminateJobObject(job, 1)
            elif os.name == "nt":
                process.kill()
            else:
                os.killpg(process.pid, signal.SIGKILL)
        except ProcessLookupError:
            pass

    def drain() -> None:
        nonlocal read_error
        try:
            with process.stdout as stdout:
                while block := stdout.read1(min(65536, cap + 1 - len(captured))):
                    captured.extend(block)
                    if len(captured) > cap:
                        stop_tree()
                        break
        except OSError:
            read_error = True

    def feed() -> None:
        try:
            with process.stdin as stdin:
                stdin.write(input_bytes)
        except (BrokenPipeError, OSError):
            pass

    threads = [Thread(target=drain, name="command-output")]
    if input_bytes is not None:
        threads.append(Thread(target=feed, name="command-input"))
    for thread in threads:
        thread.start()
    try:
        process.wait(timeout=max(0, deadline - monotonic()))
        for thread in threads:
            thread.join(timeout=max(0, deadline - monotonic()))
            if thread.is_alive():
                raise subprocess.TimeoutExpired(argv, timeout)
    except subprocess.TimeoutExpired:
        stop_tree()
        process.wait()
        raise ProjectError("SEARCH_TIMEOUT", "Query exceeded its time limit") from None
    finally:
        for thread in threads:
            thread.join()
        if job is not None:
            job.Close()
    truncated = len(captured) > cap
    if read_error and not truncated:
        raise ProjectError("DEPENDENCY_UNAVAILABLE", "Could not capture command output")
    del captured[cap:]
    return Output(bytes(captured), process.returncode, truncated)
