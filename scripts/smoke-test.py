import argparse
import asyncio
import json
import os

import httpx2
from mcp import ClientSession
from mcp.client.streamable_http import streamable_http_client


async def smoke(url: str, token: str) -> None:
    async with httpx2.AsyncClient(headers={"Authorization": f"Bearer {token}"}, timeout=30, trust_env=False) as http:
        async with streamable_http_client(url, http_client=http) as (read, write):
            async with ClientSession(read, write, read_timeout_seconds=30) as session:
                version = (await session.initialize()).protocol_version
                tools = sorted(tool.name for tool in (await session.list_tools()).tools)
                result = await session.call_tool("get_project_info")
                if result.is_error:
                    raise RuntimeError(result.structured_content["error"]["code"])
                project = result.structured_content["data"][0]
                print(json.dumps({
                    "protocol": version, "tools": tools, "commit": result.structured_content["commit"],
                    "unity_version": project["unity_version"], "unity_project_path": project["unity_project_path"],
                }, ensure_ascii=False, indent=2))


if __name__ == "__main__":
    parser = argparse.ArgumentParser(description="Initialize and query the Unity project MCP service with the official SDK")
    parser.add_argument("--url", default="http://127.0.0.1:8766/mcp")
    args = parser.parse_args()
    token = os.environ.get("UNITY_MCP_TOKEN")
    if not token:
        parser.error("Set UNITY_MCP_TOKEN in the client environment")
    asyncio.run(smoke(args.url, token))
