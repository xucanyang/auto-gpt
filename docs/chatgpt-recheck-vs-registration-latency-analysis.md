# 失效测活 vs 注册：为什么"路径差不多"但注册慢得多

> 调研日期：2026-08-20
> 代码基线：`b2b8bd5`（v2.32.2）
> 性质：只读代码路径追踪，本文档不含任何已实施的改动

## 0. 问题

**失效测活**（invalid recheck）和**注册**（registration）看起来走同一条 Camoufox 浏览器链路，
但注册单次耗时明显更长。本文回答这个差异到底来自哪里。

**结论先行**：两者只有中间一段 —— `_browser_registration_flow` 状态机 —— 是同一个函数，
**其余每一层都不同**。

- 函数内：`login_only` 开关砍掉了 6 个阶段处理分支中的 3 个，
  并把最重的入口路径换成了最轻的；
- 函数外：注册在编排层、运输层、收尾层各多出一整套工作。

按最坏时间预算：注册 ≈ **625s**（被 420s 硬超时截断），失效测活 ≈ **228s**。

---

## 1. 三层对照总表

| 层 | 注册 | 失效测活 |
|---|---|---|
| 入口框架 | 注册任务框架 `api/tasks.py` `_do_one` | 账号动作框架 `api/actions.py:498` |
| 代理候选循环 | 有，逐候选试（`tasks.py:23673`） | 无，`proxy_url` 由调用方传入 |
| 独立出口 IP | `_probe_unique_exit_ip` + registry claim + 心跳线程（`tasks.py:23114` / `:23681`） | 无 |
| 邮箱 | **新建**：`create_email()` → `_mailbox.get_email()` 向上游开信箱（`plugin.py:434`，引擎 `:1393`） | **复原**：`RestoredEmailService.create_email()` 纯本地还原（`restored_email_service.py:390`） |
| 浏览器运输层 | `run_any_auto_browser_registration_isolated`（`sentinel_browser.py:2247`）→ **独立子进程 worker** | `run_any_auto_browser_registration`（`any_auto/transport.py:323`）→ **同进程直跑** |
| 硬超时 | **420s**（`access_token_only_registration_engine.py:737-742`） | **无** |
| 容量车道 | `priority="registration"`（保底 4） | `priority="normal"` → recheck 车道（保底 2），`browser_register.py:4179` |
| 状态机 | `_browser_registration_flow(login_only=False)` | 同函数，`login_only=True` |
| 收尾 | `_probe_plus_checkout_billing`（`:351-500`）：建 checkout + 探金额，2 次上游往返 | 无 |
| 外层重试 | 浏览器模式强制 `registration_max_retries=1`（`:1306`）+ 运输层 1 次瞬态重试（`browser_register.py:4138`） | 3 次尝试，5s / 10s 退避（`invalid_account_recheck.py:589-590`） |

---

## 2. 同一个状态机里，`login_only` 砍掉了什么

`services/chatgpt_core/any_auto/browser_register.py:3590 _browser_registration_flow`

### 2.1 入口 —— 最大的单点差异

**注册**（`:3620`）走 `_start_browser_signup_via_page`（`:1248`）：

- 最多试 **2 个入口 URL**，每个 `page.goto(timeout=30000)`；
- 然后 `_wait_for_signup_entry_transition`（`:1087`）：活动窗 40s、**硬窗 75s**，
  0.25s 轮询，8s 后补一次 `requestSubmit`，再 10s 后补一次可信 `Enter`。

入口最坏 **≈ 135s**。

**失效测活**（`:3611`）走 `_start_browser_signup_via_authorize(screen_hint="login")`（`:1353`）：

- **1 次** `goto` + 8s DOMContentLoaded + CSRF + signin + authorize。

入口最坏 **≈ 40s**。

### 2.2 阶段分支

| 分支 | 注册 | 失效测活 |
|---|---|---|
| `create_account_password` | 提交注册密码（`:3698`） | **直接 raise**「失效测活拒绝进入新账号注册密码阶段」（`:3700`） |
| `login_password` | 仅作兜底恢复 | 主路径，可切一次性验证码（`:3767`） |
| `email_otp` | 走（预算见 2.3） | 走 |
| `about_you` | 提交资料 + `_ensure_about_you_page`（`:3898`） | **直接 raise**（`:3900`） |
| `add_phone` | 走 `_handle_add_phone_challenge` 短信验证（`:3977`） | **跳过直接返回**（`:3979`） |
| 完成后 onboarding | `_handle_post_signup_onboarding`（`:2492`，弹窗 + 问卷点击，最多 ~6s） | 不执行（`:3671`） |

6 个阶段处理分支，失效测活只走 2 个，另外 3 个是硬拒绝。

### 2.3 OTP 预算 —— 注册的最大单项

- **注册**：`access_token_only_registration_engine.py:1313-1343`
  首等 **120s** + 重发等 **90s**，单账号累计预算 **210s**（`RegistrationOtpBudget`）。
- **失效测活**：`web_session_login.py:245-250`
  `timeout=120`，**单次、无预算对象、无重发**
  （`EmailServiceAdapter(email_service, email, log_fn)` 只传 3 个位置参数 → `otp_budget=None`）。

