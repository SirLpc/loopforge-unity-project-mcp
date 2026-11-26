import asyncio
import json
import os
import socket
import subprocess
import sys
import time
from pathlib import Path

import httpx2
import pytest
from mcp import ClientSession
from mcp.client.streamable_http import streamable_http_client


TOKEN = "test-token-only-available-to-mcp"
P0 = {"get_project_info", "list_directory", "search_files", "search_code", "read_file", "find_references", "get_packages"}
P1 = {"find_asset_by_guid", "get_asset_references", "get_git_log", "get_git_diff", "get_assembly_info"}


@pytest.fixture
def live_service(reference, tmp_path, request):
    workspace = Path(__file__).resolve().parents[1]
    with socket.socket() as sock:
        sock.bind(("127.0.0.1", 0))
        port = sock.getsockname()[1]
    token = {"Authorization": f"Bearer {TOKEN}"}
    url = f"http://127.0.0.1:{port}/mcp"
    config = tmp_path / "service.toml"
    config.write_text(
        f"[project]\nname='fixture'\nrepository_root='{reference.as_posix()}'\nunity_project_path='client'\n"
        f"[server]\nport={port}\nallowed_origins=['https://trusted.example']\n"
        f"[runtime]\nstate_dir='{(tmp_path / 'state').as_posix()}'\n",
        encoding="utf-8",
    )
    if getattr(request, "param", False):
        request.getfixturevalue("refresh_settings")
        with config.open("a", encoding="utf-8") as stream:
            stream.write("[refresh]\nenabled=true\n")
    output = tmp_path / "service.log"
    with output.open("wb") as logs:
        process = subprocess.Popen(
            [sys.executable, "-m", "unity_project_mcp.server", "--config", str(config)],
            cwd=workspace, env={**os.environ, "UNITY_MCP_TOKEN": TOKEN, "PYTHONPATH": str(workspace / "src")},
            stdout=logs, stderr=subprocess.STDOUT,
        )
        try:
            with httpx2.Client(trust_env=False) as client:
                for _ in range(100):
                    if process.poll() is not None:
                        pytest.fail(output.read_text(encoding="utf-8"))
                    try:
                        if client.get(url, timeout=0.25).status_code == 401:
                            break
                    except httpx2.TransportError:
                        time.sleep(0.05)
                else:
                    pytest.fail(f"MCP server did not start\n{output.read_text(encoding='utf-8')}")
            yield url, token, output, config
        finally:
            process.terminate()
            process.wait(timeout=5)


def test_auth_and_dns_rebinding(live_service):
    url, token, _, _ = live_service
    accept = {"Accept": "application/json, text/event-stream"}
    headers = {**token, **accept}
    payload = {"jsonrpc": "2.0", "id": 1, "method": "initialize", "params": {
        "protocolVersion": "2025-06-18", "capabilities": {}, "clientInfo": {"name": "fixture", "version": "0.1"},
    }}
    with httpx2.Client(trust_env=False) as client:
        assert client.get(url).status_code == 401
        assert client.delete(url).status_code == 401
        assert client.post(url, json=payload, headers={"Authorization": "Bearer wrong"}).status_code == 401
        assert client.post(url, json=payload, headers={**headers, "Host": "untrusted.example"}).status_code == 421
        assert client.post(url + "/", json=payload, headers={**headers, "Host": "untrusted.example"}).status_code == 421
        assert client.post(url, json=payload, headers={**headers, "Origin": "https://untrusted.example"}).status_code == 403
        duplicates = [*headers.items(), ("Origin", "https://trusted.example"), ("Origin", "https://untrusted.example")]
        assert client.post(url, json=payload, headers=duplicates).status_code == 403
        response = client.post(url, json=payload, headers=headers)
        assert response.status_code == 200 and response.json()["result"]["protocolVersion"] == "2025-06-18"


