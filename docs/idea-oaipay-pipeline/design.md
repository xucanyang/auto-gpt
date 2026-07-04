# Idea / 手机号 / OAIPay 账号处理流水线设计

## 目标

新增一条可配置、可观察、可恢复的账号处理流水线，把现有能力按账号维度串起来：

```text
账号来源（注册新账号 / 本地账号快照）
  -> 可选 Idea 提交
  -> 可选本地状态刷新
  -> 可配置状态放行
  -> 可选手机号绑定
  -> 可选 OAIPay 上传
```

该流水线不是“模式二选一”。注册和本地选择只是账号来源；Idea、状态刷新、手机号绑定、OAIPay 都是可启用/可跳过的步骤。

## 设计原则

1. **账号级状态优先**：每个账号独立记录 register / idea / check / phone / oaipay / overall 状态，不能只有整批大状态。
2. **不写死 plus**：本地状态刷新是通用检验；是否要求 plus/free/账号有效/上传就绪由 `status_gate` 配置决定。
3. **复用现有成熟链路**：注册复用 `enqueue_register_task`，Idea 复用 `enqueue_baxigpt_cdk_submit_task`，手机号复用 `enqueue_phone_binding_test_task`，OAIPay 复用 `backfill_chatgpt_account_to_oaipay`。
4. **幂等与补跑**：远端已存在、已 paid、已绑定手机号、已上传 OAIPay 都应能识别并跳过；失败项可按单账号/单阶段重试。
5. **运行事实可回放**：保存子任务 id、order_id、远端状态、失败原因、主流水线日志，服务重启后能恢复或安全标记中间态。
6. **敏感参数不进展示日志**：代理、token、手机号 API 密钥等只进入运行配置，不在 UI 明文展示。

## 流程配置

### 账号来源 `source`

- `type=register`：注册新账号，并以最终成功数为目标持续补位。
- `type=local`：从选中账号 / 当前筛选范围生成一次性账号快照，运行中不再动态跟随筛选条件。

### Idea `idea`

- `enabled=true` 时，对未满足跳过条件的账号执行 Idea 提交。
- 支持卡密池或粘贴卡密。
- 上游 paid 只代表 Idea 订单成功，不代表本地状态已满足放行条件。

### 本地状态刷新 `check`

- `enabled=true` 时调用本地 ChatGPT 状态刷新，写回账号列表状态缓存。
- `status_gate` 决定是否放行：
  - `none`：不限制。
  - `account_valid`：只要求账号非 invalid / auth invalid。
  - `subscription_in`：订阅类型在允许列表内，例如 `free`、`plus`。
  - `upload_ready`：按现有上传能力判断。

### 手机号 `phone`

- `policy=disabled`：不绑定。
- `policy=best_effort`：尝试绑定，失败不阻塞后续 OAIPay。
- `policy=required`：绑定失败则该账号停止在手机号阶段。
- `apply_to` 可控制只对 free / plus / 放行账号执行。

### OAIPay `oaipay`

- `enabled=true` 时先探测远端，再上传。
- 远端已存在默认视为成功。
- ambiguous / cross-workspace 等不安全状态进入人工处理。

## 状态模型

### 任务表：`idea_oaipay_pipeline_tasks`

| 字段 | 含义 |
| --- | --- |
| id | 主键 |
| task_key | 任务 key |
| status | running / paused / stopped / done / failed |
| source_type | register / local |
| target_success_count | 目标最终成功数；本地账号来源可为 0 |
| config_json | 脱敏后的配置快照 |
| runtime_config_json | 运行配置，避免直接展示敏感字段 |
| summary_json | 汇总统计 |
| logs_json | 主流水线日志 |
| active_register_task_id | 活跃注册任务 |
| active_idea_task_id | 活跃 Idea 提交任务 |
| active_phone_task_id | 活跃手机号任务 |
| last_error | 最近错误 |
| started_at / stopped_at / created_at / updated_at | 时间字段 |

### 明细表：`idea_oaipay_pipeline_items`

| 字段 | 含义 |
| --- | --- |
| pipeline_task_id | 所属任务 |
| account_id / email | 账号引用 |
| source_stage | registered / selected |
| register_stage | pending / running / success / failed / skipped |
| idea_stage | pending / submitting / polling / paid / failed / timeout / skipped / disabled |
| check_stage | pending / running / refreshed / failed / skipped / disabled |
| gate_stage | pass / blocked / skipped |
| phone_stage | disabled / pending / running / success / failed / skipped |
| oaipay_stage | disabled / pending / probing / uploaded / exists / ambiguous / failed / skipped |
| overall_status | pending / running / done / failed / manual_required / skipped |
| subscription_type_before / after | 检验前后订阅状态 |
| idea_task_id / cdk_id / order_id | Idea 追踪字段 |
| phone_task_id / phone_policy | 手机号追踪字段 |
| oaipay_remote_state / message | OAIPay 追踪字段 |
| last_error | 当前账号最后失败原因 |

## 默认策略

- 启用 Idea：默认状态放行为 `subscription_in: [plus]`，手机号 `best_effort`，OAIPay 开启。
- 不启用 Idea：默认状态放行为 `account_valid`，手机号和 OAIPay 按用户选择。
- free 账号绑定手机号：关闭 Idea，启用状态刷新，放行 `subscription_in: [free]` 或 `account_valid`，手机号 `required/best_effort`，OAIPay 可关闭或按需开启。

## API

- `POST /api/idea-oaipay-pipeline/start`：创建并启动流水线。
- `POST /api/idea-oaipay-pipeline/pause`：暂停调度新动作。
- `POST /api/idea-oaipay-pipeline/resume`：恢复。
- `POST /api/idea-oaipay-pipeline/stop`：停止。
- `GET /api/idea-oaipay-pipeline/status`：当前任务、汇总、队列、日志。
- `GET /api/idea-oaipay-pipeline/history`：历史任务。
- `POST /api/idea-oaipay-pipeline/items/{id}/retry/{stage}`：后续补单入口。

## 首版实现边界

首版交付：

1. 任务/明细持久化。
2. 注册来源和本地账号来源。
3. Idea 提交任务启动与结果回收。
4. 本地状态刷新和可配置放行。
5. 手机号绑定任务启动与结果回收。
6. OAIPay 上传。
7. 前端配置、运行态、明细表、日志。
8. 暂停、恢复、停止。

首版不做：多条流水线并发、复杂优先级、自动购买卡密、跨项目远端修改。

## 风险与处理

- **Idea 任务只返回整批结果**：通过 task meta/runtime_results、账号 extra.baxigpt_cdk、baxigpt_cdk_pool 共同回收结果。
- **手机号任务批量结果结构不稳定**：首版串行单账号绑定，降低状态归因复杂度。
- **OAIPay 远端 ambiguous**：不强传，进入人工处理。
- **服务重启中间态丢失**：重启时 active child task 若已不存在，则把对应 item 标记为 failed/manual_required，并保留子任务 id。
