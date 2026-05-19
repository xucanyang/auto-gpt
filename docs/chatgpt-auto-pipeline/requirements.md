# Requirements Document

## Introduction

本需求文档定义 ChatGPT 自动流水线模块在 `any-auto-register-local-live-icloud-hme` 项目中的目标行为。

该模块不是把“单个账号从注册一路同步串到支付和补抓”的长链路硬编码在一个线程里，而是采用更稳定的编排方式：

- 注册任务作为“生产者”持续产出账号
- 支付任务作为“消费者”按固定时间从待支付账号池中取账号，批量执行支付
- Auth 补抓作为可选的回收步骤，对支付成功账号顺序处理

流水线只做调度与状态编排，各步骤尽量复用现有实现：

- 注册：复用现有注册任务
- 订阅链接：复用现有 `payment_link` action
- GoPay：复用现有 GoPay batch
- Auth 补抓：复用现有 `resume-subscription-auth` task

## Glossary

- **Pipeline_Engine**：自动流水线调度器，负责注册补货、支付批处理、可选 Auth 补抓调度
- **PipelineTask**：一条流水线任务，表示一次长期运行的自动流水线实例
- **PipelineAccountItem**：流水线中的单账号记录，表示账号从注册、入池、支付、可选补抓的完整状态
- **Payment_Pool**：待支付账号池，只接收流水线自己新注册成功的账号
- **Paid_Pool**：支付成功账号池，用于可选 Auth 补抓
- **Failed_Pool**：支付失败或补抓失败账号池
- **GoPay_Batch**：现有 GoPay 批量支付系统，包含手机号递延、窗口恢复、任务持久化能力
- **Primary_Account_Status**：账号主状态，对应 `accounts.status`，只表达稳定的账号结果状态

## Requirements

### Requirement 1: 流水线配置管理

**User Story:** As a 运维人员, I want 配置流水线调度参数, so that 注册补货、支付节奏和可选补抓行为符合预期。

#### Acceptance Criteria

1. 系统 SHALL 支持以下基础配置参数：
   - `payment_pool_threshold`：待支付池补货阈值
   - `payment_pool_target`：触发补货后补到的目标池深度
   - `payment_batch_interval_seconds`：支付批处理固定执行间隔
   - `payment_batch_max_size`：单批支付最大数量，`0` 表示不额外限制
   - `auto_start`：服务启动时是否自动启动流水线
   - `enable_auth_capture`：是否启用 Auth 补抓，默认 `false`
   - `auth_poll_interval_seconds`：Auth 补抓任务轮询间隔
   - `register_poll_interval_seconds`：注册任务轮询间隔
   - `gopay_batch_poll_interval_seconds`：GoPay batch 状态轮询间隔
   - `gopay_timeout_seconds`：支付任务超时时间
2. 系统 SHALL 继承现有注册与支付参数：
   - `platform=chatgpt`
   - `mail_provider`
   - `proxy`
   - `executor_type`
   - `captcha_solver`
   - 注册模式相关 `extra`
   - GoPay 支付参数 `country / currency / plan`
3. 当 `payment_pool_threshold < 1`、`payment_pool_target < payment_pool_threshold`、或任何时间参数小于 `0` 时，系统 SHALL 拒绝保存配置并返回校验错误。
4. 流水线配置 SHALL 以 JSON 形式持久化到现有 `config_store`。
5. 流水线运行中修改配置时，系统 MAY 限制部分关键参数不可立即修改；至少对会影响当前调度结构的核心参数返回明确错误或提示。

### Requirement 2: 注册补货调度

**User Story:** As a 运维人员, I want 流水线自动补充待支付账号池, so that 支付端持续有可用账号可消费。

#### Acceptance Criteria

1. 流水线 SHALL 只把“流水线自己新注册出来的账号”加入待支付账号池。
2. 当待支付账号池数量低于 `payment_pool_threshold` 时，系统 SHALL 触发注册补货。
3. 一次补货的目标 SHALL 为将待支付账号池补到 `payment_pool_target`。
4. 注册补货 SHALL 复用现有注册任务，而不是重写注册逻辑。
5. 注册任务应带有可识别的流水线来源标记，以便后续只筛选流水线产生的账号进入待支付池。
6. 注册成功账号进入待支付池时，系统 SHALL 记录对应的流水线账号记录，并标记为待支付。
7. 注册失败不会阻塞支付调度，但 SHALL 在流水线账号记录中标记失败原因。
8. 系统 SHALL 在以下时机重新评估是否需要补货：
   - 流水线启动时
   - 流水线从暂停恢复时
   - 任一注册任务进入终态后
   - 任一支付批次进入终态并导致待支付池数量变化后
