# 更新日志 (Changelog)

本项目的所有显著更改都将记录在此文件中。

格式基于 [Keep a Changelog](https://keepachangelog.com/zh-CN/1.0.0/)，并且本项目遵循 [Semantic Versioning](https://semver.org/lang/zh-CN/) (语义化版本)。

## [Unreleased] (未发布)
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
