# Tasks

## Task 1: 创建流水线模块骨架

- [x] 1.1 创建 `services/pipeline/` 目录
- [x] 1.2 创建基础文件：
  - `services/pipeline/__init__.py`
  - `services/pipeline/models.py`
  - `services/pipeline/config.py`
  - `services/pipeline/state.py`
  - `services/pipeline/engine.py`
  - `services/pipeline/register_scheduler.py`
  - `services/pipeline/payment_scheduler.py`
  - `services/pipeline/auth_scheduler.py`
  - `services/pipeline/logs.py`
- [x] 1.3 创建 `api/pipeline.py`
- [x] 1.4 创建前端页面文件 `frontend/src/pages/Pipeline.tsx`

## Task 2: 定义配置模型与配置存储

- [x] 2.1 在 `models.py` 中定义 `PipelineConfig`
- [x] 2.2 在 `config.py` 中实现配置加载、保存、校验
- [x] 2.3 实现以下配置项：
  - `payment_pool_threshold`
  - `payment_pool_target`
  - `payment_batch_interval_seconds`
  - `payment_batch_max_size`
  - `auto_start`
  - `enable_auth_capture`
  - `auth_poll_interval_seconds`
  - `register_poll_interval_seconds`
  - `gopay_batch_poll_interval_seconds`
  - `gopay_timeout_seconds`
- [x] 2.4 接入 `config_store`
- [x] 2.5 实现配置运行中修改的限制策略

## Task 3: 定义数据模型与数据库表

- [x] 3.1 在 `models.py` 中定义 `PipelineTask`
- [x] 3.2 在 `models.py` 中定义 `PipelineAccountItem`
- [x] 3.3 在 `core/db.py` 中接入新表的建表逻辑
- [x] 3.4 为关键查询字段建立索引：
  - `pipeline_task_id`
  - `account_id`
  - `email`
  - `pipeline_status`
  - `register_stage`
  - `payment_stage`
  - `auth_stage`
- [x] 3.5 明确并实现 `PipelineAccountItem` 的最小状态字段集合

## Task 4: 实现状态持久化与原子更新接口

- [x] 4.1 在 `state.py` 中实现 `PipelineStateStore`
- [x] 4.2 实现 `PipelineTask` 的创建、读取、更新
- [x] 4.3 实现 `PipelineAccountItem` 的创建、批量查询、状态更新
- [x] 4.4 实现账号从 `pending_payment` 到 `payment_reserved` 的原子预留更新
- [x] 4.5 实现队列视图查询：
  - 待支付池
  - 已支付池
  - 失败池
  - 待补抓池
- [x] 4.6 实现历史任务查询

## Task 5: 实现日志总线

- [x] 5.1 在 `logs.py` 中实现流水线日志发布器
- [x] 5.2 支持内存订阅与取消订阅
- [x] 5.3 支持 SSE 消费
- [x] 5.4 约束最近日志缓冲长度

## Task 6: 实现注册补货调度器

- [x] 6.1 在 `register_scheduler.py` 中实现补货判断逻辑
- [x] 6.2 实现待支付池深度计算
- [x] 6.3 实现“低于阈值补到目标值”的补货量计算
- [x] 6.4 复用现有 `enqueue_register_task(...)`
- [x] 6.5 注册任务调用时写入流水线来源标记：
  - `source="pipeline"`
  - `meta.pipeline_task_id`
  - `meta.pipeline_key`
- [x] 6.6 轮询现有注册任务状态直到终态
- [x] 6.7 注册成功后把本轮新增账号写入 `PipelineAccountItem`
- [x] 6.8 注册失败时写入注册阶段错误字段
- [x] 6.9 保证同一时刻最多一个活跃注册补货任务

## Task 7: 实现注册结果回收与账号来源识别

- [x] 7.1 设计“流水线自己注册出来的账号”识别策略
- [x] 7.2 实现按注册任务来源和任务上下文定位新增账号
- [x] 7.3 防止历史人工账号被错误纳入待支付池
- [x] 7.4 为流水线来源账号写入必要的来源标记

## Task 8: 实现支付批处理调度器

