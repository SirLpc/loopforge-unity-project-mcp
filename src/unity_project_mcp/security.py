import re
import stat
from pathlib import Path

from .config import DEFAULT_FOLDERS
from .errors import ProjectError


BLOCKED_COMPONENTS = {
    ".git", ".codex", ".codegraph", "codex-skills", ".ssh", ".aws", ".gnupg", ".kube",
    ".npmrc", ".netrc", "_netrc", ".pypirc", ".git-credentials", ".htpasswd",
    "id_rsa", "id_ed25519", "id_dsa", "id_ecdsa", "local.settings.json", "service-account.json",
}
PRIVATE_SUFFIXES = {"pem", "key", "pfx", "p12", "ppk", "jks", "keystore", "kdbx"}
DEVICE = re.compile(r"(?:con|prn|aux|nul|conin\$|conout\$|clock\$|(?:com|lpt)[0-9¹²³])\Z", re.I)
INVALID_CHAR = re.compile(r'[\x00-\x1f\x7f<>:"|?*]')
SECRET_NAME = re.compile(r"(?:credentials?|secrets?|private[-_]key|service[-_]account)(?:[._-]|$)")


def normalize_path(path: str) -> str:
    path = path.replace("\\", "/")
    if not path or len(path) > 1024 or path.startswith("/") or INVALID_CHAR.search(path):
        raise ProjectError("INVALID_PATH", "Use a Git repository-relative path")
    parts = [part for part in path.split("/") if part not in {"", "."}]
    if ".." in parts:
        raise ProjectError("PATH_OUTSIDE_PROJECT", "Parent path traversal is forbidden")
    if any(part.endswith((" ", ".")) or DEVICE.fullmatch(part.split(".")[0].rstrip(" ")) for part in parts):
        raise ProjectError("INVALID_PATH", "Windows special path components are forbidden")
    return "/".join(parts) or "."


def within(path: str, root: str) -> bool:
    return root == "." or path == root or path.startswith(root + "/")


def is_reparse(info) -> bool:
    return stat.S_ISLNK(info.st_mode) or bool(
        getattr(info, "st_file_attributes", 0) & stat.FILE_ATTRIBUTE_REPARSE_POINT
    )


def ensure_no_links(root: Path, rel: str) -> None:
    """Check each existing component before descending into it."""
    current = root
    if is_reparse(root.lstat()):
        raise ProjectError("PATH_NOT_ALLOWED", "Symbolic links and reparse points are forbidden")
    for part in rel.split("/") if rel != "." else ():
        current /= part
        try:
            info = current.lstat()
        except (FileNotFoundError, NotADirectoryError):
            return
        if is_reparse(info):
            raise ProjectError("PATH_NOT_ALLOWED", "Symbolic links and reparse points are forbidden")


class PathPolicy:
    def __init__(self, root: Path, unity_path: str, include: list[str] | None, exclude: list[str]):
        self.root = root
        self.excluded = {entry.casefold() for entry in exclude}
        self.includes = tuple(dict.fromkeys(
            normalize_path(path) for path in (include if include is not None else
                                              [f"{unity_path}/{name}" for name in DEFAULT_FOLDERS])
        ))
        self.include_keys = tuple(path.casefold() for path in self.includes)
        if not self.includes or any(self.blocked(path) or path == "." for path in self.include_keys):
            raise ProjectError("INVALID_CONFIG", "Configure explicit, non-excluded include_paths inside the repository")
        if len(set(self.include_keys)) != len(self.includes):
            raise ProjectError("INVALID_CONFIG", "include_paths must have unique Windows path names")

    def blocked(self, key: str) -> bool:
        return any(
            part in self.excluded or part in BLOCKED_COMPONENTS or part.startswith(".env")
            or SECRET_NAME.match(part) or part.rpartition(".")[2] in PRIVATE_SUFFIXES
            for part in key.split("/")
        )

    def allows(self, rel: str, *, ancestors: bool = False) -> bool:
        key = rel.casefold()
        return not self.blocked(key) and any(
            within(key, allowed) or (ancestors and within(allowed, key)) for allowed in self.include_keys
        )

    def require(self, path: str, *, ancestors: bool = False) -> str:
        rel = normalize_path(path)
        if not self.allows(rel, ancestors=ancestors):
            raise ProjectError("PATH_NOT_ALLOWED", "Path is outside the configured search scope")
        ensure_no_links(self.root, rel)
        return rel
