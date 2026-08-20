# ChatGPT 注册任务并发瓶颈分析与优化方案

> 调研日期：2026-08-20
> 代码基线：`bc0a926`（v2.31.7）
> 性质：只读代码路径追踪 + 宿主运行态观测，本文档不含任何已实施的改动

## 0. 问题描述

同时运行 **3 个浏览器注册任务时非常流畅**；一旦增加到 **5 个甚至更多，多出来的任务就会出问题并导致任务中断**。

目标：定位真实瓶颈，给出可落地的提高并发能力的方案。

---

## 1. 为什么恰好卡在 3 个：容量算术

| 层级 | 取值 | 出处 |
|---|---|---|
| 浏览器执行器 每任务默认并发 | 2 | `api/tasks.py:173` `REGISTER_BROWSER_DEFAULT_CONCURRENCY` |
| 浏览器执行器 每任务并发**上限** | **2** | `api/tasks.py:174` `REGISTER_BROWSER_DEFAULT_MAX_CONCURRENCY` |
| 进程内浏览器总槽位（`auto-gpt-plus`） | 6 | `AUTH_BROWSER_MAX_CONCURRENCY:-6`，`docker-compose.multi.yml` |
| 进程内浏览器总槽位（`auto-gpt` / `auto-plus2`） | 2 | `AUTH_BROWSER_MAX_CONCURRENCY_MAIN/PLUS2:-2` |
| registration / recheck lane 保底 | 4 / 2 | `AUTH_BROWSER_REGISTRATION_RESERVE` / `AUTH_BROWSER_RECHECK_RESERVE` |
| 代码硬上限 | 无固定值 | Auth 容量由实例配置动态决定，锁内活动计数保证不超配 |

**3 任务 × 2 并发 = 6 = 槽位总量，正好打满。**

第 4 个任务开始，多出来的尝试只能排队，**吞吐量一点都不增加**，只增加排队者与排队者手里握着的稀缺资源。

关键点：`REGISTER_BROWSER_DEFAULT_MAX_CONCURRENCY = 2` 是**每任务的硬上限**，
用户无法通过"把一个任务的并发调高"来提速，只能"多开任务"——
于是用户的行为被这个参数逼向了唯一会打爆系统的路径。

宿主为 **8 核 / 32GB**，且同时运行着 tempmail、marzban、kiro、tg_invite、openai-pay 等
20 多个其它容器。6 个 Camoufox 进程已接近这台机器的实际极限。

---

## 2. 任务"中断"的直接机制

`api/tasks.py:22600` `_is_fatal_registration_infrastructure_error()` 把以下两个错误码判为
"基础设施不可用"：

```python
"browser_registration_unavailable",
"browser_registration_hard_timeout",
```

命中后（`api/tasks.py:24819-24827`）：

```python
if _is_fatal_registration_infrastructure_error(result.message):
    fatal_registration_error = str(result.message or "").strip()
    _registration_task_log("[FATAL] Sentinel 浏览器基础设施不可用，立即停止后续注册尝试", ...)
    control.request_stop()
```

后果链条：

1. **一次**浏览器尝试超时 → **整个任务**立即 `request_stop()`；
2. `_cancel_queued_attempts()` 取消该任务全部排队尝试；
3. 同任务其它**正在跑**的尝试被 `stop_check` 打断，它们手里的邮箱 / 代理 / 出口 IP 全部作废；
4. 任务终态 `failed`（`api/tasks.py:24984`、`25071`）。

这两个码在高并发下恰恰是**拥塞的正常产物**，而非基础设施故障：

- `browser_registration_hard_timeout`（`sentinel_browser.py:2330`）
  = 420s（`chatgpt_browser_registration_hard_timeout_seconds`，
  `access_token_only_registration_engine.py:738`）内没跑完。
  6 个 Camoufox 挤 8 核 → 页面加载变慢 → 超时概率随并发陡升。
- `browser_registration_unavailable`（`sentinel_browser.py:2332`）
  = worker `status != "ok"`，三个来源：
  worker 未返回结果就退出（OOM / 被杀，`sentinel_browser.py:929`）、
  worker 启动或通信失败（`:947`）、
  worker 内未捕获异常（`:652`，如 Camoufox launch timeout、`TargetClosedError`）。
  全部是资源紧张的典型表现。

