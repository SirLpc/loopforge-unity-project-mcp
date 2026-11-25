# LoopForge 接入 Unity Project MCP

## 已核实的版本

以下配置基于 LoopForge **v0.4.2** 的发布版源码。其外部 HTTP MCP 客户端会执行 `initialize`、`tools/list` 和 `tools/call`，为请求传递配置的 `headers`，并从工具的 `content` 文本块读取结果。本服务兼容该版本发出的 `2024-11-05` 初始化请求和后续 Streamable HTTP POST，也提供结构化结果给支持它的客户端。LoopForge 自己的 `/mcp` 则为其他客户端提供 LoopForge 平台工具。 [发布版本](https://github.com/Tencent/LoopForge/releases/tag/v0.4.2) · [外部 HTTP MCP 客户端](https://github.com/Tencent/LoopForge/blob/v0.4.2/internal/mcpclient/client.go)

LoopForge 只需登记外部 MCP 定义。背包方案等查询可以在 Main Bot 的平台模式完成，项目可保持在 LoopForge 中没有 Unity Git 仓库绑定。项目工具调用从运行 LoopForge 网关的环境发出。使用其他执行器或工作区时，也应从该执行环境验证可达性。 [对话工具网关](https://github.com/Tencent/LoopForge/blob/v0.4.2/internal/httpapi/assistant_mcp.go) · [对话工作区](https://github.com/Tencent/LoopForge/blob/v0.4.2/docs/conversation-workspaces.md)

## Windows、WSL2 和 Docker 连通性

1. 按 [README](../README.md) 安装并在 Windows 本机运行 `smoke-test.py`。初次上线可保持本机回环监听，先确认鉴权和查询成功。
2. WSL2 **镜像网络**中的 Ubuntu 进程可尝试 `http://127.0.0.1:8766/mcp`。WSL 客户端的 `::1` 与 Windows 互通规则不同，回环示例使用 IPv4。 [Microsoft WSL 网络说明](https://learn.microsoft.com/windows/wsl/networking)
3. WSL2 **NAT** 中的 Ubuntu 进程可运行 `ip route show default`，将默认网关地址用作 Windows 宿主机地址。使用 `http://<Windows-Host-IP>:8766/mcp`，并在 Windows 配置中设置该私有接口的 `host` 以及精确的 `allowed_hosts = ["<Windows-Host-IP>:8766"]`。由管理员将防火墙入站范围限定到 WSL 子网。IP 变化后更新两端配置。 [Microsoft WSL 网络说明](https://learn.microsoft.com/windows/wsl/networking)
4. Docker Desktop 容器可尝试 `http://host.docker.internal:8766/mcp`，Windows 的 `allowed_hosts` 同时加入 `host.docker.internal:8766`。WSL 内独立运行的 Docker Engine 可使用容器可达的 Windows NAT IP。该 Engine 的 `host-gateway` 通常指向 Docker 宿主环境，需实测抵达 Windows 的路径。 [Docker Desktop 网络说明](https://docs.docker.com/desktop/features/networking/networking-how-tos/)

从 LoopForge API 或 Worker 容器内部执行下面的探测。`UNITY_MCP_TOKEN` 由该环境的密钥来源提供，`MCP_URL` 使用上述可达地址。正常结果包含 JSON-RPC 的 `result` 和协商的协议版本。

```bash
printf 'Authorization: Bearer %s\n' "$UNITY_MCP_TOKEN" |
  curl --fail-with-body --silent --show-error --max-time 10 \
    -H @- -H 'Content-Type: application/json' \
    -H 'Accept: application/json, text/event-stream' \
    --data '{"jsonrpc":"2.0","id":1,"method":"initialize","params":{"protocolVersion":"2025-06-18","capabilities":{},"clientInfo":{"name":"loopforge-network-probe","version":"1"}}}' \
    "$MCP_URL"
```

如果容器未安装 curl，可从同一网络的诊断容器或已有 Python 客户端运行相同探测。允许来源由防火墙或受控反向代理限制。Host 要匹配访问 URL 的名称和端口，只有发送 `Origin` 的客户端需要设置 `[server].allowed_origins`。跨不可信网络使用 HTTPS 或安全隧道，服务限私有网络访问。

## 注册到项目并在 Chat 使用

v0.4.2 的资产 API 支持 `PUT /api/mcp-servers` 保存项目级定义。使用有写权限的项目账号登记。将下列 JSON 中的 `projectId` 和 `url` 改成当前部署的值，提交为请求体。`UNITY_MCP_TOKEN` 需注入实际发起 MCP 请求的 **LoopForge 进程环境**，值与 Windows 服务一致。`${secret:...}` 由该版本运行时解析，发送给 MCP 的 Header 形式为 `Authorization: Bearer <token>`。 [注册表 API](https://github.com/Tencent/LoopForge/blob/v0.4.2/docs/api/registry.md#mcp-server) · [项目作用域校验](https://github.com/Tencent/LoopForge/blob/v0.4.2/internal/domain/assets.go#L367) · [Header 解析](https://github.com/Tencent/LoopForge/blob/v0.4.2/internal/runtime/materialize.go#L464)

```json
{
  "id": "unity-reference",
  "name": "Unity Project MCP",
  "description": "通过只读工具查询指定 Git 基线的 Unity 工程",
  "scope": "project",
  "projectId": "replace-with-loopforge-project-id",
  "transport": "http",
  "url": "http://<Windows-Host-IP>:8766/mcp",
  "headers": {"Authorization": "Bearer ${secret:UNITY_MCP_TOKEN}"},
  "timeoutSec": 30
}
```

1. 在项目 Chat 中选择已注册的外部 MCP。如果使用请求 API，v0.4.2 用 `mcpServerIds: ["unity-reference"]` 将 MCP 加入本轮工具目录。引用绑定此 MCP 的项目角色也会将其加入选中集合。界面入口以部署版本为准。 [选中集合与角色绑定](https://github.com/Tencent/LoopForge/blob/v0.4.2/internal/httpapi/assistant.go#L345) · [工具目录](https://github.com/Tencent/LoopForge/blob/v0.4.2/internal/httpapi/agent_tool_catalog.go#L281)
2. 将 [Main Bot 系统提示词](main-bot-system-prompt.md) 配入承接方案分析的角色或会话指令。请 Main Bot 先调用 `get_project_info`，再用 `search_files`、`search_code` 和 `read_file` 查询 `client` 下的 Inventory、Item、Sell 等代码，之后按需查资源和 Package。
3. 在回复中确认至少包含完整提交 SHA、真实路径和行号、已有实现、候选引用及待确认的出售规则。查看工具跟踪记录核对调用使用了 `expected_commit`。

## URL 限制与审批

LoopForge v0.4.2 的登记校验要求 `transport=http` 的 URL 使用 HTTP 或 HTTPS。Chat 网关按项目作用域加载选中的工具，外部写操作由该版本的调用分类和会话权限模式触发审批。使用受控的管理员流程登记 URL，限制 LoopForge 平台或容器的出站目的地，并保留默认确认模式。MCP 的读注解用于工具元数据。Windows 服务的只读权限由固定命令、路径、版本与 NTFS 权限共同约束。 [外部 MCP 校验](https://github.com/Tencent/LoopForge/blob/v0.4.2/internal/domain/assets.go#L322) · [调用审批](https://github.com/Tencent/LoopForge/blob/v0.4.2/internal/httpapi/agent_tool_mcp.go#L476)

如果部署版本限制私网 URL，可使用只转发固定 `/mcp` 上游的内部 HTTPS 反向代理，限制使用方并保留 Bearer 校验。代理的上游 Host 需列入 Windows `allowed_hosts`。如果旧版客户端缺少 Streamable HTTP 支持，可使用独立的 MCP 协议桥或升级客户端，优先验证 `initialize`、`tools/list` 和 `tools/call` 的完整链路。
