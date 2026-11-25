import re
from collections.abc import Iterator
from dataclasses import dataclass
from pathlib import PurePosixPath
from time import monotonic
from typing import TYPE_CHECKING

from .errors import ProjectError
from .git import REGULAR_MODES, SHA
from .models import Page, json_bytes
from .security import ensure_no_links, normalize_path, within
from .textfiles import TEXT_EXTENSIONS, clip, decode_text, redact_package_json, redact_urls

if TYPE_CHECKING:
    from .project import Project


DIFF_FLAGS = ("--no-ext-diff", "--no-textconv", "--no-renames", "--no-color", "--no-relative",
              "--ignore-submodules=all", "--no-abbrev")
NO_FILE = b"000000"
LOG_RECORD = b"\0unity-mcp-commit\0"


@dataclass(frozen=True)
class Change:
    path: str
    status: str
    old: str | None
    new: str | None


class History:
    def __init__(self, project: "Project"):
        self.project = project

    def _pathspecs(self, path: str | None) -> tuple[str, list[str]]:
        project = self.project
        rel = project.policy.require(path, ancestors=True) if path is not None else "."
        key = rel.casefold()
        directories = [rel] if project.policy.allows(rel) else [
            root for root in project.policy.includes if within(root.casefold(), key)
        ]
        return key, [f":(top,icase,literal){item}" for item in directories]

    def _changes(self, raw: bytes, scope: str) -> Iterator[Change]:
        project = self.project
        fields = iter(raw.split(b"\0"))
        for header in fields:
            header = header.lstrip(b"\n")
            if not header:
                continue
            path = next(fields, b"")
            pieces = header.removeprefix(b":").split()
            if not header.startswith(b":") or len(pieces) != 5 or not path:
                raise ProjectError("GIT_ERROR", "Invalid Git change metadata")
            old_mode, new_mode, old_oid, new_oid, status = pieces
            if any(mode != NO_FILE and mode not in REGULAR_MODES for mode in (old_mode, new_mode)):
                continue
            try:
                name = path.decode("utf-8")
                if normalize_path(name) != name:
                    continue
            except (ProjectError, UnicodeDecodeError):
                continue
            if not within(name.casefold(), scope) or not project.policy.allows(name):
                continue
            ensure_no_links(project.repository_root, name)
            yield Change(name, status.decode("ascii"), old_oid.decode("ascii") if old_mode != NO_FILE else None,
                         new_oid.decode("ascii") if new_mode != NO_FILE else None)

    def get_git_log(self, path: str | None = None, limit: int = 10) -> Page:
        if not 1 <= limit <= 50:
            raise ProjectError("INVALID_ARGUMENT", "Git log limit must be between 1 and 50")
        project = self.project
        scope, pathspecs = self._pathspecs(path)
        deadline = monotonic() + project.limits.search_timeout_seconds
        scan = min(limit * 4 + 8, 200)
        output = project.git.run(
            "log", "--root", "--raw", "-z", "--no-show-signature", "--no-notes", *DIFF_FLAGS,
            f"--max-count={scan + 1}", "--format=%x00unity-mcp-commit%x00%H%x00%cI%x00%an%x00%s",
            project.commit, "--", *pathspecs, cap=project.limits.max_index_output_bytes, allow_partial=True,
            deadline=deadline, pathspec_magic=True,
        )
        records = output.stdout.split(LOG_RECORD)[1:]
        if output.truncated:
            records = records[:-1]
            if not records:
                raise ProjectError("OUTPUT_LIMIT", "Narrow the Git log scope or increase its index output limit")

        def rows() -> Iterator[dict]:
            for record in records[:scan]:
                commit, date, author, subject, changes = record.split(b"\0", 4)
                commit = commit.decode("ascii")
                if not SHA.fullmatch(commit):
                    raise ProjectError("GIT_ERROR", "Invalid Git history record")
                if next(self._changes(changes, scope), None) is not None:
                    yield {"commit": commit, "commit_time": date.decode("ascii"),
                           "author": clip(author.decode("utf-8", "replace"), 128)[0],
                           "subject": clip(redact_urls(subject.decode("utf-8", "replace")), 256)[0]}

        page = project.pagination.page(rows(), limit)
        page.truncated |= output.truncated or len(records) > scan
        return page

    def _kind(self, change: Change, deadline: float) -> str:
        if PurePosixPath(change.path.casefold()).suffix not in TEXT_EXTENSIONS:
            return "binary_or_unsupported"
        for oid in (change.old, change.new):
            if oid is None:
                continue
            git = self.project.git
            entry = git.sized_blob(change.path, oid, deadline=deadline)
            try:
                raw = git.blob(entry, deadline=deadline)
                decode_text(raw)
                if b"\0" in raw:
                    return "non_utf8_text"
            except ProjectError as exc:
                if exc.code not in {"FILE_TOO_LARGE", "BINARY_FILE", "GIT_LFS_POINTER"}:
                    raise
                return exc.code.lower()
        return "text"

    def get_git_diff(self, from_commit: str, to_commit: str, path: str | None = None, max_lines: int | None = None) -> Page:
        project = self.project
        if max_lines is None:
            max_lines = min(200, project.limits.max_read_lines)
        if not 1 <= max_lines <= project.limits.max_read_lines:
            raise ProjectError("INVALID_ARGUMENT", "max_lines is outside the configured diff range")
        if any(len(commit) != len(project.commit) for commit in (from_commit, to_commit)):
            raise ProjectError("INVALID_COMMIT", "Use full SHAs in the repository's Git object format")
        scope, pathspecs = self._pathspecs(path)
        deadline = monotonic() + project.limits.search_timeout_seconds
        old, new = (project.git.checked_commit(value, deadline=deadline) for value in (from_commit, to_commit))
        output = project.git.run("diff", "--raw", "-z", *DIFF_FLAGS, old, new, "--", *pathspecs,
                                 cap=project.limits.max_index_output_bytes, deadline=deadline, pathspec_magic=True)
        page = Page(details={"from_commit": old, "to_commit": new, "patch_lines": 0})
        bytes_left = project.limits.max_response_bytes - 384
        lines_left = max_lines
        for change in self._changes(output.stdout, scope):
            if not lines_left or len(page.data) >= project.limits.max_search_limit:
                page.truncated = True
                break
            row = {"path": change.path, "status": change.status, "kind": "binary_or_unsupported", "patch": "", "truncated": False}
            if bytes_left < len(json_bytes(row)) + 1:
                if not page.data:
                    raise ProjectError("OUTPUT_LIMIT", "One diff item exceeds the configured response limit")
                page.truncated = True
                break
            row["kind"] = self._kind(change, deadline)
            if row["kind"] == "text":
                room = bytes_left - len(json_bytes(row)) - 1
                # A positive Git pathspec also matches descendants across file/directory transitions.
                escaped = re.sub(r"([\[\]])", r"\\\1", change.path)
                patch = project.git.run(
                    "diff", *DIFF_FLAGS, "--patch", "--unified=3", "--no-prefix", "--no-text",
                    old, new, "--", f":(top,literal){change.path}", f":(exclude,top,glob){escaped}/**",
                    cap=max(room, 0), allow_partial=True, deadline=deadline, pathspec_magic=True,
                )
                raw_lines = patch.stdout.splitlines(keepends=True)
                if patch.truncated and raw_lines and not raw_lines[-1].endswith(b"\n"):
                    raw_lines.pop()
                lines = []
                redact = change.path.casefold() in {project.manifest_path.casefold(), project.lock_path.casefold()}
                for raw in raw_lines:
                    line = raw.decode("utf-8", "replace")
                    if redact:
                        line = redact_package_json(line)
                    cost = len(json_bytes(line)) - 2
                    if not lines_left or cost > room:
                        row["truncated"] = True
                        break
                    lines.append(line)
                    room -= cost
                    lines_left -= 1
                row["patch"] = "".join(lines)
                row["truncated"] |= patch.truncated
            bytes_left -= len(json_bytes(row)) + 1
            page.truncated |= row["truncated"]
            page.data.append(row)
        page.details["patch_lines"] = max_lines - lines_left
        return page
