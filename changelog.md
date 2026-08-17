# 更新日志 (Changelog)

本项目的所有显著更改都将记录在此文件中。

格式基于 [Keep a Changelog](https://keepachangelog.com/zh-CN/1.0.0/)，并且本项目遵循 [Semantic Versioning](https://semver.org/lang/zh-CN/) (语义化版本)。

## [Unreleased] (未发布)

- **ChatGPT AT/RT 认证生命周期与账号证据可观测性（v2.27.0）**：
  - **新增 (Added)**：`core/db.py` 新增 `chatgpt_auth_lifecycles`、`chatgpt_subscription_states` 和 `chatgpt_auth_probe_events` 非敏感投影；`services/chatgpt_core/auth_lifecycle.py` 统一记录 AT/RT/Session/Cookie 材料存在性、JWT `iat/exp`、OAuth `expires_in`、Web Session `expires`、刷新结果、账号证据和综合可用性。RT 刷新成功或手动更新材料时只持久化状态摘要与材料 revision，原始 AT/RT/Cookie 不进入生命周期表和探针事件。
  - **优化 (Changed)**：无 RT 的 AT-only 账号按已知 10 天策略展示“预计到期”，并明确 `at_only_10d_policy + estimated`，JWT/OAuth 的真实到期继续使用 `jwt_exp`/`oauth_expires_in + exact`；不知道的时间不伪造为精确值。历史账号回填按 250 条批次执行，生产启动后台回填，避免全量 `extra_json` 写入阻塞健康检查。
  - **修复 (Fixed)**：`status_probe.py` 将 RT 端点的拒绝与 `/backend-api/me` 的 AT 过期、撤销、账号停用、403 疑似封禁分开；RT 403 不再直接生成封禁证据。认证材料替换会清空旧探针快照，后续 `/me 200` 可恢复 `active_confirmed`，订阅当前态、历史确认态与认证材料失效分开保存。`cpa_upload.py` 与 `sub2api_upload.py` 不再用固定 10 天伪造导出有效期，未知时输出未知/空值并保留来源。
  - **前端 (Changed)**：`frontend/src/pages/Accounts.tsx` 增加“AT状态/到期”“RT刷新”“账号证据”列及移动端状态；`AccountDetailModal.tsx` 展示 AT 到期来源/精度、RT 最近结果、账号证据、综合可用性、当前/历史订阅到期，明确区分“AT已过期”“RT已拒绝”“账号已停用”和“疑似封禁”。新增 `GET /api/accounts/{account_id}/auth-lifecycle` 返回脱敏快照与探针历史；侧栏版本同步为 `v2.27.0`。
  - **测试 (Tests)**：新增隔离 Docker `test` stage、`docker-compose.test.yml`、`requirements-test.txt`、严格 marker 和 `scripts/test-in-docker.sh`；覆盖 AT-only 10 天估算、JWT 过期、RT 拒绝、RT 失败后 AT 回退、账号证据恢复、生命周期持久化、注册/登录/刷新/导出兼容，默认测试容器断网且不挂载生产数据。

- **拆分注册后 PayPal 提链并入队成功账号结果（v2.26.1）**：
  - **修复 (Fixed)**：`services/chatgpt_core/registration_paypal_payment.py` 在原有混合 `results` 之外独立发布 `submitted_results`、`submitted_results_total` 与 `submitted_results_truncated`。已成功提取 PayPal approval URL 并提交支付队列的账号不再被后续大量 `extract_failed`、`submit_failed` 或 `pending_auth` 结果挤出任务展示；独立明细仍按 500 条安全上限保留并明确报告截断，原字段继续保留以兼容旧前端和历史任务。
  - **前端 (Changed)**：`RegistrationPaypalPaymentSummary.tsx` 将统计/异常摘要与“提链成功并已提交支付队列”账号拆成两个结果框；成功框按 `state=submitted` 精确展示账号 ID、邮箱、远端状态、批次 ID、条目 ID 和提交时间，并提供成功邮箱、账号 ID + 邮箱的一键复制。旧任务没有 `submitted_results` 时自动从原 `results` 回退筛选；界面明确说明“已交支付队列”不代表最终支付完成，避免把队列接收结果误报为付款成功。共享组件同步覆盖独立注册页和账号页注册弹窗，侧栏版本更新为 `v2.26.1`。
  - **测试 (Tests)**：`tests/test_registration_paypal_payment.py` 增加独立成功结果、混合成功/失败、总数和截断标记合同，使用不依赖并发完成顺序的断言；`frontend/tests/registrationPaypalPaymentContract.test.mjs` 增加独立结果字段、成功框、复制入口和支付语义回归。专项后端隔离测试 `11 passed`，前端完整合同 `78 passed`，单文件 ESLint 与 TypeScript/Vite 生产构建通过。

- **注册成功后 PayPal 提链并自动提交支付队列（v2.26.0）**：
  - **新增 (Added)**：`api/tasks.py` 为 ChatGPT 注册请求增加默认关闭的 `registration_paypal_payment_enabled` 开关；开启时在任务创建阶段同时冻结当前 `openai-pay-long-link` 的 PayPal 提链 `profile_hash`、结账国家/币种和 `/opt/paypal-agreement-protocol` 的 Buyer 配置摘要，支付端必须通过内部 Bearer 通道返回 `configured=true`、`ready=true` 才允许创建任务。初始任务快照写入 `registration_paypal_payment_request`，旧客户端未传字段时保持关闭。
  - **新增 (Added)**：`services/chatgpt_core/paypal_agreement_auto_client.py` 实现受 `X-Internal-Auto-Channel` 与服务端 Token 保护的 profile/入队客户端；严格校验 HTTPS PayPal `/agreements/approve?ba_token=...` 链接、批次/条目响应和错误脱敏，不把内部 Token、完整 PayPal 链接或 BA Token 写入任务日志。`services/chatgpt_core/registration_paypal_payment.py` 新增独立并发协调器，注册账号落库后复用现有 `payment_link` action 生成并持久化 PayPal 链接，再只等待支付队列持久化入队，不等待真实支付完成。
  - **优化 (Changed)**：后处理使用进程级最多 2 路并发，状态独立记录为 `submitted`、`extract_failed`、`submit_failed`、`pending_auth` 或 `skipped`；提链失败、支付入队失败、缺少 Access Token 和账号身份变化均不回写为注册失败。账号 `extra.chatgpt_paypal_auto_payment` 只保存任务/profile/批次/条目/远端状态等审计摘要，完整链接继续由既有支付链接缓存维护；同一注册任务本地去重，支付端 BA Token 幂等响应可安全复用已有批次。
  - **修复 (Fixed)**：启用自动支付的注册账号将本地状态刷新延后到 PayPal 后处理协调器完成之后，避免原有延迟刷新线程用旧的 `extra_json` 快照覆盖自动支付标记或提链审计。
  - **前端 (Changed)**：`RegisterTaskPage.tsx` 与 `RegisterTaskModal.tsx` 共用 `RegistrationPaypalPaymentField`，本地记忆选择但默认关闭，并在任务面板展示“已交支付队列、提链失败、入队失败、待补 Auth”等真实状态；摘要明确区分 ChatGPT 提链国家/币种与 PayPal Buyer/代理国家，避免把两条支付边界混为一谈。侧栏版本同步为 `v2.26.0`；`docker-compose.multi.yml` 将受限的 `/etc/paypal-auto-integration.env` 注入 `auto-gpt`、`auto-gpt-plus`、`auto-plus2`。
  - **测试 (Tests)**：新增 `tests/test_registration_paypal_payment.py` 覆盖内部客户端鉴权/脱敏、PayPal URL 校验、profile 冻结、默认关闭、提链失败、入队失败、幂等复用和多账号并发；新增 `frontend/tests/registrationPaypalPaymentContract.test.mjs` 覆盖两个注册入口的开关请求、持久化与状态摘要。当前专项后端 `10 passed`，提链/支付历史相邻回归 `104 passed`，前端合同 `78 passed`，TypeScript/Vite 生产构建通过。

- **将注册后 0 元检测改为可选并统一注册国家选择器（v2.25.7）**：
  - **优化 (Changed)**：`api/tasks.py` 为 `/api/tasks/register` 增加 `registration_zero_amount_eligibility_enabled` 显式开关并默认关闭；只有任务开启时才校验和冻结 `registration_zero_amount_checkout_country`、读取支付资格代理配置、创建检测协调器，并在账号成功入库后提交 0 元资格检测。关闭时不会构造检测链、写入账号 0 元状态或生成检测汇总，检测失败继续与注册结果隔离；任务初始快照通过 `registration_zero_amount_eligibility_request.enabled` 保留实际选择，便于回放。未传新字段的旧客户端按默认关闭处理。
  - **修复 (Fixed)**：`RegistrationEligibilityCountryField.tsx` 移除会在组件挂载和 `resetFields()` 时把本地选择覆盖回 `VN` 的固定初始值/监听回写链，改为只在用户真实切换开关或国家时持久化；账号页“保存设置”和开始注册同时保存开关与国家，重新打开注册面板时优先恢复最新专用本地值，再兼容已保存表单画像。两个注册入口现在都能稳定保留“注册后 0 元检测国家”。
  - **前端 (Changed)**：新增 `RegistrationCountrySelect.tsx`，复用 `GET /api/tasks/chatgpt/zero-amount-eligibility/profile` 的 232 国/地区目录，统一提供中文国家名、两位代码、币种、搜索、加载失败提示和刷新操作；账号页注册弹窗与独立注册页的“注册出口国家”由自由文本改为同一搜索列表。动态代理仍要求必选国家，代理池和指定代理失败切换场景仍可清空为“不限”；注册出口国家与 0 元检测结账国家继续是两个独立任务字段。
  - **测试 (Tests)**：`tests/test_registration_zero_amount_eligibility.py` 新增默认关闭、关闭时不写账号检测标记、开启时国家冻结、初始任务快照和非法国家拒绝回归；`frontend/tests/paymentEligibilityTaskContract.test.mjs` 覆盖两个注册入口的开关请求、异步配置水合持久化以及注册出口国家不再使用自由文本。一次性断网、只读 checkout、临时 SQLite/shared config 的测试容器运行注册控制、诊断、邮箱请求和支付资格相邻套件 `123 passed`，前端合同 `75 passed`，TypeScript/Vite 生产构建通过；Playwright 在 `1440×1000` 与 `390×844` 验证开关显隐、国家跨刷新恢复、无横向溢出和相关表单无重叠，侧栏可见版本同步为 `v2.25.7`。

- **修复浏览器资源恢复时多任务同时抢占并改为严格 FIFO 逐一放行（v2.25.6）**：
  - **修复 (Fixed)**：`services/chatgpt_core/sentinel_browser.py` 将注册、Auth 与 Sentinel 共用的浏览器容量门禁从无公平保证的 Semaphore 竞争改为进程内严格 FIFO 票号队列；只有队首可以检查运行并发、PID、cgroup 内存、宿主机可用内存与 CPU PSI，资源恢复时不再让所有等待线程在同一时刻同时获得槽位。每个成功队首会原子认领现有浏览器启动错峰时间，Plus 当前 3 秒间隔因此成为相邻票号的最小放行间隔，后续请求按到达顺序逐一推进。
  - **兼容 (Changed)**：保留调用方既有 `priority` 参数与注册等待者统计，但取消后到注册任务越过先到 Auth/Sentinel 请求的插队语义；运行快照新增 `queue.policy=fifo`、队列深度、队首票号与队首操作，等待/放行日志增加稳定的 `fifo_queue`、`fifo_stagger`、`ticket` 与 `queue_wait` 字段。任务停止、跳过或异常退出会从任意队列位置摘除自身票号并唤醒新队首，避免取消任务形成死票堵塞后续任务。
  - **测试 (Tests)**：`tests/test_sentinel_browser.py` 新增“后到注册不得越过先到 Auth”、资源同时恢复后三个等待者按票号和启动间隔依次执行、队中任务停止后自动摘票并放行下一项等并发回归；既有容量、内存、PID、启动错峰、硬超时和持久浏览器测试继续通过。`docs/testing-in-docker.md` 同步固定 FIFO 日志与调度合同，侧栏可见版本更新为 `v2.25.6`。

- **统一已有邮箱账号的验证码登录并修复手机号绑定旧密码阻断（v2.25.5）**：
  - **修复 (Fixed)**：`services/chatgpt_core/oauth_client.py` 将已有邮箱账号的认证策略统一为邮箱一次性验证码优先，库存 `password` 非空不再自动触发 `force_password_login`；补抓 Auth、单/批量手机号绑定、限定号段绑定、短信探测、单/批量邮箱测活，以及注册任务的已有账号 AT/Auth 抓取均复用该语义。协议密码接口收到 OpenAI 当前的 `HTTP 400/401 + Incorrect email address or password` 时可受控切换 OTP，同时只为旧 `401 invalid_request_error` 保留兼容识别；`403 deleted/deactivated` 和通用 `400 invalid_request_error` 不会被误判为密码拒绝。
  - **兼容 (Changed)**：`services/chatgpt_core/browser_registration.py` 提取浏览器密码页切换 OTP 的共享状态机，监听 `/api/accounts/passwordless/send-otp` 业务响应并覆盖 SPA 跳转竞态；Codex OAuth 已有账号抓取在库存密码拒绝后会再检查一次验证码入口。页面确实没有验证码入口时仍可使用显式密码，已经选择 OTP 后的发码失败、网络失败或超时不会回头提交伪密码；手机号注册中已注册手机号的真实密码登录合同保持不变。
  - **修复 (Fixed)**：`services/chatgpt_core/web_session_login.py` 与 `api/tasks.py` 移除“执行登录态必须存在非空密码”的前置门禁，有 `mailbox_state` 的无密码账号现可直接进入单/批量浏览器验证码登录；真正缺少邮箱恢复状态的账号仍会在任务创建阶段跳过。密码错误分类同步识别 OpenAI 当前组合文案。
  - **测试 (Tests)**：`tests/test_chatgpt_register.py`、`tests/test_browser_registration_flow.py`、`tests/test_any_auto_web_session_contract.py`、`tests/test_subscription_auth_capture.py`、`tests/test_custom_email_recheck.py`、`tests/test_web_session_login.py`、`tests/test_invalid_account_recheck.py`、`tests/test_access_token_only_checkout.py`、`tests/test_chatgpt_registration_mode_adapter.py` 与 `tests/test_phone_binding_assignment.py` 覆盖 OTP 优先、当前密码拒绝、停用账号、通用 400、显式密码兼容、浏览器竞态、空密码任务筛选和手机号绑定公共调用链；侧栏可见版本同步为 `v2.25.5`。

- **细分支付资格检测失败原因并在任务与账号列表中直接展示（v2.25.4）**：
  - **新增 (Added)**：`services/chatgpt_core/payment_eligibility.py` 为 0 元资格、支付方式、GCash 和支付链接格式共用的技术失败增加稳定结构化分类，覆盖“网络问题、无法创建 Checkout、认证问题、代理问题、上游接口问题、返回格式问题、配置问题、其他问题”；保留原始脱敏 message、失败阶段和 HTTP 状态，不再把所有异常压成无法区分的 `technical_error`。其中 checkout 创建阶段收到 HTTP 拒绝或 `unusual activity` 明确归为“无法创建 Checkout”，连接超时/断连归为“网络问题”，代理解析及出口国家校验单独归为“代理问题”。
  - **优化 (Changed)**：`api/tasks.py` 在逐账号结果、账号 `extra.*.last_attempt`、任务实时 meta 和终态历史详情中贯穿 `failure_category / failure_label`，并新增 `eligibility_failure_summary` 聚合；任务日志在“技术失败”后直接写出类型，最终摘要在“检测失败”计数后列出各原因数量。`probe_failed` 继续只代表技术尝试态，不覆盖既有 `eligible / ineligible / available / no_methods / oaics / cs` 确认态，也不改变 `zero_amount_eligibility_display_state` 的现有筛选合同。
  - **前端 (Changed)**：新增 `frontend/src/lib/paymentEligibilityFailure.ts` 统一失败分类展示和旧记录兼容推导；实时任务面板、任务历史详情、注册后自动 0 元摘要，以及账号表的“0元资格 / 支付方式 / 链接类型”列均在“检测失败”后显示具体原因。支付链接格式任务同时补齐 `OAICS / Stripe (CS) / 检测失败 / 跳过` 实时与历史统计；升级前只有 message 的历史任务和账号无需重跑检测，即可按既有错误文本显示原因。
  - **测试 (Tests)**：`tests/test_payment_eligibility_probe.py`、`tests/test_payment_eligibility_tasks.py` 和 `frontend/tests/paymentEligibilityTaskContract.test.mjs` 覆盖八类稳定映射、checkout HTTP 400、网络错误、任务聚合、失败字段持久化、历史 message 回退及四类前端展示面；侧栏可见版本同步为 `v2.25.4`。

- **修复失效测活被旧库存密码阻断（v2.25.3）**：
  - **修复 (Fixed)**：`services/chatgpt_core/any_auto/browser_register.py` 的 `login_only` 状态机在 OpenAI 将已有账号直接路由到 `log-in/password` 时，优先点击页面提供的“一次性验证码登录”，并同时监听 `/api/accounts/passwordless/send-otp` 业务响应；发码接口 `2xx` 后即使慢速代理下 SPA 尚未切换地址栏，也按已发码状态进入 `email_otp_verification`，复用失效测活现有的 `RestoredEmailService` 收码和完整 Web Session 写回链。只有页面确实没有验证码入口时才兼容提交库存密码；已经进入 OTP 分支的发码失败保持验证码登录语义，不会回退伪密码。历史账号因别名复用、密码变更或旧数据失配而被 `Incorrect email address or password` 阻断时，不再因为邮箱仍可用却提前结束测活。
  - **兼容 (Changed)**：现场账号以无密码、仅邮箱验证码登录为主，因此库存 `password` 不再被当成失效测活的必需凭据：若密码页展示验证码入口，发码失败、限流或页面推进超时会保持在 OTP 语义并按网络/限流错误重试，绝不回头提交伪密码；只有页面确实没有验证码入口的历史密码账号才沿用密码登录。`invalid_account_recheck.py` 同步识别 OpenAI 当前的组合密码错误文案，真正的密码账号失败时准确记录为 `password_invalid`，不再落入 `unknown_error`；侧栏版本同步为 `v2.25.3`。
  - **测试 (Tests)**：`tests/test_any_auto_web_session_contract.py` 覆盖密码页 OTP 优先、发码 `2xx` 早于 SPA 跳转、OTP 发码失败不得提交密码、密码拒绝后二次 OTP 兜底、库存密码不再重复提交及验证码状态继续推进；`tests/test_invalid_account_recheck.py` 锁定 `Incorrect email address or password` 的稳定分类和 passwordless 网络失败可重试语义。

- **修复首次任务摘要回填阻塞三实例启动（v2.25.2）**：
  - **修复 (Fixed)**：`main.py` 将 `v2.25.1` 的存量任务摘要回填从 FastAPI lifespan 就绪前的同步 threadpool 调用，改为数据库建表及常规后台服务启动完成后运行的 daemon 线程；Uvicorn 不再等待完整历史 JSON 解析后才开放健康检查和账号接口。现场首次迁移中主实例 `1,867` 条回填耗时约 `101s`、Plus `2,235` 条约 `203s`，曾导致发布健康门禁超时；Plus2 的 10 条回填约 3 秒。
  - **兼容 (Changed)**：`api/tasks.py` 为后台回填与列表触发的缺失摘要修复增加进程级互斥，同一实例只允许一个回填器按 10 条短事务推进；容器在中途停止时，已提交批次保留，下次启动从缺失 ID 继续。摘要尚未完成时仅任务历史请求可能等待同一回填锁，账号表、设置页、健康检查和其它业务接口保持可用；侧栏版本同步为 `v2.25.2`。

- **修复账号表格与任务历史在大体积 SQLite 数据库上的冷加载延迟（v2.25.1）**：
  - **修复 (Fixed)**：`core/db.py` 新增 `task_log_summaries` 轻量摘要旁表，`api/tasks.py` 在保存任务历史的同一事务内同步任务来源、逻辑任务分组和列表摘要；`GET /api/tasks/logs` 继续按最新 `task_id` 去重并兼容旧 `detail.meta.source`，但列表、分页和任务类型筛选只读取摘要索引，不再为约 30 KB 的列表响应扫描、加载和反序列化数百 MB 的 `task_logs.detail_json`。任务详情仍按点击单独读取完整日志，且复用已解析详情生成头部摘要，避免同一大 JSON 重复解析。
  - **兼容 (Changed)**：`main.py` 在数据库建表后以每批 10 条的短事务回填既有任务摘要，不改写原始任务详情，也不改变历史日志、错误、统计、详情合并、复制或批量删除语义；回填中断时后续启动/列表会继续补齐，摘要设施不可用时保留旧查询兼容路径。批量删除和数据库触发器同步清理摘要，避免旁表残留；缓存读取仍按当前内存任务状态把重启前遗留的 `running` 归一化为 `stopped`。
  - **优化 (Changed)**：账号表新增 `status + platform` 复合索引，限流恢复轮询和账号列表的 30 秒兼容检查不再扫描大账号行；新增覆盖 `platform + updated_at + email + created_at` 的列表状态 freshness 索引，使筛选请求核对 `account_list_state` 是否陈旧时无需读取 Plus 实例约 410 MB 的 `accounts.extra_json` 主表页。任务历史同时新增 `platform + id + task_id` 覆盖索引，保留摘要回填失败时的轻量去重路径。
  - **测试 (Tests)**：`tests/test_task_logs_history.py` 增加大详情零读取、摘要写入同步、整组删除清理和账号索引声明回归；一次性断网测试容器确认任务历史的 `task_id` 去重、旧来源筛选、运行态归一化、旧重复详情合并及摘要合同全部通过。优化前现场基线为主实例任务历史冷读约 `3.1s`、Plus 约 `7.7s`，账号列表 Plus 首次约 `0.6s`；侧栏版本同步为 `v2.25.1`。

- **重构账号浏览器身份并支持 Camoufox v152 进程级原生深画像（v2.25.0）**：
  - **新增 (Added)**：新增 `services/chatgpt_core/browser_identity.py` 的版本化 `BrowserFingerprint v2`，新注册账号保存浏览器族/版本、具体 `curl_cffi` target、TLS/HTTP2/Header profile、UA/Client Hints、OS/平台、locale/languages/timezone/geolocation、屏幕/viewport/DPR、CPU/触控、WebGL、Canvas/Audio/字体种子、字体清单、语音清单、媒体设备和 Context capability。协议注册可从 `chrome146`、`firefox147`、`safari2601` 三个当前稳定支持目标生成相关联画像；配置 `CHATGPT_PROTOCOL_BROWSER_FAMILIES` 可限制允许的浏览器族，持久化始终使用具体 target，不使用会随依赖升级漂移的别名。
  - **优化 (Changed)**：运行栈升级为 Camoufox `152.0.4-beta.28`、`curl_cffi 0.16.0`、Patchright `1.61.2` 和 Playwright `1.60.0`（Camoufox Python API 保持当前最新版 `0.5.4`；Playwright 使用其官方 `playwright<1.61` 约束下的最高兼容版本）；浏览器注册固定采用 Firefox 147 对外画像，使 Camoufox 152 的 JS/HTTP 身份可与 `curl_cffi firefox147` 对齐。项目内仍参与运行的旧 Chrome 110/120/124/131/136/145 fallback 全部收敛到当前实际可用的 `chrome146`。
  - **新增 (Added)**：`services/chatgpt_core/shared_camoufox.py` 为每个深画像创建独立 Camoufox 进程和唯一 BrowserContext，通过启动期 `CAMOU_CONFIG` 固化 Screen、Audio、完整 WebGL、字体、语音、媒体设备、locale、窗口几何和请求头，并保留官方 schema 声明的 Canvas seed；Context 创建时只调用 `152.0.4-beta.28` 二进制真实存在的 13 个原生 setter，创建工作页前执行 capability probe，任一 setter 缺失即 fail closed。该版本发布资产并不存在 `setScreenDimensions`、`setScreenColorDepth`、`setCanvasSeed`，官方源码也明确移除了 Canvas noise，不能把 `canvas:seed` 误报成有效的原生扰动；Audio 则在并发 Context 下回落到进程配置。画像按实际运行宿主选择同 OS preset，避免 Linux 容器伪装 macOS 字体渲染；同账号后续流程复用完整画像，不同账号使用不同 Camoufox PID、Screen、Audio、WebGL、字体集合和种子。
  - **修复 (Fixed)**：`api/tasks.py`、`any_auto` 注册运输层、`ChatGPTClient`、OAuth/状态探针、Sentinel PoW 与 Sentinel VM 统一消费同一账号画像；Firefox/Safari 不再发送 Chromium Client Hints，Chrome 的 macOS UA、Client Hints 与 `curl_cffi` TLS target 保持一致。浏览器 Web Session 采集扩展到 navigator/screen/timezone/geolocation/WebGL，并将真实 Camoufox Context 画像提升为新账号 canonical profile，避免被外层 Chrome fallback 污染。`TaskLog` 的查找/插入事务增加进程内串行化，避免异步运行中 checkpoint 与终态 callback 并发首写时为同一 `task_id` 生成重复历史行。
  - **兼容 (Changed)**：不迁移、不回填、不随机补齐现有账号。历史 10 字段 Chrome 指纹继续按 legacy 结构读取和保留；数据库保存、已有账号抓取与后续合并不会把旧账号升级或覆盖为 v2。新账号额外记录 `protocol_transport` 或 `process_isolated_context_deep_native` 隔离枚举，同时保留旧 `chatgpt_browser_fingerprint_isolated` 布尔字段供旧客户端读取。Sentinel 容量门禁恢复为每个 Camoufox 槽使用完整浏览器内存/PID预算，不再使用旧的 `384 MiB / 32 PID` 共享 Context 预算。
  - **测试 (Tests)**：新增三浏览器 target/Header 合同、v2/legacy 持久化边界、any-auto 参数透传、Sentinel 环境一致性，以及 Camoufox 双进程真实集成探针；浏览器探针用两个线程保持两个进程同时存活，验证不同 Camoufox PID、13 个 setter 自毁、主页面/Worker UA、同画像跨进程 Canvas/Audio 稳定、跨画像 Audio hash 与 Screen/WebGL 差异，并明确不对官方已禁用的 Canvas noise 编造通过结果。批量状态探测连接生命周期测试固定调度时钟，避免首账号实际执行超过 50ms 后错误地要求额外休眠。侧栏版本同步为 `v2.25.0`。

- **修复注册后 0 元检测国家无法切换（v2.24.8）**：
  - **修复 (Fixed)**：`api/tasks.py` 为 `/api/tasks/register` 增加独立的 `registration_zero_amount_checkout_country` 请求字段；任务入队时按现有 232 国/地区目录校验并冻结结账国家，后置资格检测的 checkout、promotion、taxes 三阶段统一使用该国家，不再无条件覆盖为 `VN`。不传该字段的旧客户端继续使用 `VN` 默认值，注册代理出口字段与 0 元检测国家保持独立。
  - **新增 (Added)**：`frontend/src/features/auth/components/RegistrationEligibilityCountryField.tsx` 与 `frontend/src/lib/registrationEligibilityCountry.ts` 提供共用国家/币种选择器，复用 `GET /api/tasks/chatgpt/zero-amount-eligibility/profile`，支持搜索、目录读取失败重试和浏览器本地记忆；`RegisterTaskPage.tsx` 与账号页 `RegisterTaskModal.tsx` 均可在创建注册任务时直接更换“注册后 0 元检测国家”。
  - **优化 (Changed)**：任务初始快照增加 `registration_zero_amount_eligibility_request.checkout_country_code`，注册结果摘要继续展示实际冻结的账单国家、币种和代理链，便于排查配置是否真正生效。
  - **测试 (Tests)**：补充注册请求国家冻结/非法国家拒绝、后置协调器配置传递和两套前端入口选择器/请求字段合同；侧栏版本同步为 `v2.24.8`。

- **修复 OAIPay 内部连通与上传密钥漂移（v2.24.7）**：
  - **修复 (Fixed)**：`docker-compose.multi.yml` 将 `auto-gpt`、`auto-gpt-plus`、`auto-plus2` 接入 OAIPay 的 `app_default` 外部网络；`services/chatgpt_core/oaipay_upload.py` 将历史 `https://gpt.cccy.me` URL 收敛为容器内 `http://gpt-cccy-me:8789`，分类读取、自动上传和手动上传不再经过 Cloudflare。
  - **兼容 (Changed)**：`services/oaipay_sync.py` 的远端账户回查和保留的 `api/integrations.py` 兼容入口复用同一内部地址，并优先请求以 `UPLOAD_KEY` 鉴权的 `/api/auto-gpt/*` 路由；需要浏览器管理员会话的 `/api/admin/*` 只保留为最后兼容回退。
  - **配置 (Changed)**：`api/config.py` 保存 OAIPay URL 时持久化内部地址，`frontend/src/pages/Settings.tsx` 的默认值和占位符同步为当前容器网络，避免旧表单再次写回公网域名；侧栏版本同步为 `v2.24.7`。
  - **测试 (Tests)**：新增 OAIPay URL 归一化、设置保存、分类读取和账户回查请求地址的隔离回归，覆盖旧公网 URL 与非目标自定义 URL 的兼容边界。

- **修复 TempMail 与 HME Ready 内部接口地址回退（v2.24.6）**：
  - **修复 (Fixed)**：`core/base_mailbox.py` 将已退役的 `tempmail.cccy.me`、旧 `127.0.0.1:18080-18083` 以及 HME 的 `hme.cccy.me` / `host.docker.internal:18765` 规范为当前容器拓扑可达的 `tempmail-api-1:8080` 与 `172.20.0.1:18765`；TempMail 内部转发固定使用 HTTP，避免把公网入口的 HTTPS 协议误带入容器 API。
  - **兼容 (Changed)**：`api/config.py` 在保存 TempMail/HME 配置时持久化规范后的内部地址，旧客户端、历史表单值和直接 API 调用不再把失效公网入口重新写回共享或实例配置；其它邮箱服务的自定义 URL 不受影响。
  - **优化 (Changed)**：`frontend/src/pages/Settings.tsx` 和 `frontend/src/pages/RegisterTaskPage.tsx` 的默认值、占位和帮助文本统一为当前生产网络，空配置下新建注册任务不会再生成容器内不可达的 `127.0.0.1:18081`。
  - **测试 (Tests)**：新增 `tests/test_mailbox_endpoint_normalization.py`，覆盖 TempMail/HME 旧地址兼容、非目标自定义 URL 保留及配置保存后的规范化结果；侧栏版本同步为 `v2.24.6`。

- **补齐 0 元资格“检测失败”筛选并修复派生索引回填（v2.24.5）**：
  - **修复 (Fixed)**：`frontend/src/pages/Accounts.tsx` 在账号页顶部筛选、列头筛选和筛选组合共用的“0 元资格”选项中新增“检测失败”；`services/account_filters.py` 新增带索引的 `zero_amount_eligibility_display_state`，按列表现有规则优先采用最新 `running / probe_failed / pending_auth` 技术状态，否则回落到 `eligible / ineligible / unknown` 业务确认态，使 `zero_amount_eligibility_state=probe_failed` 精确命中当前显示“0 元检测失败”的账号。
  - **兼容 (Changed)**：`extra.chatgpt_zero_amount_eligibility.confirmed_state` 和既有 `account_list_state.zero_amount_eligibility_state` 继续保存最近一次明确的 0 元/非 0 元结论；一次代理、网络或上游技术失败只更新展示态，不会把历史可用结论覆盖成失败，也不会混入“非 0 元”或“未检测”。
  - **优化 (Changed)**：`core/db.py` 与 `services/account_filters.py` 为三个实例兼容增加展示态列及索引，派生版本升级后自动回填；刷新流程在写入前冻结目标账号 ID，避免首段 UPSERT 清除 stale 条件后第二段支付方式投影漏写，同时保持逐账号写回和列表请求只处理实际缺失/陈旧账号，不引入请求时 JSON 全表扫描或无条件全表 UPDATE。
  - **测试 (Tests)**：新增历史 `eligible` 后最新 `probe_failed`、四种列表展示态、多选筛选、派生版本陈旧回填、支付方式后置投影和列表/批任务筛选范围一致性回归；前端合同覆盖“检测失败”选项，侧栏版本同步为 `v2.24.5`。

- **修复 Plus 大批量检测拖慢数据库与任务日志加载（v2.24.4）**：
  - **修复 (Fixed)**：`api/tasks.py` 的任务历史列表不再为计算第一页摘要把 `task_logs.detail_json` 全表加载到 Python；先按 `task_id` 读取轻量索引并完成任务去重，再只读取当前分页的详情行。SQLite 环境下的来源筛选改为数据库侧 JSON 提取，保留旧版 `meta.source` 兼容路径；大批量历史日志不再触发数百 MB 的全量反序列化。
  - **优化 (Changed)**：支付资格批任务保留任意正整数网络并发，但批量账号不再为每个探测额外写入一次临时 `running` 账号快照；最终账号结果仍完整写回 `accounts` 与 `account_list_state`。同一进程内的账号状态提交增加写入互斥，避免高并发 worker 互相触发 SQLite `database is locked`；任务内逐账号实时汇总继续立即更新，完整 `results` 列表改为按数量/时间节流快照并在终态强制刷新，消除全量结果列表重复排序、脱敏和复制造成的 CPU 放大。
  - **测试 (Tests)**：`tests/test_task_logs_history.py` 增加任务来源筛选和旧版 `meta.source` 兼容回归；前端侧栏版本同步为 `v2.24.4`。

- **修复支付资格任务实时统计并解除批量并发 10 上限（v2.24.3）**：
  - **修复 (Fixed)**：`frontend/src/components/TaskLogPanel.tsx` 在 0 元试用资格、支付方式和 GCash 支付方式任务运行期间每 500ms 读取一次活动任务快照，实时反映每个账号完成后的 `eligibility_summary`；任务进入终态后停止轮询，继续由 SSE 负责日志和任务结束通知，避免统计一直停留在任务初始的全零值。
  - **优化 (Changed)**：`frontend/src/pages/Accounts.tsx` 移除批量支付资格配置的“1-10”输入上限和本地存储截断；`api/tasks.py` 移除支付资格执行器的并发 10 硬限制，实际并发仍按本次可执行账号数收敛，保留正整数校验和单账号串行语义。注册后自动 0 元检测的独立并发 2 约束保持不变。
  - **测试 (Tests)**：新增批量并发大于 10 的后端任务创建回归，更新前端支付资格合同测试覆盖无上限正整数并发及运行中快照轮询；侧栏版本同步为 `v2.24.3`。

- **修复支付资格账号执行链整体失效并补齐通用支付方式状态合同（v2.24.2）**：
  - **修复 (Fixed)**：
    - `api/tasks.py` 恢复注册后自动 0 元检测、单账号检测与批量检测共同依赖的 `_run_payment_eligibility_for_account(account_id, kind, settings, ...)` 调用合同，修复 `v2.24.0` 半重命名后调用方仍引用旧名而触发 `name '_run_payment_eligibility_for_account' is not defined` 的致命错误；同时移除误引入且未定义的 `SessionLocal` 路径，恢复基于项目 `engine` 的会话边界。
    - 恢复账号级 `local_status_identity_slot` 互斥、注册后配置异常隔离、缺少 AccessToken 的 `pending_auth` 写回、探针中断收口和历史确认态保留，避免支付资格与本地状态刷新并发覆盖 `extra_json`，也不会让后处理故障反转已经成功的注册结果。
    - 探针输入恢复为已加载的 `AccountModel` 快照，修复普通 `dict` 无法被 `_access_token()`、Cookie/邮箱读取器与 Stripe 适配器识别，导致有效账号在进入 Checkout 前被误判为缺少认证信息的问题。
    - `_payment_eligibility_skip_reason()` 仅对新的通用 `PAYMENT_METHODS_KIND` 放开已订阅账号；既有 0 元、GCash 与链接格式任务继续保持原订阅预筛边界，修复 `v2.24.1` 条件过宽造成 GCash 已订阅账号被错误执行。
    - 支付方式任务将 `available / no_methods`、链接格式任务将 `oaics / cs` 统一识别为正常业务终态；`no_methods` 不再被任务执行器误记为技术失败，任务成功数、错误列表、实时摘要和历史摘要保持一致。
  - **优化 (Changed)**：
    - `services/account_filters.py` 保留旧 `gcash_payment_method_state` 字段与请求参数以兼容历史客户端和筛选组合，但派生索引改为优先读取 `extra.chatgpt_payment_methods.confirmed_state`，仅以历史 GCash 可用结果作为正向兜底；`no_methods / unavailable` 查询双向兼容，派生版本升级后自动刷新旧索引。
    - `frontend/src/pages/Accounts.tsx` 将支付方式筛选的负向值统一为 `no_methods`，加载旧组合时自动迁移 `unavailable`，并修正移动端残留的“GCash 方式”文案，确保桌面列、快速筛选和高级筛选使用同一通用支付方式语义。
  - **测试 (Tests)**：
    - `tests/test_payment_eligibility_tasks.py`、`tests/test_payment_eligibility_probe.py` 与 `frontend/tests/paymentEligibilityTaskContract.test.mjs` 增加注册共享 runner、位置参数兼容、已订阅账号边界、OAICS/Stripe 通用方式解析、`no_methods` 正常计数、账号持久化、筛选索引及旧值迁移回归。
    - 前端侧栏可见版本同步升级为 `v2.24.2`。

- **修复支付方式检测分发链路与代理探测容错（v2.24.1）**：
  - **前端交互与任务分发修复 (`frontend/src/pages/Accounts.tsx`)**：
    - 修复批量检测工具栏菜单点击分发中遗漏 `kind === 'payment_methods'` 导致点击“批量检测支付方式”无法触发弹窗和任务的问题。
    - 修复任务日志弹窗来源识别 `taskModalModeFromSource` 遗漏 `payment_methods` 与 `batch_payment_methods` 的问题。
    - 修复移动端表格视图中遗漏 `payment_methods` 列展示的问题，并同步更新快速筛选抽屉中的文案与选项。
  - **后端探测容错与代理链重构 (`services/chatgpt_core/payment_eligibility.py` & `api/tasks.py`)**：
    - 修复 `run_payment_eligibility_probe()` 传参兼容性，支持接收动态 `checkout_country_code` 等额外参数。
    - 优化 `_resolve_proxy_chain()` 代理出口逻辑：仅对 0 元试用（`ZERO_AMOUNT_KIND`）执行严格的 IP 出口国家校验；放开支付方式（`PAYMENT_METHODS_KIND`）与链接格式（`CHECKOUT_LINK_TYPE_KIND`）的强制 GeoIP 强校验拦截，允许在全局代理/直连模式下正常查询目标国家的结账收银台通道。
    - 调整 `_payment_eligibility_skip_reason()` 过滤逻辑，仅对 0 元试用限制“已订阅账号不可参与”，允许对已订阅账号或各类有效账号执行支付方式检测。
    - 前端 UI 版本号同步升级为 `v2.24.1`。

- **支持指定国家查询账号可用支付方式并拆分“0元资格”与“支付方式”为独立两列（v2.24.0）**：
  - **核心探测与全量国家支持 (`services/chatgpt_core/payment_eligibility.py`)**：
    - 新增 `PAYMENT_METHODS_KIND = "payment_methods"` 与 `probe_payment_methods()` 探测器，支持指定任意国家（参考 `openai-pay-long-link` 对齐的 232 国家/地区与 39 种币种映射表 `TEAM_BILLING_COUNTRY_CURRENCIES`）查询 ChatGPT 账号在目标国家下支持的所有支付方式。
    - 兼容 OAICS（提取 `payment_method_types`、`custom_payment_methods`，如菲律宾 PH 下的 GCash，并计算最终税后金额 `amount_display`）与 Stripe（提取 `payment_method_types` 如 Card, Link 等）双通道收银台。
    - 内置完善的支付方式中文映射字典（Card 信用卡/借记卡, PayPal, Pix, GCash, Kakao Pay, Naver Pay, Link, iDEAL, SEPA, Bancontact 等）。
  - **任务调度与持久化 (`api/tasks.py` & `api/accounts.py`)**：
    - 新增 `POST /api/tasks/chatgpt/payment-methods`、`POST /api/tasks/chatgpt/payment-methods/batch` 与 `GET /api/tasks/chatgpt/payment-methods/profile` 接口，支持单账号与批量按所选或筛选范围执行支付方式检测，动态联动目标国家的代理出口与币种。
    - 结果持久化写入 `extra["chatgpt_payment_methods"]`，包含国家、币种、通道提供商、方式列表与显示名称，并在目标国家为 PH 或包含 GCash 时同步向后兼容 `extra["chatgpt_gcash_payment_method"]`。
    - 账号详情接口向前端输出结构化的 `zero_amount_eligibility`、`payment_methods` 与 `gcash_payment_method` 数据。
  - **前端列拆分与 UI 交互升级 (`frontend/src/pages/Accounts.tsx` & 相关组件)**：
    - 账号管理表格原“支付资格”列正式拆分为“0元资格”（`zero_amount_eligibility`，宽 120px）与“支付方式”（`payment_methods`，宽 180px）两列，分别支持独立列头筛选与快速筛选。
    - 支付方式列以直观的彩色标签（如 `[BR] 信用卡/借记卡, Pix`、`[SG] 信用卡/借记卡, PayPal`、`[PH] GCash, 信用卡/借记卡`）展示，悬浮 Tooltip 呈现国家、币种、通道类型、完整支持方式列表、应付金额与检测时间。
    - 单账号操作菜单与批量检测工具栏新增“检测支付方式”入口，配置弹窗支持搜索全量 232 国家并自动联动币种与代理出口，浏览器本地存储持久化记忆最近选择。
    - `loadVisibleAccountColumnKeys` 增加向后兼容迁移逻辑，自动将历史持久化的 `payment_eligibility` 列偏好平滑展开为 `zero_amount_eligibility` 与 `payment_methods`。
    - `TaskLogPanel.tsx`、`TaskDetailHeader.tsx`、`RegisterTaskModal.tsx` 与 `taskTypes.ts` 接入支付方式检测的任务标签与结果概览统计。
    - 侧边栏底部系统版本号更新至 `v2.24.0`。


- **修正 Plus 实例 MiyaIP 住宅代理运行配置（v2.23.1）**：
  - 仅在 `auto-gpt-plus` 的实例本地 ConfigStore 启用 `dynamic + miyaip`，配置美国住宅 `mainKey`、代理密码、线路池 `1`、美国接入网关、HTTP 协议、Generate 超时、出口探测、国家强匹配和候选切换；未写入共享配置，也未改动主服务 `auto-gpt` 或 `auto-plus2`。
  - 写入前已备份 Plus 的 `account_manager.db` 并通过 SQLite 完整性检查；写入后使用保存配置完成真实 MiyaIP Generate 与出口探测，确认实际出口国家为美国且国家匹配通过。运行验证全程仅输出凭据存在性、长度、哈希摘要和脱敏代理地址。
- **优化注册配置折叠置顶、MiyaIP 字段语义与代理实测（v2.23.1）**：
  - `frontend/src/pages/Settings.tsx` 将“注册设置”接入与 ChatGPT 配置相同的面板管理方式：所有注册配置面板默认折叠，操作员可按“任务基础 / 运行资源 / 状态同步”选择常用面板置顶；置顶项自动前移并展开，选择通过独立的浏览器本地存储键持久化，不写入实例配置或共享配置。
  - “账号网络默认出口”中的 MiyaIP 配置改用“代理密码 / 主 Key / 线路池 / 接入网关 / 代理协议 / 接口超时”等业务标签，并以响应式网格集中展示；帮助文案明确代理密码对应 Generate 参数 `Crc`、住宅/移动主 Key 对应 `KeyName`，避免把网站登录 Token 或最终生成的代理用户名误填为鉴权参数。`frontend/src/pages/Proxies.tsx` 同步统一凭据提示与占位文案。
  - 全局配置新增“获取并测试代理”操作，直接使用当前尚未保存的表单参数调用既有 `POST /api/proxies/dynamic-preview`：Cliproxy 会刷新 SID，MiyaIP 会实时 Generate 一条线路，并继续探测基础连通性、出口 IP、实测国家、国家匹配与延迟；结果只展示脱敏运行代理，失败时区分未获取线路与已获取但出口验证未通过。
  - `frontend/tests/dynamicProxyProviderContract.test.mjs` 增加注册面板置顶持久化、默认折叠、MiyaIP 业务标签和动态代理测试请求合同；侧栏可见版本同步升级为 `v2.23.1`。
- **优化支付资格批量检测弹窗校验与前端产物同步（v2.23.0）**：
  - `frontend/src/pages/Accounts.tsx` 完善批量检测配置弹窗表单校验流程，捕获验证失败并弹出直观的输入错误 Toast 提示，修复之前因国家或并发字段未校验通过导致弹窗无反应或静默退出的问题；同时优化提交流程中非正常退出时的 loading 状态清理。
  - 完成宿主机前端构建（`npm run build`）并将最新静态构建产物同步更新部署至全部常驻容器。
- **解除批量支付资格与链接格式检测 1000 账号上限（v2.23.0）**：
  - `api/tasks.py` 移除了 `_resolve_batch_payment_eligibility_accounts()` 中对跨页多选与 `all_filtered=true` 当前筛选范围的 1000 个账号硬编码数量限制，支持一次性对 1700+ 及更多全量账号执行 0 元优惠检测、链接格式检测与 GCash 支付方式检测。
- **支付资格与链接格式检测自动识别 AT 失效并联动改写失效/刷新（v2.23.0）**：
  - `services/chatgpt_core/payment_eligibility.py` 与 `api/tasks.py` 在 0 元优惠检测、GCash 支付方式检测与支付链接格式检测期间，若 OpenAI 上游返回 `401 Unauthorized`（AccessToken 已过期/被撤销），检测结果明确标记为 `auth_invalidated`（`账号认证已失效 (HTTP 401)`）。
  - 后端自动同步改写账号本地探针状态 `chatgpt_local.auth`，通过 `apply_chatgpt_status_policy` 立即将账号主状态改写为 `invalid` 并刷新 `account_list_state` 派生索引，同时无锁调度 `schedule_chatgpt_local_status_refresh_for_account_id` 尝试使用 Refresh Token 自动换新或确认最终死活，实现单步检测顺带排查死号与自动联动刷新。
- **新增支付链接格式检测与账号列表链接类型列及筛选（v2.23.0）**：
  - `services/chatgpt_core/payment_eligibility.py` 新增 `CHECKOUT_LINK_TYPE_KIND = "checkout_link_type"` 与 `probe_checkout_link_type()`，通过请求 ChatGPT Checkout API 获取结账收银台会话后，直接根据返回的提供商与 Session ID 前缀区分 `oaics`（OpenAI 原生收银台链接，`oaics_...`）与 `cs`（Stripe 托管收银台链接，`cs_...`），并在完成收银台创建后立即返回结果，无需等待后续 Promotion/Taxes 阶段。
  - `api/tasks.py` 注册 `checkout_link_type` 任务源并提供 `POST /api/tasks/chatgpt/checkout-link-type` 与 `/batch` 接口，支持单账号与批量按所选或筛选范围执行支付链接格式检测，运行日志与任务快照统一接入任务中心。
  - `core/db.py` 与 `services/account_filters.py` 在 `account_list_state` 派生表中新增 `checkout_link_type` 字段与索引，支持通过 SQL 快速筛选 `oaics`、`cs` 或 `none`（未检测/无链接），自动从账号已有的收银台链接、支付资格检测记录或最新链接格式检测中提取并索引。
  - `frontend/src/pages/Accounts.tsx`、`AccountsToolbar.tsx` 与 `AccountActionSurface.tsx` 在账号列表中新增独立的“链接类型”列（展示蓝色 `OAICS` / 紫色 `Stripe (CS)` / 灰色 `-` 标签），支持表格列头筛选与顶栏/高级筛选弹窗筛选，并为单账号操作菜单与批量检测工具栏增加了“检测支付链接格式”入口。
  - 前端版本号同步升级为 `v2.23.0`。
- **0 元试用资格按所选结账国家读取最终 Plus 应付价格（v2.22.0）**：`frontend/src/pages/Accounts.tsx` 将原“优惠检测代理国家”收敛为“结账国家”，通过独立只读接口 `GET /api/tasks/chatgpt/zero-amount-eligibility/profile` 加载 ChatGPT Checkout 国家/币种目录；单账号、批量和注册后自动检测都把所选国家冻结为 `checkout_country_code`，后端从同一目录得到对应币种并写入 Checkout `billing_details.country/currency`、Promotion 和 Taxes `billing_country/currency/billing_address.country`。OAICS 继续读取 Taxes 刷新后的 `checkout_state.total.total.minorUnitsAmount`，Stripe 继续读取既有 `payment_pages/init` 结构化金额；结果新增按 ChatGPT currency exponent 格式化的 `amount_display`，账号列表和注册任务可直接查看该国 Plus 最终应付价格，资格判定仍严格使用原始最终金额是否为 `0`。
- **ChatGPT 注册成功后自动检测 0 元试用资格（v2.21.1）**：`api/tasks.py` 在账号成功落库、HME/外部自动同步完成后，将该账号交给新的 `services/chatgpt_core/registration_eligibility.py` 后处理协调器，自动复用正式 `checkout -> promotion -> taxes` 资格链；检测档案固定为 Plus `chatgptplusplan`、PH/PHP、`plus-1-month-free` 与 `US -> VN -> US`，网络参数在注册任务入队时按所属实例冻结，不复用注册账号出口。资格请求在独立线程池执行，不占注册 worker；单任务和进程级实际并发均限制为 `2`，多个重叠注册任务也不会绕过总上限。注册主体完成后会等待已入队检测收口再进入任务终态，确保任务快照展示的是完整结果而非后台悬空状态。
- **注册任务和账号列表展示自动资格结果（v2.21.1）**：新增 `frontend/src/features/auth/components/RegistrationEligibilitySummary.tsx`，并接入注册弹窗及独立注册页，实时展示待检测、检测中、0 元可用、非 0 元、检测失败、待补 Auth、已跳过及最近逐账号结果；档案摘要明确显示 PHP 与实际 Checkout/Promotion/Taxes 地区链。`api/accounts.py` 为紧凑账号列表增加脱敏的最新尝试状态、原因、金额、验证阶段和地区链，`frontend/src/pages/Accounts.tsx` 的既有“支付资格”列据此区分检测中、检测失败和待补 Auth，同时保留最近一次业务确认态；侧栏版本同步为 `v2.21.1`。
- **新增 MiyaIP 动态代理渠道并保留 Cliproxy 双渠道（v2.21.0）**：`core/miyaip_proxy.py` 按 MiyaIP 当前开发者中心合同接入 `GET https://miyaip.com/api/ProxyLogic/Generate`，固定 `Num=1`、`SessionTime=-1`、`Format=1`，并支持 `Country / Server / Crc / Pool / KeyName / GenType`；`Format=1` 响应按 `username:password@host:port` 解析，HTTP 直接进入现有请求链，SOCKS5 统一规范为远端 DNS 的 `socks5h://` 并复用现有 Playwright 认证桥。`core/proxy_utils.py` 将 `dynamic` 扩展为 `cliproxy | miyaip`，失败切换只在当前渠道内刷新 SID 或重新 Generate，不做 MiyaIP 与 Cliproxy 之间的隐式回退；支付资格的 Checkout、Promotion、Taxes 三段代理、注册、失效测活、执行登录态、自定义邮箱测活、批量本地状态同步和手机号绑定均复用同一解析合同。
- **代理管理与任务入口增加双渠道选择（v2.21.0）**：`frontend/src/pages/Proxies.tsx` 增加 `Cliproxy / MiyaIP` 分段选择、MiyaIP `Crc / KeyName / Pool / 美洲-亚洲-欧洲网关 / HTTP-SOCKS5 / 请求超时` 配置及实时 Generate/出口预览；`Settings.tsx` 提供同样的全局字段并继续按 touched-field 保存，切换渠道不会删除未启用渠道的配置。注册页、注册弹窗、失效测活、执行登录态、自定义邮箱测活和手机号绑定只选择 provider 与公共任务参数，不在任务表单携带 MiyaIP 凭据；侧栏版本同步为 `v2.21.0`。
- **0 元优惠检测支持选择 Promotion 代理国家（v2.19.6）**：`frontend/src/pages/Accounts.tsx` 在单账号 0 元检测前新增配置弹窗，并在既有批量并发弹窗中增加可搜索的“优惠检测代理国家”选择，默认及旧客户端继续使用 `VN`，当前浏览器会保存最近一次有效选择；单账号与批量请求通过专用 `promotion_proxy_country_code` 字段提交，避免与账号任务的通用代理国家混用。`api/tasks.py` 与 `services/chatgpt_core/payment_eligibility.py` 仅将该选择应用于 0 元检测的 Promotion 刷新，Checkout、Taxes 继续固定 `US`，PH/PHP 账单合同不变，GCash 即使收到覆盖字段也强制保持 `US -> VN -> US`。请求入口严格校验两位 ISO 国家代码，任务 meta、成功结果、Promotion 明确业务拒绝、技术失败 evidence 及账号确认态均记录实际地区链且不暴露代理地址；旧结果缺少地区证据时回退 `US -> VN -> US`。专项后端回归覆盖默认值、国家覆盖、GCash 隔离、结果分类与持久化，前端合同覆盖单/批入口、浏览器持久化和字段隔离；侧栏版本同步为 `v2.19.6`。
- **账号页支持批量复制 AccessToken（v2.19.1）**：`frontend/src/pages/Accounts.tsx` 与 `frontend/src/features/accounts/components/AccountsToolbar.tsx` 在现有导出旁增加独立“复制 AT”操作；有跨页勾选时优先复制已选账号，无勾选时复用当前完整筛选范围、固定组合 revision 与 `expected_total` 一致性校验，不会退化为仅复制当前分页。复制链路复用现有一次性导出票据和 AccessToken 专用查询，程序化读取每行一个 AT 的纯文本响应后直接写入剪贴板，不触发文件下载，也不在列表接口、浏览器存储、日志或提示中保留凭据；大批量处理提供 loading，成功时报告实际复制数及无 AT 跳过数，空结果、筛选范围变化和浏览器剪贴板拒绝均给出明确反馈。原 Sub2API JSON、AccessToken TXT 与 PIX 链接导出合同保持兼容；前端合同测试及侧栏版本同步为 `v2.19.1`。
- **批量支付资格检测增加可持久化并发设置（v2.18.2）**：`frontend/src/pages/Accounts.tsx` 在批量“0 元试用资格”和 GCash 支付方式检测启动前增加统一配置弹窗，可按当前所选账号或筛选范围设置 `1-10` 并发，并在当前浏览器保存最近一次取值；提交值继续由 `api/tasks.py` 的支付资格执行器二次截断到实际可执行账号数，启动反馈明确展示有效并发。单账号检测仍固定串行，代理来源、重试、预筛、停止控制和结果持久化合同保持不变；前端合同测试及侧栏版本同步为 `v2.18.2`。
- **新增独立的 0 元试用资格检测任务（v2.18.0）**：新增 `services/chatgpt_core/payment_eligibility.py` 及 `POST /api/tasks/chatgpt/zero-amount-eligibility`、`POST /api/tasks/chatgpt/zero-amount-eligibility/batch`，固定使用 Plus `chatgptplusplan`、PH/PHP、`custom` Checkout 和 `plus-1-month-free`，按 Checkout US、Promotion VN、Taxes US 的独立代理阶段读取最终结构化金额。OAICS 分支只读取 `checkout_state.total.total.minorUnitsAmount`，Stripe 分支复用现有 `checkout_probe` 的 `payment_pages/init` 结构化金额解析；最终金额为 0 与非 0 分别落为 `eligible / ineligible`，协议、网络和上游异常单独记为 `probe_failed`，不会把技术失败冒充无资格。
- **新增独立的 GCash 支付方式检测任务（v2.18.0）**：新增 `POST /api/tasks/chatgpt/gcash-payment-method` 与 `/batch`，只在 `oaics_* + checkout_provider=open_ai + processor_entity=openai_llc` 且初始、优惠刷新、税费刷新三个节点始终暴露同一个唯一 `cpmt_*` custom method 时确认 `available`；Stripe `cs_*` 或最终方法缺失、歧义、漂移时确认 `unavailable`。检测严格停在支付方式可用性确认，不调用 checkout confirm/approve、`custom_payment_method/start`、Adyen/provider 跳转或二维码生成，不产生扣款和支付链接。
- **账号页增加支付资格操作、双状态列和筛选（v2.18.0）**：`frontend/src/pages/Accounts.tsx`、`AccountsToolbar.tsx` 与 `AccountActionSurface.tsx` 增加单账号动作和“支付资格检测”批量菜单，两个功能分别支持所选账号或当前筛选范围；账号列表以同一“支付资格”列并列展示“0 元可用/非 0 元/未检”和“GCash 可用/不可用/未检”，桌面、移动端、列筛选、筛选组合、实时任务日志和历史任务详情均保持两个状态独立。列可见性存储升级到 `v4`，兼容迁移 `v2/v3` 偏好并为现有用户补入新列；侧栏版本同步为 `v2.18.0`。
- **执行登录态升级为人工控制的持久浏览器租约（v2.17.0）**：新增 `services/chatgpt_core/web_session_lease.py`，单账号登录成功并核对身份后立即写回最新 AccessToken、Session Token、完整 Cookie 和浏览器指纹，同时把同一 Playwright BrowserContext/Page 保持在线；任务持续处于 `ready_holding`，只有操作员明确请求释放后才原子保存 `/runtime/chatgpt_browser_profiles/<account_id>/storage-state.json` 并关闭本地 Camoufox。下次执行优先注入保存的 Cookie、LocalStorage、IndexedDB、Session Token 与 `oai-did`，现有 `/api/auth/session` 有效时直接跳过密码和邮箱 OTP；Profile 无效时才回落到原有已有账号登录链路。
- **任务日志面板新增逐账号登录态浏览器控制（v2.17.0）**：`api/tasks.py` 新增 `GET /api/tasks/{task_id}/web-session-leases`、逐账号 `refresh / release` 与 `release-all` 接口；`frontend/src/components/TaskLogPanel.tsx` 每 2 秒同步任务快照和租约状态，展示账号、保持时长、Profile 落盘/注入状态、刷新次数及异常原因，支持同步最新 AT/Session/Cookie、逐账号保存释放、批量停止新增浏览器和停止并释放全部。登录态任务不再显示容易混淆的通用“跳过当前/立即停止”主操作，单账号与批量任务均在人工释放前保持 `running`。
- **增加实例本地的登录态保持容量配置（v2.17.0）**：`chatgpt_web_session_hold_max_sessions` 可在“全局设置 → 注册设置”调整，范围 `1-32`，并被 `core/shared_config.py` 强制排除出共享模板；`docker-compose.multi.yml` 为主服务、Plus、Plus2 分别设置默认 `2 / 4 / 2`，批量任务的实际并发受当前实例持久浏览器容量截断并在浏览器释放后滚动补位。
- **新增 ChatGPT 单账号与批量“执行登录态”任务（v2.16.0）**：新增 `services/chatgpt_core/web_session_login.py` 及 `/api/tasks/chatgpt/web-session-login`、`/api/tasks/chatgpt/web-session-login/batch`，对任意业务状态的 ChatGPT 账号复用 any-auto Camoufox `login_only` 浏览器链路，按账号保存的邮箱通道自动处理登录 OTP，并以 AccessToken、Session Token、完整 Cookie 和明确 ChatGPT account ID 同时存在作为成功判据。写回前会同时核对账号行邮箱、原 ChatGPT account ID 与新 Session 身份，身份不一致或材料不完整时保留全部旧凭据；成功时仅更新 `token/access_token`、`session_token`、`cookies/cookie_header`、`account_id/workspace_id`、恢复后的邮箱状态和 Session 浏览器身份，保留现有 refresh token、`used`、订阅、手机号绑定、邮箱绑定及其他业务状态，随后独立调度本地状态刷新。该任务为 GCash 等后续提链提供最新 AT，但 `openai-pay-long-link` 继续维持 AT-only 边界，不接收 Cookie、Session 或浏览器上下文。
- **账号页增加登录态任务操作面与完整任务日志（v2.16.0）**：`frontend/src/pages/Accounts.tsx` 在每个 ChatGPT 账号行增加“执行登录态”，工具栏增加可置顶的“批量执行登录态”；单账号与批量入口共用动态代理、代理池、指定代理、直连及失败切换配置，批量支持账号筛选范围和无固定业务上限的并发数。`api/tasks.py` 使用滚动补位线程池、短数据库 Session、任务级代理候选、逐账号结果、立即停止/完成当前后停止和持久化任务快照；准备、代理选择、浏览器登录、邮箱 OTP、Session 捕获、身份核对、写回及终态均进入现有实时日志和历史详情面板。`RegisterTaskModal.tsx`、`taskTypes.ts` 与 `AccountsToolbar.tsx` 增加独立标题、来源和结果标签，不再把该动作混同为失效测活或补抓 Auth。
- **固定账号组合直接展示订阅状态分布（v2.15.3）**：`services/account_fixed_groups.py` 与 `api/accounts.py` 在 `/api/accounts/filter-presets` 的每个 `fixed_groups` 项中新增 `subscription_counts.plus / free / unknown`，一次批量查询全部固定组成员后按当前确认订阅聚合；Plus 同时归并 Pro、Team、Business、Enterprise 等付费计划，历史订阅、待刷新快照和无法确认的计划统一进入 Unknown。`frontend/src/features/accounts/components/FilterPresetBar.tsx` 在固定组合下拉项、当前选中项和置顶快捷按钮悬浮时以 `p / f / u` 紧凑显示数量，组合管理列表常驻显示 `Plus / Free / Unknown` 全称统计；类型为绿色、数量为白色，并保留键盘可访问的管理列表事实源。
- **新增注册全链路诊断抓包与制品管理（v2.15.0）**：`services/chatgpt_core/registration_diagnostics.py`、`api/registration_diagnostics.py` 与 `core/db.py` 为 ChatGPT 浏览器注册增加“关闭 / 智能诊断 / 全量留存”三种任务级模式。每次注册尝试使用独立目录同步采集 Playwright Trace、full HAR、关键业务响应、console/pageerror/requestfailed、任务与邮箱事件、运行时快照、最终 DOM/截图和仅含哈希的 Cookie 元数据，并自动生成规则型 `diagnosis.json`；手机号注册的 curl 链路生成脱敏 HAR 兼容包，全量模式在浏览器运行时支持时额外保留单页视频或多页面 `video.zip`。诊断制品存入各实例独立的 `/runtime/registration_diagnostics`，`account_manager.db` 仅保存可检索的生命周期索引，不将 HAR、Trace 或视频写入 SQLite/WAL。
- **任务详情新增诊断制品操作面（v2.15.0）**：`frontend/src/components/RegistrationDiagnosticsPanel.tsx` 与 `TaskLogPanel.tsx` 在注册任务详情展示采集状态、失败阶段/错误码、脱敏账号、大小及 Trace/HAR/协议 HAR/视频/现场标签，支持完整诊断包或单文件下载、固定保留、取消固定和显式删除；运行中任务每 8 秒静默刷新。桌面使用固定操作列，移动端保持页面本身不横向溢出，通过表格内部横向滚动访问阶段、账号和制品列。
- **账号列表可显式设置默认每页显示数量（v2.14.2）**：`frontend/src/pages/Accounts.tsx` 与 `frontend/src/features/accounts/components/AccountsTable.tsx` 将“当前使用的每页条数”和“以后打开账号列表时的浏览器默认值”拆分管理；分页设置弹层在 `10/20/50` 及自定义选项右侧新增“设为默认”按钮，并在选项和按钮上明确标出当前默认值。临时切换页大小或应用携带页大小的筛选组合不再覆盖默认值；删除作为默认值的自定义选项时会安全回落到 `20`，旧版已保存的浏览器页大小继续作为升级后的初始默认值。侧栏可见版本同步为 `v2.14.2`。
- **新增实例本地的注册浏览器容量控制面（v2.14.0）**：`api/config.py`、`core/shared_config.py` 与 `frontend/src/pages/Settings.tsx` 新增 `chatgpt_runtime_*` 配置组，可在“全局设置 → 注册设置”独立调整浏览器容量模式、Auth/注册浏览器上限、启动错峰、单次 PID 预算、PID 应急保留、宿主机内存保留、CPU PSI 阈值、注册页面状态等待，以及 Solver 模式、暖机数、最大浏览器数和空闲回收时间；该前缀强制留在当前实例，不进入 `shared_config`，避免 Plus 的高容量参数传播到主实例或 Plus2。`api/system.py` 同步暴露当前浏览器占用、资源门禁和 Solver 池的实际运行指标，设置保存后 Solver 会自动按新参数重启。
- **账号列表支持自定义每页显示数量（v2.13.3）**：`frontend/src/pages/Accounts.tsx` 与 `frontend/src/features/accounts/components/AccountsTable.tsx` 在现有 `10/20/50` 基础项旁增加分页设置入口，可添加并立即使用 `1-200` 的任意整数（包括 `35`、`100`），自定义项保存在当前浏览器并可通过标签关闭按钮删除；删除当前值时自动回落到 `20`，移动端也可使用同一入口。筛选组合继续保存页大小，应用已删除但仍被组合引用的自定义值时会自动恢复该选项；`api/accounts.py` 同步放宽组合归一化边界到账号列表接口已有的 `1-200`，超界或非法历史值仍回落到 `20`。侧栏可见版本同步为 `v2.13.3`。
- **账号筛选组合升级为两级排他结构（v2.13.0）**：`core/db.py` 与新增的 `services/account_fixed_groups.py` 引入实例本地 `account_fixed_groups / account_fixed_group_members`，一级仅保存动态条件组合，二级固定账号组合必须挂在一个一级组合下；成员表以账号 ID 为唯一主键，并同时保存规范化邮箱与创建时间，保证一个账号在单实例内最多归属一个固定组合且 SQLite ID 复用不会误绑定。`api/accounts.py` 的组合接口新增 `dynamic_items / fixed_groups / legacy_fixed_items` 分区响应、父级校验、冲突 `409`、成员 revision 和显式移动能力，旧 `items` 与 `filter_preset_id` 继续兼容。
- **旧固定组合提供显式迁移预览（v2.13.0）**：账号页组合管理新增旧结构迁移入口，操作方必须逐组选择一级父级并明确排列冲突优先级；预览展示迁入、重复、缺失和不符合父级的账号数，不符合父级时禁止提交。提交前使用 SQLite backup API 备份实例账号库并执行 `PRAGMA integrity_check`，成功后才移除已迁移的旧配置；发布过程不会自动选择父级、不会自动处理重复归属，也不会触发迁移。
- **失效测活支持统一代理方式与账号级批量并发（v2.12.0）**：`frontend/src/pages/Accounts.tsx` 将账号行内与工具栏“批量失效测活”统一接入同一个任务配置弹窗，可选择动态代理、代理池、指定代理或直连；批量入口新增可持久化的并发数。`api/tasks.py`、`api/actions.py` 与 `services/chatgpt_core/invalid_account_recheck.py` 将任务级代理参数贯穿到 any-auto Camoufox `login_only` 浏览器事务，按现有候选策略执行代理池筛选、动态 SID 刷新和仅限网络/代理故障的候选切换，并在任务 meta 中记录脱敏代理摘要、请求/实际并发和逐账号结果。批量 runner 使用滚动补位线程池，账号读取、浏览器网络事务和结果写回使用分离的短数据库 Session，避免并发登录期间占满 SQLAlchemy 连接池；旧客户端未传代理或并发时继续保持直连、串行，现有筛选范围和 `status=invalid` 门禁不变。
- **恢复 Plus 登录态短链生成（v2.10.0）**：`services/chatgpt_core/payment.py`、`plugin.py` 与 `api/tasks.py` 重新开放 `payment_source=chatgpt_hosted + payment_link_format=short_chatgpt`，使用 `checkout_ui_mode=custom` 创建 Checkout Session，并直接返回 `https://chatgpt.com/checkout/<processor_entity>/<cs_id>`，不再把短链归一化为 `pay.openai.com` Hosted 长链。账号页原“支付链接生成”弹窗新增“Plus 登录态短链”，支持本地账单国家/币种选择，单账号与批量范围继续复用同一任务、日志、当前链接和生成历史合同；现有 Plus long-link 与 Team 优惠码长链接入口保持不变。
- **HME Ready-only 邮箱通道（v2.9.1）**：`core/base_mailbox.py`、`services/chatgpt_core/mailbox_state.py` 与 `services/chatgpt_core/restored_email_service.py` 将历史 `icloud_hme` / `helper_ready_api` 状态统一规范为 `hme_ready_api`。Helper 只负责 HME 出池、lease、registration/logical/physical identity 与 finalize；验证码读取固定由 auto-gpt 通过 TempMail 转发箱完成，注册状态不再依赖 Helper `/wait-code`。
- **筛选组合支持固定账号成员**：`api/accounts.py` 将原有组合契约扩展为向后兼容的 `dynamic / fixed` 两种内容模式；未勾选账号时仍保存动态筛选条件，人工跨页勾选账号后则可从同一个“筛选组合”入口保存固定成员。固定组合使用实例本地配置持久化，支持现有的选择、置顶、复制、编辑、覆盖、还原和删除操作；账号状态变化不会改变成员，新出现的同条件账号也不会自动加入。`GET /api/accounts` 新增短参数 `filter_preset_id`，按组合 ID 分页返回固定成员，避免在 URL 中展开最多 5000 个账号 ID；现有批量任务和 PIX 导出继续复用明确的 `account_ids` 范围。
- **支付链接列新增“当前有链接 / 当前无链接”筛选**：`frontend/src/pages/Accounts.tsx` 在账号表“支付链接”列的当前链接筛选中增加 `当前有链接`，并保留 `当前无链接`；`services/account_filters.py` 将 `has_link / with_link / current_has_link` 统一展开为 Hosted、PayPal、iDEAL、UPI、PIX、TWINT、Kakao Pay、Team 与 other 等当前实际可打开链接类型，列表接口、批量任务筛选范围和筛选组合复用同一后端语义，避免把历史“已成功提取”误当成当前仍有链接。
- **新增 Docker 测试规范**：`docs/testing-in-docker.md` 固化运行依赖统一、测试镜像与生产镜像同源、一次性测试容器、临时数据库/共享配置、网络隔离、浏览器资源约束和外部实时烟测分层要求，明确禁止在常驻业务容器或生产挂载上执行完整 pytest。
- **新增并校正 ChatGPT 注册失败分析文档**：`docs/chatgpt-registration-failure-analysis.md` 基于主实例与 Plus 实例的历史 `task_logs`，结合 `api/tasks.py`、any-auto 注册链、Sentinel 浏览器和 Docker 运行态重新核对统计与调用边界。文档不再把旧线程池上限 `5` 写成默认并发，不再把独立 `:8889` Solver、无 cgroup 总内存上限时的第二槽门控或未经日志证明的 CSRF 假设写成当前注册根因，并明确区分相关性、因果性、同进程出口租约和跨容器残余风险。

### 优化 (Changed)
- **0 元检测改为一国一出口一 Checkout 环境（v2.22.0）**：`services/chatgpt_core/payment_eligibility.py` 对每次 0 元检测只解析一个代理并复用于 Checkout、Promotion、Taxes，三个请求同时复用同一个 `curl_cffi.Session`；任何代理模式都必须通过 `basic + geo` 确认实际出口等于所选结账国家，直连、出口不匹配或 GeoIP 无法确认均关闭式记为技术失败，不再把请求标签冒充实际出口。动态代理仍复用 `resolve_task_proxy_candidates()` 的 SOCKS 规范化、MiyaIP/Cliproxy 生成和实例配置，但国家选择同时覆盖实际代理出口、Checkout 账单国家、币种和 Taxes 地址。
- **保持 GCash 与普通支付链接边界不变（v2.22.0）**：GCash 支付方式检测仍固定使用 PH/PHP 与 `US -> VN -> US`，保持每个阶段独立代理解析和独立 HTTP Session，不接收 `checkout_country_code`；本次不新增 GCash 提链，不调用 confirm/approve/provider start，也未修改普通 Plus、Team、短链或 long-link 生成合同。旧客户端的 `promotion_proxy_country_code` 仅作为 0 元检测国家输入兼容回退；旧 `US -> <Promotion> -> US` evidence 继续保留历史 PH/PHP 含义，不会被新规则重解释为其他国家/币种。
- **统一支付资格的账号级执行与状态写回边界（v2.21.1）**：`api/tasks.py::_run_payment_eligibility_for_account()` 现在由手工单账号、批量任务和注册后自动检测共同复用，并与本地状态刷新共用 `local_status_identity_slot`，避免并发读改写整段 `extra_json` 时互相覆盖。`eligible / ineligible` 继续更新 `extra.chatgpt_zero_amount_eligibility.confirmed_state`，`running / probe_failed / pending_auth` 仅更新 `last_attempt`，不会抹掉历史已确认结论；所有写回同时刷新 `account_list_state` 派生筛选。缺少 AccessToken 的新注册账号明确记录为 `pending_auth`，认证失效或已订阅账号保持跳过，不修改账号 `status`、`used`、订阅、认证、手机号、邮箱或支付链接。
- **统一动态代理渠道的继承与旧客户端兼容语义（v2.21.0）**：真正省略代理模式或显式使用 `global / config / task / task_proxy / default / inherit` 时读取当前全局 provider；旧客户端显式提交 `proxy_mode=dynamic` 但没有 provider 时固定解释为 Cliproxy，避免全局切到 MiyaIP 后历史请求被静默改义。两套渠道配置长期共存，默认 provider 仍为 Cliproxy，上线不会自动切换现有出口；账号页“保存注册设置”同步持久化 provider，避免表单选择与实际全局渠道分叉。
- **管理员认证改为 12 小时滑动空闲可信期（v2.20.0）**：`api/auth.py` 与 `core/db.py` 参考 `/opt/gpt.cccy.me` 的服务端会话维护边界，将原先 JWT 和数据库共用固定 12 小时截止的实现改为“连续空闲 12 小时才重新验证”。新会话按 5 分钟节流记录最近认证活动、按 1 小时节流把空闲截止滚动到当前时间后 12 小时，并保留不可续写的 7 天绝对登录上限；密码/TOTP 变更、主动注销、全量撤销、实例隔离和 `auth_version` 失效继续即时生效。旧 `AUTH_SESSION_TTL_SECONDS/auth_session_ttl_seconds` 配置继续作为兼容回退，旧数据库会话只迁移字段、不延长原固定过期时间；`frontend/src/pages/Settings.tsx` 显示服务端实际空闲期和绝对上限，登录页与侧栏同步为 `v2.20.0`。
- **清理前端已知漏洞依赖（v2.20.0）**：`frontend/package.json` 与 lockfile 将 `react-router-dom/react-router` 从 `7.13.1` 升至 `7.18.2`、Vite 升至 `8.2.1`，并更新 Babel、PostCSS、brace-expansion 等无破坏性的传递依赖；更新后 `npm audit` 的生产依赖与完整开发依赖树均为 0，现有 BrowserRouter、React 19、Vite 分块和前端合同保持不变。
- **全项目运行时与用户可见时间统一为北京时间（v2.19.2）**：`Dockerfile`、`docker-compose.multi.yml` 及单实例编排为主服务、Plus、Plus2、Phone API Relay 和 Turnstile Solver 固定 `TZ=Asia/Shanghai`，镜像安装 `tzdata` 并同步 `/etc/localtime`；`core/timezone.py` 为任务日志、阶段起止、手机号结果、注册元数据、诊断制品、后台调度器、交付卡自然日统计和导出命名提供显式 `+08:00` 时间。`core/logging_config.py` 同时为 Uvicorn 应用/访问日志写入带 `+0800` 的北京时间戳，不依赖 Docker daemon 的 UTC 日志前缀。OAuth/JWT/Stripe/OpenAI 协议时间、epoch 计算、数据库排序和过期比较继续保留 UTC 绝对时间，避免把时区展示要求错误扩散到鉴权与业务判断。
- **所有管理端时间固定按 `Asia/Shanghai` 展示（v2.19.2）**：新增 `frontend/src/lib/dateTime.ts`，统一解析 epoch、带偏移 ISO 时间以及 SQLite 历史无偏移 UTC 行，并用 `Intl.DateTimeFormat` 明确指定北京时间；任务历史/详情、账号注册与订阅刷新、BaxiGPT 卡密、手机号池、交付卡、代理扫描/调度、Codex 探测、共享配置和注册诊断桌面/移动端不再跟随访问者浏览器所在时区。任务历史 API 和系统健康接口同步返回显式 `+08:00` 与 `timezone=Asia/Shanghai`，便于页面、脚本和运维探针使用同一事实源。
- **注册浏览器改为单进程多无痕上下文（v2.19.0）**：新增 `services/chatgpt_core/shared_camoufox.py`，每个业务实例按 `headless / headed` 运行模式懒启动一个 Camoufox Server，并由 `any_auto/browser_register.py`、`browser_registration.py` 与 `sentinel_browser.py` 为每个注册 worker 预分配独立 `BrowserContext + Page`；Cookie、LocalStorage、动态代理、GeoIP 时区/语言、HAR/Trace 和持久登录态 Profile 均保持 context 级隔离，不再让注册并发数直接等于完整 Camoufox 进程数。Playwright Firefox 在多个远端 client 并发创建 page 时会阻塞，因此共享 Server 使用 shared dispatcher，由受锁保护的管理连接串行创建带随机 context token 的标记页和工作页，worker 随后并发操作各自工作页；正常退出、异常、停止和硬超时均由父进程按 token 兜底关闭 context，不会杀死其他注册会话。
- **共享 Camoufox 使用 context 资源门禁并公开真实隔离边界（v2.19.0）**：共享进程首次启动仍执行原完整浏览器 PID、内存和启动错峰检查，进程就绪后的新增注册槽改用默认 `32 PID / 384 MiB` 的 context 预算，不再重复套用 Plus 的 `220 PID / 1280 MiB` 完整进程预算；空闲 `300s` 后自动回收共享进程，并显式排除运行镜像中反复下载失败的可选 UBO 扩展，避免首次注册为无效外网下载额外等待。`api/system.py` 的浏览器容量指标经 `browser_capacity_snapshot()` 新增共享进程 PID、活动 context、generation、空闲回收、存储/代理隔离范围和 `fingerprint_scope=browser_process`，并明确 WebRTC 在进程级关闭以避免不同 context 泄露出口；Canvas、WebGL、字体等深层 Camoufox 指纹仍由同一浏览器进程共享，不伪装成账号级独立指纹。断网、只读 checkout、临时 runtime 的一次性容器专项回归 `78 passed`，真实 Camoufox 双代理探针确认两个并发 worker 在单一浏览器进程中各自使用独立代理与 LocalStorage，隔离 worker 子进程也能领取并回收父进程预分配页面；侧栏可见版本同步为 `v2.19.0`。
- **账号当前订阅与固定组合统计拆分不可确认和待刷新（v2.18.4）**：`services/account_filters.py` 在不改写 `account_list_state.subscription_type` 既有套餐索引的前提下，为订阅 Unknown 增加 `unconfirmable / pending_refresh` 两个可筛选状态；当前套餐明确为 Free 或付费计划时继续保持原精确套餐筛选，套餐未知且认证失效归为“不可确认”，其余未知态归为“待刷新”。SQL 账号列表与 Python 批量任务回退路径复用同一分类，历史筛选组合保存的 `unknown` 继续命中两类全集，避免升级后组合范围缩小。`services/account_fixed_groups.py` 与固定组合 API 将原 `Plus / Free / Unknown` 聚合升级为 `Free / Plus / 不可确认(u) / 待刷新(w)` 四桶，并保留旧 `unknown` 聚合字段供滚动发布期间的旧页面读取；`frontend/src/pages/Accounts.tsx` 的桌面表头、移动筛选和组合编辑器同步提供两个独立选项，旧浏览器组合值会无损展开为新状态。
- **支付资格结果采用确认态与尝试态分层持久化（v2.18.0）**：`api/tasks.py` 分别写入 `extra.chatgpt_zero_amount_eligibility` 和 `extra.chatgpt_gcash_payment_method`；业务结论更新 `confirmed_state / confirmed_at / evidence`，技术失败只更新脱敏的 `last_attempt`，保留上一次已确认结论。`core/db.py` 与 `services/account_filters.py` 新增 `zero_amount_eligibility_state / gcash_payment_method_state` 物化筛选字段及旧库在线迁移，列表与批量任务复用同一筛选范围合同。任务预筛会跳过缺少 AT、认证失效和明确已订阅账号，检测过程不修改账号 `status`、`used`、订阅、认证、手机号、邮箱或已有支付链接，也不持久化 AT、原始代理、Checkout session id 和完整上游 payload。
- **持久登录态使用独立容量与浏览器线程所有权（v2.17.0）**：`sentinel_browser.py` 将长时间保持的浏览器从普通 Auth/注册槽位拆出独立计数和信号量，但在启动前仍把普通与持久浏览器合并执行 PID、cgroup/宿主机内存和 CPU PSI 门禁；`browser_register.py` 始终由创建 Playwright 的 owner 线程处理刷新、Profile checkpoint 和关闭命令，API 线程只投递命令，避免跨线程操作 BrowserContext。普通注册、失效测活和补抓 Auth 继续透传原停止回调并使用既有容量合同。
- **批量登录态任务改为“保持占位，释放后补位”（v2.17.0）**：批任务最多并行保持当前实例容量允许的浏览器，逐个释放后才启动后续账号；“停止新增浏览器”只关闭调度门，不中断已保持的账号，“停止并释放全部”先停止补位再保存、关闭现有浏览器。浏览器在登录成功后异常关闭会以 `browser_lease_interrupted` 收口，已成功写回的凭据继续保留且不会切换代理重做登录。
- **任务日志检查点改为异步合并写入（v2.15.4）**：`api/tasks.py` 将运行中任务每 `200` 条或 `10s` 触发的完整日志快照从业务 worker 的同步 SQLite 写入改为单一 daemon writer；同一任务只保留最新待写快照，锁冲突采用有界重试并在重试前吸收更新状态。任务 worker 只负责内存日志追加和快照入队，不再因 `task_logs` 写锁等待阻塞账号处理；终态写入成功后清理冗余 pending，终态写入失败时保留检查点兜底，已有终态合并规则继续阻止晚到 running 快照回退状态或计数。
- **账号派生筛选请求去除无条件写事务（v2.15.4）**：`services/account_filters.py::refresh_stale_account_list_state()` 不再在每次派生筛选 GET 后执行全表孤儿行 `DELETE`。账号单删、批删和按筛选删除仍通过 `delete_account_list_state_for_account_ids()` 在原事务内精确清理；支付长链、登录态短链及缓存复用写回则在账号事务内同步刷新对应 `account_list_state`，fresh-cache 列表请求在其他 SQLite writer 存在时保持只读并可立即返回。
- **批量本地状态刷新增加订阅结果分布（v2.15.3）**：`api/tasks.py` 在每个已落库的刷新结果后按当前计划累计 Plus、Free、Unknown，持续写入任务 `meta.subscription_counts`，最终摘要、实时任务弹窗及历史任务详情均展示“刷新后订阅分布”。认证失效等已成功确认的业务结果计入 Unknown；网络异常等没有落库的结果不拿旧订阅冒充本次分布。账号页在任务终态同时重新拉取账号列表和固定组合统计，避免刷新完成后组合浮窗仍显示旧数量。
- **将 `create_account=2xx` 提升为不可逆开户边界（v2.15.2）**：`services/chatgpt_core/any_auto/browser_register.py` 在 about-you 开户成功后不再依赖后续 SPA URL、DOM 或 OAuth 回调是否及时推进来决定注册成败。开户后的导航超时、Auth `/error`、页面回落及未知状态会形成结构化 post-signup partial state，并在同一浏览器上下文进入一次 existing-account 登录恢复；首次 Session 抓取异常也不会抹掉已确认的开户事实。恢复后仍缺 AccessToken/Session/Cookie 时，`services/chatgpt_core/any_auto/transport.py`、`access_token_only_registration_engine.py` 与 `chatgpt_registration_mode_adapter.py` 会保存 `registered_auth_pending + session_capture_pending` 账号、原邮箱/密码、可用 Cookie 快照及精确待补抓原因，沿用现有账号页“补抓 Auth”流程继续恢复，禁止对同邮箱重新 signup，并跳过 checkout 探测和外部上传。
- **诊断保留策略按失败取证与磁盘安全收敛（v2.15.0）**：智能模式保留全部未受容量压力清理的失败样本及每任务最近 `3` 个成功对照样本，固定制品不参与自动淘汰；默认限制为全实例 `8 GiB`、单任务 `2 GiB`、单尝试 `150 MiB`、结构化响应 `20 MiB`、磁盘保留空间 `20 GiB` 和普通制品 `72h`。收口采用 `.partial` 到最终目录的原子重命名，支持重试恢复、`finalize_failed` 残包下载、陈旧采集/孤儿目录/索引墓碑清理；视频、DOM、HAR 和 Trace 按诊断价值降级删除，始终优先保留 `diagnosis.json`。Camoufox 当前不支持 Playwright 原生 `Browser.setScreencastOptions` 时，全量模式会缓存该运行时能力并立即重建无视频 Context，保证 Trace/HAR 不随可选视频一起丢失，同时在 warning 与 `diagnosis.capture.video_unavailable_reason` 留下明确证据。采集初始化、监听器、Trace 停止、HAR flush、索引更新和清理失败均只记录诊断警告，不覆盖实际注册成功/失败结果。
- **纯前端热发布不再中断运行任务（v2.14.3）**：`deploy.sh` 为 `--mode=hot` 增加显式 `--frontend-only` 门禁，发布仍会完成 Git 归档、宿主机前端构建、`auto-gpt:latest` 规范镜像构建、三个常驻实例静态目录的原子替换及完整 health/index smoke，但不会复制后端源码或重启容器进程。该模式只允许 `frontend/`、`changelog.md` 和发布脚本自身发生变化，混入任何后端源码会直接拒绝发布；适用于账号页等纯静态修复，可避免 Plus 正在执行长注册批次时因前端上线丢失内存任务。常规 multi 和现有 hot 后端发布语义保持不变。
- **any-auto 浏览器注册改为业务响应驱动的有界状态推进（v2.14.1）**：`services/chatgpt_core/any_auto/browser_register.py` 不再为密码、OTP、about-you 各维护一套只看 URL/DOM 的旧提交轮询，而是复用 `services/chatgpt_core/browser_registration.py` 已有回归覆盖的浏览器事务实现，统一观察 `user/register`、`email-otp/validate` 与 `create_account` 的真实响应、表单业务请求是否发出及 Sentinel/Cloudflare 前置活动。邮箱提交保留实例设置的基础等待时间，业务响应到达后可在总计最多 `75s` 的硬边界内重新获得状态推进窗口；点击后确实没有业务请求时只执行一次同表单 `requestSubmit` 和一次可信 Enter，不会在已经发出请求时重放注册动作。
- **Plus 注册容量提升到 10 并发，Solver 改为按需 `0-5`（v2.14.0）**：`api/tasks.py` 将 ChatGPT 注册总硬上限与 headed/headless 浏览器模式硬上限提升到 `10`，旧实例未配置时仍保持浏览器任务默认/上限 `2/2`；Plus 实例上线配置固定为浏览器任务 `10/10`、启动延时 `0-0s`，共享 Auth/注册浏览器上限 `10`。`docker-compose.multi.yml` 将 Plus `pids_limit` 从 `1536` 提升到 `3072`，保留 `/dev/shm=2GiB`，使用 `4s` 启动错峰、`220` 单次 PID 预算、`256` PID 应急保留、`6144MiB` 宿主机内存保留和 CPU PSI `avg10=20%` 暂停阈值。`services/turnstile_solver/api_solver.py` 与 `solver_manager.py` 保持 Solver HTTP 服务常驻，但启动时不再预热浏览器；请求到达后从 `0` 动态扩到最多 `5` 个，空闲 `300s` 后回收到 `0`，Solver 浏览器池与注册/Auth 容量继续分开计数。
- **10 路注册调度改为按需准备资源（v2.14.0）**：`services/chatgpt_core/sentinel_browser.py` 为注册浏览器增加高优先级等待队列，存在注册等待时失效测活和 Auth 补抓不会抢占新释放的浏览器容量；每次真实 Camoufox 启动在错峰等待后重新读取 cgroup PID、cgroup 内存、宿主机 `MemAvailable` 和 CPU PSI，避免多个 worker 先领完槽位后绕过资源检查。`api/tasks.py` 的动态代理候选从每 worker 预生成最多 6 个改为按失败预算一次生成 1 个，10 路任务不再启动前集中产生最多 60 次候选探测；注册后本地状态刷新延后到整批注册 worker 退出后再调度，避免与仍在运行的注册浏览器竞争资源。
- **上次订阅增加本地状态刷新时间（v2.13.4）**：`frontend/src/pages/Accounts.tsx` 在当前订阅不可确认、继续展示“上次 Free/Plus”等历史订阅时，新增一行真实本地探测时间，直接读取列表 API 已有的 `chatgptLocal.subscription.checked_at` 并按浏览器本地时区显示为 `MM-DD HH:mm`；桌面表格与移动端状态区保持一致，悬浮说明同时提供完整本地时间。“上次订阅”和时间统一使用主题的高对比次级正文色与 `12px` 字号，保证暗色/亮色表格中可扫描。缺少探测时间的旧记录保持空白，不使用账号 `updated_at` 冒充刷新时间；侧栏可见版本同步为 `v2.13.4`。
- **组合栏操作图标移到左侧标题并提升辨识度（v2.13.1）**：`frontend/src/features/accounts/components/FilterPresetBar.tsx` 将条件筛选组合的设置菜单和固定账号组合的新建按钮分别移动到“条件筛选组合”“固定账号组合”标题右侧，保留原有保存、管理、刷新菜单以及新建固定组的启用条件和提示；`frontend/src/index.css` 移除两行末尾的独立操作列，将齿轮与加号放大到 `17px`、按钮点击框调整为 `28px`，桌面与窄屏均保持操作入口紧邻所属标题，选择器和置顶快捷项继续占用后续空间。
- **账号页组合栏改为两行名称选择（v2.13.0）**：`frontend/src/features/accounts/components/FilterPresetBar.tsx`、`frontend/src/pages/Accounts.tsx` 与 `frontend/src/index.css` 分别显示“条件筛选组合”和“固定账号组合”；二级只提供“未固定”或当前父级下的固定组，快捷项与下拉项只显示自定义名称，不追加数量、“按条件”或“内置”。选中态同时使用按钮状态、勾选图标与 `aria-pressed`，移动端改为稳定的两行全宽布局；新建固定组只在已选一级、“未固定”范围且已勾选账号时开放，旧组合迁移入口会先关闭管理弹窗再打开迁移弹窗，避免双 Modal 层级互相遮挡。
- **查看范围、临时勾选与批量任务彻底解耦（v2.13.0）**：切换一级或二级组合会清空表格临时勾选，但选择固定组不再自动全选成员；跨页勾选继续只代表本次明确 ID 操作。`useAccountsQuery.ts` 和统一批任务请求增加 `primary_preset_id / secondary_scope / fixed_group_id / fixed_group_revision`，列表、OAIPay/Sub2API、手机号绑定、失效测活、支付链接和 PIX 导出共用同一范围解析；固定组状态变化只影响临时表格条件，不改变持久归属，成员 revision 仅在成员实际新增、移动、移除或身份重绑定时递增。
- **按现场要求整体回退运行版本至 v2.12.5**：将运行代码、测试合同和侧栏版本恢复到提交 `d3418b418e14e50d868eeea9f9a688493cae5982`，撤销 `v2.12.6` 的 HME MIME 可见正文取码与路由数字隔离，以及 `v2.12.7` 的 OTP `403` 停用原因保真、`account_deactivated` 账号终态同步和手机号资源复用边界；此前已撤销的 `v2.12.8` OTP 成功响应事件轮询继续不启用。`v2.12.5` 的 OAIPay Plus/Pro 仅 AT 无 RT 私有门禁及 `PLUS--未接码` 自动分类完整保留。此次仅回退代码与静态资源，不反向改写主服务、Plus、Plus2 的账号数据库、任务历史、实例 `.env` 或共享配置；历史任务和已经落库的账号状态保持原样。
- **按现场要求回退运行版本至 v2.12.7**：撤销 `v2.12.8` 对失效测活 OTP 提交等待的 45 秒 Playwright 事件轮询、截止事件排空、输入时自动提交兼容及对应新增测试，恢复 `c428b5a` 发布前的 `v2.12.7` 代码与侧栏版本。`v2.12.7` 已有的 OTP `403` 结构化响应保真、`account_deactivated` 明确分类、手机号绑定停用账号状态同步和三实例代理/并发合同继续保留；本次只回退代码与静态资源，不反向改写主服务、Plus、Plus2 的账号数据库、任务历史、实例 `.env` 或共享配置，回退前已经成功复活的账号状态保持原样。
- **根据现场测活结果再次回退至 v2.12.1**：`v2.12.2` 虽仅包含 OAIPay Plus 仅 AT 上传补丁，但操作方确认其线上失效测活不可用、`v2.12.1` 可用，因此本次反向撤销定向恢复提交 `7843de1fc3edb122bc807c196b2b44911ce1b689`，将全部代码和前端重新恢复到 `043ef6ba84e1088dfec2e0bb3585230c92cb152f` 的 `v2.12.1` 状态。账号数据库、任务日志、实例 `.env` 与 `shared_config` 不迁移、不改写；OAIPay 再次恢复要求 RefreshToken，失效测活继续使用 v2.12.1 的 OTP 和浏览器状态机行为。
- **运行版本整体回退至 v2.12.1**：按操作方要求将源码与前端恢复到提交 `5e65526d9ed5f89fad5bf6a8c093746cc838b9c3` 的完整行为基线，并使用新的反向提交保留后续版本历史，不改动主服务、Plus、Plus2 的账号数据库、任务日志、实例 `.env` 或 `shared_config`。本次完整回退同时撤销 v2.12.2 的 OAIPay Plus 仅 AT 上传例外、v2.12.3 的 OTP validate 响应监听与 403 结构化分类、v2.12.4 的动态代理候选探测停止检查；因此 OAIPay 恢复要求 RefreshToken，OTP 403 恢复显示旧的“提交后未跳转”，动态代理准备阶段的立即停止也恢复 v2.12.1 行为。侧栏可见版本恢复为 `v2.12.1`。
- **Plus 注册浏览器扩容到五槽并增加启动保护（v2.12.1）**：`docker-compose.multi.yml` 将 `auto-gpt-plus` 的进程级 `AUTH_BROWSER_MAX_CONCURRENCY` 从 `3` 提高到 `5`，`pids_limit` 从 `768` 提高到 `1536`，保持 `/dev/shm=2gb`，并把当前 ChatGPT 注册链未使用的独立 Solver 从 `6/1` 收敛为 `1/1`；单个浏览器注册任务自身的有效并发上限仍为 `2`，主实例与 Plus2 继续使用 Auth `2`、Solver `4/1`、PID `768`。`services/chatgpt_core/sentinel_browser.py` 新增 cgroup PID 余量门控与进程级启动错峰：Plus 仅在 `pids.current + 220 <= pids.max` 时领取实际浏览器槽，相邻 Camoufox/Auth 浏览器至少间隔 `4s` 启动；PID 不足会释放 semaphore、按 `browser_slot=waiting reason=pids` 等待重试，停止或异常仍成对释放容量。主实例与 Plus2 的 PID 余量和启动间隔默认均为 `0`，保持原行为。
- **置顶筛选组合不再限制显示数量（v2.12.1）**：`frontend/src/pages/Accounts.tsx` 移除桌面最多 `8` 个、移动端最多 `4` 个置顶组合的硬截断，所有 `pinned=true` 组合都会交给既有 `FilterPresetBar` 渲染，并继续使用单行横向滚动承载超出宽度的内容；后端组合数量、账号列表排序、分页和每页数量均未调整。`frontend/src/app/AppShell.tsx` 同步可见版本为 `v2.12.1`。
- **取消手机号绑定与失效测活的固定并发 5 上限（v2.12.0）**：`api/tasks.py` 和 `frontend/src/pages/Accounts.tsx` 删除手机号绑定的 `PHONE_BINDING_MAX_CONCURRENCY=5`、提交端 `Math.min(5, ...)` 与输入框 `max=5`，新批量失效测活同样不设置固定 worker 数上限；两类任务的实际并发只按本次可执行账号数收敛，手机号绑定还会按可用号码数收敛。`reuse_phone_until_unusable` 的同号连续绑定仍强制串行，实例级共享浏览器容量仍负责真实浏览器资源排队，避免把取消表单/任务硬截断误解为绕过身份互斥或运行资源门禁。
- **本地状态刷新改为设置驱动的进程级并发（v2.11.1）**：`services/chatgpt_core/local_status_refresh.py` 新增可动态调整、FIFO 补位的进程级容量闸门，`api/tasks.py` 的手工批量任务、注册后自动刷新和旧同步调用现在共享 `chatgpt_local_status_probe_concurrency` 设置的 `1-10` 个真实槽位；多个重叠任务的合计在途账号不会超过该值，每完成一个立即滚动补下一个，不增加分批屏障。任务启动时重读当前设置，设置保存后也会立即调整当前进程闸门；`api/config.py` 用同一配置更新锁串行化数据库提交与 limiter 应用，避免旧读取或并发保存把内存上限反向覆盖。任务 meta/API 补充 `global_concurrency_limit`，避免把单任务 worker 数误当全局容量。进程级账号+指纹 keyed gate 同时防止重叠任务对同一身份并发探测和竞争写回；原有单任务请求并发上限、指纹复用和部分失败仍以 `done + errors` 收口的合同保持不变。
- **动态代理 SID 改为每账号惰性准备（v2.11.1）**：`services/chatgpt_core/local_status_proxy.py` 统一批量任务、`/subscription`、`/probe-local`、旧 action、平台插件及注册后自动刷新使用的代理候选策略；Dynamic 模式不再按 `dynamic_proxy_max_attempts` 为每个账号预建整组候选，而是账号真正取得并发槽位后只生成 `1` 个主 SID。只有候选准备失败，或错误明确命中连接、超时、TLS、SOCKS、代理隧道、代理传输故障及 HTTP 407 时才按预算补位；非独立出口模式优先复用同任务其他账号已验证健康的 SID，无可复用候选时才生成新 SID。`HTTP/1.1 429`、`HTTP/2 429`、`status_code=429`、`http_code=429`、HTTP 403、Token 失效、账号停用、订阅/配额等上游业务状态不会切换 SID；结构化代理传输失败在候选耗尽后直接失败，不会覆盖账号已有的 canonical `chatgpt_local`。显式开启的独立出口 IP 校验仍保留真实出口碰撞检测，缺失配置的默认值调整为关闭，已有显式 `true/false` 原样兼容。
- **按执行器冻结 ChatGPT 注册有效并发（v2.11.0）**：`api/tasks.py` 与 `api/config.py` 将 protocol 缺省并发设为 `2`、后端硬上限设为 `3`，headed/headless 缺省与硬上限均为 `2`；手机号注册和 `manual_email_otp` 继续固定串行。任务入队前与 `_run_register()` 启动前执行两层收敛，配置只能下调、不能突破模式硬上限；任务元数据和控制日志同时记录 requested/effective concurrency、截断原因和延迟来源，历史显式 `4/5` 会被截断，非 ChatGPT 注册保持旧默认 `1` 与上限 `5`。
- **注册启动改为默认 `15-30s` 随机抖动（v2.11.0）**：ChatGPT 请求未显式提供延迟时，首个账号立即启动，后续账号通过单调时钟和共享启动门按 `random.uniform(15, 30)` 错开；显式 `0/0` 继续关闭，旧客户端只提交最小值以及 `min>0,max=0` 时继续按固定延迟执行。`frontend/src/lib/chatgptRegisterTaskControls.ts` 统一两个注册页面与账号页弹窗的默认值和模式上限，`register_delay_max_seconds` 现在完整参与 hydrate、保存、切换执行器归一化和 `/tasks/register` payload，不再只显示却不提交。
- **动态代理增加同进程跨任务出口租约（v2.11.0）**：新增 `core/chatgpt_register_exit_ip_registry.py`，动态代理缺省 `auto` 启用真实出口探测和原子领取，IPv4 按地址、IPv6 按 `/64` 去重；attempt 执行期间持有默认 `1800s` active lease 并自动续租，结束后进入 `900s` 冷却。`api/tasks.py` 在入队时按实际 resolver 别名冻结代理模式、地址、国家和 failover，动态独立出口候选默认固定为 `6`、可配置范围限制为 `1-12`，任务预检只生成 `1` 个候选；刚生成的首候选直接复用已有出口探测，失败切换后的候选强制重新探测，避免旧 IP 结果误领租约，同时消除候选数随注册数量形成的二次方级串行探测。显式 `true/false|required/off` 继续兼容，直连和不可轮换的单代理不会被缺省策略误拦；registry 明确只覆盖单 Python 进程/容器，不把瞬态锁写入 `shared_config.db`，三个容器之间仍需独立运行态协调才能全局去重。
- **按当前宿主机资源调整 Plus 浏览器容量（v2.11.0）**：`docker-compose.multi.yml` 为主实例、Plus、Plus2 分别固定独立 Solver `4/1`、`6/1`、`4/1` 的 max/warm 容量，仅将主用 `auto-gpt-plus` 的 `shm_size` 从 `1gb` 提高到 `2gb`；主实例与 Plus2 保持 `1gb`，Auth Browser 保持 `2/3/2`，三个实例继续使用 `pids_limit=768` 且不新增应用 `mem_limit`。独立 Solver 当前不在 ChatGPT 注册调用链，Plus 的 `6/1` 是容量预留，不宣称解决当前注册 Turnstile 排队；侧栏版本同步为 `v2.11.0`。
- **ChatGPT HME 恢复原地址 + 单一平台 Tag 组合（v2.10.3）**：`core/base_mailbox.py` 将正常 ChatGPT HME Ready prepare 从严格 `address_mode=base` 改为 `address_mode=platform_default`，由已先行发布的 Helper 按同一物理 HME 最多分配 `base 1 + 一个 +gpt[a-z0-9]{3} Tag = 2` 个身份；不能用两个 Tag 替代原地址加 Tag，early failure 继续复用同一 registration/Tag，历史账号、旧 `+gpt1..4` 和既有 lease 不迁移、不改写。Tag 长度实验仍显式使用隔离的 `random_tag` 模式，其他平台继续遵循 Helper 的通用 `base 1 + random 4 = 5` 组合和四个跨平台 random slot 物理容量。`effective_address_mode=base|random_tag` 现在贯穿 Helper 响应解析、账号邮箱状态白名单和恢复服务，调用方不再从邮箱文本反推实际形态；侧栏版本同步为 `v2.10.3`。
- **ChatGPT HME 注册改用物理原地址（v2.10.2）**：`core/base_mailbox.py` 在非 Tag 实验且 Helper consumer 为 `auto-gpt/chatgpt_register` 时，为 HME Ready prepare 显式发送 `address_mode=base`；新注册由 Helper 领取物理 HME 对应的 canonical base logical address，不再默认使用 `+gpt...` random tag。auto-gpt 只声明地址策略，不重复裁决 Helper 返回结构；Helper 先行发布并作为 `physical -> logical:base -> platform registration -> lease` 身份链、单物理 HME 活跃租约、ChatGPT 额度和幂等语义的权威端。既有 tag 账号、历史 registration/lease 和其它 consumer 不迁移、不改写，验证码仍由 auto-gpt 按返回的转发目标直接读取 TempMail。Helper 回传的 `address_mode` 会随账号邮箱状态持久化，侧栏版本同步为 `v2.10.2`。
- **注册浏览器统一进入共享容量队列（v2.10.1）**：`services/chatgpt_core/sentinel_browser.py` 抽出可复用的进程级浏览器容量租约，`services/chatgpt_core/any_auto/browser_register.py` 在启动整段 Camoufox 注册前获取同一租约；注册任务仍可用多个 worker 并行处理邮箱、代理和结果，但真正的 ChatGPT 浏览器上下文会和手机号绑定、Sentinel/Auth 浏览器共用容量并在不足时输出 `browser_slot=waiting reason=capacity|memory` 后排队，不再让注册并发数直接等于完整浏览器进程数。`AUTH_BROWSER_MAX_CONCURRENCY` 上限改为可配置，生产默认主实例 2、Plus 3、Plus2 2，侧栏可见版本同步为 `v2.10.1`。
- **三个业务实例取消 Docker 内存上限**：`docker-compose.multi.yml` 移除 `auto-gpt`、`auto-gpt-plus`、`auto-plus2` 的 `mem_limit`、`mem_reservation`、`memswap_limit` 与 `mem_swappiness`，浏览器和 Python 进程可直接使用宿主机物理内存与系统 Swap；独立 `phone-api-relay` 继续保留 256 MiB 隔离。三个业务实例仍保留并提高为 `pids_limit=768` 的有限进程护栏，避免取消内存 cgroup 后把“可用宿主机内存”误解为无限浏览器并发。
- **浏览器容量测试改用稳定日志合同**：`tests/test_sentinel_browser.py` 新增 any-auto 任意浏览器工作与 Sentinel 共用同一 semaphore 的并发测试，并将旧中文文案断言改为 `browser_slot=waiting` + `reason=memory` 结构化字段，避免展示文字变化掩盖真实门禁行为。
- **隔离本地短链与 long-link 生成变体**：支付链接 variant key 现在包含来源、输出格式和本地代理维度，`payment_link_cache_for_params()` 对旧 key 做只读兼容扫描；任务 guard 同时按 source/format 区分当前链接、成功历史和运行中记录。已有 long-link 不会阻止同账号生成登录态短链，已有短链也不会阻止后续生成 long-link；历史 Plus/Team 缓存无需数据库迁移即可继续读取。批量短链使用本地串行 runner，每次请求前后核对账号邮箱、创建时间和身份摘要，避免账号删除或 SQLite ID 复用后串写 Checkout 结果。侧栏可见版本同步为 `v2.10.0`。
- **移除旧 iCloud HME 活动面**：`main.py` 不再挂载 `/api/icloud-hme/*`，也不再启动旧自动补池/自动删除 worker；`api/config.py`、`api/system.py` 与 `frontend/src/pages/Settings.tsx` 删除 Apple Cookie、iCloud 域、全局 `icloud_forward_mailbox_id` 及旧补池/删除配置，只保留 HME Ready API + TempMail 配置。历史模块仍作为只读迁移兼容代码保留，不能通过活动路由或配置重新启用。
- **保留账号级转发目标并刷新物理 mailbox 缓存**：全局转发地址只作无账号路由时的 fallback，已有账号的 `forward_to` 与 `forward_mailbox_id` 不再被全局值覆盖；`core/base_mailbox.py` 在 TempMail 列表/详情返回 404 时按账号转发地址重新查找邮箱并回写新的 mailbox ID，继续沿用原 `before_ids`、`otp_sent_at` 与验证码排除边界。
- **手机号绑定失败也持久化邮箱重绑结果**：`services/chatgpt_core/subscription_auth_capture.py` 在 Auth/手机号流程失败或中断时仅更新 `chatgpt_mailbox_state`，不会改账号 `status`、token、`used`、订阅或手机号绑定字段；重试使用同一轮刚刷新的 mailbox state，避免旧 ID 循环失败。
- **隔离旧 alias 重跑同步**：`api/tasks.py` 与 `services/chatgpt_core/plugin.py` 只有带明确 `legacy-icloud-hme` anonymous identity 的历史状态才写旧 alias 表，当前 Helper lease 不再被误当成 anonymous ID。
- **固定组合使用稳定账号身份并保持单入口交互**：固定成员除数字 ID 外同步保存规范化邮箱和创建时间，加载时必须三者匹配，避免 SQLite 删除账号后复用主键而把新账号错误加入旧组合；不存在、跨平台或身份不匹配的成员会从本次范围剔除并在页面提示，全部成员失效时返回空列表而不是回退成全量账号。历史筛选组合没有 `mode` 字段时继续按动态组合迁移，内置组合和三个实例之间的本地隔离语义不变。`frontend/src/pages/Accounts.tsx`、`FilterPresetBar.tsx` 与 `useAccountsQuery.ts` 继续只保留原“筛选组合”选择器和管理弹窗，通过分段控件区分“筛选条件 / 固定账号”，没有新增第二套页面或工具栏入口。侧栏可见版本同步为 `v2.9.0`。
- **收口已退役的 GoPay / 流水线入口**：`main.py` 继续保持不挂载 `api.integrations`、`api.pipeline` 与 `api.idea_oaipay_pipeline`，`api/system.py` 不再展示自动流水线健康资源，`frontend/src/app/AppShell.tsx` / `router.tsx` 也移除了对应导航与路由；`frontend/src/pages/Settings.tsx` 同步删去 GoPay 分组标题与本地模式提示中的 GoPay 文案，避免新界面继续暴露已退役入口。
- **停用 GoPay 作为支付链接分类**：`api/tasks.py`、`services/account_filters.py` 与 `services/chatgpt_core/pix_payment_link_cleanup.py` 不再把 `gopay` 当作独立支付类型；过滤、清理与清单统计现在会把旧的 `gopay` payload 归入 `other`，而不是继续向前端和任务契约暴露一个新的分类。
- **统一任务历史展示契约**：`api/tasks.py` 的任务历史列表与详情现在统一输出 `success / skipped / failed / interrupted / total / stats_available`，并从旧快照的 `registered_accounts`、`auth_pending_accounts`、`runtime_results`、Idea 提交摘要、错误列表及 `[SUMMARY]`/结果日志恢复统计；`frontend/src/lib/taskTypes.ts`、`TaskHistory.tsx` 与 `TaskDetailHeader.tsx` 对零值统计、状态别名和中断数量使用同一套推导规则，无法从旧数据可靠恢复时明确显示“统计暂不可用”，不再留空或伪造零值结论。
- **收敛任务类型中文显示**：任务历史补齐本地状态同步、HME 复测、支付链接清理、浏览器认证、手动轮询、代理池测试及历史 `codex_*` / `register_*` 等来源映射；未知英文内部来源统一展示为“其他任务”，桌面端仅在 tooltip 保留内部 `source` 供排障，避免实现键直接暴露给操作员。
- **收敛 Idea 订单轮询生命周期**：`main.py` 不再在服务启动时自动恢复旧的 BaxiGPT/Idea 订单，`services/chatgpt_core/baxigpt_status_poller.py` 关闭账号级常驻补偿扫描；显式卡密轮询接口仍保留。这样本地 Idea 任务因停止、异常或服务重启结束后，不会继续产生隐藏的上游状态请求。
- **持久化本地任务停止语义**：`api/tasks.py` 在 Idea 提交任务收尾阶段调用轮询停止逻辑，将同一任务遗留的未终态账号订单写为 `stopped` 并保存 `polling_disabled`、停止时间和原因；账号列表状态、提交摘要和前端筛选同步支持“已停止”，避免旧 `processing` 记录在后续调用中重新入队。

- **重构手机号绑定取号模式与固定快照契约**：`frontend/src/pages/Accounts.tsx` 与 `api/tasks.py` 将手机号绑定统一为 `普通绑定`、`限定号段绑定`、`号段抽样测试`、`不可用号码全量复测` 四种 canonical `phone_pool_mode`。限定号段新增 `可用号码 / 不可用号码 / 全部号码` 的行级筛选，号段只作为范围，不再把前缀健康状态当成禁选条件；不可用/全部筛选和全池不可用复测在创建任务时冻结候选，关闭同号复用与手工粘贴入口，候选预览同时展示候选号码、账号数、预计测试数和未覆盖号码。普通可用号段绑定继续沿用原有动态分配和容量校验，号段抽样仍严格限制每段 1/2 个号码。
- **统一固定快照任务的审计元数据**：`api/tasks.py` 为 `prefix_bind`、`unavailable_numbers` 和通用 `phone_selection` 写入号码筛选、候选数、预计测试数、未覆盖候选和是否固定快照；旧的 `prefix_bind_enabled` / `prefix_sample_enabled` 布尔请求继续兼容，新增的 `phone_number_filter`、`prefix_number_filter`、`unavailable_number_test_enabled` 可与 canonical mode 并存。
- **增加 HME Tag 长度对照测试的任务级传递能力**：`core/base_mailbox.py` 为 Helper Ready prepare 增加显式测试字段透传，但仅在请求携带测试模式时发送 `test_run_id`、指定物理 HME、Tag 和 Tag scheme；普通注册不携带测试覆盖字段，实际地址策略由当前 HME Ready consumer 合同决定。
- **统一测试文档入口**：`README.md`、`AGENTS.md` 与 `docs/docker-image-release.md` 现在统一指向 Docker 测试规范，移除会吞掉收集失败的 `pytest tests -q || true` 作为推荐门禁，明确当前 `requirements-test.txt`、`docker-compose.test.yml` 和测试脚本仍待落地。

### 修复 (Fixed)
- **资格后处理故障不再反向污染注册结果（v2.21.1）**：注册入队和直接执行入口都通过安全配置快照收敛支付资格代理配置异常，错误只写为账号 `probe_failed / configuration_error` 和注册任务资格汇总；探测异常、线程提交失败、无效返回状态、任务 meta 更新失败及日志回调失败同样在协调器内部关闭式收口。账号已经保存后，这些资格链技术故障不会增加注册 `errors`、删除账号或把成功注册改成失败；代理预检导致注册主体提前失败时，也会先关闭资格协调器再持久化终态快照，避免终态回调早于资格 meta 更新而丢失汇总。
- **在任务创建阶段关闭式校验动态代理配置（v2.21.0）**：配置 API 对 provider、Pool、网关、协议、超时和凭据格式返回明确 `400`，允许 Cliproxy 启用时分别暂存 MiyaIP 凭据，但切换到 MiyaIP 前必须已有完整 `Crc/KeyName`。注册与各类任务在入队前冻结并验证当前渠道运行参数；批量本地状态同步修复显式旧 `dynamic` 错误继承全局 MiyaIP 的边界，MiyaIP Generate 返回重复线路时继续消耗当前渠道刷新预算，不再提前停止切换。
- **将 Promotion `403` 按业务资格而非技术故障处理（v2.19.5）**：`v2.19.4` 发布后的真实复测发现，将 Promotion 改走 US 虽能得到 `200`，但会绕过 VN 优惠资格门禁，使昨晚已确认 `0 PHP` 的样本返回全价 `110000 PHP`，产生假阴性，因此恢复支付资格原有的 `US -> VN -> US` 地区链。`services/chatgpt_core/payment_eligibility.py` 现在保留上游 HTTP 状态、阶段和脱敏 JSON detail；仅当 0 元任务在 Promotion 刷新阶段收到精确的 `403 + This promotion is not available.` 时，直接确认 `ineligible / promotion_unavailable` 并停止无意义重试，不再把明确业务拒绝计为 `probe_failed`。其他 Promotion `403`、Checkout/Taxes `403`、Cloudflare/鉴权/代理错误仍为技术失败，GCash 检测也不会误用 0 元业务映射；专项回归覆盖精确分类、单次终止、泛化 403 保持失败、GCash 隔离和确认态地区证据，侧栏版本同步为 `v2.19.5`。
- **修复 0 元试用资格检测统一卡在 Promotion `403`（v2.19.4）**：现场任务 `task_1786498679495_fd92d07d` 的 32 个实际执行账号全部为 `probe_failed`，其中 26 个在 `/backend-api/payments/checkout/update` 返回 `403`、6 个为动态代理出口不可用；同账号、同请求体的受控对照确认 VN 出口稳定返回 `This promotion is not available.`，US 出口在复用或新建 HTTP Session 时均返回 `200`，排除 AT、Cookie、Cloudflare 和请求字段问题。`services/chatgpt_core/payment_eligibility.py` 将 0 元检测从已经失效的 `US -> VN -> US` 调整为 `US -> US -> US`，GCash 支付方式检测继续保留独立的 `US -> VN -> US`，结果证据与 `api/tasks.py` 的账号确认态分别记录真实地区链；上游 JSON 业务错误现在会在脱敏任务日志中附带安全 detail，不再只显示裸 `HTTP 403`。专项回归锁定两个任务的路由互不串用、确认态 profile 与错误详情，侧栏版本同步为 `v2.19.4`。
- **修复北京时间日志改造后 Turnstile Solver 直接入口无法启动（v2.19.3）**：`services/turnstile_solver/start.py` 在加载 `api_solver.py` 前同时注册 Solver 目录与项目根目录，保证由 `services/solver_manager.py` 直接执行脚本时可以解析 `core.timezone`；修复三实例发布后 Solver 因 `ModuleNotFoundError: No module named 'core'` 退出、容器内 `8889` 未监听的问题。修改不改变 Solver 浏览器池、并发、端口或代理语义，侧栏版本同步为 `v2.19.3`。
- **修复任务历史和日志相对北京时间慢 8 小时（v2.19.2）**：SQLite 会丢弃 SQLModel aware datetime 的 `tzinfo`，旧任务 `created_at` 因而以无偏移 UTC 字符串返回，浏览器此前又按本地时间直接解释，导致北京时间环境也显示为 UTC 数字；现在任务历史序列化先按 UTC 还原旧行再输出 `+08:00`，新任务日志行直接使用北京时间，任务结果中的旧无时区 `finished_at / bound_at / registered_at` 改为显式偏移 ISO。历史日志正文只有 `HH:mm:ss`、没有日期和时区，保持原始证据不批量改库；新日志从本版本开始准确标记北京时间。
- **固定组合订阅统计悬浮窗改为不透明实色（v2.18.4）**：`frontend/src/features/accounts/components/FilterPresetBar.tsx` 为固定组合下拉项、当前选中项和置顶快捷按钮的统计 Tooltip 设置独立实色背景与箭头，不再继承主题中带透明度的 `colorBgSpotlight` 而透出下层文字；`SubscriptionStatusCounts.tsx` 在固定组合场景紧凑显示 `f / p / u / w`，组合管理列表常驻显示四个全称状态，普通本地状态刷新任务仍保留既有三项统计合同。无网络、源码只读、临时 SQLite/runtime 的一次性容器定向回归 `8 passed`，前端 Node 合同 `56 passed` 且 TypeScript/Vite 生产构建通过；侧栏可见版本同步为 `v2.18.4`。
- **修复注册、手机号绑定、补抓 Auth 与失效测活后订阅长期停留“待刷新”（v2.18.3）**：`services/chatgpt_core/local_status_refresh.py` 对同一认证材料版本执行跨轮次证据合并，新的 `unknown_plan / probe_failed / probe_incomplete` 只记录刷新尝试和错误，不再覆盖已确认的 Free、Plus、Pro、Team 或 Enterprise 本地快照；认证明确失效仍正常落库并触发原有失效策略。新增无凭据、无代理密钥的 `chatgpt_local_status_refresh_jobs` 持久队列，由 `core/db.py` 创建索引和账号删除触发器，自动刷新采用最多三轮有界退避，进程退出后由 `main.py` 启动恢复器续跑；升级启动时会把旧版“当前 Unknown + 历史套餐存在”的有效账号纳入一次性持久刷新，避免只修新任务而遗留旧数据。
- **认证成功链路复用实际出口并收敛重复刷新（v2.18.3）**：`subscription_auth_capture.py`、`invalid_account_recheck.py`、`custom_email_recheck.py`、`web_session_login.py` 与注册任务将实际登录/注册成功代理传给首轮订阅刷新，重复请求按账号和认证版本合并时保留显式成功代理及更早执行时间；注册保存与任务收尾继续互为兜底，但已成功的同版本刷新在短窗口内不会再次发起网络探测。认证材料替换统一清除旧凭据绑定的 401 快照，同时仅保存脱敏的历史套餐证据；`PATCH /api/accounts/{id}` 同步更新 `accounts.token` 与 `extra.access_token`，不再出现页面已换 AT、探测仍优先使用旧 AT 的分叉。
- **账号列表区分刷新中、刷新失败与认证失效（v2.18.3）**：`api/accounts.py` 将持久队列的 `pending / running / retry_wait / failed` 汇总为无密钥的订阅刷新状态、尝试次数和脱敏错误；`frontend/src/pages/Accounts.tsx` 对历史套餐保留行分别显示“刷新中”“刷新失败”或“不可确认”，已保留当前套餐证据时继续显示 Free/Plus 并附带刷新状态，不再把重试耗尽的失败终态伪装成无限期“待刷新”。侧栏可见版本同步为 `v2.18.3`。
- **修复支付资格任务动态代理触发 `curl(35) WRONG_VERSION_NUMBER`（v2.18.1）**：`services/chatgpt_core/payment_eligibility.py` 的 Checkout US、Promotion VN、Taxes US 三段出口改为复用 `core/proxy_utils.py::resolve_task_proxy_candidates()`，在请求前统一把供应商模板的 `socks5://` 规范为 `socks5h://`，刷新独立 SID，并按全局开关完成出口连通性和国家匹配探测；外层任务重试仍负责重新生成整条代理链，避免未经探测的原始 SOCKS URL 被直接交给 `curl_cffi` 后在 ChatGPT TLS 握手阶段失败。固定指定代理同步经过运行协议规范化，网络异常会明确记录 `checkout / promotion / taxes` 失败阶段；动态模式使用共享模板时，任务 meta 改记 `template=global`，不再误报为 `direct`。`tests/test_payment_eligibility_probe.py` 与 `tests/test_payment_eligibility_tasks.py` 增加 SOCKS 规范化、三段地区、保留时长、阶段错误和脱敏元数据回归；侧栏版本同步为 `v2.18.1`。
- **修复登录态浏览器无法显式释放及释放竞态（v2.17.0）**：人工释放现在可在等待容量、启动浏览器、密码/OTP 登录、Session 刷新或保持阶段协作式生效；登录材料尚未就绪时释放会保留原账号认证材料并将当前账号记为 `released/skipped`，已就绪时先 checkpoint Profile 再关闭。重复释放保持幂等，刷新超时会取消未执行命令，释放中的租约不会被迟到的 `authenticating / ready_holding` 状态重新激活；整个路径不发送 ChatGPT logout 请求，也不删除 AT、Session、Cookie 或保存的 Profile。
- **修复批量支付结果与任务检查点互等造成的固定 30 秒卡顿（v2.15.4）**：`api/tasks.py::_run_batch_payment_links()` 的远端结果落库现在先在一个账号事务中完成账号当前链接、`payment_link_generations` 和派生筛选状态写入并提交，提交成功后才更新任务成功数、收银台 URL、终态 request ID 和输出 `[OK]/[FAIL]` 日志。此前 `_record_remote_items()` 持有账号库写事务时调用 `_log()`，恰逢日志检查点便由另一 Session 写同库 `task_logs`，形成外层等待日志返回、内层等待外层释放写锁的自锁，逐结果耗满 `busy_timeout=30000` 并连带账号列表与本地状态刷新报 `database is locked`。同时将远端结果解析错误与本地事务持久化错误分离，后者回滚并交由任务级失败处理，不再误记成远端生成失败。
- **修复本地状态刷新把不完整探测误计为成功（v2.15.3）**：`api/tasks.py` 不再以“探测结果已写入 SQLite”作为批量刷新成功的唯一条件；当持久化结果中的 Auth 或 Codex 子探测为 `probe_failed` 时，任务现在记录具体失败账号和脱敏原因并进入失败数，同时保留已经确认的订阅分布。此前会出现任务显示“成功 35/35”，但账号列表因 `codex.state=probe_failed` 正确显示“刷新失败”的矛盾口径；认证明确失效、订阅 Unknown 等有效业务结论仍视为刷新完成，不会与网络/远端探测失败混淆。
- **消除 about-you UI 双提交覆盖开户成功的竞态（v2.15.2）**：`services/chatgpt_core/browser_registration.py` 对同一次页面 invocation 观察到的 `create_account` 响应优先选择首个 `2xx`；一旦确认开户，随后 React 事件处理器重复发出的 `409 invalid_auth_step / invalid_state` 只作为 post-commit 诊断信号记录，不能再把成功结果覆盖为失败，也不会触发第二次 API 兜底或重新提交资料。
- **OTP 后保持原 Auth 上下文进入 about-you（v2.15.2）**：any-auto 复用共享 `_ensure_about_you_page()` 的 SPA settle 与有界重试逻辑，遇到 `NS_BINDING_ABORTED` 时先确认真实 DOM/URL，再在原上下文重试 about-you 导航；不再重开 authorize 导致已验证账号回到邮箱 OTP。开户后的 callback 超时、`AuthApiFailure` 错误页和 Web Session 抓取异常均转入已有账号登录恢复，恢复失败则留下可补抓库存记录，不再产生“OpenAI 已开户、本地无任何账号”的黑洞。
- **细化开户与身份提供商诊断分类（v2.15.2）**：`services/chatgpt_core/registration_diagnostics.py` 新增 `identity_provider_mismatch`、`post_signup_auth_api_failure`、`post_signup_navigation_failed`、`post_signup_duplicate_submission`、页面回落/未知状态及 Session 抓取失败/待补抓分类；`identity_provider_mismatch` 现在优先于泛化 `about_you_failed`，便于把该地址永久退出注册候选。账号 metadata 同时保留最初 post-signup 异常与最终 Session 补抓原因，诊断包仍不向任务日志泄露密码、Cookie 或 OAuth 参数。
- **同步侧栏可见版本为 `v2.15.2`**：`frontend/src/app/AppShell.tsx` 更新版本标识，便于确认主服务、Plus 与 Plus2 已加载本次开户后恢复与 pending 持久化修复。
- **隔离注册任务之间的 HME Helper 幂等键（v2.15.1）**：`core/base_mailbox.py` 不再把每个注册任务都会从 `1` 重新计数的局部 `_task_attempt_token` 直接作为 Helper `request_id`，而是使用 `APP_INSTANCE_ID + registration task_id + attempt_id` 的 SHA-256 稳定摘要生成固定长度键，同时继续把原始父任务 ID 单独放在 `task_id` 审计字段。相同尝试的超时重试仍可幂等复用，不同任务或不同实例的同序号尝试不再误领同一邮箱/lease，避免一条任务失败 discard 后让另一条成功任务在 `finalize_success` 收到 `409 stale_lease`；缺失父任务或 attempt token 的旧调用保持原 request ID 兼容行为，任务停止/跳过仍使用原局部 token。
- **适配日文 about-you 新版 React DateField（v2.15.1）**：`services/chatgpt_core/browser_registration.py` 现在读取 `[role=group][aria-labelledby]` 关联的“生年月日”标签，并在同时出现标题“年齢を確認します”与 `year/month/day` 分段控件时优先判定为生日模式，不再把年龄值写回姓名输入框。分段生日按年、月、日逐段选中替换可见 contenteditable 状态，随后严格校验隐藏 `input[name=birthday]` 已同步为目标 `YYYY-MM-DD` 才允许提交；既有年龄输入、普通生日文本框和下拉日期路径保持兼容。
- **按 OpenAI 结构化业务码分类注册诊断（v2.15.1）**：`services/chatgpt_core/registration_diagnostics.py` 从关键失败响应的 JSON 或受截断文本中提取 `error.code`，使 `invalid_username_or_password`、`username_already_exists`、`invalid_auth_step` 与 `invalid_state` 优先于泛化的 `upstream_http_400/401`，分别落到密码登录、已有账号或注册会话阶段。成功 outcome 现在无条件清空陈旧失败码并写入 `failure_stage=completed`，诊断标题同步改为“注册尝试已完成”，避免成功抓包仍显示“注册尝试未完成”。
- **同步侧栏可见版本为 `v2.15.1`**：`frontend/src/app/AppShell.tsx` 更新版本标识，便于确认主服务、Plus 与 Plus2 已加载本次 HME、生日表单和诊断分类修复。
- **修复独立注册页无法显示注册诊断模式（v2.15.0）**：`frontend/src/pages/RegisterTaskPage.tsx` 将固定的 ChatGPT 平台值注册为隐藏 Form 字段，使 `Form.useWatch('platform')` 能读取真实平台并在选择 headed/headless 执行器时渲染诊断三段控件；提交请求继续对平台和执行器做归一化，切回纯协议或非 ChatGPT 入口时强制发送 `off`。账号页注册弹窗沿用其已有的显式平台状态，不改变现有注册配置持久化。
- **修复切换筛选组合把默认每页数量重置为 `20`（v2.14.3）**：`frontend/src/pages/Accounts.tsx` 不再从条件筛选组合应用历史 `pageSize`，组合切换、固定组与“未固定”范围切换现在只改变筛选条件，始终保留当前浏览器的分页数量；页大小也从组合 dirty 签名和组合编辑表单中移除，避免设置 `35` 为默认后切换组合被旧组合值覆盖或误报“当前筛选已修改”。后端继续接受旧组合中的 `pageSize` 字段以保持数据兼容，但前端永久忽略该字段；侧栏可见版本同步为 `v2.14.3`。
- **修复 10 路注册下偶发的邮箱、密码、OTP 与 about-you“提交后未跳转”（v2.14.1）**：密码表单现在只定位输入框所属表单的可见提交按钮，未产生业务 POST 时按阶梯执行单次兜底；`user/register=2xx`、`email-otp/validate=2xx` 和 `create_account=2xx` 被记录为不可重放的提交事实，即使 OpenAI SPA 仍停留在旧页面也不会重复密码、重复消费验证码或再次开户。OTP 已验证但 about-you 导航失败时仅允许一次同上下文 authorize 重入；开户已确认但 ChatGPT Web Session 仍未就绪时仅允许一次同上下文已有账号登录恢复，成功后继续复用原 Cookie 抓取 AccessToken/Session。邮箱 OTP 与 add-phone 手机 OTP 保持独立提交函数，手机号路径只操作当前 UI 表单，不会误调用 `/email-otp/validate`。任务日志新增 `business_request`、`last_http`、`elapsed_ms` 与各阶段提交标记，能区分“没有发出请求”“服务端拒绝”“请求成功但前端未推进”。`ChatGPTBrowserRegister` 的 request/response/requestfailed trace 监听器现在由 `ExitStack` 在成功、异常或停止时统一移除并清空未完成请求；`_NetworkActivityObserver.close()` 同步释放请求与响应对象，避免常驻 Plus 进程随浏览器事务累计引用。
- **修复高并发注册的任务快照丢更新与短跳转窗口（v2.14.0）**：`api/tasks.py` 对 `registered_accounts`、`auth_pending_accounts` 的任务 meta 读改写增加任务级锁，防止多个成功线程用旧快照互相覆盖；`services/chatgpt_core/any_auto/browser_register.py` 将邮箱、OAuth、OTP 和 about-you 提交后的固定 `20s` 状态窗口统一改为实例可配置等待，Plus 默认 `40s`，降低代理出口或上游 SPA 变慢时被误判为“提交后未跳转”的概率。对应的后端容量、Solver 空闲回收、10 并发控制、动态代理按需生成、共享配置隔离和前端设置合同均补充隔离测试；侧栏可见版本同步为 `v2.14.0`。
- **修复分页设置弹层的按钮点击被提示层拦截（v2.13.3）**：`frontend/src/features/accounts/components/AccountsTable.tsx` 不再为分页齿轮使用嵌套的 Ant Design Tooltip；真实 Chromium 烟测确认原提示层在 Popover 打开后会残留并覆盖“添加并使用”按钮，导致点击一直被 tooltip 捕获。齿轮改用不会生成额外浮层的原生 `title` 提示，图标语义和无障碍名称保持不变，分页设置弹层内的新增、选择与删除操作不再受遮挡。
- **固定账号排他范围收敛到所属一级组合（v2.13.2）**：`services/account_filters.py` 将显式 `primary_preset_id + secondary_scope=unassigned` 的排除条件从“账号属于任意固定组”改为“账号属于当前一级组合下的固定组”。固定在 `Free 已注册` 下的账号升级为 Plus/Pro 后，会重新进入 `Plus 未接码未传`、`Plus 长效未传` 或其他符合当前状态的一级条件组合；原 Free 固定组成员记录、revision 和批次查看能力保持不变，加入其他固定组时仍通过现有 `409` 冲突要求显式移动。没有一级组合上下文的旧列表、手工条件请求和批任务继续沿用全局未固定边界，避免扩大历史调用范围；本次不迁移、不解绑、不改写任何实例账号数据，并将筛选审计版本提升为 `account-list-state-v11-parent-scoped-fixed-groups`。
- **账号导出未勾选时改用当前筛选全部结果（v2.13.1）**：`frontend/src/pages/Accounts.tsx` 的 Sub2API JSON 与纯 AccessToken 导出现在优先使用跨页明确勾选的账号；没有勾选时复用统一任务范围合同，提交当前搜索、状态、列筛选、一级条件组合、二级固定组合及其 revision，并以当前筛选总数做并发变化校验，不再把空 `ids` 隐式解释成全库，也不受当前页码和每页数量限制。`api/chatgpt.py` 在签发一次性下载票据前通过 `resolve_filtered_accounts()` 解析并冻结完整账号 ID，筛选结果变化返回 `409`，空范围返回 `400` 而不会退化成全量；显式 ID、旧调用方的历史空 ID 全表语义以及 PIX“已选账号 / 当前筛选”双入口保持兼容。`tests/test_filtered_task_scope.py` 与 `frontend/tests/accountsExportAndPresetActionsContract.test.mjs` 覆盖 JSON、AccessToken、跨页筛选、空范围保护和前端范围选择合同。
- **固定账号不再被任意条件范围重新吸收（v2.13.0）**：`services/account_filters.py` 将固定归属设为 ChatGPT 条件查询的默认排他边界；未明确选择固定组的列表和条件批任务只解析未固定账号，固定组成员不会再同时出现在 Free 已注册、Plus 或其他临时条件结果中。明确账号 ID 的编辑、删除和维护接口不受该边界限制；按条件删除继续关闭式排除全部固定成员。
- **修复固定组身份漂移、父级越界和响应丢失（v2.13.0）**：固定组创建及后续新增成员都必须在加入时满足父级动态条件，已加入成员之后状态变化不会自动退出；父级或成员 revision 变化会在批任务冻结前返回 `409`。稳定身份 SQL 同时比对 ID、邮箱和创建时间，账号删除触发器清理旧归属，避免主键复用串组；创建响应现在合并返回跨平台、不存在等全部被忽略账号，前端可以准确提示影响数量。
- **定向修复 OAIPay Plus/Pro 仅 AT 未接码上传（v2.12.5）**：`services/oaipay_sync.py` 新增 OAIPay 私有 readiness 判定，只对认证有效、具备 AccessToken 与 account/workspace 标识、当前或最近有效订阅为 Plus/Pro 的无 RefreshToken 账号放行；`services/chatgpt_core/oaipay_upload.py` 同步收紧底层门禁，使该类账号携带空 RT 进入既有 `paid_without_refresh_token` 自动分类并上传到 `PLUS--未接码`。缺少 AccessToken、缺少 workspace/account_id、Free 无 RT、认证失效及 Team/Business/Enterprise 账号仍关闭式拒绝；共享 `services/chatgpt_account_state.py`、Sub2API、CPA、失效测活与注册链均未修改。侧栏可见版本同步为 `v2.12.5`。
- **恢复邮箱登录测活对失效测活公共常量的兼容导入（v2.12.0）**：v2.11.2 将 `AT_ONLY_CLEAR_EXTRA_KEYS` 重命名并扩展为失效测活专用集合时，`services/chatgpt_core/custom_email_recheck.py` 的既有导入没有同步，导致邮箱登录测活模块首次加载即 `ImportError`。`invalid_account_recheck.py` 现在恢复旧常量及其原始字段语义，并在此基础上单独扩展失效测活需要清理的 Cookie 与账号 ID；邮箱测活不会因此新增凭据清理行为，失效测活的完整 Web Session 覆盖规则也保持不变。
- **修复失效测活误入补抓 Auth/RT 与手机号链路（v2.11.2）**：`services/chatgpt_core/invalid_account_recheck.py` 将 `status=invalid` 的账号恢复收口为单一登录测活事务：使用 any-auto Camoufox 的 `login_only` 模式登录已有账号，只在同时取得新的 AccessToken、NextAuth/Auth.js session token 与完整 Cookie header 后写回原账号；成功时清除旧 refresh token、旧 `chatgpt_local` 失效证据，恢复账号主状态与 `account_list_state`，并用新 AT 调度常规本地状态刷新后立即结束。该路径不再调用 `custom_email_recheck`，不再进入 Codex OAuth/RT、`resume_subscription_auth` 或 `add_phone` 绑定；登录落到新账号密码页或 `about_you` 时关闭式失败，落到 `add_phone` 时也只尝试 Web Session 桥接且不会调用手机号回调。
- **恢复失效测活独立任务身份与单阶段时间线（v2.11.2）**：`frontend/src/pages/Accounts.tsx` 与 `RegisterTaskModal.tsx` 新增独立 `invalid_recheck` 弹窗模式，单个、批量、活动任务恢复和历史快照均显示“失效测活”，不再映射为“补抓Auth”；`api/tasks.py` 将任务时间线改为“登录已有账号并刷新 Web Session”的单阶段合同，完成文案只报告会话写回与本地刷新，不再出现“完整 Auth/RT”或“待补抓 Auth”。侧栏版本同步为 `v2.11.2`。
- **修复本地状态并发占满 SQLAlchemy QueuePool 导致网页卡死（v2.11.1）**：`services/chatgpt_core/local_status_refresh.py` 将账号读取、远程 ChatGPT 探测和写回拆为 detached snapshot + 短事务；`api/tasks.py` 现在会在指纹锁、SID 准备、代理探测和 ChatGPT HTTP 前关闭读 Session，写回前重读账号并比对 auth-material revision，防止旧探测覆盖新 Token。认证材料或保存指纹变化时会先释放旧 identity/capacity lease，再按新身份重新排队；detached snapshot 同时固化账号 ID、平台、邮箱和创建时间，SQLite 删除后复用同一数字 ID 时会中止旧刷新，禁止把结果写入替代账号。旧 `sync_chatgpt_account_local_status(session, account)` 签名保留，但会在 `commit` 前建立快照，规避 SQLAlchemy `expire_on_commit` 隐式重取连接；支付链接、BaxiGPT 和既有保存 hook 无需改调用签名。
- **修复连接池耗尽时认证中间件阻塞 Uvicorn event loop（v2.11.1）**：`main.py` 将管理员密码初始化检查和 `verify_token()` 整体放入 Starlette threadpool，未初始化继续返回 `503`、无效/撤销 Token 继续返回 `401`、存储异常关闭式返回 `503`，不再卡住静态资源和其他 API 的事件循环。`core/config_store.py` 的只读路径扩展为捕获全部 `SQLAlchemyError`，QueuePool timeout 时回退已加载缓存；本地 `auth_*` key 先判断不可共享，避免每个请求多做一次 shared-mode 数据库查询。管理员 session 有效性不做正向缓存，logout/撤销仍即时生效。
- **修复手机号绑定拿到 RT 后账号仍显示失效（v2.10.4）**：`services/chatgpt_core/subscription_auth_capture.py` 在 OAuth 成功签发新 AccessToken/refresh_token 后，不再让旧凭证对应的 `chatgpt_local.access_token_invalidated / HTTP 401` 快照继续覆盖新认证事实；旧探测中的订阅计划会保留为 last-known 能力上下文，账号主状态、`chatgpt_capabilities` 与 `account_list_state` 改为同一事务内立即重算。`services/chatgpt_core/local_status_refresh.py` 为长耗时探测增加认证材料 revision 校验，新 RT 在探测期间落库时会丢弃旧 token 的结果并用最新凭证重跑；同账号已有刷新时不再吞掉新请求，而是合并一次尾随刷新。`frontend/src/pages/Accounts.tsx` 在手机号绑定、补抓 Auth、本地状态同步等任务进入终态时同时刷新任务列表与账号查询，避免 React Query 的一分钟缓存继续显示任务前的“失效”状态；侧栏版本同步为 `v2.10.4`。
- **修复手机号绑定与注册链路首封邮箱 OTP 被误判为旧邮件**：`services/chatgpt_core/oauth_client.py` 与 `utils.py` 为 OAuth `FlowState` 增加真实发码时间锚点，在 `/api/accounts/authorize/continue`、`/api/accounts/password/verify`、`/api/accounts/passwordless/send-otp` 请求发出前记录截止线，并贯穿到邮箱轮询；Playwright Sentinel、页面响应解析和进入 OTP 页的耗时不再把截止线重置为“开始轮询时刻”。成功重发才切换为重发请求起点，重发失败保留原截止线；只有旧调用没有时间锚点时才使用 60 秒兼容回退，并统一预留 5 秒邮件服务器时钟/队列偏差。`services/chatgpt_core/any_auto/register.py` 同步修正新账号显式发码、已有账号自动发码、密码响应直达 OTP 及遗留 Codex 登录分支，避免注册、已有账号登录和 Auth/RT 补抓重现同类问题。
- **修复 HME 共享转发箱的旧邮件诊断归属错误**：`core/base_mailbox.py` 现在先读取邮件详情并严格校验当前物理别名或 `+tag` 传输头，再应用 `otp_sent_at` 时间过滤；其他并发账号的邮件只记录“未匹配当前 HME 别名”，不会再被错误打印成当前 alias 的“早于截止线”邮件，匹配当前 alias 的真实旧信仍保留可诊断的提前秒数。
- **手机号绑定 Info 日志显示完整手机号和邮箱**：`services/chatgpt_core/task_logging.py` 与 `api/tasks.py` 将完整身份展示严格限定到 `phone_binding_test` 的 Info 日志，并覆盖串行/并发格式化、SSE 任务存储、任务快照读取和持久化历史；Debug 仍遮蔽手机号与邮箱，OTP、授权码、token、密码、Cookie、代理凭据和 API 密钥继续无条件脱敏。完整手机号会在 OTP 上下文脱敏前被临时保护，避免“发送验证码: +手机号”被误判为授权码而替换。
- **同步侧栏可见版本为 `v2.8.74`**：`frontend/src/app/AppShell.tsx` 更新版本标识，便于确认三个常驻实例已加载本次 OTP 时间锚点、HME 邮件归属和手机号绑定日志修复。
- **修复 OAIPay 手动上传弹窗无法拉取分组**：`main.py` 新增只包含 `/api/integrations/oaipay-categories` 的窄路由 `api/oaipay.py`，不重新挂载已退役的 `api.integrations`/GoPay 入口；手动弹窗继续使用旧前端路径，但现在由 `services/chatgpt_core/oaipay_upload.py` 的 `fetch_oaipay_categories()` 统一拉取分类，和自动上传共用 base URL 解析、分类接口候选、Bearer/raw 上传密钥兼容、`x-api-key` 头和 `data.items / data / categories` 响应解析，避免自动上传能分类、手动选择却 404 或认证失败。`frontend/src/pages/Accounts.tsx` 同步清空旧分类缓存并展示明确的分组拉取错误。
- **同步侧栏可见版本为 `v2.8.73`**：`frontend/src/app/AppShell.tsx` 更新版本标识，便于确认三个常驻实例已加载本次 OAIPay 手动分组与支付链接当前状态筛选修复。
- **修复手动手机号绑定任务启动后无实时日志**：`api/tasks.py` 将手动粘贴 `手机号----收码API` 生成的展示态 `manual` 与运行态 canonical `normal` 解耦；后台 runner 继续消费任务创建时冻结的 `phone_items`，但不会再把 `manual` 当作非法 `phone_pool_mode` 抛出 `HTTPException(400, "手机号取号模式无效")`。同时兼容旧快照中的 `manual/upload/paste` 别名，避免接口已返回 `task_id`、任务历史显示 `running`，但 background task 在首行日志前崩溃，导致前端 SSE 一直空白。
- **同步侧栏可见版本为 `v2.8.72`**：`frontend/src/app/AppShell.tsx` 更新版本标识，便于确认三个常驻实例已加载本次手机号绑定启动修复。
- **修复账号批量删除确认弹窗失效**：`frontend/src/features/accounts/components/AccountsToolbar.tsx` 改用页面 `App` 上下文提供的 `modal.confirm`，恢复“更多操作 → 删除选中”和“一键删无效”的确认交互；`frontend/src/pages/Accounts.tsx` 对选中 key 做正整数归一化，捕获 `/api/accounts/batch-delete` 失败并展示明确错误，成功后等待账号列表刷新，避免点击后无反馈或仍显示已删除账号。`api/accounts.py` 同时对请求 ID 去重，保证重复选择不会虚增删除数量；`tests/test_accounts_batch_delete.py` 覆盖账号行与派生列表状态的真实清理结果。
- **同步侧栏可见版本为 `v2.8.67`**：`frontend/src/app/AppShell.tsx` 更新版本标识，便于确认三个常驻实例已加载本次批量删除修复。
- **修复运行中断后历史详情无日志**：`api/tasks.py` 为活跃任务增加低频日志 checkpoint，并在停止、中断、错误、致命异常和汇总节点立即持久化；终态快照与历史日志窗口按单调游标合并，迟到的单账号回调不能再把整批任务提前写成成功/失败，也不能覆盖已保存的终态状态、错误、统计和完整日志。旧版本遗留的同一 `task_id` 重复行会在详情读取时只读合并，尽可能恢复完整日志窗口且不改写生产数据库。
- **保留旧任务的明确成功结论**：历史运行态只对数据库仍为 `running` 且当前进程已无对应任务的记录归一为“已停止”；数据库已经明确写为 `success/done/completed` 的旧记录继续显示成功，避免因旧快照停留在 `running` 而批量误判。
- **同步侧栏可见版本为 `v2.8.66`**：`frontend/src/app/AppShell.tsx` 更新版本标识，便于确认三个常驻实例已加载本次任务历史修复。
- **修复 Idea 任务中断后仍被后台轮询的问题**：`services/chatgpt_core/baxigpt_status_poller.py` 在入队和实际请求前检查持久化停止标记，已停止订单直接移除 target，不访问上游；`api/accounts.py`、`services/account_filters.py` 与 `frontend/src/pages/Accounts.tsx` 保留上游原始状态并明确展示本地停止结果。
- **修复服务重启造成的旧订单自动恢复**：移除 `main.py` 对 `restore_pending_targets()` 的启动调用，防止本地任务快照丢失后又从 `processing` 卡密记录恢复轮询。
- **同步侧栏可见版本为 `v2.8.64`**：`frontend/src/app/AppShell.tsx` 更新版本标识，便于确认三个常驻实例已经加载本次 Idea 轮询生命周期修复。
- **补齐独立卡密池测试兼容性**：`services/chatgpt_core/baxigpt_status_poller.py` 在账号表尚未创建的纯卡密池迁移/测试环境中安全跳过停止标记查询，不影响生产账号库中的强制停止语义。
- **修复不可用号段无法被限定绑定选择的问题**：`services/chatgpt_core/phone_pool_repository.py` 将号码可用性改为行级 `available / unavailable / all` 查询，`unavailable` 精确限定为带收码 API 的 `status=cannot_send` 记录，`all` 只组合自身可用与 `cannot_send` 行，不混入限流、冷却、耗尽或停用号码。`api/tasks.py` 的并发和串行 runner 同步保留不可用复测筛选值，避免运行时将全量复测错误回退成 `available`；固定快照的总进度按实际可测试的 `min(账号数, 候选数)` 收口，未触碰号码不会被预先恢复或改写。
- **同步侧栏可见版本为 `v2.8.63`**：`frontend/src/app/AppShell.tsx` 更新版本标识，便于确认三个常驻实例已经加载本次手机号绑定前端资源。
- **重构 ChatGPT 注册日志契约与网络 Debug 追踪**：`api/tasks.py` 将注册前缀从尝试计数改为“当前成功位/目标成功数”，并在 `start_attempt()` 与成功位快照之间使用同一把锁；目标成功数已达成时，排队补位线程在锁内直接返回 `NOT_STARTED`，不会产生越界成功位或无效注册。并发尝试、失败补位和浏览器不确定失败占槽仍保持原有调度语义；`services/chatgpt_core/task_logging.py` 统一输出 `[成功位/目标][步骤NN/09 阶段]`，移除任务名、尝试号、邮箱前缀和 `any-auto/<executor>` 重复标签。Info 业务节点补充当前邮箱、邮箱渠道、租约/邮箱 ID、代理/出口 IP、OTP 来源/等待/长度/重发次数、提交 HTTP/下一页以及库存/AT/Session/Cookie 状态；OTP 明文和密码不进入日志。`services/chatgpt_core/any_auto/register.py` 与 `browser_register.py` 为协议 Session 和 Camoufox 页面接入脱敏 `[HTTP]` 方法、主机路径、状态、耗时、页面、资源类型和请求/响应字节追踪，查询串、请求体、Cookie、Token、用户信息均剥离；`access_token_only_registration_engine.py` 仅将网络事务送入 Debug，并过滤 any-auto 重复邮箱/OTP 低层行。兼容旧邮箱适配器的字符串/对象返回值。前端侧栏版本同步为 `v2.8.62`。
- **收敛 ChatGPT 注册日志主线**：`services/chatgpt_core/access_token_only_registration_engine.py` 移除重复的 any-auto 启动/成功汇报，浏览器执行器在 Info 视图只保留一条 Web Session 成功节点，协议执行器保留统一的 AT/Session/Cookie 摘要，二次 OAuth 跳过和底层状态机细节降至 Debug；`services/chatgpt_core/task_logging.py` 将动态代理 SID、IP 保留时长和国家未验证状态拆为结构化字段，避免 `SID=refreshed retention=t-120` 粘连显示。前端侧栏版本同步为 `v2.8.61`。
- **持久化注册 Auth 边界审计字段**：`chatgpt_registration_mode_adapter.py` 将 `registration_auth_capture=not_requested` 加入账号 metadata 白名单，确保注册完成后数据库明确记录“未请求独立 Auth/RT”，与 AT/Session/Cookie 和 `auth_level=access_token_only` 一起可审计。
- **将 ChatGPT 注册与 Auth/refresh_token 获取彻底分离**：`services/chatgpt_core/chatgpt_registration_mode_adapter.py`、`services/chatgpt_core/plugin.py` 和 `api/tasks.py` 现在把所有注册请求（包括旧的 `refresh_token` 配置）规范化为单次 AccessToken-only signup。注册成功后只保存 signup 同一会话产生的 AccessToken、Web Session、Cookie、账号/Workspace 标识并结束当前尝试，不再启动独立 Codex OAuth、OAuth 邮箱验证码、密码页、`add-phone` 或第二次落库；完整 Auth/RT 继续由 `subscription_auth_capture` 的单个/批量补抓任务负责。
- **关闭注册阶段的隐式结算探测**：注册任务不再在账号落库前调用 checkout amount 或 GoPay 平台链接流程，避免支付/手机号链路拖长注册并混淆失败原因；支付链接与补抓 Auth 保持在后续流水线中执行。
- **清理前端注册模式误导**：`RegisterTaskPage`、`RegisterTaskModal` 和 ChatGPT 注册模式组件不再提供“有 RT 注册”开关或“注册后抓 RT”选项，明确展示注册仅保存 signup 凭据；旧 localStorage/请求字段仍兼容但会被服务端强制改为 AccessToken-only。
- **修复 refresh-token 注册第二阶段 OAuth 验证码适配器契约不一致**：`services/chatgpt_core/refresh_token_registration_engine.py` 的 `EmailServiceAdapter` 补齐跨阶段验证码排除接口，并支持独立浏览器 OAuth 使用 `ignore_budget=True` 绕过注册阶段 OTP 累计预算。此前 headless 注册主链已经拿到 AT/Session 后，第二阶段 OAuth 在邮箱验证码页因缺少 `used_codes_for_phases()` 直接抛出 `AttributeError`，任务却继续按仅 AT 账号成功落库；修复后 OAuth recovery 可以继续取得验证码并按完整 Auth/RT 结果收口。
- **补齐本地配置发布为共享模板的闭环**：`api/config.py` 的 `/api/config/share/push` 新增可选 `enable_shared` 契约；Settings 在本地模式下现在明确提供“发布本地并启用共享”操作，先以当前实例已保存配置覆盖共享模板，再基于同一 revision 将当前实例切回共享模式。页面存在未保存改动或共享 revision 已变化时会阻止/拒绝发布，避免把旧快照误当成共享母版；未传该字段的旧调用仍保持仅覆盖模板的行为。
- **修复 Settings 配置共享开关无法确认**：`frontend/src/pages/Settings.tsx` 的共享配置开启、关闭、推送和差异查看操作改用 `App.useApp()` 提供的上下文 `modal` 实例，不再调用当前构建中无法挂载的静态 `Modal.confirm/info`。现在切换到本地模式会正常显示确认框并提交 `PUT /api/config/share-state`；请求失败时会在页面提示具体错误，避免开关点击后无反馈。新增 `frontend/tests/sharedConfigContract.test.mjs` 锁定该交互契约。
- **记录 Argon2 与 Sentinel 测试问题的真实边界**：文档区分宿主机缺少 `argon2` 的依赖环境漂移和 Sentinel 旧中文日志断言造成的测试契约漂移，避免以后把两类问题误判为线上认证或浏览器运行故障。
- **修正 Docker 发布拓扑旧描述**：`docs/docker-image-release.md` 按当前 `docker-compose.multi.yml` 更新为 `auto-gpt`、`auto-gpt-plus`、`auto-plus2` 三个常驻业务实例与 `phone-api-relay` 共同运行，移除主服务 standby 的过时说法。

### 安全 (Security)
- **隔离并脱敏 MiyaIP 查询凭据与运行代理（v2.21.0）**：`Crc/KeyName` 只存在于配置存储和任务私有运行对象，注册账号 `extra`、任务公开 meta、任务详情与日志均不保存明文；共享配置审计仅记录 `present / length / sha256`，任务详情使用 `[REDACTED_TOKEN]`，公开模板显示 `provider-managed`。Generate 不记录含查询凭据的完整 URL，禁止重定向、使用流式响应并把读取量限制为 64 KiB；MiyaIP 配置与解析结果的 `repr()` 隐藏查询凭据、代理用户名、密码和原始代理 URL。
- **完成全项目安全审计并收紧应用层纵深防御（v2.20.0）**：新增 `docs/SECURITY-AUDIT-2026-08-12.md`，按认证授权、会话、供应链、秘密管理、注入/文件边界、容器监听与 Nginx 实际入口逐项记录确认结论。`main.py` 默认关闭 OpenAPI/Swagger/ReDoc，只允许通过显式开关启用；CORS 从任意来源改为默认同源及 `APP_CORS_ALLOWED_ORIGINS` 显式白名单；应用统一补齐 CSP、`frame-ancestors`/`X-Frame-Options`、`nosniff`、Referrer/Permissions Policy、HSTS 与 API `no-store`，Uvicorn 停止暴露服务端标识。`/api/auth/status` 的公开投影缩到首次初始化所需字段，TOTP 启用状态、密码摘要算法和会话策略只向有效管理员会话返回；安全头在本机回环直连和外层 Nginx/Cloudflare 路径均生效，不再把关键纵深防御完全寄托于边缘配置。
- **收紧新启用 TOTP 的密钥强度（v2.20.0）**：`api/auth.py` 对 Base32 密钥做规范化和真实解码，新启用 2FA 必须至少达到 20 字节（160-bit）；系统生成密钥和现有 32 字符生产密钥保持兼容，短密钥即使能生成正确动态码也关闭式拒绝。
- **限制 TOTP 登录挑战总失败次数（v2.20.0）**：密码阶段签发的 5 分钟临时挑战现在独立累计动态码错误，最多 5 次后消费，无法再通过更换出口 IP 绕开持久限流反复猜同一个挑战；不把正常移动网络/代理切换误判为登录盗用。原有按 IP 的密码/TOTP 持久限流、单次消费竞态保护和认证版本失效继续保留。
- **恢复全部外部 TLS 身份校验并移除 URL shell 拼接（v2.20.0）**：YesCaptcha、CPA、CLIProxy、OAIPay、Sub2API 与共享 Camoufox 的 Python 外部请求不再使用 `verify=False` 或屏蔽证书告警，统一走系统 CA 与主机名校验；当前三实例的 OAIPay/Sub2API HTTPS 目标已确认使用有效证书，回环及 Docker 内服务继续使用原 HTTP 合同。YesCaptcha HTTP 非成功状态关闭式失败；`services/chatgpt_core/payment.py` 的 Windows 浏览器回退改为无 shell 参数启动，支付 URL 不再进入命令解释器。兼容 CPA 标识与 AppleMail 消息去重中的 SHA-1 只承担非安全确定性 ID，显式标记 `usedforsecurity=False` 并保留原输出合同。
- **收紧可配置上游 URL、重定向与浏览器调试制品边界（v2.20.0）**：`core/safe_http.py` 让 OAIPay、PayPal 绑定和 PayPal SMS 本地轮询客户端只接受无 URL 凭据/片段的 HTTP(S)，并在每个 30x 跳转重新校验，禁止跨主机、端口切换、HTTPS 降级及自定义 scheme，保留同源跳转和标准 HTTP 到 HTTPS 升级；浏览器失败页与 Sentinel 中间 JSON 改为显式调试开关、随机私有临时目录和独占创建，避免固定 `/tmp` 文件被并发覆盖或符号链接劫持。
- **浏览器 Profile 与跨项目认证边界收紧（v2.17.0）**：storage state 使用账号独立目录、`0700` 目录权限、`0600` 文件权限和 `fsync + os.replace` 原子写入，任务快照只暴露 Profile 路径/状态、身份和材料存在性，不返回 Cookie、Session 或 AT 明文。`openai-pay-long-link` 继续只接收 AccessToken 与 request ID，不接收 storage state、Cookie、Session Token、Page、BrowserContext 或本地 Profile；释放动作仅结束 auto-gpt 本地浏览器进程，不注销远端网页会话。
- **登录态写回增加原账号身份与敏感材料边界（v2.16.0）**：`web_session_login.py` 在任何数据库覆盖前验证账号行未被替换，并以原 `extra.account_id / user_id` 对比捕获 Session 的 account ID；`account_identity_mismatch` 被归类为不可代理重试的确定性失败，避免错误邮箱、浏览器串号或并发记录替换污染原账号。任务 meta、逐账号结果和历史日志只保存身份摘要、代理脱敏摘要及材料存在性，不回传或持久化明文 AT、Session Token、Cookie 和邮箱密钥到任务日志。
- **诊断下载与敏感材料执行纵深隔离（v2.15.0）**：所有列表、下载、固定、删除和容量接口继续经过全局管理员 Bearer 鉴权；制品查询同时校验任务 ID 与索引 ID，下载文件使用固定白名单、规范化路径和 runtime 根目录边界，响应增加 `no-store/private`、`nosniff` 与 `Content-Security-Policy: sandbox`。诊断目录/文件权限固定为 `0700/0600`，结构化日志、协议 HAR、URL 查询和响应摘要统一脱敏，最终 Cookie 仅保存长度与 SHA-256；原始 full HAR/Trace 只存在实例本地受限目录并受容量、过期和显式删除策略约束。
- **短链生成强制 Web Session 门禁**：登录态短链只接受持久化了完整 NextAuth/Auth.js Session Cookie（兼容非分片、连续分片及独立 `session_token`）的账号；AT-only、缺失分片或已清除网页会话的账号在任务解析阶段直接跳过，支付核心再次执行同一门禁作为纵深校验。Cookie、Session Token 和代理凭据不会写入任务元数据、生成历史、接口响应或前端配置摘要；本地短链配置接口仅返回非敏感国家/币种目录和登录态要求。

### 测试 (Tests)
- **补齐 0 元检测国家、会话、金额和兼容回归（v2.22.0）**：`tests/test_payment_eligibility_probe.py` 覆盖一国三阶段请求体、单代理/单 Session、动态和固定代理出口校验、直连拒绝、国家币种映射、最终货币一致性、minor exponent 与 GCash 隔离；`tests/test_payment_eligibility_tasks.py` 覆盖新 profile 接口、请求归一化及旧混合链/缺档案/all-US evidence 的 PH/PHP 兼容，注册后汇总和账号列表测试覆盖格式化最终金额。使用断网、只读源码、临时 runtime/SQLite/shared config 的一次性生产同源测试容器运行专项及相邻账号筛选/任务范围回归 `108 passed`；前端 Node 合同 `69 passed`，TypeScript/Vite 生产构建、Python `py_compile` 与 `git diff --check` 通过，未执行真实注册、真实 Checkout 或任何支付外部写操作。
- **补齐注册后自动资格检测的隔离、并发和展示合同（v2.21.1）**：新增 `tests/test_registration_zero_amount_eligibility.py`，并扩展 `tests/test_payment_eligibility_tasks.py`、`tests/test_accounts_api_list_compact.py` 与 `frontend/tests/paymentEligibilityTaskContract.test.mjs`，覆盖成功/非 0 元/技术失败/缺 Auth 分层、坏配置不反转注册成功、确认态保留、账号身份锁、跨任务进程级并发上限 `2`、线程池/回调异常收口、手工中断关闭运行态、列表证据脱敏及注册双入口状态展示。使用断网、只读 checkout、临时 SQLite/shared config/runtime 的一次性生产同源测试容器运行支付资格、注册控制、账号紧凑序列化、账号筛选及任务范围相邻回归为 `178 passed`；前端 Node 合同 `69 passed`，TypeScript/Vite 生产构建、Python `py_compile` 与 `git diff --check` 通过。
- **补齐 MiyaIP 双渠道后端与前端合同回归（v2.21.0）**：新增 `tests/test_miyaip_proxy.py`、`tests/test_dynamic_proxy_config_api.py` 与 `frontend/tests/dynamicProxyProviderContract.test.mjs`，覆盖官网 Generate 参数、Format=1 解析、HTTP/SOCKS5、业务错误、响应限长、跨渠道禁止回退、重复线路、provider 继承、配置共存/切换/非法值、任务创建时缺凭据、任务 meta/共享审计/日志脱敏，以及所有任务入口只提交 provider、不提交凭据。使用断网、只读 checkout、临时 SQLite/shared config/runtime 的一次性测试容器运行动态代理、注册、批量测活、执行登录态、失效测活、手机号绑定、支付资格与配置持久化相邻回归为 `292 passed, 6 subtests passed`；前端 Node 合同 `68 passed`，TypeScript/Vite 生产构建与 `git diff --check` 通过。未提供真实 MiyaIP `Crc/KeyName`，因此本次不把真实供应商代理连通性列为已验证结果。
- **增加 Solver 独立入口导入回归（v2.19.3）**：`tests/test_project_timezone.py` 清除 `PYTHONPATH` 后从 `services/turnstile_solver` 工作目录执行 `start.py --help`，锁定直接脚本入口能够加载项目级北京时间模块且不会再次因包搜索路径退出。断网、只读 checkout、临时 SQLite/shared config/runtime 的一次性容器中，北京时间、Solver 浏览器池和系统健康定向回归为 `9 passed`；当前生产镜像直接入口实跑返回码为 `0`，前端 Node 合同 `60 passed` 且 TypeScript/Vite 生产构建通过。发布后同时检查三个实例的 `/health` 与容器内 `8889` 监听。
- **增加北京时间边界回归（v2.19.2）**：新增 `tests/test_project_timezone.py` 覆盖旧 SQLite naive UTC 恢复、aware UTC/epoch 转北京时间、任务历史序列化不改写存储值以及 Uvicorn `+0800` 日志格式；新增 `frontend/tests/projectTimezoneContract.test.mjs` 锁定统一 `Asia/Shanghai` formatter、各类管理端时间入口、镜像/四服务 Compose 时区和任务页禁止回退浏览器本地格式化。使用只读 checkout、临时 SQLite/shared config/runtime 和 `--network none` 的一次性生产同源 pytest 镜像运行任务历史、调度器、账号 API、BaxiGPT、手机号池、交付卡、注册诊断及拓扑相邻回归为 `213 passed`；前端 Node 合同 `60 passed`，TypeScript/Vite 生产构建、新增时区模块及任务页面增量 ESLint、Python 语法编译、Compose 展开与 `git diff --check` 通过。全量 ESLint 仍为仓库既有 `507 errors, 9 warnings`，集中在历史 `any`、Fast Refresh 和 Hooks 规则，本次没有新增同类红灯。
- **补齐订阅刷新证据保护、持久重试及状态展示回归（v2.18.3）**：扩展 `tests/test_chatgpt_local_status_refresh.py`、账号列表序列化、失效测活、手机号绑定/补抓 Auth、执行登录态和注册任务合同，覆盖同认证版本的已确认套餐不被 Unknown/网络失败降级、认证替换清理旧 401、三轮退避、进程恢复、旧“待刷新”发现、账号身份与 generation 防串写、成功代理复用、Token 双字段一致性及非 ChatGPT 更新隔离；新增 `frontend/tests/accountSubscriptionRefreshStateContract.test.mjs` 锁定“刷新中 / 刷新失败 / 不可确认”显示。断网、只读 checkout、临时 SQLite/shared config/runtime 的一次性测试容器中专项及相邻后端回归为 `227 passed`，前端 Node 合同 `56 passed` 且 TypeScript/Vite 生产构建通过；完整后端回归为 `1345 passed, 1 skipped, 5 failed`，纯 `HEAD` 临时副本定向运行同 5 条亦 `5/5` 原样失败，均为已登记的旧浏览器 helper/导航参数、手机号旧文案和退役 GoPay 合同，本次没有新增红灯。
- **补充支付资格协议、任务与前端合同回归（v2.18.0）**：新增 `tests/test_payment_eligibility_probe.py`、`tests/test_payment_eligibility_tasks.py` 和 `frontend/tests/paymentEligibilityTaskContract.test.mjs`，覆盖 OAICS/Stripe 金额分支、0 元与 GCash 业务解耦、唯一稳定 `cpmt_*`、禁止支付后续动作、动态模板地区门禁、重试中断、技术失败保留确认态、失败不计成功、预筛跳过计数、单账号/批量来源隔离、双路由/双标签/双筛选 UI；`tests/test_filtered_task_scope.py` 与 `tests/test_account_filters.py` 同步覆盖筛选范围一致性和旧表字段迁移。
- **补齐持久浏览器租约、释放控制和容量隔离回归（v2.17.0）**：新增 `tests/test_web_session_lease.py` 并扩展 `test_web_session_login.py`、`test_sentinel_browser.py`、配置共享及前端合同，覆盖同账号冲突、Cookie/Session/设备 ID 注入、storage state 权限与原子落盘、登录成功立即写回、保持中刷新、刷新超时取消、登录前/保持中释放、浏览器崩溃保留凭据、逐账号/全部释放 API、停止补位、代理错误分类、持久容量独立计数和实例本地配置。断网、只读 checkout、临时 SQLite/shared config/runtime 的一次性测试容器中相关及相邻回归为 `190 passed`；完整后端回归为 `1318 passed, 1 skipped, 7 failed`，同一 7 条在未挂载本次源码的 v2.16.0 测试镜像中 `7/7` 原样失败，均为已登记的旧浏览器 helper/导航断言、临时 Camoufox 可执行权限、手机号旧文案和退役 GoPay 合同，本次没有新增红灯。前端 Node 合同 `51 passed`，TypeScript/Vite 生产构建、增量 ESLint、Python `py_compile`、Compose 展开及 `git diff --check` 通过，侧栏版本同步为 `v2.17.0`。
- **补齐执行登录态服务、任务与前端合同回归（v2.16.0）**：新增 `tests/test_web_session_login.py`，覆盖任意账号状态、完整 Session 原子写回、refresh token 与订阅/使用/绑定状态保持、浏览器设备身份同步、账号身份错配拒绝覆盖、缺密码/邮箱状态跳过、单/批任务来源、并发参数、同毫秒任务 ID 隔离、部分失败继续、本地状态刷新调度失败不反转成功、worker 启动前立即停止及完成当前账号后不再补位。失效测活、any-auto Web Session、任务控制、任务日志、本地状态批处理、插件、账号筛选和历史任务扩展回归在断网、只读 checkout、临时 SQLite/shared config/runtime 的一次性测试容器中为 `252 passed`；完整后端回归为 `1305 passed, 1 skipped, 6 failed`，其中 5 条是已登记的旧浏览器 helper/导航参数、手机号旧文案和退役 GoPay 合同，另 1 条任务日志异步检查点时序用例在扩展组连续通过两次并于独立容器复跑 `3/3 passed`，本次没有新增稳定失败。新增 `frontend/tests/webSessionLoginTaskContract.test.mjs` 锁定行内/批量入口、API payload、代理/并发配置、独立任务标题及业务字段不变文案；前端 Node 合同 `50 passed`，TypeScript、Vite 生产构建、涉及 Python 模块 `py_compile` 与 `git diff --check` 通过。
- **补齐 SQLite 自锁、合并写入和支付提交顺序回归（v2.15.4）**：新增 `tests/test_task_checkpoint_locking.py`，使用文件型 SQLite 与独立连接覆盖另一 writer 持有 `BEGIN IMMEDIATE` 时 `_log()` 立即返回、锁释放后检查点最终落库、同任务 pending 只写最新快照、晚到 running 快照不覆盖终态，以及 fresh-cache 派生筛选在写锁存在时仍保持只读。`tests/test_register_task_controls.py` 以 20 个远端终态结果验证账号、支付历史和 `account_list_state` 已提交后才允许任务日志另开连接写入，防止等待时间随结果数线性叠加。断网、只读 checkout、临时 SQLite/shared config/runtime 的一次性生产依赖容器中，新增锁回归、账号筛选与完整注册控制模块共 `112 passed`；前端 Node 合同 `46 passed`，TypeScript/Vite 生产构建、涉及 Python 模块 `py_compile` 与 `git diff --check` 通过。
- **补齐固定组合计数与刷新结果统计回归（v2.15.3）**：`tests/test_account_filter_presets.py` 覆盖当前 Plus/Pro、Free、Unknown 及未确认历史 Plus 的三类聚合；`tests/test_probe_local_status_batch_config.py` 覆盖正常 Plus 分布、认证失效归入 Unknown、结构化 HTTP 429 不再计成功，以及 Codex 部分失败仍保留已落库 Free 分布；`tests/test_account_filter_presets_ui.py` 与 `frontend/tests/taskCompletionRefreshContract.test.mjs` 锁定固定组合短标签悬浮、管理列表全称统计、刷新任务终态统计和账号/组合同步重载合同。断网、只读 checkout、临时 SQLite/shared config/runtime 的一次性测试容器中相关账号筛选、状态刷新和任务持久化回归为 `182 passed, 4 subtests passed`；前端 Node 合同 `46 passed`，TypeScript/Vite 生产构建和新增统计组件相关增量 ESLint 通过。
- **补齐开户后恢复、双提交和 pending 落库回归（v2.15.2）**：`tests/test_browser_registration_flow.py` 锁定首个 create-account `2xx` 压倒后续 `409` 且表单只提交一次；`tests/test_any_auto_web_session_contract.py` 覆盖 OTP 后原上下文 about-you settle、开户后导航超时、Auth 错误页、Session 抓取异常、existing-account 恢复及失败后可持久化 pending 合同；`tests/test_registration_diagnostics.py` 覆盖 identity-provider 与 post-signup 精确阶段；AccessToken-only 与 mode-adapter 测试确认 pending 账号 finalize 邮箱成功、跳过 checkout/外部上传并保留补抓字段。断网、只读 checkout、临时 SQLite/shared config/runtime 的一次性测试容器中相关及相邻回归为 `131 passed, 1 skipped, 16 subtests passed`，pending 保存/覆盖/上传门禁另为 `4 passed`；主动排除的 3 条仍是 changelog 已登记的旧 `_run_browser_registration` 与旧导航参数断言漂移，本次没有新增失败。
- **补齐 HME 跨任务隔离、日文分段生日与诊断分类回归（v2.15.1）**：`tests/test_icloud_hme_mailbox_finalize.py` 锁定同实例同任务重算稳定、不同父任务不共享 Helper request ID 及旧空 token 回退；`tests/test_browser_registration_flow.py` 复现标题含“年齢”且 age accessible-name 误命中姓名的现场，确认姓名不被数字覆盖、年月日可见段与隐藏生日值一致；`tests/test_registration_diagnostics.py` 覆盖四类结构化业务码优先于 HTTP 400/401/409，以及 success 清空陈旧失败。使用断网、只读 checkout、临时 SQLite/shared config/runtime 的一次性测试容器运行上述专项与 AccessToken-only、ChatGPT 注册相邻合同，结果 `102 passed, 6 subtests passed`；涉及模块 `py_compile`、`git diff --check` 与前端 TypeScript/Vite 生产构建通过。完整浏览器测试文件仍有 changelog 已记录的 3 条既有旧私有方法/导航参数断言漂移，本次新增用例通过且未扩大处理范围。
- **补齐注册诊断后端、前端与视觉合同（v2.15.0）**：`tests/test_registration_diagnostics.py` 覆盖模式校验、Trace/HAR/视频收口、协议 HAR 脱敏、诊断失败不影响注册、智能成功样本、单次配额降级、原子重试、视频能力降级、路径越界、固定/删除/清理及 API 下载；与 any-auto Web Session、手机号注册相邻回归在断网、只读 checkout、临时 SQLite/shared config/runtime 的一次性测试容器中为 `43 passed, 2 subtests passed`。真实 Camoufox 断网烟测验证全量模式在视频协议不受支持时仍生成有效 Trace、含记录的 full HAR、console、最终 DOM/截图和可下载 `ready` 诊断包。最终完整后端回归为 `1277 passed, 1 skipped, 6 failed`：其中 1 条未改动的本地状态并发时序测试单独连续复跑 `3 passed`，其余 5 条均为既有浏览器旧 helper/导航、手机号旧文案和退役 GoPay 合同失败。前端 Node 合同 `46 passed`、TypeScript、增量 ESLint、Python `py_compile` 与 `git diff --check` 通过；Playwright 在 `1440x900` 和 `390x844` 验证三段控件、固定操作列、表格横向可达性及页面无横向溢出。侧栏可见版本同步为 `v2.15.0`。
- **补齐 any-auto 页面状态推进与 late-failure 回归（v2.14.1）**：`tests/test_any_auto_web_session_contract.py` 新增邮箱业务响应先于 URL 变化、观察器异常释放、邮箱/手机 OTP 路径隔离、OTP 浏览器上下文参数、密码 2xx 后不重放、OTP 2xx 后单次 authorize 重入、开户后已有账号登录恢复及会话 trace 监听器清理覆盖；隔离测试容器中的 any-auto 定向合同 `21 passed`、共享浏览器状态机 `51 passed`，合并门禁共 `72 passed`。另有 3 条既有基线测试主动排除，分别为已删除 `_run_browser_registration` 的两处调用和旧 `35000ms` 导航断言。
- **补充历史订阅刷新时间前端合同**：新增 `frontend/tests/accountSubscriptionRefreshTimeContract.test.mjs`，锁定刷新时间必须来自订阅探测的 `checked_at`、复用 `MM-DD HH:mm` 格式化逻辑并同时进入桌面和移动端展示，且不得回退使用账号更新时间制造错误刷新事实；同时锁定暗色/亮色通用的主题次级正文色与 `12px` 可读字号。前端 Node 合同 `38 passed`，TypeScript/Vite 生产构建通过。
- **补充自定义分页数量合同回归（v2.13.3）**：新增 `frontend/tests/accountsPageSizeCustomizationContract.test.mjs`，锁定基础选项、自定义值持久化、删除当前值回落以及桌面/移动端增删入口；前端 Node 合同 `37 passed`，TypeScript 与 Vite 生产构建通过。`tests/test_account_filter_presets.py` 增加 `35/100` 保存断言，并在只读 checkout、临时 SQLite、断网的一次性测试容器中专项 `11 passed`；测试未读取或写入三个常驻实例的账号数据库和共享配置。
- **补齐固定账号父级排他回归（v2.13.2）**：`tests/test_account_filter_presets.py` 同时锁定同一父级的固定成员继续从“未固定”排除、跨到其他一级条件组合后按当前状态重新可见、固定组查看与跨组 `409` 显式移动保持不变，并验证列表和 `resolve_filtered_accounts()` 批任务范围一致；无一级组合的旧请求仍全局排除固定成员。一次性同源 pytest 镜像以只读 checkout、临时 SQLite/shared config/runtime 和 `--network none` 运行账号筛选、导出及删除相邻回归 `80 passed`；完整 `tests/` 为 `1249 passed, 1 skipped, 7 failed`，7 条均为现有浏览器旧接口/导航断言、只读容器内 Camoufox 临时可执行权限、手机号旧文案和已退役 GoPay 类型合同，没有本次新增失败。前端 Node 合同 `35 passed`，TypeScript/Vite 生产构建通过；修改后的解析器只读连接 Plus 实际库影子验证，同一批 Plus 未接码未传账号在 `Free父级 / Plus父级 / 无父级旧请求` 下分别为 `0 / 13 / 0`。
- **补齐两级组合与排他范围回归（v2.13.0）**：`tests/test_account_filter_presets.py` 覆盖父子 CRUD、全实例唯一归属、默认未固定范围、父级成员门禁、状态漂移稳定归属、成员 revision、SQLite ID 复用、旧 fixed 冲突优先级及父级不匹配迁移阻断；`tests/test_account_filter_presets_ui.py` 锁定两行名称 UI、固定组切换不自动勾选、统一任务 scope 和迁移父级不预填。一次性同源 pytest 镜像以只读 checkout、临时 SQLite/shared config 和 `--network none` 运行专项及共享筛选回归，结果 `31 passed`；完整 `tests/` 为 `1246 passed, 1 skipped, 7 failed`，7 条均位于未修改的浏览器旧接口/导航断言、Camoufox 临时可执行权限、手机号旧文案和已退役 GoPay 类型合同，与本次功能无关。前端 TypeScript/Vite 生产构建及 Python 编译检查通过，侧栏可见版本同步为 `v2.13.0`。
- **补齐 OAIPay 私有门禁回归（v2.12.5）**：`tests/test_oaipay_sync.py` 覆盖 Plus 仅 AT 账号通过 backfill、空 RT payload 自动进入 `PLUS--未接码`，并锁定无 AccessToken、Free 无 RT 及缺少 workspace 的账号均在分类/上传网络请求前被拒绝，避免 OAIPay 例外再次扩散到通用上传 readiness 或其他业务链路。基于 `auto-gpt:test-v2121-predeploy` 的一次性断网、只读 checkout、临时 SQLite/shared config/runtime 容器中，OAIPay 专项及通用账号能力、Sub2API、退役能力相邻回归共 `66 passed`；前端 TypeScript 与 Vite 生产构建通过。
- **验证现场回退后的 v2.12.1 运行基线**：在只读 checkout、临时 SQLite/shared config/runtime 和 `--network none` 的 `auto-gpt:test-v2121-predeploy` 容器中，重新运行失效测活、Web Session、动态代理、共享任务代理、代理扫描、OAIPay v2.12.1 基线、Sentinel 浏览器容量与筛选 UI 回归，共 `124 passed`；另执行前端 TypeScript/Vite 生产构建与回退涉及 Python 模块的 `py_compile`，测试未访问真实账号、邮箱、代理、OAIPay 或 OpenAI。
- **验证 v2.12.1 完整回退基线**：使用 `auto-gpt:test-v2121-predeploy` 同源依赖镜像，以只读 checkout、临时 SQLite/shared config/runtime 和 `--network none` 运行失效测活、Web Session、动态代理、共享任务代理、代理扫描、OAIPay、Sentinel 浏览器容量与筛选 UI 回归，共 `124 passed`；测试未访问真实账号、邮箱、代理或 OpenAI。另执行前端 TypeScript/Vite 生产构建和回退涉及 Python 模块的 `py_compile`，确保发布内容与 `5e65526` 行为基线一致且可加载。
- **补齐五槽资源门控与置顶组合显示合同（v2.12.1）**：`tests/test_sentinel_browser.py` 覆盖 PID 余量不足时后续浏览器等待、余量恢复后继续、结构化 `reason=pids` 日志，以及启动间隔不会绕过共享容量上限；`tests/test_account_filter_presets_ui.py` 锁定置顶组合仍按 `pinned` 过滤、旧 `slice(0, isMobile ? 4 : 8)` 截断不再出现，并确认组件遍历全部 `pinnedFilterPresets`。基于当前生产镜像派生的一次性 pytest 镜像，以只读 checkout、临时 runtime/shared config 和 `--network none` 运行两个专项文件，结果 `31 passed`；前端 TypeScript 与 Vite 生产构建通过，Compose 展开校验确认三实例资源参数分别为 Auth `2/5/2`、Solver `4/1/4`、PID `768/1536/768`。
- **补齐失效测活代理/并发与手机号无固定上限回归（v2.12.0）**：`tests/test_invalid_account_recheck.py` 覆盖任务代理进入实际浏览器运输、旧直连合同、指定代理参数、批量请求并发 `6` 不被截断及线程池实测峰值 `6`；`tests/test_phone_pool_task_integration.py` 锁定手机号绑定请求/实际并发 `6` 与任务快照一致；`frontend/tests/invalidRecheckTaskModalContract.test.mjs` 锁定单个/批量配置入口、四种代理方式、并发 payload 及前端不再出现 5 上限。基于生产依赖派生的只读 checkout、临时 SQLite/shared config、断网一次性测试容器中专项 `33 passed`，扩大到代理解析、筛选范围、浏览器登录合同、手机号分配与任务日志后 `119 passed`；修复兼容常量后全仓为 `1236 passed, 1 skipped, 5 failed`，5 条均是既有 changelog 已记录的旧浏览器接口/导航断言、手机号旧文案和已退役 GoPay 类型合同，没有本次新增失败。前端 Node 合同 `33 passed`，TypeScript/Vite 生产构建通过。
- **补齐失效测活 Web Session 收口回归（v2.11.2）**：`tests/test_invalid_account_recheck.py` 覆盖完整 AT/session/cookie 覆盖旧材料、清理旧 RT 与本地失效证据、原账号状态及派生列表状态同步、禁止后续 Auth 调用、缺失 session/cookie 保持失效、停用账号分类和 `login_only` 运输参数；`tests/test_any_auto_web_session_contract.py` 覆盖只登录模式透传、注册专属状态拒绝、`add_phone` 不触发手机号绑定以及登录后继续执行 Web Session 桥接；`frontend/tests/invalidRecheckTaskModalContract.test.mjs` 锁定失效测活来源、启动处理器和标题均不得回退为 `resume_auth` / “补抓Auth”。从当前生产镜像派生的一次性断网测试容器中专项 `19 passed`；浏览器状态机、AccessToken-only 注册和账号筛选相邻回归 `106 passed, 3 failed`，3 条失败已在镜像内置的发布前源码复现，均为 changelog 已记录的旧 `_run_browser_registration` / 导航参数测试漂移；前端 Node 合同 `32 passed`，TypeScript/Vite 生产构建通过。全仓 ESLint 仍有既存的 `489 errors, 9 warnings`，主要来自历史 `no-explicit-any` 与 React hooks 规则，不属于本次改动。
- **补齐本地状态并发、SID 与认证阻塞回归（v2.11.1）**：`tests/test_probe_local_status_batch_config.py`、`test_chatgpt_local_status_refresh.py`、`test_chatgpt_probe_endpoints.py`、`test_chatgpt_plugin.py`、`test_config_store_env_fallback.py` 与 `test_admin_auth_security.py` 覆盖两个批任务合计峰值不超过设置、跨任务重复指纹串行、等待停止/异常不泄漏槽位、legacy/by-id/endpoint/action/plugin 路径在阻塞网络期间 `QueuePool.checkedout()==0`、auth revision/指纹变化后重领 lease、SQLite 账号 ID 复用防误写、每账号只生成一个主 SID、代理失败复用健康 SID、HTTP 407 切换、HTTP-version 429 与业务错误不切换、配置更新竞态、配置超时缓存回退及慢认证不阻塞 event loop。从当前生产镜像派生的一次性 pytest 镜像以只读 checkout、临时 SQLite/shared config/runtime 和 `--network none` 运行，最终聚焦回归 `139 passed`；完整 `tests/` 套件为 `1225 passed, 1 skipped, 7 failed`。7 条失败均位于本次未修改的浏览器旧接口/Camoufox 可执行测试、手机号 UI 旧文案和已退役 GoPay 旧合同，并已在基线镜像复现，不属于本次改动回归；前端 Node 合同 `30 passed`，TypeScript/Vite 生产构建通过。
- **补充注册容量与兼容性回归**：新增 `tests/test_register_task_config.py`、`tests/test_chatgpt_register_exit_ip_registry.py` 并扩展 `tests/test_register_task_controls.py`，覆盖 protocol/browser 默认值与不可抬高的硬上限、手机号/手动邮箱串行、非 ChatGPT 旧行为、`0/0` 与旧固定延迟、requested/effective 元数据、首个账号立即启动、后续随机抖动、配置关系校验、跨任务原子 claim、冷却和 IPv6 `/64`。一次性断网容器使用只读源码、临时 runtime/SQLite/shared config，后端专项 `72/72` 通过；前端 Node 合同测试 `27/27`、TypeScript 与 Vite 生产构建通过。
- **补充 HME 原地址 + 单 Tag 消费合同**：`tests/test_icloud_hme_mailbox_finalize.py` 锁定正常 ChatGPT 注册 prepare 发送 `address_mode=platform_default`、历史 Tag 长度实验显式发送 `random_tag`，并分别验证 Helper 返回 base 与 `+gpt随机3位` identity 时 `effective_address_mode`、完整地址和 Tag 元数据均被持久化；`tests/test_mailbox_state.py` 与 `tests/test_restored_email_service.py` 锁定该实际形态字段能通过有界账号邮箱状态白名单并在恢复链保留。一次性断网、只读运行依赖容器中 HME/finalize `30` 条和恢复服务 `9` 条测试通过，账号状态身份函数通过；前端 TypeScript/Vite 生产构建与 `20` 条 Node 合同测试通过。测试未执行真实注册、OTP 或 HME checkout。
- **补齐登录态短链端到端合同回归**：`tests/test_chatgpt_payment.py` 覆盖 custom Checkout 短链、processor entity、Session Cookie 非分片/分片门禁及长短 URL 双向规范化；`tests/test_payment_link_sources.py`、`test_chatgpt_payment_link_endpoint.py`、`test_payment_link_task_guard.py` 与 `test_register_task_controls.py` 覆盖本地/远端分流、长短缓存与历史隔离、缺失 Web Session 跳过、单账号持久化及批量 runner。一次性断网、只读 checkout 测试容器相关支付回归 `109 passed`；前端 TypeScript/Vite 生产构建与 20 条 Node 合同测试通过。
- **补充 HME Ready/TempMail 合同回归**：`tests/test_icloud_hme_mailbox_finalize.py` 覆盖列表与详情 404 自动重绑及 OTP 边界保持；`tests/test_mailbox_state.py`、`tests/test_restored_email_service.py` 覆盖 provider 规范化、历史 anonymous/lease 身份隔离和账号路由优先；`tests/test_subscription_auth_capture.py` 覆盖失败任务只持久化新 mailbox ID。相关后端专项 155 passed（7 skipped），前端 `npm run build` 与 18 条 Node 合同测试通过。
- **补充固定筛选组合端到端合同回归**：`tests/test_account_filter_presets.py` 覆盖旧动态组合迁移、固定成员去重、ChatGPT 平台校验、5000 个成员上限、持久化身份引用、成员删除、SQLite ID 复用和全员失效空列表；新增 `tests/test_account_filter_presets_ui.py` 锁定同一筛选组合入口、按当前勾选自动选择固定模式、短组合 ID 查询、成员恢复以及批量 `account_ids` 范围。隔离测试容器相关及相邻账号筛选回归 `44 passed`，前端合同测试 `18 passed`，TypeScript 与 Vite 生产构建通过。
- **补充 OTP 时间锚点与手机号绑定日志合同回归**：`tests/test_chatgpt_register.py` 覆盖 authorize、密码、passwordless、首轮延迟、成功/失败重发；新增 `tests/test_any_auto_otp_timing.py` 覆盖注册、已有账号和显式发码；`tests/test_icloud_hme_mailbox_finalize.py` 锁定 HME 先匹配 alias 再判断新旧；`tests/test_chatgpt_task_logging.py` 与 `tests/test_register_task_controls.py` 锁定 Info 完整身份、Debug 身份遮蔽及强敏感字段始终脱敏。使用从生产镜像派生的一次性测试镜像、只读 checkout、临时运行目录和 `--network none` 分组执行，本次相关及相邻回归共 `292 passed`。
- **补充 OAIPay 手动分组与支付链接当前状态筛选回归**：`tests/test_oaipay_sync.py` 覆盖分类拉取复用自动上传解析器、raw Authorization 失败后的 Bearer fallback、窄路由响应 envelope；`tests/test_retired_capabilities_contract.py` 确认只恢复 OAIPay 分类兼容接口而不暴露 GoPay；`tests/test_account_filters.py`、`tests/test_account_filter_presets.py` 与 `tests/test_accounts_payment_link_filter_ui.py` 锁定 `has_link` 在 Python/SQL 筛选、筛选组合和账号页静态 UI 中的合同。
- **补充手动手机号绑定启动回归**：`tests/test_phone_pool_task_integration.py` 锁定手动上传号码时对外仍展示 `phone_pool_mode=manual`，任务 runner 实际收到 canonical `phone_pool_mode=normal`，并覆盖旧别名归一化，防止并发手机号绑定再次在创建后、首条日志前崩溃。
- **更新 GoPay 退役回归**：`tests/test_retired_capabilities_contract.py` 继续锁定退役路由与配置键不再进入 OpenAPI / config allowlist；`tests/test_account_filters.py` 与 `tests/test_pix_payment_link_cleanup.py` 切换到“旧 gopay 只算 other”的新分类预期，避免回归时又把退役支付类型重新恢复成独立枚举。
- **补充任务历史回归覆盖**：`tests/test_chatgpt_task_logging.py` 锁定活跃批任务的单账号结果不得提前关闭整批、终态中断不得被迟到回调覆盖、旧注册统计/汇总恢复、未知旧统计不得伪装为已知零值，以及内存日志压缩后仍保留持久化完整窗口；`tests/test_task_logs_history.py` 覆盖重复旧行的详情日志合并和明确成功状态不被误判。任务历史/控制专项 `92 passed`，相关任务运行时、终态守卫和支付摘要回归 `34 passed`，前端 Node 合同测试 `14 passed`，生产构建及本次修改文件 ESLint 通过。
- 新增 Idea 任务停止标记、轮询队列清理、手动入队拦截、账号筛选和提交摘要回归测试；相关专项测试共 `44 passed`，BaxiGPT 卡密提交专项 `49 passed`。
- **补充手机号绑定模式回归**：`tests/test_phone_pool.py` 覆盖指定前缀内 `available / unavailable / all` 的行级候选边界；`tests/test_phone_pool_task_integration.py` 覆盖限定号段不可用固定快照、全池 `cannot_send` 全量候选快照、候选数超过账号数时的未覆盖统计及 legacy 布尔模式优先级；`tests/test_phone_prefix_ui_contract.py` 锁定新模式、状态筛选、不可用号段可选、候选预览和请求字段契约。使用一次性 Docker 容器、`--network none`、只读 checkout 和临时运行目录执行该专项，结果为 `55 passed`。
- 新增 HME Ready 测试字段回归覆盖：普通 prepare 的调用参数保持向后兼容，测试模式才携带 Tag 长度实验所需的物理 alias、Tag、scheme 与 run 标识；`tests/test_icloud_hme_mailbox_finalize.py` 专项回归 `29 passed`。
- 新增注册日志合同断言：`tests/test_chatgpt_task_logging.py` 覆盖成功位/九阶段前缀、邮箱与 OTP 字段、HTTP Debug 脱敏（含用户信息/查询串/数字验证码）；`tests/test_access_token_only_checkout.py` 覆盖业务里程碑与网络 Debug 分流；`tests/test_register_task_controls.py` 覆盖失败不递增成功位、并发启动快照及“Debug 仅保留 HTTP”门禁。注册与浏览器/协议合同合并回归 `113 passed, 7 skipped, 2 subtests passed`；涉及模块 `py_compile` 通过。
- 扩展 `tests/test_chatgpt_registration_mode_adapter.py`、`tests/test_chatgpt_plugin.py` 和 `tests/test_register_task_controls.py`，锁定默认/legacy refresh-token 注册均只执行一次 signup、不调用第二阶段 Auth、不会写入 Auth 失败标记，并确认独立补抓 Auth 入口仍使用原适配器。
- 后端注册专项回归 `96 passed, 1 skipped`，前端 `npm run build` 通过；侧栏版本同步为 `v2.8.60`。
- 新增 `tests/test_restored_email_service.py` 适配器合同测试，覆盖跨注册阶段排除已消费验证码，以及独立 OAuth 等待不受注册 OTP 预算截断；专项测试 `9 passed`。
- **完成文档一致性校验**：通过 `git diff --check`、Markdown 代码围栏配对和文档目标链接存在性检查；本次仅修改项目文档，未执行 Python、前端或容器测试，也未触碰生产运行态。

## [2.8.68] - 2026-07-31

### 修复 (Fixed)
- **恢复 ChatGPT 手机号注册入口与实际请求合同**：`frontend/src/features/auth/components/RegisterTaskModal.tsx` 的 ChatGPT 注册入口重新提供“手机号注册”，`frontend/src/pages/RegisterTaskPage.tsx` 同步恢复该选项，避免两个注册界面继续分叉。`frontend/src/pages/Accounts.tsx` 现在在选择该入口时校验手机号池或 `手机号----收码API` 输入及统一登录密码，串行提交任务，并透传 `chatgpt_registration_entry=phone_signup`、`chatgpt_phone_signup_*` 参数与任务密码；邮箱注册专用的邮箱服务校验和注册模式适配不再污染手机号注册。后端继续复用既有 `PhoneRegistrationEngine`，手机号注册与手机号绑定保持独立业务语义。

### 测试 (Tests)
- **增加手机号注册前端入口回归合同**：`frontend/tests/phoneSignupEntryContract.test.mjs` 同时锁定 ChatGPT 注册弹窗、`/register` 页面中的“手机号注册”选项，以及账号页创建任务时的 `phone_signup` 请求字段、串行并发和密码透传，防止再次出现表单代码存在但没有入口或入口没有实际任务合同的问题。

## [2.8.57] - 2026-07-27

### 修复 (Fixed)
- **修复超过 1000 个账号时无法启动批量本地状态校验**：`api/tasks.py` 为 `batch_probe_local_status` 增加独立的 `LOCAL_STATUS_PROBE_MAX_ACCOUNTS=5000` 安全边界，显式选择与当前筛选两种范围统一使用该上限。现在可一次覆盖 `auto-gpt-plus` 当前 2863 个 free 账号，其他批处理原有的 1000 个上限保持不变；并发仍由 `LOCAL_STATUS_PROBE_MAX_CONCURRENCY=10` 单独限制，不会随账号总数放大。
- **修复主实例发布被宿主机 solver 端口冲突阻断**：`docker-compose.multi.yml` 将 `auto-gpt` 的宿主机 solver 映射改为 `${SOLVER_PORT_BIND_MAIN:-8889}:8889`；本机通过忽略的 `.env` 使用 `8894`，避让正在运行的 `abai-autoplus.service`，容器内 solver 契约和其他实例端口不变。
- **侧栏版本同步为 `v2.8.57`**：`frontend/src/app/AppShell.tsx` 更新可见版本号，便于确认三个常驻实例已加载本次发布资源。

### 测试 (Tests)
- 扩展 `tests/test_probe_local_status_batch_config.py`，覆盖 2863 个具备本地认证材料的 free 账号可完整解析，以及超过 5000 个专用安全上限仍返回 400 的边界。

## [2.8.56] - 2026-07-26

### 修复 (Fixed)
- **修复 Web Session 桥接的 authorize 假超时**：v2.8.54 只把 `services/chatgpt_core/any_auto/browser_register.py` 的注册入口导航改成主文档 `commit`，遗漏了 `services/chatgpt_core/browser_registration.py` 中 `_browser_chatgpt_openai_signin_bridge` 的 `auth.openai.com/api/accounts/authorize` 跳转，它仍以 `domcontentloaded` + 35s 等待。实测三次尝试中有两次在该处耗满 35s 才抛导航超时，桥接结束时 `/api/auth/session` 只能读到空 payload（`status=0 keys=[]`），accessToken 依赖后续“补拉”碰运气；唯一一次在 13s 内完成 `domcontentloaded` 的尝试则立即命中 accessToken。现在该跳转与桥接内首页导航、CSRF reload、回落 ChatGPT 首页统一改为 `wait_until="commit"`，超时压到 20s，落地后仍由 `_wait_for_auth_page_settle` 独立确认 URL 稳定。
- **收敛 Web Session 等待的导航预算**：`_wait_for_web_session` 的首页导航从 `domcontentloaded` + 45s 改为 `commit` + 20s。此前单次导航超时就能吃掉调用方 55s 总预算的绝大部分，导致 next-auth 桥接来不及触发或只剩一次轮询；改动后桥接能在预算内启动，并保留原有的“桥接晚于 deadline 命中也不丢弃”与超时前补拉逻辑。

## [2.8.55] - 2026-07-26

### 修复 (Fixed)
- **Helper `early_failure` 不再误占浏览器目标槽**：HME Helper 已确认“尚未进入 OpenAI 注册提交”的首页、CSRF、导航等早期失败时，`core/base_mailbox.py` 将权威 finalize outcome 依次传递到 `access_token_only_registration_engine.py`、`services/chatgpt_core/plugin.py` 和 `api/tasks.py`。任务结果现在输出 `reason_code=registration_early_failure`、`mailbox=early_failure`、`slot=0`、`backfill=yes`，会继续使用剩余 `register_max_attempts` 补足目标成功数。
- **保持晚期失败的非幂等保护**：只有邮箱系统成功 finalize 为 `early_failure` 才允许补位；OTP 等待已经开始、账号可能已完成密码/OTP/about_you 的 `late_failure`，以及没有权威邮箱 outcome 的普通浏览器异常，仍保持 `slot=1`、不重放身份。此次变更不放开同一尝试切换代理，也不改变已有账号确定性分流。
- **修复“最大尝试 9 次实际只跑 3 次”的调度语义**：此前任务层只按“浏览器是否已启动”判断所有失败均占槽，忽略 Helper 已释放租约的早期失败；目标为 3 时，1 成功加 2 失败会提前填满三个身份槽。现在早期失败不计入 `consumed_browser_failure_slots`，调度器会启动第 4 次及后续尝试，直到成功数达标、真正不确定槽满或到达最大尝试上限。

### 测试 (Tests)
- 扩展 `tests/test_subscription_auth_capture.py`、`tests/test_chatgpt_local_status_refresh.py` 与 `tests/test_chatgpt_codex_usage.py`，覆盖原账号为 `invalid`、旧本地探测为 `token_expired / HTTP 401` 时补抓新 RT 的完整写回，断言旧探测被废弃、last-known Plus 计划保留、`account_list_state` 立即恢复为 `valid / refresh_token`，并验证探测过程中凭证换代会自动重跑、同账号刷新请求会合并而不丢失；新增前端终态轮询合同测试，锁定任务完成后必须同时刷新账号状态。
- 扩展 `tests/test_any_auto_web_session_contract.py`、`tests/test_chatgpt_plugin.py`、`tests/test_icloud_hme_mailbox_finalize.py` 和 `tests/test_register_task_controls.py`，覆盖 finalize outcome 返回、失败 metadata 透传、`early_failure` 安全补位及未知失败继续占槽。一次性断网、只读生产依赖容器回归共 `96 passed`。

## [2.8.54] - 2026-07-26

### 修复 (Fixed)
- **修复认证 SOCKS5 代理下 Web Session 桥接稳定失败**：`services/chatgpt_core/browser_registration.py` 在页面内 `/api/auth/csrf` 暂时返回 `NetworkError` 时，优先复用浏览器 cookie jar 中的 next-auth CSRF，并兼容 cookie 将 `token|hash` 序列化为 `token%7Chash` 的形式；不再先调用对该代理返回 `invalid Socks5 initial handshake` 的 Playwright `APIRequestContext`，避免无效等待后使用错误 CSRF 值。
- **拒绝把 next-auth 自身登录页当作 OpenAI authorize URL**：Web Session signin 现在校验返回地址，明确拒绝 `chatgpt.com/api/auth/signin`、`/api/auth/error` 和 `/auth/login`；首次 signin 未生成 OpenAI authorize 时刷新页面与 CSRF 事务后再以兼容提示重试，避免注册已完成的账号被错误导航回登录页并最终报“Web Session 材料不完整”。
- **修复代理慢响应时注册入口的假超时**：`services/chatgpt_core/any_auto/browser_register.py` 将 OpenAI/ChatGPT 入口和 authorize 导航的完成条件从 `domcontentloaded` 调整为主文档 `commit`，随后独立检查 DOM/表单状态；即使 Playwright 报导航超时，只要页面已提交且注册状态或邮箱表单可读，仍继续当前浏览器事务。备用 authorize 入口复用同一套加固后的 CSRF/signin 实现。
- **阻止已提交注册事务被入口 fallback 重放**：any-auto 页面入口仅在尚未到达邮箱表单时抛出结构化 `_BrowserSignupEntryUnavailable` 并允许回退 ChatGPT authorize；邮箱提交后的状态推进错误会原样失败，不再被宽泛 `except Exception` 当成安全入口故障重放。Web Session 缺失日志改用 `AT状态/Session状态/Cookie状态` 存在性字段，避免 `access_token=no` 等诊断布尔值被日志脱敏器误识别为真实凭证。

### 测试 (Tests)
- 新增 `tests/test_any_auto_web_session_contract.py` 合同覆盖：编码 CSRF cookie、认证 SOCKS5 `APIRequestContext` 禁用、next-auth 自路由拒绝、导航超时后已 commit 页面恢复，以及邮箱提交后禁止 authorize 重放。一次性断网、只读生产依赖容器专项测试通过 `10/10`。
- 扩大运行 `tests.test_browser_registration_flow`：本次涉及的 Web Session/注册事务测试通过；全文件 `52` 条通过，另有 `2` 条 v2.8.46 后仍调用已删除 `_run_browser_registration` 的既有陈旧测试报错，与本次实现无关。

## [2.8.51] - 2026-07-26

### 优化 (Changed)
- **共享动态节点改为字段级保存**：`frontend/src/lib/taskProxySettings.ts` 新增显式字段补丁构建，调用方未提供的动态节点地址、出口国家、IP 保留分钟、探测开关和候选参数不再用前端默认值写回 `/api/config`；动态任务请求也不再隐式携带 `dynamic_proxy_ip_retention_minutes=5` 覆盖共享配置。
- **任务提交与全局动态节点保存解耦**：手机号绑定、单个邮箱测活和批量邮箱测活只提交本次任务的代理参数，不再在任务启动时修改共享配置；全局动态节点继续由代理管理页和明确的注册设置保存动作维护。
- **界面术语统一为“动态节点”**：代理预览、注册、手机号绑定、邮箱测活和 Settings 页面将用户可编辑的动态地址称为动态节点，保留 `dynamic_proxy_template` 后端 key 作为兼容别名。

### 修复 (Fixed)
- **修复 Settings 旧快照覆盖共享动态配置**：`frontend/src/pages/Settings.tsx` 记录加载时的代理字段快照，只提交本次真正发生变化的代理字段；打开旧 Settings 页保存邮箱或其他设置时，不会把动态节点的保留时间回退到 `5`。
- **修复 legacy 代理字段复活 canonical 节点**：`core/task_proxy_config.py` 在部分更新只涉及国家或保留时间时优先保留已有 canonical 动态节点，旧 `task_proxy_url/task_proxy_country_code` 仅在 canonical 为空时兼容回退。

### 测试 (Tests)
- 扩展 `tests/test_task_proxy_config.py` 覆盖 canonical/legacy 冲突下的国家与 retention 部分更新；扩展 `tests/test_settings_persistence_ui.py` 覆盖任务提交不写共享配置、缺省字段不产生默认补丁和 Settings 快照字段过滤。
- 前端生产构建（`cd frontend && npm run build`）通过；专项 Python 回归 `20 passed`。

## [2.8.50] - 2026-07-26

### 优化 (Changed)
- **ChatGPT 注册 Info 改为九阶段详细时间线，不压缩事件密度**：`services/chatgpt_core/task_logging.py` 与 `api/tasks.py` 统一输出 `[ChatGPT注册][尝试 x/y][脱敏邮箱][步骤NN/09 阶段] 状态｜字段=值`。任务策略、指纹、代理/HME 租约、验证码、开户、Web Session、保存、外部同步、单号结果与批次汇总继续逐项展示，但执行器、来源、出口 IP、邮箱回写、占槽和补位等字段统一中文语义，避免原有中英文 key/value、重复 attempt 前缀和散乱标点混排。
- **补齐 any-auto 可运营业务节点**：`access_token_only_registration_engine.py` 从原始 Debug 流中投影邮箱入口提交、密码提交、注册 OTP 提交、`about_you` 提交、OpenAI 账号创建、`https://chatgpt.com/api/auth/session` Web Session 开始/成功等 Info 里程碑；原始 selector、HTTP、callback、cookie presence 等诊断材料仍完整保留在 Debug，不牺牲排障细节。
- **无 RT 外部同步门禁改为明确跳过**：无 RT 账号因缺少 `refresh_token` 不满足外部上传条件时，注册 Info 现在显示 `[SKIP]` 与具体原因，不再在整条注册成功时间线末尾出现误导性的 `[FAIL]`。

### 修复 (Fixed)
- **修复标准化字段分隔符触发 URL 脱敏递归**：日志 URL 识别现在在 `｜/|/，/,/；/;` 字段边界停止，避免 `代理=http://...｜出口IP=...` 被当成一个畸形 URL 后递归脱敏并影响注册任务执行。

### 测试 (Tests)
- 扩展 `tests/test_chatgpt_task_logging.py`、`tests/test_access_token_only_checkout.py` 与 `tests/test_register_task_controls.py`，覆盖九阶段字段格式、邮箱脱敏、动态代理详情、失败/占槽/补位语义、批次附加统计、Info 事件逐条保留、无 RT 上传门禁、any-auto 业务里程碑及原始 Debug 并存；注册相关扩大回归通过 `184 passed, 3 skipped, 4 subtests passed`。
- 全量 `pytest -q tests` 在当前 checkout 主机仍于收集 `tests/test_admin_auth_security.py` 时受既有环境缺少 `argon2` 阻断；本次注册与任务日志专项回归、Python 编译检查及前端生产构建均已通过。
- 侧栏版本同步为 `v2.8.50`。

## [2.8.49] - 2026-07-26

### 修复 (Fixed)
- **修复浏览器 signup callback 被丢弃导致 Web Session bridge 落到邮箱验证页**：`services/chatgpt_core/any_auto/browser_register.py` 不再在 `oauth_callback` 完成分支重新从 `page.url` 构造状态而丢失 `continue_url`，现在保留开户接口返回的 callback，并在同一 Camoufox context 先跟随 OpenAI callback，再访问 `https://chatgpt.com/api/auth/session`。该路径仍属于 GPT signup 收尾，不会启动 Codex OAuth 或额外 OAuth OTP。

### 测试 (Tests)
- 扩展 `tests/test_any_auto_web_session_contract.py`，断言 signup callback 在 Web Session 抓取前被同上下文跟随。

## [2.8.48] - 2026-07-26

### 优化 (Changed)
- **拆分 any-auto GPT 注册与 RT 捕获职责**：`services/chatgpt_core/any_auto/browser_register.py` 的 Camoufox 运输层现在只负责邮箱入口、密码、注册 OTP、`about_you`、开户提交，以及同一浏览器上下文访问绝对地址 `https://chatgpt.com/api/auth/session` 获取 Web Session；不再在注册完成后启动独立 Codex OAuth、二次 OAuth OTP 或 `add_phone` 流程。`oauth_callback` 仅作为 OpenAI 注册完成后的内部 redirect 状态保留。
- **保留有 RT 模式的独立第二阶段**：`chatgpt_registration_mode_adapter.py` 继续由 refresh-token 模式单独执行完整 Auth/RT 捕获；无 RT 模式在拿到 `access_token`、`session_token` 和 cookie 后直接完成账号保存，RT 阶段失败不会重放已经提交的 signup。
- **Web Session 材料完整性与停止传播**：any-auto 结果必须同时包含 `access_token`、`session_token` 和 cookie header；浏览器注册将外层用户资料、停止检查透传到状态机与 Session 等待，手动停止不会被普通异常吞掉。协议 cookie 序列化兼容 curl_cffi `items()/get_dict()` 容器，保留完整 cookie header。

### 修复 (Fixed)
- **修复注册后错误消耗第二个 OTP 预算**：移除浏览器注册运输层中的 `_retry_oauth_fresh_browser` 自动调用，避免第一个账号在共享注册 OTP 预算耗尽、第二个账号继续进入 OAuth 邮箱页超时的连锁失败。
- **修复 Web Session 结果丢失上下文元数据**：access-token 注册引擎现在保留 any-auto 的 Session 捕获 metadata、cookie header 和页面信息，便于账号库存与后续 RT 第二阶段复用同一 Web 会话材料。

### 测试 (Tests)
- 更新 `tests/test_access_token_only_checkout.py` 成功夹具，明确要求完整 Web Session cookie；新增 `tests/test_any_auto_web_session_contract.py`，覆盖绝对 Session API、同上下文 cookie 重读、禁止 Codex OAuth、协议停止传播、完整性门槛及 curl_cffi cookie 序列化。
- 相关注册回归通过：`76 passed`（当前 checkout 未安装 Camoufox 的浏览器集成测试跳过）；全套 `pytest` 仍受环境缺少 `argon2` 的既有依赖阻断。

## [2.8.47] - 2026-07-26

### 修复 (Fixed)
- **any-auto 日文 about_you 误判为生日模式**：JP 表单标签是 `氏名/年齢`，旧探测只认中文「年龄」、不认「年齢」，于是把年龄框当分段生日填写，最终 `400 続けるには有効な年齢を入力してください`。现强制 `input[name=age]` / `年齢` → age 模式，补齐姓名/年龄定位与 native 强制写入，age 模式必须写出数字年龄才允许提交。
- **any-auto 密码页提交无响应（status=0）**：仅 click Continue 在 headless+代理下常不发业务请求。现对齐 staged 兜底：`click → 无业务请求则 form.requestSubmit → 仍无则 Enter`，观察窗延长到 35s，监听 `/api/accounts/user/register` 等业务请求。
- 侧栏版本同步为 `v2.8.47`。

## [2.8.46] - 2026-07-26

### 优化 (Changed)
- **ChatGPT 注册运输层整段替换为 any-auto 三执行器**：`protocol` / `headless` / `headed` 统一走 vendored 包 `services/chatgpt_core/any_auto/`（源对照 `/opt/any-auto-register/platforms/chatgpt`）。
  - 协议：`RegistrationEngine`（curl_cffi 同 session create + NextAuth session AT，Codex RT 可选）
  - 无头/有头：`ChatGPTBrowserRegister`（Camoufox 整段邮箱→OTP→about_you；优先同上下文 Web AT，Codex OAuth 仅作 RT 升级/AT 兜底）
  - 引擎入口：`access_token_only_registration_engine._run_any_auto_registration`
- **注册成功合同收紧**：必须拿到 `access_token` 才算成功；去掉注册主链上的独立 OAuth recovery 二次浏览器、以及 `registered_auth_pending` 半成品当成功落库。邮箱出池 / HME finalize / 库存字段仍由本项目负责。
- 侧栏版本同步为 `v2.8.46`。

## [2.8.45] - 2026-07-26

### 修复 (Fixed)
- **浏览器/协议已有账号分流统一**：浏览器执行器不再绕开 `chatgpt_existing_account_login_route_enabled` 契约。登录路由开启时走 browser OAuth 登录恢复并保存；关闭时确定性已有账号 `SKIP`（HME `keep`、不入库、不占目标身份槽并补下一 attempt）。涉及 `access_token_only_registration_engine.py`、`browser_registration.py`、`registration_route_policy.py`、`api/tasks.py`。
- **取消 login→signup 强制掰回**：邮箱后/密码阶段命中 `login_password` 不再默认点 Sign up 强行恢复注册流；改为按已有账号策略分流。
- **about_you late `account already exists` 结构化兜底**：OpenAI 在密码/OTP 成功后才于 `create_account` 返回已存在时，仍按确定性已有账号处理，避免被任务层当成「结果不确定」失败。
- **任务占槽语义纠偏**：确定性 SKIP（已有账号 / 路由关闭）`consumes_target_slot=0` 且可 backfill；真正不确定的浏览器半注册故障仍 `slot=1` 禁止身份重放。`AttemptResult` 增加 `metadata`（outcome / reason_code / mailbox_action / slot / backfill / certainty）。

### 优化 (Changed)
- **注册 Info/Debug 分层治理**（`task_logging.py`、engine、`api/tasks.py`）：
  - Info 绑定 attempt/脱敏 email，输出 `[账号][代理][邮箱][路由][阶段][已有账号][结果][控制]` 运营可读链路；
  - 低层 selector、raw HTTP、Sentinel、cookies presence 等留 Debug；
  - 业务错误不再误标 `[代理]`；策略横幅每任务一次；
  - 有 task callback 时 engine 避免 Docker 双重打日志。
- authorize/continue 响应优先解析有效 `page.type`（含 `email_otp_send` 归并），与页面 URL/DOM 交叉早分流。

### 测试 (Tests)
- 扩展 `test_access_token_only_checkout.py`、`test_browser_registration_flow.py`、`test_register_task_controls.py`、`test_chatgpt_task_logging.py`：覆盖 browser 已有账号 login recovery / 路由关闭 SKIP 不占槽、late about_you、`page.type` 优先、attempt 上下文与 email mask、不确定失败仍占槽。专项 `94 passed` + browser stub `54 passed`。

## [2.8.44] - 2026-07-25

### 修复 (Fixed)
- **Web Session 桥接已命中 AT 却丢弃再跑 OAuth**：日志出现 `桥接后立即命中 accessToken` 后立刻 `超时未拿到 accessToken`，因为桥接耗时超过 55s 外层 deadline，探测结果未回传。现桥接直接返回 session JSON；deadline 过后仍接受桥接/补拉命中，避免再开一整轮独立 OAuth（+OTP 120s×N）把单号拖到 8–15 分钟。

## [2.8.43] - 2026-07-25

### 修复 (Fixed)
- **about_you 已成功但仍失败 / 重试撞 `user_already_exists`**：真实日志显示 CSRF 桥接已拿到 authorize URL，随后进程在 Web Session 长等待中被中断；HME lease 未 finalize_success，下一轮同一 `ck_*` 再出池 → OpenAI 已注册 → 必 FAIL。现于 **注册状态机完成（about_you/callback）后立刻** 通过 `signup_committed` 回调执行 Helper `finalize_success`，再抓 Web Session；Session 等待缩短为 55s，authorize 落地补 URL 日志与即时 AT 探测。
- 父任务即使在桥接中途被停/重启，lease 也不会再当 ready 脏出池。

### 测试 (Tests)
- `signup_committed` 提前 finalize HME 合同。

## [2.8.42] - 2026-07-25

### 修复 (Fixed)
- **开户完成后 Web Session 桥接 `missing csrfToken`**：真实失败日志为 signup 已到 `oauth_callback` / about_you，但 `fetch('/api/auth/csrf')` 常返回 HTTP 200 空 body，桥接直接失败，长时间空转后任务被停。现对齐注册入口实现：
  1. 用 `_browser_fetch` + `context.request` 取 CSRF；
  2. 仍失败则解析 `__Host-next-auth.csrf-token` cookie（`token|hash` 取前半）；
  3. `signin/openai` 同样支持 cookie-jar 回退；
  4. reload 后重试一次 CSRF。
- **入口 `Page.goto` 超时误标 late_failure**：未发码前的注册入口超时 / 邮箱页失败纳入 HME `early_failure`，避免白白烧掉 ready lease。

### 测试 (Tests)
- CSRF API 空 body 时 cookie 回退桥接合同。

## [2.8.41] - 2026-07-25

### 修复 (Fixed)
- **注册成功率被 `missing_web_session` 大量吃掉**：Camoufox 已完成 OTP/about_you（甚至已有 `__Secure-next-auth.session-token`），但同上下文 `/api/auth/session` 拿不到 `accessToken` 时，阶段结果 `ok=False` 会直接 **FAIL + HME late_failure**，既不走独立 OAuth 补抓，也不落 `registered_auth_pending`。现把 `browser_registration_missing_web_session` 识别为 **开户已完成**：
  1. 继续跑独立浏览器 OAuth 补 AT/RT；
  2. 仍无 AT 时 **成功落库 auth_pending**（邮箱/密码/session_token/cookies 保留，Helper finalize_success）；
  3. 不再把已开户身份当注册失败丢掉。
- **Web Session 抓取加硬**：首页 `NS_BINDING_ABORTED` 后 settle/commit 重试；`WARNING_BANNER` 空会话更早触发 next-auth 桥接，并允许一次桥接重试；超时窗口 75s→100s。

### 测试 (Tests)
- `missing_web_session` → OAuth 补抓失败后 `registered_auth_pending` 成功落库合同。

## [2.8.40] - 2026-07-25

### 修复 (Fixed)
- **OTP 提交 200 后仍停在验证码页 → 二次收码把同码永久排除**：浏览器注册把验证码在「取出时」就写入 `used_codes`；若 `email-otp/validate` 返回 2xx 但 SPA 未离开 OTP（或 OpenAI 重发同一串数字），下一轮收码会命中「解析到验证码但在排除列表中已跳过」并耗尽 120s/90s 预算。现增加 `release_code`：提交未推进时释放该码；提交后额外 settle/重试一轮，避免误杀可用验证码。
- **about_you `Page.goto: NS_BINDING_ABORTED`**：OTP 成功后 Auth SPA 常已在导航到 `/about-you`，硬 `page.goto` 与 SPA 竞态会直接 FAIL。改为 `_ensure_about_you_page`：先看 DOM/URL，goto 遇 `NS_BINDING_ABORTED` 则 settle 并在页面已可用时继续填写，不再整单失败。
- **validate 响应 URL 含 `email-otp` 被误判为仍在 OTP**：`_success_result` 对 `email_otp_send/validate` 与 API URL 统一按 OTP 态处理，并优先采用页面 live DOM 的下一状态。

### 测试 (Tests)
- `release_code` 可复用合同；`NS_BINDING_ABORTED` 后 about_you 可继续。

## [2.8.39] - 2026-07-25

### 修复 (Fixed)
- **TempMail 里已有验证码邮件却等满 120s**：HME Ready → TempMail 转发箱链路本身可解析（`+gpt1` 的 Return-Path 匹配与 6 位码提取正常），问题在浏览器注册的 `otp_sent_at` 截止时间。密码提交后 SPA 若卡住数十秒才进入 OTP 页，旧逻辑只在 `otp_triggered=true` 时保留密码提交时间戳，否则回退 `now-8s`，把**已投递到 TempMail 的首封 OTP** 当成旧邮件静默丢掉；只有重发后的第二封才能命中。现改为：
  1. 密码 `/user/register` 成功响应**始终**带出并保留 `otp_sent_at`（含 `email_otp_send` / 通用 SPA 跳转）；
  2. 无明确发码时间时的回退宽限由 8s 提升到 **60s**（`OTP_SENT_AT_FALLBACK_GRACE_SECONDS`）；
  3. 任务日志增加「早于 otp_sent_at 被跳过 / 别名匹配但解析失败」诊断，区分「没信」与「有信被时间窗滤掉」。
- **HTML-only OpenAI 邮件**：转发箱 `body_text` 常为空时，额外用 `_decode_raw_content` 解码 raw MIME 再提取验证码，降低 QP/HTML 噪音下的漏提。

### 测试 (Tests)
- 密码提交后即使 `otp_triggered=false` 仍保留早期 `otp_sent_at` 传给收码回调；主动发码路径的 fallback grace 合同同步为 60s。

## [2.8.38] - 2026-07-25

### 修复 (Fixed)
- **浏览器注册验证码只等一轮就 FAIL**：Camoufox 路径在出现 OTP 输入框后只调用一次收码（默认 120s），**没有**协议模式的「超时 → 重发 → 再等」。OpenAI 慢投递或首次发码丢失时直接 `未获取到验证码`，预算里的 `resend_wait=90s` 从未用上。现对齐协议：
  1. 首次等待 `otp_wait_timeout`（默认 120s）；
  2. 未收到则 **页面 Resend 按钮或 `email-otp/send` API 重发**；
  3. 再等 `otp_resend_wait_timeout`（默认 90s）。
- **收码可观测性**：TempMail 转发箱若有 ChatGPT/验证码类主题邮件但 **+tag 运输头未匹配** 当前 HME 别名，写入任务日志，区分「没信」与「有信未匹配」。

## [2.8.37] - 2026-07-25

### 修复 (Fixed)
- **Camoufox `InvalidIP`/ipecho 不再整单杀死注册**：代理模式下启动前尽量多源探测出口 IP 并写入 `geoip=<ip>`；若 Camoufox 仍因 `ipecho.net`/ipify SSL 失败抛 `InvalidIP`，自动 **关闭 geoip 降级重试**，避免「浏览器还没开 OpenAI 就 FAIL 并占身份槽」。
- **HME Helper 失败 outcome 收紧**（`core/base_mailbox.py`）：
  - `InvalidIP` / geoip / 纯 CSRF·首页失败 → `early_failure`（可干净回收）；
  - `user_already_exists` / `login_password` /「邮箱已存在」→ **`keep` 永久退役**，禁止同 lease 再当 ready 出池；
  - 手动停任务 / OTP·密码后失败 → `late_failure`（不确定半开户）。
- **任务中断强制 finalize HME lease**：`AccessTokenOnlyRegistrationEngine` 捕获 `TaskInterruption` 时，若已领取别名则必定 `finalize_failure`，修复此前密码 200/OTP 中途点停止导致 **同 `ck_*` lease 再次出池 → 必撞已存在** 的脏池问题；成功/失败 finalize 加幂等门闩防双写。

### 测试 (Tests)
- Helper outcome 分桶与 `keep`/`early_failure` finalize 合同；Camoufox geoip 失败识别合同。

## [2.8.36] - 2026-07-25

### 变更 (Changed)
- **一键回归 v2.8.29 注册保存模型**：从提交 `e453647` 恢复注册核心链路（`access_token_only_registration_engine` / `chatgpt_client` / `oauth_client` / `plugin` / `refresh_token_registration_engine` / `chatgpt_registration_mode_adapter` 及对应专项测试），撤销 `v2.8.30` 起「会话齐套才 success」对业务落库的过严门闩。
- **恢复「开户完成即可落库」语义**：浏览器 signup 完成后若 Web AT 暂缺，先走独立 Camoufox OAuth recovery 补 Token；仍无 AT 时以 `registered_auth_pending` + `needs_auth_capture` **成功落库**（邮箱/密码/画像保留，禁止同邮箱 signup 重放），不再整单记 FAIL 并占用身份槽后空手而归。
- **有意保留的后续修复（不整树回退）**：
  - `browser_registration.py` 保持 v2.8.32–v2.8.35（OTP 导航竞态、Web Session 桥接、`/api/auth/callback` OAuth 可导航）；
  - 代理国家空串 / 无可用候选致命停（v2.8.34）保留。
- 侧栏版本同步为 `v2.8.36`。

## [2.8.35] - 2026-07-25

### 修复 (Fixed)
- **Camoufox about_you 成功后误报「未支持的注册状态: page=external_url」**：US/部分出口在 about_you `200` 后 `continue_url` 为 `https://chatgpt.com/api/auth/callback/openai`。状态机此前把所有含 `/api/` 的 continue 当成「内部协议 API」禁止 `page.goto`，于是开户已完成却立刻 `RuntimeError: 未支持的注册状态: page=external_url`，身份槽被白白占用。现区分：
  - **可导航的浏览器 OAuth/next-auth 回调**（`/api/auth/callback`、`platform.openai.com/auth/callback` 等）必须跟随；
  - **auth.openai.com `/api/accounts/*` 等状态机 API** 仍禁止当页面打开。
- 影响文件：`services/chatgpt_core/browser_registration.py`（`_is_oauth_browser_callback_url` / `_is_internal_auth_api_continue_url` / `_requires_registration_navigation` 与状态机导航分支）；侧栏版本 `v2.8.35`。

### 测试 (Tests)
- `tests/test_browser_registration_flow.py` 增补 ChatGPT OAuth callback external_url 必须导航、platform callback 可导航、accounts API 仍拦截的合同用例。

## [2.8.34] - 2026-07-25

### 修复 (Fixed)
- **代理池国家留空被静默改成 JP**：`resolve_task_proxy_candidates` 对表单显式提交的空 `proxy_country_code` 再跳过并回落到全局/默认 JP。现改为：字段存在则尊重空串（pool=不限）；dynamic 留空直接报错「必须填写出口国家」，不再硬编码 JP。
- **无可用代理时无限刷 FAIL**：注册任务 `attempt_cap=0` 时失败会一直补尝试（日志出现数百条「代理池没有可用候选」）。默认补尝试上限改为 `min(100, count*3)`；将「代理池/动态代理没有可用候选」列为致命错误并停止任务；启动前代理预检失败立即结束。
- **前端**：任务代理默认国家改为空；`normalizeTaskProxySettings` 允许显式空国家，不再被默认 JP 覆盖。

## [2.8.33] - 2026-07-25

### 修复 (Fixed)
- **Camoufox 注册收尾抓 Web Session**：about_you/callback 常落在 `platform.openai.com`，仅靠 `chatgpt.com/api/auth/session` 会只剩 `oai-did`。现在在同一浏览器上下文内导航 ChatGPT 首页，并通过 **next-auth `signin/openai` 桥接**把 OpenAI 登录态铸成 ChatGPT `accessToken`/`session_token`，超时与 Cookie 摘要写进任务日志。
- **缺 AT 时保留 Cookie 并协议补抓**：浏览器 signup 完成但 session 读空时返回 `browser_registration_missing_web_session`（不算业务成功），同时把 Cookie 交给 `AccessTokenOnlyRegistrationEngine` 合并进协议 Session 再 `reuse_session_and_get_tokens` 二次取 AT。

## [2.8.32] - 2026-07-25

### 修复 (Fixed)
- **Camoufox 浏览器注册导航竞态**：密码提交后 API 返回 `email_otp_send` + `/api/accounts/email-otp/send` 时，状态机不再 `page.goto` API URL；改为 settle 后按 OTP 阶段处理，避免 `Page.evaluate: Execution context was destroyed`。
- **`_browser_fetch` / 页面状态探测**：`page.evaluate` 增加导航重试与 settle；邮箱填写三次重试；about_you 提交后等待 SPA 稳定再读状态。
- **OTP 发码**：若页面已出现 OTP 输入框则跳过 `email-otp/send` 重放，减少与 SPA 导航冲突。

### 测试 (Tests)
- 新增 `email_otp_send` 禁止页面导航、API continue_url 不 goto 的合同用例。

## [2.8.31] - 2026-07-25

### 修复 (Fixed)
- **方案 R：协议 create 对齐 any-auto-register**：`sentinel_token.py` 接回 `sentinel_vm.solve_turnstile_dx`，存在 turnstile `dx` 时解出非空 `t`（不再固定空串）；`ChatGPTClient` 协议模式仅 HTTP PoW+VM，禁止为 Sentinel/create 启动浏览器。
- **create 前 `client_auth_session_dump` + signup continue + 密码页预热**：`register_complete_flow` / `register_user` / `_create_account_via_protocol` 对齐 any-auto 同 session 状态机；`oauth_client` 协议 about_you 同样 dump 后 create。
- **三执行器硬隔离恢复**：`access_token_only_registration_engine` 对 `protocol` 走 `register_complete_flow`，`headless`/`headed` 走整段 Camoufox `run_browser_registration_stage`，失败不跨 transport 兜底；RT 引擎浏览器模式复用同一 AT-only 浏览器运输层。
- **成功门闩加固**：注册 success 必须齐套 access_token + session_token + cookies/cookie_header + account_id；禁止 synthetic `v2_acct_*` 冒充 account_id。
- **`registration_disallowed` 同身份不 3 连**：默认 `max_retries=1`，`_should_retry` 将 disallowed / create 400 / sentinel 不可用标为不可重试。
- **任务日志可见性**：create 失败、dump、disallowed、transport 关键字强制 INFO，避免仅 DEBUG 不可见。
- **强密码合同补回**：`plugin._generate_chatgpt_registration_password` 必含大小写/数字/符号。

### 测试 (Tests)
- `test_sentinel_protocol_vm` 转绿；`test_sentinel_browser` 协议 create dump 合同；注册/插件/任务控制/浏览器流等专项共 130 passed。

## [2.8.30] - 2026-07-25

### 变更 (Changed)
- **注册链路整段回退到 2026-07-18 提交 `b880955` 的保存模型**：从该提交恢复 `access_token_only_registration_engine.py`、`refresh_token_registration_engine.py`、`chatgpt_registration_mode_adapter.py`、`chatgpt_client.py`、`oauth_client.py`、`sentinel_token.py`、`plugin.py`，以及对应注册专项测试。
- **恢复“会话材料齐套才算注册成功”的落库语义**：开户后必须 `reuse_session_and_get_tokens` 拿到 Web AT / session_token / cookies / account_id 后才 `success` 并进入 `save_account`；撤销 7/24 之后的 `registered_auth_pending` 空壳成功入库，以及浏览器独立 OAuth recovery 作为注册收尾的路径。
- **与 plus free+仅 AT 历史库存对齐**：注册成功默认继续产出 access_token + session_token + cookies + password/email 的 Web Session 包；`refresh_token`/`id_token` 不作为注册成功条件。RT 两阶段代码仍保留为可选（与 7/18 一致），Stage1 无 AT 时失败而非落空壳。
- **兼容保留**：`sentinel_browser.py` 保持当前版本，避免手机号注册 / GoPay 等仍依赖后续 API 的模块被连带回退。侧栏版本同步为 `v2.8.30`。

### 测试 (Tests)
- 同步恢复 7/18 的 `test_chatgpt_registration_mode_adapter` / `test_access_token_only_checkout` / `test_chatgpt_register` / `test_register_task_controls`；上述 89 项通过。

## [2.8.29] - 2026-07-25

### 修复 (Fixed)
- **协议注册对齐 any-auto-register 的 create_account 成功路径**：`services/chatgpt_core/sentinel_token.py` 引入 `sentinel_vm.py`（自 any-auto 移植），在协议 Sentinel 拿到 `turnstile.dx` 时用 VM 解出真实 `t`，不再固定空串提交 `oauth_create_account` / `username_password_create`。
- **create_account 前补 `client_auth_session_dump`**：`ChatGPTClient._create_account_via_protocol` 与 `OAuthClient._submit_about_you_create_account_via_protocol` 在开户 POST 前推进 auth 状态机，请求体改为与 any-auto 一致的紧凑 JSON。
- **协议状态机补齐 signup continue + 密码页预热**：authorize 后若未进入密码/OTP/about_you，显式 `authorize/continue`（`screen_hint=signup`）；密码提交前预加载 `/create-account/password` 并刷新 Sentinel，减少直接落到 `email-verification` 跳过密码段、随后 `registration_disallowed` 的情况。

### 测试 (Tests)
- 新增 `tests/test_sentinel_protocol_vm.py`，覆盖 turnstile `t` 填充与无 dx 回退；扩展 `test_protocol_create_account` 断言 `client_auth_session_dump`。侧栏版本同步为 `v2.8.29`。

## [2.8.28] - 2026-07-25

### 变更 (Changed)
- **撤销 v2.8.27 回退，恢复上一版 v2.8.26 运行态**：`git revert` 掉 `fec1d28`（曾整段撤掉 v2.8.24–v2.8.26 注册改造），注册三执行器硬分派、Camoufox 密码 staged 提交、身份槽与禁止 signup 重放、OTP/about-you committed 合同等全部恢复为 v2.8.26 时的代码与测试。侧栏版本记为 `v2.8.28`，避免与已下线的 v2.8.27 混淆。

## [2.8.27] - 2026-07-25 (已于 v2.8.28 撤销)

曾整段回退 v2.8.24–v2.8.26 注册改造；**已在 v2.8.28 撤销该回退**。

## [2.8.26] - 2026-07-25

### 修复 (Fixed)
- **封死浏览器注册的身份分叉与结果不确定重放**：`api/tasks.py` 在 `executor_type=headless/headed` 进入 `Platform.register()` 后，失败不再切换代理候选、换邮箱或换指纹补位，并通过 `core/task_runtime.py` 的 `consumes_target_slot` 将不确定失败计入目标身份槽预算；`count > 1` 时仍允许跑用户原本请求的其他身份槽。`protocol` 的代理 failover 与补尝试语义保持不变。
- **Camoufox 完整 signup 单次执行**：`services/chatgpt_core/access_token_only_registration_engine.py` 对普通浏览器开户强制 `registration_max_retries=1`，禁止默认 `max_retries=3` 对结果不确定的 signup 整流程重放；纯协议与显式已有账号浏览器登录仍保留原重试。
- **密码提交改为受控 staged 降级**：`services/chatgpt_core/browser_registration.py` 引入 `_NetworkActivityObserver` 与 `_PasswordFormSubmission`，按 `真实 click → 无业务请求再 requestSubmit → 再无请求才 Enter` 推进，并在 Sentinel/Cloudflare 进行中延长观察。注册与 OAuth 密码页均以业务 API 响应（`/api/accounts/user/register`、`/api/accounts/password/verify`）为准推进，`2xx` 后等待 SPA 离开旧密码 DOM，不再在 click 异常后立即 `requestSubmit`。
- **OTP / about-you 禁止非幂等重放**：OTP 在填充前安装网络观察器，自动提交后不再重复点击 Continue；`2xx/204` 记为 `otp_committed`/`signup_committed` 后有界等待页面推进。请求已发出但 response 丢失时，不再走 API fallback 重放验证码，也不再用新 invocation ID 重建 `create_account`。`signup_committed` 后的 add-phone 页按注册完成处理，便于后续严格浏览器 OAuth。

### 优化 (Changed)
- **注册入口等待与 passwordless 单次点击**：邮箱入口等待超时放宽到 60 秒，passwordless 一次性验证码入口只点击一次并记录 `otp_sent_at`，避免循环点击造成重复发码竞态。

### 测试 (Tests)
- 扩展 `tests/test_browser_registration_flow.py`、`tests/test_register_task_controls.py`、`tests/test_access_token_only_checkout.py`，覆盖密码 staged 提交、OTP/about-you committed、浏览器失败占用身份槽、协议 failover 不受影响、engine 单次 signup。侧栏版本同步为 `v2.8.26`。

## [2.8.25] - 2026-07-25

### 优化 (Changed)
- **浏览器注册优先采用已验证的 OpenAI 页面入口**：`services/chatgpt_core/browser_registration.py` 现在与参考实现一致，先从 `platform.openai.com/login` 在同一 Camoufox 上下文推进邮箱与 passwordless OTP，页面入口不可用时才回退 ChatGPT authorize。一次性验证码入口只点击一次，后续仅观察状态，避免循环点击造成重复发码竞态；整个过程仍保持 `headless/headed` 浏览器 transport，不接入协议 Session。

### 修复 (Fixed)
- **修复随机弱密码导致注册页原地不动**：`services/chatgpt_core/plugin.py` 不再从混合字符池无约束抽取 16 位密码，改为使用 `secrets` 强制生成不少于 12 位且必含大写、小写、数字和符号的密码。该规则补齐了从 `/opt/any-auto-register` 移植浏览器流程时遗漏的 ChatGPT 专用密码合同，避免命中 `/create-account/password` 时因缺少字符类别被前端校验拦截。
- **按真实注册响应推进密码阶段**：密码提交前监听同一浏览器上下文的 `/api/accounts/user/register` response 与 request failure；`2xx` 即使 SPA URL 未变化也按返回的 `page.type` 进入 OTP，`4xx` 保留服务端错误，不再统一等 20 秒后伪装成 `status=0`。页面状态识别改为可见 OTP/about-you 优先，不会再被 SPA 遗留的隐藏 password input 拉回密码阶段；浏览器原生 validity 与关联错误节点也会进入失败原因。

### 测试 (Tests)
- 扩展 `tests/test_chatgpt_plugin.py` 与 `tests/test_browser_registration_flow.py`，覆盖密码字符类别、显式密码保持、page-first/authorize fallback、passwordless 单次点击、隐藏密码 DOM、密码注册 `2xx` URL 不变及 `4xx` 错误传播。插件、Camoufox 浏览器流、注册模式、AT-only 与任务控制专项回归均通过；侧栏版本同步更新为 `v2.8.25`。

## [2.8.24] - 2026-07-25

### 优化 (Changed)
- **建立 ChatGPT 三执行器硬分派合同**：`services/chatgpt_core/access_token_only_registration_engine.py`、`chatgpt_client.py`、`oauth_client.py`、`browser_registration.py` 与 `sentinel_browser.py` 现在将 `protocol`、`headless`、`headed` 作为互斥执行器。纯协议注册只使用 `curl_cffi` 与 HTTP Sentinel PoW，Cloudflare、Sentinel 或协议状态机失败时原样失败；浏览器注册从第一步直接进入独立 Camoufox 上下文，不再先跑协议注册、回灌浏览器 Cookie 到 curl Session，或在失败后由另一执行器接管。显式无头/有头选择不会再被运行环境变量改写，未知执行器直接拒绝。
- **统一浏览器注册与 RT 两阶段的 transport 所有权**：浏览器注册上下文在关闭前直接读取 `/api/auth/session` 并返回 Web AT、Session、Cookie 与账号身份；需要 RT 时，第二阶段继续使用严格 Camoufox OAuth，只保留标准 authorization-code exchange，不允许 curl OAuth 状态机接管。注册阶段遇到 `login_password` 只报告已有账号，必须显式使用“已有账号抓取”，不会从 signup 偷偷切换登录恢复。
- **补齐执行器运行审计**：账号注册上下文与 `extra` 记录 requested/effective executor、registration transport、各阶段 transport 以及 Camoufox 实测 UA/device profile。浏览器账号不再被任务层预生成的 Chrome fingerprint 覆盖；协议账号继续保留稳定的账号级指纹与签名。
- **统一前端任务级执行器选择**：`RegisterTaskPage.tsx` 与账号页 `RegisterTaskModal.tsx` 共享三项互斥说明，注册画像保存并恢复 `executor_type`，任务提交使用表单当次选择而非全局默认。异步加载配置时会检测字段是否已被用户触碰，避免响应返回后覆盖刚选择的执行器。

### 修复 (Fixed)
- **固定单次注册身份与本地化资料填写**：同一 account attempt 的整流程重试固定邮箱、密码、姓名、生日、device ID 与指纹种子，不再重试一次就换一套人物资料。Camoufox about-you 表单补齐日文 `氏名` / `年齢` 识别，年龄输入框填写实际年龄，不再误填完整生日字符串。
- **安全保存“已注册但认证待补抓”账号**：浏览器已完成 signup 但暂未拿到 AT/RT 时，使用 `registered_auth_pending`、`needs_auth_capture` 和独立 auth level 落库，禁止重放同邮箱 signup，也不再伪造远端 account ID。RT adapter 会完成真实邮箱/HME finalize；任务快照、日志与 HME 重跑队列明确区分远端注册完成和认证可用，且不会把本地账号 ID 误判为已保存 AT。
- **防止 pending 覆盖有效账号**：`core/db.py::save_account()` 在同邮箱已有有效认证材料时保留原密码、AT/RT/ID Token、真实 account/workspace、状态与指纹，只追加有界的 pending 审计事件，避免一次补抓失败清空现有可用账号。
- **封死空认证材料的外部上传旁路**：`services/chatgpt_account_state.py` 新增 `blocked_missing_at`，Sub2API、OAIPay backfill 与低层上传器、CPA 直传 API 和平台 action 都在网络请求前执行中央 gate，并独立拒绝缺 AT/RT 的 payload；注册后的 pending 账号不会触发本地测活或自动上传。

### 测试 (Tests)
- 新增/扩展执行器、Browser Web Session、严格 browser OAuth、protocol fail-closed、RT transport 保持、retry 身份固定、日文年龄、实际执行器审计、pending 落库与 HME 状态、同邮箱有效凭据保护、Sub2API/OAIPay/CPA 零网络 gate、任务级指纹隔离及前端画像竞态合同。后端专项测试、含 Camoufox 的镜像测试、前端 Node 测试和生产构建均通过；侧栏版本同步更新为 `v2.8.24`。

## [2.8.23] - 2026-07-25

### 优化 (Changed)
- **再次同步 Team 账单国家至上游官方 Checkout 目录**：`services/chatgpt_core/payment_link_cache.py` 对齐 `openai-pay-long-link@dde6121` 的 `2026-07-25` ChatGPT Checkout 快照，账单目录从 74 项扩展为 232 个国家/地区和 39 种 Checkout 配置币种。币种不再按 ISO 本币推断，例如玻利维亚改为 `BO / USD`，同时同步 `AR / USD`、`IS / EUR`、`TR / USD`、`UA / USD`，新增 `XK / EUR`，并移除官方账单目录未开放的 `HK`、`UM`；旧客户端提交的冲突币种仍由账单国家重新计算。账单目录与动态 IP 代理目录继续独立，本次不会把 232 个账单地区扩散到代理国家选择。

### 测试 (Tests)
- 将 Team 账单目录合同升级为完整目录哈希、232 项数量、39 种币种集合和关键国家映射校验，新增 `HK/UM` 拒绝、`XK` 接受、`BOB -> USD` 重算、profile 冻结及 long-link 客户端回归；前端侧栏版本同步更新为 `v2.8.23`。

## [2.8.22] - 2026-07-25

### 优化 (Changed)
- **Team 账单国家目录对齐 long-link 上游映射**：`services/chatgpt_core/payment_link_cache.py` 将 Team 账单国家从本地 35 项扩展为与 `openai-pay-long-link` 当前 `COUNTRY_CURRENCY` 合同一致的 74 项，并由 `api/tasks.py` 的支付 profile 接口统一下发浏览器；`frontend/src/pages/Accounts.tsx` 删除独立国家/币种表，只消费服务端目录。玻利维亚现在可选择为 `BO / BOB`，请求校验、profile 冻结、变体缓存和生成历史使用同一国家本币，不再把旧客户端提交的冲突 `currency` 当作有效覆盖。

### 修复 (Fixed)
- **修复注册任务启动覆盖已保存邮箱画像**：`frontend/src/pages/Accounts.tsx` 的启动路径现在合并保存数量、并发和邮箱草稿，不再用缺少 `mail_provider_override`、TempMail 模式及域名的精简对象覆盖显式保存的浏览器画像；再次打开注册面板时，已选择的 HME Ready/TempMail 域名画像保持不变。
- **补齐其他面板的设置保存闭环**：`frontend/src/pages/Proxies.tsx` 现在会把动态代理“实测出口”开关连同模板、出口国家和 IP 保留时长一起写入全局配置；账号页手机号绑定的号段模式、只测收发码和同号复用联动值在通过 `setFieldsValue` 变更时也会立即写入浏览器画像，避免关闭弹窗后恢复旧值。`frontend/src/pages/Accounts.tsx` 同时兼容旧 localStorage 中的字符串布尔值，避免 `"false"` 被错误解释为开启。
- **修复注册面板邮箱画像保存失效**：`frontend/src/pages/Accounts.tsx` 现在会在用户显式点击“保存设置”时持久化本任务邮箱服务选择、TempMail 建箱模式和固定域名列表，并写入显式保存标记；注册弹窗重新打开时仅在存在这份新画像时恢复对应值，否则继续以 `/api/config` 的共享默认配置为准，旧版本残留的 localStorage 字段不会越权覆盖服务端设置。这样选择 `HME Ready API` 后不会再次被共享的 TempMail 域名画像覆盖，同时“开始注册”仍只携带任务级 `extra`，不会偷偷改写三实例共享配置。
- **明确邮箱选择的作用域**：`frontend/src/features/auth/components/RegisterTaskModal.tsx` 将邮箱字段标为“本任务默认”，并提示保存后的浏览器画像与设置页全局默认相互独立，避免把一次任务覆盖误认为全局配置保存。

### 测试 (Tests)
- 扩展 Team 支付、long-link 客户端和账号页合同测试，完整锁定 74 项上游国家本币映射、`BO / BOB` 参数与缓存变体、旧 `country/currency` 清理，以及前端仅从 profile 目录生成账单国家选项；Python 专项测试与前端生产构建通过，侧栏版本同步更新为 `v2.8.22`。
- 增加其他设置面板的保存合同断言，覆盖动态代理探测开关、手机号绑定联动字段的显式画像写入，以及旧字符串布尔值的归一化。
- 扩展 `tests/test_registration_profile_ui.py`，覆盖显式保存邮箱服务/域名画像、弹窗恢复画像以及全局配置不被任务启动反写的前端合同。

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

## 2026-07-24 20:47:23 +0800
- 修复注册面板邮箱画像保存与 HME 默认选择
- 发布模式: multi

## 2026-07-24 20:53:10 +0800
- 隔离旧注册画像并稳定 HME 默认邮箱
- 发布模式: multi

## 2026-07-24 21:18:25 +0800
- 修复代理与账号其他面板设置保存覆盖问题
- 发布模式: multi

## 2026-07-24 21:30:20 +0800
- 修复注册启动覆盖已保存邮箱画像
- 发布模式: multi

## 2026-07-25 00:42:36 +0800
- 对齐 Team 账单国家与上游本币映射
- 发布模式: multi

## 2026-07-25 01:23:15 +0800
- 同步 Team 账单国家至上游官方 Checkout 目录
- 发布模式: multi

## 2026-07-25 03:37:00 +0800
- 修复 ChatGPT 注册三执行器串线并统一浏览器注册链路
- 发布模式: multi

## 2026-07-25 04:10:05 +0800
- 修复 Camoufox 注册密码校验与页面状态推进
- 发布模式: multi

## 2026-07-25 05:59:00 +0800
- 修复浏览器注册密码提交与身份槽重放合同 v2.8.26
- 发布模式: multi

## 2026-07-25 05:59:07 +0800
- 修复浏览器注册密码提交与身份槽重放合同 v2.8.26
- 发布模式: multi

## 2026-07-25 06:44:47 +0800
- 撤销 v2.8.27，恢复 v2.8.26 注册代码 v2.8.28
- 发布模式: multi

## 2026-07-25 07:07:41 +0800
- 协议注册对齐 any-auto：Sentinel VM 解 t + client_auth_session_dump + signup continue
- 发布模式: multi

## 2026-07-25 07:48:45 +0800
- v2.8.30: 注册链路回退到 7/18 b880955 保存模型（会话齐套才落库）
- 发布模式: multi

## 2026-07-25 08:36:09 +0800
- v2.8.31 方案R：any-auto协议create对齐+三执行器Camoufox隔离+Web Session齐套门闩
- 发布模式: multi

## 2026-07-25 08:58:36 +0800
- v2.8.32 修复Camoufox密码后OTP导航竞态与API continue_url误goto
- 发布模式: multi

## 2026-07-25 09:15:59 +0800
- v2.8.33 修复Camoufox收尾Web Session抓取与协议Cookie补抓
- 发布模式: multi

## 2026-07-25 09:23:38 +0800
- v2.8.34 修复代理池国家留空误选JP与无可用IP无限重试
- 发布模式: multi

## 2026-07-25 09:58:27 +0800
- v2.8.35 修复Camoufox about_you后chatgpt OAuth callback被/api误拦
- 发布模式: multi

## 2026-07-25 10:08:55 +0800
- v2.8.36 一键回归v2.8.29注册保存模型（auth_pending落库+保留浏览器补丁）
- 发布模式: multi

## 2026-07-25 10:46:34 +0800
- v2.8.37 修复HME脏lease复出+停任务强制finalize+Camoufox InvalidIP降级
- 发布模式: multi

## 2026-07-25 11:04:34 +0800
- v2.8.38 浏览器注册OTP超时后自动重发再等
- 发布模式: multi

## 2026-07-25 11:39:32 +0800
- v2.8.39 修复浏览器注册otp_sent_at过紧导致TempMail已有验证码被跳过
- 发布模式: multi

## 2026-07-25 12:44:33 +0800
- v2.8.40 修复OTP提交后同码被排除与about_you NS_BINDING_ABORTED
- 发布模式: multi

## 2026-07-25 19:47:19 +0800
- v2.8.41 missing_web_session开户完成后走OAuth或auth_pending落库提升成功率
- 发布模式: multi

## 2026-07-25 20:22:34 +0800
- v2.8.42 修复Web Session桥接missing csrfToken与入口超时early_failure
- 发布模式: multi

## 2026-07-25 20:51:04 +0800
- v2.8.43 about_you完成后提前finalize HME并缩短Web Session等待
- 发布模式: multi

## 2026-07-25 21:20:38 +0800
- v2.8.44 桥接命中AT不再丢弃避免二次OAuth拖慢
- 发布模式: multi

## 2026-07-26 01:58:48 +0800
- v2.8.45 统一已有账号分流并重构注册Info/Debug日志
- 发布模式: multi

## 2026-07-26 04:06:04 +0800
- v2.8.46: 注册运输层整段替换为 any-auto 三执行器 (protocol/headless/headed)
- 发布模式: multi

## 2026-07-26 04:25:09 +0800
- v2.8.47: 修复 any-auto 日文年龄 about_you 与密码页 staged 提交
- 发布模式: hot

## 2026-07-26 06:20:53 +0800
- v2.8.48: 分离 any-auto GPT signup 与 Web Session/RT 捕获
- 发布模式: multi

## 2026-07-26 06:31:07 +0800
- v2.8.49: 跟随 signup callback 后再抓 Web Session
- 发布模式: multi

## 2026-07-26 07:24:44 +0800
- v2.8.50: 统一注册 Info 九阶段详细时间线
- 发布模式: multi

## 2026-07-26 09:31:45 +0800
- 修复共享动态节点配置字段覆盖与任务代理默认回写
- 发布模式: multi

## 2026-07-26 10:29:18 +0800
- 修复 Settings 共享配置开关确认弹窗
- 发布模式: multi

## 2026-07-26 11:09:41 +0800
- 补齐本地配置发布为共享并启用当前实例
- 发布模式: multi

## 2026-07-26 12:15:47 +0800
- v2.8.54 修复浏览器注册 CSRF 桥接与导航假超时
- 发布模式: multi

## 2026-07-26 12:27:35 +0800
- v2.8.55 修复浏览器早期失败误占目标槽
- 发布模式: multi

## 2026-07-26 16:51:22 +0800
- v2.8.56 修复 Web Session 桥接 authorize 假超时
- 发布模式: multi

## 2026-07-27 11:28:08 +0800
- v2.8.57 修复大批量本地状态校验启动上限
- 发布模式: multi

## 2026-07-27 11:31:10 +0800
- v2.8.57 修复主实例 solver 端口冲突
- 发布模式: multi

## 2026-07-28 03:07:24 +0800
- 修复注册第二阶段 OAuth 验证码适配器契约
- 发布模式: multi

## 2026-07-28 05:37:02 +0800
- 注册与 Auth 解耦，注册仅保存 signup AccessToken/Web Session/Cookie
- 发布模式: multi

## 2026-07-28 06:14:04 +0800
- 补齐注册未请求 Auth 的审计字段持久化
- 发布模式: multi

## 2026-07-28 06:40:10 +0800
- v2.8.61 收敛 ChatGPT 注册日志主线
- 发布模式: multi

## 2026-07-28 08:15:44 +0800
- 重构 ChatGPT 注册成功位日志与网络 Debug 追踪
- 发布模式: multi

## 2026-07-28 08:19:56 +0800
- 原子化注册成功位快照时序
- 发布模式: multi

## 2026-07-28 08:22:32 +0800
- 阻止注册成功位越过目标数
- 发布模式: multi

## 2026-07-29 05:50:47 +0800
- 增加 HME Tag 长度实验字段的任务级透传与回归测试
- 发布模式: multi

## 2026-07-29 23:37:47 +0800
- 手机号绑定支持号段内号码状态筛选与全量不可用复测
- 发布模式: multi

## 2026-07-30 08:58:03 +0800
- 停止 Idea 本地中断任务的后台订单轮询
- 发布模式: multi

## 2026-07-30 09:06:39 +0800
- 补齐 Idea 停止轮询的独立卡密池兼容性
- 发布模式: multi

## 2026-07-30 09:56:43 +0800
- 修复任务历史中文类型、统计恢复与中断日志持久化
- 发布模式: multi

## 2026-07-30 09:56:58 +0800
- 修复任务历史中文类型、统计恢复与中断日志持久化
- 发布模式: multi

## 2026-07-30 13:51:34 +0800
- 修复账号批量删除确认交互与请求处理
- 发布模式: multi

## 2026-07-31 08:34:52 +0800
- 恢复 ChatGPT 手机号注册入口与任务提交合同
- 发布模式: multi

## 2026-07-31 13:42:24 +0800
- 剥离 GoPay、自动流水线和账号处理流水线残留入口
- 发布模式: multi

## 2026-08-01 17:13:19 +0800
- 修复手动手机号绑定启动后无实时日志
- 发布模式: multi

## 2026-08-01 18:40:40 +0800
- 修复 OAIPay 手动上传分组拉取并新增支付链接当前有无筛选
- 发布模式: multi

## 2026-08-02 08:06:49 +0800
- 修复 OAuth 验证码时间锚点并显示手机号绑定完整身份
- 发布模式: multi

## 2026-08-02 23:40:03 +0800
- 新增固定账号筛选组合并统一组合管理
- 发布模式: multi

## 2026-08-03 07:41:50 +0800
- 收敛 HME Ready provider 并修复 TempMail mailbox 自动重绑
- 发布模式: multi

## 2026-08-03 08:46:05 +0800
- 恢复 Plus 登录态短链支付并增加 Web Session 门禁
- 发布模式: multi

## 2026-08-03 12:30:15 +0800
- 统一注册与手机号绑定浏览器容量门禁并移除业务容器内存上限
- 发布模式: multi

## 2026-08-03 13:01:44 +0800
- ChatGPT HME 注册改用物理原地址并保留 TempMail 收码边界
- 发布模式: multi

## 2026-08-03 20:42:21 +0800
- ChatGPT HME 恢复 base 加单一平台随机 tag 分配
- 发布模式: multi

## 2026-08-04 11:27:33 +0800
- 修复手机绑定获取 RT 后本地状态未恢复
- 发布模式: multi

## 2026-08-04 15:24:02 +0800
- docs: 新增 ChatGPT 注册失败根因分析文档 (chatgpt-registration-failure-analysis.md)
- 发布模式: hot

## 2026-08-04 17:24:32 +0800
- v2.11.0 优化 Plus 注册并发、出口租约与浏览器容量
- 发布模式: multi

## 2026-08-04 21:34:18 +0800
- 修复本地状态刷新SID与数据库并发阻塞
- 发布模式: multi

## 2026-08-05 00:55:05 +0800
- 修复失效测活 Web Session 收口与任务弹窗
- 发布模式: multi

## 2026-08-05 07:58:40 +0800
- 增加失效测活代理并发并取消手机号绑定并发上限
- 发布模式: multi

## 2026-08-05 10:45:36 +0800
- 扩容 Plus 注册浏览器至五槽并开放全部置顶筛选组合
- 发布模式: multi

## 2026-08-05 16:18:05 +0800
- 整体回退运行版本至 v2.12.1
- 发布模式: multi

## 2026-08-05 17:12:23 +0800
- 根据现场测活结果再次回退至 v2.12.1
- 发布模式: multi

## 2026-08-05 18:15:26 +0800
- 定向修复 OAIPay Plus 仅 AT 未接码上传 v2.12.5
- 发布模式: multi
## 2026-08-05 19:21:43 +0800
- 修复 HME MIME 验证码解析并阻止路由地址误取码 v2.12.6
- 发布模式: multi

## 2026-08-05 21:03:43 +0800
- 修复失效测活停用响应识别并同步手机绑定账号状态 v2.12.7
- 发布模式: multi

## 2026-08-05 22:09:42 +0800
- 按现场要求回退运行版本至 v2.12.7
- 发布模式: multi

## 2026-08-05 22:22:48 +0800
- 按现场要求整体回退运行版本至 v2.12.5
- 发布模式: multi

## 2026-08-06 05:03:04 +0800
- 新增两级筛选组合与排他固定账号组 v2.13.0
- 发布模式: multi

## 2026-08-06 05:12:18 +0800
- 修复旧固定组合迁移弹窗层级 v2.13.0
- 发布模式: multi

## 2026-08-06 22:36:31 +0800
- 修复账号导出筛选范围并优化组合栏操作图标 v2.13.1
- 发布模式: multi

## 2026-08-07 07:35:13 +0800
- 修复固定账号跨一级筛选组合不可见问题 v2.13.2
- 发布模式: multi

## 2026-08-07 08:52:06 +0800
- 新增账号列表自定义每页显示数量 v2.13.3
- 发布模式: multi

## 2026-08-07 08:59:03 +0800
- 修复分页设置提示层拦截添加按钮 v2.13.3
- 发布模式: hot

## 2026-08-07 09:09:41 +0800
- 账号历史订阅增加刷新时间 v2.13.4
- 发布模式: hot

## 2026-08-07 09:16:24 +0800
- 提升历史订阅刷新时间可读性 v2.13.4
- 发布模式: hot

## 2026-08-07 11:31:43 +0800
- Plus 10并发注册与Solver自适应容量 v2.14.0
- 发布模式: multi

## 2026-08-07 13:18:29 +0800
- 修复并发注册页面状态推进与late-failure恢复 v2.14.1
- 发布模式: multi

## 2026-08-07 14:34:47 +0800
- 账号列表支持设置默认每页显示数量 v2.14.2
- 发布模式: multi

## 2026-08-07 15:01:41 +0800
- 修复筛选组合覆盖默认分页数量 v2.14.3
- 发布模式: hot

## 2026-08-07 18:07:32 +0800
- 新增注册全链路诊断抓包与制品管理 v2.15.0
- 发布模式: multi

## 2026-08-07 19:04:38 +0800
- 修复注册任务HME租约串用与新版生日表单 v2.15.1
- 发布模式: multi

## 2026-08-07 20:23:01 +0800
- 修复注册开户后恢复与重复提交竞态 v2.15.2
- 发布模式: multi

## 2026-08-07 22:23:37 +0800
- 固定组合订阅统计并修正本地刷新结果 v2.15.3
- 发布模式: multi

## 2026-08-08 22:48:27 +0800
- 修复任务日志检查点SQLite自锁并收敛账号列表写放大 v2.15.4
- 发布模式: multi

## 2026-08-09 00:23:28 +0800
- 新增ChatGPT单账号与批量登录态任务 v2.16.0
- 发布模式: multi

## 2026-08-09 02:18:04 +0800
- 新增持久浏览器登录态租约与人工释放控制
- 发布模式: multi

## 2026-08-09 11:59:06 +0800
- 新增独立的 0 元试用资格与 GCash 支付方式检测任务
- 发布模式: multi

## 2026-08-09 12:27:59 +0800
- 修复支付资格动态代理TLS握手失败 v2.18.1
- 发布模式: multi

## 2026-08-10 11:33:28 +0800
- 为批量0元检测增加可持久化并发设置 v2.18.2
- 发布模式: hot

## 2026-08-10 11:40:36 +0800
- 统一批量支付资格检测文案间距
- 发布模式: hot

## 2026-08-10 14:53:13 +0800
- 修复认证后订阅待刷新并增加持久重试 v2.18.3
- 发布模式: multi

## 2026-08-10 22:40:57 +0800
- 拆分订阅不可确认与待刷新状态并修复固定组合悬浮统计 v2.18.4
- 发布模式: multi

## 2026-08-11 18:29:25 +0800
- 注册浏览器改为单进程多无痕上下文 v2.19.0
- 发布模式: multi

## 2026-08-12 01:48:51 +0800
- 账号页增加批量复制 AT v2.19.1
- 发布模式: hot

## 2026-08-12 02:24:10 +0800
- 统一项目北京时间时区与任务日志展示 v2.19.2
- 发布模式: multi

## 2026-08-12 02:36:31 +0800
- 修复 Solver 北京时间模块直接入口 v2.19.3
- 发布模式: multi

## 2026-08-12 09:53:45 +0800
- 修复0元资格检测Promotion 403 v2.19.4
- 发布模式: multi

## 2026-08-12 10:03:51 +0800
- 正确处理0元资格Promotion业务拒绝 v2.19.5
- 发布模式: multi

## 2026-08-12 10:34:03 +0800
- 增加0元优惠检测代理国家选择 v2.19.6
- 发布模式: multi

## 2026-08-12 14:35:36 +0800
- 全面安全审计、12小时滑动管理员会话与供应链加固 v2.20.0
- 发布模式: multi

## 2026-08-12 18:32:02 +0800
- 新增 MiyaIP 动态代理渠道并保留 Cliproxy 双渠道选择 v2.21.0
- 发布模式: multi

## 2026-08-13 01:07:37 +0800
- 注册成功后自动检测0元试用资格并展示 v2.21.1
- 发布模式: multi

## 2026-08-13 22:48:11 +0800
- 修复0元优惠检测国家与结账档案一致性 v2.22.0
- 发布模式: multi

## 2026-08-14 02:47:24 +0800
- 新增支付链接格式检测与账号列表链接类型列及筛选 (v2.23.0)
- 发布模式: multi

## 2026-08-14 03:09:45 +0800
- 0元优惠与链接格式检测自动识别AT失效并联动改写失效与刷新 (v2.23.0)
- 发布模式: multi

## 2026-08-14 08:33:40 +0800
- 解除批量0元优惠与链接格式检测1000个账号上限 (v2.23.0)
- 发布模式: multi

## 2026-08-14 08:45:19 +0800
- 更新前端最新构建产物与批量支付资格弹窗校验提示 (v2.23.0)
- 发布模式: multi

## 2026-08-14 14:57:41 +0800
- 优化注册配置折叠置顶与 MiyaIP 代理测试 (v2.23.1)
- 发布模式: hot

## 2026-08-14 15:20:21 +0800
- 修正 Plus MiyaIP 住宅代理运行配置 (v2.23.1)
- 发布模式: hot

## 2026-08-14 20:04:40 +0800
- feat: 支持指定国家查询账号可用支付方式并拆分0元资格与支付方式为独立两列 (v2.24.0)
- 发布模式: multi

## 2026-08-14 20:06:27 +0800
- feat: 支持指定国家查询账号可用支付方式并拆分0元资格与支付方式为独立两列 (v2.24.0)
- 发布模式: multi

## 2026-08-14 20:17:46 +0800
- fix: 修复支付方式检测任务分发与代理出口探测容错 (v2.24.1)
- 发布模式: multi

## 2026-08-14 20:59:14 +0800
- 修复支付资格账号执行链与通用支付方式状态合同 (v2.24.2)
- 发布模式: multi

## 2026-08-15 18:41:52 +0800
- 修复0元资格检测实时统计并解除批量支付资格并发10上限
- 发布模式: multi

## 2026-08-15 21:17:19 +0800
- 优化 Plus 批量检测 SQLite 写入与任务历史加载性能
- 发布模式: multi

## 2026-08-15 22:21:22 +0800
- 补齐0元资格检测失败筛选并修复状态索引回填 v2.24.5
- 发布模式: multi

## 2026-08-15 23:42:25 +0800
- 修复 TempMail 与 HME Ready 内部接口地址 v2.24.6
- 发布模式: multi

## 2026-08-16 01:59:27 +0800
- 修复 OAIPay 内部连通与上传密钥漂移 v2.24.7
- 发布模式: multi

## 2026-08-16 02:47:04 +0800
- 修复注册后0元检测国家可切换并冻结任务结账国家 v2.24.8
- 发布模式: multi

## 2026-08-16 08:29:36 +0800
- 重构多浏览器账号指纹并升级 Camoufox v152 进程级深隔离 v2.25.0
- 发布模式: multi

## 2026-08-16 13:29:10 +0800
- 修复账号表格与任务历史大库加载延迟
- 发布模式: multi

## 2026-08-16 13:53:15 +0800
- 修复首次任务摘要回填阻塞实例启动
- 发布模式: multi

## 2026-08-16 16:39:42 +0800
- 修复失效测活旧密码账号的一次性验证码兜底 v2.25.3
- 发布模式: multi

## 2026-08-16 16:48:21 +0800
- 修复失效测活密码页发码响应竞态 v2.25.3
- 发布模式: multi

## 2026-08-16 16:51:02 +0800
- 收紧无密码账号失效测活仅验证码登录语义 v2.25.3
- 发布模式: multi

## 2026-08-16 16:55:02 +0800
- 修正无密码测活发布说明 v2.25.3
- 发布模式: multi

## 2026-08-16 17:33:23 +0800
- 细分支付资格检测失败原因并展示 v2.25.4
- 发布模式: multi

## 2026-08-16 19:21:30 +0800
- 统一已有账号OTP登录并修复手机号绑定旧密码阻断 v2.25.5
- 发布模式: multi

## 2026-08-16 22:37:53 +0800
- 修复浏览器容量等待并按FIFO逐一放行 v2.25.6
- 发布模式: multi

## 2026-08-17 01:18:47 +0800
- 注册后0元检测改为可选并统一注册国家选择器 v2.25.7
- 发布模式: multi

## 2026-08-17 02:19:58 +0800
- 增加注册后 PayPal 提链并自动支付
- 发布模式: multi

## 2026-08-17 02:24:32 +0800
- 修复注册后 PayPal 自动支付标记与状态刷新并发覆盖
- 发布模式: multi

## 2026-08-17 15:52:47 +0800
- 拆分注册后 PayPal 提链并入队成功账号结果 v2.26.1
- 发布模式: multi

## 2026-08-17 22:18:51 +0800
- 新增 ChatGPT AT/RT 生命周期追踪、过期归因与认证状态展示
- 发布模式: multi
