import { useEffect, useState } from 'react'
import { App, Card, Form, Input, Select, Button, message, Tabs, Space, Tag, Typography, Modal, QRCode, Switch, Alert, Table } from 'antd'
import {
  SaveOutlined,
  EyeOutlined,
  EyeInvisibleOutlined,
  MailOutlined,
  SafetyOutlined,
  ApiOutlined,
  CheckCircleOutlined,
  CloseCircleOutlined,
  SyncOutlined,
  PlusOutlined,
  LockOutlined,
  CopyOutlined,
} from '@ant-design/icons'
import { parseBooleanConfigValue } from '@/lib/configValueParsers'
import { apiFetch } from '@/lib/utils'

const SELECT_FIELDS: Record<string, { label: string; value: string }[]> = {
  mail_provider: [
    { label: 'LuckMail（订单接码 / 已购邮箱）', value: 'luckmail' },
    { label: '手动邮箱 + 手输验证码（仅 ChatGPT）', value: 'manual_email_otp' },
    { label: 'Outlook（本地导入）', value: 'outlook' },
    { label: 'AppleMail（小苹果 / 本地邮箱池）', value: 'applemail' },
    { label: 'Laoudo（固定邮箱）', value: 'laoudo' },
    { label: 'TempMail.lol（自动生成）', value: 'tempmail_lol' },
    { label: 'TempMail Ready API（本地接口）', value: 'tempmail_local' },
    { label: 'iCloud HME（共享转发到 TempMail）', value: 'icloud_hme' },
    { label: 'SkyMail（CloudMail 接口）', value: 'skymail' },
    { label: 'CloudMail（genToken 口令模式）', value: 'cloudmail' },
    { label: 'DuckMail（自动生成）', value: 'duckmail' },
    { label: 'MoeMail (sall.cc)', value: 'moemail' },
    { label: 'YYDS Mail / MaliAPI', value: 'maliapi' },
    { label: 'GPTMail', value: 'gptmail' },
    { label: 'OpenTrashMail', value: 'opentrashmail' },
    { label: 'Freemail（自建 CF Worker）', value: 'freemail' },
    { label: 'CF Worker（自建域名）', value: 'cfworker' },
  ],
  maliapi_auto_domain_strategy: [
    { label: 'balanced', value: 'balanced' },
    { label: 'prefer_owned', value: 'prefer_owned' },
    { label: 'prefer_public', value: 'prefer_public' },
  ],
  tempmail_mode: [
    { label: '固定域名', value: 'fixed_domain' },
    { label: '随机子域 / Ready', value: 'task_subdomain' },
  ],
  icloud_domain_base: [
    { label: 'icloud.com', value: 'icloud.com' },
    { label: 'icloud.com.cn', value: 'icloud.com.cn' },
  ],
  icloud_hme_mode: [
    { label: '实时创建', value: 'live' },
    { label: '仅导入池', value: 'import_pool' },
    { label: '优先导入池', value: 'prefer_import' },
  ],
  default_executor: [
    { label: 'API 协议（无浏览器）', value: 'protocol' },
    { label: '无头浏览器', value: 'headless' },
    { label: '有头浏览器', value: 'headed' },
  ],
  default_captcha_solver: [
    { label: 'YesCaptcha', value: 'yescaptcha' },
    { label: '本地 Solver (Camoufox)', value: 'local_solver' },
    { label: '手动', value: 'manual' },
  ],
  cpa_cleanup_enabled: [
    { label: '关闭', value: '0' },
    { label: '开启', value: '1' },
  ],
  codex_proxy_upload_type: [
    { label: 'AT（Access Token，推荐）', value: 'at' },
    { label: 'RT（Refresh Token）', value: 'rt' },
  ],
  chatgpt_gopay_billing_llm_country_strategy: [
    { label: '跟随账单国家', value: 'billing_country' },
    { label: '跟随结账国家', value: 'checkout_country' },
    { label: '固定国家', value: 'fixed_country' },
  ],
  chatgpt_phone_verification_provider: [
    { label: 'SMSToMe 号码池', value: 'smstome' },
    { label: '本地接码网关', value: 'local_gateway' },
  ],
}

const TAB_ITEMS = [
  {
    key: 'register',
    label: '注册设置',
    icon: <ApiOutlined />,
    sections: [
      {
        title: '默认注册方式',
        desc: '控制注册任务如何执行',
        fields: [
          { key: 'default_executor', label: '执行器类型', type: 'select' },
        ],
      },
    ],
  },
  {
    key: 'mailbox',
    label: '邮箱服务',
    icon: <MailOutlined />,
    sections: [
      {
        title: '默认邮箱服务',
        desc: '选择注册时使用的邮箱类型',
        fields: [
          { key: 'mail_provider', label: '邮箱服务', type: 'select' },
          { key: 'mailbox_otp_timeout_seconds', label: '邮箱验证码等待秒数', placeholder: '例如 60 / 90 / 120' },
        ],
      },
      {
        title: 'Laoudo',
        desc: '固定邮箱，手动配置',
        fields: [
          { key: 'laoudo_email', label: '邮箱地址', placeholder: 'xxx@laoudo.com' },
          { key: 'laoudo_account_id', label: 'Account ID', placeholder: '563' },
          { key: 'laoudo_auth', label: 'JWT Token', placeholder: 'eyJ...', secret: true },
        ],
      },
      {
        title: 'Freemail',
        desc: '基于 Cloudflare Worker 的自建邮箱，支持管理员令牌或账号密码认证',
        fields: [
          { key: 'freemail_api_url', label: 'API URL', placeholder: 'https://mail.example.com' },
          { key: 'freemail_admin_token', label: '管理员令牌', secret: true },
          { key: 'freemail_username', label: '用户名（可选）' },
          { key: 'freemail_password', label: '密码（可选）', secret: true },
          { key: 'freemail_domain', label: '邮箱域名（可选）', placeholder: 'example.com' },
        ],
      },
      {
        title: 'MoeMail',
        desc: '自动注册账号并生成临时邮箱',
        fields: [
          { key: 'moemail_api_url', label: 'API URL', placeholder: 'https://sall.cc' },
          { key: 'moemail_api_key', label: 'API Key', secret: true },
        ],
      },
      {
        title: 'SkyMail',
        desc: 'CloudMail 兼容接口（addUser / emailList）',
        fields: [
          { key: 'skymail_api_base', label: 'API Base', placeholder: 'https://api.skymail.ink' },
          { key: 'skymail_token', label: 'Authorization Token', secret: true },
          { key: 'skymail_domain', label: '邮箱域名', placeholder: 'mail.example.com' },
        ],
      },
      {
        title: 'CloudMail',
        desc: 'CloudMail 口令模式（genToken + emailList）',
        fields: [
          { key: 'cloudmail_api_base', label: 'API Base', placeholder: 'https://cloudmail.example.com' },
          { key: 'cloudmail_admin_email', label: '管理员邮箱（可选）', placeholder: 'admin@example.com' },
          { key: 'cloudmail_admin_password', label: '管理员密码', secret: true },
          { key: 'cloudmail_domain', label: '邮箱域名（可选）', placeholder: 'mail.example.com,mail2.example.com' },
          { key: 'cloudmail_subdomain', label: '子域名（可选）', placeholder: 'pool-a' },
          { key: 'cloudmail_timeout', label: '请求超时秒数', placeholder: '30' },
        ],
      },
      {
        title: 'YYDS Mail / MaliAPI',
        desc: '基于 API Key 创建临时邮箱并轮询收件箱消息',
        fields: [
          { key: 'maliapi_base_url', label: 'API URL', placeholder: 'https://maliapi.215.im/v1' },
          { key: 'maliapi_api_key', label: 'API Key', secret: true },
          { key: 'maliapi_domain', label: '邮箱域名（可选）', placeholder: 'example.com' },
          { key: 'maliapi_auto_domain_strategy', label: '自动域名策略', type: 'select' },
        ],
      },
      {
        title: 'AppleMail / 小苹果',
        desc: '读取本地邮箱池文件，通过 refresh_token + client_id 调用小苹果取件接口；支持在本页直接导入 JSON',
        fields: [
          { key: 'applemail_base_url', label: 'API URL', placeholder: 'https://www.appleemail.top' },
          { key: 'applemail_pool_dir', label: '邮箱池目录', placeholder: 'mail' },
          { key: 'applemail_pool_file', label: '当前邮箱池文件（可选）', placeholder: '留空则自动读取目录中最新文件' },
          { key: 'applemail_mailboxes', label: '轮询文件夹', placeholder: 'INBOX,Junk' },
        ],
      },
      {
        title: 'GPTMail',
        desc: '基于 GPTMail API 生成临时邮箱并轮询邮件；若已知本站可用域名，也可本地拼装随机地址',
        fields: [
          { key: 'gptmail_base_url', label: 'API URL', placeholder: 'https://mail.chatgpt.org.uk' },
          { key: 'gptmail_api_key', label: 'API Key', secret: true, placeholder: 'gpt-test' },
          { key: 'gptmail_domain', label: '邮箱域名（可选）', placeholder: 'example.com' },
        ],
      },
      {
        title: 'OpenTrashMail',
        desc: '对接 opentrashmail 服务；可直接轮询 /json/<email>，也支持已知域名时本地拼装随机地址',
        fields: [
          { key: 'opentrashmail_api_url', label: 'API URL', placeholder: 'http://mail.example.com:8085' },
          { key: 'opentrashmail_domain', label: '邮箱域名（可选）', placeholder: 'xiyoufm.com' },
          { key: 'opentrashmail_password', label: '站点密码（可选）', secret: true, placeholder: '启用 PASSWORD 时填写' },
        ],
      },
      {
        title: 'TempMail.lol',
        desc: '自动生成邮箱，无需配置，需要代理访问（CN IP 被封）',
        fields: [],
      },
      {
        title: 'TempMail 本地接口',
        desc: '支持固定域名建箱，也支持任务级随机子域 ready 建箱',
        fields: [
          { key: 'tempmail_api_url', label: 'API URL', placeholder: 'http://127.0.0.1:18081' },
          { key: 'tempmail_api_key', label: 'API Key', secret: true },
          { key: 'tempmail_api_key_header', label: '鉴权 Header', placeholder: 'Authorization' },
          { key: 'tempmail_mode', label: '建箱模式', type: 'select' },
          { key: 'tempmail_primary_domain', label: '主域名（固定域名模式时必填）', placeholder: 'mail.666800.xyz' },
          { key: 'tempmail_wait_timeout_seconds', label: '建箱等待秒数', placeholder: '180' },
          { key: 'tempmail_ttl_minutes', label: '邮箱 TTL 分钟', placeholder: '30' },
          { key: 'tempmail_reuse_window_minutes', label: '子域复用窗口分钟', placeholder: '20' },
          { key: 'tempmail_permanent', label: '永久邮箱', type: 'boolean' },
          { key: 'tempmail_platform', label: '平台标识', placeholder: 'chatgpt' },
        ],
      },
      {
        title: 'iCloud HME',
        desc: '调用 iCloud Hide My Email 私有接口生成别名，并统一转发到共享 TempMail 收件箱',
        fields: [
          { key: 'icloud_hme_mode', label: '别名来源模式', type: 'select' },
          { key: 'icloud_cookie', label: 'iCloud Cookie', type: 'textarea', placeholder: '从 www.icloud.com DevTools 请求头复制完整 Cookie 字符串' },
          { key: 'icloud_domain_base', label: 'iCloud 域', type: 'select' },
          { key: 'icloud_forward_to', label: '转发目标邮箱', placeholder: 'b@cccy.me' },
          { key: 'icloud_forward_mailbox_id', label: '转发目标 mailbox_id（可选）', placeholder: '0d355f68-8506-4c93-ac56-5ef017f0b932' },
          { key: 'icloud_hme_auto_create_enabled', label: '自动创建导入池邮箱', type: 'boolean' },
          { key: 'icloud_hme_auto_create_stock_limit', label: '导入池库存上限', placeholder: '10' },
          { key: 'icloud_hme_auto_create_interval_min_minutes', label: '随机间隔最小分钟', placeholder: '60' },
          { key: 'icloud_hme_auto_create_interval_max_minutes', label: '随机间隔最大分钟', placeholder: '120' },
          { key: 'icloud_hme_auto_create_rate_limit_backoff_minutes', label: '遇到限流延长等待分钟', placeholder: '360' },
        ],
      },
      {
        title: 'DuckMail',
        desc: '自动生成邮箱，随机创建账号',
        fields: [
          { key: 'duckmail_api_url', label: 'Web URL', placeholder: 'https://www.duckmail.sbs' },
          { key: 'duckmail_provider_url', label: 'Provider URL', placeholder: 'https://api.duckmail.sbs' },
          { key: 'duckmail_bearer', label: 'Bearer Token', placeholder: 'kevin273945', secret: true },
          { key: 'duckmail_domain', label: '自定义域名', placeholder: '留空则从 Provider URL 推导' },
          { key: 'duckmail_api_key', label: 'API Key（私有域名）', placeholder: 'dk_xxx（domain.duckmail.sbs 获取）', secret: true },
        ],
      },
      {
        title: 'CF Worker 自建邮箱',
        desc: '基于 Cloudflare Worker 的自建临时邮箱服务',
        fields: [
          { key: 'cfworker_api_url', label: 'API URL', placeholder: 'https://apimail.example.com' },
          { key: 'cfworker_admin_token', label: '管理员 Token', secret: true },
          { key: 'cfworker_custom_auth', label: '站点密码', secret: true },
          { key: 'cfworker_subdomain', label: '固定子域名', placeholder: 'mail / pool-a' },
          { key: 'cfworker_random_subdomain', label: '随机子域名', type: 'boolean' },
          { key: 'cfworker_fingerprint', label: 'Fingerprint', placeholder: '6703363b...' },
        ],
      },
      {
        title: 'LuckMail',
        desc: 'ChatGPT 默认走购买邮箱 / 已购邮箱模式',
        fields: [
          { key: 'luckmail_base_url', label: '平台地址', placeholder: 'https://mails.luckyous.com' },
          { key: 'luckmail_api_key', label: 'API Key', secret: true },
          { key: 'luckmail_email_type', label: '邮箱类型（可选）', placeholder: 'ms_graph / ms_imap / self_built' },
          { key: 'luckmail_domain', label: '邮箱域名（可选）', placeholder: 'outlook.com / gmail.com' },
        ],
      },
    ],
  },
  {
    key: 'captcha',
    label: '验证码',
    icon: <SafetyOutlined />,
    sections: [
      {
        title: '验证码服务',
        desc: '用于绕过注册页面的人机验证',
        fields: [
          { key: 'default_captcha_solver', label: '默认服务', type: 'select' },
          { key: 'yescaptcha_key', label: 'YesCaptcha Key', secret: true },
        ],
      },
    ],
  },
  {
    key: 'chatgpt',
    label: 'ChatGPT',
    icon: <ApiOutlined />,
    sections: [
      {
        title: 'CPA 面板',
        desc: '注册完成后自动上传到 CPA 管理平台',
        fields: [
          { key: 'cpa_api_url', label: 'API URL', placeholder: 'https://your-cpa.example.com' },
          { key: 'cpa_api_key', label: 'API Key', secret: true },
        ],
      },
      {
        title: 'Sub2API 面板',
        desc: '注册完成后自动上传到 Sub2API 管理后台',
        fields: [
          { key: 'sub2api_api_url', label: 'API URL', placeholder: 'https://your-sub2api.example.com' },
          { key: 'sub2api_api_key', label: 'API Key', secret: true },
          { key: 'sub2api_group_ids', label: '分组 ID', placeholder: '多个分组用英文逗号分隔，例如 2,4,8' },
        ],
      },
      {
        title: 'CPA 自动维护',
        desc: '定时删除 status=error 的凭证，剩余数量低于阈值时自动按现有配置补注册 ChatGPT',
        fields: [
          { key: 'cpa_cleanup_enabled', label: '自动维护', type: 'select' },
          { key: 'cpa_cleanup_interval_minutes', label: '检查间隔（分钟）', placeholder: '60' },
          { key: 'cpa_cleanup_threshold', label: '最低凭证阈值', placeholder: '5' },
          { key: 'cpa_cleanup_concurrency', label: '补注册并发数', placeholder: '1' },
          { key: 'cpa_cleanup_register_delay_seconds', label: '每个注册延迟（秒）', placeholder: '0' },
        ],
      },
      {
        title: 'Business / 工作空间',
        desc: '控制 ChatGPT 注册后是否走 team invite，以及默认抓取哪些工作空间',
        fields: [
          { key: 'chatgpt_enable_team_invite', label: '启用 team invite / business 恢复', type: 'boolean' },
          { key: 'chatgpt_team_invite_deferred_activation', label: '默认延迟邀请', type: 'boolean' },
          { key: 'chatgpt_capture_free_workspace', label: '默认抓取 free 工作空间', type: 'boolean' },
          { key: 'chatgpt_capture_business_workspace', label: '默认抓取 business 工作空间', type: 'boolean' },
          { key: 'chatgpt_existing_account_login_password', label: '已有账号抓 auth 默认密码', secret: true, placeholder: '可留空，任务里仍可临时覆盖' },
        ],
      },
      {
        title: '补抓 Auth',
        desc: '账号页与批量补抓 Auth 的默认参数',
        fields: [
          { key: 'chatgpt_resume_auth_allow_phone_verification', label: '默认允许手机号验证', type: 'boolean' },
          { key: 'chatgpt_subscription_auth_capture_retry_delays_seconds', label: '重试间隔（秒）', placeholder: '5,10' },
        ],
      },
      {
        title: 'GoPay 账单地址 LLM',
        desc: 'GoPay 启动时生成本次账单地址，失败时回落到表单/默认地址',
        fields: [
          { key: 'chatgpt_gopay_billing_llm_enabled', label: '启用 LLM 地址', type: 'boolean' },
          { key: 'chatgpt_gopay_billing_llm_base_url', label: 'Base URL', placeholder: 'https://api.666800.xyz' },
          { key: 'chatgpt_gopay_billing_llm_api_key', label: 'API Key', secret: true },
          { key: 'chatgpt_gopay_billing_llm_model', label: '模型', placeholder: 'gpt-5.4' },
          { key: 'chatgpt_gopay_billing_llm_wire_api', label: '接口格式', placeholder: 'responses' },
          { key: 'chatgpt_gopay_billing_llm_country_strategy', label: '地址国家策略', type: 'select' },
          { key: 'chatgpt_gopay_billing_llm_fixed_country', label: '固定国家代码', placeholder: 'US' },
          { key: 'chatgpt_gopay_billing_llm_reasoning_effort', label: '推理强度', placeholder: 'xhigh' },
          { key: 'chatgpt_gopay_billing_llm_timeout_seconds', label: '超时秒数', placeholder: '45' },
          { key: 'chatgpt_gopay_billing_llm_prompt', label: '提示词', type: 'textarea', placeholder: '生成一个真实可用的账单地址，地址在谷歌地图中能找到对应的位置。' },
        ],
      },
      {
        title: '无 RT / Access Token Only',
        desc: '控制是否执行订阅链接账单 amount 校验，以及 amount=0 命中后的自动停策略',
        fields: [
          { key: 'chatgpt_access_token_only_checkout_amount_check_enabled', label: '启用额度验证', type: 'boolean' },
          { key: 'chatgpt_access_token_only_checkout_country', label: '额度验证国家', placeholder: 'US' },
          { key: 'chatgpt_access_token_only_checkout_currency', label: '额度验证货币', placeholder: 'USD' },
          { key: 'chatgpt_access_token_only_zero_amount_stop_enabled', label: '启用 amount=0 自动停', type: 'boolean' },
          { key: 'chatgpt_access_token_only_zero_amount_stop_threshold', label: 'amount=0 命中阈值', placeholder: '1' },
        ],
      },
      {
        title: '外部订阅链接 API',
        desc: '允许外部支付程序领取已缓存的订阅链接，并在支付后写回账号状态',
        help: {
          title: '接口使用方法',
          lines: [
            '请求头统一使用 Authorization: Bearer <访问 Token>。',
            '领取订阅链接: POST /api/external/subscription-links/claim，body 示例 {"consumer":"payment-worker-01","limit":10,"lease_seconds":900}。',
            '查询领取状态: GET /api/external/subscription-links/{claim_id}。',
            '支付成功写回: POST /api/external/subscription-links/{claim_id}/result，body 示例 {"status":"paid","external_payment_id":"pay_123","message":"payment completed"}。',
            '支付失败写回: POST /api/external/subscription-links/{claim_id}/result，body 示例 {"status":"failed","external_payment_id":"pay_123","error_code":"declined","message":"payment failed"}。',
            '放弃本次领取: POST /api/external/subscription-links/{claim_id}/release，body 示例 {"reason":"checkout unavailable"}。',
            '接口只返回 account_id、email、payment_link、plan、country、currency、claim_id 等支付所需字段，不返回密码或 token。',
          ],
        },
        fields: [
          { key: 'external_subscription_api_enabled', label: '启用外部 API', type: 'boolean' },
          { key: 'external_subscription_api_token', label: '访问 Token', secret: true, placeholder: '外部程序使用 Authorization: Bearer <token>' },
        ],
      },
      {
        title: 'CodexProxy',
        desc: '注册完成后自动上传到 CodexProxy 管理平台',
        fields: [
          { key: 'codex_proxy_url', label: 'API URL', placeholder: 'https://your-codex-proxy.example.com' },
          { key: 'codex_proxy_key', label: 'Admin Key', secret: true },
          { key: 'codex_proxy_upload_type', label: '上传类型' },
        ],
      },
      {
        title: '手机验证 / 接码服务',
        desc: 'ChatGPT add_phone 阶段自动取号并轮询短信验证码',
        fields: [
          { key: 'chatgpt_phone_verification_provider', label: '接码服务', type: 'select' },
          { key: 'local_phone_gateway_url', label: '本地网关 URL', placeholder: 'http://sms-gateway:8720' },
          { key: 'local_phone_gateway_token', label: '本地网关 Token', secret: true },
          { key: 'local_phone_gateway_service_alias', label: '本地网关服务别名', placeholder: 'chatgpt' },
          { key: 'local_phone_gateway_timeout_seconds', label: '本地网关等待秒数', placeholder: '180' },
          { key: 'local_phone_gateway_poll_interval_seconds', label: '本地网关轮询间隔秒数', placeholder: '5' },
          { key: 'local_phone_gateway_max_attempts', label: '本地网关换号次数', placeholder: '3' },
          { key: 'smstome_cookie', label: 'SMSToMe Cookie', secret: true },
          { key: 'smstome_country_slugs', label: 'SMSToMe 国家列表', placeholder: 'united-kingdom,poland' },
          { key: 'smstome_phone_attempts', label: 'SMSToMe 手机号尝试次数', placeholder: '3' },
          { key: 'smstome_otp_timeout_seconds', label: 'SMSToMe 短信等待秒数', placeholder: '45' },
          { key: 'smstome_poll_interval_seconds', label: 'SMSToMe 轮询间隔秒数', placeholder: '5' },
          { key: 'smstome_sync_max_pages_per_country', label: 'SMSToMe 每国同步页数', placeholder: '5' },
        ],
      },
    ],
  },
  {
    key: 'cliproxyapi',
    label: 'CLIProxyAPI',
    icon: <ApiOutlined />,
    sections: [
      {
        title: '管理面板',
        desc: '用于 CLIProxyAPI 管理页登录',
        fields: [
          { key: 'cliproxyapi_base_url', label: 'API URL', placeholder: 'http://127.0.0.1:8317' },
          { key: 'cliproxyapi_management_key', label: '管理口令', secret: true, placeholder: '默认 cliproxyapi' },
        ],
      },
    ],
  },
  {
    key: 'contribution',
    label: '贡献',
    icon: <PlusOutlined />,
    sections: [],
  },
  {
    key: 'integrations',
    label: '插件',
    icon: <ApiOutlined />,
    sections: [],
  },
  {
    key: 'security',
    label: '安全',
    icon: <LockOutlined />,
    sections: [],
  },
]