**二次放大**：浏览器模式下失败会吃掉目标名额（`api/tasks.py:24485`）：

```python
consumes_target_slot = bool(browser_executor and browser_register_invoked and ...)
```

超时发生在浏览器已启动之后，因此 `browser_register_invoked=True`、`consumes_target_slot=True`。
即使不触发 FATAL，`count=5` 的任务也可能被 5 次超时耗光预算、0 成功收摊。

---

## 3. CPU PSI 门禁在这台机器上会提前关闸（真实容量 < 配置容量）

`AUTH_BROWSER_CPU_PSI_AVG10_LIMIT=15`（默认值见 `sentinel_browser.py:155-159`）。
`_browser_cpu_pressure_allows_slot()`（`:424-441`）带**滞回**：

```python
if _CPU_PRESSURE_BLOCKED and avg10 <= max(0.0, limit * 0.8):   # 需降到 ≤12 才复位
    _CPU_PRESSURE_BLOCKED = False
if avg10 >= limit:                                              # ≥15 就锁死
    _CPU_PRESSURE_BLOCKED = True
```

这是一个**绝对阈值**，但 `/proc/pressure/cpu` 是**整台宿主**的指标，
包含宿主上另外 20 多个容器的负载。8 核宿主上多任务时 avg10 几乎不可能回落到 12 以下，
于是 `_CPU_PRESSURE_BLOCKED` 长期置位，**新槽位发不出去，实际并发反而低于配置的 6**，
表现为"任务卡住不动"。

（对照观测：三个业务实例停机时 `/proc/pressure/cpu` 为 `avg10=0.43 avg60=1.71 avg300=4.78`，
说明宿主基线负载本身就已经占掉了阈值的相当一部分。）

**次要浪费**：`run_with_browser_capacity`（`:1470-1490`）在**已持有槽位**的情况下
再次调用 `_wait_for_adaptive_browser_resources`（`:1304`）——该函数 0.5s 轮询、**无上限**。
抢到槽位后 PSI 若回升，槽位被空占。

（注：launch stagger 在获取槽位**前**已经检查过 `_browser_launch_turn_delay()`（`:1080`），
`_claim_browser_launch_turn()` 返回的 `launch_at` 基本等于 now，因此持槽期间的
stagger 等待≈0，不是瓶颈。）

---

## 4. 稀缺资源在排队之前就被占用（资源获取顺序倒置）

单次尝试的真实顺序：

```
代理候选
  → 独立出口 IP 租约   api/tasks.py:23681  _claim_unique_register_exit_ip
                        （active TTL 1800s，并起心跳线程 register-exit-ip-N 持续续租）
  → 邮箱 create_email() access_token_only_registration_engine.py:1393
  → 才去排浏览器槽      sentinel_browser.py:971  browser_capacity_slot
```

而排队是**没有超时的**——`browser_capacity_slot` 的 while 循环（`:1198-1221`）
只被 `stop_check` 打断。于是排队中的尝试**握着出口 IP 租约和邮箱**无限期干等。

- 出口 IP registry 是**进程内**的（`core/chatgpt_register_exit_ip_registry.py`，
  仅被 `api/tasks.py` 引用，不跨容器），候选预算约 6 个。
  并发越高撞 IP 越多 → "独立出口 IP 检查未通过，跳过候选 N/M"（`api/tasks.py:23692`）
  → 候选耗尽 → `所有代理尝试失败`（`:23834`）。
- HME / TempMail 库存被排队尝试白白占住，加剧上游邮箱耗尽。

---

## 5. 没有准入控制

- **没有"同时运行的注册任务数"上限**（`running_tasks` / `max_active_tasks` / `task_limit` 均无实现）。
- **没有连续失败熔断**。
- 排队深度、预计等待时间对用户完全不可见。

用户只能靠"多开任务"来提速，反而把系统推过临界点——这正是本次问题的行为诱因。

---

## 6. 优化方案（容量优先）

核心思路：**不靠放宽失败语义来掩盖拥塞，而是把真实可用容量提上去、把浪费容量的机制修掉，
并让"提高并发"这件事回到单任务并发参数上，而不是逼用户多开任务。**

### 6.1 让单任务并发可调，不再逼用户多开任务 ★ 最高优先级

