# Unity Project MCP

在 Windows 上运行的只读 MCP 服务。LoopForge Main Bot 可查询 Unity 代码、文档、资源、Package 和 Git 历史，并用完整 Commit SHA 记录方案依据。Unity 工程可以位于 Git 仓库的子目录。示例部署使用 `D:\UnityReference\pet_ref\client`。

## 安装和启动

需要 Windows、Python 3.11 或以上、Git for Windows、ripgrep 和 `uv`。依赖从 `uv.lock` 安装，官方 MCP Python SDK 固定为 `2.3.0`。

```powershell
.\scripts\install.ps1
Copy-Item config.example.toml config.toml
git -C 'D:\UnityReference\pet_ref\client' rev-parse --show-toplevel
```

确认 `config.toml` 中 `repository_root` 对应 Git 输出，`unity_project_path` 为 `client`。为专用低权限服务账号授予参考仓库的 NTFS 读取权限及状态目录的写权限。同步使用持有参考仓库写权限的管理员账号。Token 从服务账号的进程环境或部署密钥注入。

```powershell
$env:UNITY_MCP_TOKEN = .\.venv\Scripts\python.exe -c "import secrets; print(secrets.token_urlsafe(32))"
.\scripts\start.ps1
```

默认入口是 `http://127.0.0.1:8766/mcp`。在另一个终端从密钥来源注入同一个 `UNITY_MCP_TOKEN`，再运行官方 SDK 的探测脚本。

```powershell
.\.venv\Scripts\python.exe .\scripts\smoke-test.py --url http://127.0.0.1:8766/mcp
```

### 重启 LoopForge 本机部署

修改本仓库的服务代码后，在部署所用的 Windows 账号下运行：

```powershell
.\scripts\restart.ps1
```

脚本复用 `%LOCALAPPDATA%\LoopForge\UnityProjectMCP\start.ps1` 及其配置和凭据。它会核对端口归属，停止旧实例并启动新实例，等新进程开始监听后输出 MCP 地址。启动失败时，可查看部署目录下的 `restart.stderr.log` 和 `state\service.log`。依赖有变更时，请先运行 `scripts\install.ps1`。

## 配置与检索范围

默认开放 `client/Assets`、`client/Packages`、`client/ProjectSettings`、`client/Docs`、`client/LocalTests` 中的已跟踪路径。其他模块可显式加入 `search.include_paths`。要开放 Bundles 或 HybridCLRData，还需从 `search.exclude_directories` 移除对应名称。Git 元数据、AI 指令目录和常见凭据文件名受固定阻止规则约束。

`runtime.state_dir` 默认是服务目录下的 `.state`，可配置 `runtime.log_file` 持久化标准日志。服务、日志和状态均保存在参考仓库之外。`UNITY_MCP_CONFIG` 可指定配置文件。环境变量 `UNITY_MCP_REPOSITORY_ROOT`、`UNITY_MCP_UNITY_PROJECT_PATH`、`UNITY_MCP_HOST`、`UNITY_MCP_PORT`、`UNITY_MCP_STATE_DIR`、`UNITY_MCP_LOG_FILE`、`UNITY_MCP_GIT`、`UNITY_MCP_RG` 覆盖对应 TOML 字段。

## 工具

| 工具 | 用途 |
| --- | --- |
| `get_project_info` | 取得 Unity 版本、当前分支、完整提交、提交时间和授权范围 |
| `list_directory` | 查询授权目录树，深度 1 至 3，支持 `cursor` |
| `search_files` | 按字面路径或文件名检索，支持扩展名和 `cursor` |
| `search_code` | 查找字面量及上下文，默认搜索 C#，可显式指定文本扩展名 |
| `read_file` | 按 1-based 闭区间读取文本，使用 `next_line` 继续 |
| `find_references` | 查找 C# 标识符的文本候选，包括声明和注释 |
| `get_packages` | 读取 manifest 与锁文件，返回脱敏依赖信息 |
| `find_asset_by_guid` | 用 GUID 查找受授权的 `.meta` 和资源路径 |
| `get_asset_references` | 查询 Unity YAML 中的正向引用和反向候选 |
| `get_git_log` | 按权限和路径查询基线历史，默认 10 条，最多 50 条 |
| `get_git_diff` | 使用两个完整提交 SHA 查询授权路径的有界差异 |
| `get_assembly_info` | 读取 `.asmdef` 和 `.asmref`，解析名称与 GUID 引用 |

调用先取 `get_project_info`，再将返回的 `commit` 作为 `expected_commit` 传给后续工具。路径使用仓库相对形式，如 `client/Assets/Scripts/InventoryManager.cs`。响应包含 `success`、`commit`、`data` 和 `meta`。`meta` 提供结果数、是否截断、下一页和耗时。失败返回 `error.code`，并设置 MCP 的 `isError`。

## 更新参考仓库

要在 Chat 开始查源码时自动检查远端，在服务使用的配置中加入：

```toml
[refresh]
enabled = true
remote = "origin"
branch = "main"
cooldown_seconds = 60
timeout_seconds = 15
```

初次启用后运行 `./scripts/restart.ps1 -StartupTimeoutSeconds 330`。服务用当前分支上干净的参考检出初始化状态目录中的独立裸仓库，需要额外空间存放 Git 对象。首次复制最长等待 300 秒，后续重启可复用缓存。首次查询和冷却结束后的下一次无 `expected_commit` 的 `get_project_info` 会检查配置的远端分支。同一服务的并发请求共用一次检查。冷却时间从尝试结束算起，远端不可用或新提交无法快进时继续使用上次可查询的 SHA。

`meta.refresh.status` 返回 `updated`、`unchanged` 或 `failed`，`checked_at` 是该次刷新结束的 UTC 时间。查询继续使用顶层 `commit`。收到 `COMMIT_MISMATCH` 时，重新读项目概况并按新的 SHA 查询。远端读取使用 Windows 部署账号的 Git 凭据，应可无交互访问。自动刷新默认关闭。

### 手动快进本地检出

停止服务后，由持有参考仓库写权限的管理员使用同一份配置同步。同步脚本先验证 Git 根目录、分支和工作区，再尝试快进，输出同步前后的完整 SHA。

```powershell
.\scripts\sync-reference.ps1 -Config .\config.toml -ExpectedBranch main -Remote origin
```

手动同步更新参考目录中的本地检出。重新启动服务并获取 `get_project_info` 确认查询基线。启用自动刷新时，服务优先恢复状态目录中的查询基线，并按需同步远端。

## 测试与部署

```powershell
.\.venv\Scripts\python.exe -m pytest -q
```

测试使用独立的小型 Git 仓库。Windows 上的 Junction 用例可以在 NTFS 上运行，符号链接用例需要账户权限或开发人员模式。部署说明见 [LoopForge 接入](docs/loopforge-integration.md)、[架构](docs/architecture.md)、[故障排查](docs/troubleshooting.md)和 [Main Bot 提示词](docs/main-bot-system-prompt.md)。
