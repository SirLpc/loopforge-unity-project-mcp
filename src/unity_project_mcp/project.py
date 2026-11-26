import logging
import re
from bisect import bisect_left
from collections import deque
from collections.abc import Iterable, Iterator
from functools import cached_property
from itertools import islice
from pathlib import PurePosixPath
from threading import Event
from time import monotonic

from .assets import Assets
from .config import DEFAULT_FOLDERS, Settings, executable
from .errors import ProjectError
from .git import Baseline, Entry, Git, remaining
from .history import History
from .models import Page, Pagination, json_bytes
from .process import QUERY_DEADLINE, command_environment, run_bounded
from .security import PathPolicy, ensure_no_links, normalize_path
from .textfiles import TEXT_EXTENSIONS, clip, decode_text, json_object, package_rows, redact_package_json, text_lines


LOGGER = logging.getLogger(__name__)
P0_TOOLS = frozenset({
    "get_project_info", "list_directory", "search_files", "search_code", "read_file", "find_references", "get_packages",
})
ASSET_TOOLS = frozenset({"find_asset_by_guid", "get_asset_references", "get_assembly_info"})
HISTORY_TOOLS = frozenset({"get_git_log", "get_git_diff"})
TOOL_NAMES = P0_TOOLS | ASSET_TOOLS | HISTORY_TOOLS
SEARCH_TOOLS = HISTORY_TOOLS | {"search_files", "search_code", "find_references", "list_directory"}


def literal(value: str) -> str:
    if not value.strip() or len(value) > 256 or any(ord(char) < 32 for char in value):
        raise ProjectError("INVALID_ARGUMENT", "Provide a single-line literal of at most 256 characters")
    return value


def suffixes(values: list[str] | None) -> frozenset[str]:
    if not values:
        return frozenset()
    if len(values) > 32 or any(not re.fullmatch(r"\.?[a-zA-Z0-9_-]{1,24}", value) for value in values):
        raise ProjectError("INVALID_ARGUMENT", "Provide simple file extensions")
    return frozenset("." + value.lstrip(".").casefold() for value in values)