interface FieldConfig {
  key: string
  label: string
  placeholder?: string
  type?: 'select' | 'input' | 'boolean' | 'textarea'
  secret?: boolean
}

interface SectionConfig {
  title: string
  desc?: string
  help?: {
    title: string
    lines: string[]
  }
  fields: FieldConfig[]
}

const CHATGPT_PINNED_SECTIONS_STORAGE_KEY = 'any-auto-register.settings.chatgpt.pinned-sections'

function loadChatgptPinnedSections(): string[] {
  if (typeof window === 'undefined') return []
  try {
    const raw = window.localStorage.getItem(CHATGPT_PINNED_SECTIONS_STORAGE_KEY)
    const parsed = raw ? JSON.parse(raw) : []
    if (!Array.isArray(parsed)) return []
    return parsed.map((item) => String(item || '').trim()).filter(Boolean)
  } catch {
    return []
  }
}

function normalizePinnedSections(pinnedSections: string[], sections: SectionConfig[]): string[] {
  const available = new Set(sections.map((section) => section.title))
  const seen = new Set<string>()
  return pinnedSections.filter((title) => {
    if (!available.has(title) || seen.has(title)) return false
    seen.add(title)
    return true
  })
}

function orderPinnedSections(sections: SectionConfig[], pinnedSections: string[]): SectionConfig[] {
  const normalizedPinned = normalizePinnedSections(pinnedSections, sections)
  if (!normalizedPinned.length) return sections
  const byTitle = new Map(sections.map((section) => [section.title, section]))
  return [
    ...(normalizedPinned.map((title) => byTitle.get(title)).filter(Boolean) as SectionConfig[]),
    ...sections.filter((section) => !normalizedPinned.includes(section.title)),
  ]
}

function getMailboxSectionProvider(title: string): string | null {
  switch (title) {
    case 'Laoudo':
      return 'laoudo'
    case 'Freemail':
      return 'freemail'
    case 'MoeMail':
      return 'moemail'
    case 'SkyMail':
      return 'skymail'
    case 'CloudMail':
      return 'cloudmail'
    case 'YYDS Mail / MaliAPI':
      return 'maliapi'
    case 'AppleMail / 小苹果':
      return 'applemail'
    case 'GPTMail':
      return 'gptmail'
    case 'OpenTrashMail':
      return 'opentrashmail'
    case 'TempMail.lol':
      return 'tempmail_lol'
    case 'TempMail 本地接口':
      return 'tempmail_local'
    case 'iCloud HME':
      return 'icloud_hme'
    case 'DuckMail':
      return 'duckmail'
    case 'CF Worker 自建邮箱':
      return 'cfworker'
    case 'LuckMail':
      return 'luckmail'
    default:
      return null
  }
}

interface TabConfig {
  key: string
  label: string
  icon: React.ReactNode
  sections: SectionConfig[]
}

interface AppleMailPoolPreviewItem {
  index: number
  email: string
  mailbox: string
}

interface AppleMailPoolSnapshot {
  filename: string
  pool_dir: string
  count: number
  items: AppleMailPoolPreviewItem[]
  truncated: boolean
}

interface ICloudHmeAutoPoolStatus {
  running?: boolean
  enabled?: boolean
  stock_limit?: number
  ready_count?: number
  interval_min_minutes?: number
  interval_max_minutes?: number
  rate_limit_backoff_minutes?: number
  next_run_at?: string
  seconds_until_next_run?: number
  rate_limit_until?: string
  in_rate_limit_backoff?: boolean
  last_run_at?: string
  last_success_at?: string
  last_created_hme?: string
  last_error?: string
  forward_to?: string
}

function formatResultText(data: unknown) {
  if (typeof data === 'string') return data
  try {
    return JSON.stringify(data, null, 2)
  } catch {
    return String(data)
  }
}

function normalizeDomainList(input: unknown): string[] {
  const items = Array.isArray(input) ? input : []
  const seen = new Set<string>()
  const domains: string[] = []
  for (const item of items) {
    const domain = String(item || '').trim().toLowerCase().replace(/^@/, '')
    if (!domain || seen.has(domain)) continue
    seen.add(domain)
    domains.push(domain)
  }
  return domains
}

function parseStoredDomainList(value: unknown): string[] {
  if (Array.isArray(value)) return normalizeDomainList(value)
  if (typeof value !== 'string') return []

  const text = value.trim()
  if (!text) return []

  try {
    const parsed = JSON.parse(text)
    if (Array.isArray(parsed)) {
      return normalizeDomainList(parsed)
    }
  } catch {}

  return normalizeDomainList(
    text
      .split('\n')
      .flatMap((line) => line.split(','))
      .map((item) => item.trim()),
  )
}

const CONTRIBUTION_REDEEM_OPTIONS = [10, 100, 1000]

