"""Fetch a configured branch and publish validated, immutable query snapshots."""

import hashlib
import logging
import os
from datetime import datetime, timezone
from pathlib import Path
from tempfile import TemporaryDirectory
from threading import Lock
from time import monotonic
from urllib.parse import urlsplit

from .config import Settings
from .errors import ProjectError
from .git import SHA, Baseline, Git, remaining
from .models import json_bytes
from .process import QUERY_DEADLINE, command_environment, run_bounded
from .project import Project
from .security import ensure_no_links


LOGGER = logging.getLogger(__name__)
ACTIVE_REF = "refs/unity-project-mcp/active"
BOOTSTRAP_TIMEOUT_SECONDS = 300


class RefreshingProject:
    def __init__(self, settings: Settings):
        self.settings = settings
        self.limits = settings.limits
        self.refresh = settings.refresh
        self.lock = Lock()
        self.next_refresh_at = 0.0
        self.last_refresh: dict[str, str] = {}
        root, state, _ = settings.locations()
        reference = Git(root, settings)
        self.executable = reference.executable
        self.branch_ref = f"refs/heads/{self.refresh.branch}"
        reference.run("check-ref-format", self.branch_ref)
        self.remote_url = self._remote_url(reference)
        identity = f"{os.path.normcase(str(root))}\n{self.remote_url}\n{self.branch_ref}"
        state.mkdir(parents=True, exist_ok=True)
        cache = state / ("reference-" + hashlib.sha256(identity.encode()).hexdigest()[:20] + ".git")
        ensure_no_links(state, cache.name)
        if not cache.exists():
            self._seed(reference, cache)
        self.git = Git(cache, settings, bare=True)
        self.current = self._snapshot(self.git, self._commit(self.git, ACTIVE_REF))

    def _remote_url(self, reference: Git) -> str:
        # The name and URL come from administrator-owned configuration.
        raw = reference.run("remote", "get-url", self.refresh.remote, cap=8192).stdout.decode().rstrip("\r\n")
        if not raw or any(ord(char) < 32 for char in raw):
            raise ProjectError("INVALID_CONFIG", "The refresh remote URL is invalid")
        if Path(raw).is_absolute() or raw.startswith("file://"):
            return raw
        url = urlsplit(raw)
        if url.scheme not in {"http", "https"} or not url.hostname or url.username is not None or url.query or url.fragment:
            raise ProjectError("INVALID_CONFIG", "Configure an HTTP(S) refresh remote without credentials in its URL")
        return raw

    @staticmethod
    def _commit(git: Git, ref: str) -> str:
        revision = git.run("rev-parse", "--verify", f"{ref}^{{commit}}").stdout.strip().decode("ascii")
        if not SHA.fullmatch(revision):
            raise ProjectError("REFRESH_FAILED", "Git did not return a valid commit")
        return revision.lower()

    def _snapshot(self, git: Git, commit: str) -> Project:
        project = Project(self.settings, git=git, baseline=Baseline(commit, self.refresh.branch, False))
        info = project.invoke("get_project_info")
        if not info["success"]:
            raise ProjectError("REFRESH_FAILED", "The commit did not pass Unity project validation")
        return project

    def _seed(self, reference: Git, cache: Path) -> None:
        reference.check_index_flags()
        baseline = reference.status()
        if baseline.dirty or baseline.branch != self.refresh.branch:
            raise ProjectError("INVALID_CONFIG", "Initialize the cache from a clean reference on the configured branch")
        with TemporaryDirectory(prefix="reference-seed-", dir=cache.parent) as temporary:
            root = Path(temporary) / "repo.git"
            self._run(Path(temporary), "-c", f"safe.directory={reference.root.as_posix()}",
                      "clone", "--bare", "--no-local", "--no-tags", "--single-branch",
                      "--branch", self.refresh.branch, "--", reference.root.as_posix(), str(root),
                      timeout=BOOTSTRAP_TIMEOUT_SECONDS, protocol="file")
            seeded = Git(root, self.settings, bare=True)
            if self._commit(seeded, self.branch_ref) != baseline.commit:
                raise ProjectError("REFRESH_FAILED", "The local reference changed during cache initialization")
            self._snapshot(seeded, baseline.commit)
            seeded.run("update-ref", ACTIVE_REF, baseline.commit)
            root.rename(cache)

    def _run(self, cwd: Path, *args: str, timeout: float, protocol: str = "") -> None:
        env = command_environment(self.executable)
        env.update(GIT_ALLOW_PROTOCOL=protocol, GIT_NO_LAZY_FETCH="1", GIT_TERMINAL_PROMPT="0",
                   GCM_INTERACTIVE="Never", GIT_CONFIG_NOSYSTEM="1", GIT_CONFIG_GLOBAL=os.devnull)
        if protocol == "http:https":
            # Let the deployment account's Git credential helper authenticate non-interactively.
            env.pop("GIT_CONFIG_NOSYSTEM")
            env.pop("GIT_CONFIG_GLOBAL")
            for key in ("HOME", "USERPROFILE", "HOMEDRIVE", "HOMEPATH", "APPDATA", "LOCALAPPDATA", "PROGRAMDATA", "XDG_CONFIG_HOME"):
                if key in os.environ:
                    env[key] = os.environ[key]
        result = run_bounded(
            [str(self.executable), "--no-pager", "--no-replace-objects", "-c", f"safe.directory={cwd.as_posix()}",
             "-c", "core.hooksPath=/dev/null", "-c", "credential.interactive=false", *args],
            cwd, env, timeout=timeout, cap=8192,
        )
        if result.returncode or result.truncated:
            raise ProjectError("REFRESH_FAILED", "The configured Git command failed")

    def _fetch(self, root: Path, url: str, ref: str, *, timeout: float) -> None:
        protocol = "file" if Path(url).is_absolute() or url.startswith("file://") else "http:https"
        self._run(root, "-c", "fetch.prune=false", "-c", "fetch.pruneTags=false", "fetch", "--no-tags",
                  "--no-recurse-submodules", "--no-auto-maintenance", "--no-write-commit-graph", "--", url, ref,
                  timeout=timeout, protocol=protocol)

    def fetch(self, deadline: float) -> Project:
        for path in ("FETCH_HEAD", "refs/unity-project-mcp/active"):
            ensure_no_links(self.git.root, path)
        self.git.verify()
        self._fetch(self.git.root, self.remote_url, self.branch_ref, timeout=remaining(deadline, self.refresh.timeout_seconds))
        commit = self._commit(self.git, "FETCH_HEAD")
        if commit == self.current.commit:
            return self.current
        if self.git.run("merge-base", "--is-ancestor", self.current.commit, commit, codes=(0, 1)).returncode:
            raise ProjectError("REFRESH_FAILED", "The remote branch cannot fast-forward the current baseline")
        candidate = self._snapshot(self.git, commit)
        try:
            self.git.run("update-ref", ACTIVE_REF, commit, self.current.commit)
        except ProjectError:
            # A timed-out process can still have committed the atomic ref update.
            token = QUERY_DEADLINE.set(monotonic() + 1)
            try:
                if self._commit(self.git, ACTIVE_REF) != commit:
                    raise
            finally:
                QUERY_DEADLINE.reset(token)
        return candidate

    def _refresh_current(self, deadline: float) -> tuple[Project, dict[str, str]]:
        with self.lock:
            if monotonic() >= self.next_refresh_at:
                deadline = min(deadline, monotonic() + self.refresh.timeout_seconds)
                token = QUERY_DEADLINE.set(deadline)
                status = "failed"
                try:
                    candidate = self.fetch(deadline)
                    status = "unchanged" if candidate is self.current else "updated"
                    self.current = candidate
                    LOGGER.info("reference refresh=%s commit=%s", status, self.current.commit)
                except (OSError, ProjectError) as exc:
                    LOGGER.warning("reference refresh=failed commit=%s error=%s", self.current.commit,
                                   exc.code if isinstance(exc, ProjectError) else "IO_ERROR")
                finally:
                    QUERY_DEADLINE.reset(token)
                    self.next_refresh_at = monotonic() + self.refresh.cooldown_seconds
                    self.last_refresh = {"status": status, "checked_at": datetime.now(timezone.utc).isoformat()}
            return self.current, self.last_refresh

    def invoke(self, name: str, *, expected_commit: str | None = None, **arguments) -> dict:
        if name != "get_project_info" or expected_commit is not None:
            return self.current.invoke(name, expected_commit=expected_commit, **arguments)
        started = monotonic()
        query_deadline = started + self.limits.query_timeout_seconds
        response_budget = min(5, self.limits.query_timeout_seconds / 2)
        current, refresh = self._refresh_current(query_deadline - response_budget)
        token = QUERY_DEADLINE.set(query_deadline)
        try:
            result = current.invoke(name, **arguments)
        finally:
            QUERY_DEADLINE.reset(token)
        result["meta"]["elapsed_ms"] = round((monotonic() - started) * 1000)
        result["meta"]["refresh"] = refresh.copy()
        if len(json_bytes(result)) > self.limits.max_response_bytes:
            return current.failed(name, started, ProjectError("OUTPUT_LIMIT", "Result exceeded the configured response limit"))
        return result

    def failed(self, name: str, started: float, error: ProjectError) -> dict:
        return self.current.failed(name, started, error)