`api/tasks.py` 中的 `REGISTER_BROWSER_DEFAULT_MAX_CONCURRENCY = 2` 只是未配置时的兼容默认值。
`_normalize_register_runtime_controls()` 支持用配置项
`chatgpt_register_browser_max_concurrency` 设置正整数上限，源码不再把浏览器任务裁剪到 15。

**改动**：把默认上限从 2 提到与实例浏览器槽位一致（例如 6），
默认并发保持 2 不变（不改变现有默认行为）。

效果：用户开 **1 个 count=12 concurrency=6 的任务**，而不是 6 个任务。
好处是所有尝试共享同一个任务的 `attempt_cap`、出口 IP 冲突集合
（`unique_exit_ip_assigned_keys`）与失败统计，调度可控、可观测、可一键停止。

### 6.2 CPU PSI 门禁改为相对基线 ★

`sentinel_browser.py:424 _browser_cpu_pressure_allows_slot()`。

绝对阈值 15 在共享宿主上没有意义。改为：

- 进程启动时（或首次调用时）采样一次空闲 PSI 作为 `baseline`；
- 阈值 = `baseline + AUTH_BROWSER_CPU_PSI_AVG10_DELTA`（新配置项，建议默认 20）；
- 滞回复位点从 `limit * 0.8` 放宽到 `limit * 0.9`，避免长时间锁死。

保留 `AUTH_BROWSER_CPU_PSI_AVG10_LIMIT` 作为绝对上限的兜底（建议同时上调到 40），
两者取较严格者。

短期无代码改动的止血手段：直接把 compose 与 `.env` 里的
`AUTH_BROWSER_CPU_PSI_AVG10_LIMIT` 从 15 提到 30–40。

### 6.3 修掉持槽空等 ★

`sentinel_browser.py:1484`。`_wait_for_adaptive_browser_resources` 增加一个短阈值
（建议 10s）。超过阈值仍被门禁挡住时，**释放槽位重新排队**，
而不是占着槽位 0.5s 轮询到门禁自己打开。

实现上需要让 `run_with_browser_capacity` 能感知这次"放弃并重排"，
在 `browser_capacity_slot` 外层套一个有限次数的重试循环。

### 6.4 先抢浏览器槽，再领出口 IP 和邮箱 ★

把浏览器准入提到 `_claim_unique_register_exit_ip`（`api/tasks.py:23681`）
和 `create_email()`（`access_token_only_registration_engine.py:1393`）之前。

推荐实现：在 `api/tasks.py` 的 `_do_one` 开头，浏览器执行器先获取一个
`browser_capacity_slot`（复用现有 contextmanager，`priority="registration"`），
在其作用域内再做代理 / 出口 IP / 邮箱 / 引擎调用。
需要给 `_run_with_browser_slot` 加一个 `slot_already_held: bool` 旁路，避免二次排队。

单项收益：同时消除出口 IP 撞车与邮箱库存浪费。

### 6.5 给排队加上限

`sentinel_browser.py:971 browser_capacity_slot` 增加 `max_wait_seconds` 参数
（建议默认 240s，可配 `chatgpt_runtime_browser_slot_max_wait_seconds`）。
超时返回明确的 `browser_capacity_wait_timeout`，让尝试干净退出并释放资源，
而不是无限期挂着。

注意：该错误码**不能**落进 `_is_fatal_registration_infrastructure_error`，
否则等于把排队超时升级成任务级 FATAL。

### 6.6 容量与隔离的运维调整 — `docker-compose.multi.yml`

- `auto-gpt` / `auto-plus2` 目前各只有 2 个槽位、`shm_size: 1gb`。
  若这两个实例也跑浏览器注册，`shm_size` 应对齐 `auto-gpt-plus` 的 `2gb`
  （Camoufox 对 `/dev/shm` 敏感，不足会导致 worker 启动即崩 → `browser_registration_unavailable`）。
- **三个业务实例都没有 `mem_limit`**。任一实例内存暴涨会拖垮另外两个 +
  宿主上 20 多个其它容器。建议按 32GB 宿主显式分配（如 main / plus2 各 6g，plus 10g）。