def test_loopforge_v042_wire_shape(live_service):
    url, token, _, _ = live_service
    headers = {**token, "Accept": "application/json, text/event-stream"}
    with httpx2.Client(trust_env=False, headers=headers, timeout=15) as client:
        def post(method, params=None, *, id=None):
            payload = {"jsonrpc": "2.0", "method": method}
            if id is not None:
                payload["id"] = id
            if params is not None:
                payload["params"] = params
            return client.post(url, json=payload)

        init = post("initialize", {
            "protocolVersion": "2024-11-05", "capabilities": {}, "clientInfo": {"name": "agentflow", "version": "1.0.0"},
        }, id=1)
        assert init.status_code == 200 and "result" in init.json()
        assert post("notifications/initialized").status_code == 202
        tools = post("tools/list", id=3).json()["result"]["tools"]
        assert P0 <= {tool["name"] for tool in tools}
        result = post("tools/call", {"name": "get_project_info", "arguments": {}}, id=4).json()["result"]
        assert not result.get("isError")
        assert json.loads(result["content"][0]["text"])["data"][0]["unity_project_path"] == "client"


def test_real_mcp_client_discovers_and_calls_tools(live_service):
    url, token, log, _ = live_service

    async def run():
        async with httpx2.AsyncClient(headers=token, trust_env=False, timeout=15) as http:
            async with streamable_http_client(url, http_client=http) as (read, write):
                async with ClientSession(read, write, read_timeout_seconds=15) as session:
                    await session.initialize()
                    tools = (await session.list_tools()).tools
                    assert {tool.name for tool in tools} == P0 | P1
                    assert all(tool.annotations and tool.annotations.read_only_hint for tool in tools)
                    project_info = await session.call_tool("get_project_info")
                    assert not project_info.is_error
                    commit = project_info.structured_content["commit"]
                    assert len(commit) == 40
                    assert json.loads(project_info.content[0].text) == project_info.structured_content
                    hits = await session.call_tool("search_files", {"query": "Inventory", "expected_commit": commit})
                    assert not hits.is_error and hits.structured_content["data"][0]["path"].startswith("client/")
                    result = await session.call_tool("read_file", {
                        "path": "client/Assets/Scripts/InventoryManager.cs", "start_line": 3,
                        "end_line": 3, "expected_commit": commit,
                    })
                    assert not result.is_error and result.structured_content["data"][0]["text"] == "  void Sell() {}"
                    history = await session.call_tool("get_git_log", {"expected_commit": commit, "limit": 1})
                    assert not history.is_error and history.structured_content["data"][0]["commit"] == commit
                    mismatch = await session.call_tool("read_file", {
                        "path": "client/Assets/Scripts/InventoryManager.cs", "expected_commit": "0" * 40,
                    })
                    assert mismatch.is_error and mismatch.structured_content["error"]["code"] == "COMMIT_MISMATCH"

    asyncio.run(run())
    smoke = subprocess.run(
        [sys.executable, str(Path(__file__).resolve().parents[1] / "scripts/smoke-test.py"), "--url", url],
        env={**os.environ, "UNITY_MCP_TOKEN": TOKEN}, capture_output=True, text=True, timeout=30, check=True,
    )
    assert set(json.loads(smoke.stdout)["tools"]) == P0 | P1
    logged = log.read_text(encoding="utf-8")
    assert "tool=read_file" in logged and "error=COMMIT_MISMATCH" in logged
    assert TOKEN not in logged and "InventoryManager" not in logged and "void Sell" not in logged


@pytest.mark.parametrize("live_service", [True], indirect=True)
def test_unpinned_project_info_refreshes_over_mcp(live_service, refresh_settings, reference):
    from conftest import git
    from test_refresh import advance

    url, token, _, _ = live_service
    _, writer, _ = refresh_settings
    old = git(reference, "rev-parse", "HEAD")
    updated = advance(writer)
    headers = {**token, "Accept": "application/json, text/event-stream"}
    with httpx2.Client(trust_env=False, headers=headers, timeout=25) as client:
        def call(method, params=None):
            response = client.post(url, json={"jsonrpc": "2.0", "id": 1, "method": method, "params": params or {}})
            response.raise_for_status()
            return response.json()["result"]

        assert "get_project_info" in {tool["name"] for tool in call("tools/list")["tools"]}
        before = call("tools/call", {"name": "get_project_info", "arguments": {"expected_commit": old}})
        assert not before.get("isError") and before["structuredContent"]["commit"] == old
        after = call("tools/call", {"name": "get_project_info", "arguments": {}})
        assert not after.get("isError")
        result = json.loads(after["content"][0]["text"])
        assert result["commit"] == updated
        assert result["meta"]["refresh"]["status"] == "updated"
