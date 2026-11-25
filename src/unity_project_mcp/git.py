import os
import re
import subprocess
from collections.abc import Iterable, Iterator
from dataclasses import dataclass
from pathlib import Path
from time import monotonic

from .config import Settings, executable
from .errors import ProjectError
from .process import QUERY_DEADLINE, Output, command_environment, run_bounded
from .security import ensure_no_links, is_reparse, normalize_path


SHA = re.compile(r"(?:[0-9a-f]{40}|[0-9a-f]{64})\Z", re.I)
REGULAR_MODES = {b"100644", b"100755"}


@dataclass(frozen=True)
class Entry:
    path: str
    oid: str
    size: int


@dataclass(frozen=True)
class Baseline:
    commit: str
    branch: str | None
    dirty: bool


def path_batches(paths: Iterable[str]) -> Iterator[list[str]]:
    # CreateProcess has a 32K UTF-16 command line limit, including the executable and options.
    batch: list[str] = []
    units = 0
    for path in paths:
        cost = len(subprocess.list2cmdline([path]).encode("utf-16-le")) // 2 + 1
        if units + cost > 22000:
            yield batch
            batch, units = [], 0
        batch.append(path)
        units += cost
    if batch:
        yield batch


def remaining(deadline: float | None, maximum: float) -> float:
    return maximum if deadline is None else min(maximum, deadline - monotonic())