function asRecord(value: unknown): Record<string, unknown> | null {
  if (!value || typeof value !== 'object' || Array.isArray(value)) return null
  return value as Record<string, unknown>
}

function pickRecord(value: Record<string, unknown> | null, keys: string[]): Record<string, unknown> | null {
  if (!value) return null
  for (const key of keys) {
    const record = asRecord(value[key])
    if (record) return record
  }
  return null
}

function pickString(value: Record<string, unknown> | null, keys: string[]): string {
  if (!value) return ''
  for (const key of keys) {
    const text = String(value[key] ?? '').trim()
    if (text) return text
  }
  return ''
}

function pickNumber(value: Record<string, unknown> | null, keys: string[]): number | null {
  if (!value) return null
  for (const key of keys) {
    const raw = value[key]
    if (typeof raw === 'number' && Number.isFinite(raw)) return raw
    if (typeof raw === 'string') {
      const parsed = Number.parseFloat(raw)
      if (Number.isFinite(parsed)) return parsed
    }
  }
  return null
}

function formatDisplayNumber(value: number | null, digits = 0): string {
  if (value === null || !Number.isFinite(value)) return '-'
  return value.toLocaleString('zh-CN', {
    minimumFractionDigits: digits,
    maximumFractionDigits: digits,
  })
}

function formatDisplayPercent(value: number | null): string {
  if (value === null || !Number.isFinite(value)) return '-'
  return `${value.toFixed(2)}%`
}

function formatDateTimeText(value: string | undefined): string {
  const text = String(value || '').trim()
  if (!text) return '-'
  const date = new Date(text)
  if (Number.isNaN(date.getTime())) return text
  return date.toLocaleString('zh-CN', { hour12: false })
}

function formatDurationText(seconds: number | undefined): string {
  const totalSeconds = Math.max(0, Math.floor(Number(seconds || 0)))
  if (!totalSeconds) return '-'
  const minutes = Math.floor(totalSeconds / 60)
  const hours = Math.floor(minutes / 60)
  const restMinutes = minutes % 60
  if (hours > 0) return `${hours}小时${restMinutes}分钟`
  if (minutes > 0) return `${minutes}分钟`
  return `${totalSeconds}秒`
}

function ConfigField({ field }: { field: FieldConfig }) {
  const [showSecret, setShowSecret] = useState(false)
  const options = SELECT_FIELDS[field.key]
  const isBooleanField = field.type === 'boolean'
  const helpText =
    field.key === 'default_executor'
      ? '当前仅对 ChatGPT 生效；支持纯协议、无头浏览器和有头浏览器模式。'
      : field.key === 'icloud_cookie'
        ? '从浏览器打开 www.icloud.com，进入 DevTools，找到发往 setup.icloud.com 或 /hme/ 的请求，把完整 Cookie 请求头原样复制到这里。不要删任何字段。'
      : field.key === 'icloud_hme_mode'
        ? '实时创建会直接调用 Apple 私有接口；仅导入池会只从本地已导入的 HME 别名池领取；优先导入池会先领池里的别名，没货再实时创建。'
      : field.key === 'icloud_hme_auto_create_enabled'
        ? '开启后后台会按随机时间间隔自动创建 1 个新 HME，并放入导入池；达到库存上限时暂停创建。'
      : field.key === 'icloud_hme_auto_create_stock_limit'
        ? '只统计当前转发邮箱下可注册的导入池库存；库存达到该数量时不再创建新 HME。'
      : field.key === 'icloud_hme_auto_create_interval_min_minutes'
        ? '每次成功创建或跳过后，会在最小和最大分钟之间随机选择下次检查时间。'
      : field.key === 'icloud_hme_auto_create_interval_max_minutes'
        ? '必须大于或等于最小分钟；填写更大的范围可以降低固定节奏触发风控的概率。'
      : field.key === 'icloud_hme_auto_create_rate_limit_backoff_minutes'
        ? '如果 Apple 返回创建限流，后台会至少等待这么久后再尝试。'
      : field.key === 'chatgpt_resume_auth_allow_phone_verification'
        ? '关闭时遇到 add_phone 只记录为需要手机号；开启后补抓 Auth 会调用已配置的手机验证码 API。'
      : field.key === 'chatgpt_subscription_auth_capture_retry_delays_seconds'
        ? '用英文逗号分隔，例如 5,10；遇到 add_phone 或临时认证错误时按这些间隔重试。'
      : field.key === 'chatgpt_phone_verification_provider'
        ? '选择 add_phone 阶段使用的接码来源；本地接码网关会把 SMSBower 等平台隔离到独立项目里。'
      : field.key === 'local_phone_gateway_url'
        ? '主容器内访问独立接码网关的地址；Docker 网络内推荐 http://sms-gateway:8720。'
      : field.key === 'local_phone_gateway_token'
        ? '独立接码网关的 Bearer Token，只保存在主项目配置中，不会展示明文。'
      : undefined

  return (
    <Form.Item
      label={field.label}
      name={field.key}
      extra={helpText}
      valuePropName={isBooleanField ? 'checked' : undefined}
    >
      {options ? (
        <Select options={options} style={{ width: '100%' }} />
      ) : isBooleanField ? (
        <Switch checkedChildren="开启" unCheckedChildren="关闭" />
      ) : field.secret ? (
        <Input.Password
          placeholder={field.placeholder}
          visibilityToggle={{
            visible: !showSecret,
            onVisibleChange: setShowSecret,
          }}
          iconRender={(visible) => (visible ? <EyeOutlined /> : <EyeInvisibleOutlined />)}
        />
      ) : field.type === 'textarea' ? (
        <Input.TextArea rows={6} placeholder={field.placeholder} />
      ) : (
        <Input placeholder={field.placeholder} />
      )}
    </Form.Item>
  )
}

function ConfigSection({
  section,
  defaultCollapsed = false,
  autoExpand = false,
}: {
  section: SectionConfig
  defaultCollapsed?: boolean
  autoExpand?: boolean
}) {
  const [collapsed, setCollapsed] = useState(defaultCollapsed)
  const [helpCollapsed, setHelpCollapsed] = useState(true)

  useEffect(() => {
    if (autoExpand) {
      setCollapsed(false)
    }
  }, [autoExpand, section.title])

  return (
    <Card
      title={section.title}
      extra={(
        <Space size={8} wrap>
          {section.desc ? <span style={{ fontSize: 12, color: '#7a8ba3' }}>{section.desc}</span> : null}
          {defaultCollapsed ? (
            <Button size="small" type="link" onClick={() => setCollapsed((value) => !value)}>
              {collapsed ? '展开' : '收起'}
            </Button>
          ) : null}
        </Space>
      )}
      style={{ marginBottom: 16 }}
    >
      {collapsed ? null : (
        <>
          {section.help ? (
            <div
              style={{
                border: '1px solid #d9e2ef',
                borderRadius: 8,
                background: '#f8fbff',
                marginBottom: 16,
                padding: '10px 12px',
              }}
            >
              <Button
                size="small"
                type="link"
                style={{ padding: 0, height: 'auto', fontWeight: 600 }}
                onClick={() => setHelpCollapsed((value) => !value)}
              >
                {helpCollapsed ? `展开${section.help.title}` : `收起${section.help.title}`}
              </Button>
              {helpCollapsed ? null : (
                <div style={{ marginTop: 10, color: '#42526e', fontSize: 13, lineHeight: 1.8 }}>
                  {section.help.lines.map((line) => (
                    <div key={line}>{line}</div>
                  ))}
                </div>
              )}
            </div>
          ) : null}
          {section.fields.map((field) => (
            <ConfigField key={field.key} field={field} />
          ))}
        </>
      )}
    </Card>
  )
}

function CFWorkerDomainPoolSection({ form }: { form: any }) {
  const watchedDomains = Form.useWatch('cfworker_domains', form) || []
  const watchedEnabledDomains = Form.useWatch('cfworker_enabled_domains', form) || []
  const normalizedDomains = normalizeDomainList(watchedDomains)
  const enabledDomains = normalizeDomainList(watchedEnabledDomains).filter((domain) => normalizedDomains.includes(domain))

  const updateEnabledDomains = (nextDomains: string[]) => {
    form.setFieldValue('cfworker_enabled_domains', normalizeDomainList(nextDomains))
  }

  const toggleEnabledDomain = (domain: string, checked: boolean) => {
    if (checked) {
      updateEnabledDomains([...enabledDomains, domain])
      return
    }
    updateEnabledDomains(enabledDomains.filter((item) => item !== domain))
  }

  return (
    <Card
      title="CF Worker 域名池"
      extra={<span style={{ fontSize: 12, color: '#7a8ba3' }}>注册时会从已启用域名中随机选择一个</span>}
      style={{ marginBottom: 16 }}
    >
      <Form.List name="cfworker_domains">
        {(fields, { add, remove }) => (
          <div style={{ display: 'flex', flexDirection: 'column', gap: 12 }}>
            {fields.map((field) => (
              <Space key={field.key} align="start" style={{ display: 'flex' }}>
                <Form.Item
                  {...field}
                  label={field.name === 0 ? '全部域名' : ''}
                  style={{ flex: 1, marginBottom: 0 }}
                  rules={[
                    {
                      validator: async (_, value) => {
                        if (!String(value || '').trim()) {
                          throw new Error('请输入域名')
                        }
                      },
                    },
                  ]}
                >
                  <Input placeholder="example.com" />
                </Form.Item>
                <Button
                  danger
                  onClick={() => {
                    const currentDomains = Array.isArray(form.getFieldValue('cfworker_domains'))
                      ? [...form.getFieldValue('cfworker_domains')]
                      : []
                    const removedDomain = String(currentDomains[field.name] || '').trim().toLowerCase().replace(/^@/, '')
                    remove(field.name)
                    if (!removedDomain) return
                    const enabledDomains = normalizeDomainList(form.getFieldValue('cfworker_enabled_domains'))
                    form.setFieldValue(
                      'cfworker_enabled_domains',
                      enabledDomains.filter((domain) => domain !== removedDomain),
                    )
                  }}
                >
                  删除
                </Button>
              </Space>
            ))}
            {fields.length === 0 ? (
              <Typography.Text type="secondary">还没有配置域名。添加后即可在下方选择启用项。</Typography.Text>
            ) : null}
            <Button type="dashed" onClick={() => add('')} icon={<PlusOutlined />} block>
              添加域名
            </Button>
          </div>
        )}
      </Form.List>

      <Form.Item name="cfworker_enabled_domains" hidden>
        <Select mode="multiple" options={normalizedDomains.map((domain) => ({ label: domain, value: domain }))} />
      </Form.Item>

      <div style={{ marginTop: 16 }}>
        <div style={{ marginBottom: 8, fontWeight: 500 }}>已启用域名</div>
        {enabledDomains.length > 0 ? (
          <Space wrap>
            {enabledDomains.map((domain) => (
              <Tag
                key={domain}
                color="blue"
                closable
                onClose={(event) => {
                  event.preventDefault()
                  updateEnabledDomains(enabledDomains.filter((item) => item !== domain))
                }}
              >
                {domain}
              </Tag>
            ))}
          </Space>
        ) : (
          <Typography.Text type="secondary">暂无启用域名，点击下方域名即可启用。</Typography.Text>
        )}
      </div>

      <div style={{ marginTop: 16 }}>
        <div style={{ marginBottom: 8, fontWeight: 500 }}>点击切换启用状态</div>
        {normalizedDomains.length > 0 ? (
          <Space wrap>
            {normalizedDomains.map((domain) => (
              <Tag.CheckableTag
                key={domain}
                checked={enabledDomains.includes(domain)}
                onChange={(checked) => toggleEnabledDomain(domain, checked)}
              >
                {domain}
              </Tag.CheckableTag>
            ))}
          </Space>
        ) : (
          <Typography.Text type="secondary">请先在上方添加域名。</Typography.Text>
        )}
      </div>
      <Typography.Text type="secondary" style={{ display: 'block', marginTop: 12 }}>
        仅已启用域名会参与注册；点击已启用标签可直接移除。
      </Typography.Text>
    </Card>
  )
}

