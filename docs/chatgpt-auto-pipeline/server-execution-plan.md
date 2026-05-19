# ChatGPT Auto Pipeline Server Execution Plan

## 目标

把自动流水线的服务器联调拆成一套可执行顺序，减少现场判断成本，并确保每一步都有明确观察点、通过条件和失败处理建议。

## 执行原则

- 每次只验证一个变量，避免同时改多个配置后难以定位问题。
- 先验证“能启动、能恢复、能落状态”，再扩大到真实支付和可选 Auth。
- 每个阶段完成后都记录截图、关键日志和账号样本，便于回溯。

## 阶段 0：联调准备

### 操作

1. 确认服务器已部署当前版本代码。
2. 确认数据库已完成 `init_db()`。
3. 确认前端已完成构建并可访问 `/pipeline`。
4. 准备联调用资源：
   - 可用邮箱能力
   - 可用代理
   - 至少 1 个可用 GoPay 手机号
   - 验证码服务可用
5. 打开以下页面：
   - `/pipeline`
   - `Accounts`
   - 服务日志窗口

### 观察点

- `/pipeline` 页面能打开
- `GET /api/pipeline/config` 返回完整配置
- `GET /api/pipeline/status` 返回完整结构
- 日志流能正常滚动

### 通过条件

- 页面、接口、日志都正常

### 不通过时先查

- 后端启动日志
- 数据库初始化日志
- 前端构建产物是否更新

## 阶段 1：配置联调

### 建议初始配置

- `payment_pool_threshold = 1`
- `payment_pool_target = 2`
- `payment_batch_interval_seconds = 60`
- `payment_batch_max_size = 1`
- `register_poll_interval_seconds = 3`
- `gopay_batch_poll_interval_seconds = 3`
- `auth_poll_interval_seconds = 3`
- `gopay_timeout_seconds = 900`
- `auto_start = false`
- `enable_auth_capture = false`

### 操作

1. 在 `/pipeline` 页面填写上面的初始配置。
2. 点击保存。
3. 刷新页面。
4. 再打开一次 `/pipeline` 页面确认配置持久化。

### 观察点

- 页面显示保存成功
- 控制区显示：
  - 自动恢复：已关闭
  - Auth 补抓：已关闭
- 刷新后配置不丢失

### 通过条件

- 保存、刷新、重进后配置一致

## 阶段 2：注册补货联调

### 操作

1. 保持 `enable_auth_capture = false`。
2. 点击“启动”流水线。
3. 观察日志和待支付池。

### 观察点

- 流水线状态切到 `running`
- 触发注册补货任务
- 同时只出现 1 个活跃注册任务
- 注册成功后：
  - 新账号进入待支付池
  - 日志出现“注册结果回收完成”
- 注册失败时：
  - 失败账号进入失败池
  - 带失败原因

### 通过条件

- 能自动补货到目标池深度

### 样本记录

- 保存 1 个注册成功账号邮箱
- 保存 1 个失败样本和失败原因

## 阶段 3：支付联调

### 前置条件

- 手机号池中有 `enabled=true` 且 `status=ready` 的手机号
- 待支付池至少有 1 个账号

### 操作

1. 保持 `payment_batch_max_size = 1`，先单账号验证。
2. 等待或手动观察到支付触发时刻。
3. 监控：
   - 流水线页面
   - `Accounts` 页面
   - 服务日志

### 观察点

- 账号先进入支付预留
- 进入当批后才生成订阅链接
- 成功启动 GoPay batch
- 成功支付后：
  - 流水线页面进入已支付池
  - `pipeline_account_items.account_primary_status = subscribed`
  - `accounts.status = subscribed`
  - 套餐类型显示真实值，或 `unknown / 待刷新`
- 支付失败后：
  - 账号进入失败池
  - 有 `payment_failed_stage`
  - 有 `payment_error_code`
  - `accounts.status = payment_failed`

### 通过条件

- 成功和失败两种路径都能正确收敛

### 重点核对

- 支付启动失败时，账号不能卡在 `payment_reserved` 或 `link_ready`
- 手机号数量口径要按唯一 `ready` 号码计算

## 阶段 4：账号列表一致性联调

### 操作

1. 在支付成功后刷新 `Accounts` 页面。
2. 对照 `/pipeline` 页面同一账号状态。
3. 对已支付但套餐未刷新完成的账号再观察一次。

### 观察点

- `Accounts` 页面不会把已支付账号显示回“已注册”
- 主状态与 `/pipeline` 页面一致
- 套餐刷新未完成时能显示中间态，而不是旧套餐

### 通过条件

- 两个页面对同一账号的主状态语义一致

## 阶段 5：Auth 关闭路径联调

### 操作

1. 保持 `enable_auth_capture = false`。
2. 再跑一轮支付成功样本。

### 观察点

- 支付成功账号直接完成
- 不会创建 Auth 子任务
- 不会进入待补抓队列

### 通过条件

- 关闭 Auth 时只跑注册 + 支付

## 阶段 6：Auth 开启路径联调

### 操作

1. 把 `enable_auth_capture = true` 保存。
2. 再跑 1 个支付成功账号。
3. 观察待补抓队列和日志。

### 观察点

- 支付成功账号进入待补抓队列
- 只会顺序处理 1 个账号
- Auth 成功：
  - `auth_stage = success`
  - 支付主状态不变
- Auth 失败：
  - `auth_stage = failed`
  - 有 `auth_error_code / auth_error_reason`
  - 支付主状态仍保留

### 通过条件

- Auth 开关切换后的行为完全符合配置

## 阶段 7：页面恢复联调

### 操作

1. 在流水线运行中关闭浏览器页面。
2. 重新打开 `/pipeline`。

### 观察点

- 当前任务状态可恢复
- 待支付池、已支付池、失败池仍可见
- 活跃 GoPay batch 能继续展示

### 通过条件

- 页面关闭不会影响任务显示恢复

## 阶段 8：服务重启恢复联调

### 操作

1. 分别选择以下 3 种时机重启服务：
   - 注册补货中
   - GoPay batch 进行中
   - Auth 补抓中
2. 服务启动后立即打开 `/pipeline` 页面和日志。

### 观察点

- 若任务之前是 `running`，恢复后后台线程继续工作
- 活跃 GoPay batch 被重新接管
- 丢失的注册/Auth 中间态被标记失败
- 页面刷新后状态与真实后台状态一致
- 不会出现“显示 running，但后台没线程”的情况

### 通过条件

- 恢复逻辑真实可用，不是假恢复

## 阶段 9：异常场景联调

### 建议优先测的异常

1. 手机号池为空
2. 支付链接生成失败
3. GoPay batch 启动失败
4. GoPay 超时
5. Auth 补抓失败

### 每个异常都要确认

- 日志中有明确失败说明
- 账号进入失败池或 Auth 失败池
- 主状态是否符合预期
- 后续调度是否还能继续推进

## 阶段 10：扩大试跑前最终确认

### 确认项

- 注册补货稳定
- 支付触发稳定
- 已支付状态真实落库
- `Accounts` 与 `/pipeline` 一致
- Auth 开关行为正常
- 页面关闭恢复正常
- 服务重启恢复正常
- 批次启动失败不会卡死账号

### 达标后建议

- 把 `payment_batch_max_size` 从 `1` 提升到你计划的真实值
- 再进行一轮小规模试跑
- 最后再进入更大规模运行

## 联调记录模板

每轮联调建议记录以下信息：

- 时间
- 当前配置快照
- 样本账号邮箱
- 样本手机号
- 触发阶段
- 实际结果
- 关键日志
- 是否通过
- 是否需要回滚或修复