- [x] 8.1 在 `payment_scheduler.py` 中实现固定间隔检查循环
- [x] 8.2 实现“当前没有活跃 GoPay batch”判断
- [x] 8.3 实现可用手机号数量计算：
  - `enabled=true`
  - `status=ready`
- [x] 8.4 实现支付批量大小计算
- [x] 8.5 从待支付池中原子预留账号
- [x] 8.6 为预留账号生成订阅链接
- [x] 8.7 复用现有 GoPay batch 启动支付批次
- [x] 8.8 记录 `payment_batch_task_id`
- [x] 8.9 轮询现有 GoPay batch 状态并同步回 `PipelineAccountItem`

## Task 9: 实现订阅链接生成适配

- [x] 9.1 复用现有 `payment_link` action
- [x] 9.2 为支付批次中账号逐个生成订阅链接
- [x] 9.3 将 `checkout_url` 写入 `PipelineAccountItem`
- [x] 9.4 链接生成失败时写入：
  - `payment_failed_stage=payment_link`
  - `payment_error_code`
  - `payment_error_reason`
  - `payment_error_detail`

## Task 10: 实现 GoPay batch 结果映射

- [x] 10.1 将现有 GoPay batch item 状态映射到 `payment_stage`
- [x] 10.2 支付成功账号转入 `Paid_Pool`
- [x] 10.3 支付失败账号转入 `Failed_Pool`
- [x] 10.4 超时后取消未完成批次项
- [x] 10.5 同步成功/失败摘要到 `PipelineAccountItem`
- [x] 10.6 保留现有手机号递延、失败释放、批次恢复逻辑，不重复实现

## Task 11: 实现 Auth 补抓调度器

- [x] 11.1 在 `auth_scheduler.py` 中实现 `enable_auth_capture` 判断
- [x] 11.2 当 `enable_auth_capture=false` 时，把支付成功账号直接标记为完成
- [x] 11.3 当 `enable_auth_capture=true` 时，把支付成功账号加入待补抓池
- [x] 11.4 顺序调用现有 `enqueue_resume_subscription_auth_task(account_id)`
- [x] 11.5 轮询现有 Auth task 状态直到终态
- [x] 11.6 Auth 成功时更新 `auth_stage=success`
- [x] 11.7 Auth 失败时更新 `auth_stage=failed` 与 Auth 错误字段
- [x] 11.8 确保 Auth 失败不回退账号主状态

## Task 12: 实现支付成功后的订阅类型刷新

- [x] 12.1 设计支付成功后的订阅刷新调用点
- [x] 12.2 支付成功后触发一次本地订阅探测/刷新
- [x] 12.3 将刷新结果写入账号可展示字段
- [x] 12.4 将确认套餐写入 `subscription_plan_confirmed`
- [x] 12.5 刷新失败时：
  - 保留主状态 `subscribed`
  - 标记 `subscription_refresh_status=failed`
  - 支持 `unknown` 展示

## Task 13: 修复主状态与流程状态覆盖问题

- [x] 13.1 梳理现有 `accounts.status` 写入链路
- [x] 13.2 限制流水线瞬时状态不直接写入 `accounts.status`
- [x] 13.3 支付成功时稳定写入 `subscribed`
- [x] 13.4 支付失败时稳定写入 `payment_failed`
- [x] 13.5 避免探测/同步把已支付账号回退成 `registered`
- [x] 13.6 Auth 失败不覆盖支付主状态
- [x] 13.7 修复现有 `Accounts` 页面刷新后已支付账号可能回退显示为“已注册”的问题
- [x] 13.8 修复现有 `Accounts` 页面支付成功后订阅类型未及时更新的问题
- [x] 13.9 为现有 `Accounts` 页面补充与自动流水线页面一致的状态展示口径

## Task 14: 实现恢复与对账逻辑

- [x] 14.1 在 `engine.py` 中实现启动时恢复入口
- [x] 14.2 恢复最近活跃或最近一条 `PipelineTask`
- [x] 14.3 恢复 `PipelineAccountItem` 队列视图
- [x] 14.4 对账现有活跃 GoPay batch
- [x] 14.5 识别不可恢复的注册中间态并转失败或待重新评估
- [x] 14.6 识别不可恢复的 Auth 中间态并转待补抓或失败
- [x] 14.7 接入 `auto_start`

