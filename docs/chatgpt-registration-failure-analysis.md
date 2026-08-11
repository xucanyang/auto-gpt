# ChatGPT 注册失败根因分析报告

> [!NOTE]
> 分析对象：`auto-gpt` 主实例与 `auto-gpt-plus` 实例的历史 `task_logs`，并结合当前注册源码与 Docker 运行态复核。
> 统计快照截止：主实例 `MAX(created_at)=2026-07-24 22:03:43.631796`，Plus 实例 `MAX(created_at)=2026-08-04 08:57:52.544548`；数据库时间字段本身不带时区。
> 统计事实复核时间：2026-08-04 17:10:22 +0800；P0/P1 落地时间：2026-08-04。
> 错误分类只能说明相关性，不能仅凭 HTTP 状态码证明失败由并发直接触发。

---

## 1. 总体失败概况

### 主实例 (`auto-gpt`)

| 状态 | 数量 | 占比 |
|:---|:---:|:---:|
| **failed** | 713 | **38.25%** |
| success | 687 | 36.86% |
| stopped | 256 | 13.73% |
| running | 175 | 9.39% |
| skipped | 19 | 1.02% |
| done | 14 | 0.75% |

表中六类状态合计 `1864`。`713 / 1864 = 38.25%`；在 `failed + success + done` 三类明确终态中，失败占比为 `713 / (713 + 687 + 14) = 50.42%`。历史 `running` 快照、人工停止和跳过记录不能直接计入注册成功率。

### Plus 实例 (`auto-gpt-plus`)

| 状态 | 数量 | 占比 |
|:---|:---:|:---:|
| done | 498 | 35.14% |
| failed | 312 | 22.02% |
| stopped | 214 | 15.10% |
| success | 212 | 14.96% |
| running | 99 | 6.99% |
| partial | 57 | 4.02% |
| interrupted | 24 | 1.69% |
| skipped | 1 | 0.07% |

表中八类状态合计 `1417`。在 `failed + success + done` 三类明确终态中，失败占比为 `312 / (312 + 212 + 498) = 30.53%`；`running`、`partial`、`interrupted`、`stopped` 和 `skipped` 不应被直接折算为成功或失败。

---

## 2. 失败错误分类（主实例 713 次失败）

| 错误类别 | 次数 | 占失败数 | 并发相关性判断 |
|:---|:---:|:---:|:---|
| 手机号绑定阶段失败 (`Internal Server Error`) | 67 | 9.4% | 可能受上游容量影响，不能仅凭 500 归因于并发 |
| Cloudflare 403（提交邮箱时） | 44 | 6.2% | 可能与出口 IP、指纹、频率或会话状态相关 |
| 验证码提交 403 | 39 | 5.5% | 可能与出口 IP、验证码状态或会话状态相关 |
| 429 请求限流 | 28 | 3.9% | 与请求频率相关性较高，但仍需结合出口和时间窗口 |
| HME 邮箱池耗尽 | 26 | 3.6% | 高并发会加速消耗，但根因是上游可用库存不足 |
| 未获取 workspace 产物 | 25 | 3.5% | 通常不是并发直接导致 |
| 账号已被删除/停用 | 21 | 2.9% | 存量账号状态问题 |
| 最终 URL 403 | 20 | 2.8% | 可能与出口 IP、指纹或上游策略相关 |
| 首页访问失败 | 15 | 2.1% | 代理质量、网络和负载均可能影响 |
| 手机号验证页拦截 | 15 | 2.1% | 上游策略与手机号资源均可能影响 |
| 无有效组织 workspace | 12 | 1.7% | 账号状态问题 |
| Browser 启动/上下文失败 | 5 | 0.7% | 可能受 CPU、PID、SHM 或浏览器资源竞争影响 |
| 注册被禁止 | 3 | 0.4% | 可能与 IP/身份信誉相关，样本过少 |
| Sentinel 浏览器不可用 | 2 | 0.3% | 浏览器基础设施异常，不等同于 Solver 排队 |
| OAuth 浏览器事务超时 | 2 | 0.3% | 可能受代理、CPU 或浏览器容量等待影响 |

