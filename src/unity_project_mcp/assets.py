import hashlib
import json
import os
import re
import tempfile
from collections import defaultdict
from collections.abc import Iterator
from functools import cached_property
from pathlib import PurePosixPath
from threading import Lock
from time import monotonic
from typing import TYPE_CHECKING

from .errors import ProjectError
from .git import Entry
from .models import Page, json_bytes
from .security import ensure_no_links, is_reparse, within
from .textfiles import decode_text, json_object

if TYPE_CHECKING:
    from .project import Project


GUID = re.compile(r"[0-9a-fA-F]{32}\Z")
META_GUID = re.compile(r"^guid:[ \t]*([0-9a-fA-F]{32})[ \t]*$", re.M)
OBJECT_ID = re.compile(r"^--- !u!\d+ &(-?\d{1,20})(?:[ \t]|$)", re.M)
FLOW_REFERENCE = re.compile(r"^[ \t]*(?:-[ \t]*)?(?:[\w.-]+[ \t]*:[ \t]*)?\{([^{}]{1,1024})\}", re.M)
FILE_ID = re.compile(r"-?\d{1,20}\Z", re.ASCII)
ASSET_TYPES = frozenset({".prefab", ".unity", ".asset"})
ASSEMBLY_TYPES = frozenset({".asmdef", ".asmref"})
BUILTINS = {f"0000000000000000{letter}000000000000000" for letter in ("d", "e", "f")}
ZERO_GUID = "0" * 32


def unity_yaml(text: str) -> bool:
    return text.startswith(("%YAML ", "--- !u!")) and OBJECT_ID.search(text) is not None


def references(text: str) -> Iterator[tuple[int, int, str | None, bool]]:
    line, position = 1, 0
    for match in FLOW_REFERENCE.finditer(text):
        fields = {}
        for item in match[1].split(","):
            key, separator, value = item.partition(":")
            if separator:
                fields[key.strip()] = value.strip()
        file_id = fields.get("fileID", "")
        if not FILE_ID.fullmatch(file_id):
            continue
        line += text.count("\n", position, match.start())
        position = match.start()
        raw_guid = fields.get("guid")
        guid = raw_guid.lower() if raw_guid is not None else None
        valid = guid is None or bool(GUID.fullmatch(guid))
        yield line, int(file_id), guid if valid else None, valid