9. 同一时刻系统 SHALL 至多存在一个活跃的注册补货任务，避免重复补货。

### Requirement 3: 支付批处理调度

**User Story:** As a 运维人员, I want 流水线按固定时间批量支付账号, so that 支付节奏可控且稳定。

#### Acceptance Criteria

1. 系统 SHALL 按固定参数 `payment_batch_interval_seconds` 周期性检查是否应启动新一批支付。
2. 只有同时满足以下条件时，系统 SHALL 启动新一批支付：
   - 流水线处于运行状态
   - 当前没有活跃的 GoPay batch 任务
   - 待支付账号池中存在可消费账号
   - 手机号池中存在可用手机号
3. “可用手机号” SHALL 明确定义为：现有 GoPay 手机号池中 `enabled=true` 且 `status=ready` 的唯一手机号条目。
4. 同一时刻系统 SHALL 至多启动一个活跃的 GoPay batch 任务。
5. 每批支付数量 SHALL 为：
   `min(待支付账号数量, 当前可用手机号数量, payment_batch_max_size 或无穷大)`
6. 一旦账号被选入支付批次，系统 SHALL 先将其状态标记为“支付预留”，以避免被下一次调度重复选中。
7. 支付批次 SHALL 复用现有 GoPay batch 能力，而不是重新实现手机号池分配、递延、状态管理。
8. 如果支付批次启动失败，系统 SHALL 将该批中相关账号退回失败池，并记录失败原因。

### Requirement 4: 订阅链接生成时机

**User Story:** As a 运维人员, I want 订阅链接只在即将支付时生成, so that 避免链接提前失效和数据污染。

#### Acceptance Criteria

1. 系统 SHALL 在账号进入当次支付批次后、正式启动 GoPay 之前，为该批账号生成订阅链接。
2. 系统 SHALL 使用现有 `payment_link` action 生成订阅链接，而不是直接调用底层支付函数。
3. 系统 SHALL 将生成得到的 `checkout_url` 写入流水线账号记录。
4. 若订阅链接生成失败，系统 SHALL 将该账号标记为支付失败，并写明失败阶段为“payment_link”。
5. 流水线 SHALL 不为尚未进入支付批次的账号提前生成订阅链接。

### Requirement 5: GoPay 支付执行

**User Story:** As a 开发者, I want 支付阶段完全复用现有 GoPay batch, so that 继承手机号递延、窗口恢复和已有支付状态管理能力。

#### Acceptance Criteria

1. 流水线 SHALL 使用现有 GoPay batch 创建支付批次。
2. 流水线 SHALL 不自行维护 Phone Pool 占用与释放，而是完全依赖现有 GoPay batch 内部逻辑。
3. 流水线 SHALL 轮询现有 GoPay batch 任务状态，并将每个账号的支付结果同步到流水线账号记录。
4. 支付超时后，系统 SHALL 尝试取消该批中未完成项，并把相关账号标记为支付失败。
5. 支付成功的账号进入 `Paid_Pool`。
6. 支付失败的账号进入 `Failed_Pool`。

### Requirement 6: Auth 补抓为可选步骤

**User Story:** As a 运维人员, I want Auth 补抓可以选择开启或关闭, so that 第一版默认只跑注册和支付。

#### Acceptance Criteria

1. 系统 SHALL 提供 `enable_auth_capture` 配置项，默认值为 `false`。
2. 当 `enable_auth_capture=false` 时，支付成功账号 SHALL 直接标记为流水线完成，不进入 Auth 队列。
3. 当 `enable_auth_capture=true` 时，支付成功账号 SHALL 进入待补抓队列。
4. Auth 补抓 SHALL 采用单线程、顺序执行。
5. Auth 补抓 SHALL 复用现有 `resume-subscription-auth` task。
6. Auth 补抓失败 SHALL 单独记录为 Auth 失败，不得把已支付账号重新降级为未支付。

### Requirement 7: 账号池与来源隔离

**User Story:** As a 运维人员, I want 流水线账号池只包含本流水线产生的账号, so that 不污染历史账号和人工操作结果。

#### Acceptance Criteria

1. 流水线 SHALL 仅消费本流水线新注册出来的账号。
2. 历史 `registered` 账号 SHALL 默认不自动进入待支付池。
3. 流水线需要有明确的账号来源标识，用于区分：
   - 流水线注册账号
   - 手工注册账号
   - 其他任务产生账号