按原报告的高/中相关性标色重新计算，高相关类别为 `143 / 713 = 20.06%`，中相关类别为 `123 / 713 = 17.25%`，合计 `266 / 713 = 37.31%`。这个数值只能表示“落在可能受并发放大的错误类别中”，不能证明这 266 次失败均由并发造成，也不能据此承诺独立出口会消除全部 403/429。

---

## 3. 六类风险与代码边界

### 3.1 出口 IP 与 WAF/限流相关风险

多个注册尝试复用同一出口 IP，会增加短时间内请求密度和关联信号，是 403/429 的合理风险因素，但不是唯一解释。IP 信誉、浏览器指纹、Cookie/Session 状态、代理质量和上游策略同样可能产生相同状态码。

本次落地后的独立出口策略为：

1. `chatgpt_register_unique_exit_ip_policy=auto` 时，仅动态代理默认启用独立出口；直连、代理池和指定代理保持显式选择。
2. 显式 `required/true` 时，动态代理会扩大刷新候选；代理池或开启 failover 的指定代理会轮换候选；不可轮换的单代理批量请求会在入队前被拒绝。
3. 每个尝试确认真实出口 IP 后通过进程级 registry 原子领取；刚生成的首个动态候选可直接复用有效探测结果，失败切换到后续候选时重新探测，避免长 OTP 流程后沿用过期 IP。并发任务共享 active/cooldown 租约，默认 active TTL 为 `1800s`、运行期间续租、完成后冷却 `900s`。动态候选默认预算固定为 `6`、可配置范围为 `1-12`，任务预检只生成 `1` 个候选，避免探测量随目标数量二次方增长。
4. registry 的作用域是单个 Python 进程/容器。`auto-gpt`、`auto-gpt-plus`、`auto-plus2` 之间仍未共享运行态租约；当前主要使用 Plus，因此先解决 Plus 同容器多任务碰撞。以后如需跨容器去重，应使用独立运行态租约表或协调服务，不应把高频瞬态锁混入 `shared_config.db` 的配置语义。

### 3.2 注册任务并发、注册浏览器容量与宿主机资源

旧行为中，API 和前端默认并发均为 `1`，`_run_register()` 的 `5` 只是历史硬上限，不是默认值。历史客户端可以显式提交 `4/5`，任务线程池才会在运行时截到 5。

本次在入队前冻结有效并发，并在运行前二次校验：

| 注册模式 | 默认并发 | 后端硬上限 |
|:---|:---:|:---:|
| protocol | 2 | 3 |
| headless / headed | 2 | 2 |
| 手机号注册 | 1 | 1 |
| `manual_email_otp` | 1 | 1 |

任务元数据同时记录 `requested_concurrency` 和 `effective_concurrency`。历史请求中的 `4/5` 不再直接变成 4-5 个执行 worker；显式 `1` 仍保持串行。

注册任务并发与浏览器槽是两套约束。`AUTH_BROWSER_MAX_CONCURRENCY` 控制注册 context 与 Auth/Sentinel 浏览器工作的总并发；ChatGPT Camoufox 注册本身不再为每个槽启动完整浏览器，而是按 `headless/headed` 运行模式各维护一个懒启动共享进程，每个注册 worker 领取独立无痕 `BrowserContext + Page`。Cookie、LocalStorage、代理、HAR/Trace 均按 context 隔离；Canvas、WebGL、字体等 Camoufox 深层指纹仍是浏览器进程级共享，这是资源复用模型的明确边界。

三个业务容器当前不设置应用总内存 `mem_limit`，Docker `Memory=0`、cgroup `memory.max=max`。因此 `sentinel_browser.py` 的第二槽 cgroup 内存判断不会在当前运行态形成硬门控；浏览器最终竞争的是宿主机约 32GB RAM、8GB Swap、8 vCPU、PID 和调度时间。

`shm_size` 只控制容器 `/dev/shm` tmpfs 上限，不是容器总内存。删除该配置通常会回落到 Docker 默认约 64MiB，不会自动获得宿主机全部可用容量。Plus 保持 `2gb`，主实例和 Plus2 保持 `1gb`；当前 Plus 的 `pids_limit` 为 `3072`，主实例和 Plus2 为 `768`，且三个实例均不新增应用容器总内存硬限制。共享 Camoufox 尚未启动时仍使用完整浏览器 PID/内存预算和启动错峰；进程就绪后，新注册槽改按默认 `32 PID / 384 MiB` 的 context 预算复核，不再对每个 context 重复套用 `220 PID / 1280 MiB` 的完整进程预算。PID 余量不足仍会释放 semaphore 并输出 `browser_slot=waiting reason=pids` 后重试。

