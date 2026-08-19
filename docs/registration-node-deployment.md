# 独立注册节点部署合同

`auto-plus3.cccy.me` 是第四个独立业务实例。它复用主仓库构建的
`auto-gpt:latest` 镜像，但不共享 Plus 的 SQLite、任务内存或运行目录。

## 数据边界

- 唯一开发路径仍为 `/opt/auto-gpt`；注册服务器不维护第二份源码。
- 注册节点运行目录固定为 `/opt/auto-gpt-register`。
- `data/account_manager.db` 必须由空实例初始化，禁止复制 Plus 数据库。
- `_ext_targets`、`external_logs` 和 `shared_config.db` 均为注册节点本地文件。
- `APP_INSTANCE_ID=auto-plus3`，Phone API Relay 按该 ID 保存独立 owner。

Plus 配置通过以下工具生成受限快照：

```bash
python3 scripts/registration-node-config.py export \
  --source-db /opt/auto-gpt-plus/data/account_manager.db \
  --source-instance auto-gpt-plus \
  --output /root/auto-plus3-config.json
```

目标容器首次启动并初始化空数据库后再导入：

```bash
python3 scripts/registration-node-config.py import \
  --target-db /opt/auto-gpt-register/data/account_manager.db \
  --input /opt/auto-gpt-register/auto-plus3-config.json \
  --replace
```

导入器发现任何账号行时必须失败。配置快照包含第三方凭证，文件权限必须保持
`0600`，导入前会执行 `PRAGMA integrity_check` 并在 `data/backups/` 自动创建
`0600` 的完整 SQLite 备份，提交后再次执行完整性检查。快照导入验证后应从
目标服务器删除。

## 跨机依赖

注册节点继续使用 Plus 的内部服务合同，但服务连接由主机上的加密转发提供：

| 容器地址 | 注册节点主机端口 | 旧服务器目标 |
| --- | ---: | ---: |
| `tempmail-api-1:8080` | `8080` | `127.0.0.1:18083` |
| `gpt-cccy-me:8789` | `8789` | `127.0.0.1:8791` |
| `phone-api-relay:8787` | `8787` | `127.0.0.1:8893` |
| `host.docker.internal:18765` | `18765` | `127.0.0.1:18765` |
| `openai-pay-long-link:8788` | `8788` | `127.0.0.1:8788` |
| `team-manage-app:8008` | `8008` | `127.0.0.1:8008` |
| `sms-gateway:8720` | `8720` | `127.0.0.1:8720` |
| OAIPay Submit | `18789` | `127.0.0.1:8789` |
| PayPal Agreement | `18098` | `172.20.0.1:18098` |

新节点使用固定的 `auto-plus3-internal` Docker 网络和 `br-auto-plus3` 网桥，网关
保持为 `172.20.0.1`。这既让 Plus 已保存的 `http://172.20.0.1:*` URL 无需改写，
也使 SSH 本地转发只绑定 Docker 网桥地址，而不是 `0.0.0.0` 或公网地址。

`deploy/registration-node/auto-plus3-dependency-tunnel.service` 是新服务器的持久转发
单元；`deploy/registration-node/auto-plus3-tunnel-sshd.conf` 是旧服务器的匹配用户
限制。旧服务器的 `auto-plus3-tunnel` 用户只能使用公钥、只能建立表中九条本地
TCP 转发，不允许会话、PTY、Agent/X11、Unix socket、远端转发或任意目标转发。
转发端口仍应由主机防火墙明确拒绝公网访问。

OAIPay 的管理/上传地址继续使用 `gpt-cccy-me:8789`。环境中的
`OAIPAY_SUBMIT_URL` 单独覆盖为 `http://172.20.0.1:18789`，避免与前者争用同一个
网关端口；PayPal Agreement 保持 Plus 原有的 `http://172.20.0.1:18098` 合同。

## 容量合同

- 浏览器注册请求、后端 dispatcher 和 Auth 浏览器槽的上限均为 15。
- 注册/失效测活保留槽位为 12/3；空闲 lane 可被另一 lane 借用。
- 容量模式为 `adaptive`，资源不足时严格 FIFO 排队，不用 OOM 换取表面并发。
- Solver 最大 10、预热 2；Auth 并发与 Solver 浏览器数不是同一个计数单位。
- 容器使用 `pids_limit=6144`、`shm_size=4gb`，宿主机保留至少 4 GiB 可用内存。
- Azure B 系列 CPU credit、CPU PSI、容器 PID 和宿主机内存必须同时纳入验收。

## 发布与验证

本地源码变更仍通过 `/opt/auto-gpt/deploy.sh --mode=multi` 发布并验证三个常驻
实例。注册节点只加载该次发布生成的同一 `auto-gpt:latest` 镜像，不在远端重建。

当前 Azure NSG 只允许 SSH 入站，因此正式公网入口使用命名 Cloudflare Tunnel，
不为该节点新增 `80/443` 入站规则。`deploy/registration-node/auto-plus3-cloudflared.service`
以受限 `cloudflared` 用户运行，配置中的唯一 hostname 为 `auto-plus3.cccy.me`，origin
固定为 `http://127.0.0.1:8000`，最后一条 ingress 必须是 `http_status:404`。DNS 使用
代理 CNAME 指向 `<tunnel-id>.cfargotunnel.com`，证书和公网 TLS 均由 Cloudflare 终结；
应用、Solver、CLIProxyAPI 以及全部依赖转发端口不直接暴露公网。

`deploy/registration-node/auto-plus3.nginx.conf` 仅保留为将来明确开放 Azure
`80/443` 后的直连回退模板，不与 Cloudflare Tunnel 同时启用。

验收至少包括：

1. `auto-gpt`、`auto-gpt-plus`、`auto-plus2` 与 `phone-api-relay` 全部正常。
2. `auto-plus3.cccy.me` 页面、登录和 `/api/health` 正常。
3. 新数据库账号数为零、共享配置关闭、JWT Secret 与 Plus 不同。
4. 从 `auto-plus3` 容器探测 TempMail、OAIPay、OAIPay Submit、Phone Relay、
   PayPal Agreement、长链、Team Manager、SMS Gateway 和 HME 依赖。
5. 容量快照显示 `max_concurrency=15`、保留槽位 `12/3`、Solver `max=10/warm=2`。
6. Plus 和 auto-plus3 的配置对比只允许预先声明的实例身份、维护调度和容量差异。
7. Azure NSG/UFW 不开放应用或依赖端口，Cloudflare Tunnel ingress 只有目标域名和
   兜底 `404`，公网直连 `20.89.45.132:80/443` 仍不可达。