function AppleMailPoolImportSection({ form }: { form: any }) {
  const [content, setContent] = useState('')
  const [filename, setFilename] = useState('')
  const [importing, setImporting] = useState(false)
  const [snapshot, setSnapshot] = useState<AppleMailPoolSnapshot | null>(null)
  const [loadingSnapshot, setLoadingSnapshot] = useState(false)
  const watchedPoolDir = Form.useWatch('applemail_pool_dir', form) || 'mail'
  const watchedPoolFile = Form.useWatch('applemail_pool_file', form) || ''

  const loadSnapshot = async () => {
    setLoadingSnapshot(true)
    try {
      const params = new URLSearchParams()
      if (String(watchedPoolDir || '').trim()) {
        params.set('pool_dir', String(watchedPoolDir || '').trim())
      }
      if (String(watchedPoolFile || '').trim()) {
        params.set('pool_file', String(watchedPoolFile || '').trim())
      }
      const result = await apiFetch(`/config/applemail/pool?${params.toString()}`)
      setSnapshot(result)
    } catch {
      setSnapshot(null)
    } finally {
      setLoadingSnapshot(false)
    }
  }

  useEffect(() => {
    void loadSnapshot()
  }, [watchedPoolDir, watchedPoolFile])

  const handleImport = async () => {
    if (!content.trim()) {
      message.error('请输入 JSON 或 TXT 内容')
      return
    }

    setImporting(true)
    try {
      const poolDir = String(form.getFieldValue('applemail_pool_dir') || 'mail').trim() || 'mail'
      const result = await apiFetch('/config/applemail/import', {
        method: 'POST',
        body: JSON.stringify({
          content,
          filename,
          pool_dir: poolDir,
          bind_to_config: true,
        }),
      })

      form.setFieldsValue({
        mail_provider: 'applemail',
        applemail_pool_dir: result.pool_dir,
        applemail_pool_file: result.filename,
      })
      setSnapshot({
        filename: result.filename,
        pool_dir: result.pool_dir,
        count: result.count,
        items: result.items || [],
        truncated: Boolean(result.truncated),
      })
      setContent('')
      setFilename('')
      message.success(`导入成功，共 ${result.count} 个邮箱，已绑定 ${result.filename}`)
    } catch (error: unknown) {
      const errorMessage = error instanceof Error ? error.message : 'AppleMail 内容导入失败'
      message.error(errorMessage || 'AppleMail 内容导入失败')
    } finally {
      setImporting(false)
    }
  }

  return (
    <Card
      title="AppleMail 内容导入"
      extra={<span style={{ fontSize: 12, color: '#7a8ba3' }}>支持 JSON 或 TXT；导入后自动绑定当前邮箱池文件</span>}
      style={{ marginBottom: 16 }}
    >
      <Space direction="vertical" style={{ width: '100%' }} size={12}>
        <Typography.Text type="secondary">
          支持数组/对象 JSON，也支持 `mail/*.txt` 那种每行一条的 `email----password----client_id----refresh_token` 格式。常见字段别名如 `clientId` / `refreshToken` / `folder` 会自动规范化。
        </Typography.Text>
        <Input
          value={filename}
          onChange={(event) => setFilename(event.target.value)}
          placeholder="可选文件名，例如 applemail_hotmail.json；留空自动生成"
        />
        <Input.TextArea
          value={content}
          onChange={(event) => setContent(event.target.value)}
          rows={10}
          placeholder={'[\n  {\n    "email": "demo@example.com",\n    "clientId": "xxxx",\n    "refreshToken": "xxxx",\n    "folder": "INBOX"\n  }\n]\n\n或粘贴 TXT:\ndemo@example.com----password----client_id----refresh_token'}
          style={{ fontFamily: 'monospace' }}
        />
        <Space style={{ width: '100%', justifyContent: 'space-between' }}>
          <Button
            danger
            onClick={() => {
              setContent('')
              setFilename('')
            }}
          >
            清空
          </Button>
          <Space>
            <Button onClick={() => void loadSnapshot()} loading={loadingSnapshot}>
              刷新预览
            </Button>
            <Button type="primary" onClick={handleImport} loading={importing}>
              确认导入
            </Button>
          </Space>
        </Space>

        <div style={{ display: 'flex', alignItems: 'center', gap: 8, flexWrap: 'wrap' }}>
          <Tag color="blue">已导入: {snapshot?.count || 0} 个邮箱</Tag>
          {snapshot?.filename ? <Typography.Text type="secondary">当前文件: {snapshot.filename}</Typography.Text> : null}
        </div>

        <div
          style={{
            border: '1px solid rgba(127,127,127,0.25)',
            borderRadius: 8,
            padding: 12,
            background: 'rgba(127,127,127,0.06)',
            minHeight: 88,
            maxHeight: 260,
            overflowY: 'auto',
            fontFamily: 'monospace',
            fontSize: 13,
            lineHeight: 1.7,
          }}
        >
          {snapshot?.items?.length ? (
            snapshot.items.map((item) => (
              <div key={`${item.index}-${item.email}`}>
                {item.index}. {item.email}
              </div>
            ))
          ) : (
            <Typography.Text type="secondary">当前还没有可预览的邮箱池内容。</Typography.Text>
          )}
        </div>
        {snapshot?.truncated ? (
          <Typography.Text type="secondary">预览只展示前 100 个邮箱，完整内容以文件为准。</Typography.Text>
        ) : null}
      </Space>
    </Card>
  )
}

function SolverStatus() {
  const [running, setRunning] = useState<boolean | null>(null)

  const checkSolver = async () => {
    try {
      const d = await apiFetch('/solver/status')
      setRunning(d.running)
    } catch {
      setRunning(false)
    }
  }

  const restartSolver = async () => {
    await apiFetch('/solver/restart', { method: 'POST' })
    setRunning(null)
    setTimeout(checkSolver, 2000)
  }

  useEffect(() => {
    checkSolver()
    const timer = window.setInterval(checkSolver, 5000)
    return () => window.clearInterval(timer)
  }, [])

  return (
    <Card title="Turnstile Solver" size="small" style={{ marginBottom: 16 }}>
      <div
        style={{
          display: 'flex',
          alignItems: 'center',
          justifyContent: 'space-between',
          gap: 12,
          flexWrap: 'wrap',
        }}
      >
        <Space size={8}>
          {running === null ? (
            <SyncOutlined spin style={{ color: '#7a8ba3' }} />
          ) : running ? (
            <CheckCircleOutlined style={{ color: '#10b981' }} />
          ) : (
            <CloseCircleOutlined style={{ color: '#ef4444' }} />
          )}
          <span style={{ color: running ? '#10b981' : '#7a8ba3', fontWeight: 500 }}>
            {running === null ? '检测中' : running ? '运行中' : '未运行'}
          </span>
        </Space>
        <Button size="small" onClick={restartSolver}>
          重启 Solver
        </Button>
      </div>
    </Card>
  )
}

function IntegrationsPanel() {
  const [items, setItems] = useState<any[]>([])
  const [loading, setLoading] = useState(false)
  const [busy, setBusy] = useState('')
  const [resultModal, setResultModal] = useState({
    open: false,
    title: '',
    ok: true,
    content: '',
  })

  const showResultModal = (title: string, data: unknown, ok = true) => {
    setResultModal({
      open: true,
      title,
      ok,
      content: formatResultText(data),
    })
  }

  const load = async () => {
    setLoading(true)
    try {
      const d = await apiFetch('/integrations/services')
      setItems(d.items || [])
    } finally {
      setLoading(false)
    }
  }

  useEffect(() => {
    load()
    const timer = window.setInterval(load, 5000)
    return () => window.clearInterval(timer)
  }, [])

  const doAction = async (key: string, request: Promise<any>) => {
    setBusy(key)
    try {
      const result = await request
      await load()
      message.success('操作完成')
      showResultModal('操作结果', result, true)
    } catch (e: any) {
      message.error(e?.message || '操作失败')
      showResultModal('操作结果', e?.message || e || '操作失败', false)
      await load()
    } finally {
      setBusy('')
    }
  }

  return (
    <div style={{ display: 'flex', flexDirection: 'column', gap: 16 }}>
      <Modal
        open={resultModal.open}
        title={resultModal.title}
        onCancel={() => setResultModal((v) => ({ ...v, open: false }))}
        onOk={() => setResultModal((v) => ({ ...v, open: false }))}
        width={760}
      >
        <Typography.Paragraph style={{ marginBottom: 8, color: resultModal.ok ? '#10b981' : '#ef4444' }}>
          {resultModal.ok ? '操作已完成。' : '操作失败。'}
        </Typography.Paragraph>
        <pre
          style={{
            margin: 0,
            maxHeight: 420,
            overflow: 'auto',
            padding: 12,
            borderRadius: 8,
            background: 'rgba(127,127,127,0.08)',
            fontSize: 12,
            lineHeight: 1.5,
            whiteSpace: 'pre-wrap',
            wordBreak: 'break-word',
          }}
        >
          {resultModal.content}
        </pre>
      </Modal>

      <Card title="批量操作">
        <Space wrap>
          <Button loading={busy === 'start-all'} onClick={() => doAction('start-all', apiFetch('/integrations/services/start-all', { method: 'POST' }))}>
            启动全部（已安装）
          </Button>
          <Button loading={busy === 'stop-all'} onClick={() => doAction('stop-all', apiFetch('/integrations/services/stop-all', { method: 'POST' }))}>
            停止全部
          </Button>
          <Button loading={loading} onClick={load}>
            刷新状态
          </Button>
        </Space>
      </Card>

      {items.map((item) => (
        <Card key={item.name} title={item.label}>
          <Space direction="vertical" style={{ width: '100%' }}>
            <div>
              状态：
              <Tag color={item.running ? 'green' : 'default'} style={{ marginLeft: 8 }}>
                {item.running ? '运行中' : '未运行'}
              </Tag>
              <Tag color={item.repo_exists ? 'blue' : 'orange'} style={{ marginLeft: 8 }}>
                {item.repo_exists ? '已安装' : '未安装'}
              </Tag>
              {item.pid ? <span style={{ marginLeft: 8 }}>PID: {item.pid}</span> : null}
            </div>
            <div>插件目录：<Typography.Text copyable>{item.repo_path}</Typography.Text></div>
            {item.url ? <div>地址：<Typography.Text copyable>{item.url}</Typography.Text></div> : null}
            {item.management_url ? <div>管理页：<Typography.Text copyable>{item.management_url}</Typography.Text></div> : null}
            {item.management_key ? <div>登录口令：<Typography.Text copyable>{item.management_key}</Typography.Text></div> : null}
            <div>日志：<Typography.Text copyable>{item.log_path}</Typography.Text></div>
            {item.last_error ? <div style={{ color: '#ef4444' }}>最近错误：{item.last_error}</div> : null}
            <Space wrap>
              {item.management_url ? (
                <Button onClick={() => window.open(item.management_url, '_blank')}>
                  打开管理页
                </Button>
              ) : null}
              {!item.repo_exists ? (
                <Button
                  type="primary"
                  loading={busy === `install-${item.name}`}
                  onClick={() => doAction(`install-${item.name}`, apiFetch(`/integrations/services/${item.name}/install`, { method: 'POST' }))}
                >
                  安装
                </Button>
              ) : null}
              <Button
                loading={busy === `start-${item.name}`}
                disabled={!item.repo_exists}
                onClick={() => doAction(`start-${item.name}`, apiFetch(`/integrations/services/${item.name}/start`, { method: 'POST' }))}
              >
                启动
              </Button>
              <Button
                loading={busy === `stop-${item.name}`}
                onClick={() => doAction(`stop-${item.name}`, apiFetch(`/integrations/services/${item.name}/stop`, { method: 'POST' }))}
              >
                停止
              </Button>
            </Space>
          </Space>
        </Card>
      ))}
    </div>
  )
}