### 3.3 独立 Turnstile Solver 当前不在 ChatGPT 注册调用链

ChatGPT 注册确实会处理 Sentinel Turnstile，但当前两条注册链均不调用容器内独立的 `:8889/turnstile`：

- 协议注册由 `services/chatgpt_core/any_auto/register.py::_check_sentinel()` 调用 `sentinel_vm.solve_turnstile_dx()`。
- 浏览器注册使用自身 Camoufox 页面执行 Sentinel 挑战，并受注册浏览器容量槽控制。
- `core/base_captcha.py::LocalSolverCaptcha` 才调用独立 Solver 的 `/turnstile` 和 `/result`，但当前 ChatGPT 注册没有 `_make_captcha()` 调用方。

因此不能把当前注册失败或 `browser_slot=waiting` 归因于独立 Solver 队列。Plus 的独立 Solver 从此前的 `6/1` 收敛为 `1/1`，避免未被当前注册链消费的预留浏览器与五个 Auth Browser 槽争抢 PID、CPU 和内存；主实例和 Plus2 保持 `4/1`。这个调整不会降低当前 ChatGPT 注册吞吐。

### 3.4 CSRF/Session 竞态目前没有证据

每个注册 worker 使用独立 HTTP Session、无痕 BrowserContext、Cookie 和 `oai-did`。共享的是 Camoufox 进程及其深层指纹，不共享浏览器存储。协议链获取 CSRF 后立即提交 `signin/openai`，源码不存在多个 worker 共享同一个 CSRF token 的竞态，也不存在“先拿 CSRF、再等待浏览器槽或独立 Solver”的通用路径。

相同出口 IP 仍可能放大 WAF 关联风险，但不能据此推导 CSRF token 因并发排队而过期。除非任务日志能给出 CSRF 获取、signin 提交和失败响应的时间证据，否则该项只能保留为待验证假设，不能列为已确认根因。

### 3.5 HME 邮箱资源池容量

`HME pool empty` 或 HME Ready `prepare status=503` 表明上游可用身份不足。提高注册并发会更快消耗库存，但增加本地线程、浏览器槽、Solver 或 SHM 都不能产生新的 HME 资源。该问题应通过池剩余量监控、预热/补池、任务前容量检查和明确降并发处理。

### 3.6 OTP 等待窗口与负载放大

当前单账号注册 OTP 策略为首次等待 `120s`、重发等待 `90s`、总预算 `210s`，不是 600 秒。并发可通过代理响应、邮件投递、宿主机 CPU 和浏览器调度增加阶段耗时。

浏览器模式在注册会话开始前取得容量槽，并由共享 Server 串行预创建带代理的独立 context，随后各 worker 并发操作自己的工作页；会话结束、worker 异常或被强杀后，父进程都会按 context token 兜底关闭该 context。它不是提交 OTP 后再等待其他 worker 释放同一页面，因此“OTP 已到达后排队等待 Sentinel 槽”不能作为通用失败路径。

---

## 4. 当前共享资源关系

```mermaid
graph TD
    P[协议注册<br/>默认2 / 最大3]
    B[浏览器注册<br/>默认2 / 最大2]
    IP[代理出口 IP<br/>探测 + 进程级租约]
    MAIL[邮箱资源与 OTP 投递]
    CPU[宿主机 CPU / PID / 调度]
    SLOT[Auth Browser 槽<br/>主2 / Plus5 / Plus2 2]
    SHM[/dev/shm<br/>主 1GiB / Plus 2GiB / Plus2 1GiB]
    FAIL[403 / 429 / 超时 / 阶段失败]
    SOLVER[独立 Solver<br/>Plus 1/1]

    P --> IP
    B --> IP
    P --> MAIL
    B --> MAIL
    P --> CPU
    B --> SLOT
    SLOT --> CPU
    SLOT --> SHM
    IP --> FAIL
    MAIL --> FAIL
    CPU --> FAIL
    SHM --> FAIL
```

