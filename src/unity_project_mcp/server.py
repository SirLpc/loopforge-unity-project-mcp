import argparse
import hmac
import json
import logging
import os
import re
from functools import partial
from pathlib import Path
from threading import BoundedSemaphore
from time import monotonic
from typing import Annotated, Literal

import anyio
import uvicorn
from mcp.server import MCPServer
from mcp.server.transport_security import TransportSecuritySettings
from mcp.types import CallToolResult, TextContent, ToolAnnotations
from pydantic import Field
from starlette.responses import PlainTextResponse
from starlette.types import ASGIApp, Receive, Scope, Send

from .activity import activity_lock, activity_name
from .config import SERVICE_ROOT, TOKEN_ENV, ServerConfig, Settings, load_settings
from .errors import ProjectError
from .git import Git
from .models import json_bytes
from .project import Project
from .refresh import RefreshingProject
from .security import ensure_no_links, normalize_path


LOGGER = logging.getLogger(__name__)
AUTH_PATTERN = re.compile(rb"[A-Za-z0-9._~+/-]+=*")
READ_ONLY = ToolAnnotations(readOnlyHint=True, destructiveHint=False, idempotentHint=True, openWorldHint=False)
PathParam = Annotated[str, Field(min_length=1, max_length=1024)]
QueryParam = Annotated[str, Field(min_length=1, max_length=256)]
CommitParam = Annotated[str, Field(max_length=64)]
CursorParam = Annotated[str, Field(max_length=64)]
ExtensionsParam = Annotated[list[Annotated[str, Field(max_length=25)]], Field(max_length=32)]


def protect_transport(app: ASGIApp, token: str, network: ServerConfig) -> ASGIApp:
    secret = token.encode("ascii", "ignore")
    if secret.decode("ascii") != token or len(secret) > 1024 or not AUTH_PATTERN.fullmatch(secret):
        raise ProjectError("INVALID_CONFIG", f"Set a valid Bearer token in {TOKEN_ENV}")
    hosts = frozenset(network.host_headers())
    origins = frozenset(network.allowed_origins)

    async def wrapped(scope: Scope, receive: Receive, send: Send) -> None:
        if scope["type"] == "http":
            authorization = [value for key, value in scope["headers"] if key == b"authorization"]
            scheme, _, credential = authorization[0].partition(b" ") if len(authorization) == 1 else (b"", b"", b"")
            if scheme.lower() != b"bearer" or not hmac.compare_digest(credential, secret):
                response = PlainTextResponse("Unauthorized", 401, {"WWW-Authenticate": 'Bearer realm="unity-project-mcp"'})
                await response(scope, receive, send)
                return
            host = [value.decode("latin-1") for key, value in scope["headers"] if key == b"host"]
            origin = [value.decode("latin-1") for key, value in scope["headers"] if key == b"origin"]
            if len(host) != 1 or host[0] not in hosts:
                await PlainTextResponse("Invalid Host header", 421)(scope, receive, send)
                return
            if len(origin) > 1 or (origin and origin[0] not in origins):
                await PlainTextResponse("Invalid Origin header", 403)(scope, receive, send)
                return
        await app(scope, receive, send)

    return wrapped