class Assets:
    def __init__(self, project: "Project"):
        self.project = project
        self.roots = tuple(f"{project.unity_path}/{suffix}".casefold() for suffix in ("Assets", "Packages"))
        self._build_lock = Lock()
        self._guid_entries: dict[str, list[str]] | None = None
        self._skipped = 0
        key = hashlib.sha256(json_bytes([
            str(project.repository_root), project.commit, project.unity_path, project.policy.includes,
            sorted(project.policy.excluded), project.limits.max_text_file_bytes,
        ])).hexdigest()
        self.cache = project.state_dir / f"guid-{key}.json"

    @property
    def status(self) -> str:
        if self._guid_entries is not None:
            return "ready"
        return "building" if self._build_lock.locked() else "not_built"

    def _unity_files(self, extensions: frozenset[str]) -> Iterator[Entry]:
        for scope in self.roots:
            if scope in self.project.directories:
                yield from self.project._files_in(scope, extensions)

    def _load_cache(self) -> bool:
        project = self.project
        try:
            info = self.cache.lstat()
            if (is_reparse(info) or self.cache.resolve().is_relative_to(project.repository_root)
                    or info.st_size > project.limits.max_index_output_bytes):
                return False
            with self.cache.open("rb") as stream:
                raw = stream.read(project.limits.max_index_output_bytes + 1)
            if len(raw) > project.limits.max_index_output_bytes:
                return False
            cache = json.loads(raw)
            if cache["version"] != 1 or cache["commit"] != project.commit or not isinstance(cache["skipped"], int):
                return False
            entries = cache["guids"]
            for guid, paths in entries.items():
                if not GUID.fullmatch(guid) or not isinstance(paths, list) or not paths:
                    return False
                for path in paths:
                    key = path.casefold()
                    entry = project.files.get(key)
                    if (entry is None or entry.path != path or not key.endswith(".meta")
                            or not any(within(key, root) for root in self.roots)):
                        return False
            self._skipped = cache["skipped"]
            self._guid_entries = entries
            return True
        except (OSError, ValueError, KeyError, TypeError, AttributeError, RecursionError):
            return False

    def _save_cache(self, guids: dict[str, list[str]], skipped: int) -> None:
        project = self.project
        if self.cache.resolve().is_relative_to(project.repository_root):
            raise ProjectError("INVALID_CONFIG", "The GUID cache must be outside the reference repository")
        payload = json_bytes({"version": 1, "commit": project.commit, "skipped": skipped, "guids": guids})
        if len(payload) > project.limits.max_index_output_bytes:
            raise ProjectError("OUTPUT_LIMIT", "The GUID cache exceeds the configured index limit")
        self.cache.parent.mkdir(parents=True, exist_ok=True)
        temporary = None
        try:
            with tempfile.NamedTemporaryFile(dir=self.cache.parent, prefix="guid-", suffix=".tmp", delete=False) as stream:
                temporary = stream.name
                stream.write(payload)
            os.replace(temporary, self.cache)
        finally:
            if temporary is not None and os.path.exists(temporary):
                os.unlink(temporary)

    def _index(self) -> dict[str, list[str]]:
        if self._guid_entries is not None:
            return self._guid_entries
        if not self._build_lock.acquire(blocking=False):
            raise ProjectError("INDEX_BUILDING", "The GUID index is being built. Retry shortly")
        try:
            if self._guid_entries is not None or self._load_cache():
                return self._guid_entries
            project = self.project
            deadline = monotonic() + project.limits.git_timeout_seconds
            guids: dict[str, list[str]] = defaultdict(list)
            skipped = 0

            def candidates() -> Iterator[Entry]:
                nonlocal skipped
                for entry in self._unity_files(frozenset({".meta"})):
                    if monotonic() >= deadline:
                        raise ProjectError("SEARCH_TIMEOUT", "GUID indexing exceeded its time limit")
                    if entry.size > project.limits.max_text_file_bytes:
                        skipped += 1
                    else:
                        yield entry

            for entry, raw in project.git.blobs(candidates(), deadline=deadline):
                try:
                    match = META_GUID.search(decode_text(raw))
                except ProjectError:
                    match = None
                if match:
                    guids[match[1].lower()].append(entry.path)
                else:
                    skipped += 1
            try:
                self._save_cache(guids, skipped)
            except OSError:
                raise ProjectError("INDEX_ERROR", "Could not save the GUID cache in the service state directory") from None
            self._skipped = skipped
            self._guid_entries = guids
            return guids
        finally:
            self._build_lock.release()

    def _locations(self, guid: str) -> Iterator[dict]:
        project = self.project
        paths = self._index().get(guid, [])
        for meta_path in paths:
            project.entry(meta_path)
            path = meta_path[:-5]
            entry = project.files.get(path.casefold())
            allowed = project.policy.allows(path)
            directory = project.directories.get(path.casefold()) if allowed else None
            resolved = entry.path if entry else directory
            if resolved:
                ensure_no_links(project.repository_root, resolved)
            status = "ambiguous_guid" if len(paths) > 1 else ("resolved_asset" if resolved else "missing_asset")
            yield {"guid": guid, "meta_path": meta_path, "asset_path": resolved, "status": status,
                   "asset_type": "file" if entry else ("directory" if directory else None)}

    def find_asset_by_guid(self, guid: str) -> Page:
        if not GUID.fullmatch(guid):
            raise ProjectError("INVALID_ARGUMENT", "Use a 32-character hexadecimal Unity GUID")
        guid = guid.lower()
        if guid in BUILTINS:
            return Page([{"guid": guid, "status": "builtin_resource"}])
        page = self.project.pagination.page(self._locations(guid), self.project.limits.max_search_limit)
        if not page.data:
            page.data.append({"guid": guid, "status": "unresolved_guid"})
        page.details["skipped_meta_files"] = self._skipped
        page.truncated |= bool(self._skipped)
        return page

    def get_asset_references(self, path: str, direction: str = "forward", limit: int | None = None) -> Page:
        project = self.project
        entry = project.entry(path)
        limit = project._limit(limit)
        if direction == "reverse":
            return self._reverse(entry, limit)
        if direction != "forward":
            raise ProjectError("INVALID_ARGUMENT", "direction must be forward or reverse")
        if PurePosixPath(entry.path.casefold()).suffix not in ASSET_TYPES:
            raise ProjectError("UNSUPPORTED_SERIALIZATION", "Use a Unity Prefab, Scene, or Asset file")
        text = project.text(entry)
        if not unity_yaml(text):
            raise ProjectError("UNSUPPORTED_SERIALIZATION", "Could not identify Unity text serialization")
        anchors = {int(match[1]) for match in OBJECT_ID.finditer(text)}

        def rows() -> Iterator[dict]:
            for line, file_id, guid, valid in references(text):
                row = {"path": entry.path, "line": line, "file_id": file_id, "guid": guid, "file_id_verified": False}
                if not valid:
                    row["status"] = "malformed_guid"
                elif not file_id:
                    row["status"] = "null_reference"
                elif guid is None or guid == ZERO_GUID:
                    row["file_id_verified"] = file_id in anchors
                    row["status"] = "local_file_id" if row["file_id_verified"] else "unresolved_file_id"
                elif guid in BUILTINS:
                    row["status"] = "builtin_resource"
                else:
                    target = next(self._locations(guid), None)
                    if target and target["status"] == "resolved_asset":
                        row.update(status="resolved_asset_guid", asset_path=target["asset_path"], meta_path=target["meta_path"])
                    else:
                        row["status"] = target["status"] if target else "unresolved_guid"
                        if target is None:
                            row["possible_reasons"] = ["asset_missing_or_out_of_scope", "package_unavailable"]
                yield row

        page = project.pagination.page(rows(), limit)
        page.details.update(direction="forward", analysis="unity_yaml_flow_references", skipped_meta_files=self._skipped)
        page.truncated |= bool(self._skipped)
        return page

    def _reverse(self, asset: Entry, limit: int) -> Page:
        project = self.project
        meta = project.entry(asset.path + ".meta")
        match = META_GUID.search(project.text(meta))
        if not match or match[1].lower() == ZERO_GUID:
            raise ProjectError("INVALID_DATA", "The asset does not have a valid tracked GUID")
        guid = match[1].lower()
        deadline = monotonic() + project.limits.search_timeout_seconds
        skipped = 0

        def candidates() -> Iterator[str]:
            nonlocal skipped
            for entry in self._unity_files(ASSET_TYPES):
                if entry.size <= project.limits.max_text_file_bytes:
                    yield entry.path
                else:
                    skipped += 1

        def rows() -> Iterator[dict]:
            nonlocal skipped
            for path in project.git.grep_files(guid, project.commit, candidates(), case_sensitive=False, deadline=deadline):
                try:
                    text = project.text(project.entry(path), deadline=deadline)
                except ProjectError as exc:
                    if exc.code not in {"BINARY_FILE", "GIT_LFS_POINTER"}:
                        raise
                    skipped += 1
                    continue
                if unity_yaml(text):
                    for line, file_id, target, valid in references(text):
                        if valid and file_id and target == guid:
                            yield {"path": path, "line": line, "file_id": file_id, "guid": guid,
                                   "confidence": "serialized_candidate"}
                else:
                    skipped += 1

        page = project.pagination.page(rows(), limit)
        page.details.update(direction="reverse", guid=guid, skipped_files=skipped)
        page.truncated |= bool(skipped)
        return page

    @cached_property
    def assemblies(self) -> tuple[dict[str, dict], dict[str, list[str]]]:
        project = self.project
        records = {}
        names: dict[str, list[str]] = defaultdict(list)
        deadline = monotonic() + project.limits.search_timeout_seconds

        def candidates() -> Iterator[Entry]:
            for entry in project._files_in(".", ASSEMBLY_TYPES):
                if entry.size > project.limits.max_text_file_bytes:
                    records[entry.path] = {"status": "FILE_TOO_LARGE"}
                else:
                    yield entry

        for entry, raw in project.git.blobs(candidates(), deadline=deadline):
            ensure_no_links(project.repository_root, entry.path)
            try:
                data = json_object(decode_text(raw))
            except ProjectError as exc:
                records[entry.path] = {"status": exc.code}
                continue
            if entry.path.casefold().endswith(".asmdef"):
                name = data.get("name")
                if not isinstance(name, str) or not name.strip():
                    records[entry.path] = {"status": "INVALID_DATA"}
                    continue
                names[name].append(entry.path)
            records[entry.path] = {"status": "parsed", "data": data}
        return records, names

    def _assembly_reference(self, ref: str, names: dict[str, list[str]], records: dict[str, dict]) -> dict:
        mode = "guid" if ref.upper().startswith("GUID:") else "name"
        candidates = names.get(ref, [])
        if mode == "guid":
            guid = ref[5:].lower()
            candidates = [item["asset_path"] for item in self._locations(guid)
                          if records.get(item["asset_path"], {}).get("status") == "parsed"
                          and item["asset_path"].casefold().endswith(".asmdef")] if GUID.fullmatch(guid) else []
        row = {"reference": ref, "mode": mode, "status": "unresolved"}
        if len(candidates) == 1:
            ensure_no_links(self.project.repository_root, candidates[0])
            row.update(status="resolved", path=candidates[0])
        elif len(candidates) > 1:
            row["status"] = "ambiguous"
        return row

    def get_assembly_info(self, path: str | None = None, limit: int | None = None) -> Page:
        project = self.project
        scope = project._scope(path)
        limit = project._limit(limit)
        records, names = self.assemblies

        def strings(data: dict, key: str) -> list[str]:
            value = data.get(key, [])
            return [item for item in value if isinstance(item, str)] if isinstance(value, list) else []

        def rows() -> Iterator[dict]:
            for entry in project._files_in(scope, ASSEMBLY_TYPES):
                project.entry(entry.path)
                record = records[entry.path]
                kind = "asmref" if entry.path.casefold().endswith(".asmref") else "asmdef"
                row = {"path": entry.path, "kind": kind, "status": record["status"]}
                if record["status"] != "parsed":
                    yield row
                    continue
                data = record["data"]
                refs = strings(data, "references")
                if kind == "asmref" and isinstance(data.get("reference"), str):
                    refs = [data["reference"]]
                row["references"] = [self._assembly_reference(ref, names, records) for ref in refs]
                if kind == "asmdef":
                    row.update(name=data["name"], include_platforms=strings(data, "includePlatforms"),
                               exclude_platforms=strings(data, "excludePlatforms"), define_constraints=strings(data, "defineConstraints"),
                               precompiled_references=strings(data, "precompiledReferences"),
                               auto_referenced=data.get("autoReferenced", True) is True,
                               override_references=data.get("overrideReferences", False) is True)
                yield row

        return project.pagination.page(rows(), limit)