独立 Solver 当前不与 ChatGPT 注册节点或失败节点连边，因为现有注册调用链不消费它；图中的 Solver 仅表示 Plus 保留一个 warm 浏览器以兼容其他潜在调用方。

核心结论不是“默认 5 路线程共享 4 个固定瓶颈”，而是注册任务并发、浏览器容量、出口 IP、邮箱资源和宿主机调度相互独立又会叠加。当前证据支持优先限制有效并发、错开启动时间并减少同出口碰撞；不支持把全部 403/429 或 OTP 失败归因于单一容量项。

---

## 5. P0/P1 落地配置与兼容性

| 优先级 | 落地配置 | 效果与兼容性 |
|:---:|:---|:---|
| P0 | protocol 默认 `2`、硬上限 `3`；browser 默认 `2`、硬上限 `2`；手机号与手动邮箱固定 `1`；记录 requested/effective | 历史显式 `4/5` 会被后端截断；显式 `1` 和非 ChatGPT 注册旧上限保持不变 |
| P0 | 动态代理缺省使用 `auto` 独立出口；同进程跨任务 active/cooldown 租约与续租；候选预算 `6`、预检 `1`；Plus 本地旧 `false` 迁移为 canonical `auto` | 减少 Plus 同容器任务撞出口，并约束正常路径的代理探测成本；不能承诺消除全部 403/429，跨容器碰撞仍是残余风险 |
| P1 | ChatGPT 账号尝试启动间隔默认随机 `15-30s` | 抖动作用于相邻账号启动时间，不是每个 HTTP 请求；显式 `0/0` 继续关闭；旧客户端只传最小值时保持固定延迟 |
| P1 | Plus：Solver `1/1`、SHM `2gb`、Auth Browser `5`、PID `1536`、启动 PID 余量 `220`、启动间隔 `4s`；主实例与 Plus2：Solver `4/1`、SHM `1gb`、Auth Browser `2`、PID `768`，PID 余量与启动间隔关闭；均无应用 `mem_limit` | 五槽提升同进程跨任务总容量，单任务浏览器并发仍为 2；PID 门控和错峰降低集中拉起风险，2GiB SHM 不代表预占 2GiB；Solver 收敛不影响当前 ChatGPT 注册链 |

主要修改边界：

- `api/tasks.py`：模式级默认/硬上限、延迟归一化、requested/effective 元数据、代理控制冻结、独立出口策略与运行时二次冻结。
- `api/config.py`：注册容量、延迟和出口租约配置的默认值、保存校验与响应合同。
- `core/chatgpt_register_exit_ip_registry.py`：同进程原子领取、active TTL、冷却和 IPv6 `/64` 去重。
- `frontend/src/pages/Accounts.tsx`、`RegisterTaskPage.tsx`、`features/auth/components/RegisterTaskModal.tsx`：前端默认值、模式上限、最大延迟持久化和 payload 对齐。
- `docker-compose.multi.yml`：三个实例分别设置 Solver、Auth Browser、PID 余量和启动间隔，仅 Plus 使用五槽、`2gb` SHM 与 `pids_limit=1536`。
- `services/chatgpt_core/sentinel_browser.py`：共享容量槽在启动前检查 cgroup PID 余量，并以进程级单调时钟错开实际浏览器事务启动；等待和取消都保持 semaphore 成对释放。

---

## 6. 剩余风险

1. 独立出口租约目前不跨容器；如果以后同时恢复三个实例的高并发注册，需要单独设计共享运行态协调。
2. 403/429 仍可能来自 IP 信誉、指纹、Session 或上游策略，独立出口和抖动只能降低相关风险，不能保证消除。
3. Plus 没有应用总内存上限；五槽启用后仍需观察宿主机内存、Swap、CPU PSI、容器 `pids.current` 和 `browser_slot=waiting`，不能把 PID 余量门控误解为内存保护。
4. 当前 ChatGPT 注册不消费独立 Solver。只有出现真实 `/turnstile` 调用和队列指标后，才应重新评估 Solver 是否需要超过 `1/1`。
5. HME 池耗尽需要上游容量治理，不能由本地并发参数补偿。
