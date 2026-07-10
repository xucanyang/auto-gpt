import { useEffect, useState } from 'react'
import { App, Card, Form, Input, Select, Button, message, Tabs, Space, Tag, Typography, Modal, QRCode, Switch, Alert, Table, Grid } from 'antd'
import type { FormInstance } from 'antd'
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
import { buildChatGPTK12ConfigData } from '@/lib/chatgptK12Config'
import { apiFetch } from '@/lib/utils'

type ConfigShareState = {
  instance_id?: string
  enabled?: boolean
  mode?: string
  baseline_revision?: string
  detached_at?: string
  last_pull_at?: string
  shared?: {
    revision?: number
    updated_at?: string
    updated_by?: string
    keys?: number
    exists?: boolean
  }
  local_only_keys?: string[]
}

const SELECT_FIELDS: Record<string, { label: string; value: string }[]> = {
  mail_provider: [
    { label: 'LuckMail（订单接码 / 已购邮箱）', value: 'luckmail' },
    { label: '手动邮箱 + 手输验证码（仅 ChatGPT）', value: 'manual_email_otp' },
    { label: '邮箱验证码 API（email----api）', value: 'email_api' },
    { label: 'Outlook（本地导入）', value: 'outlook' },
    { label: 'AppleMail（小苹果 / 本地邮箱池）', value: 'applemail' },
    { label: 'Laoudo（固定邮箱）', value: 'laoudo' },
    { label: 'TempMail.lol（自动生成）', value: 'tempmail_lol' },
    { label: 'TempMail Ready API（本地接口）', value: 'tempmail_local' },
    { label: 'HME Ready API（iCloud Helper）', value: 'hme_ready_api' },
    { label: 'iCloud HME', value: 'icloud_hme' },
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
  task_proxy_mode: [
    { label: '动态代理（默认）', value: 'dynamic' },
    { label: '代理池自动选取', value: 'pool' },
    { label: '手动指定代理', value: 'specified' },
    { label: '直连（不使用代理）', value: 'direct' },
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
      {
        title: '账号网络默认出口',
        desc: '动态模式只使用“动态代理模板 + 动态代理出口国家”；指定代理和代理池字段仅在对应模式显示。单项任务显式传代理时仍可覆盖本次任务。',
        fields: [
          { key: 'task_proxy_mode', label: '默认出口模式', type: 'select' },
          { key: 'task_proxy_url', label: '指定代理地址', secret: true, placeholder: 'http:// 或 socks5://...' },
          { key: 'task_proxy_country_code', label: '候选出口国家', placeholder: 'JP（可留空）' },
          { key: 'task_proxy_failover', label: '失败后刷新 / 切换代理', type: 'boolean' },
          { key: 'task_proxy_max_candidates', label: '代理池候选数量', placeholder: '5' },
          { key: 'task_proxy_min_score', label: '代理池最低健康分', placeholder: '50' },
          { key: 'dynamic_proxy_template', label: '动态代理模板', secret: true, placeholder: 'socks5://user-region-Rand-sid-xxxx-t-5:pass@host:port' },
          { key: 'dynamic_proxy_default_country', label: '动态代理出口国家', placeholder: 'JP' },
          { key: 'dynamic_proxy_ip_retention_minutes', label: 'IP 保留分钟数（t-N）', placeholder: '5' },
          { key: 'dynamic_proxy_require_country_match', label: '要求实测国家匹配', type: 'boolean' },
          { key: 'dynamic_proxy_probe_enabled', label: '运行前探测出口', type: 'boolean' },
          { key: 'dynamic_proxy_probe_timeout_seconds', label: '探测超时秒数', placeholder: '8' },
        ],
      },
      {
        title: 'K12 / Workspace',
        desc: '注册阶段加入 K12 工作空间并保存 workspace variants',
        help: {
          title: 'K12 配置说明',
          lines: [
            'enabled 控制是否在注册链路启用 K12 join；workspace_ids 是目标 workspace_id 列表。',
            'save_all_spaces 开启后保存本次会话可见的所有空间 variants；关闭时只处理 workspace_ids。',
            'strict_join 开启后 join/capture 未达预期会让任务显式失败；关闭时只记录部分成功。',
            'capture_refresh_tokens 是后续精确 RT 捕获预留项；当前稳定链路先保存 AT-only workspace variants。',
          ],
        },
        fields: [
          { key: 'chatgpt_k12_enabled', label: '启用 K12', type: 'boolean' },
          { key: 'chatgpt_k12_workspace_ids', label: 'Workspace IDs', type: 'stringList', placeholder: 'ws_xxx，多个用回车/逗号分隔' },
          { key: 'chatgpt_k12_save_all_spaces', label: '保存所有空间 variants', type: 'boolean' },
          { key: 'chatgpt_k12_strict_join', label: '严格 join', type: 'boolean' },
          { key: 'chatgpt_k12_join_timeout_seconds', label: 'Join 超时秒数', placeholder: '60' },
          { key: 'chatgpt_k12_join_retry_count', label: 'Join 重试次数', placeholder: '2' },
          { key: 'chatgpt_k12_post_join_poll_seconds', label: 'Join 后轮询秒数', placeholder: '3,8,15' },
          { key: 'chatgpt_k12_capture_refresh_tokens', label: '抓取 Refresh Token variants（预留）', type: 'boolean', disabled: true },
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
          { key: 'mailbox_otp_timeout_seconds', label: '通用邮箱验证码等待秒数（非 ChatGPT 注册）', placeholder: '例如 60 / 90 / 120' },
        ],
      },
      {
        title: 'ChatGPT 注册验证码等待',
        desc: '只限制单账号在注册邮箱验证码阶段的等待预算，不限制整批任务总耗时。',
        fields: [
          { key: 'chatgpt_register_otp_wait_seconds', label: '单账号首轮等待秒数', placeholder: '120' },
          { key: 'chatgpt_register_otp_resend_wait_seconds', label: '单账号补发后等待秒数', placeholder: '90' },
          { key: 'chatgpt_register_otp_account_budget_seconds', label: '单账号验证码总预算秒数', placeholder: '210' },
        ],
      },
      {
        title: '邮箱验证码 API',
        desc: '每行 email----api。API 返回 JSON，status 字段为验证码；status=0/空/非 4-8 位数字表示未收到。Gmail 可展开为“原邮箱 + N-1 个随机变体”，默认规则包含 dot、plus、dot+plus 和 googlemail。',
        fields: [
          { key: 'email_api_lines', label: '邮箱 API 行', type: 'textarea', placeholder: 'name@gmail.com----api.example.com/get?id=xxx\nuser@example.com----https://api.example.com/code?u=2' },
          { key: 'email_api_poll_interval_seconds', label: '轮询间隔秒数', placeholder: '3' },
          { key: 'email_api_request_timeout_seconds', label: '单次请求超时秒数', placeholder: '15' },
          { key: 'email_api_gmail_dot_variant_enabled', label: '启用 Gmail 随机变体', type: 'boolean' },
          { key: 'email_api_gmail_variant_count', label: '每个 Gmail 总身份数', placeholder: '2' },
          { key: 'email_api_gmail_variant_rules', label: 'Gmail 变体规则', placeholder: 'all 或 dot,plus,dot_plus,googlemail' },
          { key: 'email_api_gmail_plus_tag_template', label: 'Plus 标签模板', placeholder: 'r{rand}' },
          { key: 'email_api_default_scheme', label: '默认 URL 协议', placeholder: 'https' },
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
        title: 'HME Ready API',
        desc: '通过 icloud-hide-email-helper 领取 HME、收码和 finalize',
        fields: [
          { key: 'icloud_forward_to', label: '转发目标邮箱', placeholder: 'b@666800.xyz', type: 'stringList' },
          { key: 'icloud_hme_helper_api_url', label: 'Helper API URL', placeholder: 'http://host.docker.internal:18765' },
          { key: 'icloud_hme_helper_internal_key', label: 'Helper Internal Key', secret: true },
          { key: 'icloud_hme_helper_api_key_header', label: 'Helper 鉴权 Header', placeholder: 'X-Internal-Key' },
          { key: 'icloud_hme_helper_consumer', label: 'Helper Consumer', placeholder: 'auto-gpt/chatgpt_register' },
          { key: 'icloud_hme_helper_checkout_ttl_seconds', label: 'Helper lease TTL 秒', placeholder: '10800' },
          { key: 'icloud_hme_helper_wait_timeout_seconds', label: 'Helper 等码超时秒', placeholder: '300' },
          { key: 'icloud_hme_helper_max_cache_age_seconds', label: 'Helper iCloud 缓存有效秒', placeholder: '86400' },
        ],
      },
      {
        title: 'iCloud HME 基础配置',
        desc: 'auto-gpt 直接管理 iCloud HME Cookie、别名来源和共享收件箱',
        fields: [
          { key: 'icloud_hme_mode', label: '别名来源模式', type: 'select' },
          { key: 'icloud_cookie', label: 'iCloud Cookie', type: 'textarea', placeholder: '从 www.icloud.com DevTools 请求头复制完整 Cookie 字符串' },
          { key: 'icloud_domain_base', label: 'iCloud 域', type: 'select' },
          { key: 'icloud_forward_to', label: '转发目标邮箱', placeholder: 'b@cccy.me', type: 'stringList' },
          { key: 'icloud_forward_mailbox_id', label: '转发目标 mailbox_id（可选）', placeholder: '0d355f68-8506-4c93-ac56-5ef017f0b932' },
        ],
      },
      {
        title: 'iCloud HME 自动补池',
        desc: '按随机间隔创建新 HME，并放入导入池待使用',
        fields: [
          { key: 'icloud_hme_auto_create_enabled', label: '自动创建导入池邮箱', type: 'boolean' },
          { key: 'icloud_hme_auto_create_stock_limit', label: '导入池库存上限', placeholder: '10' },
          { key: 'icloud_hme_auto_create_interval_min_minutes', label: '随机间隔最小分钟', placeholder: '60' },
          { key: 'icloud_hme_auto_create_interval_max_minutes', label: '随机间隔最大分钟', placeholder: '120' },
          { key: 'icloud_hme_auto_create_rate_limit_backoff_minutes', label: '遇到限流延长等待分钟', placeholder: '360' },
          { key: 'icloud_hme_auto_create_error_backoff_minutes', label: '普通错误短退避分钟', placeholder: '3' },
        ],
      },
      {
        title: 'iCloud HME 自动删除',
        desc: '扫描未使用或失效别名，删除前可再次测活确认',
        fields: [
          { key: 'icloud_hme_auto_delete_enabled', label: '自动删除未使用/失效别名', type: 'boolean' },
          { key: 'icloud_hme_auto_delete_recheck_before_delete', label: '删除前先失效测活', type: 'boolean' },
          { key: 'icloud_hme_auto_delete_account_interval_min_minutes', label: '账号间隔最小分钟', placeholder: '10' },
          { key: 'icloud_hme_auto_delete_account_interval_max_minutes', label: '账号间隔最大分钟', placeholder: '30' },
          { key: 'icloud_hme_auto_delete_max_per_run', label: '单次最多删除个数', placeholder: '20' },
          { key: 'icloud_hme_auto_delete_rate_limit_backoff_minutes', label: '删除遇限流延长等待分钟', placeholder: '60' },
          { key: 'icloud_hme_auto_delete_error_backoff_minutes', label: '普通错误短退避分钟', placeholder: '3' },
          { key: 'icloud_hme_auto_delete_pause_active_tasks', label: '活跃任务期间暂停删除', type: 'boolean' },
          { key: 'icloud_hme_auto_delete_dead_statuses', label: '判为失效的测活结论', placeholder: 'account_deactivated,password_invalid' },
        ],
      },
      {
        title: 'TempMail 归档清理',
        desc: '归档共享收件箱旧邮件，保护验证码任务窗口',
        fields: [
          { key: 'tempmail_archive_cleanup_enabled', label: '归档清理共享收件箱', type: 'boolean' },
          { key: 'tempmail_archive_cleanup_interval_minutes', label: '归档清理间隔分钟', placeholder: '30' },
          { key: 'tempmail_archive_cleanup_keep_recent_minutes', label: '保留最近邮件分钟', placeholder: '60' },
          { key: 'tempmail_archive_cleanup_threshold', label: '触发清理邮件数阈值', placeholder: '100' },
          { key: 'tempmail_archive_cleanup_pause_active_tasks', label: '活跃任务期间暂停清理', type: 'boolean' },
          { key: 'tempmail_archive_cleanup_mailbox', label: '归档清理邮箱', placeholder: 'b@cccy.me' },
          { key: 'tempmail_archive_cleanup_backup_path', label: '归档备份路径', placeholder: '/runtime/tempmail_email_backups.db' },
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
          { key: 'chatgpt_phone_signup_password', label: '手机号注册/登录固定密码', secret: true, placeholder: '新手机号注册和已注册手机号登录共用' },
          { key: 'chatgpt_existing_account_login_password', label: '已有账号抓 auth 默认密码', secret: true, placeholder: '可留空，任务里仍可临时覆盖' },
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
          { key: 'chatgpt_access_token_only_gopay_provider_link_enabled', label: '注册后获取 GoPay 平台链接', type: 'boolean' },
        ],
      },
      {
        title: '外部 ChatGPT 分发 API',
        desc: '订阅链接和 AccessToken 分开领取、分开鉴权。AccessToken 只发送 live 校验有效且未订阅的账号。',
        help: {
          title: '接口使用方法',
          lines: [
            '请求头统一使用 Authorization: Bearer <对应功能的访问 Token>。',
            '领取订阅链接: POST /api/external/subscription-links/claim，body 示例 {"consumer":"payment-worker-01","limit":10,"lease_seconds":900}。',
            '领取成功后默认 300 秒会触发一次本地账号订阅探测；服务重启后会自动恢复未完成的复核计划。',
            '查询领取状态: GET /api/external/subscription-links/{claim_id}。',
            '支付成功写回: POST /api/external/subscription-links/{claim_id}/result，body 示例 {"status":"paid","external_payment_id":"pay_123","message":"payment completed"}。',
            '支付失败写回: POST /api/external/subscription-links/{claim_id}/result，body 示例 {"status":"failed","external_payment_id":"pay_123","error_code":"declined","message":"payment failed"}。',
            '放弃本次领取: POST /api/external/subscription-links/{claim_id}/release，body 示例 {"reason":"checkout unavailable"}。',
            '领取 AccessToken: POST /api/external/access-tokens/claim，body 示例 {"consumer":"token-worker-01","limit":10,"lease_seconds":86400}。',
            'AccessToken 响应会返回 account_id、email、access_token、claim_id。本轮同一个 access_token 不会重复发送。',
            'AccessToken 支付成功写回: POST /api/external/access-tokens/{claim_id}/result，body 示例 {"status":"paid","external_payment_id":"pay_123","message":"payment completed"}，本地会立即刷新账号状态。',
            'AccessToken 支付失败写回: POST /api/external/access-tokens/{claim_id}/result，body 示例 {"status":"failed","external_payment_id":"pay_123","error_code":"declined","message":"payment failed"}，账号状态不变。',
            'AccessToken 查询和释放: GET /api/external/access-tokens/{claim_id}，POST /api/external/access-tokens/{claim_id}/release。',
            '接口不返回密码、refresh_token、cookie 或 session_token。',
          ],
        },
        fields: [
          { key: 'external_subscription_api_enabled', label: '启用订阅链接分发', type: 'boolean' },
          { key: 'external_subscription_api_token', label: '订阅链接 API Token', secret: true, placeholder: '支付程序使用 Authorization: Bearer <token>' },
          { key: 'external_subscription_verify_after_seconds', label: '订阅链接领取后本地探测延迟秒数', placeholder: '300' },
          { key: 'external_access_token_api_enabled', label: '启用 AccessToken 分发', type: 'boolean' },
          { key: 'external_access_token_api_token', label: 'AccessToken API Token', secret: true, placeholder: '外部服务使用 Authorization: Bearer <token>' },
          { key: 'external_access_token_allow_refresh', label: '允许 refresh_token 刷新后发送新 AT', type: 'boolean' },
          { key: 'external_access_token_default_lease_seconds', label: 'AccessToken 租约秒数', placeholder: '86400' },
          { key: 'external_access_token_max_limit', label: 'AccessToken 单次最大领取数', placeholder: '50' },
          { key: 'external_access_token_precheck_cooldown_seconds', label: 'AccessToken 预检失败冷却秒数', placeholder: '600' },
        ],
      },
      {
        title: 'OAIPay 面板',
        desc: '一键将账号推送到 OAIPay (gpt.cccy.me)',
        fields: [
          { key: 'oaipay_api_url', label: 'API URL', placeholder: 'http://gpt.cccy.me/api/auto-gpt/upload' },
          { key: 'oaipay_api_key', label: 'API Key / 上传密钥（gpt.cccy.me 的 UPLOAD_KEY）', secret: true },
          { key: 'oaipay_group', label: '默认分组', placeholder: '例如: auto-gpt' },
        ]
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
        desc: '补抓 Auth 遇到 add_phone 后，是否允许接码以及使用哪个接码渠道',
        fields: [
          { key: 'chatgpt_resume_auth_allow_phone_verification', label: '补抓 Auth 兼容手机号总开关', type: 'boolean' },
          { key: 'chatgpt_resume_auth_allow_add_phone_verification', label: '补抓 Auth 允许 add_phone 新绑', type: 'boolean' },
          { key: 'chatgpt_resume_auth_allow_existing_phone_verification', label: '补抓 Auth 允许已绑手机号二次验证', type: 'boolean' },
          { key: 'chatgpt_recheck_allow_existing_phone_verification', label: '测活允许已绑手机号二次验证', type: 'boolean' },
          { key: 'existing_phone_otp_timeout_seconds', label: '已绑手机号 OTP 等待秒数', placeholder: '180' },
          { key: 'existing_phone_otp_poll_interval_seconds', label: '已绑手机号 OTP 轮询间隔秒数', placeholder: '5' },
          { key: 'existing_phone_otp_max_resend_attempts', label: '已绑手机号 OTP 最大重发次数', placeholder: '1' },
          { key: 'existing_phone_otp_resend_interval_seconds', label: '已绑手机号 OTP 重发间隔秒数', placeholder: '30' },
          { key: 'chatgpt_subscription_auth_capture_retry_delays_seconds', label: '补抓 Auth 重试间隔（秒）', placeholder: '5,10' },
          { key: 'chatgpt_phone_verification_provider', label: '接码服务', type: 'select' },
          { key: 'local_phone_gateway_url', label: '本地网关 URL', placeholder: 'http://sms-gateway:8720' },
          { key: 'local_phone_gateway_token', label: '本地网关 Token', secret: true },
          { key: 'local_phone_gateway_service_alias', label: '本地网关服务别名', placeholder: 'chatgpt' },
          { key: 'local_phone_gateway_auto_acquire_enabled', label: '允许自动取号', type: 'boolean' },
          { key: 'local_phone_gateway_timeout_seconds', label: '本地网关等待秒数', placeholder: '180' },
          { key: 'local_phone_gateway_poll_interval_seconds', label: '本地网关轮询间隔秒数', placeholder: '5' },
          { key: 'local_phone_gateway_max_attempts', label: '本地网关换号次数', placeholder: '3' },
          { key: 'local_phone_gateway_max_resend_attempts', label: '同号最大重发次数', placeholder: '20' },
          { key: 'local_phone_gateway_resend_interval_seconds', label: '同号重发间隔秒数', placeholder: '30' },
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
  type?: 'select' | 'input' | 'boolean' | 'textarea' | 'stringList'
  secret?: boolean
  disabled?: boolean
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

const TASK_PROXY_SECTION_TITLE = '账号网络默认出口'

function taskProxyFieldsForMode(
  fields: FieldConfig[],
  rawMode: unknown,
  failoverEnabled: boolean,
): FieldConfig[] {
  const mode = String(rawMode || 'dynamic').trim().toLowerCase()
  const byKey = new Map(fields.map((field) => [field.key, field]))
  const pick = (...keys: string[]) => keys
    .map((key) => byKey.get(key))
    .filter((field): field is FieldConfig => Boolean(field))

  if (mode === 'specified') {
    const result = pick('task_proxy_mode', 'task_proxy_url', 'task_proxy_failover')
    const failover = byKey.get('task_proxy_failover')
    if (failover) {
      result[result.findIndex((field) => field.key === failover.key)] = {
        ...failover,
        label: '失败后切换代理池',
      }
    }
    return failoverEnabled
      ? [...result, ...pick('task_proxy_country_code', 'task_proxy_max_candidates', 'task_proxy_min_score')]
      : result
  }

  if (mode === 'pool') {
    return pick('task_proxy_mode', 'task_proxy_country_code', 'task_proxy_max_candidates', 'task_proxy_min_score')
  }

  if (mode === 'direct') {
    return pick('task_proxy_mode')
  }

  const result = pick(
    'task_proxy_mode',
    'dynamic_proxy_template',
    'dynamic_proxy_default_country',
    'task_proxy_failover',
    'dynamic_proxy_ip_retention_minutes',
    'dynamic_proxy_require_country_match',
    'dynamic_proxy_probe_enabled',
    'dynamic_proxy_probe_timeout_seconds',
  )
  const failover = byKey.get('task_proxy_failover')
  if (failover) {
    result[result.findIndex((field) => field.key === failover.key)] = {
      ...failover,
      label: '失败后刷新 SID 重试',
    }
  }
  return result
}

const CHATGPT_PINNED_SECTIONS_STORAGE_KEY = 'any-auto-register.settings.chatgpt.pinned-sections'

const CHATGPT_PIN_GROUPS = [
  {
    label: '上传',
    titles: ['CPA 面板', 'Sub2API 面板', 'OAIPay 面板', 'CodexProxy'],
  },
  {
    label: '账号订阅',
    titles: ['Business / 工作空间', 'GoPay 账单地址 LLM', '无 RT / Access Token Only', '外部 ChatGPT 分发 API'],
  },
  {
    label: '维护验证',
    titles: ['CPA 自动维护', '手机验证 / 接码服务'],
  },
]

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

function getIcloudHmeModeLabel(mode: string): string {
  return SELECT_FIELDS.icloud_hme_mode.find((item) => item.value === mode)?.label || mode || '未配置'
}

function getMailboxSectionProvider(title: string): string | null {
  switch (title) {
    case '邮箱验证码 API':
      return 'email_api'
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
    case 'HME Ready API':
      return 'hme_ready_api'
    case 'iCloud HME 基础配置':
    case 'iCloud HME 自动补池':
    case 'iCloud HME 自动删除':
    case 'TempMail 归档清理':
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

const MAILBOX_QUICK_PROVIDERS = [
  'luckmail',
  'hme_ready_api',
  'icloud_hme',
  'email_api',
  'applemail',
  'tempmail_local',
  'skymail',
  'cloudmail',
  'cfworker',
  'manual_email_otp',
]

function getMailboxProviderLabel(provider: string): string {
  return SELECT_FIELDS.mail_provider.find((item) => item.value === provider)?.label || provider
}

function getMailboxProviderBrief(provider: string): string {
  switch (provider) {
    case 'luckmail':
      return '购买邮箱 / 已购邮箱，适合稳定批量注册。'
    case 'hme_ready_api':
      return 'auto-gpt 只调用 helper 的 prepare / wait-code / finalize；iCloud Cookie、alias 池和收件箱都留在 helper。'
    case 'icloud_hme':
      return 'auto-gpt 直接管理 iCloud Cookie / HME 别名池 / 共享收件箱，适合导入池、实时创建和别名治理。'
    case 'email_api':
      return '自带邮箱 + 外部 API 自动收码；Gmail 一行可展开为原邮箱 + N-1 个随机变体，共用同一 API。'
    case 'applemail':
      return '本地导入 Outlook/AppleMail 池，适合已有邮箱资产。'
    case 'tempmail_local':
      return '自建 TempMail Ready API，适合固定域名和随机子域。'
    case 'skymail':
      return 'CloudMail 兼容接口，适合 API 建箱和收件。'
    case 'cloudmail':
      return 'CloudMail genToken 口令模式，适合已有管理端。'
    case 'cfworker':
      return '自建 CF Worker 邮箱，适合多域名池。'
    case 'manual_email_otp':
      return '任务内填写邮箱并手输验证码，不走自动取件。'
    default:
      return '当前邮箱服务只展示相关配置，避免无关表单干扰。'
  }
}

function applyMailboxQuickProvider(form: FormInstance, provider: string) {
  if (provider === 'hme_ready_api') {
    form.setFieldsValue({
      mail_provider: 'hme_ready_api',
      icloud_hme_mode: 'helper_ready_api',
    })
    return
  }
  if (provider === 'icloud_hme') {
    const currentMode = form.getFieldValue('icloud_hme_mode')
    form.setFieldsValue({
      mail_provider: 'icloud_hme',
      icloud_hme_mode: currentMode === 'helper_ready_api' ? 'import_pool' : (currentMode || 'import_pool'),
    })
    return
  }
  form.setFieldValue('mail_provider', provider)
}

function orderMailboxSections(sections: SectionConfig[], selectedProvider: string): SectionConfig[] {
  const defaultSection = sections.find((section) => section.title === '默认邮箱服务')
  const selectedProviderSections = sections.filter((section) => {
    const provider = getMailboxSectionProvider(section.title)
    return provider && provider === selectedProvider
  })
  const picked = new Set<string>()
  const ordered = [defaultSection, ...selectedProviderSections].filter((section): section is SectionConfig => {
    if (!section || picked.has(section.title)) return false
    picked.add(section.title)
    return true
  })
  return [...ordered, ...sections.filter((section) => !picked.has(section.title))]
}

function MailboxOverviewPanel({
  form,
  selectedProvider,
  visibleSections,
}: {
  form: FormInstance
  selectedProvider: string
  visibleSections: SectionConfig[]
}) {
  const otpTimeout = Form.useWatch('mailbox_otp_timeout_seconds', form) || '90'
  const icloudMode = Form.useWatch('icloud_hme_mode', form) || 'live'
  const forwardTo = Form.useWatch('icloud_forward_to', form) || '-'
  const helperApiUrl = Form.useWatch('icloud_hme_helper_api_url', form) || '-'
  const providerLabel = getMailboxProviderLabel(selectedProvider)
  const providerBrief = getMailboxProviderBrief(selectedProvider)
  const configPanelCount = visibleSections.filter((section) => section.title !== '默认邮箱服务').length

  return (
    <Card size="small" style={{ marginBottom: 12 }}>
      <Space direction="vertical" size={12} style={{ width: '100%' }}>
        <div style={{ display: 'flex', justifyContent: 'space-between', gap: 12, flexWrap: 'wrap' }}>
          <div>
            <Typography.Text strong>邮箱管理概览</Typography.Text>
            <Typography.Text type="secondary" style={{ display: 'block', marginTop: 4 }}>
              只展示当前邮箱服务相关配置；HME Ready API 和 iCloud HME 是两个独立方式。
            </Typography.Text>
          </div>
          <Space size={6} wrap>
            <Tag color="blue">当前：{providerLabel}</Tag>
            {selectedProvider === 'hme_ready_api' ? <Tag color="green">Helper Ready</Tag> : null}
            <Tag>通用 OTP 等待 {otpTimeout} 秒</Tag>
            <Tag>配置面板 {configPanelCount}</Tag>
          </Space>
        </div>

        <div
          style={{
            padding: '10px 12px',
            borderRadius: 10,
            border: '1px solid rgba(99, 102, 241, 0.22)',
            background: 'rgba(99, 102, 241, 0.08)',
          }}
        >
          <Typography.Text>{providerBrief}</Typography.Text>
          {selectedProvider === 'hme_ready_api' ? (
            <Typography.Text type="secondary" style={{ display: 'block', marginTop: 4 }}>
              Helper：{String(helperApiUrl)}，转发目标：{String(forwardTo)}
            </Typography.Text>
          ) : null}
          {selectedProvider === 'icloud_hme' ? (
            <Typography.Text type="secondary" style={{ display: 'block', marginTop: 4 }}>
              当前 HME 模式：{getIcloudHmeModeLabel(String(icloudMode))}，转发目标：{String(forwardTo)}
            </Typography.Text>
          ) : null}
        </div>

        <Space size={[8, 8]} wrap>
          {MAILBOX_QUICK_PROVIDERS.map((provider) => {
            const checked = provider === selectedProvider
            return (
              <Tag.CheckableTag
                key={provider}
                checked={checked}
                onChange={() => applyMailboxQuickProvider(form, provider)}
                style={{
                  border: `1px solid ${checked ? '#91caff' : 'rgba(122, 139, 163, 0.28)'}`,
                  borderRadius: 999,
                  padding: '4px 10px',
                  marginInlineEnd: 0,
                  background: checked ? 'rgba(99, 102, 241, 0.14)' : 'transparent',
                  fontWeight: checked ? 600 : 500,
                }}
              >
                {getMailboxProviderLabel(provider).split('（')[0]}
              </Tag.CheckableTag>
            )
          })}
        </Space>
      </Space>
    </Card>
  )
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
  error_backoff_minutes?: number
  next_run_at?: string
  seconds_until_next_run?: number
  rate_limit_until?: string
  in_rate_limit_backoff?: boolean
  error_backoff_until?: string
  in_error_backoff?: boolean
  consecutive_error_count?: number
  last_backoff_reason?: string
  last_run_at?: string
  last_success_at?: string
  last_created_hme?: string
  last_error?: string
  forward_to?: string
}

interface ICloudHmeAutoDeleteStatus {
  running?: boolean
  enabled?: boolean
  account_interval_min_minutes?: number
  account_interval_max_minutes?: number
  max_per_run?: number
  rate_limit_backoff_minutes?: number
  error_backoff_minutes?: number
  recheck_before_delete?: boolean
  pause_active_tasks?: boolean
  dead_statuses?: string[]
  pending_candidates?: number
  candidate_summary?: Record<string, number>
  next_run_at?: string
  seconds_until_next_run?: number
  rate_limit_until?: string
  in_rate_limit_backoff?: boolean
  error_backoff_until?: string
  in_error_backoff?: boolean
  consecutive_error_count?: number
  last_backoff_reason?: string
  last_run_at?: string
  last_success_at?: string
  last_error?: string
  last_result?: Record<string, any>
}


interface ICloudHmeRecheckCampaignData {
  campaign_id?: string
  summary?: Record<string, any>
  data?: any[]
  total?: number
  page?: number
  size?: number
  pages?: number
}

interface TempMailArchiveCleanupStatus {
  running?: boolean
  enabled?: boolean
  mailbox?: string
  backup_path?: string
  interval_minutes?: number
  keep_recent_minutes?: number
  threshold?: number
  pause_active_tasks?: boolean
  active_task_count?: number
  next_run_at?: string
  seconds_until_next_run?: number
  last_run_at?: string
  last_success_at?: string
  last_error?: string
  last_result?: Record<string, any>
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

function StringListInput({ value, onChange, placeholder }: { value?: string, onChange?: (val: string) => void, placeholder?: string }) {
  const arr = (value || '').split(',').map(e => e.trim()).filter(Boolean)
  const handleChange = (newVals: string[]) => {
    onChange?.(newVals.join(','))
  }
  return (
    <Select
      mode="tags"
      value={arr}
      onChange={handleChange}
      placeholder={placeholder || '输入邮箱后按回车'}
      style={{ width: '100%' }}
      open={false}
      tokenSeparators={[',', ' ', '\n']}
    />
  )
}

function ConfigField({ field }: { field: FieldConfig }) {
  const [showSecret, setShowSecret] = useState(false)
  const options = SELECT_FIELDS[field.key]
  const isBooleanField = field.type === 'boolean'
  const helpText =
    field.key === 'task_proxy_mode'
      ? '默认用于单账号状态刷新、Codex 额度刷新、自动状态刷新、订阅链接、上传前套餐探测等 ChatGPT/OpenAI 账号网络动作；如需直连可在这里改为直连。'
      : field.key === 'task_proxy_url'
        ? '仅用于“手动指定代理”模式；动态代理模式不会读取本项。'
      : field.key === 'task_proxy_country_code'
        ? '仅用于代理池筛选，或“手动指定代理 + 失败切换代理池”；动态代理模式不会读取本项。'
      : field.key === 'task_proxy_failover'
        ? '动态模式开启后会刷新 sid 生成下一个出口；手动指定代理开启后会回退到代理池候选。'
      : field.key === 'task_proxy_max_candidates'
        ? '代理池或指定代理 failover 时最多尝试的候选数量。'
      : field.key === 'task_proxy_min_score'
        ? '代理池候选的最低健康分；低于此分数不会被默认账号网络动作选中。'
      : field.key === 'dynamic_proxy_template'
      ? '动态模式唯一的全局模板。支持 region-JP/region-US 等固定国家，也支持 Cliproxy 生成的 region-Rand；任务会按本项出口国家改写完整 region token，再刷新 sid；展示和日志只保存脱敏地址。'
      : field.key === 'dynamic_proxy_default_country'
        ? '动态模式唯一的默认出口国家。任务未填写出口国家时使用两位 ISO 国家码，例如 JP、US、SG。'
      : field.key === 'dynamic_proxy_ip_retention_minutes'
        ? '覆盖 Cliproxy 用户名里的 t-N 字段，例如填 5 会生成 t-5；模板没有 t-N 但包含 sid 时会自动补到 sid 后。范围 1-1440 分钟。'
      : field.key === 'dynamic_proxy_require_country_match'
        ? '开启后，动态代理实测出口国家与声明国家不一致会直接失败；若 Cliproxy 模板 region 已匹配但 GeoIP 临时不可用，会记录未实测而不误杀候选。'
      : field.key === 'dynamic_proxy_probe_enabled'
        ? '开启后任务生成动态代理候选时先探测出口 IP/国家；关闭后只做模板改写和 sid 刷新。'
      : field.key === 'dynamic_proxy_probe_timeout_seconds'
        ? '动态代理出口探测超时，建议 6-12 秒。'
      : field.key === 'chatgpt_k12_workspace_ids'
        ? '多个 workspace_id 用逗号、空格或回车分隔；启用 K12 且未保存所有空间时必填。'
      : field.key === 'chatgpt_k12_save_all_spaces'
        ? '开启后保存本次 OAuth/session 可见的所有 workspace variants，而不只处理指定 workspace_id。'
      : field.key === 'chatgpt_k12_strict_join'
        ? '开启后 K12 join 或指定 workspace capture 未达预期会让任务显式失败；关闭时允许部分成功并保存可用 variants。'
      : field.key === 'chatgpt_k12_join_timeout_seconds'
        ? 'K12 join 请求超时，默认 60 秒。'
      : field.key === 'chatgpt_k12_join_retry_count'
        ? 'K12 join / capture 失败后的重试次数；填 0 表示不重试。'
      : field.key === 'chatgpt_k12_post_join_poll_seconds'
        ? 'Join 后重新读取 workspace 列表的等待秒数，支持逗号分隔，例如 3,8,15。'
      : field.key === 'chatgpt_k12_capture_refresh_tokens'
        ? '预留开关。当前版本不为每个 K12 workspace 强制重新登录抓 RT，先稳定保存 AT-only variants。'
      : field.key === 'default_executor'
      ? '当前仅对 ChatGPT 生效；支持纯协议、无头浏览器和有头浏览器模式。'
      : field.key === 'icloud_cookie'
        ? '从浏览器打开 www.icloud.com，进入 DevTools，找到发往 setup.icloud.com 或 /hme/ 的请求，把完整 Cookie 请求头原样复制到这里。不要删任何字段。'
      : field.key === 'icloud_hme_mode'
        ? '实时创建会直接调用 Apple 私有接口；仅导入池会只从本地已导入的 HME 别名池领取；优先导入池会先领池里的别名，没货再实时创建。HME Ready API 已拆成独立邮箱方式。'
      : field.key === 'icloud_hme_helper_api_url'
        ? 'auto-gpt 容器访问 helper 的内网地址；本机 Docker 推荐 http://host.docker.internal:18765 或宿主 Docker 网关地址。'
      : field.key === 'icloud_hme_helper_internal_key'
        ? '读取 helper 项目 .internal-api-key；只用于 auto-gpt 调用本地 Helper Ready API。'
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
      : field.key === 'icloud_hme_auto_create_error_backoff_minutes'
        ? '普通网络/API/解析错误会先短暂停再重试；连续普通错误会按该值递增，最多 15 分钟。填 0 可关闭普通错误短退避。'
      : field.key === 'icloud_hme_auto_delete_enabled'
        ? '开启后后台按随机间隔扫描候选（不在任何 ChatGPT 账号里的孤儿别名 + 绑定账号已失效的别名）。每个候选删除前都会先免密登录测活：能登录的视为存活、保留并重新导入账号列表，只删确认失效的。永不删待用库存与正在注册的别名。删除在 Apple 端 deactivate+delete，不可恢复。'
      : field.key === 'icloud_hme_auto_delete_recheck_before_delete'
        ? '开启（强烈推荐）：每个待删邮箱删除前都先免密登录测活——能登录=账号还活着→保留并重新导入；提示账号不存在/已停用=确认失效→才删；网络/限流等临时失败→本轮跳过。关闭则不测活、直接信任本地状态删除（快但激进，可能误删本地没记录但其实还活着的账号）。'
      : field.key === 'icloud_hme_auto_delete_account_interval_min_minutes'
        ? '每个候选邮箱/账号处理完成后，会在最小和最大分钟之间随机等待，再处理下一个候选。'
      : field.key === 'icloud_hme_auto_delete_account_interval_max_minutes'
        ? '必须大于或等于最小分钟；例如 10-30 表示每个账号之间随机等待 10 到 30 分钟。'
      : field.key === 'icloud_hme_auto_delete_max_per_run'
        ? '每轮最多删除的别名数量，避免一次删太多触发风控；超出的留到下一轮。'
      : field.key === 'icloud_hme_auto_delete_rate_limit_backoff_minutes'
        ? '如果删除时 Apple 返回限流，后台会至少等待这么久后再继续。'
      : field.key === 'icloud_hme_auto_delete_error_backoff_minutes'
        ? '删除或删前测活遇到普通临时错误时短暂停，不当作限流；连续普通错误会按该值递增，最多 15 分钟。填 0 可关闭。'
      : field.key === 'icloud_hme_auto_delete_pause_active_tasks'
        ? '开启后，有注册等任务在跑时本轮删除会自动跳过，避免误删正在使用的别名。'
      : field.key === 'icloud_hme_auto_delete_dead_statuses'
        ? '逗号分隔；只有失效测活结论命中这些代码时才会删除绑定的别名。默认 account_deactivated（账号被删/停用）、password_invalid（密码失效）。network_failed 等临时失败不会被删。'
      : field.key === 'chatgpt_access_token_only_gopay_provider_link_enabled'
        ? '开启后，无 RT 注册/登录成功并生成 Plus checkout 后，会继续走到 GoPay/Midtrans 平台链接阶段并把链接保存到账号 extra；失败不会让已注册账号丢失。'
      : field.key === 'tempmail_archive_cleanup_enabled'
        ? '开启后后台会定时扫描共享 TempMail 收件箱，先写入本地备份库，再删除超过保留窗口的旧邮件。'
      : field.key === 'tempmail_archive_cleanup_interval_minutes'
        ? '后台定时检查的间隔；只有邮件数达到阈值时才会自动清理。'
      : field.key === 'tempmail_archive_cleanup_keep_recent_minutes'
        ? '这个时间窗口内的邮件不会删除，用来保护正在等待验证码的任务。'
      : field.key === 'tempmail_archive_cleanup_threshold'
        ? '收件箱邮件数达到该数量才触发自动清理；手动执行会绕过这个阈值。'
      : field.key === 'tempmail_archive_cleanup_pause_active_tasks'
        ? '开启后只要后台还有注册、补抓、手机号绑定等活跃任务，归档清理会先跳过。'
      : field.key === 'tempmail_archive_cleanup_mailbox'
        ? '通常填写 iCloud HME 的转发目标邮箱；留空时后端默认使用当前转发目标。'
      : field.key === 'tempmail_archive_cleanup_backup_path'
        ? 'SQLite 备份库路径；容器内推荐 /runtime/tempmail_email_backups.db，可随运行数据持久化。'
      : field.key === 'chatgpt_resume_auth_allow_phone_verification'
        ? '兼容旧任务参数；新逻辑优先看下面两个补抓开关。旧开关开启时，未单独配置 add_phone 开关会继承为允许。'
      : field.key === 'chatgpt_resume_auth_allow_add_phone_verification'
        ? '控制补抓 Auth 遇到 add_phone/未接码账号时，是否允许从接码服务取新号并绑定。关闭时不会消耗新手机号。'
      : field.key === 'chatgpt_resume_auth_allow_existing_phone_verification'
        ? '控制补抓 Auth 遇到已绑定手机号二次验证时，是否读取完整手机号并到手机号池精确匹配后自动接码；日志会展示手机号。'
      : field.key === 'chatgpt_recheck_allow_existing_phone_verification'
        ? '控制邮箱测活/失效测活遇到已绑手机号二次验证时，是否用手机号池里的同一完整号码自动收码。'
      : field.key === 'existing_phone_otp_timeout_seconds'
        ? '已绑定手机号二次验证专用等待时间；完整号码必须命中手机号池且有 API URL 才会进入等待。'
      : field.key === 'existing_phone_otp_max_resend_attempts'
        ? '已绑定手机号二次验证未收到短信时触发 OpenAI 重发的次数；不同于 add_phone 新绑的同号重发配置。'
      : field.key === 'chatgpt_subscription_auth_capture_retry_delays_seconds'
        ? '用英文逗号分隔，例如 5,10；遇到 add_phone 或临时认证错误时按这些间隔重试。'
      : field.key === 'chatgpt_phone_verification_provider'
        ? '选择补抓 Auth add_phone 阶段使用的接码来源；本地接码网关会把 SMSBower 等平台隔离到独立项目里。'
      : field.key === 'local_phone_gateway_url'
        ? '主容器内访问独立接码网关的地址；Docker 网络内推荐 http://sms-gateway:8720。'
      : field.key === 'local_phone_gateway_token'
        ? '独立接码网关的 Bearer Token，只保存在主项目配置中，不会展示明文。'
      : field.key === 'local_phone_gateway_auto_acquire_enabled'
        ? '开启时先领取短信网关面板待用池号码，待用池为空会按网关配置自动新取号；关闭时只用待用池，避免自动创建订单。'
      : field.key === 'local_phone_gateway_max_resend_attempts'
        ? '同一个手机号未收到验证码时，继续触发 OpenAI 重发和网关下一条短信的最大次数；用于把一个号码用到无法发送为止。'
      : field.key === 'local_phone_gateway_resend_interval_seconds'
        ? '同一个手机号每次触发 OpenAI resend 前的等待时间，避免短时间连续重发。'
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
        <Switch checkedChildren="开启" unCheckedChildren="关闭" disabled={field.disabled} />
      ) : field.secret ? (
        <Input.Password
          placeholder={field.placeholder}
          visibilityToggle={{
            visible: !showSecret,
            onVisibleChange: setShowSecret,
          }}
          iconRender={(visible) => (visible ? <EyeOutlined /> : <EyeInvisibleOutlined />)}
        />
      ) : field.type === 'stringList' ? (
        <StringListInput placeholder={field.placeholder} />
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
  fields,
  defaultCollapsed = false,
  autoExpand = false,
}: {
  section: SectionConfig
  fields?: FieldConfig[]
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
          {(fields || section.fields).map((field) => (
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
              <div>CPA / CodexProxy / Sub2API / OAIPay 自动上传会被停用，避免重复上报。</div>
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
  const [syncPruning, setSyncPruning] = useState(false)
  const [switchingMode, setSwitchingMode] = useState(false)
  const [togglingId, setTogglingId] = useState('')
  const [aliases, setAliases] = useState<any[]>([])
  const [total, setTotal] = useState(0)
  const [availableImportPoolCount, setAvailableImportPoolCount] = useState(0)
  const [autoPoolStatus, setAutoPoolStatus] = useState<ICloudHmeAutoPoolStatus | null>(null)
  const [autoPoolStatusLoading, setAutoPoolStatusLoading] = useState(false)
  const [archiveStatus, setArchiveStatus] = useState<TempMailArchiveCleanupStatus | null>(null)
  const [archiveStatusLoading, setArchiveStatusLoading] = useState(false)
  const [archiveRunning, setArchiveRunning] = useState(false)
  const [autoDeleteStatus, setAutoDeleteStatus] = useState<ICloudHmeAutoDeleteStatus | null>(null)
  const [autoDeleteStatusLoading, setAutoDeleteStatusLoading] = useState(false)
  const [autoDeleteRunning, setAutoDeleteRunning] = useState(false)
  const [recheckCampaign, setRecheckCampaign] = useState<ICloudHmeRecheckCampaignData | null>(null)
  const [recheckLoading, setRecheckLoading] = useState(false)
  const [rerunResetting, setRerunResetting] = useState(false)
  const [recheckStatusFilter, setRecheckStatusFilter] = useState('')
  const [previewLoading, setPreviewLoading] = useState(false)
  const [previewOpen, setPreviewOpen] = useState(false)
  const [previewData, setPreviewData] = useState<any | null>(null)
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

  const loadArchiveStatus = async () => {
    setArchiveStatusLoading(true)
    try {
      const data = await apiFetch('/tempmail-archive/status')
      setArchiveStatus(data || null)
    } catch {
      setArchiveStatus(null)
    } finally {
      setArchiveStatusLoading(false)
    }
  }

  const loadAutoDeleteStatus = async () => {
    setAutoDeleteStatusLoading(true)
    try {
      const data = await apiFetch('/icloud-hme/auto-delete/status')
      setAutoDeleteStatus(data || null)
    } catch {
      setAutoDeleteStatus(null)
    } finally {
      setAutoDeleteStatusLoading(false)
    }
  }

  const loadRecheckCampaign = async (campaignId = recheckCampaign?.campaign_id || '', statusValue = recheckStatusFilter) => {
    setRecheckLoading(true)
    try {
      const path = campaignId
        ? `/icloud-hme/recheck/campaigns/${encodeURIComponent(String(campaignId))}`
        : '/icloud-hme/recheck/current'
      const params = new URLSearchParams({ page: '1', size: '8' })
      if (statusValue) params.set('status', statusValue)
      const data = await apiFetch(`${path}?${params.toString()}`)
      setRecheckCampaign(data || null)
    } catch {
      setRecheckCampaign(null)
    } finally {
      setRecheckLoading(false)
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
      loadArchiveStatus().catch(() => {})
      loadAutoDeleteStatus().catch(() => {})
    } catch (e: any) {
      message.error(e?.message || '读取 iCloud HME 别名失败')
    } finally {
      setLoading(false)
    }
  }

  useEffect(() => {
    loadAliases().catch(() => {})
    loadAutoPoolStatus().catch(() => {})
    loadArchiveStatus().catch(() => {})
    loadAutoDeleteStatus().catch(() => {})
    loadRecheckCampaign().catch(() => {})
  }, [])

  const runArchiveCleanup = async () => {
    setArchiveRunning(true)
    try {
      const result = await apiFetch('/tempmail-archive/run', {
        method: 'POST',
        body: JSON.stringify({
          force: true,
          ignore_active_tasks: false,
          delete: true,
        }),
      })
      setArchiveStatus((prev) => ({
        ...(prev || {}),
        last_result: result || {},
        last_error: String(result?.error || result?.last_error || ''),
      }))
      if (result?.reason === 'active_tasks') {
        message.warning(`当前有 ${Number(result?.active_task_count || 0)} 个活跃任务，已跳过清理`)
      } else if (result?.ok) {
        message.success(`归档清理完成：归档 ${Number(result?.archived || 0)} 封，删除 ${Number(result?.deleted || 0)} 封`)
      } else {
        message.warning(result?.error || '归档清理已执行，但存在未完成项')
      }
      await loadArchiveStatus()
    } catch (e: any) {
      message.error(e?.message || '归档清理失败')
    } finally {
      setArchiveRunning(false)
    }
  }

  const previewDeletion = async () => {
    setPreviewLoading(true)
    try {
      const data = await apiFetch('/icloud-hme/deletion-preview')
      setPreviewData(data || null)
      setPreviewOpen(true)
    } catch (e: any) {
      message.error(e?.message || '扫描未使用别名失败')
    } finally {
      setPreviewLoading(false)
    }
  }

  const resetAliasesForRerun = async () => {
    const values = form.getFieldsValue(true)
    const forwardTo = String(values.icloud_forward_to || 'b@cccy.me').trim() || 'b@cccy.me'
    setRerunResetting(true)
    try {
      const result = await apiFetch('/icloud-hme/aliases/reset-rerun', {
        method: 'POST',
        body: JSON.stringify({
          forward_to: forwardTo,
          purpose: 'chatgpt_register',
          bound_service: 'chatgpt',
          include_in_flight: false,
          include_ready_stock: false,
          reset_existing_queue: true,
          dry_run: false,
        }),
      })
      const campaignId = String(result?.campaign_id || '').trim()
      setRecheckCampaign({
        campaign_id: campaignId,
        summary: result?.summary || {},
        data: [],
        total: Number(result?.summary?.total || 0),
      })
      message.success(`已重置到导入池：${Number(result?.reset || 0)} 个，创建进度批次 ${campaignId || '-'}`)
      await loadAliases(1, pageSize)
      await loadRecheckCampaign(campaignId)
    } catch (e: any) {
      message.error(e?.message || '重置 HME 到导入池失败')
    } finally {
      setRerunResetting(false)
    }
  }

  const confirmResetAliasesForRerun = () => {
    Modal.confirm({
      title: '重置已领取 HME 到导入池',
      content: (
        <div>
          <div>会把当前转发邮箱下已领取/已使用的 HME 本地恢复为可领取库存。</div>
          <div style={{ marginTop: 8 }}>重置后请到注册面板继续选择「iCloud HME」和「仅导入池」，分批重新跑。</div>
          <div style={{ color: '#cf1322', marginTop: 8 }}>
            本动作只改本地状态，不调用 Apple 停用/删除，也不删除 ChatGPT 账号。明确 account_deleted/account_deactivated 后只会标记待删除。
          </div>
        </div>
      ),
      okText: '确认重置',
      cancelText: '取消',
      onOk: resetAliasesForRerun,
    })
  }

  const executeAutoDelete = async () => {
    setAutoDeleteRunning(true)
    try {
      const result = await apiFetch('/icloud-hme/auto-delete/run', {
        method: 'POST',
        body: JSON.stringify({ force: true, ignore_active_tasks: false, delete: true }),
      })
      if (result?.reason === 'active_tasks') {
        message.warning(`当前有 ${Number(result?.active_task_count || 0)} 个活跃任务，已跳过删除`)
      } else if (result?.reason === 'rate_limit_backoff') {
        message.warning('当前处于限流退避中，已跳过本轮删除')
      } else if (result?.reason === 'disabled') {
        message.warning('自动删除未开启（手动执行已被拦截）')
      } else if (result?.ok) {
        message.success(`删除完成：测活 ${Number(result?.rechecked || 0)} 个，存活保留 ${Number(result?.kept_alive || 0)} 个，删除 ${Number(result?.deleted || 0)} 个，跳过 ${Number(result?.skipped || 0)} 个`)
      } else {
        message.warning(result?.error || `删除已执行：删除 ${Number(result?.deleted || 0)} 个，存在未完成项`)
      }
      setPreviewOpen(false)
      await loadAutoDeleteStatus()
      await loadAliases()
    } catch (e: any) {
      message.error(e?.message || '删除执行失败')
    } finally {
      setAutoDeleteRunning(false)
    }
  }

  const runAutoDelete = async () => {
    const summary = autoDeleteStatus?.candidate_summary || {}
    const orphan = Number(summary.orphan || 0) || 0
    const boundInvalid = Number(summary.bound_invalid || 0) || 0
    Modal.confirm({
      title: '立即删除未使用/失效别名',
      content: (
        <div>
          <div>候选：孤儿别名 {orphan} 个、失效绑定 {boundInvalid} 个。每个删除前都会先免密登录测活：能登录的视为存活、保留并重新导入账号列表，只删确认失效的。</div>
          <div style={{ color: '#cf1322', marginTop: 8 }}>
            删除会在 Apple 端 deactivate + delete，不可恢复；测活是真实登录、较慢，受单次上限限制，超出的留到下一轮。
          </div>
        </div>
      ),
      okText: '确认删除',
      okButtonProps: { danger: true },
      cancelText: '取消',
      onOk: executeAutoDelete,
    })
  }

  const syncLiveAliases = async (pruneMissing = false) => {
    const values = form.getFieldsValue(true)
    const payload = {
      icloud_cookie: String(values.icloud_cookie || '').trim(),
      icloud_domain_base: String(values.icloud_domain_base || 'icloud.com').trim() || 'icloud.com',
      forward_to: String(values.icloud_forward_to || 'b@cccy.me').trim() || 'b@cccy.me',
      purpose: 'chatgpt_register',
      bound_service: 'chatgpt',
      prune_missing: pruneMissing,
      dry_run: false,
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
      const pruned = Number(result?.prune?.deleted || 0)
      const inserted = Number(result?.result?.inserted || 0)
      const updated = Number(result?.result?.updated || 0)
      message.success(
        pruneMissing
          ? `同步完成：官网 ${Number(result?.synced_count || 0)} 条，新增 ${inserted}，更新 ${updated}，清理本地多余 ${pruned} 条`
          : `同步完成：${Number(result?.synced_count || 0)} 条`
      )
      await loadAliases(1, pageSize)
    } catch (e: any) {
      message.error(e?.message || '同步 iCloud 官网别名失败')
    } finally {
      setSyncing(false)
    }
  }

  const syncLiveAliasesAndPrune = () => {
    Modal.confirm({
      title: '同步并清理本地多余别名',
      content: (
        <div>
          <div>将先读取 iCloud 官网当前 HME 别名列表，然后删除本地别名池中“官网已不存在”的记录。</div>
          <div style={{ color: '#cf1322', marginTop: 8 }}>
            只清理本地 iCloud HME 别名池，不删除 ChatGPT 账号，也不会调用 Apple 端 delete。
          </div>
        </div>
      ),
      okText: '同步并清理',
      okButtonProps: { danger: true },
      cancelText: '取消',
      onOk: async () => {
        setSyncPruning(true)
        try {
          await syncLiveAliases(true)
        } finally {
          setSyncPruning(false)
        }
      },
    })
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
        `批量启用完成：命中 ${Number(result?.matched || 0)} 条，新启用 ${Number(result?.enabled || 0)} 条，恢复普通失败 ${Number(result?.recycled || 0)} 条；账号已禁用/死号不会回收`
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
      width: 170,
      render: (_: any, item: any) => {
        if (item.account_disabled || String(item.status || '') === 'account_deactivated' || String(item.status || '') === 'account_disabled') {
          return <Tag color="red">账号已禁用/死号</Tag>
        }
        return (
          <Tag color={item.used_by_system ? 'orange' : 'green'}>
            {item.used_by_system ? '系统已使用' : '系统未使用'}
          </Tag>
        )
      },
    },
    {
      title: '池状态',
      key: 'status',
      width: 150,
      render: (_: any, item: any) => {
        const status = String(item.status || '-')
        const color = status === 'account_deactivated' || status === 'account_disabled'
          ? 'red'
          : status === 'registered'
            ? 'green'
            : status === 'register_failed'
              ? 'orange'
              : status === 'in_use'
                ? 'blue'
                : status === 'retired'
                  ? 'default'
                  : 'cyan'
        const label = status === 'account_deactivated' || status === 'account_disabled' ? '账号已禁用' : status
        return <Tag color={color}>{label}</Tag>
      },
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
            disabled={Boolean(item.account_disabled) || String(item.status || '') === 'account_deactivated' || String(item.status || '') === 'account_disabled'}
            title={Boolean(item.account_disabled) ? '账号已禁用/死号，不允许重新启用到邮箱池' : undefined}
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
  const archiveLastResult = archiveStatus?.last_result || {}
  const archiveEmailCount = Number(archiveLastResult.email_count || 0) || 0
  const archiveArchivedCount = Number(archiveLastResult.archived || 0) || 0
  const archiveDeletedCount = Number(archiveLastResult.deleted || 0) || 0
  const archiveActiveTaskCount = Number(archiveStatus?.active_task_count || archiveLastResult.active_task_count || 0) || 0
  const autoDeleteSummary = autoDeleteStatus?.candidate_summary || {}
  const autoDeletePending = Number(autoDeleteStatus?.pending_candidates || 0) || 0
  const autoDeleteLastResult = autoDeleteStatus?.last_result || {}
  const autoDeleteLastDeleted = Number(autoDeleteLastResult.deleted || 0) || 0
  const autoDeleteLastKept = Number(autoDeleteLastResult.kept_alive || 0) || 0
  const recheckSummary = recheckCampaign?.summary || {}
  const recheckCampaignId = String(recheckCampaign?.campaign_id || '').trim()
  const recheckTotal = Number(recheckSummary.total || 0) || 0
  const recheckChecked = Number(recheckSummary.checked || 0) || 0
  const recheckPending = Number(recheckSummary.pending || 0) || 0
  const recheckRetry = Number(recheckSummary.retry || 0) || 0
  const recheckAlive = Number(recheckSummary.alive || 0) || 0
  const recheckDeleteCandidates = Number(recheckSummary.delete_candidates || recheckSummary.delete_candidate || 0) || 0
  const recheckAccessTokenSaved = Number(recheckSummary.access_token_saved || 0) || 0

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
          <Button danger loading={syncPruning} onClick={syncLiveAliasesAndPrune}>
            同步并清理多余
          </Button>
          <Button icon={<SyncOutlined />} loading={(syncing || loading) && !syncPruning} onClick={() => syncLiveAliases(false)}>
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
            <Tag color={autoPoolStatus?.in_error_backoff ? 'orange' : 'cyan'}>
              {autoPoolStatus?.in_error_backoff ? '错误短退避中' : '无错误退避'}
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
            错误短退避: {autoPoolStatus?.error_backoff_minutes ?? '-'} 分钟
          </Typography.Text>
          <Typography.Text type="secondary">
            退避到: {formatDateTimeText(autoPoolStatus?.rate_limit_until || autoPoolStatus?.error_backoff_until)}
          </Typography.Text>
          <Typography.Text type="secondary">
            最近创建: {autoPoolStatus?.last_created_hme || '-'}
          </Typography.Text>
          <Typography.Text type={autoPoolStatus?.last_error ? 'danger' : 'secondary'}>
            最近错误: {autoPoolStatus?.last_error || '-'}
          </Typography.Text>
        </Space>
      </div>
      <div
        style={{
          marginBottom: 16,
          padding: 12,
          border: '1px solid rgba(114, 46, 209, 0.22)',
          borderRadius: 8,
          background: 'rgba(114, 46, 209, 0.04)',
        }}
      >
        <Space wrap size={[8, 8]} style={{ width: '100%', justifyContent: 'space-between' }}>
          <Space wrap>
            <Tag color={recheckCampaignId ? 'purple' : 'default'}>
              重跑批次: {recheckCampaignId || '未创建'}
            </Tag>
            <Tag color="blue">总数: {recheckTotal || '-'}</Tag>
            <Tag color={recheckPending > 0 ? 'gold' : 'green'}>未跑: {recheckPending}</Tag>
            <Tag color="green">存活: {recheckAlive}</Tag>
            <Tag color={recheckDeleteCandidates > 0 ? 'volcano' : 'default'}>
              待删除标记: {recheckDeleteCandidates}
            </Tag>
            <Tag color={recheckRetry > 0 ? 'orange' : 'default'}>待重试: {recheckRetry}</Tag>
            <Tag color="cyan">AT保存: {recheckAccessTokenSaved}</Tag>
          </Space>
          <Space wrap>
            <Button size="small" loading={recheckLoading} onClick={() => loadRecheckCampaign()}>
              刷新进度
            </Button>
            <Button size="small" loading={rerunResetting} onClick={confirmResetAliasesForRerun}>
              重置已领取到导入池
            </Button>
          </Space>
        </Space>
        <Alert
          type="info"
          showIcon
          style={{ marginTop: 10, marginBottom: 10 }}
          message="重置后走原注册面板分批重跑"
          description="这里显示重跑进度；先把已领取 HME 重置回导入池，然后到注册面板选择「iCloud HME」并使用「仅导入池」模式分批跑。存活账号会重新保存 access_token；明确 account_deleted/account_deactivated 只标记待删除，真正 Apple 停用/删除仍走下面原来的自动删除模块。"
        />
        <Space wrap size={[12, 8]}>
          <Typography.Text type="secondary">
            已跑: {recheckChecked}/{recheckTotal || 0}
          </Typography.Text>
          <Typography.Text type="secondary">
            继续分批重跑请使用注册面板：邮箱服务 iCloud HME，模式仅导入池。
          </Typography.Text>
        </Space>
        <Space wrap style={{ marginTop: 10, marginBottom: 8 }}>
          <Select
            allowClear
            size="small"
            placeholder="重跑状态"
            style={{ width: 150 }}
            value={recheckStatusFilter || undefined}
            onChange={(value) => {
              const nextValue = String(value || '')
              setRecheckStatusFilter(nextValue)
              loadRecheckCampaign(recheckCampaignId, nextValue).catch(() => {})
            }}
            options={[
              { label: 'pending', value: 'pending' },
              { label: 'running', value: 'running' },
              { label: 'alive', value: 'alive' },
              { label: 'delete_candidate', value: 'delete_candidate' },
              { label: 'retry', value: 'retry' },
              { label: 'dead_kept', value: 'dead_kept' },
              { label: 'skipped', value: 'skipped' },
            ]}
          />
        </Space>
        <Table
          size="small"
          rowKey={(r: any) => String(r.id || r.anonymous_id || r.hme)}
          loading={recheckLoading}
          pagination={false}
          dataSource={Array.isArray(recheckCampaign?.data) ? recheckCampaign.data : []}
          columns={[
            {
              title: '邮箱',
              dataIndex: 'hme',
              key: 'hme',
              render: (v: string) => <Typography.Text copyable style={{ fontFamily: 'monospace' }}>{v}</Typography.Text>,
            },
            {
              title: '状态',
              dataIndex: 'status',
              key: 'status',
              width: 130,
              render: (v: string) => {
                const value = String(v || '')
                const color = value === 'alive' ? 'green' : value === 'delete_candidate' ? 'volcano' : value === 'retry' ? 'orange' : 'default'
                return <Tag color={color}>{value || '-'}</Tag>
              },
            },
            { title: '结果', dataIndex: 'result_code', key: 'result_code', width: 150, render: (v: string) => v || '-' },
            {
              title: '待删除',
              dataIndex: 'delete_candidate',
              key: 'delete_candidate',
              width: 90,
              render: (v: boolean) => <Tag color={v ? 'volcano' : 'default'}>{v ? '是' : '否'}</Tag>,
            },
            { title: '保存账号', dataIndex: 'saved_account_id', key: 'saved_account_id', width: 100, render: (v: number) => v || '-' },
            { title: '重跑时间', dataIndex: 'checked_at', key: 'checked_at', width: 180, render: (v: string) => formatDateTimeText(v) },
          ]}
        />
      </div>
      <div
        style={{
          marginBottom: 16,
          padding: 12,
          border: '1px solid rgba(82, 196, 26, 0.22)',
          borderRadius: 8,
          background: 'rgba(82, 196, 26, 0.04)',
        }}
      >
        <Space wrap size={[8, 8]} style={{ width: '100%', justifyContent: 'space-between' }}>
          <Space wrap>
            <Tag color={archiveStatus?.enabled ? 'green' : 'default'}>
              归档清理: {archiveStatus?.enabled ? '开启' : '关闭'}
            </Tag>
            <Tag color={archiveStatus?.running ? 'blue' : 'default'}>
              调度线程: {archiveStatus?.running ? '运行中' : '未运行'}
            </Tag>
            <Tag color={archiveActiveTaskCount > 0 ? 'gold' : 'green'}>
              活跃任务: {archiveActiveTaskCount}
            </Tag>
            <Tag color="cyan">最近扫描: {archiveEmailCount || '-'}</Tag>
            <Tag color="geekblue">归档: {archiveArchivedCount || '-'}</Tag>
            <Tag color="purple">删除: {archiveDeletedCount || '-'}</Tag>
          </Space>
          <Space wrap>
            <Button size="small" loading={archiveStatusLoading} onClick={() => loadArchiveStatus()}>
              刷新归档状态
            </Button>
            <Button size="small" type="primary" loading={archiveRunning} onClick={runArchiveCleanup}>
              立即归档清理
            </Button>
          </Space>
        </Space>
        <Space wrap size={[16, 6]} style={{ marginTop: 8 }}>
          <Typography.Text type="secondary">
            归档邮箱: {archiveStatus?.mailbox || '-'}
          </Typography.Text>
          <Typography.Text type="secondary">
            下次清理: {formatDateTimeText(archiveStatus?.next_run_at)}
          </Typography.Text>
          <Typography.Text type="secondary">
            剩余: {formatDurationText(archiveStatus?.seconds_until_next_run)}
          </Typography.Text>
          <Typography.Text type="secondary">
            保留最近: {archiveStatus?.keep_recent_minutes || '-'} 分钟
          </Typography.Text>
          <Typography.Text type="secondary">
            触发阈值: {archiveStatus?.threshold || '-'} 封
          </Typography.Text>
          <Typography.Text type="secondary">
            备份: {archiveStatus?.backup_path || '-'}
          </Typography.Text>
          <Typography.Text type={archiveStatus?.last_error ? 'danger' : 'secondary'}>
            最近错误: {archiveStatus?.last_error || '-'}
          </Typography.Text>
        </Space>
      </div>
      <div
        style={{
          marginBottom: 16,
          padding: 12,
          border: '1px solid rgba(245, 34, 45, 0.20)',
          borderRadius: 8,
          background: 'rgba(245, 34, 45, 0.04)',
        }}
      >
        <Space wrap size={[8, 8]} style={{ width: '100%', justifyContent: 'space-between' }}>
          <Space wrap>
            <Tag color={autoDeleteStatus?.enabled ? 'green' : 'default'}>
              自动删除: {autoDeleteStatus?.enabled ? '开启' : '关闭'}
            </Tag>
            <Tag color={autoDeleteStatus?.running ? 'blue' : 'default'}>
              调度线程: {autoDeleteStatus?.running ? '运行中' : '未运行'}
            </Tag>
            <Tag color={autoDeleteStatus?.in_rate_limit_backoff ? 'red' : 'cyan'}>
              {autoDeleteStatus?.in_rate_limit_backoff ? '限流等待中' : '未限流'}
            </Tag>
            <Tag color={autoDeleteStatus?.recheck_before_delete ? 'geekblue' : 'default'}>
              删前测活: {autoDeleteStatus?.recheck_before_delete ? '是' : '否'}
            </Tag>
            <Tag color={autoDeletePending > 0 ? 'volcano' : 'green'}>
              待测候选: {autoDeletePending}（孤儿 {Number(autoDeleteSummary.orphan || 0)} / 失效绑定 {Number(autoDeleteSummary.bound_invalid || 0)}）
            </Tag>
            <Tag color={autoDeleteStatus?.in_rate_limit_backoff ? 'red' : 'cyan'}>
              {autoDeleteStatus?.in_rate_limit_backoff ? '限流等待中' : '未限流'}
            </Tag>
            <Tag color={autoDeleteStatus?.in_error_backoff ? 'orange' : 'cyan'}>
              {autoDeleteStatus?.in_error_backoff ? '错误短退避中' : '无错误退避'}
            </Tag>
          </Space>
          <Space wrap>
            <Button size="small" loading={autoDeleteStatusLoading} onClick={() => loadAutoDeleteStatus()}>
              刷新删除状态
            </Button>
            <Button size="small" loading={previewLoading} onClick={previewDeletion}>
              扫描预览
            </Button>
            <Button size="small" danger type="primary" loading={autoDeleteRunning} onClick={runAutoDelete}>
              立即删除
            </Button>
          </Space>
        </Space>
        <Space wrap size={[16, 6]} style={{ marginTop: 8 }}>
          <Typography.Text type="secondary">
            单次上限: {autoDeleteStatus?.max_per_run || '-'}
          </Typography.Text>
          <Typography.Text type="secondary">
            账号间隔: {autoDeleteStatus?.account_interval_min_minutes ?? '-'} - {autoDeleteStatus?.account_interval_max_minutes ?? '-'} 分钟
          </Typography.Text>
          <Typography.Text type="secondary">
            限流延长: {autoDeleteStatus?.rate_limit_backoff_minutes || '-'} 分钟
          </Typography.Text>
          <Typography.Text type="secondary">
            错误短退避: {autoDeleteStatus?.error_backoff_minutes ?? '-'} 分钟
          </Typography.Text>
          <Typography.Text type="secondary">
            退避到: {formatDateTimeText(autoDeleteStatus?.rate_limit_until || autoDeleteStatus?.error_backoff_until)}
          </Typography.Text>
          <Typography.Text type="secondary">
            上轮删除: {autoDeleteLastDeleted}
          </Typography.Text>
          <Typography.Text type="secondary">
            上轮存活保留: {autoDeleteLastKept}
          </Typography.Text>
          <Typography.Text type={autoDeleteStatus?.last_error ? 'danger' : 'secondary'}>
            最近错误: {autoDeleteStatus?.last_error || '-'}
          </Typography.Text>
        </Space>
      </div>
      <Modal
        open={previewOpen}
        title="未使用/失效别名预览"
        width={760}
        onCancel={() => setPreviewOpen(false)}
        footer={(
          <Space>
            <Button onClick={() => setPreviewOpen(false)}>关闭</Button>
            <Button danger type="primary" loading={autoDeleteRunning} onClick={executeAutoDelete}>
              立即删除
            </Button>
          </Space>
        )}
      >
        {previewData ? (
          <div>
            <Space wrap style={{ marginBottom: 12 }}>
              <Tag color="volcano">孤儿(不在账号里): {(previewData.orphan || []).length}</Tag>
              <Tag color="orange">失效绑定(需测活): {(previewData.bound_invalid || []).length}</Tag>
              <Tag color="green">受保护: {Number(previewData.protected_count || 0)}</Tag>
              <Tag color="blue">别名总数: {Number(previewData.total || 0)}</Tag>
            </Space>
            <Alert
              type="warning"
              showIcon
              style={{ marginBottom: 12 }}
              message="所有候选删除前都会先免密登录测活：能登录的视为存活、保留并重新导入账号列表；只删确认失效（账号被删/停用）的。删除不可恢复，受单次上限限制。"
            />
            <Table
              size="small"
              rowKey={(r: any) => String(r.anonymous_id || r.hme)}
              pagination={{ pageSize: 8 }}
              dataSource={[...(previewData.orphan || []), ...(previewData.bound_invalid || [])]}
              columns={[
                {
                  title: '邮箱',
                  dataIndex: 'hme',
                  key: 'hme',
                  render: (v: string) => (
                    <Typography.Text copyable style={{ fontFamily: 'monospace' }}>{v}</Typography.Text>
                  ),
                },
                {
                  title: '类型',
                  dataIndex: 'disposition',
                  key: 'disposition',
                  width: 110,
                  render: (v: string) =>
                    v === 'orphan' ? <Tag color="volcano">孤儿</Tag> : <Tag color="orange">失效绑定</Tag>,
                },
                { title: '别名状态', dataIndex: 'status', key: 'status', width: 100 },
                {
                  title: '绑定账号',
                  dataIndex: 'bound_account_email',
                  key: 'bound_account_email',
                  render: (v: string) => v || '-',
                },
              ]}
            />
          </div>
        ) : (
          <Typography.Text type="secondary">暂无数据</Typography.Text>
        )}
      </Modal>
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
            { label: '账号已禁用/死号', value: 'account_deactivated' },
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
  const screens = Grid.useBreakpoint()
  const [saving, setSaving] = useState(false)
  const [saved, setSaved] = useState(false)
  const [configLoaded, setConfigLoaded] = useState(false)
  const [configLoadError, setConfigLoadError] = useState('')
  const [shareState, setShareState] = useState<ConfigShareState | null>(null)
  const [shareBusy, setShareBusy] = useState(false)
  const [activeTab, setActiveTab] = useState('register')
  const [chatgptPinEditorOpen, setChatgptPinEditorOpen] = useState(false)
  const [chatgptPinnedSections, setChatgptPinnedSections] = useState<string[]>(loadChatgptPinnedSections)
  const selectedMailProvider = Form.useWatch('mail_provider', form) || 'luckmail'
  const taskProxyMode = String(Form.useWatch('task_proxy_mode', form) || 'dynamic').trim().toLowerCase()
  const taskProxyFailover = parseBooleanConfigValue(Form.useWatch('task_proxy_failover', form))
  const icloudAutoCreateEnabled = parseBooleanConfigValue(Form.useWatch('icloud_hme_auto_create_enabled', form))
  const icloudAutoDeleteEnabled = parseBooleanConfigValue(Form.useWatch('icloud_hme_auto_delete_enabled', form))
  const tempmailArchiveCleanupEnabled = parseBooleanConfigValue(Form.useWatch('tempmail_archive_cleanup_enabled', form))
  const isMobile = screens.md === false

  const loadShareState = async () => {
    const data = await apiFetch('/config/share-state') as ConfigShareState
    setShareState(data || null)
    return data
  }

  const reloadAfterShareChange = () => {
    window.setTimeout(() => window.location.reload(), 350)
  }

  const toggleShareMode = (enabled: boolean) => {
    const actionText = enabled ? '开启共享配置并拉取共享模板' : '关闭共享并转为本地配置'
    Modal.confirm({
      title: actionText,
      content: enabled
        ? '开启后，本页保存会更新共享配置，并影响所有开启共享的实例。当前实例本地配置会先被共享模板覆盖。'
        : '关闭前会先把当前共享配置复制成本实例本地基线；之后本实例保存配置不会影响共享模板，也不会接收其他实例修改。',
      okText: '确认',
      cancelText: '取消',
      onOk: async () => {
        setShareBusy(true)
        try {
          const result = await apiFetch('/config/share-state', {
            method: 'PUT',
            body: JSON.stringify({ enabled, pull: true }),
          }) as ConfigShareState
          setShareState(result)
          message.success(enabled ? '已开启共享配置' : '已关闭共享配置')
          reloadAfterShareChange()
        } finally {
          setShareBusy(false)
        }
      },
    })
  }

  const pullSharedConfig = async () => {
    setShareBusy(true)
    try {
      await apiFetch('/config/share/pull', { method: 'POST' })
      await loadShareState()
      message.success('已从共享模板拉取到当前实例')
      reloadAfterShareChange()
    } finally {
      setShareBusy(false)
    }
  }

  const pushLocalConfigToShared = () => {
    Modal.confirm({
      title: '将本实例配置推送为共享模板',
      content: '这是覆盖共享模板的危险操作，会影响所有开启共享配置的实例。建议仅在确认当前实例配置是最新母版时执行。',
      okText: '确认覆盖共享模板',
      okButtonProps: { danger: true },
      cancelText: '取消',
      onOk: async () => {
        setShareBusy(true)
        try {
          const result = await apiFetch('/config/share/push', {
            method: 'POST',
            body: JSON.stringify({
              confirm: true,
              base_revision: shareState?.shared?.revision,
              note: `ui-push:${shareState?.instance_id || 'unknown'}`,
            }),
          }) as { state?: ConfigShareState }
          if (result?.state) setShareState(result.state)
          message.success('已用本实例配置更新共享模板')
          reloadAfterShareChange()
        } finally {
          setShareBusy(false)
        }
      },
    })
  }

  const showShareDiff = async () => {
    setShareBusy(true)
    try {
      const result = await apiFetch('/config/share/diff') as { diff_count?: number; diffs?: { key: string }[] }
      const keys = (result.diffs || []).slice(0, 40).map((item) => item.key)
      Modal.info({
        title: `本地与共享差异：${result.diff_count || 0} 个 key`,
        content: keys.length > 0
          ? (
              <div style={{ maxHeight: 360, overflow: 'auto' }}>
                {keys.map((key) => <Tag key={key} style={{ marginBottom: 6 }}>{key}</Tag>)}
                {(result.diff_count || 0) > keys.length ? <Typography.Text type="secondary">仅展示前 {keys.length} 个。</Typography.Text> : null}
              </div>
            )
          : '当前本地配置与共享模板一致。',
      })
    } finally {
      setShareBusy(false)
    }
  }

  useEffect(() => {
    if (typeof window === 'undefined') return
    window.localStorage.setItem(CHATGPT_PINNED_SECTIONS_STORAGE_KEY, JSON.stringify(chatgptPinnedSections))
  }, [chatgptPinnedSections])

  useEffect(() => {
    setConfigLoaded(false)
    setConfigLoadError('')
    loadShareState().catch(() => undefined)
    apiFetch('/config').then((data) => {
      if (!data || typeof data !== 'object' || Array.isArray(data)) {
        throw new Error('配置接口返回格式异常')
      }
      if (!data.mail_provider) {
        data.mail_provider = 'luckmail'
      }
      if (data.mail_provider === 'icloud_hme' && data.icloud_hme_mode === 'helper_ready_api') {
        data.mail_provider = 'hme_ready_api'
      }
      if (data.mail_provider === 'hme_ready_api') {
        data.icloud_hme_mode = 'helper_ready_api'
      } else if (data.mail_provider === 'icloud_hme' && data.icloud_hme_mode === 'helper_ready_api') {
        data.icloud_hme_mode = 'import_pool'
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
      if (!data.email_api_poll_interval_seconds) {
        data.email_api_poll_interval_seconds = '3'
      }
      if (!data.email_api_request_timeout_seconds) {
        data.email_api_request_timeout_seconds = '15'
      }
      if (!data.chatgpt_register_otp_wait_seconds) {
        data.chatgpt_register_otp_wait_seconds = '120'
      }
      if (!data.chatgpt_register_otp_resend_wait_seconds) {
        data.chatgpt_register_otp_resend_wait_seconds = '90'
      }
      if (!data.chatgpt_register_otp_account_budget_seconds) {
        data.chatgpt_register_otp_account_budget_seconds = '210'
      }
      if (data.email_api_gmail_dot_variant_enabled === undefined || data.email_api_gmail_dot_variant_enabled === '') {
        data.email_api_gmail_dot_variant_enabled = true
      }
      if (!data.email_api_gmail_variant_count) {
        data.email_api_gmail_variant_count = '2'
      }
      if (!data.email_api_gmail_variant_rules) {
        data.email_api_gmail_variant_rules = 'all'
      }
      if (!data.email_api_gmail_plus_tag_template) {
        data.email_api_gmail_plus_tag_template = 'r{rand}'
      }
      if (!data.email_api_default_scheme) {
        data.email_api_default_scheme = 'https'
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
      if (data.local_phone_gateway_auto_acquire_enabled === '') {
        data.local_phone_gateway_auto_acquire_enabled = true
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
      if (!data.local_phone_gateway_max_resend_attempts) {
        data.local_phone_gateway_max_resend_attempts = '20'
      }
      if (!data.local_phone_gateway_resend_interval_seconds) {
        data.local_phone_gateway_resend_interval_seconds = '30'
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
      if (!data.icloud_hme_auto_create_error_backoff_minutes) {
        data.icloud_hme_auto_create_error_backoff_minutes = '3'
      }
      if (!data.icloud_hme_auto_delete_account_interval_min_minutes) {
        data.icloud_hme_auto_delete_account_interval_min_minutes = '10'
      }
      if (!data.icloud_hme_auto_delete_account_interval_max_minutes) {
        data.icloud_hme_auto_delete_account_interval_max_minutes = '30'
      }
      if (!data.icloud_hme_auto_delete_max_per_run) {
        data.icloud_hme_auto_delete_max_per_run = '20'
      }
      if (!data.icloud_hme_auto_delete_rate_limit_backoff_minutes) {
        data.icloud_hme_auto_delete_rate_limit_backoff_minutes = '60'
      }
      if (!data.icloud_hme_auto_delete_error_backoff_minutes) {
        data.icloud_hme_auto_delete_error_backoff_minutes = '3'
      }
      if (!data.icloud_hme_auto_delete_dead_statuses) {
        data.icloud_hme_auto_delete_dead_statuses = 'account_deactivated,password_invalid'
      }
      if (!data.tempmail_archive_cleanup_interval_minutes) {
        data.tempmail_archive_cleanup_interval_minutes = '30'
      }
      if (!data.tempmail_archive_cleanup_keep_recent_minutes) {
        data.tempmail_archive_cleanup_keep_recent_minutes = '60'
      }
      if (!data.tempmail_archive_cleanup_threshold) {
        data.tempmail_archive_cleanup_threshold = '100'
      }
      if (!data.tempmail_archive_cleanup_mailbox) {
        data.tempmail_archive_cleanup_mailbox = data.icloud_forward_to || 'b@cccy.me'
      }
      if (!data.tempmail_archive_cleanup_backup_path) {
        data.tempmail_archive_cleanup_backup_path = '/runtime/tempmail_email_backups.db'
      }
      data.tempmail_permanent = parseBooleanConfigValue(data.tempmail_permanent)
      data.icloud_hme_auto_create_enabled = parseBooleanConfigValue(data.icloud_hme_auto_create_enabled)
      data.icloud_hme_auto_delete_enabled = parseBooleanConfigValue(data.icloud_hme_auto_delete_enabled)
      data.icloud_hme_auto_delete_recheck_before_delete =
        data.icloud_hme_auto_delete_recheck_before_delete === ''
          ? true
          : parseBooleanConfigValue(data.icloud_hme_auto_delete_recheck_before_delete)
      data.icloud_hme_auto_delete_pause_active_tasks =
        data.icloud_hme_auto_delete_pause_active_tasks === ''
          ? true
          : parseBooleanConfigValue(data.icloud_hme_auto_delete_pause_active_tasks)
      data.tempmail_archive_cleanup_enabled = parseBooleanConfigValue(data.tempmail_archive_cleanup_enabled)
      data.tempmail_archive_cleanup_pause_active_tasks =
        data.tempmail_archive_cleanup_pause_active_tasks === ''
          ? true
          : parseBooleanConfigValue(data.tempmail_archive_cleanup_pause_active_tasks)
      data.proxy_pool_cooldown_enabled = data.proxy_pool_cooldown_enabled === '' ? true : parseBooleanConfigValue(data.proxy_pool_cooldown_enabled)
      if (!data.task_proxy_mode) {
        data.task_proxy_mode = 'dynamic'
      }
      if (!data.task_proxy_max_candidates) {
        data.task_proxy_max_candidates = data.proxy_pool_max_candidates || '5'
      }
      if (!data.task_proxy_min_score) {
        data.task_proxy_min_score = data.proxy_scan_min_score || '50'
      }
      data.task_proxy_failover = data.task_proxy_failover === '' ? false : parseBooleanConfigValue(data.task_proxy_failover)
      if (String(data.task_proxy_mode).trim().toLowerCase() === 'dynamic') {
        // 只在内存中兼容旧配置，访问 Settings 不会因此改共享 revision。
        if (!data.dynamic_proxy_template && data.task_proxy_url) {
          data.dynamic_proxy_template = data.task_proxy_url
        }
        if (!data.dynamic_proxy_default_country && data.task_proxy_country_code) {
          data.dynamic_proxy_default_country = data.task_proxy_country_code
        }
      }
      if (!data.dynamic_proxy_default_country) {
        data.dynamic_proxy_default_country = 'JP'
      }
      if (!data.dynamic_proxy_probe_timeout_seconds) {
        data.dynamic_proxy_probe_timeout_seconds = '8'
      }
      if (!data.dynamic_proxy_ip_retention_minutes) {
        data.dynamic_proxy_ip_retention_minutes = '5'
      }
      data.dynamic_proxy_require_country_match = data.dynamic_proxy_require_country_match === '' ? true : parseBooleanConfigValue(data.dynamic_proxy_require_country_match)
      data.dynamic_proxy_probe_enabled = data.dynamic_proxy_probe_enabled === '' ? true : parseBooleanConfigValue(data.dynamic_proxy_probe_enabled)
      data.cfworker_domains = parseStoredDomainList(data.cfworker_domains)
      data.cfworker_enabled_domains = parseStoredDomainList(data.cfworker_enabled_domains)
      data.cfworker_random_subdomain = parseBooleanConfigValue(data.cfworker_random_subdomain)
      data.contribution_enabled = parseBooleanConfigValue(data.contribution_enabled)
      data.chatgpt_enable_team_invite = parseBooleanConfigValue(data.chatgpt_enable_team_invite)
      data.chatgpt_team_invite_deferred_activation = parseBooleanConfigValue(data.chatgpt_team_invite_deferred_activation)
      data.chatgpt_capture_free_workspace = data.chatgpt_capture_free_workspace === '' ? true : parseBooleanConfigValue(data.chatgpt_capture_free_workspace)
      data.chatgpt_capture_business_workspace = data.chatgpt_capture_business_workspace === '' ? true : parseBooleanConfigValue(data.chatgpt_capture_business_workspace)
      Object.assign(data, buildChatGPTK12ConfigData(data))
      data.chatgpt_gopay_billing_llm_enabled = data.chatgpt_gopay_billing_llm_enabled === '' ? true : parseBooleanConfigValue(data.chatgpt_gopay_billing_llm_enabled)
      data.chatgpt_access_token_only_checkout_amount_check_enabled =
        data.chatgpt_access_token_only_checkout_amount_check_enabled === ''
          ? true
          : parseBooleanConfigValue(data.chatgpt_access_token_only_checkout_amount_check_enabled)
      data.chatgpt_access_token_only_zero_amount_stop_enabled = parseBooleanConfigValue(
        data.chatgpt_access_token_only_zero_amount_stop_enabled,
      )
      data.chatgpt_access_token_only_gopay_provider_link_enabled = parseBooleanConfigValue(
        data.chatgpt_access_token_only_gopay_provider_link_enabled,
      )
      data.chatgpt_resume_auth_allow_phone_verification = parseBooleanConfigValue(
        data.chatgpt_resume_auth_allow_phone_verification,
      )
      data.chatgpt_resume_auth_allow_add_phone_verification = parseBooleanConfigValue(
        data.chatgpt_resume_auth_allow_add_phone_verification,
      )
      data.chatgpt_resume_auth_allow_existing_phone_verification = parseBooleanConfigValue(
        data.chatgpt_resume_auth_allow_existing_phone_verification === '' ? true : data.chatgpt_resume_auth_allow_existing_phone_verification,
      )
      data.chatgpt_recheck_allow_existing_phone_verification = parseBooleanConfigValue(
        data.chatgpt_recheck_allow_existing_phone_verification === '' ? true : data.chatgpt_recheck_allow_existing_phone_verification,
      )
      data.local_phone_gateway_auto_acquire_enabled = parseBooleanConfigValue(data.local_phone_gateway_auto_acquire_enabled)
      data.external_subscription_api_enabled = parseBooleanConfigValue(data.external_subscription_api_enabled)
      data.external_access_token_api_enabled = parseBooleanConfigValue(data.external_access_token_api_enabled)
      data.external_access_token_allow_refresh =
        data.external_access_token_allow_refresh === ''
          ? true
          : parseBooleanConfigValue(data.external_access_token_allow_refresh)
      form.setFieldsValue(data)
      setConfigLoaded(true)
    }).catch((error) => {
      const detail = error instanceof Error ? error.message : String(error || '配置加载失败')
      setConfigLoadError(detail)
      message.error(detail)
    })
  }, [form])

  const save = async () => {
    if (!configLoaded) {
      message.error(configLoadError || '配置尚未成功加载，已阻止保存，避免覆盖现有配置')
      return
    }
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
      if (values.mail_provider === 'hme_ready_api') {
        values.icloud_hme_mode = 'helper_ready_api'
      } else if (values.mail_provider === 'icloud_hme' && values.icloud_hme_mode === 'helper_ready_api') {
        values.icloud_hme_mode = 'import_pool'
      }
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
      const createErrorBackoff = Number.parseInt(String(values.icloud_hme_auto_create_error_backoff_minutes ?? '3'), 10)
      values.icloud_hme_auto_create_error_backoff_minutes = String(
        Math.max(0, Number.isFinite(createErrorBackoff) ? createErrorBackoff : 3),
      )
      values.icloud_hme_auto_delete_enabled = parseBooleanConfigValue(values.icloud_hme_auto_delete_enabled)
      values.icloud_hme_auto_delete_recheck_before_delete = parseBooleanConfigValue(
        values.icloud_hme_auto_delete_recheck_before_delete,
      )
      values.icloud_hme_auto_delete_pause_active_tasks = parseBooleanConfigValue(
        values.icloud_hme_auto_delete_pause_active_tasks,
      )
      const accountIntervalMin = Math.max(
        0,
        Number.parseInt(String(values.icloud_hme_auto_delete_account_interval_min_minutes || '10'), 10) || 10,
      )
      const accountIntervalMax = Math.max(
        accountIntervalMin,
        Number.parseInt(String(values.icloud_hme_auto_delete_account_interval_max_minutes || '30'), 10) || 30,
      )
      values.icloud_hme_auto_delete_account_interval_min_minutes = String(accountIntervalMin)
      values.icloud_hme_auto_delete_account_interval_max_minutes = String(accountIntervalMax)
      values.icloud_hme_auto_delete_max_per_run = String(
        Math.max(1, Number.parseInt(String(values.icloud_hme_auto_delete_max_per_run || '20'), 10) || 20),
      )
      values.icloud_hme_auto_delete_rate_limit_backoff_minutes = String(
        Math.max(
          1,
          Number.parseInt(String(values.icloud_hme_auto_delete_rate_limit_backoff_minutes || '60'), 10) || 60,
        ),
      )
      const deleteErrorBackoff = Number.parseInt(String(values.icloud_hme_auto_delete_error_backoff_minutes ?? '3'), 10)
      values.icloud_hme_auto_delete_error_backoff_minutes = String(
        Math.max(0, Number.isFinite(deleteErrorBackoff) ? deleteErrorBackoff : 3),
      )
      values.icloud_hme_auto_delete_dead_statuses =
        String(values.icloud_hme_auto_delete_dead_statuses || 'account_deactivated,password_invalid').trim() ||
        'account_deactivated,password_invalid'
      values.tempmail_archive_cleanup_enabled = parseBooleanConfigValue(values.tempmail_archive_cleanup_enabled)
      values.tempmail_archive_cleanup_pause_active_tasks = parseBooleanConfigValue(
        values.tempmail_archive_cleanup_pause_active_tasks,
      )
      values.tempmail_archive_cleanup_interval_minutes = String(
        Math.max(
          1,
          Number.parseInt(String(values.tempmail_archive_cleanup_interval_minutes || '30'), 10) || 30,
        ),
      )
      values.tempmail_archive_cleanup_keep_recent_minutes = String(
        Math.max(
          1,
          Number.parseInt(String(values.tempmail_archive_cleanup_keep_recent_minutes || '60'), 10) || 60,
        ),
      )
      values.tempmail_archive_cleanup_threshold = String(
        Math.max(
          1,
          Number.parseInt(String(values.tempmail_archive_cleanup_threshold || '100'), 10) || 100,
        ),
      )
      values.tempmail_archive_cleanup_mailbox = String(
        values.tempmail_archive_cleanup_mailbox || values.icloud_forward_to || 'b@cccy.me',
      ).trim() || 'b@cccy.me'
      values.tempmail_archive_cleanup_backup_path = String(
        values.tempmail_archive_cleanup_backup_path || '/runtime/tempmail_email_backups.db',
      ).trim() || '/runtime/tempmail_email_backups.db'
      values.proxy_pool_cooldown_enabled = parseBooleanConfigValue(values.proxy_pool_cooldown_enabled)
      values.task_proxy_mode = String(values.task_proxy_mode || 'dynamic').trim().toLowerCase() || 'dynamic'
      if (!['dynamic', 'pool', 'specified', 'direct'].includes(values.task_proxy_mode)) {
        values.task_proxy_mode = 'dynamic'
      }
      values.task_proxy_url = String(values.task_proxy_url || '').trim()
      values.task_proxy_country_code = String(values.task_proxy_country_code || '').trim().toUpperCase().slice(0, 2)
      values.task_proxy_failover = parseBooleanConfigValue(values.task_proxy_failover)
      values.task_proxy_max_candidates = String(
        Math.max(1, Math.min(100, Number.parseInt(String(values.task_proxy_max_candidates || '5'), 10) || 5)),
      )
      values.task_proxy_min_score = String(
        Math.max(0, Math.min(100, Number.parseInt(String(values.task_proxy_min_score || '50'), 10) || 50)),
      )
      values.dynamic_proxy_template = String(values.dynamic_proxy_template || '').trim()
      const dynamicProxyCountry = String(values.dynamic_proxy_default_country || '').trim().toUpperCase()
      if (values.task_proxy_mode === 'dynamic') {
        if (!values.dynamic_proxy_template) {
          setActiveTab('register')
          message.error('动态代理模式必须填写动态代理模板')
          return
        }
        if (!/^[A-Z]{2}$/.test(dynamicProxyCountry)) {
          setActiveTab('register')
          message.error('动态代理出口国家必须填写两位 ISO 国家码，例如 JP')
          return
        }
        // 隐藏字段仍会被 Ant Design 保留；动态模式保存必须主动清掉它们，
        // 否则历史 task_proxy_* 会再次覆盖 canonical dynamic 配置。
        values.task_proxy_url = ''
        values.task_proxy_country_code = ''
      } else if (values.task_proxy_mode === 'specified' && !values.task_proxy_url) {
        setActiveTab('register')
        message.error('手动指定代理模式必须填写指定代理地址')
        return
      }
      values.dynamic_proxy_default_country = dynamicProxyCountry || 'JP'
      values.dynamic_proxy_require_country_match = parseBooleanConfigValue(values.dynamic_proxy_require_country_match)
      values.dynamic_proxy_probe_enabled = parseBooleanConfigValue(values.dynamic_proxy_probe_enabled)
      values.dynamic_proxy_probe_timeout_seconds = String(
        Math.max(
          2,
          Math.min(60, Number.parseInt(String(values.dynamic_proxy_probe_timeout_seconds || '8'), 10) || 8),
        ),
      )
      values.dynamic_proxy_ip_retention_minutes = String(
        Math.max(
          1,
          Math.min(1440, Number.parseInt(String(values.dynamic_proxy_ip_retention_minutes || '5'), 10) || 5),
        ),
      )
      values.tempmail_mode = values.tempmail_mode || 'fixed_domain'
      values.email_api_lines = String(values.email_api_lines || '').trim()
      values.email_api_poll_interval_seconds = String(values.email_api_poll_interval_seconds || '3').trim() || '3'
      values.email_api_request_timeout_seconds = String(values.email_api_request_timeout_seconds || '15').trim() || '15'
      values.chatgpt_register_otp_wait_seconds = String(
        Math.max(
          30,
          Math.min(3600, Number.parseInt(String(values.chatgpt_register_otp_wait_seconds || '120'), 10) || 120),
        ),
      )
      values.chatgpt_register_otp_resend_wait_seconds = String(
        Math.max(
          30,
          Math.min(3600, Number.parseInt(String(values.chatgpt_register_otp_resend_wait_seconds || '90'), 10) || 90),
        ),
      )
      values.chatgpt_register_otp_account_budget_seconds = String(
        Math.max(
          30,
          Math.min(7200, Number.parseInt(String(values.chatgpt_register_otp_account_budget_seconds || '210'), 10) || 210),
        ),
      )
      values.email_api_gmail_dot_variant_enabled = parseBooleanConfigValue(values.email_api_gmail_dot_variant_enabled)
      values.email_api_gmail_variant_count = String(
        Math.max(1, Math.min(500, Number.parseInt(String(values.email_api_gmail_variant_count || '2'), 10) || 2)),
      )
      values.email_api_gmail_variant_rules = String(values.email_api_gmail_variant_rules || 'all').trim() || 'all'
      values.email_api_gmail_plus_tag_template = String(values.email_api_gmail_plus_tag_template || 'r{rand}').trim() || 'r{rand}'
      values.email_api_default_scheme = String(values.email_api_default_scheme || 'https').trim() || 'https'
      values.contribution_enabled = parseBooleanConfigValue(values.contribution_enabled)
      values.chatgpt_enable_team_invite = parseBooleanConfigValue(values.chatgpt_enable_team_invite)
      values.chatgpt_team_invite_deferred_activation = parseBooleanConfigValue(values.chatgpt_team_invite_deferred_activation)
      values.chatgpt_capture_free_workspace = parseBooleanConfigValue(values.chatgpt_capture_free_workspace)
      values.chatgpt_capture_business_workspace = parseBooleanConfigValue(values.chatgpt_capture_business_workspace)
      Object.assign(values, buildChatGPTK12ConfigData(values))
      values.chatgpt_gopay_billing_llm_enabled = parseBooleanConfigValue(values.chatgpt_gopay_billing_llm_enabled)
      values.chatgpt_access_token_only_checkout_amount_check_enabled = parseBooleanConfigValue(
        values.chatgpt_access_token_only_checkout_amount_check_enabled,
      )
      values.chatgpt_access_token_only_zero_amount_stop_enabled = parseBooleanConfigValue(
        values.chatgpt_access_token_only_zero_amount_stop_enabled,
      )
      values.chatgpt_access_token_only_gopay_provider_link_enabled = parseBooleanConfigValue(
        values.chatgpt_access_token_only_gopay_provider_link_enabled,
      )
      values.chatgpt_resume_auth_allow_phone_verification = parseBooleanConfigValue(
        values.chatgpt_resume_auth_allow_phone_verification,
      )
      values.chatgpt_resume_auth_allow_add_phone_verification = parseBooleanConfigValue(
        values.chatgpt_resume_auth_allow_add_phone_verification,
      )
      values.chatgpt_resume_auth_allow_existing_phone_verification = parseBooleanConfigValue(
        values.chatgpt_resume_auth_allow_existing_phone_verification,
      )
      values.chatgpt_recheck_allow_existing_phone_verification = parseBooleanConfigValue(
        values.chatgpt_recheck_allow_existing_phone_verification,
      )
      values.local_phone_gateway_auto_acquire_enabled = parseBooleanConfigValue(values.local_phone_gateway_auto_acquire_enabled)
      values.external_subscription_api_enabled = parseBooleanConfigValue(values.external_subscription_api_enabled)
      values.external_subscription_api_token = String(values.external_subscription_api_token || '').trim()
      values.external_access_token_api_enabled = parseBooleanConfigValue(values.external_access_token_api_enabled)
      values.external_access_token_api_token = String(values.external_access_token_api_token || '').trim()
      values.external_access_token_allow_refresh = parseBooleanConfigValue(values.external_access_token_allow_refresh)
      values.external_access_token_default_lease_seconds = String(
        Math.max(
          60,
          Number.parseInt(String(values.external_access_token_default_lease_seconds || '86400'), 10) || 86400,
        ),
      )
      values.external_access_token_max_limit = String(
        Math.max(
          1,
          Math.min(100, Number.parseInt(String(values.external_access_token_max_limit || '50'), 10) || 50),
        ),
      )
      values.external_access_token_precheck_cooldown_seconds = String(
        Math.max(
          60,
          Number.parseInt(String(values.external_access_token_precheck_cooldown_seconds || '600'), 10) || 600,
        ),
      )
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
      values.local_phone_gateway_max_resend_attempts = String(values.local_phone_gateway_max_resend_attempts || '20').trim() || '20'
      values.local_phone_gateway_resend_interval_seconds = String(values.local_phone_gateway_resend_interval_seconds || '30').trim() || '30'

      await apiFetch('/config', {
        method: 'PUT',
        body: JSON.stringify({
          data: values,
          base_revision: shareState?.enabled ? shareState?.shared?.revision : undefined,
        }),
      })
      if (shareState?.enabled) {
        await loadShareState().catch(() => undefined)
      }
      form.setFieldsValue({
        cfworker_domains: domains,
        cfworker_enabled_domains: enabledDomains,
        cfworker_domain: domains.length > 0 ? '' : values.cfworker_domain,
        cfworker_random_subdomain: values.cfworker_random_subdomain,
        tempmail_permanent: values.tempmail_permanent,
        mail_provider: values.mail_provider,
        icloud_hme_mode: values.icloud_hme_mode,
        icloud_hme_auto_create_enabled: values.icloud_hme_auto_create_enabled,
        icloud_hme_auto_create_stock_limit: values.icloud_hme_auto_create_stock_limit,
        icloud_hme_auto_create_interval_min_minutes: values.icloud_hme_auto_create_interval_min_minutes,
        icloud_hme_auto_create_interval_max_minutes: values.icloud_hme_auto_create_interval_max_minutes,
        icloud_hme_auto_create_rate_limit_backoff_minutes: values.icloud_hme_auto_create_rate_limit_backoff_minutes,
        icloud_hme_auto_create_error_backoff_minutes: values.icloud_hme_auto_create_error_backoff_minutes,
        icloud_hme_auto_delete_enabled: values.icloud_hme_auto_delete_enabled,
        icloud_hme_auto_delete_recheck_before_delete: values.icloud_hme_auto_delete_recheck_before_delete,
        icloud_hme_auto_delete_pause_active_tasks: values.icloud_hme_auto_delete_pause_active_tasks,
        icloud_hme_auto_delete_account_interval_min_minutes: values.icloud_hme_auto_delete_account_interval_min_minutes,
        icloud_hme_auto_delete_account_interval_max_minutes: values.icloud_hme_auto_delete_account_interval_max_minutes,
        icloud_hme_auto_delete_max_per_run: values.icloud_hme_auto_delete_max_per_run,
        icloud_hme_auto_delete_rate_limit_backoff_minutes: values.icloud_hme_auto_delete_rate_limit_backoff_minutes,
        icloud_hme_auto_delete_error_backoff_minutes: values.icloud_hme_auto_delete_error_backoff_minutes,
        icloud_hme_auto_delete_dead_statuses: values.icloud_hme_auto_delete_dead_statuses,
        tempmail_archive_cleanup_enabled: values.tempmail_archive_cleanup_enabled,
        tempmail_archive_cleanup_interval_minutes: values.tempmail_archive_cleanup_interval_minutes,
        tempmail_archive_cleanup_keep_recent_minutes: values.tempmail_archive_cleanup_keep_recent_minutes,
        tempmail_archive_cleanup_threshold: values.tempmail_archive_cleanup_threshold,
        tempmail_archive_cleanup_pause_active_tasks: values.tempmail_archive_cleanup_pause_active_tasks,
        tempmail_archive_cleanup_mailbox: values.tempmail_archive_cleanup_mailbox,
        tempmail_archive_cleanup_backup_path: values.tempmail_archive_cleanup_backup_path,
        task_proxy_mode: values.task_proxy_mode,
        task_proxy_url: values.task_proxy_url,
        task_proxy_country_code: values.task_proxy_country_code,
        task_proxy_failover: values.task_proxy_failover,
        task_proxy_max_candidates: values.task_proxy_max_candidates,
        task_proxy_min_score: values.task_proxy_min_score,
        dynamic_proxy_template: values.dynamic_proxy_template,
        dynamic_proxy_default_country: values.dynamic_proxy_default_country,
        dynamic_proxy_require_country_match: values.dynamic_proxy_require_country_match,
        dynamic_proxy_probe_enabled: values.dynamic_proxy_probe_enabled,
        dynamic_proxy_probe_timeout_seconds: values.dynamic_proxy_probe_timeout_seconds,
        dynamic_proxy_ip_retention_minutes: values.dynamic_proxy_ip_retention_minutes,
        contribution_enabled: values.contribution_enabled,
        chatgpt_enable_team_invite: values.chatgpt_enable_team_invite,
        chatgpt_team_invite_deferred_activation: values.chatgpt_team_invite_deferred_activation,
        chatgpt_capture_free_workspace: values.chatgpt_capture_free_workspace,
        chatgpt_capture_business_workspace: values.chatgpt_capture_business_workspace,
        chatgpt_k12_enabled: values.chatgpt_k12_enabled,
        chatgpt_k12_workspace_ids: values.chatgpt_k12_workspace_ids,
        chatgpt_k12_save_all_spaces: values.chatgpt_k12_save_all_spaces,
        chatgpt_k12_strict_join: values.chatgpt_k12_strict_join,
        chatgpt_k12_join_timeout_seconds: values.chatgpt_k12_join_timeout_seconds,
        chatgpt_k12_join_retry_count: values.chatgpt_k12_join_retry_count,
        chatgpt_k12_post_join_poll_seconds: values.chatgpt_k12_post_join_poll_seconds,
        chatgpt_k12_capture_refresh_tokens: values.chatgpt_k12_capture_refresh_tokens,
        chatgpt_gopay_billing_llm_enabled: values.chatgpt_gopay_billing_llm_enabled,
        chatgpt_access_token_only_checkout_amount_check_enabled: values.chatgpt_access_token_only_checkout_amount_check_enabled,
        chatgpt_access_token_only_checkout_country: values.chatgpt_access_token_only_checkout_country,
        chatgpt_access_token_only_checkout_currency: values.chatgpt_access_token_only_checkout_currency,
        chatgpt_access_token_only_zero_amount_stop_enabled: values.chatgpt_access_token_only_zero_amount_stop_enabled,
        chatgpt_access_token_only_zero_amount_stop_threshold: values.chatgpt_access_token_only_zero_amount_stop_threshold,
        chatgpt_access_token_only_gopay_provider_link_enabled: values.chatgpt_access_token_only_gopay_provider_link_enabled,
        chatgpt_resume_auth_allow_phone_verification: values.chatgpt_resume_auth_allow_phone_verification,
        chatgpt_resume_auth_allow_add_phone_verification: values.chatgpt_resume_auth_allow_add_phone_verification,
        chatgpt_resume_auth_allow_existing_phone_verification: values.chatgpt_resume_auth_allow_existing_phone_verification,
        chatgpt_recheck_allow_existing_phone_verification: values.chatgpt_recheck_allow_existing_phone_verification,
        existing_phone_otp_timeout_seconds: values.existing_phone_otp_timeout_seconds,
        existing_phone_otp_poll_interval_seconds: values.existing_phone_otp_poll_interval_seconds,
        existing_phone_otp_max_resend_attempts: values.existing_phone_otp_max_resend_attempts,
        existing_phone_otp_resend_interval_seconds: values.existing_phone_otp_resend_interval_seconds,
        chatgpt_subscription_auth_capture_retry_delays_seconds: values.chatgpt_subscription_auth_capture_retry_delays_seconds,
        chatgpt_phone_verification_provider: values.chatgpt_phone_verification_provider,
        local_phone_gateway_url: values.local_phone_gateway_url,
        local_phone_gateway_token: values.local_phone_gateway_token,
        local_phone_gateway_service_alias: values.local_phone_gateway_service_alias,
        local_phone_gateway_auto_acquire_enabled: values.local_phone_gateway_auto_acquire_enabled,
        local_phone_gateway_timeout_seconds: values.local_phone_gateway_timeout_seconds,
        local_phone_gateway_poll_interval_seconds: values.local_phone_gateway_poll_interval_seconds,
        local_phone_gateway_max_attempts: values.local_phone_gateway_max_attempts,
        local_phone_gateway_max_resend_attempts: values.local_phone_gateway_max_resend_attempts,
        local_phone_gateway_resend_interval_seconds: values.local_phone_gateway_resend_interval_seconds,
        external_subscription_api_enabled: values.external_subscription_api_enabled,
        external_subscription_api_token: values.external_subscription_api_token,
        external_access_token_api_enabled: values.external_access_token_api_enabled,
        external_access_token_api_token: values.external_access_token_api_token,
        external_access_token_allow_refresh: values.external_access_token_allow_refresh,
        external_access_token_default_lease_seconds: values.external_access_token_default_lease_seconds,
        external_access_token_max_limit: values.external_access_token_max_limit,
        external_access_token_precheck_cooldown_seconds: values.external_access_token_precheck_cooldown_seconds,
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
    activeTab === 'mailbox'
      ? orderMailboxSections(visibleSections, selectedMailProvider)
      : activeTab === 'chatgpt'
        ? orderPinnedSections(visibleSections, normalizedChatgptPinnedSections)
        : visibleSections
  const chatgptPinGroups =
    activeTab === 'chatgpt'
      ? (() => {
          const sectionByTitle = new Map(visibleSections.map((section) => [section.title, section]))
          const usedTitles = new Set<string>()
          const groups = CHATGPT_PIN_GROUPS
            .map((group) => {
              const sections = group.titles
                .map((title) => sectionByTitle.get(title))
                .filter((section): section is SectionConfig => Boolean(section))
              sections.forEach((section) => usedTitles.add(section.title))
              return { label: group.label, sections }
            })
            .filter((group) => group.sections.length > 0)
          const restSections = visibleSections.filter((section) => !usedTitles.has(section.title))
          if (restSections.length > 0) {
            groups.push({ label: '其他', sections: restSections })
          }
          return groups
        })()
      : []
  const toggleChatgptPinnedSection = (sectionTitle: string, checked: boolean) => {
    setChatgptPinnedSections((prev) => {
      const withoutCurrent = prev.filter((title) => title !== sectionTitle)
      return checked ? [...withoutCurrent, sectionTitle] : withoutCurrent
    })
  }
  const getMailboxSectionCollapseState = (sectionTitle: string) => {
    if (activeTab !== 'mailbox' || selectedMailProvider !== 'icloud_hme') {
      return { defaultCollapsed: false, autoExpand: false }
    }
    if (sectionTitle === 'iCloud HME 自动补池') {
      return { defaultCollapsed: true, autoExpand: icloudAutoCreateEnabled }
    }
    if (sectionTitle === 'iCloud HME 自动删除') {
      return { defaultCollapsed: true, autoExpand: icloudAutoDeleteEnabled }
    }
    if (sectionTitle === 'TempMail 归档清理') {
      return { defaultCollapsed: true, autoExpand: tempmailArchiveCleanupEnabled }
    }
    return { defaultCollapsed: false, autoExpand: false }
  }

  return (
    <div style={{ display: 'flex', flexDirection: 'column', gap: 16 }}>
      <div>
        <h1 style={{ fontSize: 24, fontWeight: 'bold', margin: 0 }}>全局配置</h1>
        <p style={{ color: '#7a8ba3', marginTop: 4 }}>配置将持久化保存，注册任务自动使用</p>
      </div>

      {configLoadError ? (
        <Alert
          type="error"
          showIcon
          message="配置加载失败，已阻止保存"
          description={`${configLoadError}。如果刚重启过服务，请重新登录后刷新页面；不要在空表单状态下保存。`}
        />
      ) : !configLoaded ? (
        <Alert type="info" showIcon message="正在加载配置" description="加载完成前暂不允许保存，避免空表单覆盖现有配置。" />
      ) : null}

      <Card size="small">
        <div style={{ display: 'flex', alignItems: 'center', justifyContent: 'space-between', gap: 12, flexWrap: 'wrap' }}>
          <Space direction="vertical" size={2}>
            <Space size={8} wrap>
              <Typography.Text strong>配置共享</Typography.Text>
              <Tag color={shareState?.enabled ? 'green' : 'default'}>
                {shareState?.enabled ? '共享模式' : '本地模式'}
              </Tag>
              <Tag>当前实例：{shareState?.instance_id || '-'}</Tag>
              <Tag>共享版本：rev {shareState?.shared?.revision ?? 0}</Tag>
            </Space>
            <Typography.Text type="secondary">
              {shareState?.enabled
                ? `保存本页会更新共享模板，并影响所有开启共享的实例；最后更新：${shareState?.shared?.updated_by || '-'} / ${shareState?.shared?.updated_at || '-'}`
                : `当前实例只使用本地配置；脱离基线：rev ${shareState?.baseline_revision || '0'}，脱离时间：${shareState?.detached_at || '-'}`}
            </Typography.Text>
            <Typography.Text type="secondary">
              本地保留不共享：CLIProxyAPI、外部分发 API Token、GoPay 近期运行态等实例专属配置。
            </Typography.Text>
          </Space>
          <Space size={8} wrap>
            <Switch
              checked={Boolean(shareState?.enabled)}
              checkedChildren="共享"
              unCheckedChildren="本地"
              loading={shareBusy}
              onChange={toggleShareMode}
            />
            <Button size="small" icon={<SyncOutlined />} loading={shareBusy} onClick={() => loadShareState()}>
              刷新状态
            </Button>
            <Button size="small" loading={shareBusy} onClick={pullSharedConfig}>
              从共享拉取
            </Button>
            <Button size="small" loading={shareBusy} onClick={showShareDiff}>
              查看差异
            </Button>
            <Button size="small" danger loading={shareBusy} disabled={Boolean(shareState?.enabled)} onClick={pushLocalConfigToShared}>
              本实例推送为共享模板
            </Button>
          </Space>
        </div>
      </Card>

      <div
        className="settings-body"
        style={{
          display: 'flex',
          flexDirection: isMobile ? 'column' : 'row',
          gap: isMobile ? 12 : 18,
          minWidth: 0,
        }}
      >
        <div
          className={`settings-tab-nav ${isMobile ? 'settings-tab-nav-mobile' : 'settings-tab-nav-desktop'}`}
          style={{
            width: isMobile ? '100%' : 'max-content',
            minWidth: isMobile ? 0 : 'max-content',
            flexShrink: 0,
          }}
        >
          {isMobile ? (
            <div className="settings-mobile-tab-grid" role="tablist" aria-label="全局配置分组">
              {TAB_ITEMS.map((t) => {
                const selected = t.key === activeTab
                return (
                  <button
                    key={t.key}
                    type="button"
                    role="tab"
                    aria-selected={selected}
                    className={`settings-mobile-tab ${selected ? 'is-active' : ''}`}
                    onClick={() => setActiveTab(t.key)}
                  >
                    <span className="settings-tab-label">
                      {t.icon}
                      <span>{t.label}</span>
                    </span>
                  </button>
                )
              })}
            </div>
          ) : (
            <Tabs
              className="settings-tabs-desktop"
              tabPosition="left"
              activeKey={activeTab}
              onChange={setActiveTab}
              items={TAB_ITEMS.map((t) => ({
                key: t.key,
                label: (
                  <span className="settings-tab-label">
                    {t.icon}
                    <span>{t.label}</span>
                  </span>
                ),
              }))}
            />
          )}
        </div>

        <div className="settings-main" style={{ flex: 1, minWidth: 0 }}>
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
                  {activeTab === 'mailbox' ? (
                    <MailboxOverviewPanel form={form} selectedProvider={selectedMailProvider} visibleSections={orderedVisibleSections} />
                  ) : null}
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
                      <Card size="small" className="settings-chatgpt-toolbar" style={{ marginBottom: 16 }}>
                        <div className="settings-chatgpt-toolbar-head">
                          <div className="settings-chatgpt-toolbar-copy">
                            <Space size={8} wrap>
                              <Typography.Text strong>ChatGPT 配置面板</Typography.Text>
                              <Tag color={normalizedChatgptPinnedSections.length > 0 ? 'blue' : 'default'}>
                                已置顶 {normalizedChatgptPinnedSections.length}
                              </Tag>
                            </Space>
                            <Typography.Text type="secondary">
                              常用面板可置顶，下面的配置面板默认折叠。
                            </Typography.Text>
                          </div>
                          <Space size={8} wrap className="settings-chatgpt-toolbar-actions">
                            {normalizedChatgptPinnedSections.length > 0 ? (
                              <Button size="small" onClick={() => setChatgptPinnedSections([])}>
                                清空置顶
                              </Button>
                            ) : null}
                            <Button
                              size="small"
                              onClick={() => setChatgptPinEditorOpen((value) => !value)}
                            >
                              {chatgptPinEditorOpen ? '收起置顶' : '编辑置顶'}
                            </Button>
                            <Button
                              size="small"
                              type="primary"
                              icon={<SaveOutlined />}
                              onClick={save}
                              loading={saving}
                            >
                              {saved ? '已保存 ✓' : '保存配置'}
                            </Button>
                          </Space>
                        </div>
                        {!chatgptPinEditorOpen && normalizedChatgptPinnedSections.length > 0 ? (
                          <div className="settings-chatgpt-pinned-summary">
                            <span className="settings-chatgpt-pin-label">当前置顶</span>
                            <div className="settings-chatgpt-pin-chips">
                              {normalizedChatgptPinnedSections.map((title) => (
                                <Tag key={title} className="settings-chatgpt-pin-chip settings-chatgpt-pin-chip-static">
                                  {title}
                                </Tag>
                              ))}
                            </div>
                          </div>
                        ) : null}
                        {chatgptPinEditorOpen ? (
                          <div className="settings-chatgpt-pin-groups">
                            {chatgptPinGroups.map((group) => (
                              <div key={group.label} className="settings-chatgpt-pin-group">
                                <span className="settings-chatgpt-pin-label">{group.label}</span>
                                <div className="settings-chatgpt-pin-chips">
                                  {group.sections.map((section) => {
                                    const checked = normalizedChatgptPinnedSections.includes(section.title)
                                    return (
                                      <Tag.CheckableTag
                                        key={section.title}
                                        checked={checked}
                                        onChange={(nextChecked) => toggleChatgptPinnedSection(section.title, nextChecked)}
                                        className="settings-chatgpt-pin-chip"
                                      >
                                        {section.title}
                                      </Tag.CheckableTag>
                                    )
                                  })}
                                </div>
                              </div>
                            ))}
                          </div>
                        ) : null}
                      </Card>
                    </>
                  ) : null}
                  {orderedVisibleSections.map((section) => (
                    (() => {
                      const mailboxCollapseState = getMailboxSectionCollapseState(section.title)
                      return (
                        <ConfigSection
                          key={`${activeTab}:${section.title}`}
                          section={section}
                          fields={
                            section.title === TASK_PROXY_SECTION_TITLE
                              ? taskProxyFieldsForMode(section.fields, taskProxyMode, taskProxyFailover)
                              : undefined
                          }
                          defaultCollapsed={activeTab === 'chatgpt' || mailboxCollapseState.defaultCollapsed}
                          autoExpand={
                            (activeTab === 'chatgpt' && normalizedChatgptPinnedSections.includes(section.title))
                            || mailboxCollapseState.autoExpand
                          }
                        />
                      )
                    })()
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