4. 系统 SHALL 避免把非流水线来源账号错误纳入支付批次。

### Requirement 8: 失败原因记录

**User Story:** As a 运维人员, I want 明确看到每个账号失败在什么阶段、因为什么失败, so that 便于排查和后续重试策略设计。

#### Acceptance Criteria

1. 注册失败 SHALL 记录至少以下信息：
   - 失败阶段
   - 错误代码
   - 人类可读失败原因
   - 原始错误详情
2. 支付失败 SHALL 记录至少以下信息：
   - 失败阶段（如：订阅链接、GoPay 启动、GoPay 运行中）
   - 错误代码
   - 人类可读失败原因
   - 原始错误详情
3. Auth 失败 SHALL 记录至少以下信息：
   - 错误代码
   - 人类可读失败原因
   - 原始错误详情
4. 系统 SHALL 能正确表达常见失败类型，例如：
   - 账号无资格支付
   - 代理问题
   - 订阅链接无效
   - GoPay 会话丢失
   - OTP 超时
   - `add_phone` 问题
5. 成功的账号也 SHALL 明确记录成功结果，而不是仅通过缺少错误字段来间接表示成功。
6. 成功记录至少应包含成功阶段、成功时间以及用于前端展示的成功摘要。

### Requirement 9: 账号主状态与流程状态分层

**User Story:** As a 开发者, I want 把账号主状态与流水线流程状态分开, so that 避免刷新后状态互相覆盖。

#### Acceptance Criteria

1. 系统 SHALL 将 `accounts.status` 视为稳定主状态，而不是流水线瞬时状态。
2. `accounts.status` 至少需要稳定表达：
   - `registered`
   - `subscribed`
   - `payment_failed`
   - `invalid`
   - `expired`
   - `trial`
3. 流水线中的瞬时状态 SHALL 存放在独立的流水线账号记录中，而不是直接写入 `accounts.status`。
4. 支付成功后，`accounts.status` SHALL 更新为支付成功主状态。
5. 支付失败后，`accounts.status` SHALL 更新为支付失败主状态。
6. Auth 补抓成功或失败 SHALL 不得覆盖支付阶段已经确认的主状态。
7. 现有探测/同步逻辑若参与状态写入，系统 SHALL 避免把已支付账号错误回退为“已注册”。
8. 现有账号列表页面在刷新、轮询或重新进入页面后，系统 SHALL 保证已支付账号不会因旧探测结果、缺失的能力快照或不完整的本地状态而回退显示为“已注册”。
9. 现有账号列表页面 SHALL 能同时拿到稳定主状态与可展示的流程摘要，而不是仅依赖单一 `accounts.status` 做全部判断。

### Requirement 10: 支付成功后的订阅类型刷新

**User Story:** As a 运维人员, I want 支付成功后订阅类型自动刷新, so that 账号列表不仅显示已支付，还能显示正确套餐类型。

#### Acceptance Criteria

1. 支付成功后，系统 SHALL 触发一次订阅信息刷新流程。
2. 刷新结果 SHALL 同步到账号可展示的订阅信息字段中。
3. 如果刷新成功，账号列表中的订阅类型应显示正确套餐，而不是保留旧值或空值。
4. 如果刷新失败，系统 SHALL 保留“已支付”主状态，并记录订阅刷新失败信息。
5. 订阅类型刷新失败不得导致账号主状态回退。
6. 当订阅类型刷新尚未成功时，前端 SHALL 允许展示“已支付 + 套餐待刷新/unknown”之类的真实中间状态，而不是静默展示过期或误导性的旧套餐类型。
7. 现有账号列表页面在支付成功后的下一次刷新或轮询中，SHALL 能展示最新的订阅类型或“待刷新/unknown”中间态，而不是继续展示旧套餐类型。

### Requirement 11: 持久化模型

**User Story:** As a 运维人员, I want 流水线状态持久化, so that 页面关闭、刷新或服务重启后仍能恢复查看。

#### Acceptance Criteria

1. 系统 SHALL 有独立的 `PipelineTask` 表，用于记录流水线任务整体状态。
2. 系统 SHALL 有独立的 `PipelineAccountItem` 表，用于记录单账号从注册、入池、支付到可选补抓的完整状态。
3. `PipelineAccountItem` 至少需要持久化：
   - 账号来源
   - 当前流程状态（如：`pending_register / registering / pending_payment / payment_reserved / link_generating / paying / paid / auth_pending / auth_running / done / failed / auth_failed`）
   - 注册阶段状态（至少：`pending / running / success / failed`）
   - 支付阶段状态（至少：`pending / reserved / link_generating / link_ready / paying / success / failed`）
   - Auth 阶段状态（至少：`disabled / pending / running / success / failed / skipped`）
   - 支付批次标识
   - 订阅链接
   - 订阅类型信息（至少包括目标套餐与确认套餐）
   - 注册失败原因字段
   - 支付失败原因字段
   - Auth 失败原因字段
   - 关键时间戳字段（注册开始/结束、支付开始/结束、Auth 开始/结束）
