# 更新日志 (Changelog)

本项目的所有显著更改都将记录在此文件中。

格式基于 [Keep a Changelog](https://keepachangelog.com/zh-CN/1.0.0/)，并且本项目遵循 [Semantic Versioning](https://semver.org/lang/zh-CN/) (语义化版本)。

## [Unreleased] (未发布)

## [2.8.21] - 2026-07-24

### 修复 (Fixed)
- **解耦 OAuth recovery 与注册验证码预算**：`services/chatgpt_core/access_token_only_registration_engine.py` 为独立浏览器 OAuth 使用独立 OTP 等待窗口，不再被注册阶段已消耗的单账号验证码预算截断；整体浏览器事务仍受 `chatgpt_browser_oauth_hard_timeout_seconds` 和任务停止控制约束，重发后的新验证码可以完整等待并提交。

### 测试 (Tests)
- 增加 OAuth 阶段绕过注册预算、仍保留阶段验证码排除的回归测试。

## [2.8.20] - 2026-07-24

### 修复 (Fixed)
- **保留 registration_disallowed 后已实际落地的账号**：`services/chatgpt_core/access_token_only_registration_engine.py` 识别同一次协议尝试已经进入 `about_you`、首个 `create_account` 返回 `registration_disallowed`、而浏览器后备再次收到 “account already exists” 的组合。该组合现在标记为服务端已提交注册并继续 AT/RT 提取；普通历史已有账号仍遵守禁止登录恢复/跳过保存策略。

### 测试 (Tests)
- 增加服务端已提交账号与浏览器后备“已有账号”响应的回归覆盖。

## [2.8.19] - 2026-07-24

### 修复 (Fixed)
- **修复独立 Camoufox OAuth 二次邮箱验证码无法推进**：`services/chatgpt_core/browser_registration.py` 为 fresh Codex OAuth 记录邮箱提交时间并向邮箱读取器传递阶段/时间戳，避免重复消费注册阶段旧 OTP；失败重试时复用 HTTP OAuth 的 `passwordless/send-otp` 与 `email-otp/send` 重发顺序，并将真实 `device_id`、浏览器 User-Agent 和 Sentinel 传入校验兜底。
- **收紧浏览器 OAuth OTP 状态判定**：独立 OAuth 不再把“HTTP 200 但页面仍停留在邮箱验证码页”伪判定为成功，会根据服务端返回的下一状态导航到 `add_phone`、workspace 或 callback，只有状态真正推进才继续提取 AT/RT。

### 测试 (Tests)
- 增加 fresh OAuth OTP 重发、跨阶段时间戳与严格状态推进回归覆盖；保留已有注册后备和 AT/RT 提取专项测试。

## [2.8.18] - 2026-07-24

### 修复 (Fixed)
- **修复协议注册转浏览器后备时重复消费邮箱验证码**：`services/chatgpt_core/access_token_only_registration_engine.py` 现在把协议阶段已经确认的 `about_you` 状态和域级 Cookie 传给 Camoufox，优先在同一认证状态继续提交姓名/生日，不再无条件重新提交邮箱并读取另一阶段的旧 OTP。
- **修复浏览器 OTP 提交被误报为未跳转**：`services/chatgpt_core/browser_registration.py` 增加真实发码时间与跨阶段已用验证码排除，读取页面 API 响应和 React 页面状态，在 URL 不变时仍能推进到 `about_you`、回调或明确返回完整错误；浏览器 worker callback 同步传递 OTP 请求上下文。
- **补齐 about_you 页面提交兜底**：当 Camoufox 页面按钮点击没有触发可观察的导航或响应时，使用同一浏览器上下文直接调用 `create_account`，沿用页面填写的姓名/生日、Cookie、设备标识和 Sentinel，避免将 SPA 事件丢失误判为注册失败。
- **保持协议与浏览器资料一致**：后备链路沿用协议阶段生成的完整姓名和生日；独立浏览器入口也不再发送只有一个词的姓名，避免 Auth API 以资料不完整返回 400。
- **修复本地化 about_you 年龄识别**：补充马来/印尼页面的 `umur/usia` 标签，并扩大 `input[name=age]` 探测范围，避免地区画像切换后只填生日而漏掉必填年龄。
- **修复浏览器回调导航顺序**：`external_url` 状态不再被提前当成 ChatGPT 完成页，必须先跟随 `continue_url` 落地到真实 callback/home 后才结束注册阶段。
- **补齐浏览器注册后的 Token 提取**：当 Camoufox 已落地 ChatGPT callback/home 但 NextAuth session cookie 尚未写入时，复用现有 `OAuthClient` 的强制密码登录/完整 OAuth 逻辑获取 AT/RT，不再重做邮箱注册或误判为已有账号。
- **对齐 any-auto-register 的注册后 OAuth 完成策略**：当上述 HTTP OAuth recovery 仍停留在 `add_phone`、workspace 或 callback 状态时，`services/chatgpt_core/access_token_only_registration_engine.py` 现在通过共享浏览器槽位、硬超时和 OTP IPC 启动独立 Camoufox Codex OAuth；它重访原始授权 URL，沿用跨阶段验证码排除规则，成功后直接回传 AT/RT，避免把已完成的注册账号再次走邮箱注册或强制绑定手机号。
- **修复独立 OAuth 移植常量兼容性**：浏览器 Codex OAuth 改用 auto-gpt 自有 `services/chatgpt_core/oauth.py` 的 client、redirect URI 和 scope 常量，避免引用对照项目私有的 `CODEX_*` 符号导致 recovery worker 在启动后立即 ImportError。

### 测试 (Tests)
- 新增浏览器后备状态恢复、OTP callback 时间/排除参数、API 成功但 URL 不变及错误响应回归；前端侧栏版本同步更新为 `v2.8.18`。

## [2.8.17] - 2026-07-24

### 修复 (Fixed)
- **修复 Camoufox 运行时误判未安装**：`scripts/install_camoufox.py` 改用 Camoufox 0.5.4 的 `browsers/official/<version>-<release>` 多版本目录并写入 `active_version`，保留构建阶段下载的固定浏览器、GeoIP 和 UBO 资源，避免注册后备、LocalSolver 或普通浏览器启动时因旧扁平目录被清理而触发运行时下载。`services/chatgpt_core/browser_registration.py` 同时支持 `CAMOUFOX_EXECUTABLE_PATH` 和旧镜像扁平二进制，并从 `version.json` 传递匹配的 Firefox 主版本。

### 测试 (Tests)
- 新增 Camoufox 自定义可执行路径、版本参数、非法路径 fail-closed 及浏览器注册阶段参数传递回归；前端侧栏版本同步更新为 `v2.8.17`。

## [2.8.16] - 2026-07-24

### 新增 (Added)
- **ChatGPT 注册增加同浏览器上下文后备链路**：`services/chatgpt_core/browser_registration.py` 复用 `/opt/any-auto-register` 已实际完成注册阶段的 Camoufox 状态机；当原有协议链路命中 `registration_disallowed`、Cloudflare 403 或浏览器开户基础设施错误时，在同一个邮箱上改走“邮箱提交 -> 一次性验证码 -> about_you -> callback”，并将浏览器生成的域级 Cookie 合并回 `ChatGPTClient`，继续沿用本项目既有的 ChatGPT Session / AccessToken 提取、第二阶段 Auth/RT、邮箱占用和账号入库逻辑。对照项目仍失败的独立 Codex OAuth 重登段未被迁入。

### 优化 (Changed)
- **完整浏览器注册纳入统一资源门禁**：`services/chatgpt_core/sentinel_browser.py` 与 `sentinel_browser_worker.py` 扩展可双向回调的隔离进程协议，浏览器子进程只在进入 OTP 页后向父任务请求验证码；整个事务继续受全局最多两槽、cgroup 第二槽内存判断、任务停止检查和最长 600 秒硬截止控制，超时或异常会清理 Camoufox、Playwright 与脱离 Session 的子进程，避免绕过 v2.8.13-v2.8.15 已建立的内存保护。
- **Camoufox 代理画像补齐 GeoIP 能力**：`requirements.txt` 改用 `camoufox[geoip]==0.5.4`，`Dockerfile` 在构建时下载并校验 MMDB；认证 SOCKS5 继续通过本机 HTTP CONNECT 桥交给浏览器，Camoufox 据真实出口同步地区、时区与 locale，而不是暴露容器默认画像。

### 修复 (Fixed)
- **协议开户失败不再立即浪费当前邮箱**：`services/chatgpt_core/access_token_only_registration_engine.py` 默认启用浏览器后备，仅对明确的风控/浏览器开户失败触发；成功结果记录 `registration_transport=camoufox_browser_fallback`，浏览器 worker 不可用或硬超时被提升为批次级基础设施错误，停止继续创建邮箱。可通过任务配置 `chatgpt_browser_registration_fallback_enabled=false` 显式关闭。

### 测试 (Tests)
- 新增浏览器 worker OTP 双向 IPC 与停止传播、共享槽位结果解析、`registration_disallowed` 后备成功、显式关闭以及新基础设施致命分类回归；注册/Sentinel/任务控制专项 104 项通过，侧栏版本同步更新为 `v2.8.16`。

## [2.8.15] - 2026-07-24

### 优化 (Changed)
- **浏览器 2 槽改为 cgroup 内存自适应**：`services/chatgpt_core/sentinel_browser.py` 保留单实例最大 2 套 Chromium，但不再无条件填满第二槽。第一套浏览器始终可运行；第二套仅在 `memory.current + reserve <= memory.max` 时启动，默认 reserve 为 1280 MiB，可通过 `BROWSER_SECOND_SLOT_RESERVE_MIB` 在 512-2048 MiB 范围调整。内存高水位时 Auth 或前置 Sentinel 会排队并记录当前值、上限与保留量，自动退化为单浏览器；建箱、协议请求、发码和 OTP 轮询的注册 worker 并发仍保持原配置。

### 修复 (Fixed)
- **避免 Python 高水位与双浏览器再次顶满 2.5 GiB**：`auto-gpt-plus` 的真实 200 目标 / 5 worker 压测显示，修复卡死和无界日志后，Python 匿名堆在活跃注册阶段仍可达到约 1.28 GiB；固定两套有头 Chromium 叠加时 cgroup 峰值达到约 2.40 GiB，仅余约 105 MiB。自适应第二槽在该状态下保持一个 Chromium，避免正常轮转任务重新触发 swap、`memory.events.max` 与 PSI 压力，同时在冷启动低占用时仍允许两套并行。

### 测试 (Tests)
- 新增第二槽内存保留回归，验证 cgroup 余量不足时第二事务不会进入、余量恢复后可继续并发且信号量正确释放；Sentinel/Auth 专项 18 项通过，侧栏版本同步更新为 `v2.8.15`。

## [2.8.14] - 2026-07-24

### 修复 (Fixed)
- **硬超时清理覆盖 Chromium 自建 Session**：真实 Xvfb smoke 发现 Playwright Node 继承 browser worker 的 PGID，但 Chromium 主进程会再次 `setsid()`，Crashpad 也会脱离父进程并各自建立进程组；仅对 worker PGID 发信号仍可能在 Renderer 冻结时留下浏览器。`services/chatgpt_core/sentinel_browser.py` 现在为每次事务注入随机 `AUTO_GPT_BROWSER_WORKER_ID`，超时、停止、worker 异常及正常退出残留检查都会从 `/proc/*/environ` 找出继承该事务标记的 worker、Node、Chromium 主进程和 Crashpad，先 TERM、再 KILL 所有独立 PGID/PID，Chrome 同组的 zygote、renderer、GPU 与 utility 进程随主进程组一并回收。

### 测试 (Tests)
- 将浏览器硬超时与停止测试中的模拟 Chromium 改为 `start_new_session=True`，锁定跨 Session 子进程仍被清理且后续事务可复用信号量槽；容器实测确认 worker/Node、Chromium、Crashpad 分属三个以上 PGID/SID，但都继承同一事务标记。侧栏版本同步更新为 `v2.8.14`。

## [2.8.13] - 2026-07-24

### 优化 (Changed)
- **Sentinel 与 Auth Chromium 共用全局容量**：`services/chatgpt_core/sentinel_browser.py` 将前置 Sentinel token 浏览器和最终 Auth 开户浏览器统一纳入进程级 2 槽闸门。注册 worker 仍可按任务配置并发执行建箱、发码和 OTP 轮询，但单个实例内任意时刻最多运行两套 Playwright Chromium，避免注册并发 5 时前置浏览器绕过 Auth 限制叠加占满 2.5 GiB 容器。
- **任务日志改为有界内存窗口**：`core/task_runtime.py` 按 UTF-8 实际字节限制活跃任务为 4000 条 / 4 MiB，终态快照完成 SQLite 持久化后将内存副本压缩为 500 条 / 512 KiB；`api/tasks.py` 将已完成任务内存保留量从 200 个降到 50 个。快照新增 `logs_truncated`、丢弃条数/字节数和单调日志游标，历史 TaskLog 继续保留较大的活跃窗口，不会被终态内存压缩后的重复写入覆盖。

### 修复 (Fixed)
- **冻结的 Renderer 获得外部硬截止**：新增 `services/chatgpt_core/sentinel_browser_worker.py`，每次 Sentinel/Auth 浏览器事务都由独立 OS session 中的 worker 执行。父进程为 Sentinel 设置默认 90 秒、Auth 开户设置默认 150 秒硬截止，并在 `finally` 释放浏览器槽；v2.8.13 初版按 worker 进程组执行 TERM/KILL，后续 v2.8.14 真实进程树验证发现 Chromium 会再次 `setsid()`，因此补充了跨 Session 事务标记清理。截止时间可分别通过 `SENTINEL_BROWSER_HARD_TIMEOUT_SECONDS` 与 `AUTH_BROWSER_HARD_TIMEOUT_SECONDS` 调整。
- **任务停止可以中断正在运行的浏览器事务**：`ChatGPTClient`、`OAuthClient` 与 AccessToken 注册引擎将任务控制检查传到浏览器父进程；等待槽位或浏览器运行期间收到停止请求，会先完整清理子进程组再传播原始任务中断，不再等待页面内部 JavaScript 超时。
- **日志窗口移动后 SSE 仍持续推送**：`api/tasks.py` 与 `frontend/src/components/TaskLogPanel.tsx` 不再把当前数组长度当作永久游标，改用 `log_start_index / log_next_index` 单调序号。活跃日志达到裁剪上限后，新日志仍会实时到达，前端自身也只保留最近 4000 行，避免长任务把浏览器页面内存同步拖高。

### 测试 (Tests)
- 扩展 Sentinel/Auth、任务运行时、SQLite 终态历史和 SSE 回归，覆盖 worker 正常结果与日志传输、整个子进程组硬超时清理、停止中断、超时后槽位复用、前置 Sentinel 与 Auth 共享容量、UTF-8 条数/字节裁剪、持久化先于内存压缩及移动窗口游标；专项注册链路 86 项通过，前端侧栏版本同步更新为 `v2.8.13`。

## [2.8.12] - 2026-07-24

### 修复 (Fixed)
- **强制刷新注册画像时保持配置加载器稳定**：`frontend/src/pages/Accounts.tsx` 使用 `useRef` 保存配置缓存，使 `loadConfigCache` 不再随 React state 更新而改变函数引用。注册弹窗每次打开仍会强制读取最新 `/api/config`，但不会因为 effect 依赖变化持续重复请求配置接口。

### 测试 (Tests)
- 扩展注册画像前端合同测试，锁定稳定缓存 ref、强制服务端刷新和任务启动不写全局配置；前端侧栏版本同步更新为 `v2.8.12`。

## [2.8.11] - 2026-07-24

### 优化 (Changed)
- **有头 Auth 开户增加进程级并发闸门**：`services/chatgpt_core/sentinel_browser.py` 将跨任务同时运行的 Xvfb Chromium 开户事务限制为最多 2 个，其余已完成邮箱 OTP 的尝试等待浏览器槽位后继续。避免注册并发 5 或旧任务尚未排空时触发 GLib worker pool 耗尽、Chromium 在 `BrowserContext.new_page` 前崩溃；邮箱建箱、发码和 OTP 轮询仍可并发执行。

### 修复 (Fixed)
- **注册弹窗不再被旧浏览器设置污染**：`frontend/src/pages/Accounts.tsx` 提升注册表单本地存储版本，并在每次打开弹窗时以 `/api/config` 的邮箱服务、TempMail 域名、代理国家和 OTP 等待策略作为默认值。邮箱服务下拉明确为“仅本任务”，历史保存的 `HME Ready + JP + 60s` 不会再覆盖共享默认的 `TempMail aa8.pl + SG + 120s`。
- **开始注册不再反写共享配置**：账号页弹窗与独立注册页创建任务时都只把当前表单画像放入该任务请求，不再隐式调用配置保存接口。只有账号页显式点击“保存设置”才更新代理、独立出口、登录路由和 OTP 等全局值，修复旧页面在启动任务时把共享配置 `dynamic_proxy_default_country` 从 `SG` 改回 `JP` 的问题。

### 测试 (Tests)
- 新增 Auth 浏览器跨线程并发上限回归与注册弹窗静态合同测试，锁定服务端配置优先、任务启动零全局写入和显式保存完整 OTP 配置；前端侧栏版本同步更新为 `v2.8.11`。

## [2.8.10] - 2026-07-24

### 优化 (Changed)
- **注册默认画像对齐成功 HAR**：共享运行配置将 ChatGPT 邮箱注册默认来源切换为 TempMail 固定域名 `aa8.pl`，动态代理默认出口切换为 `SG`。真实对照中 `HME + JP` 在有头浏览器下仍返回应用层 `registration_disallowed`，而 `aa8.pl + SG` 与新 HAR 的邮箱域名、Auth `country_code_hint=SG` 和 Cloudflare SIN 路由一致。

### 修复 (Fixed)
- **开户事务固定使用 Xvfb 有头 Chromium**：`ChatGPTClient` 与 `OAuthClient` 的 Auth finalize 只在 `auth.openai.com/about-you` 开户事务中强制有头浏览器；共享 `core/browser_runtime.py` 允许显式 headed 请求覆盖容器级 `PLAYWRIGHT_HEADLESS=1`。修复完整 JSD、`cf_clearance` 与 Sentinel `p/t/c` 均存在时，headless 开户 POST 仍被 Cloudflare 返回 403 challenge 的问题；密码、OTP、OAuth 和其他协议步骤保持原执行模式。

### 测试 (Tests)
- 新增容器环境变量与显式 headed 优先级、两条开户客户端必须传递 `headless=False` 的回归。真实单账号任务 `task_codex_har_parity_sg_aa8_headed_v289_1784845000109` 已验证 `create_account 200`、OAuth callback 200、AccessToken 入库及任务 `1/1 done`；前端侧栏版本同步更新为 `v2.8.10`。

## [2.8.9] - 2026-07-24

### 修复 (Fixed)
- **Sentinel SDK 改由顶层 Auth 页面调用**：v2.8.8 真实单账号验证进一步确认 `SentinelSDK.token()` 与 `init()` 一样禁止在 iframe 内执行。`services/chatgpt_core/sentinel_browser.py` 删除 Auth finalize 中手工查找/注入 Sentinel iframe 的错误分支，改为在同一 `auth.openai.com/about-you` 顶层 Page 中加载并调用 SDK；SDK 自身创建隐藏 frame，通过 `postMessage` 驱动 `sentinel.openai.com/backend-api/sentinel/req`，与成功 HAR 的调用栈和 Referer 一致。
- **阻止再次从 frame 调用 SDK**：共享 Sentinel token helper 在执行前显式校验 `window.top === window`，并始终按顶层合同执行 `init(flow) + token(flow)`。开户请求仍由同一个 Auth Page 的 `fetch('/api/accounts/create_account')` 发出，继续保留 v2.8.7 已建立的 Cloudflare、Sentinel 与注册 Cookie 连续性。

### 测试 (Tests)
- 更新 Sentinel 浏览器合同测试，锁定顶层上下文校验、SDK 初始化以及 Auth finalize 将真实顶层 Page 传给 token helper；前端侧栏版本同步更新为 `v2.8.9`。

## [2.8.8] - 2026-07-24

### 修复 (Fixed)
- **嵌入式 Sentinel 不再错误调用 `init()`**：v2.8.7 首次单账号线上 smoke 证明 Auth 页面与 Cloudflare Cookie 已进入同一 Playwright context，但注入的 Sentinel iframe 明确返回 `init() should not be called from within an iframe`。该版本先取消 iframe 内的 `init()` 并保留 `token()`；后续 v2.8.9 真实验证确认 SDK 的两个公开方法都必须从顶层调用，并完成最终修正。
- **基础设施错误取消账号内三轮重试**：`services/chatgpt_core/access_token_only_registration_engine.py` 将 `sentinel_browser_unavailable` 和 `auth_browser_finalize_unavailable` 设为账号内不可重试错误。浏览器启动、frame 或 finalize 链路故障时不再对同一个 HME 地址重复走三轮首页、验证码和开户，随后由任务层 fatal gate 停止调度新邮箱。

### 测试 (Tests)
- 新增 iframe Sentinel 调用模式和账号内 retry gate 回归，锁定嵌入 frame 的 `initializeSdk=false`、普通服务端 `registration_disallowed` 仍可按现有策略重试，以及浏览器基础设施错误立即返回；前端侧栏版本同步更新为 `v2.8.8`。

## [2.8.7] - 2026-07-24

### 优化 (Changed)
- **开户阶段改为单一 Auth 浏览器事务**：`services/chatgpt_core/sentinel_browser.py` 新增浏览器拥有的 `about-you` finalize 流程，将协议会话 Cookie 按原始域和路径导入 Playwright，在真实 `auth.openai.com/about-you` 页面完成 Cloudflare JSD、Sentinel iframe `oauth_create_account` token 和 `POST /api/accounts/create_account`。开户请求不再把 Playwright 生成的风控 token 交给另一个 `curl_cffi` 会话发送，保持 Cookie、设备标识、TLS/HTTP2 浏览器连接和 Sentinel 上下文一致。
- **浏览器 Cookie 双向同步保留真实作用域**：共享 Cookie bridge 不再把 `.openai.com` 压成 `auth.openai.com` host-only，也不再为同一 Cookie 同时写入点域和裸域副本。浏览器运行期间新生成的 `cf_clearance`、`oai-sc`、`__cf_bm`、登录会话及开户响应 Cookie 会按原始 domain/path 回灌协议会话，供 OAuth callback、AccessToken 抓取和后续账户状态推进复用。
- **两套注册引擎统一开户合同**：`ChatGPTClient.create_account()` 与 `OAuthClient._submit_about_you_create_account()` 都接入同一浏览器 finalize，不再单独拼装 `oai-device-id` 和 Sentinel 请求头后执行协议 POST。浏览器请求补齐每次动作独立的 `x-access-flow-invocation-id` 与现有 Datadog trace，HTTP 错误仍保留 `registration_disallowed` 等精确服务端错误码。

### 修复 (Fixed)
- **修复完整 `p/t/c` 仍被拒绝开户**：此前 v2.7.3 虽已修通认证 SOCKS5 到 Chromium 的本地 HTTP CONNECT 桥，并能生成非空 `p/t/c`，但临时 Sentinel 浏览器只访问顶层 frame、只接收扁平 Cookie 字符串且退出前不回写 Cookie；最终开户由另一个会话发送，缺少浏览器生成的 `cf_clearance`、`oai-sc` 及域级稳定身份。现在开户前日志只记录 Cookie 名和 Sentinel 字段长度，既可核验上下文连续性，也不会输出凭证值。
- **浏览器基础设施失败立即停止批次**：新增 `auth_browser_finalize_unavailable` 致命基础设施分类。Auth 页面跳转异常、Sentinel frame 不可用或同浏览器开户请求未完成时，注册批次停止调度新邮箱，避免在本地浏览器链路故障时持续消耗 HME 地址和代理尝试。

### 测试 (Tests)
- 扩展 Sentinel、ChatGPT 注册、OAuth about-you 与任务控制回归，覆盖 `.openai.com` Cookie 域保留、无重复域写入、同一 Playwright context 顺序执行 Auth 页面/Sentinel/开户、浏览器 Cookie 回灌、禁止回退 `session.post()`、`x-access-flow-invocation-id` 请求合同、`registration_disallowed` 错误保真及基础设施失败批次 fail-fast；前端侧栏版本同步更新为 `v2.8.7`。

## [2.8.6] - 2026-07-24

### 新增 (Added)
- **ChatGPT Team 固定 24 小时有效期**：`services/chatgpt_core/pix_payment_link_cleanup.py` 以当前链接的 `generated_at`（兼容 `created_at`）派生提取后 24 小时过期点，并在当前缓存缺少时间时按账号和 URL 回退到 `payment_link_generations.generated_at`。恰好第 24 小时进入过期状态；仍无提取时间的链接保持“状态未知”，不使用无来源的过期猜测。
- **十类支付链接开放五状态人工删除**：Hosted Checkout、PayPal、iDEAL、UPI、PIX、TWINT、Kakao Pay、GoPay、ChatGPT Team 与其他链接均支持按“有效 / 已支付 / 过期 / 支付已取消 / 状态未知”单独预览和删除。数量为零时不显示删除动作；未知状态继续默认保留，仅在运营人员主动展开并二次确认后删除。

### 优化 (Changed)
- **扫描弹窗全部默认收起**：`PixLinkScanModal.tsx` 不再根据是否存在链接自动展开类型面板。首次扫描和重新扫描都以十个类型全部收起呈现，减少大批量状态表占用；需要查看计数或执行删除时再展开对应类型。
- **通用删除墓碑兼容全部组合**：保留 PIX、UPI、iDEAL 既有九种清理墓碑，新类型以及有效/未知状态统一写入 `payment_link_deleted`，并通过 `payment_link_cleanup_type`、`payment_link_cleanup_mode`、`payment_link_cleanup_through_at` 和 `previous_link_status` 保留实际语义。`payment_link_cache.py`、账号摘要和前端状态映射同步识别该墓碑，允许后续重新生成链接。
- **删除范围保持最小化**：删除事务只移除账号当前支付链接对象中的 URL 字段，并仅在 `cashier_url` 与扫描确认的当前 URL 完全一致时清空镜像字段；账号、支付生成历史、支付 CDK、提交结果及其他业务状态均不修改。

### 修复 (Fixed)
- **防止通用删除后旧链接被列表索引复活**：`services/account_filters.py` 的 Python 与全部 SQLite 派生分支加入 `payment_link_deleted` 终态识别，同时提升账号列表派生版本和筛选解析版本，确保三个常驻实例重算后统一显示为“当前无链接”。
- **保留删除前并发复核**：所有新增类型和状态继续执行预览、二次确认、SQLite 完整性校验备份、`BEGIN IMMEDIATE` 事务及 URL 精确复核；扫描后链接被其他任务刷新时跳过该账号，不会误删新链接。

### 测试 (Tests)
- 扩展支付链接扫描、清理任务、列表派生和前端静态合同回归，覆盖 Team `23:59:59 / 24:00:00` 边界、生成历史时间回退、十类型乘五状态的预览与删除、通用墓碑可重新生成、未知链接人工删除、历史数据保留、精确 `cashier_url` 清理及全部面板默认收起；前端侧栏版本同步更新为 `v2.8.6`。

## [2.8.5] - 2026-07-24

### 新增 (Added)
- **支付链接扫描覆盖全部规范类型**：`services/chatgpt_core/pix_payment_link_cleanup.py` 与 `GET /tasks/chatgpt/payment-links/scan` 复用账号列表的统一分类合同，同时扫描 Hosted Checkout、PayPal、iDEAL、UPI、PIX、TWINT、Kakao Pay、GoPay、ChatGPT Team 与其他链接。响应按类型提供有效、已支付、过期、支付已取消和状态未知五个互斥状态桶，并保留 PIX/UPI 兼容字段与旧接口。
- **iDEAL 增加 15 分钟有效期与过期清理**：iDEAL 当前链接以 `generated_at`（兼容 `created_at`）后的 15 分钟作为明确过期点，缺少提取时间时保持状态未知而不猜测。过期 iDEAL 可沿用现有预览、确认、SQLite 校验备份、事务清理和列表索引刷新流程，清理后记录独立的 `ideal_expired_cleaned` 墓碑，历史生成记录保持不变。

### 优化 (Changed)
- **扫描弹窗改为按支付类型折叠**：`frontend/src/features/accounts/components/PixLinkScanModal.tsx` 展示全部支付类型，有链接的面板默认展开，零链接面板默认收起，避免空状态表格占用大块空间。总数、各类型数量和状态计数使用稳定布局；PIX/UPI 继续显示 Stripe 实时查询覆盖，iDEAL 明确显示“提取后 15 分钟到期”。
- **未知状态安全保留**：没有实时状态、明确本地终态或可信过期时间的链接不再被误标为“有效”，统一进入“状态未知”并显示保留状态。Hosted、PayPal、TWINT、Kakao Pay、GoPay、Team 和其他类型暂不暴露无证据清理动作；PIX/UPI 保留原三类清理，iDEAL 仅在过期状态提供清理入口。
- **清理任务按类型表达**：`api/tasks.py`、`Accounts.tsx` 与 `RegisterTaskModal.tsx` 让清理任务来源、日志、任务标题和确认文案携带实际支付类型，不再把 UPI/iDEAL 统一显示成 PIX；账号详情与当前链接摘要同步识别 iDEAL 清理状态和通用清理元数据。

### 修复 (Fixed)
- **修复混合扫描把非 QR 链接计为 Stripe 查询失败**：实时查询分母和本地兜底数量现在只统计 PIX/UPI 指令链接，其他支付类型按持久化证据分类，不再拉低 Stripe 查询成功率。
- **修复 iDEAL 清理后列表索引可能复活旧链接**：`services/account_filters.py` 的 Python 与 SQLite 派生逻辑同时识别三种 iDEAL 清理墓碑，并提升派生/筛选合同版本，确保三个常驻实例重算后都将已清理链接稳定归为“当前无链接”。

### 测试 (Tests)
- 扩展支付链接扫描、清理任务和前端合同回归，覆盖十类混合链接的互斥状态总账、未知链接保留、iDEAL 第 15 分钟边界、过期清理墓碑、列表索引刷新、独立任务来源，以及零条类型默认收起；前端生产构建通过，侧栏版本同步更新为 `v2.8.5`。

## [2.8.4] - 2026-07-24

### 新增 (Added)
- **支付链接列表补齐 long-link 全类型筛选**：`frontend/src/pages/Accounts.tsx` 的“当前链接类型”现在按 `/opt/openai-pay-long-link` 规范完整提供 Hosted Checkout、PayPal、iDEAL、UPI、PIX、TWINT、Kakao Pay、GoPay 与 ChatGPT Team 九种独立选项，同时保留“其他支付链接”和“当前无链接”兜底。账号行类型标签同步展示规范名称，不再把已支持通道压缩成模糊的 ChatGPT/其他分类。

### 优化 (Changed)
- **统一当前链接类型的 Python/SQL 分类合同**：`services/account_filters.py` 让账号序列化、纯 Python 筛选和 `account_list_state` SQLite 派生索引使用相同的 long-link 类型语义。具体 `payment_method_type` 可以纠正泛化 Hosted，Team 通过 `link_type`、`generation_kind`、`plan` 或 `plan_name` 保持独立；规范 UPI/PIX 路径及 PayPal、iDEAL、TWINT、Kakao Pay 域名仅作为旧记录兜底，未知类型继续归入 `other`。
- **旧筛选组合无损迁移**：历史前端筛选值和 API 参数 `chatgpt` 自动展开为 `hosted + team`，其他旧别名同步归一化到规范值。提升账号列表派生版本与筛选解析版本，三个常驻实例会自动重算已有 `account_list_state`，无需重写支付链接历史或修改数据库结构。

### 修复 (Fixed)
- **修复 iDEAL 等已知链接无法单独筛选**：此前后端只单列 PIX、UPI、PayPal，将 Hosted/Team 合并为 `chatgpt`，并把 iDEAL、TWINT、Kakao Pay、GoPay 全部落入 `other`；新增筛选项即使显示也无法匹配缓存数据。现在前端选项、列表标签、批量任务筛选范围与后端派生索引保持一致。

### 测试 (Tests)
- 扩展账号筛选与前端静态合同回归，覆盖九种规范类型、Hosted 加具体支付方式、Team 元数据、TWINT/Kakao Pay 旧 URL、未知类型、无链接、旧 `chatgpt` 组合，以及纯 Python 与 SQLite 派生结果一致性；侧栏版本同步更新为 `v2.8.4`。

## [2.8.3] - 2026-07-24

### 新增 (Added)
- **Team 账单国家改为任务级可选参数**：`frontend/src/pages/Accounts.tsx` 在“动态 IP 国家”之外新增独立“账单国家”选择器，默认 `US`，提供 long-link 当前支持的 35 个国家，并在选项中直接显示对应币种。切换账单国家会重新读取本次 Team profile，但不会联动或改写动态代理出口。

### 优化 (Changed)
- **Team 不再继承管理页账单配置**：`api/tasks.py`、`services/chatgpt_core/plugin.py` 与 `long_link_payment_client.py` 在 Team profile 预览、单账号 action 和批次提交中始终显式传递已校验的 canonical `billing_country`；未传时固定使用 `US`，旧 `country/currency` 字段会在客户端边界清除。Auto-GPT 不向 long-link 发送 `currency`，币种继续由 long-link 按账单国家唯一派生，Plus 支付链接仍保持原管理配置合同。
- **账单国家进入 Team 变体与审计身份**：`payment_link_cache.py`、`long_link_payment_client.py`、`api/actions.py` 与任务历史将实际账单国家、派生币种贯穿 profile 冻结、变体键、缓存匹配、成功历史防重及结果持久化。相同账号与业务参数在不同账单国家下不会互相复用；即使旧客户端同时提交 `country/currency`，也以任务级账单国家为准；上游返回国家与任务选择不一致时会失败关闭。

### 测试 (Tests)
- 扩展 Team 支付、long-link 客户端和账号页合同测试，覆盖默认 `US`、显式国家、非法国家拒绝、国家/币种变体隔离、动态 IP 与账单国家独立、旧 `country/currency` 输入清理、profile/batch 覆盖一致性及历史持久化；前端生产构建、单测与 ESLint 通过，侧栏版本同步更新为 `v2.8.3`。

## [2.8.2] - 2026-07-24

### 新增 (Added)
- **Team 支付页模式改为任务级配置**：`frontend/src/pages/Accounts.tsx` 在 Team 优惠码长链接配置中增加 Hosted / Custom 分段选择，默认明确选中 `hosted`。Profile 预览与批次提交都会携带本次选择，不再继承 `/opt/openai-pay-long-link` 管理页当前支付方式的 `checkoutMode`；Plus 支付链接仍保持原管理配置合同。

### 优化 (Changed)
- **支付页模式进入 Team 变体身份**：`api/tasks.py`、`api/actions.py`、`services/chatgpt_core/payment_link_cache.py`、`long_link_payment_client.py` 与 `plugin.py` 将 `checkout_ui_mode` 贯穿参数过滤、profile 冻结、任务元数据、单账号/批量结果持久化、缓存匹配和变体键。相同账号及 Team 业务参数在 Hosted / Custom 下不会互相复用，运行日志会记录实际冻结模式。

### 修复 (Fixed)
- **修复 Team 优惠链接生成成功但无法打开**：此前 Team 请求未传支付页模式，会继承 long-link 当前 UPI 的 `custom` 配置；上游仅返回 Checkout Session ID 时，最终保存为依赖 ChatGPT 自定义支付页上下文的 `chatgpt.com/checkout/openai_llc/...` 路由。现在缺省请求强制覆盖为 `hosted`，生成可独立打开的 `pay.openai.com/c/pay/...` Hosted Checkout 长链。
- **阻止旧 Custom 链被 Hosted 默认错误复用**：没有 `checkout_ui_mode` 字段的历史 Team 缓存会按 URL 形态识别旧 Custom 路由，账号扫描和成功历史防重不会再把这类故障链接视为 Hosted 变体；用户可直接为同账号重新生成新的 Hosted 链。

### 测试 (Tests)
- 扩展 Team 支付、long-link 客户端和账号页静态合同测试，覆盖默认 Hosted、显式 Custom、非法模式拒绝、Hosted / Custom 变体隔离、旧 Custom URL 识别、profile/batch 覆盖一致性及前端参数提交；侧栏版本同步更新为 `v2.8.2`。

## [2.8.1] - 2026-07-24

### 修复 (Fixed)
- **统一账号行级 Team 支付链接入口**：`frontend/src/pages/Accounts.tsx` 将账号行内“支付链接生成”和更多菜单中的“强制重新生成”统一接入 v2.8.0 的 Plus / Team 配置弹窗。单账号操作现在也必须显式选择 Team 动态 IP 国家，并可编辑默认 `MyTeam` Workspace，不再通过旧动作面板绕过任务级 Team 参数。
- **锁定行级任务范围**：从账号行打开配置时，支付链接任务只提交当前账号 ID，不受页面已有勾选或筛选范围影响；弹窗明确显示目标账号，防止单账号重新生成误扩展为批量任务。

### 测试 (Tests)
- 增加账号页静态合同回归，锁定普通生成与强制重新生成共用配置弹窗、当前账号 ID 定向提交和目标范围提示；前端侧栏版本同步更新为 `v2.8.1`。

## [2.8.0] - 2026-07-24

### 新增 (Added)
- **Team 优惠链接支持任务级动态 IP 国家**：`frontend/src/pages/Accounts.tsx` 在 Plus / Team 支付链接配置中为 Team 增加必选、可搜索的两位国家代码控件。国家选择随 profile 预览和批次请求提交，只控制本次 Team Checkout 的动态代理出口，不再继承 `/opt/openai-pay-long-link` 当前 UPI/PIX/Plus 路由地区，也不改写账单国家、币种或全局代理配置。
- **Workspace 提供可编辑默认名称**：Team Workspace 输入框现在实际预填 `MyTeam`，用户仍可按批次修改；上游 Team profile 在管理默认值为空时也使用同一默认值，避免空 Workspace 到执行阶段才返回 422。

### 优化 (Changed)
- **代理国家贯穿配置身份与审计信息**：`api/tasks.py`、`services/chatgpt_core/payment_link_cache.py`、`long_link_payment_client.py` 和 `plugin.py` 将 `checkout_proxy_region` 纳入 profile 冻结、变体键、缓存复用、任务元数据、运行日志和支付历史。相同优惠码与 Workspace 在不同代理国家下会形成不同 Team 变体，不会复用其他地区生成的链接。
- **支付国家语义拆分展示**：账号页 Team 配置预览分别展示“动态 IP 国家”和“账单国家 / 币种”，账号列表及详情历史显示实际 Team Checkout IP 国家；前端侧栏版本同步更新为 `v2.8.0`。

### 修复 (Fixed)
- **禁止 Team 国家静默回退**：Team profile 和批任务缺少有效两位国家码时，会在账号扫描及远端提交前直接拒绝；上游严格按所选国家改写动态代理并核验真实出口，地区不匹配时不会继续生成优惠链接。

### 测试 (Tests)
- 扩展 Team 支付专项、long-link 客户端和账号页合同测试，覆盖国家必填、大小写归一化、不同国家变体隔离、profile/batch 覆盖一致性、历史持久化、`MyTeam` 默认值及安全预览。

## [2.7.3] - 2026-07-23

### 优化 (Changed)
- **注册浏览器与协议指纹重新对齐**：`requirements.txt` 将相关运行栈固定为 `playwright 1.58.0 / Chromium 145.0.7632.6 / curl_cffi 0.15.0 chrome145`，`services/chatgpt_core/utils.py` 只为新注册尝试生成同版本 User-Agent、Client Hints 与 TLS impersonate。修复镜像无上限依赖已漂移到 Chromium 148、任务仍随机声明 Chrome 131/133/136 的跨层指纹矛盾；`frontend/src/app/AppShell.tsx` 同步展示 `v2.7.3`。
- **Sentinel 入口与会话信号统一**：`services/chatgpt_core/sentinel_browser.py` 改为在项目已验证的 Sentinel frame 与固定 SDK 上调用 `SentinelSDK.init/token`，同时携带本次注册的 `oai-did`、OpenAI 会话 Cookie、语言、视口和完整 Client Hints。`sentinel_batch.py` 与 OAuth 浏览器 bootstrap 复用同一组 URL、浏览器版本和代理适配，避免同一注册流程内部再次漂移。

### 修复 (Fixed)
- **修复认证 SOCKS 代理导致 Sentinel 浏览器完全不可用**：新增 `core/playwright_proxy.py`，把 Playwright 不支持的“带账号密码 SOCKS5”安全适配为仅监听 loopback 的临时 HTTP CONNECT 代理，再通过 PySocks 使用原凭据和远端 DNS 转发。注册 Sentinel、OAuth bootstrap、账号浏览器登录、批量 Sentinel 与通用 Playwright executor 全部接入该适配，解决动态 Cliproxy 下 `net::ERR_NO_SUPPORTED_PROXIES` 后持续 `registration_disallowed` 的主故障。
- **禁止注册用残缺 Sentinel token 继续撞接口**：`ChatGPTClient` 与 `OAuthClient` 的创建账号阶段必须拿到浏览器生成且同时包含 `p/t/c` 的 token；浏览器失败时不再降级到只有 PoW 的 HTTP token，也不会继续提交 `create_account`。`api/tasks.py` 将 `sentinel_browser_unavailable` 视为基础设施故障并立即停止批次，避免连续消耗邮箱、代理和注册尝试。

### 测试 (Tests)
- **代理、Sentinel 与批次停止回归**：新增认证 SOCKS loopback 适配、远端 DNS、完整 `p/t/c` 校验、Cookie 传递、禁止 HTTP 降级、创建前终止、浏览器版本锁定和批次 fail-fast 覆盖；真实 Sentinel 探针已分别验证直连与当前动态认证 SOCKS 代理均可返回完整 `p/t/c` token。

## [2.7.2] - 2026-07-23

### 优化 (Changed)
- **本地订阅状态缺失自动补刷**：`services/chatgpt_core/local_status_refresh.py` 在手机绑定后的 Auth/RT 补抓、支付成功回写及手工本地状态刷新等既有入口中，若首轮探测已经确认认证有效但未得到当前套餐，或已确认付费套餐但缺少订阅到期时间，会等待 3 秒后以完整探测链路重试一次。重试仍会执行后端资料、`accounts/check` 兜底和 Codex 状态探测，避免 OpenAI 侧状态传播稍慢时把刚完成手机号绑定的 Plus 账号降为 `subscription_type=unknown` 并从“Plus 长效未传”中错误排除。
- **重试结果可审计且保持保守**：本地 `chatgpt_local.subscription` 现在记录 `refresh_attempts`、`retry_reason` 和 `retry_outcome`。二次探测若退化为认证失败，不会覆盖首轮的有效结果；两次都没有套餐或到期信息时仍保留 `unknown_plan`，不会仅凭历史支付链接或 `last_known_plan` 自动提升为可上传 Plus。
- **前端版本同步至 v2.7.2**：`frontend/src/app/AppShell.tsx` 侧栏版本更新，便于确认三个常驻实例已加载订阅探测容错逻辑。

### 测试 (Tests)
- **订阅补刷回归**：新增 `tests/test_chatgpt_local_status_refresh.py`，覆盖首轮套餐未知后二次恢复 Plus、付费套餐缺少到期时间后二次补齐、两次均缺失时的有界停止，以及套餐已确认或认证失效时不产生额外探测。

## [2.7.1] - 2026-07-23

### 安全 (Security)
- **Settings 密钥字段恢复默认遮罩**：`frontend/src/pages/Settings.tsx` 修正 Ant Design `Input.Password` 的受控可见状态，API Key、Token、密码、Cookie 和代理凭据等所有 `secret` 字段现在默认显示为遮罩，只在用户主动点击眼睛图标时临时显示；支付长链服务 API Key 不再随面板展开直接明文暴露。

### 测试 (Tests)
- 增加 Settings secret 可见状态合同，锁定 `showSecret=false` 对应遮罩状态，防止后续再次反转。

## [2.7.0] - 2026-07-23

### 新增 (Added)
- **支付长链服务支持运行时 API Key 配置**：`api/config.py` 与 `frontend/src/pages/Settings.tsx` 在 ChatGPT 设置中新增服务地址、API Key 和原位连接测试。配置通过现有 `config_store` 持久化并进入共享模板，主服务、Plus、Plus2 可共用同一稳定 HTTPS 地址和服务密钥；环境变量继续作为未配置时的兼容兜底。
- **接入版本化远程接口**：`services/chatgpt_core/long_link_payment_client.py` 默认调用 `/api/v1/payment-links/*` 并使用标准 `Authorization: Bearer`。首次请求仅在 v1 明确返回 404 时回退旧 `/api/internal/payment-links/*` 与 `X-Internal-API-Key`，后续请求固定已探测的协议版本，支持两个项目滚动升级。

### 安全 (Security)
- **服务凭据输入和错误回显收紧**：服务地址拒绝内嵌用户名密码、查询参数、片段和非 HTTP(S) 协议；API Key 限制控制字符和长度。连接测试只返回支付类型、国家、币种、并发和 profile hash 前缀，不回传 API Key 或完整上游配置；客户端错误会同步脱敏服务密钥和账号 Access Token。

### 测试 (Tests)
- 增加 v1 Bearer 请求、旧接口 404 单次回退、非 404 不降级、共享配置优先级、URL 校验、错误脱敏、配置持久化、连接测试响应和 Settings 前端合同回归；前端侧栏版本同步至 `v2.7.0`。

## [2.6.1] - 2026-07-22

### 优化 (Changed)
- **账号列表默认按最新注册排序**：`services/account_filters.py` 与 `frontend/src/pages/Accounts.tsx` 将无参数、首次进入、清空筛选和订阅到期次级排序统一改为 `accounts.created_at DESC, accounts.id DESC`，在服务端分页前稳定排序，保证最新注册账号优先出现在第一页；显式选择“注册最早”及旧 API 的 `sort_order=asc` 继续有效，现有 `(platform, created_at, id)` 索引可直接反向扫描。
- **筛选组合迁移到 v3 排序语义**：`api/accounts.py` 将筛选组合结构升级为 v3，旧版本自动保存或缺失的注册正序默认值在读取时一次性迁移为最新优先；v3 之后用户显式保存的“注册最早”保持不变，避免主列表、内置组合和历史自定义组合出现相反默认顺序。
- **支付链接复制增加行级成功反馈**：`frontend/src/pages/Accounts.tsx` 在支付链接列表复用已有 Clipboard API 与 textarea 降级复制链路；复制成功后当前账号的图标按钮切换为橙色确认状态和勾选图标，失败不变色，新支付 URL 自动恢复未复制状态。该反馈仅保存在当前页面会话，不写账号“已使用”字段，也不持久化支付 URL。
- **前端版本同步至 v2.6.1**：侧栏版本号随注册排序和支付链接复制反馈更新，便于从 live 页面确认三实例静态资源版本。

### 测试 (Tests)
- 更新账号默认倒序、同时间 ID 兜底、订阅到期次级排序、分页顺序和索引使用回归；增加筛选组合 v2 到 v3 单次迁移、v3 显式正序保留，以及支付链接复制成功状态、可访问标签和橙色视觉反馈合同测试。

## [2.6.0] - 2026-07-22

### 优化 (Changed)
- **账号列表支持注册时间与订阅到期多字段排序**：`services/account_filters.py` 将账号排序合同扩展为逗号分隔的有序字段列表，默认使用 `accounts.created_at ASC, accounts.id ASC`，保证最早注册账号出现在第一页；选择订阅到期排序时自动以注册时间作为次级排序，同一到期时间内可继续按注册时间正序或逆序排列。`api/accounts.py` 在分页前统一应用服务端排序，并继续兼容旧的单字段 `sort_by/sort_order` 请求。
- **注册时间排序增加数据库索引**：`core/db.py` 新增并在 `init_db()` 中兜底创建 `idx_accounts_platform_created_at_id(platform, created_at, id)`；排序 SQL 按 `created_at` 原生顺序读取（该列为非空），确保 SQLite 实际使用覆盖索引，避免账号列表按平台和注册时间分页时反复构建临时排序表。
- **本地状态同步收敛为单一入口**：`frontend/src/pages/Accounts.tsx` 移除账号页内“配置代理与延时”的第二个同步入口及临时配置弹窗，页面只保留一个“同步本地状态”动作；任务请求不再携带页面级直连/代理选择，统一读取全局 `task_proxy_mode` 及代理模板、国家和失败切换配置。
- **本地状态同步参数纳入全局配置**：`frontend/src/pages/Settings.tsx` 增加同步并发、独立出口 IP、账号间最小/最大延时配置；`api/config.py` 注册对应配置键，`api/tasks.py` 在任务创建时从全局配置冻结参数，同时保留 API 显式参数的兼容覆盖能力。
- **本地状态同步全局参数校验收紧**：Settings 加载/保存时将独立出口开关归一化为真实布尔值，并限制并发 `1-10`、延时 `0-3600` 秒且最大延时不小于最小延时；配置 API 与任务入口同步拒绝 `直连 + 独立出口`、不可切换的单指定代理组合，独立出口要求不再因并发为 1 而被静默忽略。
- **前端版本同步至 v2.6.0**：侧栏版本号随账号排序与本地状态同步全局配置发布同步更新，便于从 live 页面确认静态资源版本。

### 测试 (Tests)
- 增加账号默认/逆序/多字段排序、空到期值置后、旧排序参数兼容、索引迁移、全局本地状态参数回退及账号页单入口合同测试。

## [2.5.1] - 2026-07-21

### 新增 (Added)
- **支付链接扫描按支付类型自动归类并接入 UPI**：`services/chatgpt_core/pix_payment_link_cleanup.py` 将原 PIX 专用扫描扩展为 PIX/UPI QR 链路统一扫描，依据 `link_type`、`payment_method_type` 和 Stripe 指令 URL 自动识别支付类型；新增混合扫描接口 `/api/tasks/chatgpt/payment-links/scan`、通用预览/清理任务接口，以及保留旧 PIX 路由的 UPI 兼容别名。账号页扫描面板现在分别展示 PIX、UPI 的有效、已支付、过期和支付已取消分桶，清理操作带支付类型参数，不会跨通道误删。
- **UPI QR 过期清理**：UPI 过期判断只接受 Stripe `setup_intent.next_action.upi_handle_redirect_or_display_qr_code.qr_code.expires_at`（及其指令页等价的 QR 到期值），按 5 分钟 QR 有效期展示和清理；清理后写入独立的 `upi_expired_cleaned`、`upi_paid_cleaned`、`upi_cancelled_cleaned` 墓碑，保留生成/到期/清理时间和历史记录。

### 优化 (Changed)
- **UPI 到期字段贯穿缓存、任务历史和同步**：`payment_link_cache.py`、`long_link_payment_client.py`、`api/tasks.py` 与 `services/long_link_history_sync.py` 统一保存 `link_expires_at` 与 `link_expiry_source=upi_qr_code`；当旧账号当前缓存缺少到期字段时，扫描器按账号/URL 从 `payment_link_generations.result_json` 回填，不猜测 Checkout Session 到期时间。
- **账号列表类型索引同步升级**：`services/account_filters.py` 将 UPI 纳入当前链接类型筛选，支持从支付方式字段或 `/upi/instructions/` URL 自动识别，并升级派生索引版本，使现有 Plus 账号的 UPI 不再显示为“其他支付链接”。账号详情和历史卡片同步显示 UPI 到期倒计时及清理状态。

### 修复 (Fixed)
- **防止 UPI 链接被误当作 hosted/PIX**：当上游同时返回通用 `link_type=hosted` 和 `payment_method_type=upi` 时，QR 支付方式优先；UPI QR 到期值优先于旧的 Checkout Session 值，避免 5 分钟二维码被错误延长。
- **清理与生成防重边界统一**：通用清理墓碑加入支付链接生成跳过、防复活和列表“已成功提取”判断；旧 PIX 清理接口、任务源和前端合同保持兼容。

### 测试 (Tests)
- 新增 UPI QR HTML 安全解析、嵌套 `qr_code.expires_at` 优先级、缓存自动归类、UPI 过期清理、历史同步和账号列表类型筛选回归；定向后端回归 82 项通过，前端 `npm run build` 通过。

## [2.5.0] - 2026-07-20

### 优化 (Changed)
- **HME Helper 平台注册兼容链路**：`core/base_mailbox.py` 的 ChatGPT HME prepare 请求现在显式传递 `platform=chatgpt`，保存 Helper 返回的 `registration_id`、`logical_address_id`、`physical_alias_id`、`lease_id`、`lease_state`、`physical_hme`、`logical_type`、`tag`、`tag_namespace` 和 `tag_slot`。finalize 以 registration/lease 标识为主，同时保留 `chatgpt_account_email` 兼容投影，并在 authoritative 响应后刷新 mailbox state。
- **随机/legacy tag 收码边界**：tag 地址（包括随机六位 tag 与历史 `+gptN`）只依据完整逻辑地址的可信 transport header 匹配；ChatGPT 不回退物理 HME。base 地址不再从正文宽松命中，多转发箱在多箱扫描时使用 mailbox-scoped message ID，避免相同 provider ID 串箱。
- **恢复与本地旁路隔离**：`services/chatgpt_core/mailbox_state.py`、`restored_email_service.py` 保留并恢复完整 Helper 身份字段；SQLite HME alias 恢复查询限定 ChatGPT association，不以本地全局邮箱记录阻塞其它平台注册。

### 修复 (Fixed)
- **无效 Helper prepare 自动释放租约**：Helper 返回已领取但无有效逻辑邮箱时，auto-gpt 会带已知 registration/logical/physical ID 立即执行 early finalize，再报告 prepare 异常，避免无主 lease 等待 TTL。

## [2.4.0] - 2026-07-19

### 新增 (Added)
- **新增 ChatGPT Team 优惠码 checkout 长链**：账号页“支付链接生成”增加 Plus / Team 分段模式。Team 支持 Workspace 名称、月付/年付、席位数量、优惠码和取消跳转 URL；字段留空时继承 `/opt/openai-pay-long-link` 管理页默认值，提交前通过 `POST /api/tasks/chatgpt/payment-links/profile` 预览并冻结实际生效配置。
- **支付链接历史增加产品与变体身份**：`core/db.py` 的 `payment_link_generations` 新增 `generation_kind`、`variant_key` 字段和索引；账号当前链接、详情历史及任务元数据展示 PLUS / TEAM、Workspace、周期和席位，优惠码只保存摘要，不在浏览器或任务日志中回显明文。

### 优化 (Changed)
- **Auto-GPT 继续由 long-link 统一提供底层提链能力**：`services/chatgpt_core/long_link_payment_client.py` 对 Team 使用带 `profileOverrides` 的 profile 预览和批量提交，Plus 继续保持旧 GET/批量请求结构。Team 只消费 long-link 返回的 hosted checkout URL，不在 Auto-GPT 内复制 ChatGPT checkout、Stripe init、Provider 或 Approve 实现。
- **Plus 与 Team 配置变体完全隔离**：`services/chatgpt_core/payment_link_cache.py` 新增 `chatgpt_payment_link_variants` 变体缓存，以产品、国家、币种、profile hash、Workspace、周期、席位、优惠码摘要和取消 URL 计算稳定键；相同账号的 Plus、不同 Team Workspace 或不同优惠码不会互相覆盖或错误触发跳过，旧 `chatgpt_last_payment_link` 仅保留为兼容当前指针。
- **批任务按实际配置冻结后再提交**：`api/tasks.py` 先读取 long-link 生效 profile，再写入最终 `generation_kind` / `variant_key`，完成全部账号预提交后统一轮询远端批次。Team 不读取或覆盖旧 `chatgpt_paypal_url`，Plus/PayPal 历史兼容行为保持不变。

### 修复 (Fixed)
- **严格区分受支持计划与历史产品标记**：支付链接入口只允许 Plus 与 checkout-only Team；`business`、`enterprise` 及缺少 `plan=team` 的孤立 Team 参数在账号扫描前拒绝。缺少 `team_checkout` 身份或 Workspace 的旧 Team/Business 缓存不会被重新解释为本次 Team checkout。
- **Team 配置重试与安全展示修正**：Team profile 读取失败后的“重新读取”继续携带当前表单参数，不再误读 Plus 配置；浏览器 profile 只显示优惠码是否配置，不返回稳定 digest，并对异常席位/并发值安全降级。

### 测试 (Tests)
- **Team 专项与兼容回归**：新增 `tests/test_team_payment_links.py`，扩展 long-link client、退役能力和账号页契约测试，覆盖部分参数继承、非法输入、变体键隔离、Team/Plus 缓存、单账号 action、历史持久化和 profile/batch override 一致性。定向回归 `34 passed`（另有 8 个 subtests），排除一个未改动的相邻 custom-email 默认值既有失败后其余后端回归 `966 passed`（另有 17 个 subtests）；容器依赖环境下管理员认证与 OpenAPI 契约 `22 passed`，前端 `npm run build` 通过。

## [2.3.8] - 2026-07-18

### 优化 (Changed)
- **支付链接筛选拆分为当前状态与历史记录两个维度**：`frontend/src/pages/Accounts.tsx` 与 `frontend/src/features/accounts/hooks/useAccountsQuery.ts` 将支付链接筛选拆为“当前链接类型”（PIX、PayPal、ChatGPT 结账、其他、当前无链接）和“提取记录”（已成功提取、尚未成功提取）。提取记录保留二元 OR 语义：只选一项筛对应状态，两项同时选择自动归一为不筛选；筛选组合、移动端控件、任务范围和 API 查询参数统一携带 `payment_link_generated`。
- **历史成功证据进入账号列表派生索引**：`services/account_filters.py` 与 `core/db.py` 新增 `account_list_state.payment_link_generated` 及索引。当前合法 HTTP(S) 链接、三类 PIX 清理墓碑（`expired_cleaned`、`paid_cleaned`、`cancelled_cleaned`）、成功生成历史和合法旧版 PayPal 链接均归入“已成功提取”；当前链接平台仍只表示实际可打开的当前 URL，清理后的账号显示为“当前无链接”。普通列表和详情接口只对缺失/过期页状态做批量刷新，避免分页序列化触发逐账号查询。
- **清理墓碑保留可追溯元数据**：列表摘要保留链接类型、生成时间、清理时间和清理原因等非 URL 信息；残留 URL 不会被展示或通过旧版 PayPal 字段复活，前端清理行不再提供复制/打开操作。

### 修复 (Fixed)
- **普通支付链接生成默认防重复**：`api/tasks.py` 在任务解析、worker 准备阶段和远端批量提交前复核当前合法链接、清理墓碑、成功历史和旧版 PayPal URL。普通模式遇到任一成功证据都会跳过；失败/中断或非法 URL 仍可重试。
- **强制重新生成保留明确边界**：`force_refresh=true` 可绕过普通成功历史、过期/取消清理和非付款终态旧链接，但账号失效、已订阅、明确 `paid`/`already_paid`/`paid_cleaned` 以及新鲜的 `submitting`/`queued`/`running` 任务始终阻断。历史成功只认 `payment_link_generations.status=succeeded` 且记录 URL 合法，生成成功状态不会误判为已付款。
- **并发任务采用 SQLite 原子 claim**：最终提交前使用 `BEGIN IMMEDIATE` 检查并写入本任务的 `submitting` provisional 记录，只有首个持锁任务可以调用远端；异常任务通过失败/中断终态或 `max(30 分钟, OPENAI_PAY_LONG_LINK_JOB_TIMEOUT_SECONDS + 5 分钟)` TTL 释放，避免重复提交和永久卡死。
- **删除账号不污染新账号历史**：`core/db.py` 在初始化时创建账号删除触发器，账号删除会同步清理 `payment_link_generations`，防止 SQLite 复用主键后新账号继承旧支付链接成功记录。

### 测试 (Tests)
- **支付筛选与任务回归**：`tests/test_account_filter_presets.py`、`tests/test_accounts_payment_link_filter_ui.py`、`tests/test_account_filters.py`、`tests/test_filtered_task_scope.py`、`tests/test_payment_link_generation_history.py`、`tests/test_payment_link_task_guard.py`、`tests/test_pix_payment_link_cleanup.py`、`tests/test_register_task_controls.py` 等定向回归共 `128 passed`，覆盖清理后历史筛选、非法/超长 URL、旧版 PayPal、强制边界、在途 TTL、批量历史加载、账号删除后 ID 复用和任务范围一致性。
- **前端与构建验证**：`frontend` 的 `npm run build` 和 `npm test`（9 项）通过；全量后端测试在当前工作站缺少 `argon2` 依赖，排除受影响的认证测试后为 `961 passed`，剩余失败为既有 custom-email 代理默认值契约及同一缺失依赖引起的导入失败。

## [2.3.7] - 2026-07-18

### 优化 (Changed)
- **提交筛选消除同名歧义**：`frontend/src/pages/Accounts.tsx` 将原“是否已提交 → 已提交/未提交”明确改为“提交记录 → 有提交记录/无提交记录”，与同一菜单中的当前状态“未提交、提交中、已完成、提交失败、待人工复核”等区分。筛选仍使用既有 `has_submitted=true/false` 合同，旧筛选组合、任务范围和 API 参数无需迁移；筛选组合摘要与编辑器同步使用新文案。前端侧栏版本同步至 `v2.3.7`。

### 测试 (Tests)
- **筛选标签合同回归**：扩展 `tests/test_accounts_submission_state_ui.py` 与 `test_accounts_integration_upload_filter_ui.py`，锁定提交历史维度不再复用当前状态的“已提交/未提交”标签，并继续验证所有筛选组合选项使用统一中文标签。

## [2.3.6] - 2026-07-18

### 优化 (Changed)
- **Sub2API/OAIPay 筛选收敛为二元上传状态**：`frontend/src/pages/Accounts.tsx` 的两列筛选统一只显示“已上传”和“未上传”。`services/account_filters.py` 以 `uploaded=true`、远端状态 `exists/uploaded` 或最近上传记录 `last_upload.status=success` 作为明确上传证据，其余远端未发现、多候选、不可达、历史删除和未同步状态统一归入“未上传”；原始技术状态继续保留在同步记录、上传记录和任务日志中用于防重复与排障。
- **旧筛选条件无感迁移**：账号列表请求和筛选组合把历史 `exists` 映射为“已上传”，把 `unknown/not_found/cross_workspace_only/deleted_exact_match/ambiguous/unreachable` 映射为“未上传”；同时选择两类时按不限处理。`account_list_state` 派生版本升级为 `integration-upload-state-v1`，三个实例会从各自账号数据重新生成二元索引，不修改原始同步证据。
- **常用筛选组合同步简化**：`api/accounts.py` 的 OAIPay 待补传组合改用单一“未上传”条件，“Sub2API 已有但 OAIPay 未传”同时覆盖上传成功记录与远端探测已存在记录；移除已经失去独立筛选语义的 OAIPay 多候选/不可达内置组合，避免继续暴露废弃技术枚举。
- **其他列筛选标签统一**：筛选组合编辑弹窗现在统一通过 `toSelectOptions()` 把业务状态、认证材料、手机号、支付链接、订阅、认证状态、提交状态等 `{text,value}` 配置转换为 Ant Design 所需的 `{label,value}`，避免弹窗显示原始英文值；桌面表头、移动端和组合编辑器使用同一套中文标签。账号规范化同时优先保留紧凑列表接口返回的 Sub2API、OAIPay 与 CLIProxyAPI 同步摘要，避免安全裁剪后的 `extra` 空对象覆盖上传记录。前端侧栏版本同步至 `v2.3.6`。

### 测试 (Tests)
- **二元状态与筛选合同回归**：扩展 `tests/test_account_filters.py`、`test_account_filter_presets.py`、`test_filtered_task_scope.py` 和 `test_integrations_backfill_scope.py`，覆盖上传标记、远端存在、成功历史三类正向证据，未上传归类、旧枚举迁移、SQL 索引与 Python 回退一致性及冻结任务范围；新增 `test_accounts_integration_upload_filter_ui.py` 锁定两项筛选和全部组合编辑标签转换。

## [2.3.5] - 2026-07-18

### 新增 (Added)
- **PIX 链接改为 Stripe 实时状态扫描**：`services/chatgpt_core/pix_payment_link_cleanup.py` 现在并发 GET 当前 `payments.stripe.com/qr/instructions/` 链接，从 HTML 的 `meta#payload[data-message]` 解码 Stripe `qr_instructions` 响应，并按 `intent_state=succeeded`、`canceled/cancelled`、其他非终态结合 `server_timestamp` 与链接到期时间，实时归类为已支付、支付已取消、过期或有效。扫描接口、清理预览和后台清理统一复用同一分类规则，不再把滞后的本地支付标记当作首选真相。
- **实时查询覆盖率可见**：扫描报告新增 `direct_scan_attempted_links`、`direct_scan_success_links`、`direct_scan_fallback_links` 与脱敏状态计数；`PixLinkScanModal.tsx` 展示 Stripe 实时查询成功数/总数，并在请求失败、非 Stripe URL 或响应无法验证时明确显示本地记录兜底数量。前端侧栏版本同步至 `v2.3.5`。

### 优化 (Changed)
- **实时探测增加并发、超时和响应边界**：仅允许 HTTPS、精确主机 `payments.stripe.com` 和 `/qr/instructions/` 路径，重定向仍需通过同一白名单；默认并发 12、上限 32，连接/读取超时分别为 5/10 秒，响应体限制 256 KiB。解析结果只保留 `intent_state` 与 `server_timestamp`，不会把原始链接、HTML、Base64 payload、`client_secret` 或 publishable key 写入报告和任务日志。
- **联网与 SQLite 写事务彻底分离**：实际清理先加载候选并释放读取事务，再完成 Stripe 并发扫描和校验备份，最后进入 `BEGIN IMMEDIATE`。事务内只复用 `(account_id, current_url)` 完全匹配的实时结果；扫描后链接发生变化时安全跳过，避免持锁联网、误删新链接或扩大数据库阻塞时间。

### 测试 (Tests)
- **Stripe 实时分类与事务边界回归**：扩展 `tests/test_pix_payment_link_cleanup.py`，覆盖 Base64 HTML 解析、实时 `succeeded/canceled/requires_action` 覆盖滞后本地状态、非终态到期判断、请求失败本地兜底、非 Stripe URL 零网络访问、异常响应不泄露敏感字段、写事务内不联网及并发换链跳过清理；前端合同测试同步锁定实时覆盖率与兜底提示。

## [2.3.4] - 2026-07-18

### 新增 (Added)
- **PIX 链接统一扫描面板**：支付链接生成菜单新增“扫描 PIX 链接”入口，调用 `GET /api/tasks/chatgpt/payment-links/pix-cleanup/scan` 后集中展示当前实例的总 PIX 链接、有效、已支付、过期和支付已取消数量。`frontend/src/features/accounts/components/PixLinkScanModal.tsx` 使用紧凑状态表呈现结果，并支持原位重新扫描；有效链接只显示数量，不提供清理按钮。
- **按扫描分类原位清理**：扫描面板在已支付、过期和支付已取消三行分别提供独立清理按钮，继续复用原有服务端复核、二次确认、后台任务、SQLite 校验备份、事务清理和任务日志链路。按钮数量为零时禁用，执行前会重新读取当前实例计数，不使用可能过期的前端扫描结果直接删除。

### 优化 (Changed)
- **扫描统计改为互斥状态桶**：`services/chatgpt_core/pix_payment_link_cleanup.py` 按“已支付 → 支付已取消 → 过期 → 有效/安全保留”优先级把每条当前 PIX 链接只归入一个类别，保证四类数量之和等于总数，并确保清理任务处理的集合与扫描面板对应行完全一致。缺少有效过期时间且没有明确支付终态的链接归入安全保留，同时通过 `valid_missing_expiry_links` 单独提示，避免误删。
- **支付链接菜单收敛**：移除菜单中三个平铺的危险清理入口，改为单一扫描入口后在结果位置选择清理类别，降低误点风险并让运营先看到全量状态分布。`frontend/src/app/AppShell.tsx` 侧栏版本同步至 `v2.3.4`。
- **操作约定与三实例常驻拓扑对齐**：`AGENTS.md` 移除已经失效的主服务 standby 规则，明确 `auto-gpt` (`8000`)、`auto-gpt-plus` (`8001`) 与 `auto-plus2` (`8003`) 均为常驻业务实例；常规发布和热更新必须覆盖三者，发布完成后禁止 Agent 再手动停止任一业务实例，并将推荐 smoke 同步扩展为三个实例。

### 修复 (Fixed)
- **扫描面板打开前主动收起操作菜单**：`AccountsToolbar.tsx` 将支付链接和更多操作下拉改为受控开关，选择“扫描 PIX 链接”时先关闭菜单再打开面板，避免桌面端或移动端的菜单浮层滞留并遮挡状态表及清理按钮。

### 测试 (Tests)
- **互斥分类与清理边界回归**：扩展 `tests/test_pix_payment_link_cleanup.py`，覆盖同一链接同时满足终态和过期条件时只进入优先级更高的分类、四类之和等于总数、过期清理不会跨类删除已支付或支付已取消链接，以及缺少时间记录安全归入保留项。
- **扫描 API 与前端合同回归**：扩展任务路由和账号页合同测试，锁定专用 GET 扫描接口、统一扫描入口、四类状态行、有效行无删除按钮、终态行原位清理、服务端二次预览与后台任务入口。

## [2.3.3] - 2026-07-18

### 新增 (Added)
- **PIX 链接清理扩展为三种独立动作**：账号页“支付链接生成”下拉菜单在原“清理过期 PIX 链接”之外，新增“清理已支付 PIX 链接”和“清理支付已取消 PIX 链接”。三个动作分别调用带 `cleanup_mode=expired|paid|cancelled` 的预览与后台任务接口，继续执行当前实例扫描、确认弹窗、任务日志、校验备份、事务清理和账号列表索引刷新，不受当前分页、勾选或筛选范围影响。
- **终态清理审计标记**：`services/chatgpt_core/payment_link_cache.py` 与 `pix_payment_link_cleanup.py` 新增 `paid_cleaned`、`cancelled_cleaned` tombstone。清理只移除当前 PIX URL 及完全相同的 `cashier_url`，保留原链接状态、清理模式、生成/到期时间和清理截止点；账号状态、支付生成历史、PIX CDK、订单提交结果及任务日志保持不变。

### 优化 (Changed)
- **已支付识别采用链接级组合证据**：清理服务只接受当前链接自身的 `paid/already_paid`，或“当前链接已标记 `pix_submitted` + 对应 `user_link` PIX 订单明确为 paid”的组合；不会仅凭账号已订阅、历史订单 paid 或 `auto_extract` 结果删除后来生成的新链接。支付标记时间早于当前链接生成时间时会安全排除。
- **支付取消与普通失败严格分离**：取消清理识别链接自身的 `cancelled/canceled/payment_cancelled/payment_canceled`，以及 `user_link` PIX 订单返回明确“PIX 支付已取消”证据的记录。普通上游失败、超时、结果未知和人工复核状态继续保留，避免把可重试或未决链接误删。
- **扩展 attsms 手机绑定收码格式**：`services/chatgpt_core/phone_service.py` 现在支持解析 `attsms.com` 这类纯文本响应，例如 `【OpenAI/ChatGPT】暂无短信，到期时间：2026-7-31 13:04`。轮询阶段继续将“暂无短信”视为等待，并记录纯文本到期时间；收到带“验证码”的后续响应时可直接提取验证码完成手机号绑定。
- **手机号池到期探测兼容纯文本 API**：`services/chatgpt_core/phone_pool_repository.py` 的有效期探测不再强制要求 JSON，能够从纯文本 `到期时间` 字段写入 `api_expired_date`，避免 attsms 号码导入后被误标为探测失败。

### 修复 (Fixed)
- **所有清理终态都阻止旧历史复活链接**：`services/long_link_history_sync.py` 统一识别三种 PIX 清理 tombstone，旧 long-link 成功记录仍进入 `payment_link_generations` 审计历史，但不会重新写回已清理 URL；只有清理截止点之后真正生成的新链接可以覆盖 tombstone。
- **前端终态文案与发布版本对齐**：账号详情将 paid、payment cancelled、PIX 已提交及三种清理状态显示为明确中文标签；`frontend/src/app/AppShell.tsx` 侧栏版本同步至 `v2.3.3`，用于确认三个实例已经加载本次清理能力和此前 attsms 兼容更新。

### 测试 (Tests)
- 新增 attsms 纯文本轮询和手机号池有效期探测回归用例，覆盖“暂无短信”等待、验证码提取、`2026-7-31 13:04` 到期时间保存及非 JSON 响应。
- **PIX 清理模式与证据边界回归**：扩展 `tests/test_pix_payment_link_cleanup.py`、`test_pix_payment_link_cleanup_task.py`、`test_long_link_history_sync.py` 和前端合同测试，覆盖三种模式参数冻结、paid 组合证据、取消文案证据、旧支付结果不误删新链接、普通失败隔离、不同 tombstone 防回填、零候选任务及后台日志持久化。

## [2.3.2] - 2026-07-17

### 修复 (Fixed)
- **历史日志脱敏验证改为幂等**：`scripts/redact-nginx-query-tokens.py` 不再把已经写成 `access_token=<redacted>` 的安全占位符继续识别为待脱敏凭据。重复执行 `--apply` 不会无意义重写压缩日志，后续 dry-run 可以可靠返回 `files_with_matches=0 redacted_values=0`，区分真实残留令牌与已经完成的脱敏记录。
- **前端版本同步至 v2.3.2**：`frontend/src/app/AppShell.tsx` 更新侧栏版本，用于确认三实例已经加载包含最终日志脱敏修复的发布产物。

### 测试 (Tests)
- **脱敏重复执行回归**：扩展 `tests/test_redact_nginx_query_tokens.py`，在普通与 gzip 日志完成脱敏后立即再次扫描，锁定原始令牌已消失且安全占位符不会产生误报。

## [2.3.1] - 2026-07-17

### 优化 (Changed)
- **统一镜像发布只构建一次**：`docker-compose.multi.yml` 仅保留 `auto-gpt` 作为 `auto-gpt:latest` 的规范构建服务，Plus、Plus2 与 `phone-api-relay` 继续只引用该共享镜像。`deploy.sh --mode=multi` 现在显式执行 `compose_multi build auto-gpt`，随后使用 `up --no-build` 启动全部活动服务，避免 Compose 为相同 Dockerfile 和相同镜像标签创建两次独立导出任务。

### 修复 (Fixed)
- **修复多实例发布并行导出耗尽磁盘**：此前 `auto-gpt` 与 `auto-gpt-plus` 同时声明相同 `build`，无服务过滤的 Compose build 会并行解包两份约 4 GiB rootfs，并可能在 Chromium 层导出时触发 `no space left on device`。现在发布路径从编排文件和脚本两层消除重复构建，并禁止 `up` 隐式补建，保证三个业务实例原子切换到同一已完成镜像。
- **前端版本同步至 v2.3.1**：`frontend/src/app/AppShell.tsx` 更新侧栏版本，便于确认浏览器已加载修复后的统一镜像发布产物。

### 测试 (Tests)
- **单构建拓扑合同**：扩展 `tests/test_deploy_topology_contract.py`，锁定四个活动服务共用同一镜像、Compose 仅存在一个规范 `build`、multi 发布只构建 `auto-gpt` 且启动阶段必须使用 `--no-build`，防止重复导出和磁盘峰值问题回归。

## [2.3.0] - 2026-07-17

### 新增 (Added)
- **持久管理员会话与认证审计**：`core/db.py` 新增 `admin_auth_sessions`、`admin_auth_audit`、`admin_auth_throttles`，`api/auth.py` 为每个管理员 JWT 创建服务端 `jti` 会话并提供受保护的 `/api/auth/sessions`、`/api/auth/audit` 与全会话撤销接口。审计仅保存实例、时间、真实客户端 IP、User-Agent、阶段、结果、原因和会话标识，不保存密码、TOTP 或 JWT。
- **统一 fetch SSE 客户端**：新增 `frontend/src/lib/eventStream.ts`，使用带 `Authorization: Bearer` 的 `fetch` 流式读取 SSE，支持 CRLF/分块边界、多行 `data`、心跳、Abort、终态主动断流与指数退避重连；自动流水线和任务日志面板不再依赖无法设置请求头的原生 `EventSource`。
- **可回滚的边缘安全配置与运维工具**：新增 `ops/nginx/` 三域名规范配置、`scripts/install-auto-gpt-nginx-security.sh`、`scripts/rotate-admin-auth-secrets.py`、`scripts/redact-nginx-query-tokens.py` 和 `scripts/prepare-legacy-auth-rollback.py`，分别用于 nginx 原子安装/失败回滚、三实例 JWT 基础密钥无回显轮换、历史日志查询串令牌脱敏，以及回滚到 v2.3.0 之前镜像时仅恢复旧版可识别的管理员密码/TOTP 而不覆盖期间新增的业务数据；兼容回滚仍会生成全新 JWT 密钥，避免旧会话复活。

### 优化 (Changed)
- **密码与会话强度升级**：管理员密码改用 Argon2id；旧无盐 SHA-256 在首次成功验证后原地迁移，不强制现有管理员立即重置。新设或修改密码最低 12 位，JWT 默认有效期由 7 天缩短为 12 小时；真实 logout、密码变更、2FA 变更和密钥轮换都会撤销对应服务端会话。
- **凭据变更改为 step-up 验证**：首次 `/api/auth/setup` 只允许未初始化实例，并支持 `APP_AUTH_BOOTSTRAP_TOKEN` / `X-Auth-Bootstrap-Token`；未配置令牌时只接受本机或显式可信容器网关。修改密码必须验证当前密码；启用 2FA 必须验证当前密码和新验证码；停用 2FA 必须同时验证当前密码和当前 TOTP。前端安全面板同步展示实例身份、一次性初始化流程和二次验证表单。
- **管理端口与来源日志收口**：`docker-compose.multi.yml` 将 `8000/8001/8003`、`8317/8318/8320` 全部改为仅绑定 `127.0.0.1`，继续保留三个 solver 端口的本机绑定。三个域名使用独立 JSON access/error log，访问日志仅记录 `$uri` 而不记录查询串；`auto-gpt.cccy.me` 仅对 Cloudflare 官方代理网段信任 `CF-Connecting-IP`，Plus 与 Plus2 继续使用直连来源 IP。 安装脚本会把主站从历史聚合配置 `cccy-apps.conf` 原子迁移到实际被 nginx 顶层加载的独立 vhost，并在 reload 前通过 `nginx -T` 断言唯一生效来源，避免安全配置写入未加载的 `conf.d/managed/` 后形成假上线。

### 修复 (Fixed)
- **修复三实例认证横向复用**：JWT 签名密钥现在通过 `APP_INSTANCE_ID` 派生，并强制校验实例独立的 `iss`、`aud`、`jti` 和 `auth_version`。即使底层配置被误复制，主实例、Plus、Plus2 的令牌也不能跨实例验签，主实例未启用 TOTP 不再成为 Plus 系列 2FA 的绕过入口。
- **修复认证缺失时 fail-open**：`main.py` 在管理员密码缺失时对普通受保护 API 返回 `503`，不再直接放行；`/api/auth/disable` 被明确禁止，`/api/auth/setup` 不能再被已有 Bearer 用来绕过当前密码覆盖凭据，关闭“清空认证后重新抢占初始化”的接管链。
- **修复 JWT 进入 URL 与 nginx 日志**：删除后端 query `access_token` 兼容入口，前端所有管理员 SSE 和导出请求统一走 Authorization header；nginx 同时拒绝旧式 `access_token` 查询参数，避免新旧浏览器或扫描器再次把完整 JWT 写入 access/error log。

### 安全 (Security)
- **登录限速、冷却与可信代理边界**：密码、TOTP、bootstrap 和凭据 step-up 按实例、真实 IP、阶段持久计数，SQLite/PostgreSQL 使用原子 UPSERT 防止多 worker 丢计数，达到阈值返回 `429 + Retry-After`。应用仅在直连 peer 命中 `APP_TRUSTED_PROXY_CIDRS` 时采纳转发 IP；nginx 会清除调用方自带的 Cloudflare 身份头。 三个 vhost 对 `/api/auth/` 单独限制请求体为 16 KiB，Pydantic 同步限制密码、临时令牌、TOTP 和密钥字段长度；业务上传接口仍保留 200 MiB 上限，避免认证入口被大请求并发占满磁盘或内存。 单进程运行态还会原子消费密码阶段的临时 2FA challenge，并串行化首次 bootstrap 初始化，阻止同一临时令牌并发兑换多份会话或两个初始化请求互相覆盖。
- **实例密钥轮换不触碰用户凭据**：发布流程会为主实例、Plus、Plus2 生成不同 JWT 基础密钥并递增 `auth_version`，使发布前所有令牌立即失效；现有管理员密码和 TOTP 不会被脚本静默改写，仍可由管理员在各实例安全面板逐一轮换。

### 测试 (Tests)
- **认证、SSE 与基础设施合同回归**：新增 `tests/test_admin_auth_security.py`、`tests/test_auth_infra_contract.py`、`tests/test_rotate_admin_auth_secrets.py`、`tests/test_redact_nginx_query_tokens.py`、`tests/test_prepare_legacy_auth_rollback.py` 及 `frontend/tests/`，覆盖旧哈希迁移、跨实例拒绝、真实注销、会话撤销、bootstrap、公网初始化拦截、2FA step-up、并发限速、审计字段、URL 令牌清除、SSE 解析、401 会话清理、回环端口和 nginx 无查询串日志合同。 前端测试通过仓库既有 TypeScript 编译器在临时目录转译目标模块，再使用 Node 20 原生 `node --test` 运行，与 Dockerfile 的构建基线一致且不新增运行器依赖，不再依赖宿主 Node 22 的实验参数。

## [2.2.16] - 2026-07-17

### 优化 (Changed)
- **主实例恢复为常驻发布拓扑**：`docker-compose.multi.yml` 移除 `auto-gpt` 的 `standby` profile，`deploy.sh` 将主实例加入 `ACTIVE_SERVICES`；后续 multi 发布会与 Plus、Plus2 一并重建并保持运行，不再在发布结尾主动执行 `docker stop auto-gpt`。
- **三个业务实例发布路径对齐**：hot 发布现在依次同步 `auto-gpt`、`auto-gpt-plus` 和 `auto-plus2`，multi/hot 发布后的 smoke 同时验证三个实例的 `/api/health` 与首页，避免只验证 Plus 系列而遗漏公网主站。

### 修复 (Fixed)
- **修复主站发布后 502**：恢复 `127.0.0.1:8000` 的持久监听，使 nginx 的 `auto-gpt.cccy.me` 上游与真实运行拓扑重新一致；主实例不再因历史 standby 规则收到人为 `SIGTERM`，并保留独立 `/opt/auto-gpt/data` 运行数据边界。

### 测试 (Tests)
- **发布拓扑合同与在线烟测**：新增 `tests/test_deploy_topology_contract.py`，锁定主实例无 profile、四个活动服务、三个实例 hot 同步及 8000/8001/8003 健康检查；发布前执行 shell 语法、Compose 配置、定向 pytest 与前端生产构建，发布后验证三个本地实例及 `auto-gpt.cccy.me` 公网入口。

## [2.2.15] - 2026-07-17

### 新增 (Added)
- **号段部分可用状态**：`services/chatgpt_core/phone_pool_repository.py` 的四位号段统计新增 `partial` 分组，明确展示同段内同时存在可用、拒绝或临时受限单号的真实混合状态。手机号池页与账号页绑定面板同步展示“部分可用”，并分别列出实际可用单号数、拒绝记录数和剩余账号容量。

### 优化 (Changed)
- **号段只作为相关性统计**：普通绑定和限定号段绑定的资格统一按具体号码判断，只接收自身为 `active`、未冷却、未绑满且带有效 API 的号码。限定号段模式仍严格限制在运营所选的前四位范围内，但同段其他号码的成功或失败不再覆盖当前号码状态；账号页按真实单号数及是否允许复用计算可分配账号容量。
- **抽样结果改为样本口径**：`api/tasks.py` 与 `PhoneBindingResultsTable.tsx` 将号段抽样结果统一为“成功样本 / 失败样本 / 样本混合 / 未测试”，并保留旧字段别名供历史任务详情兼容读取。抽样日志和复制按钮不再把一两个号码的结果描述成整个号段确定可用或不可用。

### 修复 (Fixed)
- **阻止单号结果污染整段库存**：`record_prefix_unavailable()` 保留兼容入口，但只回写本次真实测试号码；绑定成功、短信探测成功、`phone_already_used` 和每分钟维护任务也不再批量恢复同号段其他记录。号段统计不再成为普通任务的隐藏跳过条件。
- **修复号段抽样提前恢复污染**：手机号绑定任务排队和手机号注册抽样加载不再在实际测试前把历史 `cannot_send` / `rate_limited` 号码改为 `active`。兼容的 `restore_prefix_sample_records()` 已改为只读返回；只有具体号码产生真实任务结果后，才通过 `record_task_status()` 更新该号码，任务停止、账号前置失败或未触碰号码时原状态保持不变。

### 测试 (Tests)
- **单号隔离与面板合同回归**：扩展 `tests/test_phone_pool.py`、`tests/test_phone_pool_task_integration.py` 和 `tests/test_chatgpt_phone_registration.py`，覆盖同段失败不误伤健康号码、同段成功不复活失败号码、混合号段归入 `partial`、限定号段只返回真实可用单号、抽样排队不预恢复以及新旧摘要字段兼容；新增 `tests/test_phone_prefix_ui_contract.py` 锁定账号页、手机号池页和任务结果表的单号/样本文案。

## [2.2.14] - 2026-07-16

### 新增 (Added)
- **过期 PIX 链接清理任务日志弹窗**：`api/tasks.py` 新增 `/api/tasks/chatgpt/payment-links/pix-cleanup/task` 后台任务入口，确认清理后立即返回 `task_id`，并通过现有任务快照、SSE 日志流和 `TaskLog` 持久化完整展示重新扫描、缺少时间安全保留、SQLite 备份校验、事务清理、列表索引刷新及并发变化跳过过程。旧同步清理接口继续保留，兼容部署前已经打开的页面。
- **固定终态清理汇总**：成功任务最后一条日志统一输出 `[SUMMARY] 总：N；保留：N；过期：N`；“保留”包含有效链接及缺少时间而未清理的链接，实际清理数、并发跳过数、索引刷新数和备份结果在上一条明细日志中单独记录。没有过期链接时仍生成成功任务与 `过期：0` 汇总，执行失败则以明确的 `[FAIL]` 终态结束，不伪造成功总结。

### 优化 (Changed)
- **清理任务复用与操作边界**：同一实例已有 `pending/running` 的 PIX 清理任务时复用原 `task_id`，避免多标签页并发创建重复数据库备份；清理日志弹窗隐藏不适用于原子数据库操作的“跳过当前账号 / 完成当前后停止 / 立即停止”按钮，保留 Info/Debug 切换和复制日志。
- **任务完成后统一刷新账号列表**：`frontend/src/pages/Accounts.tsx` 将清理执行切换到现有任务弹窗与活动任务面板；任务终态继续走统一 `onTaskDone -> load()` 刷新账号列表和派生筛选，不再依赖同步接口返回后即时刷新。`frontend/src/app/AppShell.tsx` 侧栏版本同步为 `v2.2.14`。

### 测试 (Tests)
- **后台任务与前端合同回归**：新增 `tests/test_pix_payment_link_cleanup_task.py`，覆盖独立 POST 路由、请求线程不执行清理、活动任务并发复用、成功/零过期/失败终态、最后一行汇总及完整 `TaskLog` 持久化；扩展 `tests/test_accounts_pix_link_cleanup_ui.py`，锁定 React 19 上下文确认框、任务 source/mode、日志弹窗和清理任务控制按钮隐藏。
- **清理服务与历史保护回归**：定向执行 PIX 清理服务、后台任务、前端合同及 long-link 历史同步测试，继续验证 SQLite 备份完整性、原子清理、列表索引刷新、历史保留和 tombstone 防复活边界。

## [2.2.13] - 2026-07-16

### 修复 (Fixed)
- **修复过期 PIX 链接清理确认弹窗不执行**：`frontend/src/pages/Accounts.tsx` 不再调用 antd 的静态 `Modal.confirm`，改用 `App.useApp()` 提供的上下文 `appModal.confirm`。当前运行时使用 React 19，静态 Modal 依赖的旧 `react-dom.render/createRoot` 入口不会渲染确认框，导致用户只能完成预览请求而永远不会触发清理 POST；修复后确认按钮会正常调用 `/api/tasks/chatgpt/payment-links/pix-cleanup`，保留服务端重新计算、原子事务、历史保留和并发跳过边界。

### 测试 (Tests)
- **补强清理前端合同断言**：`tests/test_accounts_pix_link_cleanup_ui.py` 锁定页面使用上下文 Modal、禁止回退到静态确认 API，并继续验证预览、确认后的清理请求和列表刷新契约。
- **前端生产构建验证**：`frontend` 的 TypeScript 检查与 Vite 构建通过，确保 React 19 下的 Modal 调用可以随线上 bundle 发布。

## [2.2.12] - 2026-07-16

### 新增 (Added)
- **通用提交状态与真实提交证据筛选**：`services/account_filters.py`、`core/db.py`、`api/accounts.py` 与账号页新增 `submit_state` / `has_submitted`，将原“Idea提交”列统一为“提交状态”。结果状态与“是否真正提交过”可组合筛选；`chatgpt_last_payment_link.link_status=pix_submitted` 会被识别为 PIX 已提交证据，因此“已提交但处理失败/待复核/账号不可用”可以同时表达，不再把 Idea 与 PIX 拆成两套可见列。旧 `idea_submit_state` API、响应别名和筛选预设继续兼容，旧预设加载后迁移到通用状态且不会凭失败记录猜测已提交。
- **PIX CDK 只读预检与精确额度批量调度**：`services/chatgpt_core/baxigpt_client.py` 接入 `/api/pix/cdks/preflight`，在发送账号前识别本站余额卡与外部一次性卡。本站 `site_cdk` 只有在上游明确返回权威、精确额度后才获得多额度能力；runner 在跨实例任务级 lease 内按账号串行创建订单，每次上游原子扣 1 额度，完成本批订单创建后再统一轮询。每个 paid 结果独立写入无明文卡密的 `pix_cdk_usage_history`，同一订单的历史写入保持幂等。

### 优化 (Changed)
- **PIX 批次从逐单等待改为先提交后轮询**：`api/tasks.py` 将本站多额度卡的执行顺序改为 `preflight -> submit 1..N -> poll 1..N`，不再为每个账号先等待一次终态才提交下一个账号。预检额度在每次任务创建成功后立即在内存扣减，处理失败不会错误返还已由上游消费的额度，也不会超过预检余额继续投递；外部 PIX 卡仍维持一次只有一个有效占用，明确安全失败后才允许下一账号重新预约。
- **轮询间隔使用创建时冻结配置**：`frontend/src/pages/Accounts.tsx` 将状态轮询间隔统一为默认 5 秒、可配置范围 `1..3600` 秒，并在 LocalStorage 恢复、表单提交和请求体创建时使用同一归一化逻辑。界面明确说明运行中的旧任务继续使用创建时参数，新设置只作用于新任务；PIX 批次按该间隔统一轮询，不再表现为固定 30 秒。
- **前端版本同步至 v2.2.12**：`frontend/src/app/AppShell.tsx` 更新侧栏版本，用于确认浏览器是否已加载通用提交筛选、PIX 预检和批量调度逻辑。

### 修复 (Fixed)
- **已保存 PIX 链接不再受 Idea 不可用标记阻断**：PIX `user_link` 在任务准入和实际执行两层均跳过历史 Idea eligibility gate；成功、失败或超时只更新 `baxigpt_cdk` 与支付链接状态，不创建、不覆盖、不清除 `idea_submit`、`idea_submit_unavailable*` 或 `chatgpt_account_unavailable`。自动提链/Idea 路径继续保留既有账号不可用判定。
- **链接已提交提示消除支付成功歧义**：重复链接仍写为 `pix_submitted` 并禁止再次投递，但错误文案现在明确“不可重复使用且不代表支付成功”；运营必须重新同步或生成新链接，而不是重试旧 Stripe PIX 指令链接。
- **预检与占用异常全面失败关闭**：预检响应的模式、索引、布尔字段、来源、权威性和额度组合会严格校验；整体失败、缺项、重复/越界索引或矛盾能力均不会进入提交。网络/5xx、缺少轮询凭据、轮询硬超时及无法解释的异常会优先设置 `release_forbidden` 并锁定人工复核；即使 `mark_uncertain` 写库失败，清理流程及同卡其他结果也不能释放、重排或阻断覆盖这笔 reservation。

### 安全 (Security)
- **敏感值仍只存在于调用栈**：PIX 原始 CDK、已保存 Stripe 链接和一次性 `status_token` 不写入任务快照、运行结果、账号提交摘要或 `TaskLog`。预检元数据只保存脱敏标签、来源、额度和状态；共享占用与 paid 历史继续只保存 HMAC fingerprint、任务/账号 ID、订单 ID 和时间。

### 测试 (Tests)
- **提交筛选与 PIX 调度回归**：扩展 `tests/test_account_filters.py`、`tests/test_filtered_task_scope.py`、`tests/test_account_filter_presets.py` 与新增的提交摘要/前端合同测试，覆盖 SQL 索引和 Python fallback 一致性、PIX 已提交证据、旧预设迁移及 filtered task 范围。`tests/test_baxigpt_cdk_pool.py` 覆盖本站三额度先全量提交后统一轮询、精确额度不超投、外部卡 paid 后 blocked、明确失败复用、非权威预检零提交、未知结果写库失败仍保留 lease、Idea marker 隔离、paid 历史幂等及敏感值不落盘。

## [2.2.11] - 2026-07-16

### 新增 (Added)
- **当前实例一键清理过期 PIX 链接**：账号页“支付链接生成”下拉菜单新增“清理过期 PIX 链接”。`frontend/src/pages/Accounts.tsx` 会先调用 `/api/tasks/chatgpt/payment-links/pix-cleanup/preview` 展示当前实例、北京时间截止点、可清理数量和缺少时间信息的安全跳过数量；运营确认后再调用清理接口，并刷新账号列表和支付链接平台筛选，不受当前勾选、分页或筛选范围限制。
- **北京时间 11:00 到期模型**：新增 `services/chatgpt_core/pix_payment_link_cleanup.py`。新链接优先采用 Stripe 明确返回的 `link_expires_at`；旧链接缺少上游时限时，按北京时间生成时刻推导，当天 11:00 前生成的链接在当天 11:00 到期，11:00 及之后生成的链接在次日 11:00 到期。恰好 11:00 归入新周期，缺少两类时间证据的记录保持不动。

### 优化 (Changed)
- **低内存、可回滚的单事务清理**：清理预览通过 SQLite JSON 函数只提取账号当前支付链接小对象，不把账号完整 `extra_json` 批量加载进 Python。执行阶段在确有过期候选时先为当前实例创建权限 `0600` 的在线 SQLite 备份并执行完整性校验，再使用 `BEGIN IMMEDIATE` 重新计算资格并原子写入；只在 `accounts.cashier_url` 与被清 URL 完全一致时置空，提交后再次执行完整性校验，同时刷新对应 `account_list_state.payment_link_platform`，避免账号页 PIX 筛选保留陈旧数量。
- **审计边界保持清晰**：过期当前链接改写为不含 URL 的 `expired_cleaned` tombstone，保留生成时间、实际/推导到期时间、清理时间和旧链接状态。账号业务状态、订阅状态、支付生成历史、PIX CDK 占用/核销、PIX 提交结果及任务日志均不修改；重复执行保持幂等。

### 修复 (Fixed)
- **阻止历史同步复活已清链接**：`services/long_link_history_sync.py` 识别 PIX 清理 tombstone 及 `pix_cleanup_through_at` 截止点，仍将旧成功记录保存在 `payment_link_generations` 审计历史中，但不会重新写回已清理的当前 URL；只有截止点之后真正生成的新链接才能覆盖 tombstone。
- **前端版本同步至 v2.2.11**：`frontend/src/app/AppShell.tsx` 更新侧栏版本，用于确认浏览器已加载过期 PIX 链接清理入口与确认交互。

### 测试 (Tests)
- **PIX 清理边界回归覆盖**：新增 `tests/test_pix_payment_link_cleanup.py`，覆盖 10:59:59、11:00:00、新旧周期、上游到期优先、旧数据推导、缺时间跳过、非 PIX 隔离、`cashier_url` 精确匹配、账号状态不变、派生筛选刷新、历史保留和重复清理幂等；`tests/test_long_link_history_sync.py` 增加 tombstone 防回填用例，`tests/test_accounts_pix_link_cleanup_ui.py` 锁定预览、确认、执行和列表刷新合同。

## [2.2.10] - 2026-07-16

### 新增 (Added)
- **批量本地状态同步并发配置**：`frontend/src/pages/Accounts.tsx` 的“批量同步本地状态配置”新增 `1-10` 个账号的并发数与“任务内强制独立出口 IP”开关；这两个值保存到 `/config`，再次打开弹窗会恢复。创建任务时会把请求并发、实际并发和隔离规则冻结进任务元数据，任务回放不会被后续全局配置修改影响。
- **任务内出口 IP 领取审计**：`api/tasks.py` 为并发 worker 增加真实出口 IP 探测和原子领取。相同出口 IP 不会被同一批任务的两个账号静默复用；runner 会尝试下一个代理候选，候选耗尽时将该账号明确记为失败，并在任务元数据保存已分配、撞 IP 与探测失败的聚合审计。

### 优化 (Changed)
- **账号级浏览器身份复用与并发隔离**：`services/chatgpt_core/status_probe.py` 和 `services/chatgpt_core/token_refresh.py` 让本地状态探测、OAuth 刷新和 ChatGPT API 请求复用账号保存的 UA、Client Hints、`oai-did` 与 curl-cffi TLS profile。`api/tasks.py` 按完整账号指纹加锁：不同稳定指纹可并发，相同指纹会串行；缺少历史指纹的旧账号统一退化为串行，避免伪装成安全并发。
- **并发运行边界清晰化**：每个本地状态 worker 使用独立 SQLModel `Session`，调度线程统一处理进度、停止后不再启动新账号、失败记录和最终任务归档；动态代理在独立出口模式下自动增加候选数量，直连或单指定代理会在创建任务时被拒绝。

### 修复 (Fixed)
- **代理日志递归脱敏**：批量同步日志不再写入代理 URL 或原始候选内容，只写“动态代理 / 代理池 / 指定代理”等安全网络标签与已验证出口 IP，修复 URL 进入旧脱敏链路时可能导致任务 worker 异常的递归问题，同时避免代理端点落入任务历史。
- **前端版本同步至 v2.2.10**：`frontend/src/app/AppShell.tsx` 更新侧栏版本，用于确认浏览器已加载并发与隔离控制。

### 测试 (Tests)
- **并发与身份隔离回归覆盖**：`tests/test_probe_local_status_batch_config.py` 覆盖并发参数冻结、动态候选扩容、独立出口下直连拒绝、独立数据库会话、不同指纹真实并发、旧账号指纹缺失时串行，以及出口 IP 冲突阻断；`tests/test_chatgpt_status_probe.py` 覆盖已保存账号指纹传入 Token Refresh 与本地 API 探测。

## [2.2.9] - 2026-07-16

### 优化 (Changed)
- **PIX 链接状态成为可执行边界**：`api/tasks.py` 将“已保存 Stripe PIX 链接已被管理端提交”的事实写回对应 `chatgpt_last_payment_link.link_status=pix_submitted`，且只在任务执行期读取到的 URL 与当前缓存仍一致时更新，避免晚到的任务结果覆盖新同步链接。该状态只封存旧链接，不把账号标记为不可用；重新同步或重新生成的新链接仍可继续提交。
- **支付链接缓存可自动换新**：`services/chatgpt_core/payment_link_cache.py` 将 `pix_submitted` 显示为“已提交 PIX 管理端”，并纳入需要重新生成的链接状态，避免后续支付链接任务误复用已被远端占用的二维码。
- **任务面板先于快照打开**：`frontend/src/pages/Accounts.tsx` 在创建 iDEAL / PIX 任务拿到 `task_id` 后立即打开日志面板，再异步刷新快照。短任务、瞬时失败或快照请求暂不可用都不会阻塞操作反馈；面板会自动重试读取任务。

### 修复 (Fixed)
- **已保存 PIX 链接重复投递**：管理端返回“该 PIX 支付链接已被提交，请勿重复使用”时，runner 识别为链接级终态并转换为明确的“请先重新同步或生成新链接”结果。后续批量提交会在创建任务前跳过这些链接并显示数量，不再反复发送同一 URL、消耗操作时间或让快速失败看起来像点击无响应。
- **任务标题与结果语义**：已保存链接模式的任务窗口明确显示“PIX 链接上传”；无可投递账号时，弹窗直接说明被管理端占用的链接数量及下一步，而不是只给出泛化的空任务提示。
- **前端版本同步至 v2.2.9**：`frontend/src/app/AppShell.tsx` 更新侧栏版本，便于确认浏览器已加载本次任务反馈与重复链接保护。

### 测试 (Tests)
- **重复链接回归覆盖**：`tests/test_baxigpt_cdk_pool.py` 覆盖上游 HTTP 409 的已提交链接、状态写回、后续 admission 跳过及敏感值不落入快照；`tests/test_accounts_pix_link_upload_ui.py` 锁定“任务已创建即打开面板”的前端契约。

## [2.2.8] - 2026-07-16

### 新增 (Added)
- **本站多额度 PIX CDK 成功历史**：`core/pix_cdk_usage.py` 将跨 Plus / Plus2 实例共享的 PIX 使用表拆分为“当前占用”和 `pix_cdk_usage_history`。每一笔已确认 paid 都以不可逆、无明文卡密的指纹审计记录保存；本站多额度 CDK 在写入历史成功后删除当前 reservation，下一账号才能重新预约同一张卡。旧版本残留的 `state=paid` 当前锁在首次读取/预约时会原子迁移到历史表，不会因为升级永久堵住已充值卡。
- **统一已保存链接合同**：`services/chatgpt_core/baxigpt_client.py` 的 `submit_pix_user_link()` 切换为 `submitMode=pix_unified_user_link + cdk`，并读取上游返回的 `cdk_reuse_policy`。`after_paid` 表示本站余额卡可在 paid 后串行复用；缺字段或 `one_success` 一律按外部一次性 PIX 卡处理，兼容旧支付服务而不冒险重复扣款。
- **PIX 支付链接定向导出**：账号页“导出”菜单新增“PIX 支付链接（已选账号）”和“PIX 支付链接（当前筛选）”。`api/chatgpt.py` 通过既有单次下载票据增加 `mode=pix_payment_links`，只输出每行一个已保存、已校验的 PIX HTTPS 链接；当前筛选导出会携带完整筛选条件与页面总数，服务端校验后冻结账号范围，避免分页或刷新导致导出范围漂移。

### 优化 (Changed)
- **按账号范围计算 PIX 目标**：`api/tasks.py` 不再把默认目标成功数压缩为 CDK 行数。`target_success_count=0` 现在和 iDEAL 一致，表示按所选/筛选账号范围尽量全部提交；多张卡仍只影响并行槽位，不再限制本站多额度卡能处理的账号总数。
- **同卡严格串行调度**：PIX runner 只在远端轮询明确 `paid`、本地 paid 历史事务完成、且未达到本次目标/未请求停止后，才把本站卡放回当前可用队列。任何时刻同一 fingerprint 只能有一个 reservation 或在途上游订单；不同 CDK 可以各自处理一笔，吞吐量不会被单卡保护牺牲。
- **账号页说明同步**：`frontend/src/pages/Accounts.tsx` 明确区分本站多额度 CDK 与外部一次性 PIX CDK，说明已保存链接无需 AT、本站卡的 paid 后串行复用、余额不足仅停止本轮、以及目标数量 0 的账号范围语义。侧栏版本同步为 `v2.2.8`。

### 修复 (Fixed)
- **已保存 Stripe PIX 链接可使用本站 CDK**：对接统一上游入口后，账号库不会再把本站 CDK 错当成外部 `pixCdk` 提交并收到“本站 CDK 不能作为外部 PIX CDK 使用”。上游会按本地余额扣减并由 IDEA 处理，Auto-GPT 保留现有状态令牌轮询链路。
- **余额不足不再永久封卡**：上游明确返回“CDK余额不足 / 当前剩余 0 / 额度已耗尽”等容量错误时，runner 只释放当前 reservation、不把卡重新塞回本轮队列，也不写 `blocked`。后续充值后可重新创建任务；卡无效、禁用、归档或已使用仍保持 blocked。
- **未知结果继续 fail-closed**：网络异常、5xx、提交响应缺少 `task_id + status_token`、轮询超时或无法解释的异常都保留为 `uncertain` 人工复核，不会因为多额度支持而自动复用或重投。

### 安全 (Security)
- **敏感边界保持不变**：新历史表只保存 HMAC fingerprint、任务/账号 ID、订单 ID 与时间，不写原始 CDK、Stripe URL 或状态令牌；任务快照、账号 `extra_json`、运行结果和 `TaskLog` 继续只保留脱敏 CDK 标识及非敏感结果。
- **链接导出最小化**：导出仅接受 `account_payment_link_summary()` 认可的当前 PIX 缓存，不回退旧 `cashier_url`，也不携带邮箱、密码、AccessToken、代理或内部任务字段；下载响应增加 `Cache-Control: no-store, private`，票据仍为五分钟内单次消费。

### 测试 (Tests)
- **回归覆盖多额度与兼容边界**：`tests/test_baxigpt_cdk_pool.py` 覆盖单张本站 CDK 连续处理三个已保存链接且提交/轮询严格交替、旧 paid 锁迁移、本站余额耗尽后可再次准入、外部卡 paid 后保持 blocked、5xx 保持 uncertain，以及统一直传请求体和敏感值不落盘。
- **导出范围与内容回归**：新增 PIX 链接导出测试，覆盖仅输出有效 PIX 链接、排除 PayPal/畸形/旧 `cashier_url`、不泄露 AT，以及“当前筛选”账号范围冻结和筛选总数变化时返回 `409 FILTER_SCOPE_CHANGED`。

## [2.2.7] - 2026-07-16

### 新增 (Added)
- **账号库直传已保存 PIX 链接**：`frontend/src/pages/Accounts.tsx` 的既有“iDEAL / PIX 批量提交”弹窗在 PIX 通道新增“上传已保存 PIX 链接”方式。它可直接对选中账号或当前筛选范围发起任务，按账号顺序使用已同步到 `chatgpt_last_payment_link` 的支付链接；无需重新提取 Access Token。运营可先用账号列表的 `支付链接 = PIX` 筛选收敛范围，避免把 PayPal、Hosted Checkout 或无链接账号混入。
- **服务端直连提交合同**：`services/chatgpt_core/baxigpt_client.py` 增加 `submit_pix_user_link()`，仅在内存中向支付提交服务发送 `submitMode=pix_user_link`、`pixCdk`、`pixPayLink` 和空账号数组；任务继续复用现有 PIX 状态令牌轮询、明确失败释放、成功核销、未知结果锁定与本地订阅状态刷新链路。

### 修复 (Fixed)
- **链接与账号资格在执行前双重收敛**：`api/tasks.py` 仅接收账户缓存内 HTTPS `payments.stripe.com/qr/instructions/...` 的 Stripe PIX 指令链接，并拒绝缺链接、错误平台、非指令链接、临近到期链接、已标记不可提交账号或范围变化后的账号。直传模式不再要求账号保有 Access Token；运行时会重新从账号缓存读取链接，因此 URL 不写入任务元数据、运行结果或任务日志。
- **提交开关失败关闭**：创建直传任务前会读取支付提交服务的公开通道状态；PIX 总通道或“自带链接”子通道未开启时返回明确错误且不创建任务、不预留 CDK。该保护不改写本地 iDEAL 卡密池或其后续校验路由。

### 安全 (Security)
- **敏感值不持久化**：PIX CDK、支付链接和上游一次性状态令牌均只在任务调用栈中存在。错误、任务摘要、账号提交标记和 `TaskLog` 会继续脱敏；账号只保存提交方式标识，不复制链接或 CDK。

### 测试 (Tests)
- **覆盖直传边界与兼容性**：新增测试验证无 AT 的已保存 Stripe PIX 链接可以提交、非兼容或临近到期链接被跳过、关闭的远端开关不会创建任务、上游请求体严格使用 `pix_user_link` 合同，以及 CDK、链接、状态令牌不进入快照或日志。

### 优化 (Changed)
- **版本同步**：侧栏版本更新为 `v2.2.7`，用于确认浏览器已加载 PIX 已保存链接上传入口。

## [2.2.6] - 2026-07-16

### 新增 (Added)
- **long-link 历史订阅链接补齐工具**：新增 `scripts/sync_long_link_payment_history.py` 与 `services/long_link_history_sync.py`，可从 `/opt/openai-pay-long-link/app/data/tasks.db` 的 `long_link_success_history` 将已成功生成的链接回填到活跃 Auto-GPT 实例。同步使用管理员历史中已解析的账号邮箱和本地账号唯一匹配，不读取或复制 Access Token、代理或 long-link 配置；内部批次已存在的 `remote_job_id` 会优先作为既有归属核验。
- **可审计且幂等的支付链接迁移**：每条历史链接以 `long-link-history:<job_id>` 写入 `payment_link_generations`，重复执行不会重复建历史；同一账号的所有成功链接都会保留为生成历史，只有完成时间最新的一条在未被更新本地链接覆盖时写入 `chatgpt_last_payment_link` 与 `cashier_url`。账户订阅状态、`used`、手机号、邮箱和认证材料均不修改。

### 安全 (Security)
- **跨库同步保护**：默认 dry-run；apply 前对每个目标 SQLite 运行完整性检查并创建权限为 `0600` 的 `.backup`，每库用独立即时事务提交后再次校验完整性。邮箱重复、目标账号缺失、远端任务归属冲突、无效 URL 和损坏 `extra_json` 都会跳过并以聚合计数报告，不会猜测或覆盖错误账号。

### 测试 (Tests)
- **覆盖历史回填边界**：新增回归验证 dry-run 不写库、同账号多条历史只提升最新链接、旧历史不覆盖更新的当前链接、跨实例重复邮箱拒绝同步、重复运行幂等，以及历史和当前缓存均不携带源端敏感输入。

### 优化 (Changed)
- **版本同步**：侧栏版本更新为 `v2.2.6`，用于确认已加载 long-link 历史同步能力对应的发布版本。

## [2.2.5] - 2026-07-16

### 新增 (Added)
- **账号列表支付链接列与平台筛选**：`frontend/src/pages/Accounts.tsx` 的“支付链接”列表头现在与“手机号/API”列使用同一筛选交互，支持 `PIX`、`PayPal`、`ChatGPT 结账`、其他支付链接和“无支付链接”。移动端筛选栏、筛选组合保存/恢复及组合编辑器同步提供该条件；对“当前筛选结果”执行支付链接生成、补抓、手机号绑定、状态同步或上传等批量操作时，服务端会使用同一平台范围而非当前分页数据。

### 优化 (Changed)
- **支付链接平台建立可索引的派生事实**：`services/account_filters.py` 与 `account_list_state` 新增 `payment_link_platform`，从通用 `chatgpt_last_payment_link` 缓存识别 PIX、PayPal、ChatGPT Hosted Checkout 和其他有效链接；无有效 HTTP(S) 链接、畸形链接或仅有旧 `cashier_url` 的账号归入“无支付链接”。旧 `chatgpt_paypal_url` 缓存继续兼容识别为 PayPal，避免历史账号被漏筛。
- **账号列表实际返回可操作的链接摘要**：`api/accounts.py` 的紧凑列表响应新增 `payment_link` 与 `payment_link_platform`，账号页可直接显示平台、生成状态并复制或打开链接。仅返回 URL、平台、类型、状态、格式和时间；代理、profile hash、远端批次/任务 ID 等内部字段仍不出现在列表响应。
- **版本同步**：侧栏版本更新为 `v2.2.5`，用于确认浏览器已加载支付链接列与筛选能力。

### 测试 (Tests)
- **锁定平台判定与全范围一致性**：覆盖 PIX、旧 PayPal 缓存、Hosted Checkout、未知未来平台、无链接及 `javascript:` 畸形值，验证 Python 回退路径与 SQLite 索引筛选一致；同时覆盖紧凑响应脱敏、筛选预设保存和账号列表与批量任务范围完全一致。

## [2.2.4] - 2026-07-16

### 新增 (Added)
- **账号列表手机号绑定情况筛选**：账号页“手机号/API”列表头新增“已绑定 / 未绑定”条件筛选，同时在移动端筛选栏与筛选组合编辑器提供相同条件。筛选预设会保存并恢复该条件，当前筛选范围发起的手机号绑定、补抓、状态同步、上传及支付等批量操作也会携带相同范围，不会因为服务端分页而只处理当前页。

### 修复 (Fixed)
- **绑定判断与 RT、被动手机号线索彻底分离**：`services/account_filters.py` 为账号列表索引新增 `phone_binding_state`，只在本地记录明确为 `bound / success / completed` 且手机号至少含 8 位数字时标记“已绑定”。仅有 Refresh Token、被动发现的已绑定手机号、验证码挑战记录、短号码或没有手机号记录的账号均不会被误判为“已绑定”；其中后三类会归入页面的“未绑定”筛选。
- **服务端分页与批量范围一致**：`api/accounts.py`、筛选请求模型和账号列表索引统一支持 `phone_binding_state`，列表总数、翻页结果、筛选预设与“当前筛选结果”批量任务复用同一查询条件。旧实例启动后会自动升级索引表、建立状态索引并按新的派生版本回填，避免历史缓存造成筛选偏差。

### 测试 (Tests)
- **覆盖确认绑定边界与跨接口范围**：新增回归覆盖 Plus + RT 但仅有手机号线索、短号码、无手机号记录和真实 OTP 绑定成功四种状态，验证 SQL 索引筛选与 Python 语义一致，并验证账号列表 API 与批量任务解析得到相同账号集合。

## [2.2.3] - 2026-07-16

### 新增 (Added)
- **手机号池绑定情况筛选**：`frontend/src/pages/PhonePool.tsx` 在号码列表的既有筛选栏增加“全部绑定情况 / 已绑定 / 未绑定”。已绑定以本地 `bound_count` 为主、已绑定账号邮箱为兼容兜底；未绑定只返回两者均为空的号码。筛选与搜索、号码状态、普通任务状态及 API 到期条件可叠加，切换后自动回到第一页并清空隐藏选中项，避免批量操作误作用于不可见号码。

### 优化 (Changed)
- **筛选空态与版本同步**：号码列表在任一筛选条件没有结果时明确显示“没有符合当前筛选条件的手机号”，侧栏版本更新为 `v2.2.3`，便于确认浏览器已加载绑定情况筛选。

## [2.2.2] - 2026-07-16

### 新增 (Added)
- **PIX 上游二维码有效期透传与展示**：`services/chatgpt_core/long_link_payment_client.py` 接收 long-link 通用批次结果中的 `link_expires_at`，并通过账号 `chatgpt_last_payment_link` 缓存和 `payment_link_generations.result_json` 持久化。账号详情的当前支付链接与最近生成历史会显示上游明确返回的绝对到期时间、剩余时间或已过期状态；历史记录没有该字段时保持“未知”，不使用 Checkout Session 到期时间猜测。

### 优化 (Changed)
- **PIX 缓存复用按真实二维码时限收敛**：`services/chatgpt_core/payment_link_cache.py` 仅对 `link_type=pix` 且具有合法上游 epoch 的链接执行有效期判断；已过期或将在 60 秒内到期的缓存不会被单账号或批量“支付链接生成”复用，会重新提交至 long-link。旧缓存、非 PIX 链接和没有上游时限的记录维持原有兼容行为。
- **版本同步**：侧栏版本更新为 `v2.2.2`，用于确认浏览器已加载 PIX 有效期展示和缓存失效规则。

### 修复 (Fixed)
- **兼容单账号接口的生成历史保留有效期**：`api/actions.py` 与 `api/tasks.py` 的安全结果字段白名单加入 `link_expires_at`，使旧 `POST /api/chatgpt/{account_id}/payment-link`、平台 action 和批量任务都不会在落库前丢失该非敏感字段。

### 测试 (Tests)
- **补齐有效期回归覆盖**：覆盖 long-link PIX 结果标准化、账号缓存持久化、临近到期缓存拒绝复用、历史接口安全返回以及兼容单账号接口的结果落库。

## [2.2.1] - 2026-07-16

### 修复 (Fixed)
- **RT 与手机号绑定事实分离**：`services/chatgpt_core/oauth_client.py` 在 OpenAI `phone-otp/validate` 明确成功后才记录确认绑定；`services/chatgpt_core/bound_phone.py` 统一写入 `chatgpt_phone_binding`、历史、`chatgpt_bound_phone` 及号码别名。新注册账号尚未创建本地行时，确认事件会随 RT 注册结果透传，由注册适配器保存，避免“真实绑号成功但账号没有绑定记录”。
- **阻止 add_phone 继续地址绕过本地确认流程**：passwordless OAuth 命中 `add_phone` 时，只有显式关闭自动新绑才保留历史 continue-url 兼容路径；当前允许新绑的配置必须进入标准发码、收码和 OTP 校验流程，不能仅因获得 authorization code 就把账号当作已完成手机号步骤。
- **OAIPay Plus 分类改为失败关闭**：账号能力新增 `has_confirmed_phone_binding` 和 `phone_binding_state`；仅 Plus、RT 和完整已确认绑定同时成立时才进入 `PLUS--已接美国长效`。Plus + RT 但本地无确认绑定记录统一落入 `PLUS--未接码`，不再把 RT 当作手机号绑定证明。账号列表 API 会为旧能力快照实时补算该字段。

### 优化 (Changed)
- **自动分类提示与版本同步**：账号页 OAIPay 自动分类说明明确“已确认手机号绑定”前提，侧栏版本更新为 `v2.2.1`，便于确认浏览器已加载修复。

### 测试 (Tests)
- **补齐绑定与分类回归**：覆盖 OTP 成功后的确认绑定持久化、新注册 metadata 透传、允许新绑时不走 add_phone continue-url 快捷路径、未确认绑定的 Plus + RT 分类降级，以及账号列表能力字段的兼容输出。

## [2.2.0] - 2026-07-16

### 新增 (Added)
- **通用 long-link 支付链接内部协议**：新增 `services/chatgpt_core/long_link_payment_client.py`，Auto-GPT 只向 `/api/internal/payment-links/profile`、`/batches` 和 `/batches/{batch_id}` 传递 Access Token、稳定请求 ID 与预期 profile hash。支付类型、账单国家/币种、Checkout 模式、浏览器指纹、代理链和全局并发均由 `/opt/openai-pay-long-link` 管理端当前配置冻结并执行。
- **批量提交后统一轮询与可审计历史**：`api/tasks.py` 的批量支付链接任务会先持久化所有待处理账号并一次性提交远端批次，再按远端 `batch_id` 统一轮询；新增 `payment_link_generations`，持久化账号、任务、远端批次/任务 ID、profile hash、支付类型、状态、链接、提交/开始/生成/落库时间及脱敏错误。账号详情新增最近生成记录，账号表新增支付链接列。
- **管理端配置只读摘要**：新增 `GET /api/tasks/chatgpt/payment-links/profile` 与历史查询接口。浏览器只读取支付类型、国家/币种、Checkout 模式、指纹名称、代理已配置状态、区域/PIX 摘要、有效并发和 profile hash，不暴露代理、密钥或 Access Token。

### 优化 (Changed)
- **支付链接生成统一到 long-link**：账号动作、批量工具栏、任务弹窗及兼容单账号 URL `POST /api/chatgpt/{account_id}/payment-link` 全部使用 long-link 当前管理端配置。原有本地 Hosted、短链接、国家、币种、代理和 PayPal 专用来源参数不再出现在界面或影响执行；旧客户端继续可发送这些字段，但服务端忽略它们以保持请求兼容。
- **缓存按配置版本收敛**：仅 `payment_source=long_link`、`payment_link_format=long_link` 且 profile hash、国家、币种一致的缓存可在普通模式复用；强制重新生成跳过缓存。历史 Hosted、短链和 PayPal 缓存仍可读、可展示，但不会误复用为当前管理端配置生成的链接。
- **状态与命名一致**：任务运行时与前端补充 `partial`、`interrupted` 终态；所有操作入口统一显示“支付链接生成”，侧栏版本同步至 `v2.2.0`。

### 修复 (Fixed)
- **PayPal 返回兼容旧字段而不恢复旧执行器**：当 long-link 当前类型为 `paypal` 时，通用结果仍镜像写入 `extra.chatgpt_paypal_url`，同时使用通用 `chatgpt_last_payment_link` 与历史记录，避免外部交付读取历史 PayPal 字段时断裂。
- **批次终态与重启审计正确落库**：`/opt/openai-pay-long-link/app/app.py` 的内部批次在每个 job 状态变化和服务重启恢复后都会聚合状态并写入 `completed_at`；不会再出现所有 job 已终态但批次审计记录仍显示未完成的情况。
- **兼容单账号 action 适配器**：`api/chatgpt.py` 的 `_to_codex_account()` 补齐标准 `token` 属性，使旧 URL 通过平台 action 复用通用 long-link 执行器，而不会因鸭子类型缺字段失败。

### 安全 (Security)
- **严格保持敏感值边界**：批量请求、任务元数据、TaskLog、生成历史和前端 profile 摘要均不保存或回传 Access Token、管理端代理凭证或内部 API 密钥。客户端验证远端回传的请求 ID 与提交集合完全一致，避免错误批次句柄被映射到本地账号。

### 测试 (Tests)
- **覆盖通用协议与回归边界**：新增 `tests/test_long_link_payment_client.py`、`tests/test_payment_link_generation_history.py`、`tests/test_chatgpt_payment_link_endpoint.py`；扩展支付来源和批量任务测试，覆盖先全量提交后批次轮询、profile 缓存隔离、强制刷新、PIX/PayPal 结果、PayPal 旧字段镜像、远端中断、历史脱敏/分页及旧 URL 不再调用本地 Hosted/短链生成器。
- **完成构建与跨模块回归**：验证 Python 编译、前端 TypeScript/Vite 生产构建、long-link 内部 API/调度测试，以及外部订阅交付和退役能力契约，确保既有历史链接读取不被本次收敛破坏。

## [2.1.8] - 2026-07-15

### 修复 (Fixed)
- **并发注册不再同时压垮 HME lease 控制面**：`core/base_mailbox.py` 为 `HmeReadyApiClient.prepare()` 增加进程内共享闸门；同一 auto-gpt 实例的注册 worker 会在发起 HTTP 前串行领取 HME lease，而不是让多个请求同时在 Helper 的单一账本锁后排队并各自触发 20 秒 read timeout。锁等待不占用 HTTP read timeout，OTP 读取仍保持并发且仍由 auto-gpt 直连 TempMail。

### 优化 (Changed)
- **前端版本同步至 v2.1.8**：`frontend/src/app/AppShell.tsx` 更新侧栏版本，便于确认浏览器已加载 HME prepare 并发闸门。

### 测试 (Tests)
- **延续 HME lease 边界回归覆盖**：保留 v2.1.7 中父任务归属、无 Helper 转发邮箱 ID 时的 TempMail 直查兼容，以及 HME 控制面错误不误切代理的用例；本次闸门只包围 lease prepare，不改变 OTP 轮询与 finalize 契约。

## [2.1.7] - 2026-07-15

### 修复 (Fixed)
- **HME Ready prepare 明确归属父注册任务且不再误切代理**：`core/base_mailbox.py` 的 Helper Ready 客户端将 attempt 级 `request_id` 与父 `task_id` 分开传给 HME Helper；`api/tasks.py` 在创建邮箱实例时注入真实注册任务 ID。这样即使 prepare 客户端超时，Helper 中已写入的 checkout 仍可按父任务审计、回收或对账。`core/proxy_utils.py` 同时把 `HME Ready API` / iCloud Helper 控制面异常排除出代理失败判定，避免内部邮箱服务超时被当成代理故障并触发无意义的换代理、重复 prepare 放大。
- **保持 auto-gpt 作为 TempMail 邮件数据面唯一读取方**：Helper prepare 未返回 `forward_mailbox_id` 时，现有 `IcloudHmeMailbox` 会继续只按 lease 的 `forward_to` 直连 TempMail 解析邮箱并读取 OTP；tag 地址仍只通过原始投递头精确匹配，不会退回 Helper 收件/转发中转。

### 优化 (Changed)
- **前端版本同步至 v2.1.7**：`frontend/src/app/AppShell.tsx` 更新侧栏版本，便于确认浏览器已加载 HME 控制面与代理错误分类修复。

### 测试 (Tests)
- **锁定 HME lease 审计与重试边界**：`tests/test_icloud_hme_mailbox_finalize.py` 覆盖 attempt idempotency key 与父 task ID 的独立传递及无 `forward_mailbox_id` 响应兼容；新增 `tests/test_proxy_utils.py`，确认 HME Helper timeout 不再被误判为代理 timeout，而真实 SOCKS 连接超时仍会走代理失败语义。

## [2.1.6] - 2026-07-15

### 修复 (Fixed)
- **HME tag 地址直接从 TempMail 原始投递头精确收码**：`core/base_mailbox.py` 保持 Helper 仅负责 HME lease/状态、业务实例直接按 `forward_mailbox_id` 读取 TempMail 的既有边界；当逻辑地址为 `+gptN` tag 时，provider 只在邮件 header block 中匹配完整 logical 地址或 Apple 在 `Return-Path` 中使用的 `local+tag=domain_at_...` transport token。此前 Apple 会将可见 `To` 与 `X-ICLOUD-HME p=` 归一化为 physical HME alias，导致实际已投递的 OTP 被误判为“不属于当前邮箱”。tag 分支明确禁止回退 physical alias，也不将正文中的引用地址作为归属证据，避免同一 physical HME 的 gpt1/gpt2/gpt3/gpt4 槽位串码或 gpt1/gpt10 前缀误命中。
- **HME Ready 的实际 OTP 等待时间不再被旧全局 20 秒配置截断**：`services/chatgpt_core/plugin.py` 让 `hme_ready_api`（及显式 `helper_ready_api` 模式）与既有 `email_api` 一样遵从 ChatGPT 注册状态机传入的首次/重发等待窗口，并继续受单账号总预算控制。此前日志会显示等待 60/90 秒，底层却优先读取 `mailbox_otp_timeout_seconds=20` 并在 20 秒超时，造成慢投递邮件和重发阶段被提前放弃。

### 优化 (Changed)
- **保留直查 TempMail 的 HME Ready 架构并补齐诊断字段**：命中 OTP 后的 verification metadata 新增 `alias_match_source`，可区分 tag transport-header 路由与旧 `received_for/raw` 路径；未改为 Helper `wait-code` 中转，避免把共享转发箱轮询 I/O 回灌到 HME 控制面。
- **前端版本同步至 v2.1.6**：`frontend/src/app/AppShell.tsx` 更新侧栏版本，便于确认当前浏览器对应 HME tag 收码与 OTP 等待修复版本。

### 测试 (Tests)
- **锁定 tag 隔离和等待窗口契约**：`tests/test_icloud_hme_mailbox_finalize.py` 覆盖真实 Apple `Return-Path` 编码、physical `received_for`、sibling tag、gpt1/gpt10 前缀冲突及正文伪地址，确认仍由 auto-gpt 直查 TempMail 且不会调用 Helper 读信；`tests/test_chatgpt_plugin.py` 覆盖 HME Ready 不被 `mailbox_otp_timeout_seconds` 静默缩短。

## [2.1.5] - 2026-07-15

### 新增 (Added)
- **所有任务面板提供两种明确的停止模式**：`core/task_runtime.py`、`api/tasks.py` 与 `frontend/src/components/TaskLogPanel.tsx` 将停止控制统一为“完成当前后停止”和“立即停止”。支持排空的任务在 API 快照中声明 capability；前端仅对支持该能力的 `TaskLogPanel` 展示“完成当前后停止”，旧客户端不带请求体调用 `POST /api/tasks/{task_id}/stop` 仍保持立即停止语义。
- **停止控制日志在点击和终态两次持久化**：停止请求在返回前将当时完整日志、控制状态和任务快照 upsert 到 `TaskLog`；`RegisterTaskStore.finish()` 再以终态快照持久化收尾日志。这样注册、手机绑定、邮箱复测、上传、提链、Auth 补抓、PIX、Idea/Baxi、本地状态探测等任务即使中途停止或进程异常，也不会丢失用户已经看到的运行日志。

### 修复 (Fixed)
- **完成当前后停止不再偷跑下一个执行单元**：`api/tasks.py` 在账号、手机号账号单元、订单和并发任务真正开始前统一领取 attempt；注册与 OaiPay 在排队延迟结束、上游调用前二次检查，手机绑定允许已领取账号继续其内部号码重试，PIX/Idea 保持已提交订单轮询但停止新的 CDK/订单提交。任务最终状态统一收敛为 `stopped`，不新增前端无法识别的终态。
- **立即停止的日志与外部会话一致**：`api/chatgpt.py` 的单账号 GoPay 会先取消真实支付会话，再推进通用停止状态；取消失败会返回 `409`、保存诊断日志且允许再次重试。GoPay 批量任务改由后端持久化调度，`after_current` 会排空已启动会话并阻断后续轮次、手机号递延和新会话；立即停止任一会话取消失败时不再错误显示为已取消，也不会再派发后续账号。
- **修复两个长等待对立即停止不响应**：`api/tasks.py` 新增受控制的分段等待，Idea/Baxi 活跃订单轮询和批量本地状态探测的随机延时最多每 0.5 秒检查一次立即停止，避免任务卡在原生 `sleep()` 到计时结束才停止。

### 优化 (Changed)
- **GoPay 批量状态脱离浏览器计时器**：`frontend/src/pages/Accounts.tsx` 与 `frontend/src/features/accounts/components/BatchGopayWorkbench.tsx` 改为读取、恢复和轮询服务端批任务；关闭或刷新工作台不会丢失当前批次、停止模式或后续派发闸门。
- **多实例发布正确识别 standby 容器**：`deploy.sh` 的 standby 检查改用 `docker container inspect`，不再把同名 `auto-gpt` 镜像误判为容器并在状态模板解析处中断发布自检；不存在的 standby 继续保持不启动。
- **前端版本同步至 v2.1.5**：`frontend/src/app/AppShell.tsx` 更新侧栏版本，便于确认浏览器已加载停止模式、日志持久化和 GoPay 调度修复。

### 测试 (Tests)
- **覆盖控制语义与持久化边界**：扩展 `tests/test_task_runtime.py`、`tests/test_chatgpt_task_logging.py`，验证 graceful/immediate 优先级、同账号内部重试、停止响应前日志落库、终态日志保留，以及长等待在首个切片后响应立即停止。
- **覆盖 GoPay 停止契约**：新增 `tests/test_gopay_batch_stop_modes.py`，验证单会话真实取消、失败重试、批量排空、启动闸门、批量立即停止失败不假报取消和旧版无 body cancel 兼容。

## [2.1.4] - 2026-07-15

### 修复 (Fixed)
- **手机号池号段策略改为真实状态回写**：`services/chatgpt_core/phone_pool_repository.py` 不再只把“同号段有可用号码”体现在汇总标签。OpenAI 明确拒绝、收码 API 无验证码或 API 异常会将同一前四位号段内仍有绑定容量的号码统一标记为不可用；任一真实绑定成功、发码/收码探测成功或已占用号码信号会恢复同段可恢复号码为可用。人工停用记录与已达到本地绑定上限的号码保留原状态，避免把人为禁用或无容量号码误投入任务。
- **修复历史混合号段未恢复**：新增启动后及每分钟一次的手机号池维护任务，扫描仍存在可用号码的历史号段并补齐同段号码状态。此前 `1803`、`1515` 等号段会显示为可用，但同段历史 `cannot_send` 或过期限流号码没有被实际复位。
- **手机号限流一小时后自动恢复**：手机号绑定的 `rate_limited` 冷却从不可恢复的状态残留改为明确一小时；维护任务和号码池读路径都会在到期后恢复 `active` 并清空冷却/最近错误，不再需要人工点击恢复可用。

### 优化 (Changed)
- **前端版本同步至 v2.1.4**：`frontend/src/app/AppShell.tsx` 更新侧栏版本，便于确认浏览器已加载号段状态回写与限流自动恢复逻辑。

### 测试 (Tests)
- **锁定号段传播与恢复契约**：扩展 `tests/test_phone_pool.py`，覆盖 OpenAI/收码 API 失败的整段停用、发码收码成功的整段恢复、历史混合状态对账、人工停用/已绑满保护以及一小时限流自动恢复。

## [2.1.3] - 2026-07-15

### 修复 (Fixed)
- **PIX 5xx 不再错误释放 CDK**：`api/tasks.py` 将上游 HTTP `5xx` 与传输中断统一归类为提交结果未知，写为待人工复核并保留跨实例占用；此前这类响应可能在上游已受理任务后被误判为明确失败并允许复用。`tests/test_baxigpt_cdk_pool.py` 锁定 502 场景，确认 CDK 状态为 `uncertain`。

### 优化 (Changed)
- **前端版本同步至 v2.1.3**：`frontend/src/app/AppShell.tsx` 更新侧栏版本，区分已上线的多 CDK 初版和本次失败关闭修复。

## [2.1.2] - 2026-07-15

### 新增 (Added)
- **PIX 多 CDK 队列**：`frontend/src/pages/Accounts.tsx` 的 PIX 提交框改为每行一个 CDK，单次最多 100 个；后端 `api/tasks.py` 以 CDK 队列调度账号。每张 CDK 同时只处理一个账号，明确失败后释放并继续尝试后续账号，支付成功后停止分配该 CDK。
- **跨常驻实例的成功核销登记**：新增 `core/pix_cdk_usage.py`，在共享配置 SQLite 中只保存带独立 HMAC 秘钥的 CDK 指纹及非敏感状态。Plus 与 Plus2 会原子占用同一 CDK，成功后永久标记为 `paid`；再次输入已核销、处理中或待复核的 CDK 会被后端拒绝，不会依赖前端判断。

### 安全 (Security)
- **保持 PIX 凭证零明文持久化**：多行输入、任务元数据、TaskLog、账号扩展字段和共享登记都不保存原始 CDK；登记只包含不可逆指纹、内部任务 ID 和上游任务 ID。浏览器关闭、通道切换或任务创建后均清空输入字段。
- **不确定结果继续失败关闭**：网络中断、未返回轮询凭据、轮询超时或任务中断时，CDK 进入待人工复核锁定而不自动释放；只有上游明确返回失败时才允许复用，避免把可能已经支付的 CDK 再次提交。

### 优化 (Changed)
- **PIX 成功目标按 CDK 数量收敛**：目标成功数量同时受候选账号数和可用 CDK 数限制；默认目标为每个可用 CDK 最多成功一次。任务结果继续分别展示账号失败、成功和待复核，不把中途失败的重试尝试误当作整批失败。
- **前端版本同步至 v2.1.2**：`frontend/src/app/AppShell.tsx` 更新侧栏版本，便于确认多 CDK 输入与核销规则已加载。

### 测试 (Tests)
- **覆盖失败复用与成功锁定**：扩展 `tests/test_baxigpt_cdk_pool.py`，验证两张 PIX CDK 并行调度、明确失败后复用、支付成功后跨任务拒绝重用，以及 CDK/status token 不泄露进任务快照。

## [2.1.1] - 2026-07-15

### 新增 (Added)
- **账号页 iDEAL / PIX 批量提交切换**：`frontend/src/pages/Accounts.tsx` 在原 iDEAL 批量提交窗口增加支付通道分段选择。PIX 模式按账号范围提交一次性 PIX CDK，复用现有目标成功数、失败继续和任务面板；iDEAL 仍使用原有卡密池、已保存卡密与手工导入流程。任务类型、工具栏、任务弹窗和结果汇总同步显示具体通道，避免将 PIX 当作 iDEAL 卡密订单。
- **对接 openai-pay-submit PIX 自动提链协议**：`services/chatgpt_core/baxigpt_client.py` 增加 `submit_pix()` 与 `pix_status()`，严格使用 `POST /api/task/submit` 的 `submitMode=pix_auto_extract`、单账号 `accounts` 和 `pixCdk` 请求体，再以 `GET /api/pix/tasks/status?task_id=&status_token=` 轮询上游终态。
- **PIX 独立异步执行器**：`api/tasks.py` 增加 `enqueue_pix_submit_task()` 与 `_run_pix_submit()`。每个账号只创建一个上游 PIX 任务，已受理任务可并行轮询；确认 paid 后仍以本地 ChatGPT 状态刷新结果作为账号主状态来源，并同步 `account_list_state`，不把上游 paid 直接伪装成订阅已确认。

### 安全 (Security)
- **PIX CDK 与轮询令牌不落盘**：PIX CDK 只在 API 入队到后台执行器的内存调用栈中传递；任务元数据、TaskLog、运行结果、`extra.baxigpt_cdk`、`extra.idea_submit`、历史字段和浏览器 localStorage 只保留固定 `PIX CDK` 标签及脱敏的任务 ID，绝不保存原始 CDK 或 `status_token`。账号页切换/关闭后不会复用密码字段。
- **不确定提交结果失败关闭**：网络中断、非 JSON 回应或上游未返回可轮询凭据时，任务和账号提交状态都标记为 `timeout` 待人工复核，不会自动以同一账号/CDK 重投；上游明确的账号资格失败才会写入现有 Idea 不可用标记，网络和临时错误不污染账号可用性。

### 优化 (Changed)
- **iDEAL 卡密池边界收敛**：导航和 `BaxiGptCdkPool` 页面明确为“iDEAL 卡密池”，PIX 模式隐藏库存、卡密选择、导入和额度告警，也不会调用 `BaxiGptCdkRepository` 或写入 `baxigpt_cdk_pool`。
- **前端版本同步至 v2.1.1**：`frontend/src/app/AppShell.tsx` 更新侧栏版本，便于确认浏览器已经加载 PIX 提交入口。

### 测试 (Tests)
- **覆盖 PIX 契约与敏感值边界**：扩展 `tests/test_baxigpt_cdk_pool.py`，验证 PIX 请求体和状态查询路径、每次提交仅携带一个 Access Token、成功后账号状态快照与结果汇总写入 `payment_channel=pix`，以及 PIX CDK、`status_token` 不出现在任务快照、TaskLog 或账号扩展字段；同时验证未知提交结果不会自动重投。

## [2.1.0] - 2026-07-14

### 新增 (Added)
- **新增独立 Phone API Relay**：在 `services/phone_api_relay.py` 提供独立的 Registry-backed 转发服务，公开 Origin 为 `https://phone-api.aa8.pl`，仅接受 `GET/HEAD`，以完整 raw `path + query` 的 SHA-256 作为路由键，使不同供应商域名可以共用同一转发域名而不改动原 API 路径。
- **手机号池保存源地址并生成实际请求地址**：数据库继续保存供应商 `api_url`，列表、诊断、绑定记录、注册记录、OAuth 二次验证和 OAIPay 输出同时提供 `source_api_url` 与 Relay 地址；Plus、Plus2 和 standby 启动/增删改/导入后同步各自库存 Registry。
- **Relay 管理与诊断界面**：`frontend/src/pages/PhonePool.tsx` 增加主域名、兼容旧域名、启停开关、Registry 同步状态、冲突提示、源域名/转发域名统计和默认复制实际 API 的抽屉配置。

### 优化 (Changed)
- **业务链路统一走转发地址**：OTP 轮询、API 到期探测、手机号注册、已绑定手机号 OAuth 二次验证、手工粘贴号码和动态号码池均在请求冻结时解析 Relay URL，保持原始路径、重复参数和编码字节不变。
- **手机号池列表整批复用转发配置快照**：`serialize_phone_pool_records()` 每个响应只读取一次 Relay Admin 配置，再为全部行派生源地址与转发地址；Relay 不可达时列表快速返回统一的 `unavailable` 状态，不再按号码数量串行累计控制面超时。严格注册/绑定路径继续独立解析并抛出 `api_forward_error`，不会把展示层快照当成“转发关闭”后回源。
- **兼容仅保存历史 Relay URL 的 OAIPay 记录**：`services/chatgpt_core/oaipay_upload.py` 会识别当前和兼容旧 Origin，将旧 Relay 链接迁移为当前主域名而不冒充供应商 `source_api_url`；如果转发已关闭且无法从手机号池恢复源地址则明确失败，避免双转发或错误回源。
- **多实例发布拓扑接入 Relay**：`docker-compose.multi.yml` 新增 `phone-api-relay` 服务（`127.0.0.1:8893`），Plus/Plus2 依赖 Relay healthy；`auto-gpt` standby 保留独立恢复能力；`deploy.sh` 增加 Relay 构建与健康检查。

### 安全 (Security)
- **Relay 关闭危险回源行为**：Relay 不可用、路由冲突、库存同步失败时不再静默直连供应商；以独立 `api_forward_error` 分类失败，号码保持 `active`，不污染 `cannot_send` 或号段不可用判断。
- **加入 SSRF 与协议边界**：注册源地址时阻断 loopback、RFC1918、link-local、metadata 和 Relay 自递归地址；连接时固定解析 IP、保留 HTTPS SNI、禁止上游重定向、限制超时和响应大小；日志只写 route hash、状态和耗时，不记录 query/token。
- **外部入口隔离**：Nginx `phone-api.aa8.pl` 仅公开 Relay 转发路径，`/admin` 管理接口直接 404，清空外部 Authorization/Cookie，仅允许 GET/HEAD 并关闭缓存。

### 测试 (Tests)
- **覆盖 Relay 与业务集成**：新增 `tests/test_phone_api_relay.py`、`tests/test_phone_api_forwarding.py`，并扩展手机号池、注册、绑定、OAuth、OAIPay 和任务集成测试；锁定批量列表单次读取配置、公共字段源/转发契约、注册 Relay 故障不误报空池、历史 Relay-only OAIPay 记录迁移与关闭时失败语义。完成 Python 编译、前端生产构建、Compose 渲染和 `git diff --check` 验证，前端和 ChatGPT 运行时版本同步至 `v2.1.0`。

## [2.0.0] - 2026-07-14

### 移除 (Removed)
- **下线 K12 产品能力与专用实例**：删除 K12 注册加入、空间抓取/重抓、批量任务、配置项、API、账号动作和前端设置，并从 `docker-compose.multi.yml`、`deploy.sh` 与运行拓扑中移除 `auto-k12` 及其 `8002/8319/8891` 端口。退役前已归档容器元数据、镜像、Compose 定义和 SQLite 一致性备份；`/opt/auto-k12` 原始数据继续作为冷数据保留，不做表删除或历史账号清理。
- **下线多 Workspace 与 Business 工作流**：移除 workspace variants/linked accounts、多空间抓取、Business workspace recovery、Team invite、延迟激活、pending invite API/任务和相关前端入口。普通 ChatGPT OAuth、支付与状态探测仍保留当前单账号必需的协议级 `account_id/workspace_id`，但不再枚举、加入或保存多个产品空间。
- **下线 Team 产品与支付能力**：删除 Team 页面、Team-lite API/服务、Team Manager 上传与内嵌运行时、Team 支付链接、席位/工作区名称/promo 参数及对应容器挂载和网络。主支付、GoPay、批量支付和流水线统一只允许 Plus；独立 `team-manage-app` 服务不属于本项目，继续独立运行。

### 优化 (Changed)
- **注册与账号持久化收敛为单账号契约**：ChatGPT 注册适配器和 RT/Auth 捕获不再生成 workspace artifacts 或额外账号变体；同一平台和邮箱只更新当前账号记录，不主动删除历史重复行。历史 `extra_json`、pending invite 表与旧 Team 数据库保持原样，仅停止被运行代码读取或写入。
- **Idea/OAIPay 流水线只保留当前套餐**：可编辑的跳过、Gate 与上传订阅列表仅保留 `free/plus/pro`；历史 Team/Business/Enterprise 账号由后端固定跳过 Idea、拒绝 Gate/手机号 Plus 分流，并在 OAIPay 统一写入边界于网络请求前失败关闭。旧配置载入时会过滤退役值，不修改任务历史或账号状态记录。
- **多实例发布拓扑同步收口**：常驻发布目标固定为 `auto-gpt-plus:8001` 与 `auto-plus2:8003`，`auto-gpt:8000` 保持停止的 standby；主发布入口移除会复活单实例的旧 image 模式，发布健康检查不再包含 K12。显式 `--backup` 仍只读备份现存历史 `team_manage.db`，但 Compose、启动逻辑和运行网络不再挂载或使用它。`auto-k12.cccy.me` 的 Nginx vhost 与 Cloudflare A 记录同步退役，独立 `k12.cccy.me` 和 `ex.cccy.me` 不受影响。
- **历史记录改为只读兼容展示**：账号详情只保留当前 OAuth 协议身份诊断；旧 K12/Business 任务仍可在历史页按“已退役”标签识别，但不提供重试、激活或管理操作，避免保留历史数据时被误判为当前能力。

### 安全 (Security)
- **封闭旧客户端绕过入口**：账号 Action、批量任务、GoPay 主/UID/批量入口和流水线均在账号扫描或任务调度前执行 Plus-only 校验，公共 GoPay 不再接受外部 `checkout_url`，历史 `team/business/enterprise` 缓存链接也不会被重标为 Plus 后复用。OAuth workspace 选择仅接受明确的 personal/free 候选，找不到时失败关闭，同时保留个人账号后续必需的 organization/project 协议步骤。OpenAPI 与共享配置允许列表不再暴露 K12、pending invite、Team-lite、Business capture 或延迟激活入口。
- **支付领取与 OAIPay 写入改为显式套餐证明**：外部领取接口只接收缓存元数据明确标记为 Plus 的 Hosted/PayPal 链接，裸 `cashier_url` 或缺失套餐元数据的历史记录不再默认归类为 Plus；OAIPay 的直接上传与所有上层入口共同拒绝当前或最后已知套餐属于 Team/Business/Enterprise 的账号。
- **解除 Team Manager 运行时耦合**：三个 Compose 入口均移除 Team Manager 代码/数据库只读挂载、外部网络和启动时数据库 bootstrap；Docker 构建上下文同步排除本机 `.bak*` 旧源码备份，避免已下线能力继续被带入发布镜像或通过容器内部依赖意外恢复。

### 测试 (Tests)
- **新增退役能力负向契约测试**：`tests/test_retired_capabilities_contract.py` 锁定 OpenAPI 路由、共享配置键、支付计划归一化与 GoPay UID 入口，防止 K12、Business、Team 或多 Workspace 产品面被后续改动误加回来；既有注册测试同步改为单账号 two-stage/Auth 契约。
- **覆盖构建、静态检查与 live smoke**：发布门禁包含 Python compileall、后端 pytest、前端 TypeScript/Vite 生产构建、Compose 渲染、Shell 语法和 diff 检查；上线后分别验证 Plus、Plus2、退役端口、OpenAPI、DNS 以及独立 K12/Team 服务边界。前端版本同步至 `v2.0.0`。

## [1.4.0] - 2026-07-13

### 新增 (Added)
- **接入 long-link 当前 PayPal 配置作为独立支付链接来源**：新增 `services/chatgpt_core/long_link_paypal_client.py`，通过 Docker 内网调用 `/opt/openai-pay-long-link` 的内部 profile、任务启动和任务轮询 API。auto-gpt 只提交账号 AccessToken、幂等请求 ID 与预期 profile hash；PayPal 国家、货币、代理链、浏览器指纹、账单地址和重试策略继续由 long-link 管理端当前配置统一决定，不在本项目复制协议代码或敏感配置。
- **单账号和批量提链统一支持 PayPal API**：`services/chatgpt_core/plugin.py`、`api/actions.py` 与 `api/tasks.py` 增加 `payment_source=long_link_paypal` 分流。账号动作弹窗和账号页批量弹窗可在“本地 Hosted / PayPal API”之间选择；选中账号和当前筛选范围均可创建后台批量任务。PayPal 批量保持串行处理，启动时冻结一次 profile，并为每个账号生成带实例命名空间的稳定幂等 ID，避免 Plus 与 Plus2 同时运行时发生跨实例碰撞。
- **PayPal 结果写入现有通用支付链接契约**：生成成功后写入 `cashier_url`、`extra.chatgpt_last_payment_link` 和 `extra.chatgpt_paypal_url`，并沿用 `pending_payment` 状态语义；不修改订阅结果、`used`、手机号/邮箱绑定或 Auth 捕获状态。PayPal API 只支持 Plus，Team 仍明确走本地 Hosted 生成器。

### 优化 (Changed)
- **按来源和配置版本隔离支付链接缓存**：`services/chatgpt_core/payment_link_cache.py` 为缓存加入 `payment_source` 与 `profile_hash`。PayPal API 链接不继承历史 Hosted 的代理、账单或来源元数据，profile 发生变化时旧链接不会被错误复用；批量 PayPal 默认强制生成新链接，本地 Hosted 的既有缓存、金额探测和 Team 行为保持不变。
- **前端按来源收敛参数和历史显示**：`AccountActionSurface.tsx` 与 `Accounts.tsx` 在 PayPal API 模式隐藏 Hosted 专属套餐、国家、货币、代理和 promo 参数，切回 Hosted 时恢复对应字段；历史 OaiPay 等 `paypal_url` 仅标记为“PayPal 链接”，不会冒充本次 long-link 的“PayPal API”来源。侧栏版本同步至 `v1.4.0`。

### 安全 (Security)
- **内部 Provider 使用独立密钥与漂移保护**：调用只使用 `OPENAI_PAY_LONG_LINK_API_KEY`，不复用 long-link 管理 Cookie、AES 密钥或管理员密码。每次任务要求匹配服务端 SHA-256 profile hash，客户端错误文本会遮蔽 AccessToken；远端任务默认最长等待 1800 秒，避免长流程被普通 HTTP 短超时提前切断。
- **幂等请求绑定实例和账号身份**：批量请求 ID 包含 `APP_INSTANCE_ID + task_id + account_id`；long-link 服务端同时校验不可逆 AccessToken 摘要与 profile hash。同 ID 被另一账号或另一配置误用时返回 `409`，不返回已有 PayPal 链接。

### 测试 (Tests)
- **覆盖 Provider 客户端、来源隔离和批量参数契约**：新增 `tests/test_long_link_paypal_client.py`、`tests/test_payment_link_sources.py`，并扩展 `tests/test_register_task_controls.py`，验证内部 API 轮询、错误脱敏、PayPal URL 校验、profile 缓存、Hosted/PayPal 缓存隔离、数据库写入边界、Team 拒绝、批量 profile 固定及实例级幂等 ID。
- **完成前端生产构建与后端回归**：覆盖单账号和批量 PayPal API 分流，同时保留本地 Hosted、注册任务控制和外部 PayPal 领取契约的既有行为；前端 TypeScript/Vite 构建用于验证来源切换及响应式弹窗未引入编译回归。

## [1.3.59] - 2026-07-11

### 新增 (Added)
- **ChatGPT 账号列表增加网页会话退出操作**：`services/chatgpt_core/web_logout.py` 按实际浏览器抓包实现 `GET /auth/logout.data?account_switch_action=logout` 后 `POST /api/auth/signout` 的完整退出链路。请求只携带已保存的 ChatGPT cookies 与 NextAuth CSRF token，不发送 AccessToken 或 RefreshToken；成功后 `api/actions.py` 原子移除当前账号本地 `cookies`、`session_token` 及兼容别名，同时保留 AT/RT，避免把“退出网页会话”误做成 OAuth 凭证撤销。
- **账号行“更多”动作增加显式确认**：`frontend/src/features/accounts/components/AccountActionSurface.tsx` 为“退出 ChatGPT 网页会话”提供不可跳过的确认弹窗，明确标注影响范围：网页 Cookie 会话会退出并清理本地副本，AccessToken/RefreshToken 不会被撤销或删除；失败时保留本地凭证，便于排障或重试。
- **前端版本同步至 v1.3.59**：`frontend/src/app/AppShell.tsx` 更新侧栏版本，用于确认浏览器已加载新的账号退出入口。

### 测试 (Tests)
- **覆盖退出接口凭证边界与请求顺序**：新增 `tests/test_chatgpt_web_logout.py`，验证完整 cookie + CSRF 才可执行、session token 仅作旧记录兼容、请求先走 logout data route 再走 signout，且 POST body 不包含 AT/RT。

## [1.3.58] - 2026-07-11

### 优化 (Changed)
- **注册尝试日志直接显示已完成成功数**：`api/tasks.py` 的账号尝试标题现在输出“尝试 N / 目标成功 M / 当前成功数 K”，其中 `K` 与任务进度使用同一调度器归集计数。串行注册在上一个账号的结果完成归集后，会先写入一条真正的空白日志再启动下一个账号；重复的“开始第 N 次尝试”日志已移除，使日志边界与实际账号流程一致。
- **空白日志完整穿透任务面板**：显式空日志不再被后端包装为带时间戳的伪空行；`TaskLogPanel` 的 SSE 消费会保留空字符串并前移日志游标，渲染层为其保留一个完整行高。因此实时流、任务快照和复制日志均保持相同的账号间空行，不会发生 SSE 重连重复或漏行。
- **前端版本同步至 v1.3.58**：`frontend/src/app/AppShell.tsx` 更新侧栏版本，用于确认浏览器已加载本次注册日志格式。

### 测试 (Tests)
- **覆盖串行账号日志边界**：扩展 `tests/test_register_task_controls.py`，验证两次串行成功尝试分别显示 `当前成功数 0/1`，且第一条成功日志与第二条标题之间保存真正的空字符串；同步更新注册日志等级断言以覆盖新的标题格式。

## [1.3.57] - 2026-07-11

### 修复 (Fixed)
- **修复账号页 TempMail Ready 注册任务无法创建**：`frontend/src/pages/Accounts.tsx` 现在在打开注册弹窗时从共享配置回填 `tempmail_mode`、`tempmail_fixed_domains` 和兼容的主域名；提交时会规范化并把用户选择的全部固定域名写入 `extra.tempmail_fixed_domains`，再用首个候选域名同步 `tempmail_primary_domain`。此前账号页虽然显示了域名选择器，但请求只携带空的旧单域名字段，固定域名模式会在 `/api/tasks/register` 入队前被 `400` 拒绝。
- **按建箱模式收敛 TempMail 弹窗门禁**：`frontend/src/features/auth/components/RegisterTaskModal.tsx` 仅在 `fixed_domain` 模式加载、展示并校验可用域名；`task_subdomain` / Ready 随机子域模式不再被固定域名必填项阻断，界面会明确说明无需选择域名。注册提交与 K12 配置保存的异常也被分开呈现，避免把注册字段错误误报为“保存 K12 配置失败”。
- **为旧客户端增加后端兼容回退**：`api/tasks.py` 在 TempMail 固定域名请求缺少候选域名时，会回退读取共享 `tempmail_fixed_domains`，并始终写回确定的 `tempmail_primary_domain`。这保留了旧弹窗/旧 bundle 的可用性，同时请求中显式传入的多域名仍优先于全局默认值。

### 测试 (Tests)
- **覆盖 TempMail 注册参数契约**：新增 `tests/test_tempmail_register_request.py`，验证全局多域名回退、请求多域名优先且完整保留，以及 `task_subdomain` 在无固定域名时可通过请求准备层；同时执行既有 `tests/test_register_task_controls.py`，确认相邻注册控制逻辑未回归。
- **发布前完成构建和多实例 smoke**：执行 Python 语法检查、定向 pytest、前端 TypeScript/Vite 生产构建，并在发布后验证 Plus 与 auto-plus2 的新静态包、健康接口和 TempMail 请求准备路径。

## [1.3.56] - 2026-07-11

### 优化 (Changed)
- **移除已选筛选组合的重复状态行**：`frontend/src/features/accounts/components/FilterPresetBar.tsx` 在选择了未修改的筛选组合时，不再于选择器下方重复显示“当前组合：名称”；下拉框和置顶快捷项原有的选中态继续作为唯一状态提示。未关联组合的手动筛选摘要、组合被改动后的“覆盖保存 / 另存为 / 还原”操作，以及移动端已选账号信息均保留，避免压缩布局时损失可执行操作或移动端上下文。
- **前端版本同步至 v1.3.56**：`frontend/src/app/AppShell.tsx` 更新侧栏版本，便于确认浏览器已加载账号页筛选组合布局收敛后的静态资源。

### 测试 (Tests)
- **完成前端生产构建与静态改动检查**：本次仅调整筛选组合摘要的渲染条件；发布前执行 TypeScript/Vite 生产构建和 `git diff --check`，确认无类型或空白错误。

## [1.3.55] - 2026-07-11

### 优化 (Changed)
- **筛选组合收敛为紧凑单行**：`frontend/src/features/accounts/components/FilterPresetBar.tsx` 将桌面端置顶筛选组合快捷项并入“筛选组合”选择器、管理入口所在的同一操作行；`frontend/src/index.css` 让快捷项只占用下拉框后的剩余空间并在组合过多时保留该区域的横向滚动，避免固定 180px 的选择框下方再产生独立空行。移动端原有筛选和组合展示逻辑不变。
- **批量操作显示取消三项上限**：`frontend/src/pages/Accounts.tsx` 不再在浏览器本地偏好读取、保存或勾选面板中截断固定操作；`frontend/src/features/accounts/components/AccountsToolbar.tsx` 同步移除渲染层二次截断。运营可直接固定任意数量的普通批量操作，桌面工具栏按既有换行规则承载，未固定操作仍在“更多操作”中可用，危险删除操作继续只留在该菜单中。
- **前端版本同步至 v1.3.55**：`frontend/src/app/AppShell.tsx` 更新侧栏版本，便于确认已加载本次账号页布局与操作偏好修复的静态资源。

### 测试 (Tests)
- **完成前端生产构建与改动检查**：`frontend npm run build` 已通过 TypeScript 编译和 Vite 静态资源构建，`git diff --check` 无空白错误。账号页历史 `Accounts.tsx` 仍存在既有 ESLint 债务；本次涉及的筛选组合、工具栏和侧栏组件未新增 ESLint 报错。

## [1.3.54] - 2026-07-11

### 优化 (Changed)
- **全局动态代理收敛为唯一配置源**：`core/proxy_utils.py` 将动态模式的正式配置固定为 `dynamic_proxy_template` 与 `dynamic_proxy_default_country`。显式单任务代理仍优先；历史 `task_proxy_url` / `task_proxy_country_code` 只在 canonical 字段为空时作为兼容回退，不再能悄悄覆盖动态模板或动态国家。`specified`、`pool`、`direct` 的原有后端语义保留。
- **全局设置按出口模式显示真实需要的字段**：`frontend/src/pages/Settings.tsx` 在动态模式只展示“动态代理模板 + 动态代理出口国家”及动态探测/SID 参数；指定代理和代理池字段只在相应模式显示。动态模式保存会校验模板与两位国家码，并明确清空历史 `task_proxy_*` 动态副本，防止条件隐藏字段继续随表单提交。
- **所有任务入口统一写入语义**：`frontend/src/lib/taskProxySettings.ts`、注册、账号批量同步、K12、手机号绑定、邮箱测活和代理页现在动态模式只写 canonical 动态字段。动态任务输入留空继续表示“沿用全局模板”，不会把正常模板清空；指定代理/代理池不再污染动态默认国家。相关页面文案同步为“全局默认”，不再把实际持久化行为伪称为仅本次覆盖。
- **受控历史迁移工具**：新增 `core/task_proxy_config.py` 与 `scripts/migrate_dynamic_proxy_config.py`。脚本默认 dry-run；apply 前执行共享 SQLite 完整性检查和在线备份，按 revision/CAS 归一化旧字段。若历史两组字段冲突，迁移保留升级前真正的 runtime 优先值，避免上线时无声切换出口；输出仅包含字段、长度和哈希摘要。

### 安全 (Security)
- **共享配置审计脱敏代理凭据**：`core/shared_config.py` 将 `task_proxy_url`、`dynamic_proxy_template` 及明确的代理 URL/模板字段纳入敏感值脱敏，审计 API 只返回存在性、长度和 SHA-256；历史 `diff_json` 在读取时立即遮蔽，并可由迁移脚本物理重写，避免代理用户名、密码和完整 URL 经 `/api/config/share/audit` 泄露。

### 测试 (Tests)
- **覆盖动态代理 canonical 优先、legacy fallback 与四模式兼容**：扩展 `tests/test_dynamic_proxy.py`，验证全局 dynamic 不再被旧 `task_proxy_*` 覆盖、旧配置仍可运行、specified 语义不回归、动态预览和自定义邮箱测活使用相同 fallback。
- **覆盖归一化和审计安全边界**：新增 `tests/test_task_proxy_config.py`、`tests/test_shared_config_proxy_redaction.py`，验证 legacy 提升、冲突保留旧 runtime、幂等迁移、非动态模式不误删，以及新旧共享审计均不回传代理凭据。

## [1.3.53] - 2026-07-11

### 修复 (Fixed)
- **陈旧终态任务页面的强制换代兜底**：`api/tasks.py` 为 `GET /api/tasks/active-summary` 增加有界、一次性的 legacy-poll fuse。当前前端统一通过 `apiFetch` 发送 task-poll protocol `2`；只有未携带该协议的旧 bundle、同一 `X-Real-IP + Bearer token` 摘要在 10 秒内连续请求 40 次以上、且运行时不存在任何 `pending/running` 任务时，服务端才返回一次 `401 CLIENT_REFRESH_REQUIRED`。旧版 `apiFetch` 会清理本地令牌并跳转到无缓存 `/login`，因此重新登录必然加载新 bundle，而不是让内存中的旧 React effect 无限打空摘要。计数器只保存 token SHA-256 截断摘要、5 分钟冷却并最多保留 512 个客户端；正常新页面、真实运行任务和普通 API 调用不受影响。
- **前端版本号同步至 v1.3.53**：`frontend/src/app/AppShell.tsx` 显示当前已包含终态轮询协议与陈旧页面熔断兼容的版本。

### 测试 (Tests)
- **覆盖旧页面轮询熔断边界**：新增 `tests/test_tasks_legacy_poll_guard.py`，验证门限触发只返回一次强制刷新、当前协议客户端和真实运行任务不受影响、不同 Bearer 会话隔离，以及 endpoint 返回的 `401/no-store` 契约。

## [1.3.52] - 2026-07-11

### 修复 (Fixed)
- **服务重启后的旧页面轮询熔断**：`api/tasks.py` 对已因进程重启丢失的 `task_*` 运行时任务返回短时可缓存的 `stopped` tombstone，而不是让旧版页面把 404 误判为临时网络失败并无限重试；已结束的内存任务同样返回私有缓存响应。任意非运行时格式的未知 ID 继续维持 404，任务控制与日志流的权限/存在性边界不变。新增 `tests/test_tasks_terminal_tombstone.py` 覆盖重启 tombstone、终态缓存和普通未知 ID 404。
- **前端版本号同步至 v1.3.52**：`frontend/src/app/AppShell.tsx` 更新侧边栏版本，用于确认终态轮询修复 bundle 已加载。

## [1.3.51] - 2026-07-11

### 修复 (Fixed)
- **根除 HME 邮箱恢复状态的全局配置复制**：`services/chatgpt_core/mailbox_state.py` 新增唯一的 v2 mailbox-state 契约，`services/chatgpt_core/plugin.py`、`core/base_mailbox.py` 的 `IcloudHmeMailbox.export_state_config()` 与旧 `platforms/chatgpt` 兼容入口均改为 provider/account 双层显式白名单。`hme_ready_api` 只保留 alias、lease/checkout、转发邮箱与 Helper/TempMail 恢复参数，强制排除 `icloud_cookie`、GoPay 批量任务、手机号池、筛选组合、流水线和任意未知全局配置；`before_ids` 同时限制为最多 128 项和 16 KiB，恢复前仍优先从实时邮箱刷新基线。
- **封死二次写入与嵌套复制面**：注册模式适配器、订阅 Auth 补抓、失效测活、手动邮箱测活、待激活邀请以及 iCloud HME 重跑队列在持久化前统一 sanitize；`chatgpt_invalid_recheck` / `chatgpt_custom_email_recheck` 结果只保存 provider/email/schema/before-count 摘要，不再把完整邮箱恢复状态复制进结果对象。`ManualTaskEmailService.export_state()` 不再把合并后的运行配置直接写入账号。
- **提供可审计的历史压缩迁移**：新增 `scripts/migrate_hme_mailbox_state.py` 与回归测试。脚本默认 dry-run、逐行 keyset 处理、拒绝在线 apply；正式 apply 会先完整 integrity check 和 SQLite 一致性备份，再在单事务/CAS 保护下将账户收敛为唯一 `chatgpt_mailbox_state`（缺失时从 legacy/invalid/custom 状态按优先级恢复），删除可安全识别的旧副本，最后 checkpoint、可选 VACUUM 和完整性复核。`pending_business_invites.mailbox_state_json` 继续保留一份受限的延迟激活快照。
- **终态任务轮询真正停止**：`frontend/src/pages/Accounts.tsx`、`frontend/src/components/TaskLogPanel.tsx` 与新增 `frontend/src/lib/taskStatus.ts` 统一状态归一化。终态 `done/failed/stopped/cancelled/completed/...` 不再重建任务详情 effect、不再重复拉取 active-summary，也会从账号快照监听集合移除；隐藏页面、关闭弹窗、切换任务和终态时会清理 timer、SSE 与 AbortController 请求，修复完成任务仍以 1–3 次/秒请求的根因。
- **降低无关全表读取的峰值**：`api/accounts.py` 的 `/api/accounts/stats` 改为 SQL 聚合，不再把每个 `accounts.extra_json` ORM 物化到 Python 仅为统计平台和状态数量。

### 安全 (Security)
- **多实例运行时资源护栏**：`docker-compose.multi.yml` 为主、Plus、Plus2、K12 容器设置 `mem_limit=2560m`、`mem_reservation=768m`、`memswap_limit=3072m`、`mem_swappiness=10` 和 `pids_limit=512`。这是数据修复后的隔离保险丝，不替代邮箱状态压缩；不会禁用 OOM killer，也未在未压测前压缩浏览器所需的 1 GiB shared memory。

### 测试 (Tests)
- **补齐污染与恢复回归覆盖**：新增 `tests/test_mailbox_state.py`、`tests/test_migrate_hme_mailbox_state.py`；扩展 `tests/test_chatgpt_plugin.py`、`tests/test_icloud_hme_mailbox_finalize.py`。覆盖 1 MiB 级 GoPay/手机号池/流水线污染不进入新状态、Helper 无 iCloud Cookie、身份字段与转发目标保留、before_ids 双上限、四个历史账户路径与 pending 表迁移、dry-run 零写入、备份/回滚/幂等/事务失败回滚及真实 HME exporter。

## [1.3.50] - 2026-07-10

### 修复 (Fixed)
- **消除全库 AT 导出的双重全表扫描**：`api/chatgpt.py` 的导出票据恢复为只保存筛选条件和模式，不再在创建票据时提前加载全量账号及解析完整 `extra_json`。实际下载阶段针对纯 AT 模式只查询 `token / extra_json` 两列并执行一次有序扫描，避免账号历史扩展字段较大时票据创建超时，同时维持非空 AT 一行一个和无 AT 明确报错。

## [1.3.49] - 2026-07-10

### 新增 (Added)
- **账号页导出新增纯 AccessToken 模式**：`frontend/src/features/accounts/components/AccountsToolbar.tsx` 将 ChatGPT 账号页“导出”改为保留原 Sub2API JSON 默认行为的模式菜单，新增“仅 AccessToken（每行一个）”。选中账号时只导出这些账号；未选中时沿用原有全库导出语义，方便直接批量交付 AT 文本。
- **导出接口支持 AT 文本与旧数据字段兼容**：`api/chatgpt.py` 的导出票据、直连和下载接口新增 `mode=access_token`，服务端只输出非空 AT、无标题/账号信息/其他凭证，每个账号一个独立文本行，并兼容 `access_token`、`accessToken`、`webAccessToken` 与旧主表 `token` 存储字段。无任何可用 AT 时在下载前直接返回明确错误；原 `sub2api` JSON 模式、票据时效和一次性下载语义不变。
- **新增独立的 Plus 副本实例 `auto-plus2`**：`docker-compose.multi.yml` 增加 `auto-plus2` 服务，复用 `auto-gpt:latest`、共享全局设置库、TempMail 与 Team Manager 网络，但使用独立的 `/opt/auto-plus2/{data,_ext_targets,external_logs}` 运行态目录与 `APP_INSTANCE_ID=auto-plus2`。服务固定映射 `8003 -> 8000`、`127.0.0.1:8892 -> 8889`、`8320 -> 8317`，避免与现有 Plus 实例的端口、SQLite 数据和运行日志混用。

### 优化 (Changed)
- **发布拓扑切换为双 Plus 常驻**：`deploy.sh` 现在只构建和升级 `auto-gpt-plus`、`auto-plus2`，并分别做 `8001/8003` 的健康检查和首页检查；显式保留 `auto-gpt`、`auto-k12` 为 standby 容器，发布完成后只停止它们，不删除容器、数据目录或挂载卷。热更新与可选发布前备份清单也同步覆盖 `auto-plus2`，避免后续发布再次误启动已下线实例。

## [1.3.48] - 2026-07-10
### 修复 (Fixed)
- **修复 Idea 多额度卡密被最后一笔订单状态锁死**：`services/chatgpt_core/baxigpt_cdk_repository.py` 新增提交候选读取规则，除普通 `available` 卡密外，会把本地仍记录 `remaining > 0` 的 `paid / failed` 终态卡密重新交给任务执行上游 `code-info` 校验；`api/tasks.py` 的 Idea 批量提交不再只按单行状态取卡，避免一张多额度卡最后一次订单写成 paid/failed 后，剩余额度在卡密池和任务中被永久忽略。`api/baxigpt_cdk_pool.py` 与账号页提交弹窗同步使用该候选集合，因此库存选择与实际任务取卡一致。
- **明确粘贴卡密被上游拒绝的真实原因**：Idea 预校验发现 `can_submit=false`、额度耗尽等情况时，`api/tasks.py` 现在把卡密掩码和上游原因写入任务错误及未提交账号原因，不再只显示泛化的“可用卡密额度不足”。例如“该卡密失败次数过多，已被风控限制”会直接可见，避免误判为粘贴内容没有读取。

### 测试 (Tests)
- **补齐可复用终态卡密回归覆盖**：`tests/test_baxigpt_cdk_pool.py` 覆盖 paid/failed 但仍有剩余额度的卡密进入 Idea 候选、已耗尽/人工停用卡密仍被排除，以及任务创建能使用终态但有余额的多额度卡。

## [1.3.47] - 2026-07-10
### 优化 (Changed)
- **Sub2API 与 OAIPay 补传改为只上传、不检测**：`services/sub2api_sync.py`、`services/oaipay_sync.py` 以及 `services/chatgpt_core/sub2api_upload.py`、`services/chatgpt_core/oaipay_upload.py` 移除上传前的远端存在探测、本地 ChatGPT 状态刷新、上传就绪 gate 和套餐网络探测；补传现在直接调用目标系统上传接口，由 Sub2API/OAIPay 自身完成最终账号检测。独立“状态同步”操作及 `probe_chatgpt_*_status()` 保持不变，上传成功/失败仍写回 `sync_statuses.last_upload`；无缓存失败使用 `remote_state=unknown`，单次上传失败不会抹掉已有远端存在记录。
- **远端补传菜单改用完整筛选范围计数**：`frontend/src/pages/Accounts.tsx` 不再用当前页账号估算 Sub2API、OAIPay、CLIProxyAPI 待补传数量或禁用操作，菜单明确展示当前筛选总数，实际待补传资格继续由后端在完整冻结集合内求交集，避免跨页账号被漏报。

### 修复 (Fixed)
- **修复账号页筛选任务静默扩大范围**：`services/account_filters.py` 新增九字段共享请求契约和统一 SQL resolver，`api/accounts.py`、`api/tasks.py`、`api/actions.py` 与 `api/integrations.py` 现在对邮箱、业务状态、使用状态、认证材料、订阅、认证状态、Sub2API、OAIPay、Idea 提交使用同一集合语义。前端 filtered 请求携带页面 `expected_total`，后端在任务创建、线程启动、手机号/CDK 导入和外部调用前校验；数量变化返回 `409 FILTER_SCOPE_CHANGED`，不会自动重试。selected 请求只按显式 `account_ids` 执行，不受残留筛选字段影响。
- **冻结并审计批量任务账号集合**：手机号绑定、补抓 Auth、K12 重跑、订阅链接、失效测活、本地/远端状态同步、Sub2API/OAIPay/CLIProxyAPI 补传、PayPal 绑定和 Idea 提交均先冻结完整匹配账号 ID，再让任务自身资格与 `limit` 只缩小集合；任务 meta 新增规范化筛选、expected/matched 数量、resolver 版本、完整匹配 ID 哈希和最终执行 ID 哈希，便于事故回放。
- **统一限流账号恢复与范围校验时序**：列表、filtered task/action 和同步补传入口都在解析筛选集合前归一化已到期的 `rate_limited` 状态，避免账号按旧状态通过数量校验后再改变状态；若恢复导致页面数量失效，将直接 409 要求重新确认。
- **保留 OAIPay 范围冲突确认现场**：OAIPay 上传确认框只在后端成功接受请求后关闭；发生 `FILTER_SCOPE_CHANGED` 时保留弹窗、刷新账号列表并通过 `AntdApp` 消息实例提示重新确认，不会静默吞掉拦截结果，也不会把失败请求伪装成已启动任务。

### 测试 (Tests)
- **补充筛选范围与只上传回归覆盖**：新增 `tests/test_filtered_task_scope.py`、`tests/test_integrations_backfill_scope.py`，并更新 `tests/test_sub2api_sync.py`、`tests/test_oaipay_sync.py`，覆盖九字段 schema、列表/任务集合一致、错误 expected_total 无任务副作用、限流恢复、selected 残留筛选隔离、pending-only 只缩小集合、上传不调用远端/本地探测及上传结果写回。统一定向回归 `122 passed`，前端 TypeScript/Vite 构建与 scoped ESLint 通过。

## [1.3.46] - 2026-07-10
### 优化 (Changed)
- **恢复 ChatGPT 账号页正确的信息归属**：`frontend/src/features/accounts/components/AccountsToolbar.tsx`、`frontend/src/pages/Accounts.tsx` 与 `frontend/src/index.css` 将 `总数 / 已选`移回状态同步所在的批量操作栏；桌面端依次展示统计、最多三个固定操作、更多操作、操作显示和显示字段，不再为显示控制创建独立卡片或额外纵向区块。跨页选中账号继续从 `selectedAccountSnapshots` 生成，已选 Popover、单项移除和清空选择语义保持不变。
- **恢复邮箱列内搜索并保留移动端唯一入口**：桌面端邮箱列标题重新承载 `Input.Search`，复用原有 `search / columnFilters.email / debouncedSearch`、300ms debounce 和筛选组合序列化；`<992px` 的账号卡片视图没有桌面表头，因此邮箱搜索仅保留在移动筛选卡内，任一断点均不会出现重复搜索入口。
- **压缩默认筛选区高度**：`frontend/src/features/accounts/components/FilterPresetBar.tsx` 在桌面无有效筛选时不再渲染“筛选：无筛选条件”空摘要，只保留筛选组合与置顶组合；存在有效筛选、已应用组合或组合 dirty 时仍展示摘要、覆盖保存、另存为与还原操作。

### 修复 (Fixed)
- **修复移动端显示控制伪装成独立设置卡的问题**：移动端批量操作折叠时，“操作显示”和“字段”仍可访问，但改为无边框的紧凑双按钮控制条；展开批量操作后继续使用既有两列/超窄单列操作网格，不产生标题为“显示设置”的独立面板。

### 测试 (Tests)
- **完成账号页浏览器回归**：通过临时真实数据预览验证 1440、1280、900、420px：桌面无 `.accounts-toolbar-settings`、邮箱表头仅一个搜索框、筛选组合卡无桌面搜索、统计在工具栏；移动端仅保留筛选卡邮箱搜索和紧凑控制条。桌面勾选一条账号后工具栏从 `已选：0` 更新为 `已选：1`，Popover 可展示选中邮箱；桌面与移动邮箱搜索均发起正确的 `/api/accounts?email=...` 查询。`npm run build`、组件 scoped ESLint、`git diff --check` 均通过；全量 ESLint 基线维持 `618 errors / 24 warnings`。

## [1.3.45] - 2026-07-10
### 优化 (Changed)
- **收敛账号页组件的类型边界**：`frontend/src/features/accounts/components/AccountsTable.tsx` 使用 Ant Design 表格泛型和账号记录类型替代表格列、账号记录、详情回调及表格变更回调上的隐式 `any`；`frontend/src/features/accounts/components/AccountsToolbar.tsx` 为运行中任务快照补充最小字段类型，保留未知扩展字段透传，降低账号页 UI 继续演进时的类型回归风险。

### 修复 (Fixed)
- **修复响应式侧栏的 effect lint 债务**：`frontend/src/app/AppShell.tsx` 移除移动断点变化时在 `useEffect` 中同步更新折叠状态的写法，改由 Ant Design `Sider` 的断点回调收起侧栏；移动端手动打开、菜单跳转后关闭和遮罩关闭行为保持不变，同时消除 `react-hooks/set-state-in-effect` 错误。

### 测试 (Tests)
- **完成前端 lint 与构建验证**：账号页目标组件 scoped ESLint 通过；全量 ESLint 从 `629 errors / 24 warnings` 降至 `618 errors / 24 warnings`，剩余问题为其他页面和历史模块债务；`cd frontend && npm run build` 通过，仅保留已有的 `page-accounts` 大 chunk 警告。

## [1.3.44] - 2026-07-10
### 优化 (Changed)
- **收敛 ChatGPT 账号页工具栏分区**：`frontend/src/features/accounts/components/AccountsToolbar.tsx` 与 `frontend/src/index.css` 将批量操作和显示设置拆成稳定的上下两行；桌面端不再让设置按钮与批量操作争抢同一行，操作按钮空间不足时可以正常换行，移动端按两列网格展示并在超窄屏切换为单列。
- **稳定账号列表统计摘要**：`frontend/src/features/accounts/components/FilterPresetBar.tsx` 固定展示 `总数 / 已选`，即使没有选择账号也显示 `已选：0`；仅在存在选择时提供已选账号 Popover 与清空入口，移动端脏筛选组合操作与统计区改为上下排列，避免互相挤压。

### 修复 (Fixed)
- **修复账号页响应式断点覆盖**：统一处理 `max-width: 768px` 与 `max-width: 991px` 下的批量操作规则，移除横向滚动和 `nowrap` 对操作按钮的隐藏风险，保持移动端批量操作折叠时显示设置仍可用。

### 测试 (Tests)
- **完成账号页 UI 回归验证**：通过 `cd frontend && npm run build`；使用 Chromium 检查 checkout preview 的 1440、1280、1024、900、768、420、390 宽度，确认桌面上下分区、移动端网格/单列、统计摘要和表格区域无新增横向溢出。


## [1.3.43] - 2026-07-09
### 优化 (Changed)
- **重构 ChatGPT 账号页顶部信息架构**：`frontend/src/features/accounts/components/FilterPresetBar.tsx` 升级为账号列表的查询与视图控制条，将邮箱搜索、筛选组合、操作显示、显示字段以及列表状态集中到同一层；原独立“已选账号”横条不再单独占用表格上方空间，选中账号数量改为与列表总数合并展示为 `总数 / 已选`，并保留点击查看已选账号、逐个移除与清空选择能力，避免跨页选择后无法确认或释放选中范围。
- **账号页批量操作支持按需固定与更多操作收纳**：`frontend/src/features/accounts/components/AccountsToolbar.tsx` 不再把状态同步、补抓 Auth、远端补传、K12 重跑、手机号/PayPal 绑定、Idea 提交、GoPay、Business 补激活和危险删除动作全部平铺在总数行；默认仅固定“状态同步”和“批量订阅链接”，其余动作进入“更多操作”菜单，并新增 `frontend/src/pages/Accounts.tsx` 的浏览器本地 `auto-chatgpt.accounts.toolbar-actions.v1` 偏好，让运营可像配置显示字段一样调整一级工具栏固定动作。
- **显示字段从批量操作区迁入视图控制区**：`frontend/src/pages/Accounts.tsx` 将原“列显示”入口迁到筛选组合旁并改名为“显示字段/字段”，面板补充“ID / 邮箱 / 操作固定显示”的说明，继续沿用 `auto-chatgpt.accounts.visible-columns.v2` 本地偏好，不写入筛选组合、不触发组合 dirty，保持筛选集合与浏览器视图偏好的边界。
- **移动端账号筛选避免与桌面语义漂移**：移动端筛选面板移除重复邮箱搜索，并把使用状态、业务状态、认证材料、订阅、认证状态、Sub2API、OAIPay、Idea 提交统一改为多选，避免桌面表头多选筛选在小屏只显示第一个值造成误解。

### 修复 (Fixed)
- **隐藏未完整接通的 Codex 状态筛选入口**：`frontend/src/pages/Accounts.tsx` 暂时移除移动端与筛选组合编辑弹窗中的 `Codex 状态`筛选项，并在筛选组合归一化时不再应用旧 payload 中的 `codexState`，避免界面展示“可筛选”但 `/api/accounts` 与当前筛选批量任务实际未携带 `codex_state` 的假筛选风险；Codex 用量列与状态展示本身不受影响。
- **同步前端版本号至 v1.3.43**：`frontend/src/app/AppShell.tsx` 侧边栏版本展示更新为 `v1.3.43`，用于上线后确认账号页 UI 收敛版本已加载。

### 测试 (Tests)
- **完成账号页前端构建验证**：已执行 `cd frontend && npm run build`，通过 TypeScript 编译与 Vite 产物构建；新增的查询/视图控制条、工具栏动作固定配置和移动端多选筛选均通过前端类型检查。


## [1.3.42] - 2026-07-09
### 新增 (Added)
- **全局设置新增账号网络默认出口**：`frontend/src/pages/Settings.tsx` 在注册设置中新增“账号网络默认出口”，统一维护 `task_proxy_mode / task_proxy_url / task_proxy_country_code / task_proxy_failover / task_proxy_max_candidates / task_proxy_min_score`；运营可在全局把 ChatGPT/OpenAI 账号网络动作切换为 `dynamic` 动态代理、`pool` 代理池、`specified` 指定代理或 `direct` 直连，默认值改为动态代理并优先使用 `dynamic_proxy_template` 与默认国家。

### 优化 (Changed)
- **账号网络动作默认从直连改为全局动态代理**：`core/proxy_utils.py` 新增 `resolve_default_chatgpt_proxy_with_metadata()` / `resolve_default_chatgpt_proxy()`，并让 `resolve_probe_candidate_proxies()` 默认读取全局 `task_proxy_*` 配置；显式传入代理时仍优先使用显式代理，避免旧 API 传 `proxy` 被全局动态代理覆盖。
- **单账号状态刷新与 Codex 额度刷新统一走默认出口**：`services/chatgpt_core/status_probe.py` 与 `services/chatgpt_core/codex_usage.py` 会在执行 RT 刷 AT、`/backend-api/me`、`accounts/check`、`wham/usage`、`codex/responses` 前解析全局默认代理，并在返回结构中写入 `network.proxy_used / proxy_source / proxy_error`，代理解析失败时返回 `proxy_resolve_failed` 而不是静默直连。
- **批量/自动账号状态同步沿用同一代理策略**：`services/chatgpt_core/local_status_refresh.py`、`api/tasks.py`、`api/actions.py`、`api/chatgpt.py`、`api/external_access_tokens.py` 将自动状态刷新、批量本地状态同步、K12 workspace 重跑、浏览器登录态捕获、RT 刷新、订阅链接/GoPay 链接、外部 AT 预检等 ChatGPT/OpenAI 账号网络动作切到全局默认出口；候选代理循环已显式传 `use_default_proxy=False`，避免同一次尝试二次解析代理。
- **注册、手机号绑定和邮箱测活默认跟随全局出口**：`api/tasks.py`、`services/chatgpt_core/plugin.py`、`services/chatgpt_core/access_token_only_registration_engine.py`、`services/chatgpt_core/refresh_token_registration_engine.py` 与 `services/idea_oaipay_pipeline/models.py` 不再把空代理模式落到直连/代理池，默认使用全局账号网络出口；注册后的 checkout/GoPay 链接也复用当前默认代理，不再绕回旧代理池路径。
- **前端任务代理默认值同步为动态代理**：`frontend/src/lib/taskProxySettings.ts`、`frontend/src/pages/Accounts.tsx`、`frontend/src/features/accounts/components/AccountActionSurface.tsx`、`frontend/src/pages/CustomEmailRecheckPage.tsx`、`frontend/src/pages/IdeaOaiPayPipeline.tsx` 与 `frontend/src/features/auth/components/RegisterTaskModal.tsx` 将状态同步、K12 重跑、注册、手机号绑定、邮箱测活等表单默认代理模式调整为动态代理，并继续从全局设置回填国家、候选数量和失败切换配置。
- **同步前端版本号至 v1.3.42**：`frontend/src/app/AppShell.tsx` 侧边栏版本展示更新为 `v1.3.42`，用于上线后确认默认账号网络出口改造版本已加载。

### 测试 (Tests)
- **补充默认代理回归覆盖**：`tests/test_dynamic_proxy.py` 新增全局动态代理、全局直连关闭和显式代理优先级测试；`tests/test_chatgpt_status_probe.py`、`tests/test_chatgpt_codex_usage.py`、`tests/test_chatgpt_plugin.py`、`tests/test_register_task_controls.py` 与 `tests/test_chatgpt_phone_registration.py` 更新测试夹具，覆盖状态探测、Codex 用量、单账号 Action、注册与手机号绑定在新默认代理语义下的行为。已通过 159 条定向回归、前端构建与 Python 语法检查。


## [1.3.41] - 2026-07-09
### 修复 (Fixed)
- **修复手机号绑定任务手动停止后仍占用右上角运行中任务**：`api/tasks.py` 的手机号绑定串行与并发 runner 在捕获 `StopTaskRequested` 时现在会同步调用 `_task_store.finish(status="stopped")`，再写入任务历史；避免任务日志已经记录“任务已手动停止”，但内存任务快照仍保持 `running + stop_requested=true + active_attempts=0`，导致账号页右上角“正在运行任务”长期显示幽灵任务。
- **修复 Idea 提交状态长期停在“提交中”**：`services/chatgpt_core/baxigpt_status_poller.py` 新增账号级 Idea 订单回填器，后台会扫描 `accounts.extra_json.baxigpt_cdk.status=submitted/processing` 且已过短暂冷却的账号，直接按账号保存的 `order_id`（或 `cdk_id + display_id` 回补出的订单号）查询上游终态，并把账号级 `baxigpt_cdk`、历史记录和 `account_list_state.idea_submit_state` 同步更新为 `paid / failed / processing`。这补齐了原来只按 `baxigpt_cdk_pool` 单行轮询的缺口：一个卡密可提交多个账号，但卡密池表只能保存最后一个订单，任务停止或重启后较早订单会失去 poller target，账号页就会错误地一直显示“提交中”。

### 优化 (Changed)
- **增强 BaxiGPT/Idea 轮询可观测性**：`/api/baxigpt-cdk-pool/poll` 的 poller 快照新增 `account_reconcile`，展示账号级回填器的下次执行时间、最近一次检查数、更新数、paid/failed/processing 分布和错误摘要，方便运营区分“上游仍在处理”和“本地漏同步”。
- **同步前端版本号至 v1.3.41**：`frontend/src/app/AppShell.tsx` 侧边栏版本展示更新为 `v1.3.41`，用于上线后确认本次任务状态与 Idea 提交状态修复版本已加载。


## [1.3.40] - 2026-07-09
### 新增 (Added)
- **手机号绑定任务支持账号级并发执行**：`api/tasks.py` 的 `PhoneBindingTestTaskRequest` 新增 `concurrency` 参数，手机号绑定任务会在 `concurrency > 1` 时启用并发 runner，同时处理多个账号；后端将请求并发数限制在 `1~5`，并在任务 `meta.settings`、`requested_concurrency`、`effective_concurrency` 中记录请求值、实际值和强制降级原因，方便任务详情回放。并发 runner 对手机号分配、结果列表、成功/跳过计数、任务快照同步使用线程锁，避免多个账号同时领取同一个上传号码或同一个手机号池候选。

### 优化 (Changed)
- **保留手机号绑定既有串行语义并对冲高风险模式**：默认并发仍为 `1`，现有串行 `_run_phone_binding_test()` 路径不变；只有用户显式设置并发大于 `1` 才进入新并发路径。开启“尽量用满同一个手机号”时，后端会强制实际并发降为 `1`，避免同号连续绑定与并发账号抢号冲突；当手机号数量不足以支撑请求并发时，也会按可用手机号数自动降级。并发路径继续保留短信探测模式、限定号段、号段抽样、代理失败切换、绑定后 Auth/RT 三次补抓和 `chatgpt_phone_binding` 持久化语义。
- **账号页手机号绑定弹窗新增并发配置**：`frontend/src/pages/Accounts.tsx` 在“手机号绑定 > 参数设置”里新增“并发数”输入，复用现有高密度表单风格，提示建议值 `2-3` 与后端硬上限 `5`；当“尽量用满同一个手机号”开启时，前端自动把并发锁定为 `1` 并禁用输入，避免界面允许提交后端必然降级的组合。侧边栏版本号同步更新为 `v1.3.40`。

### 测试 (Tests)
- **补充手机号绑定并发回归测试**：`tests/test_phone_pool_task_integration.py` 覆盖并发参数写入任务 meta、响应体回传和同号连续绑定强制串行降级；`tests/test_phone_binding_assignment.py` 新增并发 runner 测试，验证两个并发账号会领取不同的手动上传手机号并完整写入绑定结果。已通过 `tests/test_phone_pool.py`、`tests/test_phone_pool_task_integration.py`、`tests/test_phone_binding_assignment.py`、`tests/test_chatgpt_task_logging.py` 定向回归和前端 `npm run build`。


## [1.3.39] - 2026-07-08
### 新增 (Added)
- **手机号/API 导入兼容管道分隔格式**：手机号池、手机号绑定、手机号注册和 Idea 提交流程的手工号码输入均兼容 `+手机号|https://...` 供应商原生格式，并继续统一保存为既有 `手机号----收码API` 规范行，避免新格式号码无法导入池或无法进入绑定/注册任务。

### 修复 (Fixed)
- **兼容 sms24.uk 收码 JSON 协议**：上传手机号收码轮询与手机号池 API 到期探测均支持 `expireTime` 字段；当接口返回 `{"code":false,"message":"暂无短信","expireTime":"..."}` 时按“暂无验证码”继续轮询并记录固定到期时间，当后续 `code=true` 且短信内容在 `message` 中时可提取 OpenAI 验证码。

### 测试 (Tests)
- **补充手机号/API 新格式回归测试**：`tests/test_chatgpt_phone_flow.py` 覆盖 `手机号|API` 解析、sms24 JSON 暂无短信与验证码提取；`tests/test_phone_pool.py` 覆盖手机号池导入管道分隔行和 `expireTime` 到期时间刷新。前端侧边栏版本号同步更新为 `v1.3.39`。


## [1.3.38] - 2026-07-08
### 新增 (Added)
- **账号级浏览器指纹标准化持久化**：新增 `services/chatgpt_core/account_fingerprint.py`，将注册尝试中的 `chatgpt_browser_fingerprint` 统一提升为账号级字段，保存 `chatgpt_browser_fingerprint / signature / source / saved_at`；`api/tasks.py`、`services/chatgpt_core/chatgpt_registration_mode_adapter.py`、`access_token_only_registration_engine.py`、`refresh_token_registration_engine.py` 与 `phone_registration_engine.py` 均在账号保存前写入完整指纹，保证新注册账号不只保存签名而是保存可复用的完整浏览器画像。

### 优化 (Changed)
- **后续任务优先复用账号注册指纹**：`services/chatgpt_core/subscription_auth_capture.py`、`invalid_account_recheck.py`、`custom_email_recheck.py`、`pending_business_invites.py`、`k12_recapture.py` 和 `api/chatgpt.py` 统一从账号顶层字段或历史 `chatgpt_registration_context.browser_fingerprint` 解析指纹并注入运行配置；手机号绑定后的 Auth/RT 补抓、补抓 Auth、失效测活、自定义邮箱测活、pending invite 激活、K12 重跑和浏览器登录态捕获都遵循“账号间隔离、账号内稳定”的同一指纹语义。
- **保存账号时防止指纹被后续更新冲掉**：`core/db.py::save_account()` 新增账号级指纹保护，更新同邮箱/同 workspace 账号时优先保留已存在的账号指纹；如果新 payload 缺少顶层指纹但历史上下文里已有指纹，会自动回填到顶层字段，避免补抓 Auth、测活或二阶段注册保存时把注册指纹抹掉。
- **强制独立出口 IP 增加创建前硬校验**：`api/tasks.py::_prepare_register_request()` 在任务创建阶段拒绝 `强制独立出口 IP + 直连`，并拒绝 `批量注册 + 单个指定代理 + 未开启失败切换` 的组合；前端注册弹窗和独立注册页同步展示错误级提示，保存/提交注册配置时会把 `chatgpt_register_unique_exit_ip_enabled` 写入 `/config`，避免 UI 默认值与后端运行策略漂移。

### 修复 (Fixed)
- **恢复支付流水调度的平台查找兼容钩子**：`services/pipeline/payment_scheduler.py` 恢复 `get("chatgpt")` 形式的平台构造入口，保持支付链接准备流程与既有测试/扩展的 monkeypatch 约定兼容，避免全量回归中支付链接生成测试无法替换平台实例。

### 测试 (Tests)
- **补充账号级指纹与出口 IP 校验回归测试**：新增 `tests/test_account_fingerprint.py` 覆盖顶层持久化、历史 `registration_context` 回填、保存时保留已有账号指纹和运行配置注入；`tests/test_register_task_controls.py` 补充强制独立出口 IP 的非法代理组合校验，并确认注册任务保存账号时已写入完整 `chatgpt_browser_fingerprint`；全量 `pytest tests -q` 覆盖支付流水调度兼容回归。前端侧边栏版本号同步更新为 `v1.3.38`。

## [1.3.37] - 2026-07-08
### 新增 (Added)
- **注册任务新增“强制独立出口 IP”开关**：`frontend/src/features/auth/components/RegisterTaskModal.tsx` 与 `frontend/src/pages/RegisterTaskPage.tsx` 在 ChatGPT 注册代理配置区新增 `chatgpt_register_unique_exit_ip_enabled`。开启后，`api/tasks.py` 会在每个注册尝试进入核心链路前探测真实出口 IP，并在同一注册任务内记录已分配出口；动态代理会自动扩大 sid 刷新候选，代理池会切换候选，撞到已分配 IP 时跳过当前候选并写入任务日志与 `register_unique_exit_ip` 任务 meta。
- **注册结果面板展示独立出口 IP 分配状态**：任务快照新增 `register_unique_exit_ip.assigned_count / collision_count / failed_count / assigned_exit_ips / events`，注册弹窗和独立注册页会显示已分配出口数量与撞 IP 次数，方便确认并发注册时是否真的换到了不同出口。

### 优化 (Changed)
- **注册链路强制注入任务级独立浏览器指纹**：`api/tasks.py` 为每个 ChatGPT 注册尝试生成独立 `chatgpt_browser_fingerprint`，并通过 `services/chatgpt_core/refresh_token_registration_engine.py`、`access_token_only_registration_engine.py`、`phone_registration_engine.py` 和 `phone_signup_client.py` 传入底层客户端；同一账号流程内继续复用同一指纹，账号与账号之间避免复用 UA / viewport / Accept-Language / device_id 组合。
- **动态代理注册参数补齐 retention 透传**：`RegisterTaskRequest` 新增 `dynamic_proxy_ip_retention_minutes`，注册任务现在会把前端动态代理保留时间传给 `core.proxy_utils.resolve_task_proxy_candidates()`，避免动态代理配置只在前端保存但注册链路未使用。

### 测试 (Tests)
- **补充并发注册出口隔离回归测试**：`tests/test_register_task_controls.py` 覆盖两个并发注册同时拿到相同首选动态代理候选时，第二个尝试会跳过重复出口并切到下一个候选，同时确认每个尝试都收到独立浏览器指纹；前端侧边栏版本号同步更新为 `v1.3.37`。

## [1.3.36] - 2026-07-08
### 新增 (Added)
- **注册面板新增“遇到已注册邮箱时路由到登录”开关**：`frontend/src/features/auth/components/RegisterTaskModal.tsx` 与 `frontend/src/pages/RegisterTaskPage.tsx` 在 ChatGPT 注册配置区新增独立开关 `chatgpt_existing_account_login_route_enabled`，默认开启以兼容旧任务；关闭后，注册状态机发现邮箱已存在或被 OpenAI 推入登录路径时，会直接跳过当前邮箱、不保存到库存，避免 HME/邮箱池中的历史 OpenAI 账号混入“新注册”库存。
- **任务结果记录已注册邮箱处理明细**：`api/tasks.py` 新增 `existing_account_login_routes` 任务 meta 记录，成功路由到登录恢复和被策略跳过的邮箱都会写入任务日志与任务快照；注册弹窗和独立注册页会显示“已路由 / 已跳过”的邮箱、数量和触发原因，便于回放排查。

### 优化 (Changed)
- **注册状态机显式识别“未创建账号却完成回调”的登录恢复路径**：`services/chatgpt_core/chatgpt_client.py` 在 `register_complete_flow()` 中记录 `last_registration_route_event`，当邮箱 OTP 后未经过密码注册/资料创建却直接回到 ChatGPT，或进入 `log-in/password` 时，不再把它当作干净新注册成功，而是返回 `user_already_exists` 标记交给上层策略处理。
- **RT 与无 RT 注册链路统一已有账号策略**：`services/chatgpt_core/refresh_token_registration_engine.py` 和 `services/chatgpt_core/access_token_only_registration_engine.py` 统一读取新增开关；开启时继续走现有登录恢复并保存，关闭时抛出可识别的跳过中断，任务统计计入“跳过”而不是误报注册成功。

### 测试 (Tests)
- **补充已有账号路由策略回归测试**：`tests/test_chatgpt_register.py` 覆盖 RT 注册遇 `user_already_exists` 时默认登录恢复、关闭开关时跳过不调用 OAuth，以及邮箱 OTP 后未创建账号直接 callback 的识别；`tests/test_access_token_only_checkout.py` 覆盖无 RT 注册传递开关、关闭后跳过、开启后通过 OAuth 登录恢复并记录 metadata。前端侧边栏版本号同步更新为 `v1.3.36`。

## [1.3.35] - 2026-07-08
### 新增 (Added)
- **Idea 批量提交窗口新增“查询全部剩余”按钮**：`frontend/src/pages/Accounts.tsx` 在账号页 “idea批量提交” 弹窗的卡密来源区域新增主动刷新动作，先读取 `/api/baxigpt-cdk-pool` 的全部 CDK，再分批调用 `/api/baxigpt-cdk-pool/quota` 执行 `code-info/query` 校验并回写本地库存；完成后自动刷新可用卡密列表、库存摘要和剩余额度展示，解决原“刷新库存”只读取本地缓存、不能实时查询所有 CDK 剩余次数的问题。
- **Idea 批量提交支持本次目标成功数量**：`frontend/src/pages/Accounts.tsx` 在弹窗顶部新增“本次目标成功数量”输入框，`api/tasks.py` 的 `BaxiGptCdkSubmitTaskRequest` 同步新增 `target_success_count`；任务运行时会按目标 paid 数控制新账号提交窗口，达到目标后停止继续提交剩余候选账号，并在任务 summary 中把未继续提交的账号标记为“已达到本次目标成功数量”，避免一次选择大量账号时超量开通。

### 优化 (Changed)
- **放大 Idea 提交卡密选择控件并强化目标/额度提示**：`frontend/src/pages/Accounts.tsx` 将 “使用已保存卡密” 多选框调整为 large 尺寸、提高下拉列表高度并放宽卡密/备注展示宽度；弹窗宽度同步从 820 调整到 900，并在选择区展示目标成功数量、可提交额度和本轮后预计剩余，减少长卡密与备注被截断导致的误选。

### 测试 (Tests)
- **补充目标成功数量回归测试**：`tests/test_baxigpt_cdk_pool.py` 覆盖 Idea 提交任务创建时保存 `target_success_count` / `effective_target_success_count`，以及运行时达到目标 paid 数后不再继续提交后续候选账号，防止后续调整再次出现目标数失效或超量提交。

## [1.3.34] - 2026-07-08
### 新增 (Added)
- **Idea 批量提交窗口支持选择已保存卡密并展示剩余额度**：`frontend/src/pages/Accounts.tsx` 在账号页 “idea批量提交” 弹窗中新增“使用已保存卡密”多选区，读取 `/api/baxigpt-cdk-pool?status=available` 的可用卡密，直接展示卡密、备注、剩余额度与预计本轮提交后的剩余量；不选择时沿用全部可用卡密，选择后只使用指定卡密。`api/tasks.py` 的 `BaxiGptCdkSubmitTaskRequest` 新增 `cdk_ids`，提交任务创建阶段会调用 `BaxiGptCdkRepository.list_available(ids=...)` 限定卡密来源，避免运营在弹窗里选了卡密但后端仍从全池自动取用。
- **Idea 批量提交窗口新增“保存到卡密池”动作**：`frontend/src/pages/Accounts.tsx` 的粘贴卡密区域增加独立保存按钮，调用 `/api/baxigpt-cdk-pool/import` 将卡密先入库并自动刷新库存/可选列表；保存成功后自动切换回卡密池模式并选中新入库卡密，避免关闭弹窗或下次打开时粘贴卡密丢失。直接点“开始提交”仍兼容原有“先导入再提交”的链路。

### 优化 (Changed)
- **简化账号列表 Idea 提交筛选语义**：`frontend/src/pages/Accounts.tsx` 将账号列表和筛选组合里的 Idea 提交状态收敛为 `未提交 / 不可用 / 提交中 / 已开通 / 提交失败` 五类，不再暴露 “未标记不可用 / 已提交 / 处理中” 这类容易混淆的内部状态。`services/account_filters.py` 对 `unsubmitted` 映射旧 `available`，对 `submitting` 同时映射 `submitted + processing`；`api/accounts.py` 同步规范化旧筛选组合里的 `available/submitted/processing`，保证历史组合继续可用但新界面只展示清晰分类。

### 测试 (Tests)
- **补充 Idea 筛选与指定卡密提交回归测试**：`tests/test_account_filters.py` 覆盖 `unsubmitted/submitting` 新语义在 Python 与 SQL `account_list_state` 过滤中的兼容性；`tests/test_account_filter_presets.py` 覆盖旧 `available/submitted/processing` 筛选组合自动归一；`tests/test_baxigpt_cdk_pool.py` 覆盖 `cdk_ids` 只使用选中卡密且库存不足时剩余账号进入未提交列表。

## [1.3.33] - 2026-07-08
### 修复 (Fixed)
- **修正 Helper Ready 出库后的验证码监听范围**：`core/base_mailbox.py` 调整 `helper_ready_api` 模式下的 TempMail 转发箱候选选择逻辑；当 Helper 返回 `forward_mailbox_id` 或明确 `forward_to` 时，只监听该目标转发收件箱，不再继续追加扫描 `icloud_forward_to` 配置里的全部转发邮箱，避免单个注册任务制造不必要的多邮箱轮询和跨收件箱噪音。只有 Helper/历史账号状态缺失明确转发目标时，才回退扫描当前配置的全部转发邮箱。
- **保留 Helper 任意池出库但区分监听目标**：`_helper_get_email()` 继续以 `forward_to="*"` 向 Helper 领取任意 ready alias，保证库存不被某个默认转发邮箱硬限制；领取结果中存在真实转发邮箱时会写入任务状态并在日志里显示目标，缺失时才标记为“回退扫描全部配置转发箱”，让出库范围与收码监听范围不再混淆。

### 测试 (Tests)
- **补充 HME Helper 转发箱范围回归测试**：`tests/test_icloud_hme_mailbox_finalize.py` 覆盖带 `forward_mailbox_id`、仅带 `forward_to`、以及完全缺失显式转发目标三种场景，确保有目标时只扫目标、无目标时才扫全部配置邮箱；前端侧边栏版本号同步更新为 `v1.3.33`。

## [1.3.32] - 2026-07-08
### 新增 (Added)
- **账号列表新增“已标记不可用于 Idea 提交”筛选能力**：`services/account_filters.py` 在 `account_list_state` 派生筛选缓存中新增 `idea_submit_state` 非敏感索引列，并同步支持 `available / unavailable / paid / submitted / processing / failed` 状态筛选；`api/accounts.py` 的 `/api/accounts` 增加 `idea_submit_state` 查询参数，能够直接筛出 `extra.idea_submit.unavailable=true`、`extra.idea_submit_unavailable=true` 以及旧兼容标记 `chatgpt_account_unavailable=true + baxigpt_cdk.status=failed` 的账号，避免 Idea 批量提交失败后只能靠肉眼查看标签。
- **账号页 Idea 提交列接入筛选下拉与筛选组合**：`frontend/src/pages/Accounts.tsx` 将“Idea提交”表头升级为可筛选列，移动端筛选区和筛选组合编辑弹窗同步新增 “Idea 提交”条件；`frontend/src/features/accounts/hooks/useAccountsQuery.ts` 会把当前条件作为 `idea_submit_state` 发送给后端，批量任务的“当前筛选账号”范围也会继承该条件，保证列表显示、筛选组合和批量操作口径一致。

### 测试 (Tests)
- **补充 Idea 提交状态筛选回归测试**：`tests/test_account_filters.py` 覆盖 Python 行过滤、SQL `account_list_state` 过滤、旧不可用标记兼容和缺失列自动升级；`tests/test_account_filter_presets.py` 覆盖筛选组合保存 `ideaSubmitState=["unavailable"]` 不被规范化逻辑丢弃。前端侧边栏版本号同步更新为 `v1.3.32`。

## [1.3.31] - 2026-07-08
### 优化 (Changed)
- **统一 iCloud HME 验证码读取链路为 TempMail 转发箱轮询**：`core/base_mailbox.py` 将 `helper_ready_api` 模式下的新 HME Ready 出库账号也纳入 TempMail 转发收件箱扫描，Helper 只负责领取/出库 HME alias 与保留 lease 结果归档，不再通过 `/api/hme-ready/mailboxes/{lease_id}/wait-code` 承担验证码等待主链路；新账号优先使用 Helper 返回的 `forward_mailbox_id/forward_to`，缺失绑定时回退扫描当前配置的全部 `icloud_forward_to` 转发邮箱。
- **保留旧账号与新出库账号一致的收码规则**：旧账号列表中的 iCloud HME alias 继续按已有邮箱地址去 TempMail 里匹配 `received_for` 或原始邮件头，带历史 `forward_mailbox_id` 时优先扫描该收件箱，避免账号测活依赖 Helper checkout 状态；新 Helper 出库账号同样只按当前 alias 匹配邮件验证码，防止跨转发箱误用其他账号邮件。

### 测试 (Tests)
- **补充 Helper Ready 转发箱读码回归测试**：`tests/test_icloud_hme_mailbox_finalize.py` 新增覆盖 `helper_ready_api` 模式下 `wait_for_code()` 与 `get_current_ids()` 均调用 TempMail 转发箱，而不会调用 Helper 的 `wait-code/list-emails` 读信接口；同步前端侧边栏版本号至 `v1.3.31` 便于上线确认。

## [1.3.30] - 2026-07-08
### 修复 (Fixed)
- **修复旧 iCloud HME 账号复查误用 Helper Ready lease 导致验证码等待刷屏**：`services/chatgpt_core/pending_business_invites.py` 在恢复历史 `chatgpt_mailbox_state` 时识别旧 `icloud_hme` 状态中保存的 Apple anonymous_id，当前全局邮箱模式为 `helper_ready_api` 但账号没有明确 `lease_id/checkout_id` 时自动切回 `import_pool` 转发邮箱扫描，避免把 `m5...` 这类匿名 ID 当成 `/api/hme-ready/mailboxes/{lease_id}/wait-code` 的 checkout lease 使用。
- **HME Ready 缺失租约错误改为致命邮箱配置错误**：`services/chatgpt_core/oauth_client.py` 将 `HME Ready API status=404 checkout_id 或 alias_id 不存在`、`lease_not_found`、`alias_not_found` 与 `helper_ready_api 当前任务缺少 lease_id` 归类为不可重试错误，立即停止本轮 OTP 等待并输出明确失败原因；非致命邮箱异常增加短退避，防止远端即时错误把任务日志刷爆。
- **恢复旧转发邮箱多目标扫描能力**：`core/base_mailbox.py` 重新保留 `icloud_forward_to` 的多地址列表，旧账号使用 `import_pool` 恢复收码时会按 `b@cccy.me,b@666800.xyz,...` 逐个解析并扫描对应 TempMail 收件箱，不再因构造器固定 `*` 而无法做本地转发邮箱扫描。

### 测试 (Tests)
- **补充 HME Ready / 旧 HME 状态回归测试**：`tests/test_pending_business_invites.py` 覆盖旧 `icloud_hme` 状态在全局 helper 模式下回退到 `import_pool`，以及带显式 lease 的 helper 状态继续走 Helper Ready；`tests/test_icloud_hme_mailbox_finalize.py` 覆盖多 forward 地址保留和旧 anonymous_id 不再被当作 helper lease；`tests/test_oauth_mailbox_errors.py` 覆盖 HME Ready 404 与缺失 lease 的致命错误判定。
- **同步前端版本号至 v1.3.30**：`frontend/src/app/AppShell.tsx` 侧边栏版本展示更新为 `v1.3.30`，用于上线后确认验证码等待刷屏修复版本已加载。

## [1.3.29] - 2026-07-08
### 优化 (Changed)
- **内置账号筛选组合允许直接维护**：`api/accounts.py` 将 `/api/accounts/filter-presets` 的内置组合从只读常量升级为“默认模板 + 本实例持久化覆盖”的模型，允许对 `builtin_*` 组合执行 `PUT` 修改名称、描述、置顶状态及完整筛选条件；同时支持 `DELETE` 将指定内置组合从当前实例隐藏，避免运营侧必须复制成自定义组合才能调整默认 OAIPay/Sub2API 快捷筛选。配置仍保存在本实例 `chatgpt_account_filter_presets`，新增 `version=2` 结构兼容旧的自定义组合列表，保留主服务、Plus、K12 三实例隔离语义。
- **账号页管理弹窗开放内置组合编辑/覆盖/删除**：`frontend/src/pages/Accounts.tsx` 取消内置组合只能复制的前端限制，“筛选组合管理”中所有组合统一提供“编辑 / 覆盖条件 / 复制 / 删除”；应用内置组合后如继续手动调整条件，`frontend/src/features/accounts/components/FilterPresetBar.tsx` 也会显示“覆盖保存”，直接回写该内置组合在当前实例的覆盖配置。内置组合的“置顶到账号页快捷筛选”现在按保存值生效，不再因 `built_in` 标记强制常驻快捷按钮。
- **同步前端版本号至 v1.3.29**：`frontend/src/app/AppShell.tsx` 侧边栏版本展示更新为 `v1.3.29`，用于上线后确认内置筛选组合可维护版本已加载。

### 测试 (Tests)
- **补充内置筛选组合可维护回归测试**：`tests/test_account_filter_presets.py` 更新原先“内置不可变”的断言，覆盖内置组合可更新、删除后从列表隐藏、二次删除返回 404，并补充旧版纯列表配置仍可读取的兼容性测试。

## [1.3.28] - 2026-07-07
### 优化 (Changed)
- **重构账号列表中“当前订阅 / 历史订阅 / 认证状态”的语义边界**：`services/chatgpt_account_state.py` 不再把上一次 `chatgpt_capabilities.subscription_plan` 直接兜底写回当前订阅；当本次本地刷新结果为 `unknown`、探测失败或认证失效时，当前订阅保持 `unknown`，并单独输出 `last_known_subscription_plan`、`subscription_refresh_state` 与 `subscription_plan_stale`，避免历史 Plus 被误当作当前 Plus 参与筛选、交付或上传判断。
- **账号筛选缓存改为只按当前确认订阅入库**：`services/account_filters.py` 的 `account_subscription_type()` 与 `account_list_state.subscription_type` 只接受当前刷新明确返回的订阅计划，或 `subscription_checked=true` 的确认快照；历史订阅、`workspace_scope=free/business` 与旧 `chatgpt_plan_type` 不再混入当前订阅筛选。`account_validity` 同步扩展为 `valid / invalid / refresh_failed / not_checked`，让网络失败、未验证和认证失效分开表达；`account_list_state.derivation_version` 会在语义规则升级后强制刷新旧缓存，避免线上旧 `Plus` 缓存继续污染筛选结果。
- **账号列表 API 增加订阅刷新状态与上次确认订阅摘要**：`api/accounts.py` 的紧凑列表序列化新增 `last_known_subscription_plan`、`subscription_refresh_state`、`subscription_plan_stale`，并在 `subscription` 摘要中返回 `last_known_plan / refresh_state / stale`；`account_validity_summary` 现在会区分 `auth_invalid`、`codex_auth_invalid`、`probe_failed` 与 `not_checked`，前端和外部调用方可同时看到当前事实与历史线索。
- **账号页列名与筛选文案按职责重命名**：`frontend/src/pages/Accounts.tsx` 将“认证类型”调整为“认证材料”、“账号状态”调整为“业务状态”、“订阅类型”调整为“当前订阅”、“账号有效性”调整为“认证状态”；订阅列在当前不可确认但存在历史订阅时显示“待刷新 / 不可确认”并附带“上次 Plus/Free”等副文本，筛选组合摘要和编辑弹窗同步使用新语义。
- **同步前端版本号至 v1.3.28**：`frontend/src/app/AppShell.tsx` 侧边栏版本展示更新为 `v1.3.28`，用于上线后确认账号订阅状态语义修正已加载。

### 测试 (Tests)
- **补充订阅刷新语义回归测试**：`tests/test_chatgpt_account_state.py` 覆盖刷新失败时旧 Plus 只保留为 `last_known_subscription_plan`、不再晋升为当前付费订阅；`tests/test_account_filters.py` 覆盖 `account_list_state` 中认证失效的历史 Plus 会落为当前订阅 `unknown` 且认证状态 `invalid`，防止后续再次把历史状态混入当前筛选。

## [1.3.27] - 2026-07-07
### 新增 (Added)
- **手机号绑定兼容宽松手机 API 输入与纯文本收码响应**：`services/chatgpt_core/phone_service.py` 将上传手机号行解析从固定 `手机号----收码API` 升级为 URL 优先识别，继续兼容四横杠格式，并新增 `手机号---https://...`、空格或逗号等宽松分隔输入；内部仍统一规范为 `+手机号` 与 `api_url`，保证手机号池 upsert、运行结果回写和绑定结果导出保持稳定。收码轮询新增 JSON / `YES|短信内容` / `NO|暂无短信` / 通用验证码文本解析链，`NO|暂无短信` 会按 pending 继续轮询，`YES|您的 OpenAI 验证代码是：421804` 会提取验证码并进入提交阶段，后续新增 API 形态只需扩展解析器而不改手机号绑定主流程。

### 优化 (Changed)
- **手机号绑定日志按账号分组留白显示**：`frontend/src/components/TaskLogPanel.tsx` 在渲染实时任务日志时识别 `[手机号绑定][账号 x/y]` 前缀，当账号序号切换时自动增加视觉空行，复制当前视图日志时也同步插入空行；后端日志存储不写入伪空行，历史 Plus / 主服务 / K12 手机绑定任务打开后同样获得账号间隔。
- **更新临时粘贴号码提示与前端版本号**：`frontend/src/pages/Accounts.tsx` 将“临时粘贴号码”的说明从单一 `+手机号----收码API` 扩展为推荐格式与兼容格式并列，示例补充 `手机号---https://...`；`frontend/src/app/AppShell.tsx` 侧边栏版本展示同步更新为 `v1.3.27`。

### 测试 (Tests)
- **补充手机号 API 兼容回归测试**：`tests/test_chatgpt_phone_flow.py` 覆盖 `手机号---https://...` 导入、标准化 raw_line、`NO|暂无短信` 继续轮询与 `YES|您的 OpenAI 验证代码是：421804` 提取验证码；`tests/test_phone_pool_task_integration.py` 覆盖手机绑定面板粘贴新分隔符后仍会导入手机号池并保持本轮固定列表执行。

## [1.3.26] - 2026-07-07
### 新增 (Added)
- **支持编辑和覆盖筛选组合的筛选条件**：`frontend/src/pages/Accounts.tsx` 针对用户反馈的“只能添加和删除筛选组合、无法修改条件”的问题进行了功能增强：
  1. **弹窗直接编辑条件**：在编辑筛选组合、保存当前筛选或复制组合时，弹窗下方新增“筛选条件配置”专区，提供关键词搜索、账号状态、认证材料、订阅类型、有效性、Codex 状态、Sub2Api 状态、OAIPay 状态、排序方式及分页条数等全维度表单项，支持在弹窗内自由调整和修改任意维度的筛选条件。
  2. **一键同步/覆盖条件**：编辑弹窗内新增“从当前页面筛选填充”按钮，一键将当前页面已选择的筛选条件带入表单；在“筛选组合管理”列表页中，针对自定义组合新增“覆盖条件”操作按钮，点击后可将当前页面已设置好的筛选条件直接覆盖保存到指定组合中。
- **同步前端版本号至 v1.3.26**：`frontend/src/app/AppShell.tsx` 侧边栏版本展示更新为 `v1.3.26`。

## [1.3.25] - 2026-07-07
### 优化 (Changed)
- **架构重构：拆分 Accounts 页面组件**：`frontend/src/pages/Accounts.tsx` 将原先庞大的文件中的 `FilterPresetBar` 和 `SelectedAccountsSummary` 独立提取为了可复用的独立组件，分别存放至 `frontend/src/features/accounts/components/FilterPresetBar.tsx` 与 `frontend/src/features/accounts/components/SelectedAccountsSummary.tsx`，显著提升了代码的可读性与维护性。
- **同步前端版本号至 v1.3.25**：`frontend/src/app/AppShell.tsx` 侧边栏版本展示更新为 `v1.3.25`。

## [1.3.24] - 2026-07-07
### 优化 (Changed)
- **极简压缩 Accounts 页面已选账号区域**：`frontend/src/pages/Accounts.tsx` 重构了 `renderSelectedAccountsSummary` 的布局。去除了占用大量纵向空间的内联标签列表，改为将已选账号的详细列表收纳到 `Popover` 中。缩小了 `padding` 尺寸，使得“已选账号”摘要区域变为紧凑的单行展示，释放了更多的纵向空间给数据表格。
- **同步前端版本号至 v1.3.24**：`frontend/src/app/AppShell.tsx` 侧边栏版本展示更新为 `v1.3.24`。

## [1.3.23] - 2026-07-07
### 优化 (Changed)
- **支持再次点击筛选组合来释放所有条件**：`frontend/src/pages/Accounts.tsx` 新增了 `clearFilterPreset` 逻辑。当用户点击当前已激活的 Pinned 固定组合按钮，或在下拉框中清空已选组合时，将自动释放所有过滤条件并恢复到默认视图。点击其他的组合则正常切换。
- **同步前端版本号至 v1.3.23**：`frontend/src/app/AppShell.tsx` 侧边栏版本展示更新为 `v1.3.23`。

## [1.3.22] - 2026-07-07
### 优化 (Changed)
- **极简压缩 Accounts 页面筛选组合区域**：`frontend/src/pages/Accounts.tsx` 重构了 `renderFilterPresetBar` 的布局。去除了占用大量纵向空间的 `padding`（从 `12px` 缩减为 `4px 8px`）和 `marginBottom`（从 `12px` 缩减为 `8px`）。将“筛选组合”选择器、Pinned 固定组合、状态文本以及“管理/保存”等操作项采用 Flex 弹性布局合并到了极度紧凑的单行内，释放了账号表格垂直可视高度，避免页面仅能展示2条数据的拥挤问题。
- **整合筛选管理辅助操作**：取消原先平铺的“保存当前筛选”、“管理”、“刷新组合”等次级操作按钮，将它们统一折叠收拢至一个设置（齿轮）图标的下拉菜单 (`Dropdown`) 内，使得界面的整体信噪比大幅提升。
- **同步前端版本号至 v1.3.22**：`frontend/src/app/AppShell.tsx` 侧边栏版本展示更新为 `v1.3.22`，用于上线后确认界面优化已加载。


## [1.3.21] - 2026-07-07
### 新增 (Added)
- **邮箱 API 支持 Gmail 多身份随机变体**：`core/base_mailbox.py` 将原先“Gmail 原邮箱 + 1 个 dot 变体”的固定展开升级为 `build_gmail_variants()` 规则生成器；每行 Gmail 现在可通过 `email_api_gmail_variant_count` 指定总身份数，语义为“原邮箱 + N-1 个随机变体”，默认规则 `all` 覆盖 Gmail dot、plus tag、dot+plus 混合以及 `googlemail.com` 域名等价形式。`parse_email_api_lines()` 会继续保留实际提交给 ChatGPT 的邮箱地址，同时把所有 Gmail / Googlemail / dot / plus 形式归一到同一个 `gmail_root` 用于锁定。
- **新增 Gmail 变体配置项**：`api/config.py`、注册页、账号页注册弹窗与 Settings 同步暴露 `email_api_gmail_variant_count`、`email_api_gmail_variant_rules`、`email_api_gmail_plus_tag_template`；默认值分别为 `2`、`all`、`r{rand}`。旧开关 `email_api_gmail_dot_variant_enabled` 保留兼容，但 UI 语义调整为“启用 Gmail 随机变体”，关闭后即使配置了较大的身份数也只使用原邮箱。

### 优化 (Changed)
- **冻结每个注册任务的随机邮箱候选集**：`api/tasks.py` 在创建邮箱 API 注册任务时生成并保存 `email_api_candidates` 与 `email_api_gmail_variant_random_seed`，后续运行、重试、attempt cap 和邮箱池分配都复用同一批候选，避免随机变体在任务创建、运行和日志统计阶段反复重算导致数量或邮箱不一致。
- **保持 Gmail/API 串行锁防串码**：多身份变体仍沿用 `api:<url>` 与 `gmail:<canonical_root>` 两类锁；`gmail.com`、`googlemail.com`、dot 变体、plus 变体和 dot+plus 变体都会落到同一个 canonical root，防止同一收件箱同时收到多个 OpenAI 验证码后串码。
- **同步前端版本号至 v1.3.21**：`frontend/src/app/AppShell.tsx` 侧边栏版本展示更新为 `v1.3.21`，用于上线后确认 Gmail 多身份随机变体配置已加载。

### 测试 (Tests)
- **补充邮箱 API Gmail 变体回归测试**：`tests/test_email_api_mailbox.py` 新增默认随机规则、指定总身份数、关闭变体、plus/googlemail 覆盖、Gmail 与 Googlemail 同 root 去重，以及注册任务候选集冻结的断言，防止后续把多规则随机生成退回成单一 dot 变体或运行期重算。


## [1.3.20] - 2026-07-07
### 优化 (Changed)
- **手机号绑定 Info 日志保留 18 行粒度但统一字段格式**：`services/chatgpt_core/task_logging.py` 重写手机绑定任务时间线 formatter，继续保留准备、代理、OAuth、邮箱验证码、短信发送/接收、确认绑定、补抓 Auth、保存账号、回写号码池和汇总等完整 Info 事件，但不再使用空格表格和混乱中英文字段；日志统一输出为 `[手机号绑定][账号 x/y][号码 x/y][步骤NN/12 阶段] 状态｜字段=值`，方便前端换行、复制和后续解析。
- **手机号绑定周边事件改为同一日志语法**：`api/tasks.py` 将手机号池导入、同号复用、短信探测、号段抽样、限定号段、重复号码跳过、Auth/RT 重试、号码池回写失败和结果汇总等非步骤日志统一改成 `[手机号绑定][分类] 事件｜字段=值` 格式，保留原有关键短语以兼容历史排障与测试断言。
- **手机号绑定详情字段语义化**：代理日志中的 `country/actual/exit_ip/provider/sid/probe` 会规范成 `目标国家/实际国家/出口IP/供应商/SID/探测`，接码日志中的 `otp_received/otp_length` 会规范成 `收码/长度`，验证码、手机号和代理凭据继续沿用统一脱敏规则。
- **同步前端版本号至 v1.3.20**：`frontend/src/app/AppShell.tsx` 侧边栏版本展示更新为 `v1.3.20`，用于上线后确认手机号绑定日志格式重构已加载。

### 测试 (Tests)
- **更新手机号绑定日志格式回归测试**：`tests/test_chatgpt_task_logging.py` 与手机号绑定相关任务流测试同步断言新的 `[手机号绑定][步骤] 状态｜字段=值` 格式，并覆盖同号复用、短信探测、Auth/RT 重试、重复号码跳过和结果表汇总等兼容短语。


## [1.3.19] - 2026-07-07
### 优化 (Changed)
- **注册 / 手机号注册任务日志按 Info 与 Debug 重新分层**：`services/chatgpt_core/task_logging.py` 新增统一的 `classify_task_log_level()` 分类器，`access_token_only_registration_engine.py` 与 `refresh_token_registration_engine.py` 改为复用同一套规则，把 ChatGPT 首页预热、CSRF、authorize 跳转、Sentinel Browser、注册状态机推进、HTTP 响应片段、OTP 提交、callback 与 session 探测等底层链路归入 Debug；默认 Info 视图保留账号尝试、代理出口、邮箱/验证码/注册/结果等操作员真正需要的阶段摘要，避免批量注册时 Info 被数千行技术流水刷屏。
- **手机绑定上游日志保留阶段化 Info、原始链路进入 Debug**：`api/tasks.py` 的 `phone_binding_test` 日志桥接现在会继续提取“发送短信 / 接收短信 / 提交短信 / 确认绑定”等关键阶段作为 Info，同时根据统一分类器把补抓 Auth、OAuth、接口响应和低层登录链路作为 Debug 展示，减少默认视图里的重复和噪声。
- **手机绑定任务时间线默认脱敏手机号与验证码**：`format_task_timeline_log()` 与 `api/tasks.py` 手机绑定桥接取消对任务日志的手机号/OTP 明文透传，Info 与 Debug 均只保留掩码手机号、验证码长度和阶段状态，真实手机号仍保存在结构化结果里用于业务回写。
- **注册任务日志桥接保留上游 level 语义**：`api/tasks.py` 为注册任务接入统一分类桥接，`subscription_auth_capture.py` 调整回调顺序，优先向任务日志传递 `level`，再兼容旧的一参回调，确保 warning/error 仍留在默认 Info 视图，debug 细节进入 Debug 视图。
- **同步前端版本号至 v1.3.19**：`frontend/src/app/AppShell.tsx` 侧边栏版本展示更新为 `v1.3.19`，用于上线后确认任务日志分层优化已加载。

### 安全 (Security)
- **补齐中文密码日志脱敏**：`services/chatgpt_core/task_logging.py` 的通用脱敏规则新增 `密码/登录密码/账号密码` 场景，避免注册链路中“邮箱: ...，密码: ...”这类中文字段被保存到实时任务日志、stdout 或历史 `task_logs.detail_json`。
- **修复验证码上下文误脱敏接口名**：`services/chatgpt_core/task_logging.py` 避免把 `phone-otp/send` 这类 OpenAI 路由误判为授权码，保证 Debug 中仍能看到关键接口名而不泄漏真正验证码。

### 测试 (Tests)
- **补充任务日志分层与脱敏回归测试**：`tests/test_chatgpt_task_logging.py` 增加注册低层日志归 Debug、业务阶段归 Info、warning/error 保持默认可见，以及中文密码字段脱敏的断言，防止后续新增日志重新污染 Info 视图或泄露密码。


## [1.3.18] - 2026-07-07
### 新增 (Added)
- **新增账号筛选组合能力**：`api/accounts.py` 新增 `/api/accounts/filter-presets` 管理接口，通过本实例 `config_store` 保存自定义筛选组合，支持创建、更新、删除和读取；筛选组合只保存搜索、账号状态、使用状态、认证材料、订阅类型、账号有效性、Sub2API/OAIPay 状态、到期排序和每页数量等条件，不保存账号 ID，确保每次应用时按最新库存动态匹配。
- **内置 OAIPay 常用筛选组合**：账号页默认提供“OAIPay 待补传”“Plus 长效未传”“Plus 未接码未传”“Free 带 RT 未传”“OAIPay 异常待处理”“Sub2API 已有但 OAIPay 未传”等组合，覆盖日常上传前的多条件筛选场景，避免运营人员反复手动拼筛选条件。

### 优化 (Changed)
- **账号页新增筛选组合操作区**：`frontend/src/pages/Accounts.tsx` 在工具栏下方增加“筛选组合”区域，可选择组合后立即完整替换当前筛选、显示当前条件摘要和匹配数量，并支持快捷置顶按钮、保存当前筛选、管理组合、复制内置组合、编辑自定义组合名称/描述、删除自定义组合。
- **组合变更状态可追踪**：应用筛选组合后，如果用户继续手动调整筛选条件，界面会提示“当前组合已变更”，并提供“覆盖保存 / 另存为 / 还原”操作；内置组合不可直接覆盖或删除，只能复制后保存为自定义版本，避免误改默认规则。
- **筛选组合保持实例隔离**：`core/shared_config.py` 将 `chatgpt_account_filter_presets` 标记为本地配置，避免主服务、Plus 和 K12 三个实例在账号库存不同的情况下共享运营筛选组合造成误用。
- **同步前端版本号至 v1.3.18**：`frontend/src/app/AppShell.tsx` 侧边栏版本展示更新为 `v1.3.18`，用于上线后确认筛选组合功能已加载。

### 测试 (Tests)
- **补充筛选组合接口回归测试**：新增 `tests/test_account_filter_presets.py`，覆盖自定义组合 CRUD、筛选条件归一化、重名拦截、内置组合不可修改/删除，以及配置键保持本实例本地隔离。


## [1.3.17] - 2026-07-06
### 新增 (Added)
- **新增单账号注册邮箱验证码等待预算**：`services/chatgpt_core/otp_budget.py` 增加 `RegistrationOtpBudget`，`access_token_only_registration_engine.py` 与 `refresh_token_registration_engine.py` 在每个账号注册链路中创建独立预算，只累计当前账号进入邮箱验证码阶段后的等待时间，不限制整批任务总耗时；预算耗尽后会直接结束当前账号，避免内部 retry 把同一个邮箱的验证码等待反复放大。
- **配置面板和注册弹窗暴露单账号 OTP 参数**：`api/config.py`、`frontend/src/pages/Settings.tsx`、`frontend/src/pages/RegisterTaskPage.tsx` 与 `frontend/src/features/auth/components/RegisterTaskModal.tsx` 新增 `chatgpt_register_otp_wait_seconds`、`chatgpt_register_otp_resend_wait_seconds`、`chatgpt_register_otp_account_budget_seconds` 三项配置，文案明确“单账号”语义，避免误解成批量任务总时长限制。

### 优化 (Changed)
- **注册验证码默认等待从保守长窗改为批量友好窗口**：ChatGPT 注册邮箱 OTP 默认首轮等待从 `600s` 调整为 `120s`，补发后等待从 `300s` 调整为 `90s`，默认单账号累计预算为 `210s`；已有显式配置继续优先，仍可手动改回更长等待窗口。
- **验证码等待日志展示有效等待和预算余量**：`EmailServiceAdapter` 在预算压缩本轮等待时会输出 `requested` 与 `single_account_remaining`，`ChatGPTClient.register_complete_flow()` 的状态机参数同步展示 `otp_account_budget_timeout`，便于从任务日志直接判断当前账号为何提前结束。
- **同步前端版本号至 v1.3.17**：`frontend/src/app/AppShell.tsx` 侧边栏版本展示更新为 `v1.3.17`，用于上线后确认单账号验证码预算配置已加载。

### 修复 (Fixed)
- **避免验证码预算耗尽后继续补发或整流程重试**：`services/chatgpt_core/chatgpt_client.py` 在首轮等待已耗尽预算时不再触发 `email-otp/send`，V2 注册外层在预算耗尽导致的验证码失败后不再进入同账号整流程重试，防止单账号等待时间超过配置语义。

### 测试 (Tests)
- **补充注册 OTP 单账号预算回归测试**：`tests/test_chatgpt_register.py` 覆盖预算耗尽时不触发补发，`tests/test_access_token_only_checkout.py` 覆盖默认 `120/90/210` 与显式配置透传，防止后续把单账号预算退化成整批任务限制或旧的 `600/300` 长等待。


## [1.3.16] - 2026-07-06
### 新增 (Added)
- **OAIPay 上传新增显式分类策略**：`api/tasks.py`、`services/oaipay_sync.py` 与 `services/chatgpt_core/oaipay_upload.py` 增加 `category_mode=auto/manual` 语义，默认保留自动分类，固定分类模式会跳过自动规则并强制使用用户所选 OAIPay 分类；旧请求继续兼容为“自动分类 + category_id 兜底”，避免历史调用语义突变。

### 优化 (Changed)
- **账号页 OAIPay 上传弹窗改为可解释的自动/固定分类选择**：`frontend/src/pages/Accounts.tsx` 将原来“选择分组，留空默认”的模糊下拉改为“自动分类（推荐）/固定分类”两种策略，明确 Plus+RT、Plus 无 RT、Free+RT 三条自动规则，并提供自动未命中时的兜底分类，避免操作者误以为系统随机上传。
- **OAIPay 上传结果写回最终分类并在账号列表展示**：上传成功、失败或跳过时会把 `category_id/category_name/category_source/category_rule` 写入 `sync_statuses.oaipay` 与 `last_upload`；`api/accounts.py` 将这些字段纳入账号列表安全摘要，`frontend/src/pages/Accounts.tsx` 的“OAIPay上传”列直接显示最近一次最终分类和来源。
- **OAIPay 批量任务日志逐账号展示分类去向**：`api/tasks.py` 的批量 OAIPay 上传日志现在会先输出分类策略，随后在每个账号的成功/失败/跳过行追加 `-> #分类ID 分类名 [来源]`，最终 summary 输出分类分布，方便直接从实时日志判断哪个账号上传到了哪个分类。
- **账号处理流水线 OAIPay 分组文案收敛为兜底语义**：`frontend/src/pages/IdeaOaiPayPipeline.tsx` 将“上传分组”改为“自动分类兜底分组”，后端 `services/idea_oaipay_pipeline/engine.py` 也按自动分类 + 兜底参数调用 OAIPay 上传，和账号页语义保持一致。
- **同步前端版本号至 v1.3.16**：`frontend/src/app/AppShell.tsx` 侧边栏版本展示更新为 `v1.3.16`，用于上线后确认 OAIPay 分类策略与日志展示修复已加载。

### 测试 (Tests)
- **补充 OAIPay 分类策略回归测试**：`tests/test_oaipay_sync.py` 覆盖自动分类解析、固定分类覆盖自动规则与远端返回分类字段透传；`tests/test_accounts_api_list_compact.py` 覆盖账号列表安全摘要中的 OAIPay 分类字段；`tests/test_chatgpt_task_logging.py` 覆盖批量上传任务 meta 中的分类策略和分类分布记录。


## [1.3.15] - 2026-07-06
### 优化 (Changed)
- **Idea 批量提交改为等待上游终态再收口**：`api/tasks.py` 将 `baxigpt_cdk_submit` 的 5 分钟“轮询超时”改为 30 分钟未返回提醒，提醒只写日志并继续等待 `paid/failed` 终态；只有超过 24 小时安全上限仍无结果时才归入超时，避免 70 个账号这类批量任务在上游状态尚未全部返回时提前结束。
- **Idea 最终结果改为日志分类逐行输出**：`api/tasks.py` 的最终 summary 现在先输出分类标题，再按 `[SUMMARY][成功账号/失败账号/超时账号/未提交账号]` 一行一个账号记录结果；日志只展示账号、上游任务 ID 和原因，不再拼接卡密或带卡密前缀的 order_id，方便直接复制排查。
- **同步前端版本号至 v1.3.15**：`frontend/src/app/AppShell.tsx` 侧边栏版本展示更新为 `v1.3.15`，用于上线后确认 Idea 提交轮询和结果展示修复已加载。

### 修复 (Fixed)
- **修复 Idea 结果卡片在深色主题下白底突兀的问题**：`frontend/src/components/idea/IdeaSubmitSummary.tsx` 移除四个账号结果表格，改为深色主题一致的摘要提示；`frontend/src/components/TaskLogPanel.tsx` 的日志区域背景、边框和状态文字改为使用 Ant Design 主题 token，和项目深蓝底色保持一致。
- **隐藏历史结果中的卡密展示**：`frontend/src/components/task-detail/TaskDetailHeader.tsx` 不再在 `baxigpt_cdk_submit` 历史详情里展示账号/卡密配对表，统一提示到任务日志末尾查看逐行分类结果，避免结果区继续暴露卡密信息。

### 测试 (Tests)
- **补充 Idea 提交结果脱敏回归测试**：`tests/test_baxigpt_submit_summary.py` 增加断言，确保结构化结果不再输出 `code_masked/order_id`，最终日志行会把 `卡密::任务ID` 清洗成纯上游任务 ID。


## [1.3.14] - 2026-07-06
### 新增 (Added)
- **新增 Idea 提交不可用账号标记**：`services/chatgpt_core/baxigpt_cdk_repository.py` 在 Idea 上游返回账号级失败后，会在账号 `extra.idea_submit` 写入 `unavailable/reason/marked_at/source/cdk_id/order_id` 等专用标记，并保留 `idea_submit_unavailable*` 兼容字段；`api/accounts.py` 将该标记序列化为账号列表/详情可读的 `idea_submit` 摘要，避免只靠通用 `payment_failed` 或 `baxigpt_cdk.last_error_message` 反推。

### 优化 (Changed)
- **Idea 批量提交日志增加最终分组总结**：`api/tasks.py` 为 `baxigpt_cdk_submit` 任务生成 `idea_submit_summary`，按成功账号、失败账号、超时账号、未提交账号和已标记不可用于 Idea 提交账号分组记录，并在最终日志中输出每组账号与原因；历史任务详情和实时 `TaskLogPanel` 通过 `frontend/src/components/idea/IdeaSubmitSummary.tsx` 直接展示同一份结构化总结，不再只显示零散流水日志。
- **账号页直接展示 Idea 提交状态**：`frontend/src/pages/Accounts.tsx` 新增“Idea提交”列和移动端状态标签，账号详情抽屉 `frontend/src/features/accounts/components/AccountDetailModal.tsx` 同步展示 Idea 标记、原因、标记时间和 order 信息；已标记不可用的账号再次发起 Idea 批量提交时会在创建阶段跳过并说明原因。
- **同步前端版本号至 v1.3.14**：`frontend/src/app/AppShell.tsx` 侧边栏版本展示更新为 `v1.3.14`，用于上线后确认 Idea 提交总结与账号标记前端资源已加载。

### 修复 (Fixed)
- **修复 Idea 提交统计把“未提交”混成跳过/失败的问题**：`api/tasks.py` 将缺少 AT、账号不存在、卡密额度不足、提交上游前失败等场景归入“未提交账号”，把上游已受理后返回 failed 的场景归入“失败账号”，并在任务结束前先写入最终 meta 再保存 `TaskLog`，避免历史详情里 `errors=[]`、summary 却显示失败的错乱状态。

### 测试 (Tests)
- **补充 Idea 提交标记与总结回归测试**：扩展 `tests/test_baxigpt_cdk_pool.py` 并新增 `tests/test_baxigpt_submit_summary.py`，覆盖账号不可用标记字段、成功后清理专用不可用标记，以及成功/失败/未提交/缺失账号的结构化汇总分组。


## [1.3.13] - 2026-07-06
### 新增 (Added)
- **K12 / Workspace 重跑改为可观察任务链路**：新增 `POST /api/tasks/chatgpt/k12-workspace-recapture` 与 `POST /api/tasks/chatgpt/k12-workspace-recapture/batch`，单账号和批量重跑都会创建 `k12_workspace_recapture` / `batch_k12_workspace_recapture` 任务，进入统一 `TaskLogPanel` 实时日志面板，可显示 join、`accounts/check`、workspace token exchange、代理候选和最终写回摘要，不再只在账号操作结果弹窗里同步等待。

### 优化 (Changed)
- **账号页 K12 重跑入口接入任务弹窗**：`frontend/src/features/accounts/components/AccountActionSurface.tsx` 的 `k12_workspace_recapture` 操作改为回调到账号页创建后台任务；`frontend/src/pages/Accounts.tsx` 的单账号 `K12重跑` 与工具栏“批量K12重跑”现在都会打开注册任务同款运行面板，并把任务 source 映射到 `k12_recapture` 标题，方便中途停止、看进度和回放历史。
- **同步前端版本号至 v1.3.13**：`frontend/src/app/AppShell.tsx` 侧边栏版本展示更新为 `v1.3.13`，用于上线后确认 K12 重跑任务化与日志面板修复已加载。

### 修复 (Fixed)
- **修复 K12 重跑“join 成功但导出 0 个空间”仍显示成功的问题**：`services/chatgpt_core/k12_recapture.py` 将成功判定收敛为必须导出至少一个可写回的 workspace artifact；只有 join 结果、`accounts/check` 空间或失败摘要不再被当作成功，避免出现“完成：导出 0 个空间、写入 0 个账号”的误导状态。
- **保留批量 K12 的筛选/代理/延时语义**：`api/tasks.py` 的批量任务复用账号筛选、代理四模式、动态代理保留时长、Join 超时/重试/轮询和账号间随机延时参数，并在任务日志中脱敏保存配置摘要，避免从旧同步批量接口迁移到任务接口后运行参数漂移。

### 测试 (Tests)
- **补充 K12 任务化回归测试**：扩展 `tests/test_chatgpt_k12_recapture.py`，覆盖“无 artifact 返回失败”以及单账号 K12 任务创建时会返回 `task_id`，固定日志面板入口和成功判定，防止后续又退回同步 action 路径。


## [1.3.12] - 2026-07-06
### 新增 (Added)
- **K12 重捕获接入账号操作列**：`services/chatgpt_core/plugin.py` 新增 `k12_workspace_recapture` 平台操作，`frontend/src/pages/Accounts.tsx` 在账号列表“操作”列直接提供 `K12重跑` 入口；执行面板统一走 `frontend/src/features/accounts/components/AccountActionSurface.tsx`，不再藏在账号详情页，避免用户必须打开详情抽屉才能重跑空间导出。
- **批量 K12 / Workspace 重跑**：`frontend/src/features/accounts/components/AccountsToolbar.tsx` 与 `frontend/src/pages/Accounts.tsx` 新增“批量K12重跑”工具栏入口，支持当前选中账号或当前筛选结果，参数包含 workspace_id、是否导出所有可见空间、严格 join、Join 超时/重试/轮询以及账号间随机延时；后端复用 `POST /api/actions/chatgpt/k12_workspace_recapture/batch`，逐账号返回成功/失败摘要并刷新列表。

### 优化 (Changed)
- **K12 重捕获代理配置统一为四模式**：单账号操作面板与批量弹窗均改为和注册/本地状态同步一致的 `direct` / `specified` / `pool` / `dynamic` 四种代理模式，支持代理池国家、健康度、候选数量、指定代理失败切换，以及动态代理模板/出口国家；`api/actions.py` 使用 `core.proxy_utils.resolve_probe_candidate_proxies()` 统一解析候选代理并在网络类失败时自动 failover。
- **详情页回归只读摘要职责**：`frontend/src/features/accounts/components/AccountDetailModal.tsx` 移除 K12 重跑按钮和单独弹窗，只保留 Workspace variants 脱敏摘要，并提示用户从列表操作列执行，避免详情页和操作列出现两套不一致入口。
- **同步前端版本号至 v1.3.12**：`frontend/src/app/AppShell.tsx` 侧边栏版本展示更新为 `v1.3.12`，用于上线后确认操作列入口、四模式代理和批量重跑配置已加载。

### 修复 (Fixed)
- **批量执行后刷新所有变更账号状态**：`api/actions.py` 对 `k12_workspace_recapture` 返回的 `changed_account_ids` 逐个调度本地状态刷新，覆盖当前账号与新建/更新的 workspace variant 账号，避免只刷新发起账号导致列表状态短期不一致。

### 测试 (Tests)
- **补充 K12 action 回归测试**：`tests/test_chatgpt_k12_recapture.py` 新增平台操作暴露与 action wrapper changed IDs 测试，确保列表操作列/批量入口能发现 `k12_workspace_recapture`，并能把重捕获写入的账号 ID 传递给后续状态刷新。

## [1.3.11] - 2026-07-06
### 新增 (Added)
- **已保存 ChatGPT 凭证支持手动重跑 K12 / Workspace 捕获**：`api/chatgpt.py` 新增 `POST /api/chatgpt/{account_id}/k12-workspaces/recapture`，复用账号库中已保存的 `access_token`、`session_token` 与完整 `cookies`，重新执行 K12 workspace join、`accounts/check` 空间列表拉取与 workspace token exchange；用于原 K12 空间失效后重新进入新空间，并把当前可进入空间重新导出为 workspace variants。
- **K12 重捕获持久化服务**：新增 `services/chatgpt_core/k12_recapture.py`，在保存重捕获结果时显式保护既有 `refresh_token`、支付状态与 Web session 材料，避免 AT-only artifact 覆盖已有 RT 账号；同时为新捕获到的 K12/workspace 创建或更新独立账号行，并刷新 `account_list_state` 与 `chatgpt_workspace_variants` 摘要。
- **账号详情页新增 K12 重新进入/导出入口**：`frontend/src/features/accounts/components/AccountDetailModal.tsx` 在“所有空间 / Workspace variants”区增加“重新进入/导出 K12”按钮，可填写新的 workspace_id、选择是否导出所有可见空间、配置严格 join、重试、轮询和代理；执行结果只展示脱敏摘要，不把 token/cookies 展开到前端。

### 修复 (Fixed)
- **避免手动 K12 重跑污染主账号认证材料**：重跑保存路径会按 `chatgpt_workspace_variant_key` 精确匹配当前账号与 linked variant，更新 AT/cookies/session 的同时保留已有 RT，不再通过通用 `save_account` 把 free 主账号误降级成仅 AT 账号。

### 测试 (Tests)
- **补充 K12 重捕获回归测试**：`tests/test_chatgpt_k12_recapture.py` 覆盖 artifact 脱敏输出、当前账号合并时保留 RT 并更新 Web session，以及 workspace variants 摘要落库行为；继续复跑 `tests/test_chatgpt_k12_workspace.py` 确认原注册阶段 K12 捕获链路未回归。

### 优化 (Changed)
- **同步前端版本号至 v1.3.11**：`frontend/src/app/AppShell.tsx` 侧边栏版本展示更新为 `v1.3.11`，用于上线后确认 K12 重捕获入口已加载。

## [1.3.10] - 2026-07-06
### 新增 (Added)
- **动态代理新增 Cliproxy IP 保留时长配置**：`api/config.py` 新增 `dynamic_proxy_ip_retention_minutes` 全局配置，默认 `5`，任务生成动态代理时会把用户名中的 `t-N` 统一覆盖为该值；模板没有 `t-N` 但包含 `sid-xxx` 时会自动补成 `sid-xxx-t-N`，让 `t-5` 这类 IP 保留时长不再只能写死在代理模板里。
- **代理管理与 Settings 暴露 t-N 编辑入口**：`frontend/src/pages/Proxies.tsx` 的动态代理预览区新增“IP保留分钟”输入并随“保存为任务默认”落库；`frontend/src/pages/Settings.tsx` 动态代理配置区新增“IP 保留分钟数（t-N）”，并把模板提示改为支持 `region-Rand` / `t-5` 的 Cliproxy 形式。

### 修复 (Fixed)
- **修复 Cliproxy `region-Rand` 被截断成坏国家 token**：`core/dynamic_proxy.py` 的 region 解析从只匹配两位国家码改为匹配完整 region token，`region-Rand` 改写为 `region-US` / `region-JP` 时不再残留 `nd`，避免生成 `region-USnd`、`region-JPnd` 后触发代理侧 TLS/SSL 错误。

### 测试 (Tests)
- **补齐动态代理回归测试**：`tests/test_dynamic_proxy.py` 覆盖 `region-Rand` 完整改写、`t-N` 覆盖/补插、候选代理使用配置保留时长，以及动态代理预览接口不泄露原始代理凭证。

### 优化 (Changed)
- **同步前端版本号至 v1.3.10**：`frontend/src/app/AppShell.tsx` 侧边栏版本展示更新为 `v1.3.10`，用于上线后确认 Cliproxy 动态代理修复与 IP 保留时长配置已加载。

## [1.3.9] - 2026-07-06
### 修复 (Fixed)
- **恢复普通邮箱注册的自动发码优先路径**：`services/chatgpt_core/chatgpt_client.py` 在 `Authorize -> /email-verification` 的注册分支中恢复旧行为，先等待 OpenAI 已自动发送到普通邮箱/邮箱 API 的验证码；只有首次等待窗口未收到时才触发 `email-otp/send` 补发，避免一进入验证码页就额外发码导致 OpenAI Auth session/OTP 状态被刷新，最终在 `/api/accounts/email-otp/validate` 返回 `409 invalid_state`、注册不能落库。

### 测试 (Tests)
- **补齐注册 OTP 状态机回归**：`tests/test_chatgpt_register.py` 新增直接进入 `/email-verification` 的两条用例，固定“秒收验证码不调用 `email-otp/send`”和“首次等待超时后才补发”的行为，防止后续为了邮箱 API 收码再破坏普通邮箱注册主链路。

### 优化 (Changed)
- **同步前端版本号至 v1.3.9**：`frontend/src/app/AppShell.tsx` 侧边栏版本展示更新为 `v1.3.9`，用于上线后确认普通邮箱注册回归修复已加载。

## [1.3.8] - 2026-07-06
### 修复 (Fixed)
- **补齐无 RT/V2 注册引擎的邮箱 API 超时与重发配置**：`services/chatgpt_core/access_token_only_registration_engine.py` 的 V2 `EmailServiceAdapter` 现在同样会把邮箱 provider 的 `TimeoutError` 归一为未收到验证码，让 `ChatGPTClient.register_complete_flow()` 能执行 `email-otp/send` 重发；同时 V2 注册调用会读取并透传 `chatgpt_register_otp_wait_seconds` 与 `chatgpt_register_otp_resend_wait_seconds`，避免 K12/email_api 实际运行仍固定显示 `otp_wait_timeout=600s`、`otp_resend_wait_timeout=300s`。

### 测试 (Tests)
- **新增无 RT/V2 注册回归测试**：`tests/test_access_token_only_checkout.py` 覆盖 V2 邮箱 adapter 超时返回 `None`、以及配置化 OTP 等待/重发秒数会传入 `register_complete_flow()`，防止只修到 RefreshToken 引擎而漏掉 K12 当前使用的 AccessToken-only 注册链路。

### 优化 (Changed)
- **同步前端版本号至 v1.3.8**：`frontend/src/app/AppShell.tsx` 侧边栏版本展示更新为 `v1.3.8`，用于上线后确认无 RT/V2 邮箱 API 重发修复已加载。

## [1.3.7] - 2026-07-06
### 修复 (Fixed)
- **邮箱 API 等待超时后允许注册状态机执行重发**：`services/chatgpt_core/refresh_token_registration_engine.py` 的 `EmailServiceAdapter` 现在会把邮箱 provider 的 `TimeoutError` 归一为“本轮未收到验证码”，返回给 `ChatGPTClient.register_complete_flow()`，从而真正进入“首次等待未收到后重发一次 `email-otp/send` 再等待”的既有分支；修复此前 `EmailApiMailbox.wait_for_code()` 超时会直接抛出并中断整轮注册，导致 K12/email_api 场景虽然配置了重发窗口却永远不会重发的问题。
- **补齐注册发码请求的浏览器一致性头**：`services/chatgpt_core/chatgpt_client.py` 的 `send_email_otp()` 与密码提交、OAuth 重发逻辑对齐，向 `/api/accounts/email-otp/send` 请求补充 `oai-device-id` 与 Datadog trace 头，降低服务端将发码请求视为不完整浏览器上下文而只返回 200 但不稳定投递验证码的概率。

### 测试 (Tests)
- **补充邮箱 API 重发链路回归**：`tests/test_chatgpt_register.py` 新增 `EmailServiceAdapter` provider 超时归一测试，以及 `send_email_otp()` 请求头测试，固定本次 K12 smsbower 排障暴露的重发阻断和请求头漂移问题。

### 优化 (Changed)
- **同步前端版本号至 v1.3.7**：`frontend/src/app/AppShell.tsx` 侧边栏版本展示更新为 `v1.3.7`，用于上线后确认邮箱 API 重发与发码请求头修复已加载。

## [1.3.6] - 2026-07-06
### 修复 (Fixed)
- **修复邮箱验证码 API 被全局邮箱等待秒数压成 20 秒的问题**：`services/chatgpt_core/plugin.py` 针对 `email_api` 改为服从 ChatGPT 注册/OAuth 状态机传入的等待窗口，避免 K12/注册链路已显示 `otp_wait_timeout=600s` 时仍被历史 `mailbox_otp_timeout_seconds=20` 提前中断，导致 `等待 Email API 验证码超时 (20s)`。
- **兼容 smsbower 实际返回结构**：`core/base_mailbox.py` 的 `EmailApiMailbox` 新增 `codes_from_payload()`，除继续支持 `status` 直接返回验证码外，也支持 `status=1`、`code` 与 `all_codes` 承载验证码的响应形态；旧码基线会记录所有已有 code，轮询时只提交新出现且未排除的验证码。
- **注册进入邮箱验证码页时主动触发发码**：`services/chatgpt_core/chatgpt_client.py` 在 authorize 后直接落到 `/email-verification` 且此前未调用过 `email-otp/send` 的场景下，会先主动请求发送注册验证码再进入邮箱 API 轮询，避免只停在验证码页等待但 smsbower 收件箱一直没有新邮件。

### 测试 (Tests)
- **补充邮箱 API 回归用例**：`tests/test_email_api_mailbox.py` 覆盖 smsbower `code/all_codes` 响应、旧码跳过与新码返回；`tests/test_chatgpt_plugin.py` 固定 `email_api` 不会被全局 `mailbox_otp_timeout_seconds` 缩短状态机 timeout。

### 优化 (Changed)
- **同步前端版本号至 v1.3.6**：`frontend/src/app/AppShell.tsx` 侧边栏版本展示更新为 `v1.3.6`，用于上线后确认本次邮箱 API 超时与 smsbower 响应兼容修复已加载。

## [1.3.5] - 2026-07-05
### 新增 (Added)
- **新增邮箱验证码 API 注册/登录收码 provider**：`core/base_mailbox.py` 新增独立 `email_api` 邮箱服务，并兼容 `api_email`、`email_otp_api`、`mail_api_otp` 别名；支持每行 `邮箱----API` 输入，自动补全无协议 API URL，轮询 JSON 响应中的 `status` 字段作为验证码来源。`status=0`、空值或非 4-8 位数字会按“尚未收到验证码”继续等待，收到有效验证码后复用现有 ChatGPT 注册与 OAuth 登录邮箱 OTP 流程。
- **支持 Gmail 一行双账号身份**：`parse_email_api_lines()` 会把 Gmail 原邮箱展开为“原地址 + 一个点号变体”两个注册身份，两个身份共用同一个接码 API；运行时按 Gmail canonical 与 API URL 建立任务级串行锁，避免单字段 `status` 在并发注册或二阶段登录时串码。账号落库时保留实际提交给 ChatGPT 的邮箱地址，不会把点号别名去点后覆盖为 canonical 邮箱。
- **补齐注册入口和配置入口**：`api/tasks.py` 在 `_prepare_register_request()` 中对 `email_api` 做后端权威解析、候选数量统计、注册数量自动展开和并发上限收敛；注册页、账号页注册弹窗与 Settings 邮箱服务配置区新增“邮箱验证码 API（email----api）”入口，可配置邮箱 API 行、轮询间隔、请求超时、默认 URL 协议与 Gmail 点号变体开关。

### 修复 (Fixed)
- **保护邮箱 API 敏感链接不进入任务日志**：`services/chatgpt_core/task_logging.py` 新增 `redact_raw_email_api_line()`，并对 `email_api_line`、`email_api_lines`、`email_api_accounts` 等字段逐行脱敏，只保留邮箱与 API host/path，移除 `s=`、token、query 和 fragment，避免接码凭证进入任务详情、历史日志和错误文本；`tests/test_chatgpt_task_logging.py` 补充结构化脱敏回归。
- **避免注册任务重复消费同一邮箱行**：`api/tasks.py` 将 mailbox 构造改为接收每次 attempt 的 `runtime_extra`，并为 `email_api` 注入 task 级共享池 key；`EmailApiMailbox` 在成功或失败 finalize 后释放同 Gmail/API 锁，任务结束时清理共享池，防止并发或代理重试时每个 worker 都从第一行重新分配邮箱。
- **确保后续登录/补抓 Auth 可恢复同一 API**：`services/chatgpt_core/plugin.py` 支持 mailbox 自定义 `export_state_config()`，避免把整批 `email_api_lines` 写进每个账号的 `chatgpt_mailbox_state.config`；成功账号只保存当前账号自己的 `api_url`、source email、Gmail root 和 variant。`services/chatgpt_core/pending_business_invites.py` 增加 `email_api` legacy state 兼容，后续 pending invite、邮箱测活和 auth recheck 能通过已保存 mailbox state 自动复用同一邮箱 API 收码。

### 优化 (Changed)
- **同步前端版本号至 v1.3.5**：`frontend/src/app/AppShell.tsx` 侧边栏版本展示更新为 `v1.3.5`，用于上线后确认邮箱验证码 API 注册/登录能力对应的静态资源已加载。

## [1.3.4] - 2026-07-05
### 优化 (Changed)
- **发布流程默认不再创建运行态备份**：`deploy.sh` 将 `.rollback-backups/deploy-*` 发布前备份改为显式 `--backup` 才创建，常规 multi/image/hot 发布只依赖 Git 提交与 live smoke，避免每次部署都复制三实例 SQLite、容器 inspect 和共享配置快照导致磁盘快速膨胀。
- **热更新脚本支持跳过备份**：`scripts/deploy-to-auto-gpt-container.sh` 增加 `SKIP_BACKUP=1`，在默认发布路径下跳过 container inspect、静态目录备份和 predeploy image commit；需要临时保守发布时仍可用 `deploy.sh "说明" --mode=hot --backup` 恢复原来的热补丁备份行为。
- **同步前端版本号至 v1.3.4**：`frontend/src/app/AppShell.tsx` 侧边栏版本展示更新为 `v1.3.4`，用于上线后确认本次发布脚本调整对应的静态资源已加载。

## [1.3.3] - 2026-07-05
### 新增 (Added)
- **新增三实例共享配置中心**：新增 `core/shared_config.py`，在 `/opt/auto-gpt/shared_config/shared_config.db` 中维护独立于账号/任务数据库的共享配置模板，使用 SQLite revision、审计记录和 JSON snapshots 替代裸文件互拷；`core/config_store.py` 按实例本地开关决定读写共享模板或本地 `configs` 表，避免共用 `account_manager.db` 导致账号、任务、日志和手机号池混库。
- **新增共享配置管理接口**：`api/config.py` 增加 `/api/config/share-state`、`/api/config/share/pull`、`/api/config/share/push`、`/api/config/share/diff` 与 `/api/config/share/audit`，支持实例开启共享、关闭共享时复制当前模板为本地基线、从共享模板拉取、显式将本实例配置推送为共享模板以及查看变更审计；常规 `/api/config` 保持纯配置对象返回，兼容现有任务入口。

### 优化 (Changed)
- **Settings 页增加共享配置状态卡片**：`frontend/src/pages/Settings.tsx` 在全局配置页顶部展示当前实例、共享/本地模式、共享 revision、最后更新来源、本地保留 key 说明，并提供“从共享拉取 / 查看差异 / 本实例推送为共享模板”等显式操作；共享模式保存时会带上 base revision，后端检测到版本冲突时返回 409，避免多实例静默覆盖。
- **三实例编排挂载共享配置目录**：`docker-compose.multi.yml` 为 `auto-gpt`、`auto-gpt-plus`、`auto-k12` 增加 `APP_INSTANCE_ID`、`SHARED_CONFIG_DB=/shared_config/shared_config.db` 与 `/opt/auto-gpt/shared_config` 挂载；`deploy.sh` 将 `shared_config/` 视为运行态敏感目录并纳入发布备份 tar，`.gitignore` 与 `.dockerignore` 防止共享模板、启动备份、密钥快照和 WAL 误入仓库或 Docker build context。
- **修正共享空值覆盖语义**：`core/shared_config.py` 增加存在性读取，`core/config_store.py` 在共享模式下即使共享模板中的值为空字符串也会按共享值返回，不再误回退到本实例旧本地值或环境变量，保证“清空某项配置”也能在共享实例间一致生效。
- **保留非 Settings 页写入的共享 key**：`api/config.py` 的共享差异和“本实例推送为共享模板”改为基于本地 `configs` 已保存全量快照，避免 `team_manager_*`、`grok2api_*` 等历史全局配置在人工推送模板时被静默删除，同时避免默认值展示层或环境变量兜底造成已同步实例仍显示大量伪差异。
- **同步前端版本号至 v1.3.3**：`frontend/src/app/AppShell.tsx` 侧边栏版本展示更新为 `v1.3.3`，用于上线后确认共享配置中心对应的静态资源已加载。

## [1.3.2] - 2026-07-05
### 新增 (Added)
- **新增 K12 独立运行实例 `auto-k12`**：`docker-compose.multi.yml` 增加第三个服务，复用统一镜像 `auto-gpt:latest`，但使用 `/opt/auto-k12/data`、`/opt/auto-k12/_ext_targets` 与 `/opt/auto-k12/external_logs` 作为独立运行态挂载；对外业务端口为 `8002`，CLIProxyAPI 端口为 `8319`，Solver 仅本机暴露为 `8891`，避免与主服务 `auto-gpt` 和 Plus 实例 `auto-gpt-plus` 的数据、日志、插件目录混用。

### 优化 (Changed)
- **扩展发布脚本到三实例闭环**：`deploy.sh` 的 multi/hot 发布、发布前 SQLite 备份、容器 inspect 归档和发布后 smoke 检查同步覆盖 `auto-k12`，上线后会同时校验 `http://127.0.0.1:8000/`、`8001` 与 `8002` 的首页和 `/api/health`，避免新增实例只写入 Compose 但未纳入发布门禁。
- **同步操作约定与前端版本号至 v1.3.2**：`AGENTS.md` 更新为主服务、Plus、K12 三实例口径，明确 `/opt/auto-k12` 仅作为数据/配置隔离目录；`frontend/src/app/AppShell.tsx` 侧边栏版本展示更新为 `v1.3.2`，用于确认新三实例编排对应的前端资源已加载。

## [1.3.1] - 2026-07-05
### 安全 (Security)
- **封闭账号详情接口的明文凭证旁路**：`api/accounts.py` 将 `/accounts/{account_id}` 与 `/accounts?detail=true` 改为沿用 compact serializer 的安全摘要模型，响应中只保留 `credentials`、`auth`、workspace variants、手机号绑定和同步状态等必要摘要；`token/password/extra_json` 中的 Access Token、Refresh Token、NextAuth session、cookies 与 ID Token 不再随详情接口返回，完整材料继续只能通过 `/accounts/{id}/secrets` 按字段读取。`tests/test_accounts_api_list_compact.py` 补充详情序列化脱敏回归，防止 K12 全空间保存后被 detail=true 绕过列表脱敏边界。

### 修复 (Fixed)
- **避免账号详情保存时误清空 Access Token**：`frontend/src/pages/Accounts.tsx` 在详情 Drawer 保存基础信息时，如果 Access Token 手动覆盖输入为空，会从 PATCH payload 中移除 `token` 字段，只保存状态等基础信息，避免后端详情脱敏后空 token 被表单原样提交并覆盖数据库中已保存的 AT。
- **Settings 页复用统一 K12 配置归一化**：`frontend/src/pages/Settings.tsx` 改为直接调用 `frontend/src/lib/chatgptK12Config.ts` 的 `buildChatGPTK12ConfigData()`，与注册页和账号页注册弹窗共享 workspace id 去重、保存所有空间默认值、join 超时/重试/轮询范围和 `capture_refresh_tokens=false` 的口径，减少后续 K12 配置字段漂移。

### 优化 (Changed)
- **同步前端版本号至 v1.3.1**：`frontend/src/app/AppShell.tsx` 侧边栏版本展示更新为 `v1.3.1`，用于上线后确认账号详情脱敏加固与 K12 Settings 配置统一对应的静态资源已加载。

## [1.3.0] - 2026-07-05
### 新增 (Added)
- **补齐 K12 注册后 workspace join 与全空间保存链路**：新增 `services/chatgpt_core/k12_workspace.py`，在 ChatGPT 注册成功并复用当前 Web session 拿到 Access Token 后，支持向配置的 K12 `workspace_id` 调用 `/backend-api/accounts/{workspace_id}/invites/request` 发起加入请求，再通过 `/backend-api/accounts/check/v4-2023-04-27` 读取当前账号可见的所有空间。捕获结果按 `workspace_artifacts` 输出，包含 free、K12、workspace 等 scope 摘要，并对每个空间复用现有 `fetch_chatgpt_session(exchange_workspace_token=true)` 交换对应 Access Token，避免重新登录、重复触发邮箱 OTP 或误走手机号绑定链路。
- **注册结果按 workspace variants 保存多条账号**：`services/chatgpt_core/access_token_only_registration_engine.py` 在 AT-only 注册成功后新增 K12/all-spaces 捕获步骤，保留 primary/free 账号不被覆盖，并将额外空间作为 linked accounts 交给现有保存逻辑落库；`services/chatgpt_core/chatgpt_registration_mode_adapter.py` 扩展 `k12` scope、artifact 去重、artifact 强度优先级、workspace 摘要与两阶段 RT 合并逻辑，确保第一阶段捕获到的 K12/全空间 AT-only variants 在第二阶段 free RT 成功后仍会保留为附加账号。
- **新增 K12 全局配置与任务参数透传**：`api/config.py` 增加 `chatgpt_k12_enabled`、`chatgpt_k12_workspace_ids`、`chatgpt_k12_save_all_spaces`、`chatgpt_k12_strict_join`、`chatgpt_k12_join_timeout_seconds`、`chatgpt_k12_join_retry_count`、`chatgpt_k12_post_join_poll_seconds`、`chatgpt_k12_capture_refresh_tokens` 配置项；`api/tasks.py` 将这些参数写入注册任务 extra 与账号 extra，任务创建日志也记录 K12 是否启用、目标数量与是否保存所有空间，方便后续排障回溯。
- **前端补齐注册页、注册弹窗、设置页和账号详情入口**：`frontend/src/pages/RegisterTaskPage.tsx` 与 `frontend/src/features/auth/components/RegisterTaskModal.tsx` 新增 K12 / Workspace 配置区，默认保存所有空间 variants、join 超时 60 秒、join 后轮询 `3,8,15` 秒，并将 Refresh Token variants 明确标记为预留禁用项；`frontend/src/pages/Settings.tsx` 新增全局 K12 / Workspace 配置分组；`frontend/src/features/accounts/components/AccountDetailModal.tsx` 新增“所有空间 / Workspace variants”摘要区，展示 scope、workspace_id、display_name、auth_level、partial_auth 和 source。

### 安全 (Security)
- **收紧 workspace variants 展示脱敏边界**：`api/accounts.py` 的 compact 账号列表只返回 workspace variants 摘要，不透出 access_token、refresh_token、id_token、session_token、cookies 或 cookie_header；账号详情页同样只展示空间摘要，完整凭证仍沿用 `/accounts/{id}/secrets` 按需读取，避免全空间保存功能把 Web session 材料泄漏到列表接口或普通详情摘要。
- **加固 K12 捕获异常与日志脱敏**：`services/chatgpt_core/k12_workspace.py` 与 `services/chatgpt_core/task_logging.py` 扩展 Bearer、`cookie_header`、NextAuth/Auth.js cookie、Python dict repr 等敏感字段脱敏；`api/tasks.py` 保存附加工作空间失败时也统一走 `sanitize_error_message()`，避免 linked workspace 的 token/cookies 因异常文本进入任务日志。

### 修复 (Fixed)
- **避免 K12 捕获影响基础注册落库**：K12 join 默认非严格模式，单个 workspace join、accounts/check 或 workspace token exchange 失败时只记录 `chatgpt_k12_join_summary`、`chatgpt_k12_join_results`、`chatgpt_all_spaces` 与 `chatgpt_k12_exchange_failures`，不会丢弃已经注册成功的基础账号；只有显式开启 `strict_join` 时才把 K12 捕获失败升级为注册失败，且严格模式现在同时覆盖目标 workspace token exchange 失败。补充 `tests/test_chatgpt_k12_workspace.py`、`tests/test_chatgpt_registration_mode_adapter.py` 与 `tests/test_accounts_api_list_compact.py`，覆盖 K12 join 请求、accounts/check 解析、全空间 artifact 生成、显式关闭 K12、compact 脱敏和两阶段 RT 合并 K12 variants。

### 优化 (Changed)
- **同步前端版本号至 v1.3.0**：`frontend/src/app/AppShell.tsx` 侧边栏版本展示更新为 `v1.3.0`，用于上线后确认 K12 workspace 注册与全空间保存对应的静态资源已加载。
- **统一 K12 前端配置口径**：新增 `frontend/src/lib/chatgptK12Config.ts`，注册页、账号页注册弹窗与设置页共用 workspace id、轮询间隔、超时与保存所有空间的归一化逻辑；`chatgpt_k12_capture_refresh_tokens` 在前端统一固定为 false 并禁用展示，明确当前版本只保存 AT-only workspace variants，避免预留开关被旧配置误触发。

## [1.2.11] - 2026-07-05
### 修复 (Fixed)
- **补齐账号详情页 Web session 凭证查看能力**：`frontend/src/features/accounts/components/AccountDetailModal.tsx` 将原先拥挤的账号详情 Modal 重构为右侧 Drawer，并新增独立“凭证材料”区；Access Token、Refresh Token、ID Token、NextAuth `session_token`、完整 `cookies` 与登录密码现在都按字段显示“已保存/未保存”，默认隐藏，点击“显示/复制”时才通过 `/api/accounts/{id}/secrets` 拉取完整内容，避免把 `extra_json` 原始结构整块铺开，同时保留状态与主表 token 的基础编辑入口。
- **扩展账号 secrets 接口覆盖注册阶段 Web 会话材料**：`api/accounts.py` 的 `/accounts/{account_id}/secrets` 新增 `cookies/cookie_header/id_token/session_token` 等字段别名与长度返回，并让详情序列化暴露 `credentials` 摘要，保证上一版注册阶段保存下来的完整 cookies/session token 能被详情页可靠发现和按需读取。
- **收敛账号列表明文凭证泄漏边界**：`api/accounts.py` 的 compact 列表序列化不再返回 `token/access_token/refresh_token` 或嵌套 `extra.access_token/extra.refresh_token`，只返回 `has_access_token/has_refresh_token/has_session_token/has_cookies/has_id_token/password_present` 等布尔摘要；`tests/test_accounts_api_list_compact.py` 增加 Web session 材料回归测试，固定列表脱敏与 secrets 按需读取契约。

### 优化 (Changed)
- **账号列表复制动作改为按需读取 secret**：`frontend/src/pages/Accounts.tsx` 的 AT、RT、密码复制按钮不再依赖列表行里的明文值，而是调用 `/accounts/{id}/secrets` 获取对应字段；列表和移动端卡片继续按布尔摘要展示“有/无”，复制成功后仍保留“已复制AT”状态反馈。
- **同步前端版本号至 v1.2.11**：`frontend/src/app/AppShell.tsx` 侧边栏版本展示更新为 `v1.2.11`，用于上线后确认账号详情凭证展示修复对应的静态资源已加载。

## [1.2.10] - 2026-07-05
### 修复 (Fixed)
- **保留注册阶段已获取的 ChatGPT Web 会话材料**：`services/chatgpt_core/chatgpt_registration_mode_adapter.py` 在 RT 两阶段注册中新增第一阶段 Web session 继承逻辑，第二阶段只补 `refresh_token/id_token` 但没有拿到 NextAuth `session_token` 或完整 `cookies` 时，会沿用第一阶段 AT-only 注册已经落地的 `session_token/cookies`，并同步填充 free workspace artifact，避免完整 Auth 成功后反而把可用于后续 Web 会话复用的材料洗空。
- **防止保存账号时空值覆盖已有 NextAuth 会话**：`core/db.py` 对 ChatGPT 账号更新增加 `session_token/cookies/cookie_header` 非空保护，新保存 payload 若这些字段为空会保留数据库旧值；补充 `tests/test_chatgpt_registration_mode_adapter.py` 与 `tests/test_save_account_web_session_preservation.py` 回归测试，固定两阶段注册和通用 `save_account()` 的持久化边界。

### 优化 (Changed)
- **同步前端版本号至 v1.2.10**：`frontend/src/app/AppShell.tsx` 侧边栏版本展示更新为 `v1.2.10`，用于上线后确认本次注册持久化修复对应的静态资源已加载。

## [1.2.9] - 2026-07-05
### 修复 (Fixed)
- **修复账号处理流水线 OAIPay 上传开关无法拉取分组的问题**：`frontend/src/pages/IdeaOaiPayPipeline.tsx` 为 `/idea-oaipay-pipeline` 的“OAIPay 上传”阶段补齐分组读取状态，当启用上传开关、打开分组下拉或点击“刷新分组”时，会调用现有 `/api/integrations/oaipay-categories` 接口拉取 OAIPay 分类，并把分类 ID、名称与库存统计展示为可搜索下拉选项；接口失败时在配置卡片内直接提示全局 OAIPay API URL / API Key 检查方向，避免只看到开关无反应。
- **避免运行状态轮询覆盖未提交的流水线配置**：`frontend/src/pages/IdeaOaiPayPipeline.tsx` 将 3 秒状态轮询改为只刷新任务状态、账号明细和日志，不再每次把历史任务配置写回表单；只有页面首次进入和启动新流水线后才同步配置，防止用户刚打开 OAIPay 上传或修改 Idea/手机号参数又被轮询重置。

### 优化 (Changed)
- **同步前端版本号至 v1.2.9**：`frontend/src/app/AppShell.tsx` 侧边栏版本展示更新为 `v1.2.9`，用于上线后确认本次 `/idea-oaipay-pipeline` 静态资源已加载。

## [1.2.8] - 2026-07-05
### 优化 (Changed)
- **规整账号处理流水线配置页布局**：`frontend/src/pages/IdeaOaiPayPipeline.tsx` 将 `/idea-oaipay-pipeline` 配置页从不等宽、不等高的 `Row/Col` 卡片拼接改为“账号来源”全宽配置区 + 四个阶段配置卡片的稳定网格结构；账号来源字段按来源、范围、筛选、注册补位目标统一排布，Idea、本地状态/Gate、手机号绑定、OAIPay 上传四个处理阶段使用一致的两列字段节奏和响应式断点，避免配置区大小随机、视觉重心歪斜。
- **补齐配置页响应式与版本展示**：`frontend/src/index.css` 新增 `idea-oaipay-config-*` 作用域样式，统一配置卡片高度、字段间距、宽字段跨列、移动端单列折叠和中等屏两列阶段布局；`frontend/src/app/AppShell.tsx` 侧边栏版本号同步更新为 `v1.2.8`，方便上线后确认新的前端资源已经加载。

## [1.2.7] - 2026-07-05
### 修复 (Fixed)
- **透出 OAIPay 上传认证失败的真实服务端错误**：`services/chatgpt_core/oaipay_upload.py` 在 OAIPay 上传接口返回 `401/403` 或其他 HTTP 错误时，新增对响应 JSON 中 `detail/message/msg/error` 的统一提取，日志与任务结果会展示 `上传失败: HTTP 401: 上传密钥无效` 这类可操作原因，不再只保留泛化的 `上传失败: HTTP 401`；`services/oaipay_sync.py` 的远端账号探测同样透出 `detail`，避免上传前探测阶段把密钥问题误报成普通不可达。
- **补充 OAIPay 401 诊断回归测试**：`tests/test_oaipay_sync.py` 新增上传与远端探测两个 401 场景，固定 FastAPI 风格 `{"detail":"上传密钥无效"}` 的错误提取行为，防止后续接口兼容重构再次吞掉真实错误详情。

### 优化 (Changed)
- **同步 OAIPay 设置页上传密钥文案**：`frontend/src/pages/Settings.tsx` 将 OAIPay API Key 标签从“管理员密码”改为“API Key / 上传密钥（gpt.cccy.me 的 UPLOAD_KEY）”，与 `gpt.cccy.me` 安全加固后的独立 `UPLOAD_KEY` 口径保持一致；侧边栏版本展示同步更新为 `v1.2.7`，方便上线后确认新前端资源已加载。

## [1.2.6] - 2026-07-05
### 新增 (Added)
- **新增账号处理流水线串联注册、本地账号、Idea、手机号与 OAIPay**：新增 `docs/idea-oaipay-pipeline/design.md` 作为方案文档，并增加 `services/idea_oaipay_pipeline/`、`api/idea_oaipay_pipeline.py` 与 `/api/idea-oaipay-pipeline/*` 接口，支持从“注册新账号”或“本地账号快照”启动同一条账号级流水线，再按配置执行 Idea 批量提交、本地状态刷新、状态 Gate、手机号绑定和 OAIPay 上传。流水线不再把 Plus 写成全局硬门槛，`status_gate` 可按 `none / account_valid / subscription_in / upload_ready` 放行，支持 Free 账号只做手机号绑定或继续上传的业务形态。
- **新增流水线持久化任务与账号明细表**：`core/db.py` 接入 `idea_oaipay_pipeline_tasks` 与 `idea_oaipay_pipeline_items`，为每个账号独立记录 `register_stage / idea_stage / check_stage / gate_stage / phone_stage / oaipay_stage / overall_status`、子任务 ID、Idea order/card 信息、手机号策略、OAIPay 远端状态和失败原因；服务启动时 `main.py` 会恢复最近仍在运行/暂停的流水线，避免长任务只存在内存态。
- **新增管理端“账号处理流水线”页面**：`frontend/src/pages/IdeaOaiPayPipeline.tsx`、`frontend/src/app/router.tsx`、`frontend/src/app/AppShell.tsx` 新增独立入口 `/idea-oaipay-pipeline`，提供运行摘要、配置表单、账号明细表、主流水线日志和子任务日志 Drawer。页面复用现有 `TaskLogPanel` 查看注册、Idea、手机号等子任务日志，明细操作支持单账号重刷状态与重传 OAIPay。

### 优化 (Changed)
- **手机号绑定与 OAIPay Gate 按策略解耦**：流水线手机号步骤支持 `disabled / best_effort / required`，`best_effort` 失败不会阻断后续 OAIPay，`required` 才会把账号停在人工处理；OAIPay 上传条件支持单独配置订阅类型与是否要求手机号，不再默认要求 Plus。手机号判定同时识别真实绑定记录 `chatgpt_phone_binding` 和 OpenAI 已绑定手机号记录 `chatgpt_bound_phone`，避免已有绑定账号被重复执行绑定。
- **子任务丢失、注册回收与无 OAIPay 场景补强**：Idea / 手机号 / 注册子任务引用在服务重启后若已从内存任务表丢失，流水线会清理 active child task 并把相关账号标记为可排查状态，不再无限卡在 polling/running；进程退出只停止调度线程，不再把数据库中的运行中任务误改为人工停止，确保下次启动可恢复最近流水线。当 OAIPay 未启用时，`oaipay_stage=disabled` 的账号也会在 Gate 与手机号策略满足后正确归档为 `done`。注册任务新增 `registered_accounts` 结构化结果快照，流水线优先按 account_id/email 回收批量注册成功账号，避免只读最后一条 `TaskLog` 导致漏收多账号注册结果。

### 修复 (Fixed)
- **补充流水线边界回归测试**：新增 `tests/test_idea_oaipay_pipeline_config.py`，覆盖 Pydantic `source.register` alias、来源/手机号策略校验、OAIPay 禁用时的账号完成归档、本地来源匹配 0 个账号的失败收口、历史任务重试拦截，以及已有手机号绑定记录的识别，防止后续把 Free 账号绑定手机号、非 Plus 放行或 OAIPay 可选流程再次写死。

## [1.2.5] - 2026-07-05
### 优化 (Changed)
- **动态代理任务表单改为默认复用全局模板**：`frontend/src/lib/taskProxySettings.ts` 调整任务代理配置读取与校验逻辑，当全局 `dynamic_proxy_template` 已保存时，注册、账号本地状态同步、手机号绑定和邮箱测活等任务弹窗不再把代理链接作为动态代理必填项；任务内“动态代理模板”字段改为“可选覆盖”，留空时后端继续使用全局模板，只要求填写出口国家并按国家改写 `region-XX`、刷新 `sid`。
- **同步动态代理表单文案与版本展示**：`frontend/src/pages/RegisterTaskPage.tsx`、`frontend/src/features/auth/components/RegisterTaskModal.tsx`、`frontend/src/pages/Accounts.tsx`、`frontend/src/pages/CustomEmailRecheckPage.tsx` 将动态代理模板标签、占位符和说明统一改为“留空使用全局模板；填写后仅本次任务覆盖”，避免操作者误以为每个任务都必须重复粘贴 Cliproxy 链接；侧边栏版本号同步更新为 `v1.2.5`。

## [1.2.4] - 2026-07-05
### 修复 (Fixed)
- **修复 Cliproxy 动态代理指定国家被 GeoIP 限流误判失败的问题**：`core/proxy_utils.py` 的动态代理候选探测现在区分“基础连通失败”“实测国家不一致”和“GeoIP 无法实测”三种状态；当 Cliproxy 模板已经按 `region-XX` 成功改写到目标国家、基础出口 IP 可用，但第三方 GeoIP 查询返回 429 或无国家时，不再把 `actual=unknown` 误判为 `country_mismatch` 并丢弃所有候选，而是记录 `actual=unverified / probe=geo_unavailable` 后允许任务继续执行。只有实测到明确的其他国家时才继续硬失败，避免 JP/US 等可用出口被探测依赖误杀。
- **修复动态代理运行态偷偷把 Cliproxy `socks5://` 改成 `http://` 的协议漂移**：`core/proxy_utils.py` 的 `normalize_proxy_url()` 不再把 Cliproxy Socks5 模板降级成 HTTP，而是统一规范为 `socks5h://` 以保持 Socks5 协议并使用远端 DNS；动态代理日志、预览接口和任务执行现在与 Cliproxy Socks5 中间服务器语义一致，同时避免本地 DNS/普通 `socks5://` 在部分 HTTPS 目标上触发 `WRONG_VERSION_NUMBER`。

### 优化 (Changed)
- **动态代理国家探测优先使用代理出口侧 Cloudflare Trace**：`services/proxy_scanner.py` 新增通过代理访问 `https://www.cloudflare.com/cdn-cgi/trace` 的国家探测路径，优先读取 `loc=XX` 作为出口国家，再回退到服务器侧 `ipapi/ipinfo` 查询；代理扫描、动态代理预览 `/api/proxies/dynamic-preview` 和任务运行前探测都能减少公共 GeoIP API 429 对国家判断的影响，并在响应中暴露 `geo_source`、`geo_unverified` 等诊断字段。
- **同步前端版本号至 v1.2.4**：`frontend/src/app/AppShell.tsx` 侧边栏底部版本展示更新为 `v1.2.4`，用于上线后确认本次动态代理修复的静态资源已经加载。

## [1.2.3] - 2026-07-05
### 优化 (Changed)
- **恢复手机号绑定面板三策略与统一号段选择器**：`frontend/src/pages/Accounts.tsx` 将原先只有“普通绑定/号段抽样”开关的面板恢复为 `普通绑定`、`限定号段绑定`、`号段抽样测试` 三种取号策略，并用同一个“号段范围”选择器承载两类号段语义。限定号段绑定会把所选号段作为正式绑定约束，号段抽样测试继续按每段 1/2 个号码执行；可用、不可用、暂不可用、已绑满号段均只作为状态提示展示，不再在前端禁点，容量与实时可用性交给后端按 `prefix_bind_enabled` / `prefix_sample_enabled` 判定。
- **压缩“只测发码/收码”控件为任务级小开关**：手机号绑定弹窗里的 `prefix_sms_probe_only` 控件从大块提示卡压缩为紧凑行内 `Switch`，并明确文案为粘贴号码、普通手机号池、限定号段、号段抽样都生效；开启后仍会自动关闭“尽量用满同一个手机号”，避免探测模式重复消耗同一号码。
- **同步前端版本号至 v1.2.3**：`frontend/src/app/AppShell.tsx` 侧边栏底部版本展示更新为 `v1.2.3`，用于上线后确认本次手机号绑定面板恢复的静态资源已经加载。

### 修复 (Fixed)
- **修复限定号段绑定缺少前端入口的问题**：任务提交时重新从 `phone_pool_mode` 推导 `prefix_bind_enabled`、`prefix_sample_enabled` 与 `selected_prefixes`，限定号段绑定会要求至少选择一个号段并把所选号段传给 `/tasks/chatgpt/phone-binding-test`；号段抽样在手动选择号段时优先使用 `selected_prefixes`，未选择时才使用“全部号段 / 测试可用号段 / 仅不可用号段”的筛选范围，避免前端看不到号段或提交后退化成普通号池取号。

## [1.2.2] - 2026-07-05
### 修复 (Fixed)
- **修复手机号池收码 API 复制仍只拿到链接本身的问题**：`frontend/src/pages/PhonePool.tsx` 新增统一的 `phoneApiCopyText()` / `copyPhoneApiLine()` 复制入口，手机号列、桌面端“收码 API”列、移动端卡片和诊断抽屉均按 `手机号----完整收码API` 格式写入剪贴板；例如 `+12094908876----https://sms.aa8.pl/api/record?token=...`，避免只复制手机号或只复制 API URL 后无法直接回填到绑定任务。
- **恢复“收码 API”列显式复制按钮**：桌面表格和移动端 API 字段重新提供带 `CopyOutlined` 的“复制完整API”按钮，底层使用项目内已验证的 Clipboard API + textarea fallback，而不是只依赖 AntD 文案复制图标，减少非安全上下文或浏览器权限导致的复制失败。

### 优化 (Changed)
- **同步前端版本号至 v1.2.2**：侧边栏底部版本展示由 `v1.2.1` 更新为 `v1.2.2`，用于上线后确认本次手机号池复制修复的静态资源已经加载。

## [1.2.1] - 2026-07-04
### 新增 (Added)
- **新增任务代理设置统一保存与复用**：代理管理页“动态代理预览”新增“保存为任务默认”入口，并新增 `frontend/src/lib/taskProxySettings.ts` 作为注册任务、批量本地状态同步、邮箱测活、手机号绑定等任务表单的统一代理配置适配层；用户在任一任务中填写的代理模式、动态代理模板/API、出口国家、失败处理、代理池候选数与最低健康分会写入 `/api/config` 的 `task_proxy_*` 全局配置，并同步镜像到 `dynamic_proxy_template` / `dynamic_proxy_default_country`，后续所有任务弹窗默认读取同一套代理参数，避免注册、本地刷新、测活、手机号绑定各自记一份导致配置漂移。
- **新增动态代理模式，支持按需指定出口国家**：后端新增 `core/dynamic_proxy.py` 与统一候选解析入口，注册任务、批量本地状态同步、邮箱测活、HME 复测和手机号绑定现在均支持 `proxy_mode=dynamic`。该模式使用任务内或全局配置的动态代理模板，按 `proxy_country_code` 改写 `region-XX`，每次候选生成刷新 `sid-xxx-t-`，并保留现有 cliproxy `socks5://` 到运行态 `http://` 的兼容转换。
- **新增动态代理出口预览接口与前端入口**：代理管理页新增“动态代理预览”卡片，调用 `/api/proxies/dynamic-preview` 生成脱敏运行代理并可按需实测出口 IP/国家；全局配置页新增动态代理默认模板、默认出口国家、国家严格匹配、运行前探测与探测超时配置，便于在任务页复用。
- **前端任务表单增加“动态代理”代理模式**：注册页、账号页注册弹窗、批量本地状态同步弹窗、邮箱测活页与手机号绑定高级设置均新增动态代理选项，并明确区分“指定代理失败回退代理池”和“动态代理失败刷新 sid 重试”的候选语义。

### 安全 (Security)
- **强化动态代理与任务日志脱敏边界**：动态代理模板、运行代理、批量本地状态同步参数、邮箱测活结果和 HME 复测详情在任务 meta、历史日志与预览接口中只保存脱敏地址；新增对 `dynamic_proxy_template`、`proxy_template` 等结构化字段的统一代理认证脱敏，避免代理账号、密码或 sid 模板信息进入可展示日志。

### 修复 (Fixed)
- **恢复 ChatGPT 账号池分页切页与每页数量选择**：根据 2026-06-17 账号列表性能审计时保留的历史实现，补回 `frontend/src/pages/Accounts.tsx` 中的 `auto-chatgpt.accounts.page-size.v1` 本地持久化、默认每页 20 条以及 10 / 20 / 50 条切换选项，并将当前页码、页大小、后端 `/accounts?page=&page_size=` 查询和 `AccountsTable` 的 `Pagination` 联动起来；修复近期回归后账号池只能固定 10 条且桌面端分页器不再显示每页数量切换的问题，避免运营跨页查看和批量选择账号时被固定页长卡住。
- **恢复手机号绑定粘贴号码的手机号池 upsert 与状态回写链路**：修复 `api/tasks.py` 在日志重构后把手动粘贴的 `手机号----收码API` 仅作为一次性任务输入、未再调用 `_import_manual_phone_entries_to_pool` 的回归问题。现在手机号绑定任务启动时会先将粘贴号码 upsert 到 `phone_pool`：新号码直接创建池记录，已存在号码会保留原有池记录并更新本次粘贴的收码 API；随后任务仍按粘贴的固定号码列表执行，不切换为动态取号，但每个号码会标记为 `pool_managed`，运行结束后继续通过 `PhonePoolRepository.record_task_status()` 回写绑定成功、短信探测成功、OpenAI 拒绝、限流、无验证码等状态。同步修正短信探测模式误继续进入绑定保存/Auth 重试的保护分支，并补充任务日志，明确展示粘贴号码启动前入池和运行后状态回写结果。
- **修复手机号绑定“只测发码/收码”前端开关丢失的问题**：补回 `frontend/src/pages/Accounts.tsx` 中全局可用的 `prefix_sms_probe_only` 表单项与本地设置保存，任务提交时同时下发 `prefix_sms_probe_only` / `sms_probe_only`，确保粘贴号码、普通手机号池、号段抽样三种模式都能进入后端短信探测分支；开启后前端会强制关闭“尽量用满同一个手机号”，任务提示也会明确显示“仅测发码/收码”，避免用户以为已开启但实际仍执行真实绑定。
- **修复批量本地状态同步动态代理异常后任务悬空的问题**：`api/tasks.py` 的 `_run_batch_probe_local_status` 增加普通 `Exception` 兜底捕获，动态代理模板缺少出口国家、缺少 `region-XX`、探测解析异常或其他未预期错误都会写入任务日志、保存失败历史并将任务状态标记为 `failed`，不再让后台任务崩溃后前端持续显示 pending/running；同时动态代理国家 fallback 与前端必填校验保持一致，缺省时使用全局默认国家。
- **修复 Idea 开通成功后账号本地状态只被外部 paid 标记覆盖的问题**：`api/tasks.py`、`api/baxigpt_cdk_pool.py` 与后台 `baxigpt_status_poller` 在上游订单确认 paid 后，改为先执行 `sync_chatgpt_account_local_status` 本地刷新，由真实 ChatGPT 探测结果统一更新账号 `status`、订阅能力和上传门禁；卡密侧 paid/order_id 仍写入 `extra.baxigpt_cdk`，并将 `local_status_refresh` 摘要同步回账号，避免仅凭外部支付成功把账号状态简单改成 `subscribed` 而遗漏订阅计划、auth/codex 状态等本地字段。
- **修复 Idea 批量提交轮询全部超时的真实任务 ID 匹配问题**：`services/chatgpt_core/baxigpt_client.py` 现在优先消费上游 `/api/task/submit` 返回的 `created_tasks` 真实任务 ID，并禁止把 Access Token 前缀生成的 `fallback_*` 当成可轮询订单；当旧上游未返回任务 ID 且无法可靠匹配时，会直接返回“上游已受理但未返回可轮询任务ID”的明确失败，避免继续轮询不存在的订单直到超时。
- **修复 Idea 上游失败原因丢失与 summary 误导问题**：`BaxiGptClient.status/query` 已兼容读取上游 `fail_reason/raw_fail_reason`，任务执行器 (`api/tasks.py`) 增加上游已受理数与轮询超时数统计，summary 不再出现“成功 0、跳过 0、失败 0”但实际全是 timeout 的错误表达。
- **修复 Idea 提交后本地状态未写入 order_id 的问题**：`api/tasks.py` 在每个账号提交成功后立即将真实 `order_id/display_id` 写入卡密提交记录和账号 `extra.baxigpt_cdk`，并为每个活动订单保留独立记录快照，避免同一卡密多账号提交时共享同一个内存记录导致最后一个账号覆盖全部轮询目标。
- **修正动态代理表单误展示代理池筛选项的问题**：注册页、账号页注册弹窗、批量本地状态同步、邮箱测活和手机号绑定的动态代理模式现在只展示动态模板、出口国家和“失败后刷新 sid 重试”，不再展示或提交“最低健康分”“最多候选/候选代理数量”等代理池专属参数；后端动态代理重试次数也改为独立的 `dynamic_proxy_max_attempts` 默认值，不再复用代理池候选数配置。
- **修复批量上传时丢失密码与手机号等附加信息的问题**：在同步账号到 OAIPay (`upload_chatgpt_account_to_cpa` 等批量操作) 过程中，修正了 `build_chatgpt_sync_account` 构造伪账户对象时遗漏了 `password` 和 `extra` 的问题，从而彻底修复了该场景下上传的账号在兑换界面只有兜底网关而没有真实专属接码链接的问题。
- **OAIPay 账号数据上传修复**：修复了上传至 OAIPay (gpt.cccy.me) 时没有包含绑定的手机号（`chatgpt_bound_phone_number`）以及本地接码网关配置（`local_phone_gateway_url` 等）的问题，现在它们会作为顶层字段被包含在 `accounts` 的对象中，使得下游接收方可以正确生成对应的 `delivery_data`。

### 优化 (Changed)
- **同步前端版本号至 v1.2.1**：侧边栏底部版本展示由 `v1.2.0` 更新为 `v1.2.1`，用于区分本次账号池分页恢复发布与 2026-07-03 的 `v1.2.0` 基线，方便上线后从 live 页面直接确认静态资源已更新。
- **OAIPay 账号数据上传优化**：
  - 增强向 OAIPay 管理系统上传账号的字段丰富度。原先 `extra_info` 仅简单上传 Access Token，现重构为将该账号的 Refresh Token、绑定的手机号 (phone) 以及手机号接收验证码的完整 API 链接 (api_url) 等字段组装合并为一个完整紧凑的 JSON 字符串（包含所有的 `token_data`），使下游接收方能一次性获取该账号的所有关键会话和关联信息。
- **优化手机号池列表的复制体验**：
  - 将手机号列原有的单纯手机号复制更新为合并复制，格式为 `手机号----API`，并在前端（PC端和移动端）移除收码 API 列独立且冗余的复制按钮，提升了数据提取的便捷性。
## [1.2.0] - 2026-07-03
### 新增 (Added)
- **前端版本号展示与规范化版本控制**：
  - **网页前端版本展示**：在侧边栏导航底部（或全局布局中）新增系统当前版本号显示，便于直观确认代码更新情况。
  - **严格遵守语义化版本控制**：完善 `AGENTS.md` 规范，对每次变更执行大/小版本更迭规范并在 Changelog 中明确标注，以此驱动项目的规范化演进。
### 新增 (Added)
- **本地状态批量同步增设日志面板与任务后台化**：
  - **实时进度与日志面板**：在 ChatGPT 账号列表页面 (`Accounts.tsx`) 触发批量同步本地状态（`probe_local_status`）时，现改为由后台任务处理并弹出实时任务日志面板 (`TaskLogPanel`)，支持实时的进度反馈与详细控制台输出查看。
  - **后台批量处理接口**：后台新增 `/chatgpt/probe-local-status/batch` 任务提交与处理端点 (`api/tasks.py`)，支持将批量状态同步任务转入异步后台执行，避免大批量处理时页面长连接阻塞与超时。
- **本地状态批量同步增设代理与延时配置功能**：
  - **前台配置弹窗支持**：在 ChatGPT 账号列表页面 (`Accounts.tsx`) 的“同步本地状态”下拉菜单中，针对选中账号及筛选账号均新增了“配置代理与延时”入口，并提供了与注册任务保持一致的参数配置弹窗。
  - **代理模式与重试容灾**：支持用户参考注册任务，为批量本地状态同步（`probe_local_status`）配置代理池自动选取、手动指定代理或直连模式；并支持配置国家缩写、最低健康度与候选数量，开启多代理候选自动重试切换（Failover）。
  - **随机延时平滑执行**：后台批量执行引擎 (`api/actions.py`) 针对批量本地状态探测等平台动作增加了区间随机延时（如 `delay_seconds` 至 `delay_max_seconds`），并在每个账号探测之间自动加入等待时间，避免并发过高触发风控限流。
- **账号同步日志强化展示网络代理与出口 IP 信息**：
  - **精细化代理使用记录**：在触发账号状态批量同步等任务时，现在会在任务执行日志中精准记录每一次探测动作所使用的代理地址（`proxy_url`）及其实际出口 IP（`exit_ip`）。当配置为多代理轮换或风控 fallback 时，日志能清晰展示对应连通性与节点网络详情。
  - **手动指定代理智能解析**：即使用户在界面中选择“手动指定代理”（`mode="specified"`），系统也会自动在底层的代理记录池中尝试回溯解析该代理的出口国家（`country`）与出口 IP；若该私有代理未曾录入本地库，还会通过实时接口 (`ip-api.com`) 进行秒级回源诊断，告别以前单调的 `specified` 占位符。
### 优化 (Changed)
- **优化 OAIPay 账号上传验证与自动分组逻辑**：
  - **放宽上传验证条件**：在账号同步（`oaipay_sync.py`）中，当向 OAIPay 上传账号时，不再强制要求账号必须包含 Access Token（`at`）或 Refresh Token（`rt`），仅需检测账号的基础有效性（`auth_level != "invalid"`）即可放行上传，提高了账号导入的兼容性。
  - **基于订阅与 RT 状态的智能自动分组**：在构建 OAIPay 上传载荷（`oaipay_upload.py`）时，新增基于账号类型及 Refresh Token（`rt`）存在与否的智能分组功能：
    - 含有 RT 的 Plus 账号默认自动归类到 `PLUS--已接美国长效`；
    - 不含 RT 的 Plus 账号自动归类到 `PLUS--未接码`；
    - 含有 RT 的 Free 账号自动归类到 `FREE--已接码带RT`。

### 修复 (Fixed)
- **修复 OAIPay 自动分组时 Plus 账号错误识别为 Free 的问题**：
  - **透传最新能力快照**：修复了在构建待上传的 `sync_account` 镜像时，因为历史缓存在内存中未随最新状态同步更新，导致 Plus 账号因缺失最新订阅快照而在底层被误分到 Free 组的问题。现在系统会将刚刚完成连通性探测的最新 `capabilities` 快照透传给上传组装器，确保账号分类严格、精准。
- **修复 Idea 批量提交功能无法弹出日志面板的问题**：
  - **移除未定义函数调用**：修复了在后端任务队列分配 (`_resolve_baxigpt_cdk_submit_accounts`) 时，调用了未定义的本地校验函数 `_baxigpt_cdk_ineligible_reason` 导致触发 500 内部服务错误的问题。通过移除该无效引用，确保账号合法性校验逻辑与全量筛选分支统一（仅校验 Access Token 存在与否），恢复了提交页面的正常运作与日志面板正常弹出。
- **修复 OAIPay 自动分组失败的问题**：
  - **动态映射分组 ID**：修正了自动分组逻辑，在向 OAIPay 提交数据前，会先拉取对方的分类列表（`/api/auto-gpt/categories`），动态将分组名称（如 `PLUS--已接美国长效`）转换为系统所需的数字 ID 进行上传，避免因为提交中文字符串导致分组无效或被归为默认。

- **修复因状态探针网络异常导致账号原有订阅状态（如 Plus）被重置丢失的问题**：
  - **后端智能 Fallback**：修改了账号能力判定策略 (`chatgpt_account_state.py`)。当探测失败、API 超时或因代理故障导致本次任务无法提取出计划信息（返回 `"unknown"`）时，将不再覆写原有的订阅级别，而是自动回退并保留账号字典 `chatgpt_capabilities` 中最后一次成功记录的 `subscription_plan`。以此确保原本为 Plus/Team 级别的账号在遭受偶然断网时不会在前端被降级或错标为 Free。
- **修复本地状态批量同步及日志面板因导入不存在的代理解析方法导致异常崩溃无法工作的问题**：
  - **纠正代理解析函数引用与候选列表调用结构**：修复了在异步批量处理函数 `_run_batch_probe_local_status` (`api/tasks.py`) 中错误导入了未定义的 `build_account_action_proxy_candidates` 与 `format_proxy_candidate_label` 导致后台任务启动即发生 `ImportError` 崩溃的问题；将其修正为调用标准的 `resolve_probe_candidate_proxies` 代理解析函数。
  - **修复代理切换与日志面板汇报逻辑**：正确处理由候选列表解析出的 `(proxy_url, proxy_pool, source)` 元组数据结构，规范化对代理名称、网络失败自动切换 (Failover) 及向代理池 (`proxy_pool`) 汇报成功与失败的状态回调，从而确保本地状态批量同步的进度展示、代理重试与延时等候日志均能在日志面板 (`TaskLogPanel`) 中正确打印和更新。
- **修复 Codex 剩余用量列表中 5 小时与 7 日/30 日窗口剩余量混淆未区分的问题**：
  - **窗口严格分离与时长校验**：后端 Codex 用量构建逻辑（`codex_usage.py`）与前台数据归一化（`Accounts.tsx`）中增设窗口时长校验（<=360分钟为 5h 短期窗口，>360分钟为 7d/30d 长期窗口），彻底解决因缺失对应短/长窗口字段而盲目互用 fallback 导致 5h 与 7d/30d 用量互串和重复展示的问题。
  - **长期窗口动态自适应标签展示**：在账号池列表（`Accounts.tsx`）及 Codex 监控列表（`CodexUsagePage.tsx`）中，根据实际检测到的额度周期（如 `window_minutes >= 20000`），将长期窗口动态精准显示为 `30d` 或 `7d`（及对应前缀 `[30d]` / `[7d]`），避免将 30 天月期用量误标为 7 天。
- **修复手机绑定成功后 RT 与手机 API 信息未在账号列表保存展示的问题**：
  - **解决并发持久化冲突**：修复在执行手机绑定或更新账号状态时，由于 `session.refresh()` 遗漏导致内存中历史字段字典覆盖最新写入数据的问题，确保获取到的 `refresh_token`、`chatgpt_phone_binding`、`chatgpt_bound_phone` 实时写入 DB。
  - **兼容旧版与新版数据读取 fallback**：重构并优化账号列表状态构建函数与号码绑定模块（`bound_phone.py` / `accounts.py`），支持在历史兼容字段 (`chatgpt_bound_phone`) 缺失或字段名调整时，自动从新版字典字段读取号码与 API 渠道，确保绑定信息在 UI 上稳健展示。

## [1.1.0] - 2026-07-02
### 新增 (Added)
- **项目规范与日志体系沉淀**：
  - 建立 [`changelog.md`](file:///opt/auto-gpt/changelog.md) 更新日志文档，并将其维护规范写入 [`AGENTS.md`](file:///opt/auto-gpt/AGENTS.md)，强制约束 AI Agent 和开发者在发生代码、功能或配置变更后必填更新日志，确立项目演进追踪标准。
- **Codex 额度与监控体系全面整合**：
  - **账号池列表富文本展示**：将 Codex 额度用量与运行状态监控深度整合至 ChatGPT 账号池列表的高级富文本字段中，支持直观显示每个账号的实时剩余配额与授权状态。
  - **监控列表与状态列恢复**：100% 恢复 7 月 1 日原版 Codex 用量列的设计与交互逻辑，修复并恢复了独立的 Codex 额度监控列表页面与账号池 Codex 状态列展示。
- **Idea 批量提交与多额度自动容灾支持**：
  - **单卡多额度批量提交 (`feat(idea)`)**：重构并全面升级 Idea 提交模块，升级为 Idea 批量提交引擎。原生支持单卡绑定多额度与自动负载分配。
  - **故障隔离与智能重试**：新增提交失败自动标记无资格（ineligible）的熔断风控机制，并在当前账号/卡号失败时自动选取并重试其他可用账号，极大提高了批量业务提交的最终成功率。
  - **前后端诊断文案同步**：实现前台 UI 页面提示与后台服务运行日志中关于 Idea 批量提交及异常诊断文案的实时精确同步，提升问题定位效率。
- **本地短信验证码网关支持 (Local SMS Gateway)**：
  - 新增本地短信验证码网关 (`sms-verification-gateway` / `smstome_tool.py`)，支持本地接收、解析与处理短信验证码（OTP），完善自动化注册链路闭环。
- **外部订阅链接服务与 API 扩展 (External Subscription Link API)**：
  - 新增外部订阅链接生成与管理 API，支持一键生成、查询和分发订阅链接。
  - 增加外部订阅链接的本地有效性自检功能，防止分发失效链路。
  - 完善接口指引文档，新增外部订阅 API 的详细使用说明 (`docs: add external subscription API usage help`)。

### 优化 (Changed)
- **Idea 批量提交引擎重构 (Single Sequential Submission)**：
  - 完全模拟人工提交模式，针对多额度卡密提交到部分上游（如 `submit.cccy.me`）时可能因批量并发引发的接口不兼容与丢单报错进行深度重构。
  - **前置动态查额度**：在为每一个账号发送提交请求前，强制进行一次 `/api/task/cdk/check`，只有查到当前真实额度 `remaining > 0` 才进行单一账号提交，避免将失效或已用完的卡密推给上游。
  - **单账号隔离提交**：废除批量发送 `accounts: [...]` 的结构，将每次发包严格控制为单一 Token。
  - **失败额度回收与重试**：提交完成后若遭遇失败（如网络抖动），当前排队账号将回滚队列。在外层循环中，重新查验该卡密，若上游未扣除额度，则再次利用该卡密重试，做到多额度卡密完全零浪费。
- **OAIPay 账号状态探测与上传兼容性优化**：
  - 全面增强 OAIPay 账号状态的自动探测能力与接口数据上传兼容性，支持批量多候选目标查询与灵活的高可用上传接口。
  - 引入容错降级逻辑：在网络探测不可连接或超时的情况下，系统不会直接丢弃任务，而是继续尝试直接发起强制上传流程，提升弱网及复杂网络环境下的账号存活利用率。
- **团队邀请与自动新注册导流优化 (ChatGPT Team Onboarding)**：
  - 深度优化 ChatGPT Team 团队邀请链条与自动化导流注册的交互流程，减少邀请链接在多步跳转中的丢单率。
- **Docker 多实例架构与自动化容器编排重构**：
  - **编排网络规范化**：统一将多实例 Docker Compose (`docker-compose.multi.yml`) 与默认编排的网络命名和子网配置规范为 `auto-gpt_default`，消除网桥冲突。
  - **并发构建优化**：移除多实例 Compose 文件中冗余的 `build` 构建块定义，避免在双实例或多容器并行拉起时发生 Docker 镜像标签 (Image Tag) 的并发写锁与标签覆盖冲突。
- **工程开发规范升级**：
  - 升级项目的 AI 与开发者操作约定 (`AGENTS.md`)，强制规定任何 Agent 在完成工作区代码、前端资源或配置文件修改后，必须主动执行根目录下的 `./deploy.sh` 进行版本存档、容器编译与在线服务更新。

### 修复 (Fixed)
- **修复 Idea 批量提交按钮无反应的问题**：
  - 修复在 `Accounts.tsx` 中 `submitBaxiCdkSubmit` 方法内因 `validateFields()` 发生校验错误时抛出未捕获的 Promise Rejection 导致页面无任何反应和反馈的问题，现在会正确捕获并提示用户检查表单参数（尤其是被折叠隐藏的高级参数区域）。
- **前端一键复制 Access Token 修复**：
  - 修复了后端在序列化紧凑版账号列表 (`compact account list serializer`) 时遗漏 Access Token 字段的问题，确保前端管理界面中“一键复制 AT”功能能够准确获取并剪贴完整的 Token 数据。
- **已支付 Checkout (Already-Paid) 流程处理修复**：
  - 修复当遇到已经完成支付 (Already Paid) 的 Checkout 会话时系统报错卡死的问题，现能够正确识别状态并平滑继续后续业务或归档流程。
  - 将已支付但关联注册失败的 Checkout 会话准确计入系统“失败注册”分类监控指标中，避免数据大盘统计失真。
  - 优化并重构对已支付 Checkout 返回值的分类解析算法，避免将成功付款的凭证误判为网络异常。
  - 对无 Refresh Token (No-RT) 的 Checkout 会话，将其默认结算货币统一规范并设置为美元 (`USD`)。
- **外部订阅与兑换逻辑修复**：
  - 修复外部订阅链接可能因高并发请求被重复声明或二次领用 (Duplicate Claims) 的并发竞争 Bug。
  - 修复前端兑换页面 (`fix(ui): ensure redeem confirm always triggers request`) 中，兑换确认弹窗在特殊点击事件下无法触发后台发包请求的交互失灵问题。
- **凭证上传与手动授权加固**：
  - 加固手动授权登录 (Manual Auth) 与 `sub2api` 系统的凭证上传管道，增加了针对非法 Token 解析与超时中断的异常防护。

---

## [1.0.0] - 历史基线与核心架构
### 记录 (Project Baseline)
确立了 ChatGPT 账号自动注册、资源池管理与自动化交付的核心体系结构：
- **核心框架**：基于 Python/FastAPI（与异步调度系统）构建的高性能后端服务，搭配多实例数据卷驱动的 SQLite 数据库集群（`/opt/auto-gpt` 为主要研发源码与前端构建路径，`/opt/auto-gpt-plus` 为增强实例运行态与数据池隔离路径）。
- **自动化注册与风控处理**：
  - 集成 Web UI 账号池可视管理、自动化批量注册、工作流状态同步及代理池调度机制。
  - 内置本地 Turnstile Solver 自动化拉起模块、Sentinel 验证重试策略、OTP 短信验证码优先复用以及 Cloudflare Cookie 智能预热修复算法。
  - 沿袭 `any-auto-register` 系列优秀的插件架构体系与 Web UI 任务调度能力，并融入 Cloudflare Worker 临时邮箱等核心反风控套件。
- **多容器双实例全栈部署体系**：
  - **主服务实例 (`auto-gpt`)**：对外服务端口 mapping `0.0.0.0:8000->8000/tcp`、`0.0.0.0:8317->8317/tcp`。
  - **Plus 增强实例 (`auto-gpt-plus`)**：增强服务端口 mapping `0.0.0.0:8001->8000/tcp`、`0.0.0.0:8318->8317/tcp`。
  - **一键运维自动化**：配备由 `deploy.sh` 驱动的极速 CI/CD 工具链，支持常规多实例热升级 (`--mode=multi`) 与极速秒级容器代码热注入 (`--mode=hot`)。
- **多维度资源管理**：
  - 实现了 ChatGPT 账号库存、使用状态 (`used`)、Access Token 管理、订阅状态、关联绑定手机号与邮箱等核心元数据的精细化边界管理与解析隔离。

### 修复 (Fixed)
- 修复 `idea批量提交` 功能点击后无反应（Log面板不弹起）的Bug：
  1. 后端 500 报错修复：旧版热更新后进程未重启，导致执行了包含已废弃 `_baxigpt_cdk_ineligible_reason` 方法的脏缓存，已通过硬重启 `auto-gpt` 容器清理旧版进程态。
  2. 修复网络超时挂起：`auto-gpt` 容器通过 `host-gateway` (172.17.0.1) 访问上游 `openai-pay-submit:8789` 时超时。原因是上游服务 `docker-compose.yml` 错误地仅绑定了 `127.0.0.1` 导致容器网桥无法通信。现已将上游端口绑定调整为 `172.17.0.1:8789:8789` 并重建容器。

## 2026-07-04 20:23:12 +0800
- 规范 auto-gpt 发布门禁
- 发布模式: multi

## 2026-07-04 20:27:04 +0800
- 修复手机号绑定粘贴号码自动入池与状态回写
- 发布模式: multi

## 2026-07-04 20:37:11 +0800
- 修复手机号绑定只测发码收码前端开关
- 发布模式: multi

## 2026-07-04 20:54:27 +0800
- 恢复账号列表分页切页设置
- 发布模式: multi

## 2026-07-05 01:21:50 +0800
- 修复手机号池完整API复制按钮
- 发布模式: multi

## 2026-07-05 03:34:35 +0800
- 恢复手机号绑定三策略号段选择器并压缩只测发码开关
- 发布模式: multi

## 2026-07-05 04:14:12 +0800
- 修复 Cliproxy 动态代理按国家探测误判
- 发布模式: multi

## 2026-07-05 04:20:12 +0800
- 修正动态代理 Socks5 运行态为 socks5h
- 发布模式: multi

## 2026-07-05 04:37:06 +0800
- 动态代理任务表单默认复用全局模板
- 发布模式: multi

## 2026-07-05 05:49:09 +0800
- 新增账号处理流水线串联Idea手机号OAIPay
- 发布模式: multi

## 2026-07-05 06:12:16 +0800
- 修复 OAIPay 上传密钥文案和 401 错误详情
- 发布模式: multi

## 2026-07-05 06:57:07 +0800
- 规整账号处理流水线配置页布局
- 发布模式: multi

## 2026-07-05 07:24:34 +0800
- 修复账号处理流水线 OAIPay 分组获取
- 发布模式: hot

## 2026-07-05 19:43:13 +0800
- 保留注册阶段 ChatGPT Web session 材料
- 发布模式: multi

## 2026-07-05 20:05:33 +0800
- 重构账号详情凭证展示并补齐 Web session 查看
- 发布模式: multi

## 2026-07-05 21:07:35 +0800
- 补齐 K12 workspace 注册与全空间保存
- 发布模式: multi

## 2026-07-05 21:17:43 +0800
- 加固账号详情脱敏并统一 K12 设置
- 发布模式: multi

## 2026-07-05 21:53:20 +0800
- 新增 auto-k12 独立实例并纳入多实例发布
- 发布模式: multi

## 2026-07-05 22:33:13 +0800
- 新增三实例共享配置中心
- 发布模式: multi

## 2026-07-05 22:41:21 +0800
- 完善共享配置推送保留全量本地配置
- 发布模式: multi

## 2026-07-05 22:44:45 +0800
- 修正共享配置差异按本地快照计算
- 发布模式: multi

## 2026-07-05 22:47:31 +0800
- 排除共享配置启动备份进入构建上下文
- 发布模式: multi

## 2026-07-05 22:55:06 +0800
- 修正共享配置差异忽略环境变量兜底
- 发布模式: multi

## 2026-07-05 23:10:58 +0800
- 默认关闭发布备份并清理历史备份
- 发布模式: multi

## 2026-07-06 00:05:14 +0800
- 新增邮箱验证码 API 注册登录能力
- 发布模式: multi

## 2026-07-06 00:26:39 +0800
- 修复邮箱验证码 API 超时和 smsbower 响应解析
- 发布模式: multi

## 2026-07-06 00:34:09 +0800
- 主动触发邮箱验证码 API 注册发码
- 发布模式: multi

## 2026-07-06 00:49:40 +0800
- 修复邮箱 API 重发链路和发码请求头
- 发布模式: multi

## 2026-07-06 00:55:12 +0800
- 补齐 V2 邮箱 API 重发和 OTP 等待配置
- 发布模式: multi

## 2026-07-06 01:49:32 +0800
- 恢复普通邮箱注册验证码等待行为
- 发布模式: multi

## 2026-07-06 04:31:45 +0800
- 兼容 Cliproxy region-Rand 并支持动态代理 IP 保留时长配置
- 发布模式: multi

## 2026-07-06 04:33:58 +0800
- 固定动态代理 IP 保留时长空值默认值
- 发布模式: multi

## 2026-07-06 05:27:33 +0800
- 新增已保存凭证重跑 K12 workspace 捕获
- 发布模式: multi

## 2026-07-06 05:48:35 +0800
- 修正 K12 重捕获入口代理模式和批量操作
- 发布模式: multi

## 2026-07-06 12:44:25 +0800
- 修复K12重跑任务日志面板和成功判定
- 发布模式: multi

## 2026-07-06 16:19:50 +0800
- 改进 Idea 提交账号不可用标记和结果总结
- 发布模式: multi

## 2026-07-06 17:43:25 +0800
- 修复 Idea 提交轮询和结果日志展示
- 发布模式: multi

## 2026-07-06 18:53:22 +0800
- OAIPay 上传分类策略与日志可观测性修复
- 发布模式: multi

## 2026-07-06 20:19:35 +0800
- 单账号注册验证码等待预算
- 发布模式: multi

## 2026-07-07 02:19:02 +0800
- 新增账号筛选组合管理
- 发布模式: multi

## 2026-07-07 03:53:03 +0800
- 优化注册与手机号绑定任务日志分层
- 发布模式: multi

## 2026-07-07 03:57:46 +0800
- 补强手机号绑定日志脱敏与回归测试
- 发布模式: multi

## 2026-07-07 05:22:31 +0800
- 统一手机号绑定任务日志信息格式
- 发布模式: multi

## 2026-07-07 06:38:22 +0800
- 支持邮箱 API Gmail 多身份随机变体
- 发布模式: multi

## 2026-07-07 07:37:57 +0800
- UI 优化: 极简压缩 Accounts 页面的筛选组合区域
- 发布模式: multi

## 2026-07-07 07:49:33 +0800
- UI 优化: 支持再次点击筛选组合来释放所有条件
- 发布模式: multi

## 2026-07-07 07:50:30 +0800
- UI 优化: 支持再次点击筛选组合来释放所有条件 (修复 TS 编译错误)
- 发布模式: multi

## 2026-07-07 08:31:49 +0800
- UI: 极简压缩 Accounts 页面已选账号区域并升级至 v1.3.24
- 发布模式: multi

## 2026-07-07 09:07:01 +0800
- 重构: 拆分 Accounts.tsx 中的 FilterPresetBar 与 SelectedAccountsSummary 组件
- 发布模式: multi

## 2026-07-07 10:19:50 +0800
- 新增: 支持编辑和覆盖筛选组合的筛选条件
- 发布模式: multi

## 2026-07-07 19:44:14 +0800
- 兼容手机号绑定 API 格式并优化账号日志分组
- 发布模式: multi

## 2026-07-07 23:12:15 +0800
- 修正账号订阅刷新语义与认证状态区分
- 发布模式: multi

## 2026-07-08 00:42:57 +0800
- 允许内置账号筛选组合直接编辑删除
- 发布模式: multi

## 2026-07-08 04:39:29 +0800
- 修复旧 HME 账号验证码等待刷屏
- 发布模式: multi

## 2026-07-08 05:13:57 +0800
- 统一 HME 验证码读取到 TempMail 转发箱
- 发布模式: multi

## 2026-07-08 05:33:25 +0800
- 增加 Idea 不可用账号筛选列
- 发布模式: multi

## 2026-07-08 06:23:40 +0800
- 修复 Helper Ready 有明确转发邮箱时误扫全部邮箱
- 发布模式: multi

## 2026-07-08 13:18:15 +0800
- 优化 Idea 提交筛选与卡密池选择保存
- 发布模式: multi

## 2026-07-08 13:20:30 +0800
- 同步 Idea 提交优化版本号 v1.3.34
- 发布模式: multi

## 2026-07-08 13:38:04 +0800
- 增强 Idea 提交 CDK 剩余额度刷新与目标成功数量
- 发布模式: multi

## 2026-07-08 18:18:15 +0800
- 注册面板增加已注册邮箱登录路由控制与记录
- 发布模式: multi

## 2026-07-08 18:45:43 +0800
- 注册任务增加独立出口IP与浏览器指纹隔离
- 发布模式: multi

## 2026-07-08 19:23:14 +0800
- 注册账号级指纹持久化与后续任务复用
- 发布模式: multi

## 2026-07-08 20:58:24 +0800
- 兼容 sms24 手机号 API 格式
- 发布模式: multi

## 2026-07-09 08:33:39 +0800
- 手机号绑定任务支持账号级并发设置
- 发布模式: multi

## 2026-07-09 16:34:53 +0800
- 修复 Idea 提交状态回填与停止任务幽灵显示
- 发布模式: multi

## 2026-07-09 17:19:04 +0800
- 默认账号网络动作走全局动态代理
- 发布模式: multi

## 2026-07-09 17:24:28 +0800
- 浏览器登录态捕获同步使用全局账号代理
- 发布模式: multi

## 2026-07-09 18:57:18 +0800
- 重构 ChatGPT 账号页筛选与操作工具栏
- 发布模式: multi

## 2026-07-10 05:09:21 +0800
- 修正 ChatGPT 账号页工具栏分区和统计摘要响应式布局
- 发布模式: multi

## 2026-07-10 06:06:44 +0800
- 清理账号页组件 lint 债务并修复响应式侧栏状态同步
- 发布模式: multi

## 2026-07-10 07:19:06 +0800
- 重构 ChatGPT 账号页操作栏与邮箱列搜索布局
- 发布模式: multi

## 2026-07-10 10:18:26 +0800
- 修复筛选任务范围并简化外部账号上传流程
- 发布模式: multi

## 2026-07-10 10:33:55 +0800
- 修复筛选范围冲突提示上下文
- 发布模式: hot

## 2026-07-10 11:10:07 +0800
- 修复 Idea 卡密候选与上游拒绝原因
- 发布模式: multi

## 2026-07-10 12:24:08 +0800
- 新增 auto-plus2 独立 Plus 实例并下线主服务与 K12 常驻容器
- 发布模式: multi

## 2026-07-10 18:23:20 +0800
- 新增 ChatGPT AccessToken 纯文本导出模式
- 发布模式: multi

## 2026-07-10 18:43:13 +0800
- 优化 AccessToken 全库导出性能
- 发布模式: hot

## 2026-07-11 01:13:35 +0800
- P0 修复 HME 邮箱状态污染、终态轮询与 OOM 护栏
- 发布模式: multi

## 2026-07-11 01:18:47 +0800
- 修复服务重启后旧任务页面的轮询重试
- 发布模式: multi

## 2026-07-11 01:37:52 +0800
- P0 熔断陈旧终态任务页面的摘要轮询
- 发布模式: multi

## 2026-07-11 02:25:52 +0800
- 合并全局动态代理配置并清除旧覆盖字段
- 发布模式: multi

## 2026-07-11 02:54:29 +0800
- 优化账号页筛选组合与操作显示
- 发布模式: multi

## 2026-07-11 04:31:58 +0800
- 修复账号页 TempMail Ready 注册域名丢失
- 发布模式: multi

## 2026-07-11 04:55:27 +0800
- 优化注册日志当前成功数与账号间分隔
- 发布模式: multi

## 2026-07-11 18:13:19 +0800
- feat(chatgpt): add browser web-session logout action
- 发布模式: multi

## 2026-07-13 07:42:21 +0800
- 接入当前 PayPal API 单账号与批量提链
- 发布模式: multi

## 2026-07-14 08:42:49 +0800
- 移除 K12、Workspace、Business 与 Team 产品能力并退役 auto-k12 实例
- 发布模式: multi

## 2026-07-14 12:37:27 +0800
- 新增手机号池 API 域名转发 Relay
- 发布模式: multi

## 2026-07-15 03:10:42 +0800
- 新增 iDEAL 批量提交 PIX 通道
- 发布模式: multi

## 2026-07-15 03:38:31 +0800
- 新增 PIX 多 CDK 成功核销锁定
- 发布模式: multi

## 2026-07-15 03:41:50 +0800
- 修复 PIX 上游 5xx 错误释放 CDK
- 发布模式: multi

## 2026-07-15 06:35:14 +0800
- 修复手机号池号段状态回写与限流自动恢复
- 发布模式: multi

## 2026-07-15 18:35:54 +0800
- 新增全任务完成当前后停止并持久化停止日志
- 发布模式: multi

## 2026-07-15 18:37:48 +0800
- 修复多实例发布的 standby 容器检查
- 发布模式: multi

## 2026-07-15 21:36:50 +0800
- 修复 HME tag 直查收码与 OTP 等待窗口
- 发布模式: multi

## 2026-07-15 22:31:36 +0800
- 修复 HME prepare 控制面直连与任务归属
- 发布模式: multi

## 2026-07-15 22:41:10 +0800
- 串行 HME prepare 避免并发控制面超时
- 发布模式: multi

## 2026-07-16 03:59:03 +0800
- 统一支付链接生成至 long-link 管理端配置并支持批量持久化
- 发布模式: multi

## 2026-07-16 04:58:42 +0800
- 修复 Plus RT 与手机号绑定分类
- 发布模式: multi

## 2026-07-16 05:09:35 +0800
- 透传 PIX 上游二维码有效期并防止过期链接复用
- 发布模式: multi

## 2026-07-16 05:23:22 +0800
- 手机号池增加绑定情况筛选
- 发布模式: multi

## 2026-07-16 06:10:42 +0800
- 账号列表增加手机号绑定情况筛选
- 发布模式: multi

## 2026-07-16 06:55:41 +0800
- 账号列表增加支付链接平台筛选
- 发布模式: multi

## 2026-07-16 07:19:38 +0800
- 同步 long-link 管理端历史订阅链接
- 发布模式: multi

## 2026-07-16 08:50:51 +0800
- 账号库支持上传已保存PIX支付链接
- 发布模式: multi

## 2026-07-16 10:31:11 +0800
- 支持按已选或当前筛选导出PIX支付链接，并完善PIX CDK复用
- 发布模式: multi

## 2026-07-16 10:51:23 +0800
- 修复已保存PIX链接上传重复投递和任务无反馈
- 发布模式: multi

## 2026-07-16 11:32:25 +0800
- 增加批量同步本地状态并发与账号隔离保护
- 发布模式: multi

## 2026-07-16 12:40:03 +0800
- feat: add one-click expired PIX payment link cleanup
- 发布模式: multi

## 2026-07-17 01:52:44 +0800
- feat: unify submission filters and enable multi-credit PIX batch submission
- 发布模式: multi

## 2026-07-17 02:32:15 +0800
- 修复 React 19 下过期 PIX 链接清理确认弹窗并延后 loading
- 发布模式: multi

## 2026-07-17 04:09:25 +0800
- 增加过期 PIX 清理任务日志与终态汇总
- 发布模式: multi

## 2026-07-17 04:46:35 +0800
- 修复手机号池单号状态与限定号段绑定语义
- 发布模式: multi

## 2026-07-17 05:47:00 +0800
- 恢复主实例常驻发布拓扑并补齐三实例发布烟测
- 发布模式: multi

## 2026-07-17 09:37:38 +0800
- 升级 v2.3.0：隔离三实例管理员认证并加固会话、审计、SSE 与公网端口
- 发布模式: multi

## 2026-07-17 09:50:51 +0800
- 修复 v2.3.1：统一多实例镜像单次构建并完成认证安全发布
- 发布模式: multi

## 2026-07-17 10:02:16 +0800
- 修复 v2.3.2：让 nginx 查询令牌脱敏校验可重复执行
- 发布模式: multi

## 2026-07-17 21:23:32 +0800
- 支持 attsms 纯文本手机号绑定收码与到期时间解析
- 发布模式: multi

## 2026-07-18 00:43:51 +0800
- 升级 v2.3.3：扩展 PIX 过期、已支付与支付取消链接清理
- 发布模式: multi

## 2026-07-18 03:37:26 +0800
- 升级 v2.3.4：新增 PIX 链接扫描与分类清理面板
- 发布模式: multi

## 2026-07-18 03:41:53 +0800
- 修复 v2.3.4 PIX 扫描面板菜单浮层遮挡
- 发布模式: multi

## 2026-07-18 03:47:25 +0800
- 更新 auto-gpt 约定：三个业务实例统一常驻
- 发布模式: multi

## 2026-07-18 04:17:58 +0800
- 升级 v2.3.5：PIX 链接改为 Stripe 实时状态扫描
- 发布模式: multi

## 2026-07-18 05:56:01 +0800
- 升级 v2.3.6：统一 Sub2API 与 OAIPay 二元上传筛选
- 发布模式: multi

## 2026-07-18 06:11:02 +0800
- 升级 v2.3.7：明确提交记录筛选语义
- 发布模式: multi

## 2026-07-18 07:43:48 +0800
- 升级 v2.3.8：支付链接历史筛选与生成防重
- 发布模式: multi

## 2026-07-19 00:48:26 +0800
- 新增 ChatGPT Team 优惠码 checkout 长链及双端配置
- 发布模式: multi

## 2026-07-20 02:36:01 +0800
- 发布 v2.5.0 HME 平台 registration 消费者兼容
- 发布模式: multi

## 2026-07-22 06:28:14 +0800
- 账号列表多字段排序并统一本地状态同步全局配置
- 发布模式: multi

## 2026-07-22 06:53:36 +0800
- 完善全局本地状态同步校验并优化注册时间排序索引
- 发布模式: multi

## 2026-07-22 22:24:15 +0800
- 发布 v2.6.1：账号按最新注册排序并增加支付链接复制反馈
- 发布模式: multi

## 2026-07-23 04:07:50 +0800
- 发布 v2.7.0：支付长链远程 API Key 配置
- 发布模式: multi

## 2026-07-23 04:28:10 +0800
- 发布 v2.7.1：修复 Settings 密钥默认遮罩
- 发布模式: multi

## 2026-07-23 11:09:06 +0800
- 发布 v2.7.2：本地订阅状态缺失自动补刷
- 发布模式: multi

## 2026-07-23 15:45:57 +0800
- 发布 v2.7.3：修复注册 Sentinel 浏览器代理与完整风控令牌
- 发布模式: multi

## 2026-07-24 00:48:01 +0800
- 发布v2.8.0：Team优惠链接支持自主动态IP国家
- 发布模式: multi

## 2026-07-24 00:53:51 +0800
- 发布v2.8.1：统一行级Team支付链接配置入口
- 发布模式: multi

## 2026-07-24 01:28:06 +0800
- 发布v2.8.2：修复Team优惠长链接Hosted模式与缓存隔离
- 发布模式: multi

## 2026-07-24 02:22:31 +0800
- 发布 v2.8.3：Team 优惠长链接支持任务级账单国家
- 发布模式: multi

## 2026-07-24 02:35:43 +0800
- 补强 v2.8.3：Team 账单国家优先并按国家派生币种
- 发布模式: multi

## 2026-07-24 02:38:49 +0800
- 补强 v2.8.3：客户端边界清理 Team 旧 country/currency 字段
- 发布模式: multi

## 2026-07-24 03:33:58 +0800
- v2.8.4 补齐支付链接全类型筛选
- 发布模式: multi

## 2026-07-24 04:10:10 +0800
- v2.8.5 扩展全类型支付链接扫描与 iDEAL 15 分钟过期
- 发布模式: multi

## 2026-07-24 04:49:39 +0800
- v2.8.6 支付链接全状态人工删除与 Team 24 小时有效期
- 发布模式: multi

## 2026-07-24 05:47:14 +0800
- 发布 v2.8.7：注册开户改为同一 Auth 浏览器事务并保持 Sentinel Cookie 连续性
- 发布模式: multi

## 2026-07-24 05:56:24 +0800
- 发布 v2.8.8：修复 iframe Sentinel 初始化并阻止基础设施错误重复注册
- 发布模式: multi

## 2026-07-24 06:06:33 +0800
- 发布 v2.8.9：Sentinel SDK 改由顶层 Auth 页面调用
- 发布模式: multi

## 2026-07-24 06:25:58 +0800
- 发布 v2.8.10：开户固定有头 Chromium 并启用 HAR 验证注册画像
- 发布模式: multi

## 2026-07-24 06:51:39 +0800
- 发布 v2.8.11：修复注册旧画像回写并限制 Auth 浏览器并发
- 发布模式: multi

## 2026-07-24 07:01:38 +0800
- 发布 v2.8.12：修复注册配置强刷导致的重复请求
- 发布模式: multi

## 2026-07-24 09:21:21 +0800
- 发布 v2.8.13：修复 Auth 浏览器卡死与任务内存膨胀
- 发布模式: multi

## 2026-07-24 09:33:11 +0800
- 发布 v2.8.14：补齐 Chromium 跨 Session 硬超时清理
- 发布模式: multi

## 2026-07-24 09:46:19 +0800
- 发布 v2.8.15：浏览器第二槽按 cgroup 内存自适应
- 发布模式: multi

## 2026-07-24 12:11:19 +0800
- 发布 v2.8.16：接入同浏览器注册后备链路
- 发布模式: multi

## 2026-07-24 12:43:11 +0800
- 修复 Camoufox 自定义安装路径识别并验证浏览器注册后备链路
- 发布模式: multi

## 2026-07-24 13:29:33 +0800
- 修复 ChatGPT 注册后备状态恢复、OTP 跨阶段去重与 SPA/API 提交判定
- 发布模式: multi

## 2026-07-24 13:35:57 +0800
- 补齐 about_you SPA 点击失败时的浏览器上下文 create_account API 兜底
- 发布模式: multi

## 2026-07-24 13:41:05 +0800
- 保持协议与浏览器后备的完整姓名生日一致
- 发布模式: multi

## 2026-07-24 13:45:03 +0800
- 修复马来/印尼 about_you 年龄字段识别
- 发布模式: multi

## 2026-07-24 13:49:13 +0800
- 修复 external_url 回调必须先导航再判定注册完成
- 发布模式: multi

## 2026-07-24 13:53:56 +0800
- 补齐浏览器注册完成后的 OAuth Token 提取兜底
- 发布模式: multi

## 2026-07-24 14:21:28 +0800
- 对齐 any-auto-register 的独立浏览器 OAuth recovery，补齐 add_phone 后 AT/RT 提取
- 发布模式: multi

## 2026-07-24 14:26:35 +0800
- 修复独立浏览器 OAuth recovery 使用 auto-gpt OAuth 常量
- 发布模式: multi

## 2026-07-24 14:41:39 +0800
- 修复独立浏览器 OAuth OTP 重发、时间戳过滤与严格状态推进
- 发布模式: multi

## 2026-07-24 14:48:27 +0800
- 保留 registration_disallowed 后已落地账号并继续 Token 提取
- 发布模式: multi

## 2026-07-24 14:55:41 +0800
- 解耦独立 OAuth OTP 等待预算，允许重发后完整等待
- 发布模式: multi
