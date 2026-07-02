# HME Mail Ready API 多邮箱并发监听分析

## 核心诉求
类似 `quick-mail` 的实现逻辑：在获取验证码时，不强绑定单个 `forward_to`，而是扫描系统内配置的所有转发接收邮箱。只要目标账号的 HME 别名出现在任何一个接收邮箱的收件列表中，就从该邮件中提取验证码。

## 可行性分析：高度可行
当前 `auto-gpt` 和 `icloud-mail-helper` 的匹配逻辑已经非常严谨。邮件匹配并非仅仅依赖“从哪个信箱取回”，而是通过扫描邮件的 Header（如 `for <alias>`, `delivered-to: alias`, `x-original-to: alias`）来确认归属（`messageMatchesAlias` 函数）。因此，在多个信箱中进行地毯式搜索是完全安全的，不会发生“张冠李戴”的情况。

### 为什么需要这种机制？（优点）
1. **防止状态脱节**：iCloud 官网上的真实转发地址可能因为手动操作或同步延迟，与本地数据库中记录的 `forward_to` 不一致。
2. **Apple 路由延迟**：有时 Apple 在切换转发地址后，仍会有部分邮件路由到旧信箱。
3. **极高的容错率**：只要验证码落入了我们的任意一个监控信箱，任务就能成功。

### 潜在挑战（缺点）
1. **API 请求量倍增**：如果系统配置了 5 个 `forward_to`，每次轮询都需要发起 5 次请求。由于默认轮询间隔为 3 秒，可能会对后端的 TempMail 或 IMAP 造成较大的 QPS 压力。
2. **性能开销**：轮询耗时增加。

---

## 改造方案

### 1. 针对 `icloud-mail-helper` (HME Ready API 服务端)
核心修改点位于 `/opt/icloud-hide-email-helper/hme-ready-service.js`。

**当前逻辑：**
`resolveLeaseReceiver` 会根据 Lease/Alias 的信息，严格挑选 **1个** `forwardTo`，随后 `matchingEmailRows` 只从这 **1个** Receiver 中拉取邮件。

**改造逻辑：**
1. **聚合所有可用信箱**：从配置中提取 `defaultForwardTo` 以及 `forwardReceivers` 字典中的所有 Keys。
2. **并行查询**：在 `matchingEmailRows` 函数中，使用 `Promise.all` 遍历所有可用的 `forwardTo`。
3. **合并过滤**：对每个信箱拉取 `listReceiverEmails`，统一丢入 `messageMatchesAlias` 进行校验。
4. **适配返回值**：在匹配到的邮件中，标记它是从哪个 Receiver 获取的，以保证 `withDetail` 获取正文时请求正确的 Receiver。

### 2. 针对 `auto-gpt` 原生 fallback 模式 (`IcloudHmeMailbox`)
若没有使用 `helper_ready_api`，核心修改点位于 `/opt/auto-gpt/core/base_mailbox.py` 的 `wait_for_code` 方法。

**当前逻辑：**
```python
mailbox_id = self._shared_mailbox_id_for(account)
mails = self._tempmail_mailbox._list_emails(mailbox_id)
```

**改造逻辑：**
需要为 `IcloudHmeMailbox` 提供一个全局的 `fallback_forward_tos` 列表。
在 `poll_once()` 中，迭代所有的 `mailbox_id`：
```python
for fwd_to in all_forward_tos:
    mailbox_id = self._shared_mailbox_id_for(MailboxAccount(email=account.email, extra={"forward_to": fwd_to}))
    mails = self._tempmail_mailbox._list_emails(mailbox_id)
    # 走后续解析...
```

---

## 结论与建议
这个思路**非常值得落地**，是提高自动化注册成功率的杀手锏。
建议在 `icloud-mail-helper` 层面优先实现，因为：
1. 它是无状态的 API 服务，改动收敛。
2. 可以在 `Promise.all` 中加入一定的并发控制或错误忽略（某个节点失败不影响整体）。
3. 可以在配置中加一个开关（如 `HME_READY_SCAN_ALL_RECEIVERS=true`），平滑过渡。
