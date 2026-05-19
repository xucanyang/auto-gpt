# ChatGPT Auto Pipeline Server Checklist

## 目标

这份清单用于服务器联调前后的快速核对，确保自动流水线在真实环境中具备可运行性、可恢复性和可观测性。

## 一、部署前静态检查

- 确认服务器代码版本包含以下模块：
  - `services/pipeline/`
  - `api/pipeline.py`
  - `frontend/src/pages/Pipeline.tsx`
  - `main.py` 中的 `pipeline_engine.restore_or_start()`
- 确认数据库会在启动时执行 `init_db()`，并创建：
  - `pipeline_tasks`
  - `pipeline_account_items`
- 确认前端已构建并包含“自动流水线”页面入口。
- 确认服务器环境变量或配置项中，GoPay、邮箱、代理、验证码服务相关参数已就绪。

## 二、启动后基础健康检查

- 打开 `/pipeline` 页面，确认页面可正常加载。
- 调用 `GET /api/pipeline/config`，确认返回包含：
  - `auto_start`
  - `enable_auth_capture`
  - `payment_pool_threshold`
  - `payment_pool_target`
  - `payment_batch_interval_seconds`
- 调用 `GET /api/pipeline/status`，确认返回字段完整：
  - `task`
  - `config`
  - `queues`
  - `summary`
  - `active_payment_batch`
- 确认 `GET /api/pipeline/logs/stream` 可以持续输出日志。

## 三、配置联调检查

- 在页面中保存一次配置，确认保存成功且刷新后值不丢失。
- 分别切换并保存：
  - `auto_start`
  - `enable_auth_capture`
- 确认控制区能正确显示：
  - 自动恢复：已开启/已关闭
  - Auth 补抓：已开启/已关闭

## 四、注册补货联调

- 将 `payment_pool_threshold` 设为大于当前待支付池数量。
- 启动流水线，确认会自动触发注册补货。
- 确认同一时刻只出现一个活跃注册任务。
- 注册成功后，确认：
  - 账号进入待支付池
  - `pipeline_account_items.source = pipeline_register`
  - `pipeline_status = pending_payment`
- 若注册失败，确认失败账号进入失败池并带失败原因。

## 五、支付联调

- 确认 GoPay 手机号池中存在 `enabled=true` 且 `status=ready` 的手机号。
- 等待到支付触发时刻，确认系统会：
  - 从待支付池预留账号
  - 仅在支付前生成订阅链接
  - 启动单个 GoPay batch
- 确认支付成功后：
  - 流水线页显示账号进入已支付池
  - `pipeline_account_items.account_primary_status = subscribed`
  - `accounts.status = subscribed`
  - 套餐类型成功刷新，或显示 `unknown / 待刷新`
- 确认支付失败后：
  - 账号进入失败池
  - `payment_error_code / payment_error_reason / payment_failed_stage` 有值
  - `accounts.status = payment_failed`

## 六、Auth 补抓联调

- 当 `enable_auth_capture = false` 时：
  - 支付成功账号应直接完成
  - 不应出现新的 Auth 子任务
- 当 `enable_auth_capture = true` 时：
  - 支付成功账号进入待补抓队列
  - 只会顺序处理一个账号
  - Auth 成功不应改坏支付主状态
  - Auth 失败应记录 `auth_error_code / auth_error_reason`

## 七、恢复联调

### 页面恢复

- 在流水线运行中关闭页面，再重新打开 `/pipeline`。
- 确认能恢复看到：
  - 当前任务状态
  - 待支付池
  - 已支付池
  - 失败池
  - 活跃 GoPay batch

### 服务重启恢复

- 在以下任一状态下重启服务：
  - 注册补货中
  - GoPay batch 运行中
  - Auth 补抓中
- 重启后确认：
  - 若任务原本为 `running`，后台调度线程会继续运行
  - 活跃 GoPay batch 会被重新接管
  - 丢失的注册/Auth 中间态会被正确标记失败
  - 页面刷新后状态与后台真实状态一致

## 八、账号列表一致性检查

- 打开 `Accounts` 页面，确认刚支付成功的账号不会回退成“已注册”。
- 确认 `Accounts` 页面状态与 `/pipeline` 页面状态语义一致。
- 确认套餐未刷新完成时，`Accounts` 页面可显示真实中间态，而不是旧套餐值。

## 九、异常场景核对

- 批量支付启动失败时，确认账号不会卡在 `payment_reserved / link_ready`。
- GoPay 超时后，确认批次会尝试取消，并把相关账号打到失败池。
- 当没有可用手机号时，确认流水线不会误启动支付批次。
- 当 `auto_start=true` 且服务重启后，确认不会出现“状态是 running，但后台实际上没线程”的假恢复。

## 十、联调完成判定

满足以下条件后，可进入更大规模服务器试跑：

- 配置保存与读取正常
- 注册补货可稳定工作
- 支付批处理可稳定触发
- 已支付主状态真实落库
- Auth 开关行为正确
- 页面关闭恢复正常
- 服务重启恢复正常
- `Accounts` 页与自动流水线页状态一致