def create_app(project: Project | RefreshingProject, token: str) -> ASGIApp:
    network = project.settings.server
    mcp = MCPServer(
        "unity-project-mcp", version="0.1.0", subscriptions=False,
        instructions="Read a pinned Unity project through Git repository-relative paths. Get the project commit first.",
    )
    slots = BoundedSemaphore(project.limits.max_concurrent_queries)

    async def invoke(tool: str, **arguments) -> CallToolResult:
        if slots.acquire(blocking=False):
            try:
                result = await anyio.to_thread.run_sync(partial(project.invoke, tool, **arguments))
            finally:
                slots.release()
        else:
            result = project.failed(tool, monotonic(), ProjectError("SERVER_BUSY", "All query slots are busy. Retry shortly"))
        return CallToolResult(
            content=[TextContent(type="text", text=json_bytes(result).decode("utf-8"))],
            structuredContent=result, isError=not result["success"],
        )

    @mcp.tool(annotations=READ_ONLY)
    async def get_project_info(expected_commit: CommitParam | None = None) -> CallToolResult:
        """Get Unity version, Git baseline and search scope. Begin each analysis without expected_commit to allow a refresh.

        Pass the returned commit to subsequent queries. If meta.refresh.status is failed, mention the refresh failure
        and the commit actually used.
        """
        return await invoke("get_project_info", expected_commit=expected_commit)

    @mcp.tool(annotations=READ_ONLY)
    async def list_directory(path: PathParam | None = None, depth: int = 1, limit: int | None = None,
                             cursor: CursorParam | None = None, expected_commit: CommitParam | None = None) -> CallToolResult:
        """List authorized tracked files and their directories. Defaults to the Unity directory. Depth is 1 to 3."""
        return await invoke("list_directory", path=path, depth=depth, limit=limit, cursor=cursor, expected_commit=expected_commit)

    @mcp.tool(annotations=READ_ONLY)
    async def search_files(query: QueryParam, extensions: ExtensionsParam | None = None, path: PathParam | None = None,
                           limit: int | None = None, cursor: CursorParam | None = None,
                           expected_commit: CommitParam | None = None) -> CallToolResult:
        """Find tracked filenames or paths by case-insensitive literal substring. Use next_cursor to paginate."""
        return await invoke("search_files", query=query, extensions=extensions, path=path, limit=limit,
                            cursor=cursor, expected_commit=expected_commit)

    @mcp.tool(annotations=READ_ONLY)
    async def search_code(query: QueryParam, path: PathParam | None = None, extensions: ExtensionsParam | None = None,
                          case_sensitive: bool = False, context_lines: int = 0, limit: int | None = None,
                          expected_commit: CommitParam | None = None) -> CallToolResult:
        """Literal code search with line numbers and up to 5 surrounding lines on each side. Searches C# by default."""
        return await invoke("search_code", query=query, path=path, extensions=extensions, case_sensitive=case_sensitive,
                            context_lines=context_lines, limit=limit, expected_commit=expected_commit)

    @mcp.tool(annotations=READ_ONLY)
    async def read_file(path: PathParam, start_line: int = 1, end_line: int | None = None,
                        expected_commit: CommitParam | None = None) -> CallToolResult:
        """Read a bounded inclusive line range from an authorized text file. Lines are 1-based."""
        return await invoke("read_file", path=path, start_line=start_line, end_line=end_line, expected_commit=expected_commit)

    @mcp.tool(annotations=READ_ONLY)
    async def find_references(symbol: QueryParam, path: PathParam | None = None, limit: int | None = None,
                              expected_commit: CommitParam | None = None) -> CallToolResult:
        """Find textual C# identifier occurrences. These are candidate references, including declarations."""
        return await invoke("find_references", symbol=symbol, path=path, limit=limit, expected_commit=expected_commit)

    @mcp.tool(annotations=READ_ONLY)
    async def get_packages(expected_commit: CommitParam | None = None) -> CallToolResult:
        """List Unity manifest and locked dependencies with redacted package origins."""
        return await invoke("get_packages", expected_commit=expected_commit)

    @mcp.tool(annotations=READ_ONLY)
    async def find_asset_by_guid(guid: Annotated[str, Field(max_length=32)],
                                 expected_commit: CommitParam | None = None) -> CallToolResult:
        """Find a tracked Unity asset and .meta path for a 32-character GUID in Assets or local Packages."""
        return await invoke("find_asset_by_guid", guid=guid, expected_commit=expected_commit)

    @mcp.tool(annotations=READ_ONLY)
    async def get_asset_references(path: PathParam, direction: Literal["forward", "reverse"] = "forward",
                                   limit: int | None = None, expected_commit: CommitParam | None = None) -> CallToolResult:
        """Inspect Unity YAML GUID/FileID dependencies, or find serialized candidates pointing to an asset's GUID."""
        return await invoke("get_asset_references", path=path, direction=direction, limit=limit, expected_commit=expected_commit)

    @mcp.tool(annotations=READ_ONLY)
    async def get_git_log(path: PathParam | None = None, limit: int = 10,
                          expected_commit: CommitParam | None = None) -> CallToolResult:
        """Return recent Git commits touching authorized paths. Default 10, maximum 50."""
        return await invoke("get_git_log", path=path, limit=limit, expected_commit=expected_commit)

    @mcp.tool(annotations=READ_ONLY)
    async def get_git_diff(from_commit: CommitParam, to_commit: CommitParam, path: PathParam | None = None,
                           max_lines: int | None = None, expected_commit: CommitParam | None = None) -> CallToolResult:
        """Compare two full local Git commit SHAs using a bounded patch for authorized paths."""
        return await invoke("get_git_diff", from_commit=from_commit, to_commit=to_commit, path=path, max_lines=max_lines,
                            expected_commit=expected_commit)

    @mcp.tool(annotations=READ_ONLY)
    async def get_assembly_info(path: PathParam | None = None, limit: int | None = None,
                                expected_commit: CommitParam | None = None) -> CallToolResult:
        """Inspect .asmdef and .asmref assembly dependencies, platform filters, and unresolved references."""
        return await invoke("get_assembly_info", path=path, limit=limit, expected_commit=expected_commit)

    app = mcp.streamable_http_app(
        stateless_http=True, json_response=True, max_request_body_size=16384,
        transport_security=TransportSecuritySettings(
            enable_dns_rebinding_protection=True,
            allowed_hosts=network.host_headers(), allowed_origins=network.allowed_origins,
        ),
    )
    return protect_transport(app, token, network)