- 总容量受 8 核宿主约束。提高 `AUTH_BROWSER_MAX_CONCURRENCY` 前，
  应先做 6.2 / 6.3 / 6.4——同样的槽位数能跑出更高有效吞吐。

### 6.7 可观测性（配合项，无风险）

`browser_capacity_snapshot()`（`sentinel_browser.py:1376`）已返回
`max_concurrency`、`lane_reserves`、各门禁状态。
挂一个只读端点 `GET /api/system/browser-capacity`（`api/system.py` 已有 router），
在注册页显示"槽位 X/6 占用，队列深度 Y"。用户看得见容量就不会盲目多开。

---

## 7. 不采用的方案（及原因）

**放宽 FATAL 判定 / 让拥塞类失败不吃目标名额。**

即把 `browser_registration_hard_timeout` 与 `browser_registration_unavailable`
从 `_is_fatal_registration_infrastructure_error` 里摘出来，改成"连续 N 次才 FATAL"。

这能让任务不再被单次超时打死，但**不增加任何吞吐**：
槽位还是 6 个，超时还是会发生，只是从"任务失败"变成"任务慢慢磨"，
同时会掩盖真实的基础设施故障信号。

结论：作为 6.1–6.5 落地后的**补充**可以考虑，但不应作为主方案。

---

## 8. 建议实施顺序

1. **6.1 单任务并发上限** — 改一个常量 + 一个配置项，立刻让用户不必多开任务。
2. **6.2 PSI 相对基线**（可先用调参止血） — 恢复真实可用容量。
3. **6.3 持槽空等 + 6.5 排队上限** — 槽位不被空占，失败可预期。
4. **6.4 资源获取顺序** — 消除出口 IP 撞车与邮箱浪费（改动面最大，放在有测试保护之后）。
5. **6.6 运维参数 + 6.7 可观测性**。

---

## 9. 验证方式

### 单元测试

必须走隔离容器（见 `docs/testing-in-docker.md`，唯一合规门禁）：

```bash
docker build --target test -t auto-gpt:test-"$(git rev-parse --short HEAD)" .
docker compose -f docker-compose.test.yml run --rm --no-deps test \
  python -m pytest -q -m "not browser and not live"
```

需新增的用例：

- `_normalize_register_runtime_controls`：`chatgpt_register_browser_max_concurrency`
  能把浏览器任务并发提到 30，且不被旧的固定 15 截断；
  未配置时默认行为不变。
- `_browser_cpu_pressure_allows_slot`：相对基线阈值的置位 / 复位滞回边界。
- `browser_capacity_slot(max_wait_seconds=...)`：超时后正确释放票号，
  `_BROWSER_WAIT_QUEUE` 无残留、`_BROWSER_REGISTRATION_WAITERS` 归零。
- 资源顺序：用 mock 记录调用序，断言 `_claim_unique_register_exit_ip`
  与 `create_email` 发生在槽位获取之后。

### 压测对照（改造前 / 后各一轮）

场景 A（现状）：6 个 `count=2 concurrency=2` 的浏览器注册任务。
场景 B（目标）：1 个 `count=12 concurrency=6` 的浏览器注册任务。

采集指标：

- `browser_slot=waiting` 各 `reason=` 的分布
  （`capacity` / `fifo_queue` / `lane_reserve` / `cpu_psi` / `host_memory` / `pids`）
- `browser_slot=acquired ... queue_wait=` 的 min / median / max
- `[FATAL] Sentinel` 出现次数
- `出口 IP 已被同进程其他注册任务占用` 出现次数
- **核心指标：完成账号数 / 消耗邮箱数**（衡量有效吞吐，而非并发数）

### 三实例连通性

```bash
curl -fsS http://127.0.0.1:8000/api/health
curl -fsS http://127.0.0.1:8001/api/health
curl -fsS http://127.0.0.1:8003/api/health
```

---

## 10. 相关文档

- `docs/chatgpt-registration-failure-analysis.md` — 更早的注册失败率分析，
  其 3.2 节的每模式并发表、3.3 节"独立 Turnstile Solver 不在注册调用链上"、
  6 节残留风险 #1"出口 IP 租约不跨容器"与本文结论一致。
- `docs/testing-in-docker.md` — 测试边界与 sentinel 日志契约
  （`event=browser_slot_waiting` / `browser_slot_acquired`）。