function ContributionPanel({
  form,
  onSave,
  saving,
  saved,
}: {
  form: any
  onSave: () => Promise<void>
  saving: boolean
  saved: boolean
}) {
  const [loadingStats, setLoadingStats] = useState(false)
  const [redeeming, setRedeeming] = useState(false)
  const [creatingKey, setCreatingKey] = useState(false)
  const [redeemAmount, setRedeemAmount] = useState<number>(CONTRIBUTION_REDEEM_OPTIONS[0])
  const [statsResponse, setStatsResponse] = useState<Record<string, unknown> | null>(null)
  const [redeemResponse, setRedeemResponse] = useState<Record<string, unknown> | null>(null)
  const [statsError, setStatsError] = useState('')

  const contributionEnabled = Form.useWatch('contribution_enabled', form)
  const contributionServerUrl = String(Form.useWatch('contribution_server_url', form) || '').trim()
  const contributionKey = String(Form.useWatch('contribution_key', form) || '').trim()

  const rawData = asRecord(statsResponse?.['data'])
  const serverInfo = pickRecord(rawData, ['server_info', 'server', 'server_stats', 'stats']) || rawData
  const keyInfo = pickRecord(rawData, ['key_info', 'keyInfo', 'public_key_info', 'quota']) || rawData

  const keyFromStats = pickString(keyInfo, ['key', 'public_key', 'api_key']) || contributionKey
  const keyBalance =
    pickNumber(keyInfo, ['balance_usd', 'balance', 'current_balance', 'remaining_balance_usd']) ??
    pickNumber(rawData, ['balance_usd', 'balance', 'current_balance'])
  const keySource = pickString(keyInfo, ['source', 'key_source', 'origin']) || '-'
  const boundAccounts =
    pickNumber(keyInfo, ['bound_account_count', 'bind_account_count', 'bound_accounts', 'account_count']) ??
    (Array.isArray(keyInfo?.['accounts']) ? keyInfo['accounts'].length : null)
  const settlementAmount =
    pickNumber(keyInfo, ['settlement_amount_usd', 'settlement_amount', 'settled_amount_usd']) ??
    pickNumber(rawData, ['settlement_amount_usd', 'settlement_amount'])
  const serverQuotaAccountCount = pickNumber(serverInfo, ['quota_account_count'])
  const serverQuotaTotal = pickNumber(serverInfo, ['quota_total'])
  const serverQuotaUsed = pickNumber(serverInfo, ['quota_used'])
  const serverQuotaRemaining = pickNumber(serverInfo, ['quota_remaining'])
  const serverQuotaUsedPercent = pickNumber(serverInfo, ['quota_used_percent'])
  const serverQuotaRemainingPercent = pickNumber(serverInfo, ['quota_remaining_percent'])
  const serverQuotaRemainingAccounts = pickNumber(serverInfo, ['quota_remaining_accounts'])
  const redeemData = asRecord(redeemResponse?.['data']) || asRecord(redeemResponse)
  const redeemCode = pickString(redeemData, ['code', 'redeem_code', 'voucher_code'])
  const redeemedAmountUSD = pickNumber(redeemData, ['redeemed_amount_usd', 'redeemed_amount', 'amount_usd'])
  const redeemSuccessText =
    redeemResponse
      ? `提现成功！额度：${redeemedAmountUSD !== null ? formatDisplayNumber(redeemedAmountUSD, 2) : '-'} 兑换码：${redeemCode || '-'}`
      : ''

  const fetchStats = async (silent = false, keyOverride?: string) => {
    if (!contributionEnabled) {
      if (!silent) message.warning('请先开启贡献功能')
      return
    }
    if (!contributionServerUrl) {
      if (!silent) message.error('请先填写服务器地址')
      return
    }

    setLoadingStats(true)
    setStatsError('')
    try {
      const data = await apiFetch('/contribution/quota-stats', {
        method: 'POST',
        body: JSON.stringify({
          server_url: contributionServerUrl,
          key: keyOverride ?? contributionKey,
        }),
      })
      setStatsResponse(asRecord(data))
      if (!silent) {
        message.success('额度信息已刷新')
      }
    } catch (e: any) {
      const detail = String(e?.message || '获取额度信息失败')
      setStatsError(detail)
      if (!silent) {
        message.error(detail)
      }
    } finally {
      setLoadingStats(false)
    }
  }

  const doRedeem = async () => {
    if (!contributionEnabled) {
      message.warning('请先开启贡献功能')
      return
    }
    if (!contributionServerUrl) {
      message.error('请先填写服务器地址')
      return
    }
    if (!contributionKey) {
      message.error('请先填写 API Key')
      return
    }

    const confirmed = window.confirm(`确认提现吗？\n将按 ${redeemAmount} 发起提现请求`)
    if (!confirmed) return

    setRedeeming(true)
    try {
      const data = await apiFetch('/contribution/redeem', {
        method: 'POST',
        body: JSON.stringify({
          server_url: contributionServerUrl,
          key: contributionKey,
          amount_usd: redeemAmount,
        }),
      })
      const result = asRecord(data)
      const payload = asRecord(result?.['data']) || result
      const code = pickString(payload, ['code', 'redeem_code', 'voucher_code'])
      const amount = pickNumber(payload, ['redeemed_amount_usd', 'redeemed_amount', 'amount_usd'])
      setRedeemResponse(result)
      if (amount !== null || code) {
        message.success(`提现成功！额度：${amount !== null ? formatDisplayNumber(amount, 2) : '-'} 兑换码：${code || '-'}`)
      } else {
        message.success('提现成功')
      }
      await fetchStats(true)
    } catch (e: any) {
      const detail = String(e?.message || '提现失败')
      setRedeemResponse({ ok: false, error: detail })
      message.error(detail)
    } finally {
      setRedeeming(false)
    }
  }

  const doGenerateKey = async () => {
    if (!contributionServerUrl) {
      message.error('请先填写服务器地址')
      return
    }
    setCreatingKey(true)
    try {
      const result = await apiFetch('/contribution/generate-key', {
        method: 'POST',
        body: JSON.stringify({
          server_url: contributionServerUrl,
        }),
      })
      const payload = asRecord(asRecord(result)?.data)
      const generated = pickString(payload, ['key', 'api_key', 'public_key'])
      if (!generated) {
        throw new Error('服务端未返回可用 key')
      }
      form.setFieldValue('contribution_key', generated)
      message.success('已新建并填充 API Key，请点击保存配置')
      if (contributionEnabled) {
        await fetchStats(true, generated)
      }
    } catch (e: any) {
      message.error(String(e?.message || '请求新建 key 失败'))
    } finally {
      setCreatingKey(false)
    }
  }

  return (
    <div style={{ display: 'flex', flexDirection: 'column', gap: 16 }}>
      <Card title="配置">
        <Alert
          type="warning"
          showIcon
          banner
          style={{ marginBottom: 12 }}
          message="开启贡献模式后，注册成功账号将只上传到贡献服务器"
          description={(
            <>
              <div>CPA / CodexProxy / Sub2API 自动上传会被停用，避免重复上报。</div>
              <div>目前该功能在xem中转站测试中 有兴趣可以进群了解</div>
              <div>中转站https://ai.xem8k5.top/ 群号634758974</div>
            </>
          )}
        />
        <Form.Item name="contribution_enabled" label="是否开启" valuePropName="checked">
          <Switch checkedChildren="开启" unCheckedChildren="关闭" />
        </Form.Item>
        <Form.Item
          name="contribution_server_url"
          label="服务器地址"
          rules={[{ required: true, message: '请输入服务器地址' }]}
        >
          <Input placeholder="http://new.xem8k5.top:7317/" />
        </Form.Item>
        <Form.Item name="contribution_key" label="API Key">
          <Input
            placeholder="留空可点击右侧按钮自动创建"
            addonAfter={(
              <Button
                type="link"
                size="small"
                loading={creatingKey}
                onClick={() => { void doGenerateKey() }}
                style={{ paddingInline: 0 }}
              >
                没有key?请求新建
              </Button>
            )}
          />
        </Form.Item>
        <Button type="primary" icon={<SaveOutlined />} onClick={onSave} loading={saving} block>
          {saved ? '已保存 ✓' : '保存配置'}
        </Button>
      </Card>

      <Card
        title="信息"
        extra={(
          <Button loading={loadingStats} onClick={() => { void fetchStats() }}>
            刷新信息
          </Button>
        )}
      >
        {!contributionEnabled ? (
          <Alert type="info" showIcon message="贡献功能已关闭，开启后可获取服务器与 key 信息。" />
        ) : (
          <Space direction="vertical" style={{ width: '100%' }} size={12}>
            {statsError ? <Alert type="error" showIcon message={statsError} /> : null}
            <div>
              <Typography.Text strong>服务器信息</Typography.Text>
              <div style={{ marginTop: 8, display: 'grid', gridTemplateColumns: 'repeat(auto-fit, minmax(180px, 1fr))', gap: 8 }}>
                <Tag color="blue">账号数: {formatDisplayNumber(serverQuotaAccountCount)}</Tag>
                <Tag color="geekblue">总额度: {formatDisplayNumber(serverQuotaTotal)}</Tag>
                <Tag color="volcano">已用额度: {formatDisplayNumber(serverQuotaUsed)}</Tag>
                <Tag color="green">剩余额度: {formatDisplayNumber(serverQuotaRemaining)}</Tag>
                <Tag color="orange">已用占比: {formatDisplayPercent(serverQuotaUsedPercent)}</Tag>
                <Tag color="cyan">剩余占比: {formatDisplayPercent(serverQuotaRemainingPercent)}</Tag>
                <Tag color="purple">折算账号数: {formatDisplayNumber(serverQuotaRemainingAccounts, 2)}</Tag>
              </div>
            </div>
            <div>
              <Typography.Text strong>API Key</Typography.Text>
              <Space style={{ marginLeft: 8 }}>
                <Typography.Text copyable={keyFromStats ? { text: keyFromStats } : undefined}>
                  {keyFromStats || '-'}
                </Typography.Text>
              </Space>
            </div>
            <div>
              <Typography.Text strong>key 信息</Typography.Text>
              <div style={{ marginTop: 8, display: 'grid', gridTemplateColumns: 'repeat(auto-fit, minmax(180px, 1fr))', gap: 8 }}>
                <Tag color="blue">余额: {keyBalance ?? '-'}</Tag>
                <Tag color="geekblue">来源: {keySource}</Tag>
                <Tag color="cyan">绑定账号数: {boundAccounts ?? '-'}</Tag>
                <Tag color="purple">结算金额: {settlementAmount ?? '-'}</Tag>
              </div>
            </div>
          </Space>
        )}
      </Card>

      <Card title="提现">
        <Space direction="vertical" style={{ width: '100%' }}>
          <Typography.Text>key 当前额度：{keyBalance ?? '-'}</Typography.Text>
          <Form.Item label="提现金额" style={{ marginBottom: 0 }}>
            <Select
              value={redeemAmount}
              onChange={setRedeemAmount}
              style={{ width: 240 }}
              options={CONTRIBUTION_REDEEM_OPTIONS.map((amount) => ({ label: String(amount), value: amount }))}
            />
          </Form.Item>
          <Button type="primary" danger onClick={() => { void doRedeem() }} loading={redeeming}>
            提现确认
          </Button>
          {redeemResponse ? (
            <Alert
              type={redeemResponse.ok === false ? 'error' : 'success'}
              showIcon
              message={redeemResponse.ok === false ? `提现失败：${String(redeemResponse.error || '-')}` : redeemSuccessText}
              description={<pre style={{ margin: 0, whiteSpace: 'pre-wrap' }}>{formatResultText(redeemResponse)}</pre>}
            />
          ) : null}
        </Space>
      </Card>
    </div>
  )
}

function OutlookImportSection() {
  const { message: msg } = App.useApp()
  const [value, setValue] = useState('')
  const [loading, setLoading] = useState(false)
  const [result, setResult] = useState<any | null>(null)

  const handleSubmit = async () => {
    const payload = String(value || '').trim()
    if (!payload) {
      msg.error('请输入 Outlook 账号内容')
      return
    }
    setLoading(true)
    try {
      const res = await apiFetch('/outlook/batch-import', {
        method: 'POST',
        body: JSON.stringify({ data: payload, enabled: true }),
      })
      setResult(res)
      msg.success(`导入完成：成功 ${res.success} / 失败 ${res.failed}`)
    } catch (e: any) {
      msg.error(e?.message || '导入失败')
      setResult({ error: e?.message || String(e) })
    } finally {
      setLoading(false)
    }
  }

  return (
    <Card
      title="Outlook 批量导入"
      extra={<span style={{ fontSize: 12, color: '#7a8ba3' }}>每行格式：邮箱----密码 或 邮箱----密码----client_id----refresh_token</span>}
      style={{ marginBottom: 16 }}
    >
      <Input.TextArea
        value={value}
        onChange={(e) => setValue(e.target.value)}
        placeholder={`example@outlook.com----password\nexample@outlook.com----password----client_id----refresh_token`}
        autoSize={{ minRows: 6, maxRows: 14 }}
      />
      <Space style={{ marginTop: 12 }}>
        <Button type="primary" loading={loading} onClick={handleSubmit}>
          导入
        </Button>
        <Button onClick={() => { setValue(''); setResult(null) }}>
          清空
        </Button>
      </Space>
      {result ? (
        <div style={{ marginTop: 12 }}>
          {'success' in result ? (
            <Alert
              type={result.failed ? 'warning' : 'success'}
              showIcon
              message={`导入完成：成功 ${result.success} / 失败 ${result.failed}`}
              description={result.errors && result.errors.length ? (
                <pre style={{ margin: 0, whiteSpace: 'pre-wrap' }}>{result.errors.join('\n')}</pre>
              ) : undefined}
            />
          ) : (
            <Alert type="error" showIcon message="导入失败" description={String(result.error || '')} />
          )}
        </div>
      ) : null}
    </Card>
  )
}

