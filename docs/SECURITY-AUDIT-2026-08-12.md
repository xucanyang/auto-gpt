# Auto-GPT 安全审计与处置记录

审计日期：2026-08-12  
范围：`/opt/auto-gpt` 源码、前端依赖、Docker 多实例编排、Nginx 入口、三个常驻实例的认证配置与本机监听面。  
结论口径：只记录从当前代码或运行态确认的事实；未复现、不可达或只存在于开发工具链的告警不升级为生产漏洞。

## 已处置问题

### 1. 固定 12 小时会话导致活跃用户被强制重新登录

- 原因：JWT `exp` 与数据库 `expires_at` 共用同一固定截止时间，持续操作无法延长可信期。
- 处置：`api/auth.py` 和 `core/db.py` 改为服务端滑动空闲会话。默认连续空闲 12 小时失效；活动写入每 5 分钟节流；到期续期每 1 小时节流；单次完整登录有 7 天绝对上限。
- 安全边界：密码或 TOTP 变更、主动注销、撤销全部会话、实例或认证版本变化仍立即失效。JWT 的签名绝对截止不可续写，持续请求不能无限延长被盗令牌。
- 兼容性：旧会话迁移时把原固定 `expires_at` 同时作为绝对截止，不凭空延长既有会话。

### 2. 新 TOTP 密钥强度可被接口降到 160-bit 以下

- 原因：启用接口只检查 Base32 字符形状和最低长度，没有校验真实解码字节数。
- 处置：新启用密钥必须合法 Base32 且至少 20 字节（160-bit）；系统生成值和现有三个实例的 32 字符密钥保持兼容。

### 3. TOTP 临时挑战缺少挑战级总失败上限

- 原因：原实现依赖 IP 维度持久限流，但临时挑战自身没有总失败上限；攻击者更换出口可绕开同一 IP 计数继续猜测。
- 处置：挑战自身最多允许 5 次错误动态码，第 5 次后消费并要求重新输入密码；不把正常移动网络/代理切换误判为会话盗用，持久 IP 限流继续作为第二层保护。

### 4. 前端生产依赖携带已知高危公告版本

- 原因：`react-router-dom/react-router 7.13.1` 和旧构建链依赖被当前 npm 安全数据库命中。
- 处置：升级到 `react-router-dom/react-router 7.18.2`、Vite `8.2.1`，并更新无破坏性的传递依赖。更新后生产依赖和完整依赖树 `npm audit` 均为 0。

### 5. 应用层纵深防御依赖外层代理

- 原因：Uvicorn 直连默认暴露 OpenAPI；应用允许任意 CORS 来源；安全响应头和 API 禁缓存只依赖边缘配置。
- 处置：默认关闭 `/docs`、`/redoc`、`/openapi.json`，仅显式 `APP_ENABLE_API_DOCS=1` 时启用；CORS 改为默认无跨域来源并支持 `APP_CORS_ALLOWED_ORIGINS` 白名单；应用统一下发 CSP、禁止嵌入、MIME 嗅探保护、严格 Referrer/Permissions Policy、HTTPS 下的 HSTS 和 API `no-store`；Uvicorn 不再主动发送 `Server` 头。
- 认证状态投影：未登录的 `/api/auth/status` 只保留首次初始化所需字段；TOTP 启用状态、密码摘要算法和会话策略仅向有效管理员会话返回。

### 6. 外部 HTTPS 调用关闭证书校验与 Windows shell URL 拼接

- 原因：多个外部 HTTP 客户端显式使用 `verify=False`，会接受中间人证书；Windows 支付链接回退通过 `shell=True` 拼接 URL。
- 处置：YesCaptcha、CPA、CLIProxy、OAIPay、Sub2API 和共享 Camoufox 的外部请求统一恢复系统 CA/主机名校验，并对 YesCaptcha HTTP 状态关闭式失败；受配置控制的 urllib 客户端额外阻断跨主机、降级和非 HTTP(S) 重定向；Windows 浏览器回退改为参数数组直接启动 Chrome/Edge，URL 不进入命令解释器。
- 复核：Bandit 的剩余中高告警均按调用点人工复核；SHA-1 仅用于保持既有非安全兼容 ID/邮件去重格式，已显式标记 `usedforsecurity=False`，不参与密码、签名、授权或 Token 熵。

