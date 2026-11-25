# 故障排查

| 现象或错误 | 处理方式 |
| --- | --- |
| `401 Unauthorized` | 检查服务端和客户端 `UNITY_MCP_TOKEN` 是否来自同一个密钥，确认网关为每个请求携带 Bearer Header |
| `421 Invalid Host` 或 `403 Invalid Origin` | 将实际 Host 和带协议的 Origin 精确配置到对应允许列表，反向代理使用的上游 Host 也要匹配 |
| Windows 连接成功，WSL 或容器超时 | 从实际调用进程所在网络探测目标，按 NAT、镜像网络或 Docker Desktop 选择地址，并让管理员检查绑定、路由和防火墙 |
| `INVALID_CONFIG` | 核对 Git 根目录、Unity 子目录、配置格式、工具绝对路径及日志和状态目录的位置。参考仓库需要仓库内的本地 `.git` 元数据和完整的已检出索引 |
| `DEPENDENCY_UNAVAILABLE` | 检查 Python 服务环境、Git for Windows 与 ripgrep 路径，核对所需本地 Git 对象是否存在 |
| `PROJECT_CHANGED` | 停止服务，检查 HEAD、分支和所有已跟踪目录的改动，确认参考仓库清洁后重启 |
| `COMMIT_MISMATCH` | 获取最新 `get_project_info`，用新 SHA 重查技术方案涉及的证据 |
| `PATH_NOT_ALLOWED` / `FILE_NOT_FOUND` | 检查仓库相对路径、授权范围、敏感名称、Git 跟踪状态和 NTFS 重解析点 |
| `FILE_TOO_LARGE` / `OUTPUT_LIMIT` | 缩小检索范围，并调整请求行数、上下文或结果数量。管理员可评估是否调整 TOML 限制 |
| `BINARY_FILE` / `GIT_LFS_POINTER` | 用文本 Meta、代码或其他已授权文件继续分析。服务读取提交中记录的 LFS Pointer 并显式返回其状态 |
| `SEARCH_TIMEOUT` / `SERVER_BUSY` | 为查询提供更具体的目录、扩展名和关键词。必要时核对 Git、存储和限额配置 |
| `INDEX_BUILDING` | 第一个资源查询正在准备外部 GUID 索引，稍后重试 |
| `SERVICE_BUSY` | 停止使用同一参考仓库的 MCP 服务或等待同步脚本退出 |

服务日志默认输出到 stderr。工具日志记录名称、耗时、条数和错误码。对外分享日志前按部署方的数据规则审查记录。

代码检索和分段读取支持 UTF-8 及带 BOM 的 UTF-16/UTF-32。资源关系目前依据 Unity YAML 中可识别的序列化引用，外部 FileID、注册表 Package 与超限资源可能需要进一步核对。Git 状态检查禁用外部过滤器。若本地 LFS 内容或文件时间变动触发 `PROJECT_CHANGED`，请在停服后由管理员核对参考工作区。`max_response_bytes` 限制工具业务 JSON，MCP 协议封装同时携带结构化数据和兼容旧客户端的文本副本。