function ICloudHmeManagerSection({ form }: { form: any }) {
  const [syncing, setSyncing] = useState(false)
  const [loading, setLoading] = useState(false)
  const [bulkEnabling, setBulkEnabling] = useState(false)
  const [bulkDisablingUsed, setBulkDisablingUsed] = useState(false)
  const [switchingMode, setSwitchingMode] = useState(false)
  const [togglingId, setTogglingId] = useState('')
  const [aliases, setAliases] = useState<any[]>([])
  const [total, setTotal] = useState(0)
  const [availableImportPoolCount, setAvailableImportPoolCount] = useState(0)
  const [autoPoolStatus, setAutoPoolStatus] = useState<ICloudHmeAutoPoolStatus | null>(null)
  const [autoPoolStatusLoading, setAutoPoolStatusLoading] = useState(false)
  const [onlyReadyView, setOnlyReadyView] = useState(false)
  const [page, setPage] = useState(1)
  const [pageSize, setPageSize] = useState(20)
  const [statusFilter, setStatusFilter] = useState('')
  const [enabledFilter, setEnabledFilter] = useState('')
  const [sourceFilter, setSourceFilter] = useState('')
  const [searchHme, setSearchHme] = useState('')

  const loadAutoPoolStatus = async () => {
    setAutoPoolStatusLoading(true)
    try {
      const data = await apiFetch('/icloud-hme/auto-pool/status')
      setAutoPoolStatus(data || null)
    } catch {
      setAutoPoolStatus(null)
    } finally {
      setAutoPoolStatusLoading(false)
    }
  }

  const loadAliases = async (
    nextPage = page,
    nextPageSize = pageSize,
    nextOnlyReadyView = onlyReadyView,
    nextStatus = statusFilter,
    nextEnabled = enabledFilter,
    nextSource = sourceFilter,
    nextSearchHme = searchHme,
  ) => {
    setLoading(true)
    try {
      const values = form.getFieldsValue(true)
      const currentForwardTo = String(values.icloud_forward_to || 'b@cccy.me').trim() || 'b@cccy.me'
      const params = new URLSearchParams({
        page: String(nextPage),
        size: String(nextPageSize),
        purpose: 'chatgpt_register',
        bound_service: 'chatgpt',
      })
      if (nextOnlyReadyView) {
        params.set('ready_only', 'true')
        params.set('forward_to', currentForwardTo)
      }
      if (nextStatus) params.set('status', nextStatus)
      if (nextEnabled) params.set('enabled', nextEnabled)
      if (nextSource) params.set('created_source', nextSource)
      if (String(nextSearchHme || '').trim()) params.set('hme', String(nextSearchHme || '').trim())

      const data = await apiFetch(`/icloud-hme/aliases?${params.toString()}`)
      setAliases(Array.isArray(data?.data) ? data.data : [])
      setTotal(Number(data?.total || 0) || 0)
      setAvailableImportPoolCount(Number(data?.available_import_pool_count || 0) || 0)
      setPage(Number(data?.page || nextPage) || 1)
      setPageSize(Number(data?.size || nextPageSize) || nextPageSize)
      loadAutoPoolStatus().catch(() => {})
    } catch (e: any) {
      message.error(e?.message || '读取 iCloud HME 别名失败')
    } finally {
      setLoading(false)
    }
  }

  useEffect(() => {
    loadAliases().catch(() => {})
    loadAutoPoolStatus().catch(() => {})
  }, [])

  const syncLiveAliases = async () => {
    const values = form.getFieldsValue(true)
    const payload = {
      icloud_cookie: String(values.icloud_cookie || '').trim(),
      icloud_domain_base: String(values.icloud_domain_base || 'icloud.com').trim() || 'icloud.com',
      forward_to: String(values.icloud_forward_to || 'b@cccy.me').trim() || 'b@cccy.me',
      purpose: 'chatgpt_register',
      bound_service: 'chatgpt',
    }
    if (!payload.icloud_cookie) {
      message.error('请先填写 iCloud Cookie')
      return
    }
    setSyncing(true)
    try {
      const result = await apiFetch('/icloud-hme/sync-live', {
        method: 'POST',
        body: JSON.stringify(payload),
      })
      message.success(`同步完成：${Number(result?.synced_count || 0)} 条`)
      await loadAliases(1, pageSize)
    } catch (e: any) {
      message.error(e?.message || '同步 iCloud 官网别名失败')
    } finally {
      setSyncing(false)
    }
  }

  const bulkEnableAvailableAliases = async () => {
    const values = form.getFieldsValue(true)
    const forwardTo = String(values.icloud_forward_to || 'b@cccy.me').trim() || 'b@cccy.me'
    setBulkEnabling(true)
    try {
      const result = await apiFetch('/icloud-hme/aliases/bulk-enable', {
        method: 'POST',
        body: JSON.stringify({
          forward_to: forwardTo,
          only_manual_created: false,
          only_unused: true,
        }),
      })
      message.success(
        `批量启用完成：命中 ${Number(result?.matched || 0)} 条，新启用 ${Number(result?.enabled || 0)} 条，恢复失败 ${Number(result?.recycled || 0)} 条`
      )
      await loadAliases(page, pageSize)
    } catch (e: any) {
      message.error(e?.message || '批量启用失败')
    } finally {
      setBulkEnabling(false)
    }
  }

  const switchToImportPoolMode = async () => {
    const values = form.getFieldsValue(true)
    const nextValues = {
      ...values,
      icloud_hme_mode: 'import_pool',
    }
    setSwitchingMode(true)
    try {
      await apiFetch('/config', {
        method: 'PUT',
        body: JSON.stringify({ data: nextValues }),
      })
      form.setFieldsValue({ icloud_hme_mode: 'import_pool' })
      message.success('已切换到 import_pool 模式')
    } catch (e: any) {
      message.error(e?.message || '切换 import_pool 模式失败')
    } finally {
      setSwitchingMode(false)
    }
  }

  const bulkDisableUsedAliases = async () => {
    const values = form.getFieldsValue(true)
    const forwardTo = String(values.icloud_forward_to || 'b@cccy.me').trim() || 'b@cccy.me'
    setBulkDisablingUsed(true)
    try {
      const result = await apiFetch('/icloud-hme/aliases/bulk-disable-used', {
        method: 'POST',
        body: JSON.stringify({
          forward_to: forwardTo,
        }),
      })
      message.success(`批量停用完成：命中 ${Number(result?.matched || 0)} 条，新停用 ${Number(result?.disabled || 0)} 条`)
      await loadAliases(page, pageSize)
    } catch (e: any) {
      message.error(e?.message || '批量停用失败')
    } finally {
      setBulkDisablingUsed(false)
    }
  }

  const toggleAliasEnabled = async (anonymousId: string, enabled: boolean) => {
    if (!anonymousId) return
    setTogglingId(anonymousId)
    try {
      await apiFetch(`/icloud-hme/aliases/${encodeURIComponent(anonymousId)}/enabled`, {
        method: 'POST',
        body: JSON.stringify({ enabled }),
      })
      message.success(enabled ? '已启用到邮箱池' : '已从邮箱池停用')
      await loadAliases(page, pageSize)
    } catch (e: any) {
      message.error(e?.message || '切换启用状态失败')
    } finally {
      setTogglingId('')
    }
  }

  const copyText = async (item: any) => {
    const anonymousId = String(item?.anonymous_id || '').trim()
    const value = String(item?.hme || '').trim()
    if (!value) return
    setTogglingId(anonymousId)
    try {
      await navigator.clipboard.writeText(value)
      if (anonymousId) {
        await apiFetch(`/icloud-hme/aliases/${encodeURIComponent(anonymousId)}/mark-used`, {
          method: 'POST',
          body: JSON.stringify({ note: 'manually_copied' }),
        })
      }
      message.success('已复制，并标记为已使用')
      await loadAliases(page, pageSize)
    } catch {
      message.error('复制失败')
    } finally {
      setTogglingId('')
    }
  }

  const columns = [
    {
      title: '标签 / 地址',
      key: 'alias',
      render: (_: any, item: any) => (
        <div style={{ display: 'flex', flexDirection: 'column', gap: 4 }}>
          <Space size={6}>
            <Typography.Text strong>{String(item.label || '-')}</Typography.Text>
          </Space>
          <Space size={6}>
            <Typography.Text style={{ fontFamily: 'monospace' }}>{String(item.hme || '')}</Typography.Text>
            <Button
              size="small"
              type="text"
              icon={<CopyOutlined />}
              loading={togglingId === String(item.anonymous_id || '')}
              onClick={() => copyText(item)}
            />
          </Space>
        </div>
      ),
    },
    {
      title: '来源',
      key: 'source',
      width: 140,
      render: (_: any, item: any) => (
        <Tag color={item.is_manual_created ? 'blue' : 'purple'}>
          {item.is_manual_created ? '手动创建' : '系统创建'}
        </Tag>
      ),
    },
    {
      title: '使用状态',
      key: 'used',
      width: 140,
      render: (_: any, item: any) => (
        <Tag color={item.used_by_system ? 'orange' : 'green'}>
          {item.used_by_system ? '系统已使用' : '系统未使用'}
        </Tag>
      ),
    },
    {
      title: '绑定账号',
      dataIndex: 'bound_account_email',
      key: 'bound_account_email',
      width: 220,
      render: (value: string) => <Typography.Text type="secondary">{String(value || '-')}</Typography.Text>,
    },
    {
      title: '使用次数',
      dataIndex: 'use_count',
      key: 'use_count',
      width: 100,
      render: (value: number) => String(value ?? 0),
    },
    {
      title: '转发',
      dataIndex: 'forward_to',
      key: 'forward_to',
      width: 180,
      render: (value: string) => <Typography.Text type="secondary">{String(value || '-')}</Typography.Text>,
    },
    {
      title: '操作',
      key: 'actions',
      width: 180,
      render: (_: any, item: any) => (
        <Space>
          <Switch
            checked={Boolean(item.enabled)}
            checkedChildren="启用"
            unCheckedChildren="停用"
            loading={togglingId === String(item.anonymous_id || '')}
            onChange={(checked) => toggleAliasEnabled(String(item.anonymous_id || ''), checked)}
          />
        </Space>
      ),
    },
  ]

  const displayedAliases = aliases
  const statusReadyCount = Number(autoPoolStatus?.ready_count ?? availableImportPoolCount) || 0
  const statusStockLimit = Number(autoPoolStatus?.stock_limit || 0) || 0

  return (
    <Card
      title="iCloud HME 已创建邮箱"
      extra={(
        <Space>
          <Tag color="blue">总数: {total}</Tag>
          <Tag color="geekblue">当前显示: {displayedAliases.length}</Tag>
          <Button loading={bulkDisablingUsed} onClick={bulkDisableUsedAliases}>
            批量停用系统已使用别名
          </Button>
          <Button loading={switchingMode} onClick={switchToImportPoolMode}>
            一键切换 import_pool
          </Button>
          <Button loading={bulkEnabling} onClick={bulkEnableAvailableAliases}>
            批量启用可用官网别名
          </Button>
          <Button icon={<SyncOutlined />} loading={syncing || loading} onClick={syncLiveAliases}>
            同步官网别名
          </Button>
        </Space>
      )}
      style={{ marginBottom: 16 }}
    >
      <Alert
        type="info"
        showIcon
        style={{ marginBottom: 16 }}
        message="来源与使用状态说明"
        description="手动创建表示该别名来自 iCloud 官网同步；系统创建表示它由当前系统 live create 或导入消费链路落库。只有启用到邮箱池的别名，才会被 import_pool / prefer_import 模式拿去注册。系统已使用的别名仅作历史标记，不代表当前自动再次复用。register_failed 的官网别名可通过批量启用恢复回可用池。"
      />
      <div
        style={{
          marginBottom: 16,
          padding: 12,
          border: '1px solid rgba(24, 144, 255, 0.18)',
          borderRadius: 8,
          background: 'rgba(24, 144, 255, 0.04)',
        }}
      >
        <Space wrap size={[8, 8]} style={{ width: '100%', justifyContent: 'space-between' }}>
          <Space wrap>
            <Tag color={autoPoolStatus?.enabled ? 'green' : 'default'}>
              自动补池: {autoPoolStatus?.enabled ? '开启' : '关闭'}
            </Tag>
            <Tag color={autoPoolStatus?.running ? 'blue' : 'default'}>
              调度线程: {autoPoolStatus?.running ? '运行中' : '未运行'}
            </Tag>
            <Tag color={autoPoolStatus?.in_rate_limit_backoff ? 'red' : 'cyan'}>
              {autoPoolStatus?.in_rate_limit_backoff ? '限流等待中' : '未限流'}
            </Tag>
            <Tag color={statusStockLimit > 0 && statusReadyCount >= statusStockLimit ? 'gold' : 'green'}>
              库存: {statusReadyCount}/{statusStockLimit || '-'}
            </Tag>
          </Space>
          <Button size="small" loading={autoPoolStatusLoading} onClick={() => loadAutoPoolStatus()}>
            刷新自动补池状态
          </Button>
        </Space>
        <Space wrap size={[16, 6]} style={{ marginTop: 8 }}>
          <Typography.Text type="secondary">
            下次创建: {formatDateTimeText(autoPoolStatus?.next_run_at)}
          </Typography.Text>
          <Typography.Text type="secondary">
            剩余: {formatDurationText(autoPoolStatus?.seconds_until_next_run)}
          </Typography.Text>
          <Typography.Text type="secondary">
            随机间隔: {autoPoolStatus?.interval_min_minutes || '-'} - {autoPoolStatus?.interval_max_minutes || '-'} 分钟
          </Typography.Text>
          <Typography.Text type="secondary">
            限流延长: {autoPoolStatus?.rate_limit_backoff_minutes || '-'} 分钟
          </Typography.Text>
          <Typography.Text type="secondary">
            最近创建: {autoPoolStatus?.last_created_hme || '-'}
          </Typography.Text>
          <Typography.Text type={autoPoolStatus?.last_error ? 'danger' : 'secondary'}>
            最近错误: {autoPoolStatus?.last_error || '-'}
          </Typography.Text>
        </Space>
      </div>
      <Space wrap style={{ marginBottom: 12 }}>
        <Tag color="green">导入池可用账号: {availableImportPoolCount}</Tag>
        <Typography.Text type="secondary">
          按当前转发邮箱统计，表示 import_pool / prefer_import 实际还能领取去注册的邮箱数量
        </Typography.Text>
      </Space>
      <Space wrap style={{ marginBottom: 12 }}>
        <Switch
          checked={onlyReadyView}
          checkedChildren="只看可注册"
          unCheckedChildren="显示全部"
          onChange={(checked) => {
            setOnlyReadyView(checked)
            setPage(1)
            loadAliases(1, pageSize, checked, statusFilter, enabledFilter, sourceFilter, searchHme).catch(() => {})
          }}
        />
        <Select
          allowClear
          placeholder="状态"
          style={{ width: 140 }}
          value={statusFilter || undefined}
          onChange={(value) => {
            const nextValue = String(value || '')
            setStatusFilter(nextValue)
            setPage(1)
            loadAliases(1, pageSize, onlyReadyView, nextValue, enabledFilter, sourceFilter, searchHme).catch(() => {})
          }}
          options={[
            { label: 'reserved', value: 'reserved' },
            { label: 'in_use', value: 'in_use' },
            { label: 'registered', value: 'registered' },
            { label: 'register_failed', value: 'register_failed' },
            { label: 'retired', value: 'retired' },
          ]}
        />
        <Select
          allowClear
          placeholder="启用状态"
          style={{ width: 140 }}
          value={enabledFilter || undefined}
          onChange={(value) => {
            const nextValue = String(value || '')
            setEnabledFilter(nextValue)
            setPage(1)
            loadAliases(1, pageSize, onlyReadyView, statusFilter, nextValue, sourceFilter, searchHme).catch(() => {})
          }}
          options={[
            { label: '已启用', value: 'enabled' },
            { label: '已停用', value: 'disabled' },
          ]}
        />
        <Select
          allowClear
          placeholder="来源"
          style={{ width: 140 }}
          value={sourceFilter || undefined}
          onChange={(value) => {
            const nextValue = String(value || '')
            setSourceFilter(nextValue)
            setPage(1)
            loadAliases(1, pageSize, onlyReadyView, statusFilter, enabledFilter, nextValue, searchHme).catch(() => {})
          }}
          options={[
            { label: '手动创建', value: 'manual_created' },
            { label: '系统创建', value: 'live_create' },
            { label: 'CSV 导入', value: 'csv_import' },
          ]}
        />
        <Input.Search
          allowClear
          placeholder="搜索邮箱地址"
          style={{ width: 220 }}
          value={searchHme}
          onChange={(event) => setSearchHme(event.target.value)}
          onSearch={(value) => {
            const nextValue = String(value || '')
            setSearchHme(nextValue)
            setPage(1)
            loadAliases(1, pageSize, onlyReadyView, statusFilter, enabledFilter, sourceFilter, nextValue).catch(() => {})
          }}
        />
      </Space>
      <Table
        rowKey={(item) => String(item.anonymous_id || item.id)}
        loading={loading}
        columns={columns}
        dataSource={displayedAliases}
        pagination={{
          current: page,
          pageSize,
          total,
          showSizeChanger: true,
          pageSizeOptions: ['20', '50', '100'],
          onChange: (nextPage, nextPageSize) => {
            loadAliases(nextPage, nextPageSize, onlyReadyView, statusFilter, enabledFilter, sourceFilter, searchHme).catch(() => {})
          },
        }}
        locale={{ emptyText: '还没有同步到任何 iCloud HME 别名。' }}
        scroll={{ x: 1100, y: '60vh' }}
      />
    </Card>
  )
}

type TotpSetupState = 'idle' | 'setup'