## Task 15: 实现 PipelineEngine 生命周期控制

- [x] 15.1 在 `engine.py` 中实现 `PipelineEngine`
- [x] 15.2 实现 `start()`
- [x] 15.3 实现 `stop()`
- [x] 15.4 实现 `pause()`
- [x] 15.5 实现 `resume()`
- [x] 15.6 实现“停止只停新调度，不取消已启动子任务”
- [x] 15.7 实现三类调度器的协调调用

## Task 16: 实现 API 路由

- [x] 16.1 在 `api/pipeline.py` 中定义 router
- [x] 16.2 实现 `GET /api/pipeline/config`
- [x] 16.3 实现 `PUT /api/pipeline/config`
- [x] 16.4 实现 `POST /api/pipeline/start`
- [x] 16.5 实现 `POST /api/pipeline/stop`
- [x] 16.6 实现 `POST /api/pipeline/pause`
- [x] 16.7 实现 `GET /api/pipeline/status`
- [x] 16.8 实现 `GET /api/pipeline/history`
- [x] 16.9 实现 `GET /api/pipeline/logs/stream`
- [x] 16.10 在 `GET /api/pipeline/status` 中返回：
  - 当前任务状态
  - 配置快照
  - 待支付池
  - 已支付池
  - 失败池
  - 待补抓池
  - 活跃 GoPay batch 快照

## Task 17: 接入主应用

- [x] 17.1 在 `main.py` 注册 `pipeline_router`
- [x] 17.2 在生命周期中接入 `PipelineEngine`
- [x] 17.3 在服务启动时根据 `auto_start` 决定是否恢复并启动
- [x] 17.4 在服务停止时优雅停止调度器

## Task 18: 实现前端自动流水线页面

- [x] 18.1 创建 `frontend/src/pages/Pipeline.tsx`
- [x] 18.2 实现配置区
- [x] 18.3 实现启停控制区
- [x] 18.4 实现待支付池表格
- [x] 18.5 实现当前批量支付任务区
- [x] 18.6 实现已支付池区
- [x] 18.7 实现失败池区
- [x] 18.8 实现可选 Auth 队列区
- [x] 18.9 实现实时日志区
- [x] 18.10 页面恢复时自动拉取当前状态和活跃 batch

## Task 19: 修复现有 Accounts 页面状态展示

- [x] 19.1 梳理 `frontend/src/pages/Accounts.tsx` 当前主状态、订阅类型、能力快照的展示来源
- [x] 19.2 让现有 `Accounts` 页面兼容“主状态 + 订阅刷新中间态”展示
- [x] 19.3 为现有 `Accounts` 页面增加“已支付 + 套餐待刷新/unknown”显示逻辑
- [x] 19.4 避免 `Accounts` 页面因旧 `chatgpt_capabilities` 或旧 `chatgpt_local` 把已支付账号渲染回“已注册”
- [x] 19.5 验证现有账号列表页与自动流水线页对同一账号展示一致

## Task 20: 接入前端导航与路由

- [x] 20.1 在 `App.tsx` 中新增自动流水线页面路由
- [x] 20.2 在侧边导航中新增入口
- [x] 20.3 处理选中态与页面跳转

## Task 21: 联调与验证

- [x] 21.1 验证注册补货逻辑
- [x] 21.2 验证支付批处理触发逻辑
- [x] 21.3 验证可用手机号数量计算逻辑
- [x] 21.4 验证订阅链接只在支付前生成
- [x] 21.5 验证支付成功后主状态与订阅类型刷新
- [x] 21.6 验证 Auth 默认关闭行为
- [x] 21.7 验证 Auth 开启后的顺序补抓
- [x] 21.8 验证支付失败/补抓失败原因记录
- [x] 21.9 验证页面关闭再打开后的状态恢复
- [x] 21.10 验证服务重启后的状态对账与恢复
- [x] 21.11 验证现有 `Accounts` 页面与自动流水线页面状态显示一致