4. 系统 SHALL 保留历史流水线任务记录，而不是只覆盖当前一条任务。
5. 页面关闭再打开后，系统 SHALL 能恢复当前流水线状态、账号池和最近批次执行情况。

### Requirement 12: 停止、暂停与恢复语义

**User Story:** As a 运维人员, I want 停止和暂停行为可预测, so that 不会误伤已经启动的子任务。

#### Acceptance Criteria

1. 点击停止后，系统 SHALL 停止新的注册补货调度、支付批处理调度和 Auth 调度。
2. 点击停止后，已经启动的注册任务、GoPay batch、Auth task SHALL 继续运行直至自然结束，默认不强制取消。
3. 点击暂停后，系统 SHALL 暂停新的调度动作，但保留当前状态。
4. 恢复后，系统 SHALL 从暂停状态继续调度，而不是重置全部进度。
5. 服务重启后，系统 SHALL 先对持久化状态与当前子任务真实状态进行对账，再决定继续调度、接管活跃 GoPay batch 或将不可恢复的中间态标记为失败/待处理。

### Requirement 13: 前端自动流水线页面

**User Story:** As a 运维人员, I want 用单独页面管理自动流水线, so that 不把复杂状态继续堆进账号主列表页面。

#### Acceptance Criteria

1. 系统 SHALL 提供独立的“自动流水线”页面入口。
2. 页面 SHALL 包含：
   - 配置区
   - 启停控制区
   - 待支付池区
   - 当前批量支付任务区
   - 已支付池区
   - 失败池区
   - 可选 Auth 队列区
   - 实时日志区
3. 页面 SHALL 能展示每个账号当前所处阶段、阶段状态、失败原因、更新时间。
4. 页面 SHALL 能恢复展示当前活跃流水线和活跃 GoPay batch 的状态。
5. 本次交付范围 SHALL 同步修复现有 `Accounts` 页面中的状态更新问题，使其与自动流水线页面使用一致的状态语义和展示口径。

### Requirement 14: API 接口

**User Story:** As a 前端开发者, I want 后端提供完整的流水线接口, so that 前端可以管理和展示流水线全貌。

#### Acceptance Criteria

1. 系统 SHALL 提供 `GET /api/pipeline/config`
2. 系统 SHALL 提供 `PUT /api/pipeline/config`
3. 系统 SHALL 提供 `POST /api/pipeline/start`
4. 系统 SHALL 提供 `POST /api/pipeline/stop`
5. 系统 SHALL 提供 `POST /api/pipeline/pause`
6. 系统 SHALL 提供 `GET /api/pipeline/status`
7. 系统 SHALL 提供 `GET /api/pipeline/history`
8. 系统 SHALL 提供 `GET /api/pipeline/logs/stream`
9. `GET /api/pipeline/status` 的返回结果 SHALL 能直接支持前端展示：
   - 当前任务状态
   - 待支付池
   - 活跃支付批次
   - 已支付池
   - 失败池
   - 可选 Auth 队列
10. 当持久化的流水线账号记录引用了仍然活跃的 GoPay batch 时，`GET /api/pipeline/status` SHALL 返回该活跃 batch 的最新状态快照，以支持页面刷新后的任务恢复展示。

### Requirement 15: 模块独立性与现有能力复用

**User Story:** As a 开发者, I want 模块独立且尽量复用现有能力, so that 降低回归风险并减少重复实现。

#### Acceptance Criteria

1. 流水线编排代码 SHALL 位于独立模块目录中。
2. 流水线路由 SHALL 位于独立 API 模块中。
3. 流水线应优先复用现有注册、支付链接、GoPay batch、Auth task 逻辑。
4. 除非为了解决状态一致性或必要集成问题，系统 SHOULD 避免大幅改动现有模块内部实现。
5. 若现有模块存在状态不一致问题，流水线实现 MAY 引入必要的最小修复，以确保：
   - 已支付账号不会刷新后回退为已注册
   - 支付成功后订阅类型能够刷新
6. 上述状态一致性修复不仅适用于自动流水线页面，也 SHALL 适用于现有账号列表页面及其刷新链路。