def configure_logging(settings: Settings) -> None:
    _, _, filename = settings.locations()
    if filename:
        filename.parent.mkdir(parents=True, exist_ok=True)
    logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(message)s", filename=filename,
                        encoding="utf-8" if filename else None)
    logging.getLogger("mcp").setLevel(logging.CRITICAL)


def main() -> int:
    parser = argparse.ArgumentParser(description="Serve a read-only Unity Git reference through MCP")
    parser.add_argument("--config", type=Path, default=Path(os.environ.get("UNITY_MCP_CONFIG", SERVICE_ROOT / "config.toml")))
    parser.add_argument("--sync-info", action="store_true", help=argparse.SUPPRESS)
    arguments = parser.parse_args()
    try:
        settings = load_settings(arguments.config)
        root, state, _ = settings.locations()
        if arguments.sync_info:
            unity_path = normalize_path(settings.project.unity_project_path)
            if unity_path == ".":
                raise ProjectError("INVALID_CONFIG", "unity_project_path must be a subdirectory of repository_root")
            ensure_no_links(root, unity_path)
            print(json.dumps({
                "repository_root": str(root), "unity_project_path": unity_path,
                "activity_name": activity_name(root), "git": str(Git(root, settings).executable),
            }, ensure_ascii=True))
            return 0
        token = os.environ.get(TOKEN_ENV, "")
        if not token:
            raise ProjectError("INVALID_CONFIG", f"Set {TOKEN_ENV} before starting the service")
        configure_logging(settings)
        with activity_lock(root, state):
            project = RefreshingProject(settings) if settings.refresh.enabled else Project(settings)
            app = create_app(project, token)
            baseline = project.current if isinstance(project, RefreshingProject) else project
            LOGGER.info("baseline commit=%s branch=%s commit_time=%s tracked_dirty=%s indexed_files=%s", baseline.commit,
                        baseline.baseline.branch or "(detached)", baseline.commit_time, baseline.baseline.dirty, len(baseline.files))
            uvicorn.run(app, host=settings.server.host, port=settings.server.port, access_log=False,
                        proxy_headers=False, server_header=False, ws="none")
        return 0
    except ProjectError as exc:
        LOGGER.error("startup error=%s %s", exc.code, exc)
    except (OSError, ValueError):
        LOGGER.error("startup error=INVALID_CONFIG Could not open the configured service or reference location")
    return 1


if __name__ == "__main__":
    raise SystemExit(main())
