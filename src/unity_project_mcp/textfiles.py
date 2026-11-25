import codecs
import json
import re
from collections.abc import Iterator
from urllib.parse import urlsplit

from .errors import ProjectError


TEXT_EXTENSIONS = frozenset({
    ".cs", ".asmdef", ".asmref", ".json", ".xml", ".yaml", ".yml", ".txt", ".md",
    ".shader", ".hlsl", ".cginc", ".compute", ".uss", ".uxml", ".prefab", ".unity", ".asset", ".meta",
})
CONTROL = re.compile(r"[\x00-\x08\x0b\x0e-\x1f\x7f-\x9f]")
URL = re.compile(r"[a-zA-Z][a-zA-Z0-9+.-]*://[^\s\"'<>]+")
SCP_URL = re.compile(r"(?:[^\s\"'<>/:]+@)?[a-zA-Z0-9.-]+:[^\s\"'<>]+\.git(?:[?#][^\s\"'<>]*)?")
JSON_STRING = re.compile(r'"(?:[^"\\\r\n]|\\[^\r\n])*"')
PACKAGE_NAME = re.compile(r"[a-zA-Z0-9][a-zA-Z0-9._-]{0,149}\Z")
PACKAGE_SOURCES = {"registry", "builtin", "embedded", "local", "git", "tarball", "local-tarball"}


def decode_text(raw: bytes) -> str:
    if raw.startswith(b"version https://git-lfs.github.com/spec/v1"):
        raise ProjectError("GIT_LFS_POINTER", "The baseline contains a Git LFS pointer for this file")
    encoding = "utf-8-sig"
    if raw.startswith((codecs.BOM_UTF32_BE, codecs.BOM_UTF32_LE)):
        encoding = "utf-32"
    elif raw.startswith((codecs.BOM_UTF16_BE, codecs.BOM_UTF16_LE)):
        encoding = "utf-16"
    try:
        content = raw.decode(encoding)
    except UnicodeDecodeError:
        raise ProjectError("BINARY_FILE", "The file is binary or uses an unsupported text encoding") from None
    if CONTROL.search(content):
        raise ProjectError("BINARY_FILE", "The file contains binary control bytes")
    return content.replace("\r\n", "\n").replace("\r", "\n")


def text_lines(text: str) -> Iterator[str]:
    start = 0
    while start < len(text):
        finish = text.find("\n", start)
        if finish < 0:
            yield text[start:]
            break
        yield text[start:finish]
        start = finish + 1


def clip(text: str, budget: int) -> tuple[str, bool]:
    encoded = text.encode("utf-8")
    if len(encoded) <= budget:
        return text, False
    return encoded[:budget].decode("utf-8", "ignore"), True


def url_origin(value: str) -> str:
    try:
        url = urlsplit(value)
        if url.hostname:
            host = f"[{url.hostname}]" if ":" in url.hostname else url.hostname
            port = f":{url.port}" if url.port is not None else ""
            return f"{url.scheme}://{host}{port}"
    except ValueError:
        pass
    return "[redacted URL]"


def redact_urls(text: str) -> str:
    return SCP_URL.sub("[redacted Git URL]", URL.sub(lambda match: url_origin(match[0]), text))


def redact_package_json(text: str) -> str:
    def replace(match: re.Match[str]) -> str:
        token = match[0]
        try:
            decoded = json.loads(token)
        except ValueError:
            return redact_urls(token)
        sanitized = redact_urls(decoded)
        return json.dumps(sanitized, ensure_ascii=True) if sanitized != decoded else token

    return JSON_STRING.sub(replace, text)


def json_object(text: str) -> dict:
    try:
        value = json.loads(text)
    except (ValueError, RecursionError):
        raise ProjectError("INVALID_DATA", "A project JSON file is invalid") from None
    if not isinstance(value, dict):
        raise ProjectError("INVALID_DATA", "Expected a JSON object in project data")
    return value


def package_value(value) -> tuple[str | None, str | None, str]:
    if not isinstance(value, str):
        return None, None, "unknown"
    if value.startswith("file:"):
        return None, None, "local"
    if URL.match(value):
        return None, url_origin(value), "git" if ".git" in value or value.startswith("git") else "tarball"
    if SCP_URL.fullmatch(value):
        return None, None, "git"
    if re.fullmatch(r"[a-zA-Z0-9+.*_~^ -]{1,128}", value):
        return value, None, "registry"
    return None, None, "unknown"


def package_rows(manifest: dict, lock: dict) -> Iterator[dict]:
    declared = manifest.get("dependencies", {})
    locked = lock.get("dependencies", {})
    if not isinstance(declared, dict) or not isinstance(locked, dict):
        raise ProjectError("INVALID_DATA", "Invalid dependencies in Unity package data")
    for name in sorted(declared.keys() | locked.keys()):
        if not PACKAGE_NAME.fullmatch(name):
            continue
        record = locked.get(name, {})
        if not isinstance(record, dict):
            raise ProjectError("INVALID_DATA", "Invalid Unity package lock entry")
        wanted, wanted_url, wanted_source = package_value(declared.get(name))
        version, source_url, inferred_source = package_value(record.get("version", declared.get(name)))
        source = record.get("source")
        dependencies = record.get("dependencies", {})
        if not isinstance(dependencies, dict):
            raise ProjectError("INVALID_DATA", "Invalid dependency list in Unity package lock")
        row = {
            "name": name, "version": version, "source": source if isinstance(source, str) and source in PACKAGE_SOURCES
            else (inferred_source if inferred_source != "unknown" else wanted_source),
            "declared_version": wanted, "source_url": source_url or wanted_url,
            "direct": name in declared,
            "dependencies": {key: package_value(value)[0] for key, value in dependencies.items()
                             if PACKAGE_NAME.fullmatch(key)},
        }
        revision = record.get("hash")
        if isinstance(revision, str) and re.fullmatch(r"[a-fA-F0-9]{40,64}", revision):
            row["revision"] = revision
        yield row
