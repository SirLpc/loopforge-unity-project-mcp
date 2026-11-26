import os
import re
import shutil
import tomllib
from pathlib import Path
from urllib.parse import urlsplit

from pydantic import BaseModel, ConfigDict, Field, ValidationError, model_validator

from .errors import ProjectError


SERVICE_ROOT = Path(__file__).resolve().parents[2]
TOKEN_ENV = "UNITY_MCP_TOKEN"
LOOPBACK = {"127.0.0.1", "localhost", "::1"}
DEFAULT_FOLDERS = ("Assets", "Packages", "ProjectSettings", "Docs", "LocalTests")
DEFAULT_EXCLUSIONS = [
    ".git", ".idea", ".codex", ".codegraph", "Library", "Temp", "Logs", "obj",
    "Bundles", "HybridCLRData", "Build", "Builds", "UserSettings", "MemoryCaptures",
    "Recordings", ".vs", "node_modules",
]


class ConfigModel(BaseModel):
    model_config = ConfigDict(extra="forbid")


class ProjectConfig(ConfigModel):
    name: str = Field(min_length=1, max_length=120)
    repository_root: Path
    unity_project_path: str = "client"


class ServerConfig(ConfigModel):
    host: str = "127.0.0.1"
    port: int = Field(default=8766, ge=1, le=65535)
    allowed_hosts: list[str] = Field(default_factory=list, max_length=32)
    allowed_origins: list[str] = Field(default_factory=list, max_length=32)

    @model_validator(mode="after")
    def validate_network(self):
        if self.host.casefold() not in LOOPBACK and not self.allowed_hosts:
            raise ValueError("A non-loopback listener requires explicit allowed_hosts")
        for host in self.allowed_hosts:
            if not re.fullmatch(r"(?:[a-zA-Z0-9.-]+|\[[a-fA-F0-9:]+\])(?::[0-9]{1,5})?", host):
                raise ValueError("allowed_hosts must contain exact host headers")
        for origin in self.allowed_origins:
            parts = urlsplit(origin)
            if (parts.scheme not in {"http", "https"} or not parts.hostname or parts.username
                    or parts.path or parts.query or parts.fragment or "*" in origin or "\r" in origin or "\n" in origin):
                raise ValueError("allowed_origins must contain exact HTTP(S) origins")
        return self

    def host_headers(self) -> list[str]:
        hosts = list(self.allowed_hosts)
        if self.host.casefold() in LOOPBACK:
            for host in ("localhost", "127.0.0.1", "[::1]"):
                hosts.extend((host, f"{host}:{self.port}"))
        return hosts


class SearchConfig(ConfigModel):
    include_paths: list[str] | None = Field(default=None, max_length=128)
    exclude_directories: list[str] = Field(default_factory=lambda: DEFAULT_EXCLUSIONS.copy(), max_length=128)


class Limits(ConfigModel):
    default_search_limit: int = Field(default=50, ge=1, le=2000)
    max_search_limit: int = Field(default=200, ge=1, le=2000)
    search_timeout_seconds: float = Field(default=10, gt=0, le=120)
    default_read_lines: int = Field(default=200, ge=1, le=5000)
    max_read_lines: int = Field(default=500, ge=1, le=5000)
    max_response_bytes: int = Field(default=65536, ge=1024, le=1048576)
    max_text_file_bytes: int = Field(default=8 * 1024**2, ge=1024, le=32 * 1024**2)
    max_index_output_bytes: int = Field(default=128 * 1024**2, ge=1024, le=512 * 1024**2)
    git_timeout_seconds: float = Field(default=45, gt=0, le=300)
    query_timeout_seconds: float = Field(default=25, gt=0, le=300)
    max_concurrent_queries: int = Field(default=4, ge=1, le=32)

    @model_validator(mode="after")
    def validate_defaults(self):
        if self.default_search_limit > self.max_search_limit or self.default_read_lines > self.max_read_lines:
            raise ValueError("Default limits cannot exceed maximum limits")
        return self