class Project:
    def __init__(self, settings: Settings, *, git: Git | None = None, baseline: Baseline | None = None):
        self.settings = settings
        self.limits = settings.limits
        self.repository_root, self.state_dir, _ = settings.locations()
        self.service_root = settings.service_root.resolve()
        reference_root = self.repository_root
        if git is not None:
            self.repository_root = git.root
        if not self.repository_root.is_dir():
            raise ProjectError("INVALID_CONFIG", "repository_root must be an existing Git repository root")
        self.git = git or Git(self.repository_root, settings)
        self.rg = executable("rg", settings.runtime.rg, reference_root)
        if self.git.bare and self.rg.is_relative_to(self.repository_root):
            raise ProjectError("INVALID_CONFIG", "rg must be installed outside the refresh cache")
        self.rg_env = command_environment(self.rg)
        self.unity_path = normalize_path(settings.project.unity_project_path)
        if self.unity_path == ".":
            raise ProjectError("INVALID_CONFIG", "unity_project_path must be a subdirectory of repository_root")
        ensure_no_links(self.repository_root, self.unity_path)
        self.unity_project_root = self.repository_root / self.unity_path
        self.version_path = f"{self.unity_path}/ProjectSettings/ProjectVersion.txt"
        self.manifest_path = f"{self.unity_path}/Packages/manifest.json"
        self.lock_path = f"{self.unity_path}/Packages/packages-lock.json"
        self.policy = PathPolicy(self.repository_root, self.unity_path, settings.search.include_paths,
                                 settings.search.exclude_directories)
        self.changed = Event()
        if baseline is None:
            for folder in (*DEFAULT_FOLDERS[:3], "ProjectSettings/ProjectVersion.txt"):
                rel = f"{self.unity_path}/{folder}"
                ensure_no_links(self.repository_root, rel)
                required = self.repository_root / rel
                if not (required.is_file() if rel == self.version_path else required.is_dir()):
                    raise ProjectError("INVALID_CONFIG", "Unity project requires Assets, Packages, ProjectSettings and ProjectVersion.txt")
            self.git.check_index_flags()
            self.index_stamp = self.git.file_stamp(self.git.index_path)
            baseline = self.git.status()
            if baseline.dirty:
                raise ProjectError("PROJECT_CHANGED", "The reference repository has uncommitted tracked changes")
        self.baseline = baseline
        self.commit = self.baseline.commit
        self.commit_time = self.git.run("show", "-s", "--no-show-signature", "--no-notes", "--format=%cI", self.commit,
                                        "--").stdout.decode("ascii").strip()
        self.pagination = Pagination(self.commit, self.limits.max_response_bytes)
        self.files: dict[str, Entry] = {}
        self.directories = {".": "."}
        self._build_catalog()
        self.file_keys = sorted(self.files)
        self.node_keys = sorted(self.files.keys() | self.directories.keys())
        self.assets = Assets(self)
        self.history = History(self)
        self.verify()

    def _build_catalog(self) -> None:
        required_folders = {f"{self.unity_path}/{folder}" for folder in DEFAULT_FOLDERS[:3]} if self.git.bare else set()
        has_version = not self.git.bare
        for entry in self.git.entries(self.commit):
            if self.git.bare and entry.path.startswith(self.unity_path + "/"):
                folder, separator, _ = entry.path[len(self.unity_path) + 1:].partition("/")
                if separator:
                    required_folders.discard(f"{self.unity_path}/{folder}")
                has_version |= entry.path == self.version_path
            try:
                if normalize_path(entry.path) != entry.path or not self.policy.allows(entry.path):
                    continue
            except ProjectError:
                continue
            key = entry.path.casefold()
            if key in self.files or key in self.directories:
                raise ProjectError("INVALID_CONFIG", "Tracked paths collide under Windows case-insensitive rules")
            self.files[key] = entry
            parent = entry.path.rpartition("/")[0]
            while parent:
                parent_key = parent.casefold()
                if parent_key in self.files or self.directories.get(parent_key, parent) != parent:
                    raise ProjectError("INVALID_CONFIG", "Tracked paths collide under Windows case-insensitive rules")
                if parent_key in self.directories:
                    break
                self.directories[parent_key] = parent
                parent = parent.rpartition("/")[0]

        if required_folders or not has_version:
            raise ProjectError("INVALID_CONFIG", "The commit is missing required Unity project files")

    def verify(self) -> None:
        if self.git.bare:
            self.git.verify()
            return
        if self.changed.is_set():
            raise ProjectError("PROJECT_CHANGED", "The reference baseline changed. Stop the service and restart after verification")
        try:
            changed = (self.git.file_stamp(self.git.index_path) != self.index_stamp or self.git.status() != self.baseline)
        except ProjectError as exc:
            if exc.code == "SEARCH_TIMEOUT":
                raise
            changed = True
        except OSError:
            changed = True
        if changed:
            self.changed.set()
            raise ProjectError("PROJECT_CHANGED", "The reference baseline or tracked worktree changed")

    def invoke(self, name: str, *, expected_commit: str | None = None, **arguments) -> dict:
        started = monotonic()
        duration = self.limits.query_timeout_seconds
        if name in SEARCH_TOOLS:
            duration = min(duration, self.limits.search_timeout_seconds)
        deadline = started + duration
        if outer_deadline := QUERY_DEADLINE.get():
            deadline = min(deadline, outer_deadline)
        deadline_token = QUERY_DEADLINE.set(deadline)
        try:
            if name not in TOOL_NAMES:
                raise ProjectError("INVALID_ARGUMENT", "Unknown project tool")
            self.verify()
            if expected_commit is not None and expected_commit.casefold() != self.commit:
                raise ProjectError("COMMIT_MISMATCH", "expected_commit does not match the service baseline")
            service = self.assets if name in ASSET_TOOLS else self.history if name in HISTORY_TOOLS else self
            page = getattr(service, name)(**arguments)
            self.verify()
            result = self._result(started, page)
            if len(json_bytes(result)) > self.limits.max_response_bytes:
                raise ProjectError("OUTPUT_LIMIT", "Result exceeded the configured response limit")
            LOGGER.info("tool=%s elapsed_ms=%d count=%d", name, result["meta"]["elapsed_ms"], len(page.data))
            return result
        except ProjectError as exc:
            return self.failed(name, started, exc)
        except Exception:
            return self.failed(name, started, ProjectError("INTERNAL_ERROR", "The project query could not be completed"))
        finally:
            QUERY_DEADLINE.reset(deadline_token)

    def _result(self, started: float, page: Page) -> dict:
        return {"success": True, "commit": self.commit, "data": page.data, "meta": {
            "returned_count": len(page.data), "truncated": page.truncated, "next_cursor": page.next_cursor,
            "elapsed_ms": round((monotonic() - started) * 1000), **page.details,
        }}

    def failed(self, name: str, started: float, error: ProjectError) -> dict:
        result = self._result(started, Page())
        result.update(success=False, error={"code": error.code, "message": str(error)})
        LOGGER.warning("tool=%s elapsed_ms=%d count=0 error=%s", name, result["meta"]["elapsed_ms"], error.code)
        return result

    def _limit(self, value: int | None) -> int:
        if value is None:
            return self.limits.default_search_limit
        if not 1 <= value <= self.limits.max_search_limit:
            raise ProjectError("INVALID_ARGUMENT", "limit is outside the configured range")
        return value

    def _scope(self, path: str | None) -> str:
        return self.policy.require(path, ancestors=True).casefold() if path is not None else "."

    @staticmethod
    def _descendants(keys: list[str], scope: str) -> Iterator[str]:
        prefix = "" if scope == "." else scope + "/"
        index = bisect_left(keys, prefix)
        while index < len(keys) and keys[index].startswith(prefix):
            yield keys[index]
            index += 1

    def _files_in(self, scope: str, extensions: frozenset[str] = frozenset()) -> Iterator[Entry]:
        if scope in self.files:
            keys: Iterable[str] = (scope,)
        elif scope in self.directories:
            keys = self._descendants(self.file_keys, scope)
        else:
            raise ProjectError("FILE_NOT_FOUND", "Search path is absent from the baseline index")
        for key in keys:
            if not extensions or PurePosixPath(key).suffix in extensions:
                yield self.files[key]

    def entry(self, path: str) -> Entry:
        key = self.policy.require(path).casefold()
        if key not in self.files:
            raise ProjectError("FILE_NOT_FOUND", "File is not tracked in the service baseline")
        entry = self.files[key]
        ensure_no_links(self.repository_root, entry.path)
        return entry

    def text(self, entry: Entry, *, deadline: float | None = None) -> str:
        ensure_no_links(self.repository_root, entry.path)
        if PurePosixPath(entry.path.casefold()).suffix not in TEXT_EXTENSIONS:
            raise ProjectError("BINARY_FILE", "The file extension is outside the supported text types")
        return decode_text(self.git.blob(entry, deadline=deadline))

    def _public_text(self, entry: Entry, *, deadline: float | None = None) -> str:
        text = self.text(entry, deadline=deadline)
        if entry.path.casefold() in {self.manifest_path.casefold(), self.lock_path.casefold()}:
            return redact_package_json(text)
        return text

    @cached_property
    def packages(self) -> list[dict]:
        manifest = json_object(self.text(self.entry(self.manifest_path)))
        has_lock = self.lock_path.casefold() in self.files
        lock = json_object(self.text(self.entry(self.lock_path))) if has_lock else {}
        return list(package_rows(manifest, lock))

    def get_project_info(self) -> Page:
        version = None
        if self.policy.allows(self.version_path):
            text = self.text(self.entry(self.version_path))
            version_line = re.search(r"^m_EditorVersion:[ \t]*([a-zA-Z0-9._-]+)[ \t]*$", text, re.M)
            if not version_line:
                raise ProjectError("INVALID_DATA", "Unity editor version is missing from ProjectVersion.txt")
            version = version_line[1]
        info = {
            "name": self.settings.project.name, "unity_project_path": self.unity_path, "unity_version": version,
            "branch": self.baseline.branch, "last_commit_time": self.commit_time, "tracked_dirty": self.baseline.dirty,
            "package_count": len(self.packages) if self.policy.allows(self.manifest_path) else None,
            "include_paths": list(self.policy.includes), "tracked_file_count": len(self.files),
            "main_directories": [directory for key, directory in self.directories.items()
                                 if key.rpartition("/")[0] == self.unity_path.casefold()],
            "project_status": "ready", "file_index_status": "ready", "guid_index_status": self.assets.status,
        }
        return Page([info])

    def list_directory(self, path: str | None = None, depth: int = 1, limit: int | None = None,
                       cursor: str | None = None) -> Page:
        if not 1 <= depth <= 3:
            raise ProjectError("INVALID_ARGUMENT", "depth must be between 1 and 3")
        scope = self._scope(path if path is not None else self.unity_path)
        if scope not in self.directories:
            raise ProjectError("FILE_NOT_FOUND", "Directory is absent from the baseline index")
        maximum_slashes = depth - 1 if scope == "." else scope.count("/") + depth

        def rows() -> Iterator[dict]:
            for key in self._descendants(self.node_keys, scope):
                if key == "." or key.count("/") > maximum_slashes:
                    continue
                if entry := self.files.get(key):
                    ensure_no_links(self.repository_root, entry.path)
                    yield {"path": entry.path, "type": "file", "size": entry.size}
                else:
                    rel = self.directories[key]
                    ensure_no_links(self.repository_root, rel)
                    yield {"path": rel, "type": "directory"}

        return self.pagination.page(rows(), self._limit(limit), binding=("directory", scope, depth), cursor=cursor)

    def search_files(self, query: str, extensions: list[str] | None = None, path: str | None = None,
                     limit: int | None = None, cursor: str | None = None) -> Page:
        query = literal(query).casefold()
        scope, extensions_set = self._scope(path), suffixes(extensions)

        def rows() -> Iterator[dict]:
            for entry in self._files_in(scope, extensions_set):
                if query in entry.path.casefold():
                    ensure_no_links(self.repository_root, entry.path)
                    yield {"path": entry.path, "type": "file", "size": entry.size}

        return self.pagination.page(rows(), self._limit(limit), binding=("files", scope, query, sorted(extensions_set)),
                                    cursor=cursor)

    def read_file(self, path: str, start_line: int = 1, end_line: int | None = None) -> Page:
        if start_line < 1 or (end_line is not None and end_line < start_line):
            raise ProjectError("INVALID_ARGUMENT", "Use a valid 1-based inclusive line range")
        entry = self.entry(path)
        limit = self.limits.default_read_lines if end_line is None else min(end_line - start_line + 1, self.limits.max_read_lines)
        text = self._public_text(entry)
        lines = enumerate(text_lines(text), 1)
        rows = (self._line(number, line) for number, line in islice(lines, min(start_line - 1, len(text)), None))
        page = self.pagination.page(rows, limit)
        last_line = page.data[-1]["line"] if page.data else None
        page.details.update(path=entry.path, start_line=start_line, end_line=last_line,
                            next_line=last_line + 1 if last_line is not None and page.truncated else None)
        page.truncated |= any(row["truncated"] for row in page.data)
        return page

    def _line(self, number: int, text: str, *, group_size: int = 1) -> dict:
        # Reserve space for metadata, line numbers and JSON escaping.
        maximum = min(4096, max(16, (self.limits.max_response_bytes - 700) // (3 * group_size)))
        content, trimmed = clip(text, maximum)
        if len(json_bytes(content)) > maximum + 2:
            content, _ = clip(content, maximum // 2)
            trimmed = True
        return {"line": number, "text": content, "truncated": trimmed}

    def search_code(self, query: str, path: str | None = None, extensions: list[str] | None = None,
                    case_sensitive: bool = False, context_lines: int = 0, limit: int | None = None) -> Page:
        extensions_set = suffixes(extensions) if extensions is not None else frozenset({".cs"})
        if not extensions_set or not extensions_set <= TEXT_EXTENSIONS or not 0 <= context_lines <= 5:
            raise ProjectError("INVALID_ARGUMENT", "Use supported text extensions and at most 5 context lines")
        return self.matches(query, path, extensions_set, case_sensitive, context_lines, limit)

    def find_references(self, symbol: str, path: str | None = None, limit: int | None = None) -> Page:
        if not re.fullmatch(r"[^\W\d]\w{0,127}", symbol):
            raise ProjectError("INVALID_ARGUMENT", "Use a single identifier as the symbol")
        page = self.matches(symbol, path, frozenset({".cs"}), True, 0, limit, symbol=True)
        page.details["analysis"] = "text_candidates"
        return page

    def matches(self, query: str, path: str | None, extensions: frozenset[str], case_sensitive: bool,
                context: int, limit: int | None, *, symbol: bool = False, deadline: float | None = None) -> Page:
        query, scope, limit = literal(query), self._scope(path), self._limit(limit)
        deadline = deadline if deadline is not None else monotonic() + self.limits.search_timeout_seconds
        word = re.compile(rf"(?<!\w){re.escape(query)}(?!\w)") if symbol else None
        needle = query if case_sensitive else query.casefold()
        skipped = 0
        clipped = False

        def candidates() -> Iterator[tuple[Entry, str]]:
            nonlocal skipped
            if scope in self.files:
                for entry in self._files_in(scope, extensions):
                    text = self._public_text(entry, deadline=deadline)
                    flags = [] if case_sensitive else ["--ignore-case"]
                    found = run_bounded(
                        [str(self.rg), "--no-config", "--fixed-strings", "--quiet", *flags, "-e", query, "--", "-"],
                        self.repository_root, self.rg_env, timeout=remaining(deadline, self.limits.search_timeout_seconds),
                        cap=1024, input_bytes=text.encode("utf-8"),
                    )
                    if found.returncode == 0:
                        yield entry, text
                    elif found.returncode != 1:
                        raise ProjectError("SEARCH_ERROR", "Ripgrep could not search the requested file")
                return

            def candidate_paths() -> Iterator[str]:
                nonlocal skipped
                for entry in self._files_in(scope, extensions):
                    if monotonic() >= deadline:
                        raise ProjectError("SEARCH_TIMEOUT", "Search exceeded its time limit")
                    if entry.size <= self.limits.max_text_file_bytes:
                        yield entry.path
                    else:
                        skipped += 1

            for path in self.git.grep_files(query, self.commit, candidate_paths(), case_sensitive=case_sensitive,
                                            deadline=deadline):
                entry = self.files[path.casefold()]
                try:
                    yield entry, self._public_text(entry, deadline=deadline)
                except ProjectError as exc:
                    if exc.code not in {"BINARY_FILE", "GIT_LFS_POINTER"}:
                        raise
                    skipped += 1

        def rows() -> Iterator[dict]:
            nonlocal clipped
            for entry, text in candidates():
                lines = enumerate(text_lines(text), 1)
                before: deque[tuple[int, str]] = deque(maxlen=context)
                upcoming = deque(islice(lines, context + 1))
                while upcoming:
                    number, line = upcoming.popleft()
                    if monotonic() >= deadline:
                        raise ProjectError("SEARCH_TIMEOUT", "Search exceeded its time limit")
                    matches = word.search(line) if word else needle in (line if case_sensitive else line.casefold())
                    if matches:
                        row = self._line(number, line, group_size=2 * context + 1)
                        row["path"] = entry.path
                        if context:
                            row["context"] = [self._line(n, value, group_size=2 * context + 1)
                                              for n, value in (*before, *upcoming)]
                        if symbol:
                            row["confidence"] = "text_candidate"
                        clipped |= row["truncated"] or any(item["truncated"] for item in row.get("context", ()))
                        yield row
                    before.append((number, line))
                    if following := next(lines, None):
                        upcoming.append(following)

        page = self.pagination.page(rows(), limit)
        page.truncated |= bool(skipped or clipped)
        page.details["skipped_files"] = skipped
        return page

    def get_packages(self) -> Page:
        # Re-check access even after parsing immutable package metadata once.
        self.entry(self.manifest_path)
        has_lock = self.lock_path.casefold() in self.files
        if has_lock:
            self.entry(self.lock_path)
        page = self.pagination.page(self.packages, self.limits.max_search_limit)
        page.details.update(manifest_path=self.files[self.manifest_path.casefold()].path, lock_present=has_lock,
                            package_count=len(self.packages))
        return page