function SecurityPanel() {
  const { message: msg } = App.useApp()
  const [status, setStatus] = useState<{ has_password: boolean; has_totp: boolean } | null>(null)
  const [loading, setLoading] = useState(false)

  const [enableForm] = Form.useForm()
  const [pwForm] = Form.useForm()
  const [codeForm] = Form.useForm()

  const [totpSetupState, setTotpSetupState] = useState<TotpSetupState>('idle')
  const [totpSecret, setTotpSecret] = useState('')
  const [totpUri, setTotpUri] = useState('')

  const loadStatus = async () => {
    try {
      const s = await apiFetch('/auth/status')
      setStatus(s)
    } catch {}
  }

  useEffect(() => { loadStatus() }, [])

  const handleEnable = async (values: { password: string; confirm: string }) => {
    if (values.password !== values.confirm) {
      msg.error('两次输入的密码不一致')
      return
    }
    setLoading(true)
    try {
      const d = await apiFetch('/auth/setup', {
        method: 'POST',
        body: JSON.stringify({ password: values.password }),
      })
      localStorage.setItem('auth_token', d.access_token)
      msg.success('密码保护已启用')
      enableForm.resetFields()
      await loadStatus()
    } catch (e: any) {
      msg.error(e.message)
    } finally {
      setLoading(false)
    }
  }

  const handleDisableAuth = async () => {
    setLoading(true)
    try {
      await apiFetch('/auth/disable', { method: 'POST' })
      localStorage.removeItem('auth_token')
      msg.success('密码保护已关闭')
      await loadStatus()
    } catch (e: any) {
      msg.error(e.message)
    } finally {
      setLoading(false)
    }
  }

  const handleChangePassword = async (values: { current_password: string; new_password: string; confirm: string }) => {
    if (values.new_password !== values.confirm) {
      msg.error('两次输入的新密码不一致')
      return
    }
    setLoading(true)
    try {
      await apiFetch('/auth/change-password', {
        method: 'POST',
        body: JSON.stringify({ current_password: values.current_password, new_password: values.new_password }),
      })
      msg.success('密码已更新')
      pwForm.resetFields()
    } catch (e: any) {
      msg.error(e.message)
    } finally {
      setLoading(false)
    }
  }

  const handleSetupTotp = async () => {
    setLoading(true)
    try {
      const d = await apiFetch('/auth/2fa/setup')
      setTotpSecret(d.secret)
      setTotpUri(d.uri)
      setTotpSetupState('setup')
    } catch (e: any) {
      msg.error(e.message)
    } finally {
      setLoading(false)
    }
  }

  const handleEnableTotp = async (values: { code: string }) => {
    setLoading(true)
    try {
      await apiFetch('/auth/2fa/enable', {
        method: 'POST',
        body: JSON.stringify({ secret: totpSecret, code: values.code }),
      })
      msg.success('双因素认证已启用')
      setTotpSetupState('idle')
      codeForm.resetFields()
      await loadStatus()
    } catch (e: any) {
      msg.error(e.message)
    } finally {
      setLoading(false)
    }
  }

  const handleDisableTotp = async () => {
    setLoading(true)
    try {
      await apiFetch('/auth/2fa/disable', { method: 'POST' })
      msg.success('双因素认证已关闭')
      await loadStatus()
    } catch (e: any) {
      msg.error(e.message)
    } finally {
      setLoading(false)
    }
  }

  return (
    <div style={{ display: 'flex', flexDirection: 'column', gap: 16 }}>
      <Card
        title="访问密码保护"
        extra={
          status?.has_password
            ? <Tag color="green"><CheckCircleOutlined /> 已启用</Tag>
            : <Tag color="default"><CloseCircleOutlined /> 未启用</Tag>
        }
      >
        {!status?.has_password ? (
          <Space direction="vertical" style={{ width: '100%' }}>
            <Typography.Text type="secondary">
              启用后，访问页面需要输入密码。默认不开启，任何能访问此地址的人均可使用。
            </Typography.Text>
            <Form form={enableForm} layout="vertical" onFinish={handleEnable} requiredMark={false} style={{ maxWidth: 360, marginTop: 8 }}>
              <Form.Item name="password" label="设置访问密码" rules={[{ required: true, message: '请输入密码' }, { min: 6, message: '至少 6 位' }]}>
                <Input.Password placeholder="至少 6 位" />
              </Form.Item>
              <Form.Item name="confirm" label="确认密码" rules={[{ required: true, message: '请再次输入' }]}>
                <Input.Password placeholder="再次输入密码" />
              </Form.Item>
              <Form.Item style={{ marginBottom: 0 }}>
                <Button type="primary" htmlType="submit" loading={loading} icon={<LockOutlined />}>
                  启用密码保护
                </Button>
              </Form.Item>
            </Form>
          </Space>
        ) : (
          <Space direction="vertical" style={{ width: '100%' }}>
            <Typography.Text type="secondary">当前已启用密码保护，关闭后任何人无需密码即可访问。</Typography.Text>
            <Button danger loading={loading} onClick={handleDisableAuth}>
              关闭密码保护
            </Button>
          </Space>
        )}
      </Card>

      {status?.has_password && (
        <>
          <Card title="修改密码">
            <Form form={pwForm} layout="vertical" onFinish={handleChangePassword} requiredMark={false} style={{ maxWidth: 360 }}>
              <Form.Item name="current_password" label="当前密码" rules={[{ required: true, message: '请输入当前密码' }]}>
                <Input.Password placeholder="当前密码" />
              </Form.Item>
              <Form.Item name="new_password" label="新密码" rules={[{ required: true, message: '请输入新密码' }, { min: 6, message: '至少 6 位' }]}>
                <Input.Password placeholder="新密码（至少 6 位）" />
              </Form.Item>
              <Form.Item name="confirm" label="确认新密码" rules={[{ required: true, message: '请再次输入' }]}>
                <Input.Password placeholder="再次输入新密码" />
              </Form.Item>
              <Form.Item style={{ marginBottom: 0 }}>
                <Button type="primary" htmlType="submit" loading={loading} icon={<SaveOutlined />}>
                  更新密码
                </Button>
              </Form.Item>
            </Form>
          </Card>

          <Card
            title="双因素认证 (2FA)"
            extra={
              status?.has_totp
                ? <Tag color="green"><CheckCircleOutlined /> 已启用</Tag>
                : <Tag color="default"><CloseCircleOutlined /> 未启用</Tag>
            }
          >
            {status?.has_totp ? (
              <Space direction="vertical">
                <Typography.Text type="secondary">
                  登录时需输入 Google Authenticator / Authy 等 App 中的 6 位验证码。
                </Typography.Text>
                <Button danger loading={loading} onClick={handleDisableTotp}>
                  关闭双因素认证
                </Button>
              </Space>
            ) : totpSetupState === 'idle' ? (
              <Space direction="vertical">
                <Typography.Text type="secondary">
                  启用后，登录时除密码外还需输入验证器 App 中的 6 位验证码，大幅提升安全性。
                </Typography.Text>
                <Button type="primary" loading={loading} onClick={handleSetupTotp} icon={<SafetyOutlined />}>
                  开启双因素认证
                </Button>
              </Space>
            ) : (
              <Space direction="vertical" style={{ width: '100%' }}>
                <Typography.Text strong>1. 用验证器 App 扫描下方二维码</Typography.Text>
                <div style={{ display: 'flex', gap: 24, alignItems: 'flex-start', flexWrap: 'wrap' }}>
                  <QRCode value={totpUri} size={180} />
                  <div style={{ flex: 1 }}>
                    <Typography.Text type="secondary" style={{ fontSize: 12 }}>无法扫码？手动输入密钥：</Typography.Text>
                    <Typography.Paragraph copyable style={{ fontFamily: 'monospace', fontSize: 13, marginTop: 4 }}>
                      {totpSecret}
                    </Typography.Paragraph>
                  </div>
                </div>
                <Typography.Text strong>2. 输入 App 中显示的 6 位验证码以确认绑定</Typography.Text>
                <Form form={codeForm} layout="inline" onFinish={handleEnableTotp}>
                  <Form.Item name="code" rules={[{ required: true, message: '请输入验证码' }, { len: 6, message: '6 位数字' }]}>
                    <Input placeholder="000000" maxLength={6} style={{ width: 140, letterSpacing: 4, textAlign: 'center' }} />
                  </Form.Item>
                  <Form.Item>
                    <Button type="primary" htmlType="submit" loading={loading}>确认启用</Button>
                  </Form.Item>
                  <Form.Item>
                    <Button onClick={() => setTotpSetupState('idle')}>取消</Button>
                  </Form.Item>
                </Form>
              </Space>
            )}
          </Card>
        </>
      )}
    </div>
  )
}