class RuntimeConfig(ConfigModel):
    state_dir: Path = Path(".state")
    log_file: Path | None = None
    git: Path | None = None
    rg: Path | None = None


class RefreshConfig(ConfigModel):
    enabled: bool = False
    remote: str = Field(default="origin", pattern=r"^[A-Za-z0-9][A-Za-z0-9._/-]*$")
    branch: str = Field(default="main", min_length=1, max_length=240)
    cooldown_seconds: float = Field(default=60, gt=0, le=86400)
    timeout_seconds: float = Field(default=15, gt=0, le=300)


class Settings(ConfigModel):
    project: ProjectConfig
    server: ServerConfig = Field(default_factory=ServerConfig)
    search: SearchConfig = Field(default_factory=SearchConfig)
    limits: Limits = Field(default_factory=Limits)
    runtime: RuntimeConfig = Field(default_factory=RuntimeConfig)
    refresh: RefreshConfig = Field(default_factory=RefreshConfig)
    service_root: Path = SERVICE_ROOT

    def locations(self) -> tuple[Path, Path, Path | None]:
        configured_root = self.project.repository_root
        if not configured_root.is_absolute():
            raise ProjectError("INVALID_CONFIG", "repository_root must be an absolute path")
        root = configured_root.resolve()
        service = self.service_root.resolve()
        state = (service / self.runtime.state_dir).resolve()
        log = (service / self.runtime.log_file).resolve() if self.runtime.log_file else None
        for path in (service, SERVICE_ROOT, Path(__file__).resolve(), state, state / "activity.lock", log):
            if path is not None and path.resolve().is_relative_to(root):
                raise ProjectError("INVALID_CONFIG", "Service code, state and logs must be outside the reference repository")
        return root, state, log


ENV_OVERRIDES = {
    "REPOSITORY_ROOT": ("project", "repository_root"),
    "UNITY_PROJECT_PATH": ("project", "unity_project_path"),
    "HOST": ("server", "host"),
    "PORT": ("server", "port"),
    "STATE_DIR": ("runtime", "state_dir"),
    "LOG_FILE": ("runtime", "log_file"),
    "GIT": ("runtime", "git"),
    "RG": ("runtime", "rg"),
}


def load_settings(path: Path) -> Settings:
    try:
        with path.open("rb") as stream:
            data = tomllib.load(stream)
        for name, (section, key) in ENV_OVERRIDES.items():
            if value := os.environ.get(f"UNITY_MCP_{name}"):
                data.setdefault(section, {})[key] = value
        if "service_root" in data:
            data["service_root"] = path.resolve().parent / data["service_root"]
        return Settings.model_validate(data)
    except ValidationError as exc:
        fields = ", ".join(".".join(map(str, item["loc"])) for item in exc.errors() if item["loc"])
        message = f"Invalid TOML settings ({fields})" if fields else "Invalid TOML settings"
        raise ProjectError("INVALID_CONFIG", message) from None
    except (OSError, TypeError, tomllib.TOMLDecodeError):
        raise ProjectError("INVALID_CONFIG", "Cannot load the TOML settings") from None


def executable(name: str, configured: Path | None, root: Path) -> Path:
    if configured is not None:
        found = configured
    else:
        # Skip empty and relative PATH entries. On Windows, do not search cwd.
        filename = name + (".exe" if os.name == "nt" else "")
        found = next((Path(part) / filename for part in os.get_exec_path()
                      if Path(part).is_absolute() and shutil.which(str(Path(part) / filename))), None)
    if found is None or not found.is_absolute() or not found.is_file():
        raise ProjectError("DEPENDENCY_UNAVAILABLE", f"A trusted {name} executable is required")
    found = found.resolve()
    if found.is_relative_to(root) or (os.name == "nt" and found.suffix.casefold() != ".exe"):
        raise ProjectError("INVALID_CONFIG", f"The {name} executable must be installed outside the reference repository")
    return found