### 2.4 注册独有的"二次登录恢复"

`browser_register.py:4682-4716`：开户 2xx 已确认但 Web Session 没抓到时，
**在同一个浏览器上下文里再跑一遍完整的 `_browser_registration_flow(login_only=True)`** ——
也就是再走一次登录、**再等一次 OTP** —— 然后再做一次 **55s** 的 Web Session 抓取。

失效测活没有这条恢复路径（`:4654` 直接 `raise`）。

---

## 3. 最坏时间预算算术（单次尝试，浏览器段）

**注册**

```
入口 2×30s(goto) + 75s(transition)      = 135s
密码页提交                                ≈  10s
OTP 预算                                 = 210s
about_you + 导航                          ≈  20s
onboarding                               ≈   6s
Web Session 抓取                          =  55s
─────────────────────────────────────────────
小计                                      ≈ 436s   ← 已超过 420s 硬超时
二次登录恢复（OTP 120s + 抓取 55s）        + 189s
─────────────────────────────────────────────
合计                                      ≈ 625s
```

**失效测活**

```
authorize 入口 30s + 8s                   =  38s
登录密码 / 一次性验证码切换                ≈  15s
OTP 单次                                  = 120s
Web Session 抓取                          =  55s
─────────────────────────────────────────────
合计                                      ≈ 228s
```

两个直接推论：

1. **注册的 420s 硬超时不是"故障阈值"，而是被正常最坏路径穿透的阈值。**
   只要入口重试一次 + OTP 走满预算 + 触发一次二次恢复，不出任何错也会超时。
   这与 `docs/chatgpt-registration-concurrency-analysis.md` §2 的结论互为佐证：
   `browser_registration_hard_timeout` 是拥塞的正常产物，不是基础设施故障。
   （v2.32.2 起该错误码已从 `_is_fatal_registration_infrastructure_error` 摘出，
   只终止当前账号、不再打死整个任务，见 `api/tasks.py:22600` 与 `:24827`。）
2. **失效测活的最坏路径（228s）低于注册的硬超时（420s）**，且它本身没有硬超时，
   所以它几乎不会以"超时"形态失败 —— 观感上就是"又快又稳"。

---

## 4. 浏览器段之外，注册还多出的开销

1. **邮箱开通**：注册每次尝试要向上游（iCloud HME / TempMail）真开一个信箱；
   失效测活从 `mailbox_state` 本地还原，仅在 TempMail 地址过期时才补建一次。
2. **出口 IP 探测**：`_probe_unique_exit_ip` 每个候选代理一次出网 HTTP 探测，
   撞 IP 就换下一个候选**重探一次**（`tasks.py:23123-23127`）。
3. **子进程冷启动**：注册走隔离 worker，每次尝试多一个 Camoufox 进程 + Python 导入 + IPC；
   失效测活在同进程内直接跑。
4. **收尾 checkout 探测**：`generate_plus_link`（`:416`）+ `probe_chatgpt_checkout_amount`（`:458`），
   两次上游 / Stripe 往返。
5. **排队位置**：注册尝试在**已经握着出口 IP 租约和邮箱**之后才去排浏览器槽，且排队无超时；
   失效测活没有这两样东西要握。

---

## 5. 一处容易误读的口径

失效测活的 `browser_mode` 默认解析成 `"protocol"`（`invalid_account_recheck.py:583-588`），
但 `capture_web_session_without_refresh_token` **无条件**调用浏览器运输层
（`web_session_login.py:240`），`browser_mode` 只用于决定 `headless` 与否。

也就是说：失效测活始终是浏览器链路，两者的可比性成立。

---

## 6. 用线上日志证实上面的算术

本文为只读分析、无代码改动，因此不需要跑单测。要验证预算算术，按下面三条抓（只读）：

```bash
# 1. 注册段实际耗时构成：入口 transition / OTP 等待 / Web Session 抓取
docker logs auto-gpt-plus 2>&1 \
  | grep -E "\[状态推进\] stage=email|elapsed_ms=|\[验证码\] 验证码(已收到|未收到)|开始抓取 ChatGPT Web Session"

# 2. 是否触发了注册独有的二次登录恢复（最贵的一段）
docker logs auto-gpt-plus 2>&1 \
  | grep -E "在同一浏览器上下文执行一次已有账号登录恢复|signup_recovery"

# 3. 失效测活侧对照
docker logs auto-gpt-plus 2>&1 | grep -E "\[失效测活\]|登录测活状态推进"
```

核心待证指标：

- 注册 `[验证码] 验证码已收到｜…｜等待=Ns` 的中位数 vs 失效测活同字段；
- `browser_registration_hard_timeout` 发生时，「二次登录恢复」日志出现的比例；
- 注册入口 `transition_elapsed_ms` 的分布 —— 若中位数接近 40000 / 75000，说明入口就是大头。

---

## 7. 相关文档

- `docs/chatgpt-registration-concurrency-analysis.md` —— 并发瓶颈分析。
  本文 §3 推论 1 与其 §2「任务中断的直接机制」互为佐证。
- `docs/chatgpt-registration-failure-analysis.md` —— 更早的注册失败率分析。
- `docs/testing-in-docker.md` —— 测试边界与 sentinel 日志契约。