### 7. 可配置上游 URL scheme 与调试制品临时文件

- 处置：OAIPay、PayPal 绑定和本地 SMS 轮询只接受 `http/https`，拒绝 `file:`、自定义 scheme、URL 凭据和片段；30x 每一跳继续校验同源、端口和协议，禁止跨主机、HTTPS 降级及非 HTTP(S) 重定向，现有内网 HTTP 地址仍兼容。浏览器失败页和 Sentinel 中间结果改为显式开关、随机私有临时目录，固定 `/tmp` 文件名不再被并发任务覆盖。

## 已核实未发现可利用问题

- 管理 API：除健康检查、单次导出票据下载和三个独立外部 API 前缀外，均由全局认证中间件关闭式保护；外部前缀各自使用独立常量时间 Token 校验或交付 Token 合同。
- 导出：敏感导出由 128-bit 随机、5 分钟、单次消费票据承接；Nginx 日志只记录 `$uri`，不记录查询串，并拒绝旧 `access_token` 查询参数。
- 注入与文件边界：未确认可达的 SQL 注入、命令注入、任意文件读取或路径穿越；诊断制品下载使用固定文件白名单和 basename 校验。
- 密钥与数据：Git 跟踪文件未命中常见私钥/云密钥模式；认证密码、JWT 密钥和 TOTP 密钥明确排除在共享配置之外。三个实例数据库与 `.env` 文件均为 `0600`，共享配置目录为 `0700`。
- 网络：主服务、Plus、Plus2、Cliproxy、Solver 与 phone relay 的宿主机映射均绑定 `127.0.0.1`；退役 K12 不在编排中。
- Python 依赖：对当前生产镜像执行 `pip-audit`，生产依赖未发现已知漏洞。

## 剩余风险与运维动作

1. 主实例当前未启用 TOTP。不能自动生成并启用，因为没有完成验证器配对会直接造成管理员锁号；需要管理员在设置页完成扫码和动态码确认。
2. Plus2 当前管理员密码摘要仍是旧 SHA-256。代码会在下一次成功密码登录后自动迁移到 Argon2id；不知道原密码时不能安全离线重哈希。
3. Bearer Token 仍保存在浏览器 `localStorage`，兼容现有前端、扩展和 SSE 合同，但一旦同源脚本发生 XSS，令牌可被读取。本次 CSP、依赖升级和无 `dangerouslySetInnerHTML` 事实降低了风险；迁移到 HttpOnly Cookie 需要同步引入 CSRF Token、扩展认证和下载/SSE 合同，不能作为窄修复直接替换。
4. 生产容器仍以 root 运行，且浏览器自动化需要较宽的系统能力和写入挂载。后续若改成非 root，需要先梳理 Camoufox/Playwright 缓存、Xvfb、运行数据卷和外部目标挂载权限，不能只加 `USER` 导致线上任务失败。
5. 项目文档定义的正式 Docker 测试 stage/compose 尚未落地。本次使用一次性容器、只读 checkout、`/tmp` 数据库、无生产挂载和默认断网完成专项回归；这不是完整 CI 供应链替代品。

## 验证证据

- 后端隔离回归：一次性生产同源测试镜像以只读 checkout、临时 SQLite/runtime、无生产挂载和默认断网执行认证、基础设施、URL 重定向、OAIPay、Sub2API、CLIProxy 与上传门禁，结果 `111 passed, 5 subtests passed`；仅有两项既有依赖弃用提示。
- 前端：Node 合同测试 `62 passed`；TypeScript/Vite 生产构建通过，保留既有 Accounts 大 chunk 和 Vite native config 未来兼容警告。
- 供应链：Python 生产依赖 `pip-audit` 为 0；npm 生产与完整依赖树审计为 0。
- 静态审计：Bandit 为 `0 high / 19 medium / 538 low`；剩余中危均为容器内固定监听常量或固定表/列集合 SQL 拼接，经调用点复核未发现用户输入进入标识符。三个动态但有限的请求超时已在调用点标注，未作为缺少 timeout 误报保留。
- 发布验收：以 `deploy.sh --mode=multi` 的三实例运行、HTTP 健康、静态 bundle、安全响应头、OpenAPI 404 和退役端口关闭结果为最终运行证据。