class Git:
    def __init__(self, root: Path, settings: Settings):
        self.root = root
        self.limits = settings.limits
        self.executable = executable("git", settings.runtime.git, root)
        self.env = command_environment(self.executable)
        self.env.update(
            GIT_CONFIG_NOSYSTEM="1", GIT_CONFIG_GLOBAL=os.devnull, GIT_ATTR_NOSYSTEM="1",
            GIT_OPTIONAL_LOCKS="0", GIT_TERMINAL_PROMPT="0", GIT_NO_LAZY_FETCH="1", GIT_ALLOW_PROTOCOL="",
        )
        self.config_paths: tuple[Path, ...] = ()
        self.config_state: tuple = ()
        self.tracked_directories: list[Path] = []
        local_metadata = root / ".git"
        for path in (".git", ".git/config", ".git/config.worktree", ".git/commondir", ".git/HEAD", ".git/index",
                     ".git/refs", ".git/objects", ".git/objects/info", ".git/objects/pack", ".git/packed-refs"):
            ensure_no_links(root, path)
        if not local_metadata.is_dir() or (local_metadata / "commondir").exists():
            raise ProjectError("INVALID_CONFIG", "repository_root must be the Git repository root with local .git metadata")
        object_store = local_metadata / "objects"
        if (object_store / "info" / "alternates").exists():
            raise ProjectError("INVALID_CONFIG", "The reference must use its own local Git object store")
        for container in (object_store, object_store / "pack"):
            if container.is_dir() and any(is_reparse(item.lstat()) for item in container.iterdir()):
                raise ProjectError("PATH_NOT_ALLOWED", "The Git object store contains a symbolic link or reparse point")
        self.options = {
            "safe.directory": root.as_posix(), "core.fsmonitor": "false", "core.hooksPath": "/dev/null",
            "core.untrackedCache": "false", "core.ignoreStat": "false", "core.quotePath": "false",
            "core.attributesFile": os.devnull, "status.submoduleSummary": "false", "grep.threads": "2",
        }
        self._set_options()
        result = self.run("rev-parse", "--show-toplevel", codes=(0, 128))
        if result.returncode or Path(os.fsdecode(result.stdout.rstrip(b"\r\n"))).resolve() != root:
            raise ProjectError("INVALID_CONFIG", "repository_root must be the Git repository root")

        git_dir, common_dir, index_path = self.run(
            "rev-parse", "--path-format=absolute", "--absolute-git-dir", "--git-common-dir", "--git-path", "index",
        ).stdout.decode("utf-8").splitlines()
        common = Path(common_dir)
        self.index_path = Path(index_path)
        if Path(git_dir) != local_metadata or common != local_metadata or self.index_path != local_metadata / "index":
            raise ProjectError("INVALID_CONFIG", "Git metadata must stay inside the reference repository")
        self.config_paths = (common / "config", Path(git_dir) / "config.worktree", common / "objects" / "info" / "alternates",
                             local_metadata, object_store, object_store / "pack", object_store / "info")
        self.config_state = self._config_stamp()
        self._disable_filters()

    def _set_options(self) -> None:
        self.env["GIT_CONFIG_COUNT"] = str(len(self.options))
        for i, (key, value) in enumerate(self.options.items()):
            self.env[f"GIT_CONFIG_KEY_{i}"] = key
            self.env[f"GIT_CONFIG_VALUE_{i}"] = value

    def _config_stamp(self) -> tuple:
        return tuple(self.file_stamp(path) for path in self.config_paths)

    @staticmethod
    def file_stamp(path: Path) -> tuple | None:
        try:
            info = path.lstat()
        except FileNotFoundError:
            return None
        if is_reparse(info):
            raise ProjectError("PROJECT_CHANGED", "Git metadata contains a symbolic link or reparse point")
        return info.st_mtime_ns, info.st_ctime_ns, info.st_size, info.st_ino

    def _disable_filters(self) -> None:
        flags = ("--no-includes", "--null", "--list")
        local = self.run("config", "--local", *flags, cap=65536).stdout
        worktree = self.run("config", "--worktree", *flags, cap=65536).stdout if self.config_state[1] is not None else b""
        for record in (local + worktree).decode("utf-8").split("\0"):
            key, _, _ = record.partition("\n")
            lower = key.casefold()
            if (lower.startswith(("include.", "includeif.")) or lower == "extensions.partialclone"
                    or (lower.startswith("remote.") and lower.endswith((".promisor", ".partialclonefilter")))):
                raise ProjectError("INVALID_CONFIG", "Use a full Git reference checkout without config includes or promisor remotes")
            if lower.startswith("filter.") and lower.rsplit(".", 1)[-1] in {"clean", "smudge", "process", "required"}:
                name = key.rsplit(".", 1)[0]
                for operation in ("clean", "smudge", "process", "required"):
                    self.options[f"{name}.{operation}"] = "false" if operation == "required" else ""
        self._set_options()

    def run(
        self, *args: str, cap: int = 262144, codes: tuple[int, ...] = (0,),
        allow_partial: bool = False, deadline: float | None = None,
        input_bytes: bytes | None = None, pathspec_magic: bool = False,
    ) -> Output:
        if self.config_paths and self._config_stamp() != self.config_state:
            raise ProjectError("PROJECT_CHANGED", "Git configuration changed since startup")
        pathspec = [] if pathspec_magic else ["--literal-pathspecs"]
        result = run_bounded(
            [str(self.executable), "--no-pager", "--no-optional-locks", "--no-replace-objects", *pathspec, *args],
            self.root, self.env, timeout=remaining(deadline, self.limits.git_timeout_seconds), cap=cap, input_bytes=input_bytes,
        )
        if result.truncated:
            if allow_partial:
                return result
            raise ProjectError("OUTPUT_LIMIT", "Git output exceeded its configured limit")
        if result.returncode not in codes:
            raise ProjectError("GIT_ERROR", "A read-only Git operation failed")
        return result

    def status(self) -> Baseline:
        # Git for Windows may classify a junction as a directory during index refresh.
        # Check tracked ancestors from the index before allowing Git to inspect the worktree.
        deadline = QUERY_DEADLINE.get()
        for path in self.tracked_directories:
            if deadline is not None and monotonic() >= deadline:
                raise ProjectError("SEARCH_TIMEOUT", "Baseline verification exceeded the query time limit")
            try:
                info = path.lstat()
            except (FileNotFoundError, NotADirectoryError):
                continue
            if is_reparse(info):
                raise ProjectError("PROJECT_CHANGED", "A tracked directory contains a symbolic link or reparse point")
        output = self.run(
            "status", "--porcelain=v2", "--branch", "--no-ahead-behind", "--no-renames",
            "--untracked-files=no", "--ignore-submodules=dirty", "-z", cap=65536, allow_partial=True,
        )
        commit = branch = None
        dirty = output.truncated
        for record in output.stdout.split(b"\0"):
            if record.startswith(b"# branch.oid "):
                commit = record[len(b"# branch.oid "):].decode("ascii")
            elif record.startswith(b"# branch.head "):
                branch = record[len(b"# branch.head "):].decode("utf-8", "replace")
            elif record and not record.startswith(b"# "):
                dirty = True
        if commit is None or not SHA.fullmatch(commit):
            raise ProjectError("INVALID_CONFIG", "The reference repository must have a valid HEAD commit")
        return Baseline(commit, None if branch == "(detached)" else branch, dirty)

    def check_index_flags(self) -> None:
        data = self.run("ls-files", "-v", "-z", cap=self.limits.max_index_output_bytes).stdout
        directories = set()
        for record in data.split(b"\0"):
            if not record:
                continue
            if not record.startswith(b"H "):
                raise ProjectError("INVALID_CONFIG", "The reference index must not hide tracked changes (including sparse checkout)")
            path = record[2:].decode("utf-8")
            if normalize_path(path) != path:
                raise ProjectError("INVALID_CONFIG", "The Git index contains an unsafe worktree path")
            parent = path.rpartition("/")[0]
            while parent and parent not in directories:
                directories.add(parent)
                parent = parent.rpartition("/")[0]
        self.tracked_directories = [self.root, *(self.root / path for path in sorted(directories))]

    def entries(self, commit: str) -> Iterator[Entry]:
        data = self.run("ls-tree", "--full-tree", "-r", "-l", "-z", commit,
                        cap=self.limits.max_index_output_bytes).stdout
        for record in data.split(b"\0"):
            if not record:
                continue
            header, path = record.split(b"\t", 1)
            mode, kind, oid, size = header.split()
            if mode in REGULAR_MODES and kind == b"blob" and size.isdigit():
                try:
                    name = path.decode("utf-8")
                except UnicodeDecodeError:
                    continue
                yield Entry(name, oid.decode("ascii"), int(size))

    def grep_files(
        self, query: str, commit: str, paths: Iterable[str], *, case_sensitive: bool, deadline: float,
    ) -> Iterator[str]:
        commit_prefix = (commit + ":").encode()
        bom = b"\xff\xfe\n\xfe\xff\n"
        # Git's case-insensitive matcher requires UTF-8 patterns. Scan raw BOMs separately in that mode.
        searches = [(('-f', '-'), query.encode("utf-8") + b"\n" + bom)] if case_sensitive else [
            (("--ignore-case", "-e", query), None), (("-f", "-"), bom),
        ]
        for batch in path_batches(paths):
            seen: set[bytes] = set()
            for flags, patterns in searches:
                output = self.run(
                    "grep", "--full-name", "--null", "--files-with-matches", "--text", "--fixed-strings",
                    "--no-textconv", "--no-recurse-submodules", *flags, commit, "--", *batch,
                    codes=(0, 1), cap=2 * 1024**2, deadline=deadline, input_bytes=patterns,
                )
                for record in output.stdout.split(b"\0"):
                    if record.startswith(commit_prefix) and record not in seen:
                        seen.add(record)
                        yield record[len(commit_prefix):].decode("utf-8")

    def blob(self, entry: Entry, *, deadline: float | None = None) -> bytes:
        if entry.size > self.limits.max_text_file_bytes:
            raise ProjectError("FILE_TOO_LARGE", "The file exceeds the configured text size limit")
        output = self.run("cat-file", "blob", entry.oid, cap=entry.size, deadline=deadline)
        if len(output.stdout) != entry.size:
            raise ProjectError("GIT_ERROR", "Git returned an incomplete blob")
        return output.stdout

    def blobs(self, entries: Iterable[Entry], *, deadline: float) -> Iterator[tuple[Entry, bytes]]:
        batch: list[Entry] = []
        size = 0
        for entry in entries:
            if entry.size > self.limits.max_text_file_bytes:
                raise ProjectError("FILE_TOO_LARGE", "A file exceeds the configured text size limit")
            estimate = entry.size + len(entry.oid) + 32
            if batch and (size + estimate > 4 * 1024**2 or len(batch) >= 4096):
                yield from self._blob_batch(batch, size, deadline)
                batch, size = [], 0
            batch.append(entry)
            size += estimate
        if batch:
            yield from self._blob_batch(batch, size, deadline)

    def _blob_batch(self, batch: list[Entry], cap: int, deadline: float) -> Iterator[tuple[Entry, bytes]]:
        objects = "\n".join(entry.oid for entry in batch) + "\n"
        output = self.run("cat-file", "--batch", "--buffer", cap=cap, deadline=deadline,
                          input_bytes=objects.encode("ascii")).stdout
        cursor = 0
        for entry in batch:
            header = f"{entry.oid} blob {entry.size}\n".encode("ascii")
            if not output.startswith(header, cursor):
                raise ProjectError("DEPENDENCY_UNAVAILABLE", "A required Git blob is unavailable locally")
            start = cursor + len(header)
            cursor = start + entry.size
            if output[cursor:cursor + 1] != b"\n":
                raise ProjectError("GIT_ERROR", "Git returned an incomplete blob batch")
            yield entry, output[start:cursor]
            cursor += 1

    def checked_commit(self, value: str, *, deadline: float) -> str:
        if not SHA.fullmatch(value):
            raise ProjectError("INVALID_COMMIT", "Use full Git commit SHAs")
        value = value.lower()
        output = self.run("cat-file", "-t", value, codes=(0, 128), deadline=deadline)
        if output.stdout.strip() != b"commit":
            raise ProjectError("INVALID_COMMIT", "The SHA does not identify a local Git commit")
        return value

    def sized_blob(self, path: str, oid: str, *, deadline: float) -> Entry:
        output = self.run("cat-file", "-s", oid, codes=(0, 128), deadline=deadline).stdout.strip()
        if not output.isdigit():
            raise ProjectError("DEPENDENCY_UNAVAILABLE", "A required Git blob is unavailable locally")
        return Entry(path, oid, int(output))