export default function Settings() {
  const [form] = Form.useForm()
  const [saving, setSaving] = useState(false)
  const [saved, setSaved] = useState(false)
  const [activeTab, setActiveTab] = useState('register')
  const [chatgptPinnedSections, setChatgptPinnedSections] = useState<string[]>(loadChatgptPinnedSections)
  const selectedMailProvider = Form.useWatch('mail_provider', form) || 'luckmail'

  useEffect(() => {
    if (typeof window === 'undefined') return
    window.localStorage.setItem(CHATGPT_PINNED_SECTIONS_STORAGE_KEY, JSON.stringify(chatgptPinnedSections))
  }, [chatgptPinnedSections])

  useEffect(() => {
    apiFetch('/config').then((data) => {
      if (!data.mail_provider) {
        data.mail_provider = 'luckmail'
      }
      if (!data.applemail_base_url) {
        data.applemail_base_url = 'https://www.appleemail.top'
      }
      if (!data.applemail_pool_dir) {
        data.applemail_pool_dir = 'mail'
      }
      if (!data.applemail_mailboxes) {
        data.applemail_mailboxes = 'INBOX,Junk'
      }
      if (!data.gptmail_base_url) {
        data.gptmail_base_url = 'https://mail.chatgpt.org.uk'
      }
      if (!data.maliapi_base_url) {
        data.maliapi_base_url = 'https://maliapi.215.im/v1'
      }
      if (!data.luckmail_base_url) {
        data.luckmail_base_url = 'https://mails.luckyous.com/'
      }
      if (!data.contribution_server_url) {
        data.contribution_server_url = 'http://new.xem8k5.top:7317/'
      }
      if (!data.chatgpt_gopay_billing_llm_base_url) {
        data.chatgpt_gopay_billing_llm_base_url = 'https://api.666800.xyz'
      }
      if (!data.chatgpt_gopay_billing_llm_model) {
        data.chatgpt_gopay_billing_llm_model = 'gpt-5.4'
      }
      if (!data.chatgpt_gopay_billing_llm_wire_api) {
        data.chatgpt_gopay_billing_llm_wire_api = 'responses'
      }
      if (!data.chatgpt_gopay_billing_llm_country_strategy) {
        data.chatgpt_gopay_billing_llm_country_strategy = 'billing_country'
      }
      if (!data.chatgpt_gopay_billing_llm_fixed_country) {
        data.chatgpt_gopay_billing_llm_fixed_country = 'US'
      }
      if (!data.chatgpt_gopay_billing_llm_reasoning_effort) {
        data.chatgpt_gopay_billing_llm_reasoning_effort = 'xhigh'
      }
      if (!data.chatgpt_gopay_billing_llm_timeout_seconds) {
        data.chatgpt_gopay_billing_llm_timeout_seconds = 45
      }
      if (!data.chatgpt_gopay_billing_llm_prompt) {
        data.chatgpt_gopay_billing_llm_prompt = '生成一个真实可用的账单地址，地址在谷歌地图中能找到对应的位置。'
      }
      if (!data.chatgpt_access_token_only_zero_amount_stop_threshold) {
        data.chatgpt_access_token_only_zero_amount_stop_threshold = '1'
      }
      if (!data.chatgpt_access_token_only_checkout_country) {
        data.chatgpt_access_token_only_checkout_country = 'US'
      }
      if (!data.chatgpt_access_token_only_checkout_currency) {
        data.chatgpt_access_token_only_checkout_currency = 'USD'
      }
      if (!data.chatgpt_subscription_auth_capture_retry_delays_seconds) {
        data.chatgpt_subscription_auth_capture_retry_delays_seconds = '5,10'
      }
      if (!data.chatgpt_phone_verification_provider) {
        data.chatgpt_phone_verification_provider = 'smstome'
      }
      if (!data.local_phone_gateway_url) {
        data.local_phone_gateway_url = 'http://sms-gateway:8720'
      }
      if (!data.local_phone_gateway_service_alias) {
        data.local_phone_gateway_service_alias = 'chatgpt'
      }
      if (!data.local_phone_gateway_timeout_seconds) {
        data.local_phone_gateway_timeout_seconds = '180'
      }
      if (!data.local_phone_gateway_poll_interval_seconds) {
        data.local_phone_gateway_poll_interval_seconds = '5'
      }
      if (!data.local_phone_gateway_max_attempts) {
        data.local_phone_gateway_max_attempts = '3'
      }
      if (!data.cloudmail_timeout) {
        data.cloudmail_timeout = 30
      }
      if (!data.tempmail_api_url) {
        data.tempmail_api_url = 'http://127.0.0.1:18081'
      }
      if (!data.tempmail_api_key_header) {
        data.tempmail_api_key_header = 'Authorization'
      }
      if (!data.tempmail_mode) {
        data.tempmail_mode = 'fixed_domain'
      }
      if (!data.tempmail_wait_timeout_seconds) {
        data.tempmail_wait_timeout_seconds = 180
      }
      if (!data.tempmail_ttl_minutes) {
        data.tempmail_ttl_minutes = 30
      }
      if (!data.tempmail_reuse_window_minutes) {
        data.tempmail_reuse_window_minutes = 20
      }
      if (!data.tempmail_platform) {
        data.tempmail_platform = 'chatgpt'
      }
      if (!data.icloud_hme_auto_create_stock_limit) {
        data.icloud_hme_auto_create_stock_limit = '10'
      }
      if (!data.icloud_hme_auto_create_interval_min_minutes) {
        data.icloud_hme_auto_create_interval_min_minutes = '60'
      }
      if (!data.icloud_hme_auto_create_interval_max_minutes) {
        data.icloud_hme_auto_create_interval_max_minutes = '120'
      }
      if (!data.icloud_hme_auto_create_rate_limit_backoff_minutes) {
        data.icloud_hme_auto_create_rate_limit_backoff_minutes = '360'
      }
      data.tempmail_permanent = parseBooleanConfigValue(data.tempmail_permanent)
      data.icloud_hme_auto_create_enabled = parseBooleanConfigValue(data.icloud_hme_auto_create_enabled)
      data.proxy_pool_cooldown_enabled = data.proxy_pool_cooldown_enabled === '' ? true : parseBooleanConfigValue(data.proxy_pool_cooldown_enabled)
      data.cfworker_domains = parseStoredDomainList(data.cfworker_domains)
      data.cfworker_enabled_domains = parseStoredDomainList(data.cfworker_enabled_domains)
      data.cfworker_random_subdomain = parseBooleanConfigValue(data.cfworker_random_subdomain)
      data.contribution_enabled = parseBooleanConfigValue(data.contribution_enabled)
      data.chatgpt_enable_team_invite = parseBooleanConfigValue(data.chatgpt_enable_team_invite)
      data.chatgpt_team_invite_deferred_activation = parseBooleanConfigValue(data.chatgpt_team_invite_deferred_activation)
      data.chatgpt_capture_free_workspace = data.chatgpt_capture_free_workspace === '' ? true : parseBooleanConfigValue(data.chatgpt_capture_free_workspace)
      data.chatgpt_capture_business_workspace = data.chatgpt_capture_business_workspace === '' ? true : parseBooleanConfigValue(data.chatgpt_capture_business_workspace)
      data.chatgpt_gopay_billing_llm_enabled = data.chatgpt_gopay_billing_llm_enabled === '' ? true : parseBooleanConfigValue(data.chatgpt_gopay_billing_llm_enabled)
      data.chatgpt_access_token_only_checkout_amount_check_enabled =
        data.chatgpt_access_token_only_checkout_amount_check_enabled === ''
          ? true
          : parseBooleanConfigValue(data.chatgpt_access_token_only_checkout_amount_check_enabled)
      data.chatgpt_access_token_only_zero_amount_stop_enabled = parseBooleanConfigValue(
        data.chatgpt_access_token_only_zero_amount_stop_enabled,
      )
      data.chatgpt_resume_auth_allow_phone_verification = parseBooleanConfigValue(
        data.chatgpt_resume_auth_allow_phone_verification,
      )
      data.external_subscription_api_enabled = parseBooleanConfigValue(data.external_subscription_api_enabled)
      form.setFieldsValue(data)
    })
  }, [form])

  const save = async () => {
    setSaving(true)
    try {
      const values = form.getFieldsValue(true)
      const domains = normalizeDomainList(values.cfworker_domains)
      const enabledDomains = normalizeDomainList(values.cfworker_enabled_domains).filter((domain) => domains.includes(domain))

      if (domains.length > 0 && enabledDomains.length === 0) {
        setActiveTab('mailbox')
        message.error('CF Worker 至少需要启用一个域名')
        return
      }

      values.cfworker_domains = JSON.stringify(domains)
      values.cfworker_enabled_domains = JSON.stringify(enabledDomains)
      if (domains.length > 0) {
        values.cfworker_domain = ''
      }
      values.cfworker_random_subdomain = parseBooleanConfigValue(values.cfworker_random_subdomain)
      values.tempmail_permanent = parseBooleanConfigValue(values.tempmail_permanent)
      values.icloud_hme_auto_create_enabled = parseBooleanConfigValue(values.icloud_hme_auto_create_enabled)
      values.icloud_hme_auto_create_stock_limit = String(
        Math.max(1, Number.parseInt(String(values.icloud_hme_auto_create_stock_limit || '10'), 10) || 10),
      )
      const intervalMin = Math.max(
        1,
        Number.parseInt(String(values.icloud_hme_auto_create_interval_min_minutes || '60'), 10) || 60,
      )
      const intervalMax = Math.max(
        intervalMin,
        Number.parseInt(String(values.icloud_hme_auto_create_interval_max_minutes || '120'), 10) || 120,
      )
      values.icloud_hme_auto_create_interval_min_minutes = String(intervalMin)
      values.icloud_hme_auto_create_interval_max_minutes = String(intervalMax)
      values.icloud_hme_auto_create_rate_limit_backoff_minutes = String(
        Math.max(
          1,
          Number.parseInt(String(values.icloud_hme_auto_create_rate_limit_backoff_minutes || '360'), 10) || 360,
        ),
      )
      values.proxy_pool_cooldown_enabled = parseBooleanConfigValue(values.proxy_pool_cooldown_enabled)
      values.tempmail_mode = values.tempmail_mode || 'fixed_domain'
      values.contribution_enabled = parseBooleanConfigValue(values.contribution_enabled)
      values.chatgpt_enable_team_invite = parseBooleanConfigValue(values.chatgpt_enable_team_invite)
      values.chatgpt_team_invite_deferred_activation = parseBooleanConfigValue(values.chatgpt_team_invite_deferred_activation)
      values.chatgpt_capture_free_workspace = parseBooleanConfigValue(values.chatgpt_capture_free_workspace)
      values.chatgpt_capture_business_workspace = parseBooleanConfigValue(values.chatgpt_capture_business_workspace)
      values.chatgpt_gopay_billing_llm_enabled = parseBooleanConfigValue(values.chatgpt_gopay_billing_llm_enabled)
      values.chatgpt_access_token_only_checkout_amount_check_enabled = parseBooleanConfigValue(
        values.chatgpt_access_token_only_checkout_amount_check_enabled,
      )
      values.chatgpt_access_token_only_zero_amount_stop_enabled = parseBooleanConfigValue(
        values.chatgpt_access_token_only_zero_amount_stop_enabled,
      )
      values.chatgpt_resume_auth_allow_phone_verification = parseBooleanConfigValue(
        values.chatgpt_resume_auth_allow_phone_verification,
      )
      values.external_subscription_api_enabled = parseBooleanConfigValue(values.external_subscription_api_enabled)
      values.external_subscription_api_token = String(values.external_subscription_api_token || '').trim()
      values.chatgpt_access_token_only_zero_amount_stop_threshold = String(
        values.chatgpt_access_token_only_zero_amount_stop_threshold || '1',
      ).trim() || '1'
      values.chatgpt_access_token_only_checkout_country = String(
        values.chatgpt_access_token_only_checkout_country || 'US',
      ).trim().toUpperCase() || 'US'
      values.chatgpt_access_token_only_checkout_currency = String(
        values.chatgpt_access_token_only_checkout_currency || 'USD',
      ).trim().toUpperCase() || 'USD'
      values.chatgpt_subscription_auth_capture_retry_delays_seconds = String(
        values.chatgpt_subscription_auth_capture_retry_delays_seconds || '5,10',
      ).trim() || '5,10'
      values.chatgpt_phone_verification_provider = String(values.chatgpt_phone_verification_provider || 'smstome').trim() || 'smstome'
      values.local_phone_gateway_url = String(values.local_phone_gateway_url || 'http://sms-gateway:8720').trim() || 'http://sms-gateway:8720'
      values.local_phone_gateway_token = String(values.local_phone_gateway_token || '').trim()
      values.local_phone_gateway_service_alias = String(values.local_phone_gateway_service_alias || 'chatgpt').trim() || 'chatgpt'
      values.local_phone_gateway_timeout_seconds = String(values.local_phone_gateway_timeout_seconds || '180').trim() || '180'
      values.local_phone_gateway_poll_interval_seconds = String(values.local_phone_gateway_poll_interval_seconds || '5').trim() || '5'
      values.local_phone_gateway_max_attempts = String(values.local_phone_gateway_max_attempts || '3').trim() || '3'

      await apiFetch('/config', { method: 'PUT', body: JSON.stringify({ data: values }) })
      form.setFieldsValue({
        cfworker_domains: domains,
        cfworker_enabled_domains: enabledDomains,
        cfworker_domain: domains.length > 0 ? '' : values.cfworker_domain,
        cfworker_random_subdomain: values.cfworker_random_subdomain,
        tempmail_permanent: values.tempmail_permanent,
        icloud_hme_auto_create_enabled: values.icloud_hme_auto_create_enabled,
        icloud_hme_auto_create_stock_limit: values.icloud_hme_auto_create_stock_limit,
        icloud_hme_auto_create_interval_min_minutes: values.icloud_hme_auto_create_interval_min_minutes,
        icloud_hme_auto_create_interval_max_minutes: values.icloud_hme_auto_create_interval_max_minutes,
        icloud_hme_auto_create_rate_limit_backoff_minutes: values.icloud_hme_auto_create_rate_limit_backoff_minutes,
        contribution_enabled: values.contribution_enabled,
        chatgpt_enable_team_invite: values.chatgpt_enable_team_invite,
        chatgpt_team_invite_deferred_activation: values.chatgpt_team_invite_deferred_activation,
        chatgpt_capture_free_workspace: values.chatgpt_capture_free_workspace,
        chatgpt_capture_business_workspace: values.chatgpt_capture_business_workspace,
        chatgpt_gopay_billing_llm_enabled: values.chatgpt_gopay_billing_llm_enabled,
        chatgpt_access_token_only_checkout_amount_check_enabled: values.chatgpt_access_token_only_checkout_amount_check_enabled,
        chatgpt_access_token_only_checkout_country: values.chatgpt_access_token_only_checkout_country,
        chatgpt_access_token_only_checkout_currency: values.chatgpt_access_token_only_checkout_currency,
        chatgpt_access_token_only_zero_amount_stop_enabled: values.chatgpt_access_token_only_zero_amount_stop_enabled,
        chatgpt_access_token_only_zero_amount_stop_threshold: values.chatgpt_access_token_only_zero_amount_stop_threshold,
        chatgpt_resume_auth_allow_phone_verification: values.chatgpt_resume_auth_allow_phone_verification,
        chatgpt_subscription_auth_capture_retry_delays_seconds: values.chatgpt_subscription_auth_capture_retry_delays_seconds,
        chatgpt_phone_verification_provider: values.chatgpt_phone_verification_provider,
        local_phone_gateway_url: values.local_phone_gateway_url,
        local_phone_gateway_token: values.local_phone_gateway_token,
        local_phone_gateway_service_alias: values.local_phone_gateway_service_alias,
        local_phone_gateway_timeout_seconds: values.local_phone_gateway_timeout_seconds,
        local_phone_gateway_poll_interval_seconds: values.local_phone_gateway_poll_interval_seconds,
        local_phone_gateway_max_attempts: values.local_phone_gateway_max_attempts,
        external_subscription_api_enabled: values.external_subscription_api_enabled,
        external_subscription_api_token: values.external_subscription_api_token,
      })
      message.success('保存成功')
      setSaved(true)
      setTimeout(() => setSaved(false), 2000)
    } finally {
      setSaving(false)
    }
  }

  const currentTab = TAB_ITEMS.find((t) => t.key === activeTab) as TabConfig
  const visibleSections =
    activeTab === 'mailbox'
      ? currentTab.sections.filter((section) => {
          if (section.title === '默认邮箱服务') return true
          const provider = getMailboxSectionProvider(section.title)
          if (!provider) return false
          return provider === selectedMailProvider
        })
      : currentTab.sections
  const normalizedChatgptPinnedSections =
    activeTab === 'chatgpt' ? normalizePinnedSections(chatgptPinnedSections, visibleSections) : []
  const orderedVisibleSections =
    activeTab === 'chatgpt' ? orderPinnedSections(visibleSections, normalizedChatgptPinnedSections) : visibleSections
  const toggleChatgptPinnedSection = (sectionTitle: string, checked: boolean) => {
    setChatgptPinnedSections((prev) => {
      const withoutCurrent = prev.filter((title) => title !== sectionTitle)
      return checked ? [...withoutCurrent, sectionTitle] : withoutCurrent
    })
  }

  return (
    <div style={{ display: 'flex', flexDirection: 'column', gap: 16 }}>
      <div>
        <h1 style={{ fontSize: 24, fontWeight: 'bold', margin: 0 }}>全局配置</h1>
        <p style={{ color: '#7a8ba3', marginTop: 4 }}>配置将持久化保存，注册任务自动使用</p>
      </div>

      <div style={{ display: 'flex', gap: 24 }}>
        <div style={{ width: 200 }}>
          <Tabs
            tabPosition="left"
            activeKey={activeTab}
            onChange={setActiveTab}
            items={TAB_ITEMS.map((t) => ({
              key: t.key,
              label: (
                <span>
                  {t.icon}
                  <span style={{ marginLeft: 8 }}>{t.label}</span>
                </span>
              ),
            }))}
          />
        </div>

        <div style={{ flex: 1 }}>
          {activeTab === 'integrations' ? (
            <IntegrationsPanel />
          ) : activeTab === 'security' ? (
            <SecurityPanel />
          ) : (
            <Form form={form} layout="vertical">
              {activeTab === 'contribution' ? (
                <ContributionPanel form={form} onSave={save} saving={saving} saved={saved} />
              ) : (
                <>
                  {activeTab === 'captcha' ? <SolverStatus /> : null}
                  {activeTab === 'mailbox' && selectedMailProvider === 'manual_email_otp' ? (
                    <Alert
                      type="info"
                      showIcon
                      style={{ marginBottom: 16 }}
                      message="手动邮箱 + 手输验证码"
                      description="这是任务级模式：真正的邮箱地址不在全局设置里填写，而是在“注册任务”页面、选择 ChatGPT 后，在邮箱服务下拉里选它，再填写邮箱地址。任务跑到邮箱 OTP 时，会在任务状态区弹出验证码输入框。若你要走“已有账号抓 auth”，默认登录密码请到 ChatGPT 分组里的“已有账号抓 auth 默认密码”填写。"
                    />
                  ) : null}
                  {activeTab === 'chatgpt' ? (
                    <>
                      <Card size="small" style={{ marginBottom: 12 }}>
                        <Space direction="vertical" size={10} style={{ width: '100%' }}>
                          <Space size={8} wrap style={{ width: '100%', justifyContent: 'space-between' }}>
                            <Space size={8} wrap>
                              <Typography.Text strong>置顶面板</Typography.Text>
                              <Typography.Text type="secondary" style={{ fontSize: 12 }}>
                                勾选后会把对应 ChatGPT 面板移动到最上方
                              </Typography.Text>
                            </Space>
                            {normalizedChatgptPinnedSections.length > 0 ? (
                              <Button size="small" type="link" onClick={() => setChatgptPinnedSections([])}>
                                清空置顶
                              </Button>
                            ) : null}
                          </Space>
                          <Space size={[8, 8]} wrap>
                            {visibleSections.map((section) => {
                              const checked = normalizedChatgptPinnedSections.includes(section.title)
                              return (
                                <Tag.CheckableTag
                                  key={section.title}
                                  checked={checked}
                                  onChange={(nextChecked) => toggleChatgptPinnedSection(section.title, nextChecked)}
                                  style={{
                                    border: `1px solid ${checked ? '#91caff' : '#d9d9d9'}`,
                                    borderRadius: 999,
                                    padding: '4px 10px',
                                    marginInlineEnd: 0,
                                    background: checked ? '#e6f4ff' : '#fafafa',
                                    color: checked ? '#0958d9' : 'rgba(0, 0, 0, 0.65)',
                                    fontWeight: checked ? 600 : 500,
                                  }}
                                >
                                  {section.title}
                                </Tag.CheckableTag>
                              )
                            })}
                          </Space>
                        </Space>
                      </Card>
                      <Button
                        type="primary"
                        icon={<SaveOutlined />}
                        onClick={save}
                        loading={saving}
                        block
                        style={{ marginBottom: 16 }}
                      >
                        {saved ? '已保存 ✓' : '保存配置'}
                      </Button>
                    </>
                  ) : null}
                  {orderedVisibleSections.map((section) => (
                    <ConfigSection
                      key={`${activeTab}:${section.title}`}
                      section={section}
                      defaultCollapsed={activeTab === 'chatgpt'}
                      autoExpand={activeTab === 'chatgpt' && normalizedChatgptPinnedSections.includes(section.title)}
                    />
                  ))}
                  {activeTab === 'mailbox' && selectedMailProvider === 'icloud_hme' ? <ICloudHmeManagerSection form={form} /> : null}
                  {activeTab === 'mailbox' && selectedMailProvider === 'applemail' ? <AppleMailPoolImportSection form={form} /> : null}
                  {activeTab === 'mailbox' && selectedMailProvider === 'cfworker' ? <CFWorkerDomainPoolSection form={form} /> : null}
                  {activeTab === 'mailbox' && selectedMailProvider === 'outlook' ? <OutlookImportSection /> : null}
                  {activeTab !== 'chatgpt' ? (
                    <Button type="primary" icon={<SaveOutlined />} onClick={save} loading={saving} block>
                      {saved ? '已保存 ✓' : '保存配置'}
                    </Button>
                  ) : null}
                </>
              )}
            </Form>
          )}
        </div>
      </div>
    </div>
  )
}
