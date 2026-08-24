import { useEffect, useRef, useState } from 'react'
import { App, Card, Form, Input, InputNumber, Select, Button, message, Tabs, Space, Tag, Typography, Modal, QRCode, Switch, Alert, Grid, Spin } from 'antd'
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
  CloudUploadOutlined,
  PlusOutlined,
  LockOutlined,
  PushpinOutlined,
  ThunderboltOutlined,
} from '@ant-design/icons'
import { parseBooleanConfigValue } from '@/lib/configValueParsers'
import { apiFetch, invalidateSession, setToken } from '@/lib/utils'
import { formatBeijingDateTime } from '@/lib/dateTime'

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

const DEFAULT_TEMPMAIL_API_URL = 'http://tempmail-api-1:8080'
const DEFAULT_HME_READY_API_URL = 'http://172.20.0.1:18765'
const DEFAULT_OAIPAY_API_URL = 'http://gpt-cccy-me:8789'

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
    { label: 'HME Ready API + TempMail（iCloud Helper）', value: 'hme_ready_api' },
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
  default_executor: [
    { label: 'API 协议（无浏览器）', value: 'protocol' },
    { label: '无头浏览器', value: 'headless' },
    { label: '有头浏览器', value: 'headed' },
  ],
  default_browser_family: [
    { label: '随机（Chrome / Firefox / Safari）', value: 'random' },
    { label: 'Chrome（协议 / Patchright 深浏览器）', value: 'chrome' },
    { label: 'Firefox（协议 / Camoufox 深浏览器）', value: 'firefox' },
    { label: 'Safari（curl_cffi 协议画像）', value: 'safari' },
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
  dynamic_proxy_provider: [
    { label: 'Cliproxy', value: 'cliproxy' },
    { label: 'MiyaIP', value: 'miyaip' },
  ],
  miyaip_gateway_server: [
    { label: '美洲网关', value: 'us' },
    { label: '亚洲网关', value: 'as' },
    { label: '欧洲网关', value: 'eu' },
  ],
  miyaip_protocol: [
    { label: 'HTTP', value: 'http' },
    { label: 'SOCKS5', value: 'socks5' },
  ],
  codex_proxy_upload_type: [
    { label: 'AT（Access Token，推荐）', value: 'at' },
    { label: 'RT（Refresh Token）', value: 'rt' },
  ],
  chatgpt_phone_verification_provider: [
    { label: 'SMSToMe 号码池', value: 'smstome' },
    { label: '本地接码网关', value: 'local_gateway' },
  ],
  chatgpt_runtime_browser_capacity_mode: [
    { label: '自适应资源门禁', value: 'adaptive' },
    { label: '固定并发上限', value: 'fixed' },
  ],
  chatgpt_runtime_solver_mode: [
    { label: '按请求自动扩缩', value: 'auto' },
    { label: '固定暖机池', value: 'fixed' },
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
          { key: 'default_browser_family', label: '默认浏览器指纹族', type: 'select' },
        ],
      },
      {
        title: '注册并发与浏览器资源',
        desc: '仅当前实例生效；浏览器按需创建，并受 PID、内存和 CPU 压力门禁。',
        fields: [
          { key: 'chatgpt_register_browser_default_concurrency', label: '浏览器注册默认并发', type: 'number', min: 1, precision: 0 },
          { key: 'chatgpt_register_browser_max_concurrency', label: '浏览器注册最大并发', type: 'number', min: 1, precision: 0 },
          { key: 'chatgpt_register_delay_seconds', label: '注册最小启动延时（秒）', type: 'number', min: 0, max: 3600, precision: 2, step: 0.5 },
          { key: 'chatgpt_register_delay_max_seconds', label: '注册最大启动延时（秒）', type: 'number', min: 0, max: 3600, precision: 2, step: 0.5 },
          { key: 'chatgpt_runtime_browser_capacity_mode', label: '浏览器容量模式', type: 'select' },
          { key: 'chatgpt_runtime_auth_browser_max_concurrency', label: 'Auth / 注册浏览器上限', type: 'number', min: 1, precision: 0 },
          { key: 'chatgpt_runtime_auth_browser_registration_reserve', label: '注册保留浏览器槽位', type: 'number', min: 0, precision: 0 },
          { key: 'chatgpt_runtime_auth_browser_recheck_reserve', label: '失效测活保留槽位', type: 'number', min: 0, precision: 0 },
          { key: 'chatgpt_web_session_hold_max_sessions', label: '登录态保持浏览器上限', type: 'number', min: 1, max: 32, precision: 0 },
          { key: 'chatgpt_runtime_auth_browser_launch_interval_seconds', label: '浏览器启动错峰（秒）', type: 'number', min: 0, max: 60, precision: 2, step: 0.5 },
          { key: 'chatgpt_runtime_auth_browser_pid_budget', label: '单次启动 PID 预算', type: 'number', min: 0, max: 4096, precision: 0 },
          { key: 'chatgpt_runtime_pid_emergency_reserve', label: 'PID 应急保留', type: 'number', min: 0, max: 4096, precision: 0 },
          { key: 'chatgpt_runtime_host_memory_reserve_mib', label: '宿主机内存保留（MiB）', type: 'number', min: 0, max: 262144, precision: 0 },
          { key: 'chatgpt_runtime_cpu_psi_avg10_limit', label: 'CPU PSI avg10 暂停阈值（%）', type: 'number', min: 0, max: 100, precision: 2, step: 0.5 },
          { key: 'chatgpt_runtime_registration_transition_timeout_seconds', label: '注册页面状态等待（秒）', type: 'number', min: 20, max: 120, precision: 0 },
        ],
      },
      {
        title: 'Solver 浏览器池',
        desc: 'Solver 服务常驻，浏览器按请求扩容并在空闲超时后回收。',
        fields: [
          { key: 'chatgpt_runtime_solver_mode', label: 'Solver 浏览器模式', type: 'select' },
          { key: 'chatgpt_runtime_solver_warm_browsers', label: 'Solver 暖浏览器', type: 'number', min: 0, max: 15, precision: 0 },
          { key: 'chatgpt_runtime_solver_max_browsers', label: 'Solver 最大浏览器', type: 'number', min: 1, max: 15, precision: 0 },
          { key: 'chatgpt_runtime_solver_idle_timeout_seconds', label: 'Solver 空闲回收（秒）', type: 'number', min: 30, max: 86400, precision: 0 },
        ],
      },
      {
        title: '账号网络默认出口',
        desc: '动态模式可选 Cliproxy 或 MiyaIP，两套渠道配置独立保存；指定代理和代理池字段仅在对应模式显示。单项任务显式传代理时仍可覆盖本次任务。',
        fields: [
          { key: 'task_proxy_mode', label: '默认出口模式', type: 'select' },
          { key: 'task_proxy_url', label: '指定代理地址', secret: true, placeholder: 'http:// 或 socks5://...' },
          { key: 'task_proxy_country_code', label: '候选出口国家', placeholder: 'JP（可留空）' },
          { key: 'task_proxy_failover', label: '失败后更换线路', type: 'boolean' },
          { key: 'task_proxy_max_candidates', label: '代理池候选数量', placeholder: '5' },
          { key: 'task_proxy_min_score', label: '代理池最低健康分', placeholder: '50' },
          { key: 'dynamic_proxy_provider', label: '动态代理渠道', type: 'select' },
          { key: 'dynamic_proxy_template', label: 'Cliproxy 动态节点地址', secret: true, placeholder: 'http:// 或 socks5://user:pass@host:port' },
          { key: 'miyaip_crc', label: '代理密码', secret: true, placeholder: 'Proxy password' },
          { key: 'miyaip_key_name', label: '主 Key', secret: true, placeholder: 'mainKey / mobileMainKey' },
          { key: 'miyaip_pool', label: '线路池', type: 'number', min: 1, max: 999999, precision: 0 },
          { key: 'miyaip_gateway_server', label: '接入网关', type: 'select' },
          { key: 'miyaip_protocol', label: '代理协议', type: 'select' },
          { key: 'miyaip_request_timeout_seconds', label: '接口超时（秒）', type: 'number', min: 2, max: 60, precision: 0 },
          { key: 'dynamic_proxy_default_country', label: '动态代理出口国家', placeholder: 'JP' },
          { key: 'dynamic_proxy_ip_retention_minutes', label: 'IP 保留分钟数（t-N）', placeholder: '5' },
          { key: 'dynamic_proxy_require_country_match', label: '要求实测国家匹配', type: 'boolean' },
          { key: 'dynamic_proxy_probe_enabled', label: '运行前探测出口', type: 'boolean' },
          { key: 'dynamic_proxy_probe_timeout_seconds', label: '探测超时秒数', placeholder: '8' },
        ],
      },
      {
        title: '本地状态同步默认参数',
        desc: '账号页的“同步本地状态”只启动任务，网络出口统一使用上方全局模式；并发、独立出口和账号间延时在这里统一配置。',
        fields: [
          { key: 'chatgpt_local_status_probe_concurrency', label: '同步并发账号数', placeholder: '1' },
          { key: 'chatgpt_local_status_probe_unique_exit_ip_enabled', label: '要求独立出口 IP', type: 'boolean' },
          { key: 'chatgpt_local_status_probe_delay_seconds', label: '最小账号间延时（秒）', placeholder: '0' },
          { key: 'chatgpt_local_status_probe_delay_max_seconds', label: '最大账号间延时（秒）', placeholder: '0' },
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
          { key: 'tempmail_api_url', label: 'API URL', placeholder: DEFAULT_TEMPMAIL_API_URL },
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
        desc: 'Helper 负责出池、身份和 finalize；auto-gpt 直接从 TempMail 转发箱读取验证码',
        fields: [
          { key: 'icloud_forward_to', label: '转发目标邮箱', placeholder: 'b@666800.xyz', type: 'stringList' },
          { key: 'icloud_hme_helper_api_url', label: 'Helper API URL', placeholder: DEFAULT_HME_READY_API_URL },
          { key: 'icloud_hme_helper_internal_key', label: 'Helper Internal Key', secret: true },
          { key: 'icloud_hme_helper_api_key_header', label: 'Helper 鉴权 Header', placeholder: 'X-Internal-Key' },
          { key: 'icloud_hme_helper_consumer', label: 'Helper Consumer', placeholder: 'auto-gpt/chatgpt_register' },
          { key: 'icloud_hme_helper_checkout_ttl_seconds', label: 'Helper lease TTL 秒', placeholder: '10800' },
          { key: 'icloud_hme_helper_wait_timeout_seconds', label: 'Helper 等码超时秒', placeholder: '300' },
          { key: 'icloud_hme_helper_max_cache_age_seconds', label: 'Helper 缓存有效秒', placeholder: '86400' },
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
        title: '账号登录凭据',
        desc: '配置手机号注册、手机号登录和已有账号补抓认证时使用的默认密码',
        fields: [
          { key: 'chatgpt_phone_signup_password', label: '手机号注册/登录固定密码', secret: true, placeholder: '新手机号注册和已注册手机号登录共用' },
          { key: 'chatgpt_existing_account_login_password', label: '已有账号抓 auth 默认密码', secret: true, placeholder: '可留空，任务里仍可临时覆盖' },
        ],
      },
      {
        title: '支付长链服务',
        desc: '跨服务器服务连接',
        fields: [
          { key: 'openai_pay_long_link_base_url', label: '服务地址', placeholder: 'https://pay.cccy.me' },
          { key: 'openai_pay_long_link_api_key', label: 'API Key', secret: true, placeholder: 'opll_live_...' },
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
        desc: '一键将账号推送到 OAIPay',
        fields: [
          { key: 'oaipay_api_url', label: 'API URL', placeholder: DEFAULT_OAIPAY_API_URL },
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
  type?: 'select' | 'input' | 'number' | 'boolean' | 'textarea' | 'stringList'
  secret?: boolean
  disabled?: boolean
  min?: number
  max?: number
  precision?: number
  step?: number
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
const PAYMENT_LINK_SERVICE_SECTION_TITLE = '支付长链服务'
const MIYAIP_FIELD_KEYS = new Set([
  'miyaip_crc',
  'miyaip_key_name',
  'miyaip_pool',
  'miyaip_gateway_server',
  'miyaip_protocol',
  'miyaip_request_timeout_seconds',
])

// Settings is a large snapshot form, but proxy defaults are also edited from
// the dedicated dynamic-node page. Only fields actually touched in this form
// may be sent back, otherwise an old tab can overwrite a newer shared value.
const TASK_PROXY_CONFIG_KEYS = [
  'task_proxy_mode',
  'task_proxy_url',
  'task_proxy_country_code',
  'task_proxy_failover',
  'task_proxy_max_candidates',
  'task_proxy_min_score',
  'dynamic_proxy_provider',
  'dynamic_proxy_template',
  'miyaip_crc',
  'miyaip_key_name',
  'miyaip_pool',
  'miyaip_gateway_server',
  'miyaip_protocol',
  'miyaip_request_timeout_seconds',
  'dynamic_proxy_default_country',
  'dynamic_proxy_require_country_match',
  'dynamic_proxy_probe_enabled',
  'dynamic_proxy_probe_timeout_seconds',
  'dynamic_proxy_ip_retention_minutes',
] as const

function taskProxyFieldsForMode(
  fields: FieldConfig[],
  rawMode: unknown,
  failoverEnabled: boolean,
  rawDynamicProvider: unknown,
): FieldConfig[] {
  const mode = String(rawMode || 'dynamic').trim().toLowerCase()
  const dynamicProvider = String(rawDynamicProvider || 'cliproxy').trim().toLowerCase() === 'miyaip'
    ? 'miyaip'
    : 'cliproxy'
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
    'dynamic_proxy_provider',
    ...(dynamicProvider === 'miyaip'
      ? [
          'miyaip_crc',
          'miyaip_key_name',
          'miyaip_pool',
          'miyaip_gateway_server',
          'miyaip_protocol',
          'miyaip_request_timeout_seconds',
        ]
      : ['dynamic_proxy_template', 'dynamic_proxy_ip_retention_minutes']),
    'dynamic_proxy_default_country',
    'task_proxy_failover',
    'dynamic_proxy_require_country_match',
    'dynamic_proxy_probe_enabled',
    'dynamic_proxy_probe_timeout_seconds',
  )
  const failover = byKey.get('task_proxy_failover')
  if (failover) {
    result[result.findIndex((field) => field.key === failover.key)] = {
      ...failover,
      label: '失败后更换线路',
    }
  }
  return result
}

const REGISTER_PINNED_SECTIONS_STORAGE_KEY = 'any-auto-register.settings.register.pinned-sections'
const CHATGPT_PINNED_SECTIONS_STORAGE_KEY = 'any-auto-register.settings.chatgpt.pinned-sections'

const REGISTER_PIN_GROUPS = [
  {
    label: '任务基础',
    titles: ['默认注册方式', '账号网络默认出口'],
  },
  {
    label: '运行资源',
    titles: ['注册并发与浏览器资源', 'Solver 浏览器池'],
  },
  {
    label: '状态同步',
    titles: ['本地状态同步默认参数'],
  },
]

const CHATGPT_PIN_GROUPS = [
  {
    label: '上传',
    titles: ['CPA 面板', 'Sub2API 面板', 'OAIPay 面板', 'CodexProxy'],
  },
  {
    label: '账号订阅',
    titles: ['账号登录凭据', '支付长链服务', '无 RT / Access Token Only', '外部 ChatGPT 分发 API'],
  },
  {
    label: '维护验证',
    titles: ['CPA 自动维护', '手机验证 / 接码服务'],
  },
]

function loadPinnedSections(storageKey: string): string[] {
  if (typeof window === 'undefined') return []
  try {
    const raw = window.localStorage.getItem(storageKey)
    const parsed = raw ? JSON.parse(raw) : []
    if (!Array.isArray(parsed)) return []
    return parsed.map((item) => String(item || '').trim()).filter(Boolean)
  } catch {
    return []
  }
}

function buildSectionPinGroups(
  sections: SectionConfig[],
  groupConfigs: { label: string, titles: string[] }[],
): { label: string, sections: SectionConfig[] }[] {
  const sectionByTitle = new Map(sections.map((section) => [section.title, section]))
  const usedTitles = new Set<string>()
  const groups = groupConfigs
    .map((group) => {
      const groupedSections = group.titles
        .map((title) => sectionByTitle.get(title))
        .filter((section): section is SectionConfig => Boolean(section))
      groupedSections.forEach((section) => usedTitles.add(section.title))
      return { label: group.label, sections: groupedSections }
    })
    .filter((group) => group.sections.length > 0)
  const restSections = sections.filter((section) => !usedTitles.has(section.title))
  if (restSections.length > 0) groups.push({ label: '其他', sections: restSections })
  return groups
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
    case 'TempMail 归档清理':
      return 'tempmail_local'
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
      return 'Helper 负责邮箱出池、身份和 finalize；auto-gpt 直接从 TempMail 转发箱读取验证码。'
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
              只展示当前邮箱服务相关配置；HME Ready API 的验证码由 auto-gpt 直接读取 TempMail 转发箱。
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
  } catch {
    // Legacy settings may store domains as a plain comma/newline list.
  }

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
        ? '动态模式开启后会在当前渠道内更换线路；手动指定代理开启后会回退到代理池候选。'
      : field.key === 'chatgpt_local_status_probe_unique_exit_ip_enabled'
        ? '按任务内真实出口 IP 去重；需要多个候选代理。直连模式或不可切换的单个指定代理无法满足该要求。'
      : field.key === 'task_proxy_max_candidates'
        ? '代理池或指定代理 failover 时最多尝试的候选数量。'
      : field.key === 'task_proxy_min_score'
        ? '代理池候选的最低健康分；低于此分数不会被默认账号网络动作选中。'
      : field.key === 'dynamic_proxy_provider'
        ? '动态任务只使用当前渠道，失败时不会跨渠道回退；切换渠道不会删除另一套已保存配置。'
      : field.key === 'dynamic_proxy_template'
      ? 'Cliproxy 渠道的全局动态节点。支持 region-JP/region-US 等固定国家，也支持 region-Rand；任务会按出口国家改写 region token 并更换 SID；展示和日志只保存脱敏地址。'
      : field.key === 'miyaip_crc'
        ? 'MiyaIP 后台显示的 Proxy password（接口参数 Crc），不是网站登录 Token。只用于生成运行代理，不会进入任务详情或日志。'
      : field.key === 'miyaip_key_name'
        ? '住宅代理填写 mainKey，移动代理填写 mobileMainKey（接口参数 KeyName）；它不是最终生成的代理用户名。'
      : field.key === 'miyaip_pool'
        ? 'MiyaIP 套餐线路池编号（接口参数 Pool），范围 1-999999。'
      : field.key === 'miyaip_gateway_server'
        ? 'Generate 请求使用的接入网关区域，与代理出口国家独立。'
      : field.key === 'miyaip_protocol'
        ? 'MiyaIP 生成的代理协议；SOCKS5 在运行时使用代理端 DNS 解析。'
      : field.key === 'miyaip_request_timeout_seconds'
        ? 'MiyaIP Generate 请求超时，范围 2-60 秒。'
      : field.key === 'dynamic_proxy_default_country'
        ? '动态模式唯一的默认出口国家。任务未填写出口国家时使用两位 ISO 国家码，例如 JP、US、SG。'
      : field.key === 'dynamic_proxy_ip_retention_minutes'
        ? '覆盖 Cliproxy 用户名里的 t-N 字段，例如填 5 会生成 t-5；模板没有 t-N 但包含 sid 时会自动补到 sid 后。范围 1-1440 分钟。'
      : field.key === 'dynamic_proxy_require_country_match'
        ? '开启后，动态代理实测出口国家与声明国家不一致会直接失败；GeoIP 临时不可用时会记录未实测而不误杀候选。'
      : field.key === 'dynamic_proxy_probe_enabled'
        ? '开启后任务生成动态代理候选时先探测出口 IP/国家；关闭后只生成当前渠道的运行代理。'
      : field.key === 'dynamic_proxy_probe_timeout_seconds'
        ? '动态代理出口探测超时，建议 6-12 秒。'
      : field.key === 'default_executor'
      ? '当前仅对 ChatGPT 生效；支持纯协议、无头浏览器和有头浏览器模式。'
      : field.key === 'default_browser_family'
      ? '当前仅对 ChatGPT 生效；纯协议支持 Chrome、Firefox、Safari 和随机。无头/有头支持 Firefox on Mac 与 Chrome on Mac；随机及 Safari 在深浏览器任务中兼容归一为 Firefox。'
      : field.key === 'icloud_hme_helper_api_url'
        ? `当前 Docker 编排使用 ${DEFAULT_HME_READY_API_URL}；不要填写容器内 127.0.0.1 或 host.docker.internal。`
      : field.key === 'icloud_hme_helper_internal_key'
        ? '读取 helper 项目 .internal-api-key；只用于 auto-gpt 调用 HME Ready API 出池和 finalize，验证码仍由 auto-gpt 直接读取 TempMail。'
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
        ? '填写 HME Ready 转发到的 TempMail 地址；留空时后端使用当前候选转发目标。'
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
      ) : field.type === 'number' ? (
        <InputNumber
          min={field.min}
          max={field.max}
          precision={field.precision}
          step={field.step}
          style={{ width: '100%' }}
          placeholder={field.placeholder}
        />
      ) : field.key === 'chatgpt_local_status_probe_concurrency' ? (
        <InputNumber min={1} max={10} precision={0} style={{ width: '100%' }} placeholder={field.placeholder} />
      ) : field.key === 'chatgpt_local_status_probe_delay_seconds' || field.key === 'chatgpt_local_status_probe_delay_max_seconds' ? (
        <InputNumber min={0} max={3600} precision={2} step={0.5} style={{ width: '100%' }} placeholder={field.placeholder} />
      ) : field.secret ? (
        <Input.Password
          placeholder={field.placeholder}
          visibilityToggle={{
            visible: showSecret,
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

function ConfigFieldList({ fields }: { fields: FieldConfig[] }) {
  const firstMiyaipIndex = fields.findIndex((field) => MIYAIP_FIELD_KEYS.has(field.key))
  if (firstMiyaipIndex < 0) {
    return fields.map((field) => <ConfigField key={field.key} field={field} />)
  }

  const miyaipFields = fields.filter((field) => MIYAIP_FIELD_KEYS.has(field.key))
  const beforeMiyaip = fields.slice(0, firstMiyaipIndex).filter((field) => !MIYAIP_FIELD_KEYS.has(field.key))
  const afterMiyaip = fields.slice(firstMiyaipIndex).filter((field) => !MIYAIP_FIELD_KEYS.has(field.key))
  return (
    <>
      {beforeMiyaip.map((field) => <ConfigField key={field.key} field={field} />)}
      <section className="settings-miyaip-field-group" aria-label="MiyaIP 鉴权与线路生成">
        <div className="settings-miyaip-field-heading">
          <Typography.Text strong>MiyaIP 鉴权与线路生成</Typography.Text>
          <Typography.Text type="secondary">
            代理密码对应 Crc，主 Key 对应 KeyName；实际代理用户名由 Generate 接口返回。
          </Typography.Text>
        </div>
        <div className="settings-miyaip-field-grid">
          {miyaipFields.map((field) => (
            <div
              key={field.key}
              className={field.key === 'miyaip_crc' || field.key === 'miyaip_key_name'
                ? 'settings-miyaip-field settings-miyaip-field-wide'
                : 'settings-miyaip-field'}
            >
              <ConfigField field={field} />
            </div>
          ))}
        </div>
      </section>
      {afterMiyaip.map((field) => <ConfigField key={field.key} field={field} />)}
    </>
  )
}

interface DynamicProxyPreviewResult {
  ok?: boolean
  provider?: string
  expected_country?: string
  actual_country?: string
  exit_ip?: string
  match?: boolean
  latency_ms?: number
  runtime_proxy_redacted?: string
  proxy?: string
  message?: string
}

function DynamicProxyConnectionTest() {
  const form = Form.useFormInstance()
  const taskProxyMode = Form.useWatch('task_proxy_mode', form)
  const providerValue = Form.useWatch('dynamic_proxy_provider', form)
  const cliproxyTemplate = Form.useWatch('dynamic_proxy_template', form)
  const retentionMinutes = Form.useWatch('dynamic_proxy_ip_retention_minutes', form)
  const miyaipCrc = Form.useWatch('miyaip_crc', form)
  const miyaipKeyName = Form.useWatch('miyaip_key_name', form)
  const miyaipPool = Form.useWatch('miyaip_pool', form)
  const miyaipGateway = Form.useWatch('miyaip_gateway_server', form)
  const miyaipProtocol = Form.useWatch('miyaip_protocol', form)
  const miyaipRequestTimeout = Form.useWatch('miyaip_request_timeout_seconds', form)
  const countryValue = Form.useWatch('dynamic_proxy_default_country', form)
  const requireCountryMatch = Form.useWatch('dynamic_proxy_require_country_match', form)
  const probeTimeout = Form.useWatch('dynamic_proxy_probe_timeout_seconds', form)
  const [testing, setTesting] = useState(false)
  const [result, setResult] = useState<DynamicProxyPreviewResult | null>(null)
  const [error, setError] = useState('')

  const provider = String(providerValue || 'cliproxy').trim().toLowerCase() === 'miyaip' ? 'miyaip' : 'cliproxy'

  useEffect(() => {
    setResult(null)
    setError('')
  }, [
    taskProxyMode,
    provider,
    cliproxyTemplate,
    retentionMinutes,
    miyaipCrc,
    miyaipKeyName,
    miyaipPool,
    miyaipGateway,
    miyaipProtocol,
    miyaipRequestTimeout,
    countryValue,
    requireCountryMatch,
    probeTimeout,
  ])

  if (String(taskProxyMode || 'dynamic').trim().toLowerCase() !== 'dynamic') return null

  const testConnection = async () => {
    const country = String(countryValue || '').trim().toUpperCase()
    const template = String(cliproxyTemplate || '').trim()
    const proxyPassword = String(miyaipCrc || '').trim()
    const mainKey = String(miyaipKeyName || '').trim()
    if (!/^[A-Z]{2}$/.test(country)) {
      message.warning('请先填写两位代理出口国家，例如 JP、US 或 SG')
      return
    }
    if (provider === 'cliproxy' && !template) {
      message.warning('请先填写 Cliproxy 动态节点地址')
      return
    }
    if (provider === 'miyaip' && (!proxyPassword || !mainKey)) {
      message.warning('请先填写 MiyaIP 代理密码和主 Key')
      return
    }

    setTesting(true)
    setResult(null)
    setError('')
    try {
      const response = await apiFetch('/proxies/dynamic-preview', {
        method: 'POST',
        body: JSON.stringify({
          provider,
          country_code: country,
          probe: true,
          require_country_match: parseBooleanConfigValue(requireCountryMatch),
          timeout_seconds: Math.max(2, Math.min(60, Number(probeTimeout) || 8)),
          ...(provider === 'cliproxy'
            ? {
                proxy: template,
                retention_minutes: Math.max(1, Math.min(1440, Number(retentionMinutes) || 5)),
                refresh_sid: true,
              }
            : {
                miyaip_crc: proxyPassword,
                miyaip_key_name: mainKey,
                miyaip_pool: Math.max(1, Math.min(999999, Number(miyaipPool) || 1)),
                miyaip_gateway_server: String(miyaipGateway || 'us').trim().toLowerCase(),
                miyaip_protocol: String(miyaipProtocol || 'http').trim().toLowerCase(),
                miyaip_request_timeout_seconds: Math.max(2, Math.min(60, Number(miyaipRequestTimeout) || 15)),
              }),
        }),
      }) as DynamicProxyPreviewResult
      setResult(response)
      if (response.ok) {
        message.success('已成功获取代理并通过出口测试')
      } else {
        message.warning(String(response.message || '已获取代理，但出口测试未通过'))
      }
    } catch (requestError) {
      const detail = requestError instanceof Error ? requestError.message : String(requestError || '代理测试失败')
      setError(detail)
      message.error(detail)
    } finally {
      setTesting(false)
    }
  }

  const runtimeProxy = String(result?.runtime_proxy_redacted || result?.proxy || '').trim()
  const actualCountry = String(result?.actual_country || '').trim().toUpperCase()
  const expectedCountry = String(result?.expected_country || countryValue || '').trim().toUpperCase()
  const resultTitle = result?.ok
    ? '代理获取成功，出口连接正常'
    : runtimeProxy
      ? '已获取代理，但可用性测试未通过'
      : '未能获取可用代理'

  return (
    <div className="settings-proxy-test">
      <div className="settings-proxy-test-head">
        <div className="settings-proxy-test-copy">
          <Typography.Text strong>代理可用性测试</Typography.Text>
          <Typography.Text type="secondary">
            使用当前表单参数获取一条代理并探测实际出口，不需要先保存配置。
          </Typography.Text>
        </div>
        <Button icon={<ThunderboltOutlined />} loading={testing} onClick={() => void testConnection()}>
          获取并测试代理
        </Button>
      </div>
      {result ? (
        <Alert
          type={result.ok ? 'success' : 'warning'}
          showIcon
          message={resultTitle}
          description={(
            <div className="settings-proxy-test-result">
              <span><b>渠道</b>{result.provider === 'miyaip' ? 'MiyaIP' : 'Cliproxy'}</span>
              <span><b>出口 IP</b>{result.exit_ip || '-'}</span>
              <span><b>国家</b>{actualCountry || '-'} / 期望 {expectedCountry || '-'}</span>
              <span><b>国家校验</b>{actualCountry ? (result.match ? '匹配' : '不匹配') : '未识别'}</span>
              <span><b>延迟</b>{Number(result.latency_ms || 0) > 0 ? `${result.latency_ms} ms` : '-'}</span>
              <span className="settings-proxy-test-result-wide"><b>运行代理</b><code>{runtimeProxy || '-'}</code></span>
              {result.message ? <span className="settings-proxy-test-result-wide"><b>说明</b>{result.message}</span> : null}
            </div>
          )}
        />
      ) : null}
      {error ? <Alert type="error" showIcon message="代理获取或出口测试失败" description={error} /> : null}
    </div>
  )
}

interface PaymentLinkConnectionResult {
  ok?: boolean
  base_url?: string
  api_version?: string
  link_type?: string
  country?: string
  currency?: string
  effective_concurrency?: number
}

function PaymentLinkConnectionTest() {
  const form = Form.useFormInstance()
  const baseUrl = Form.useWatch('openai_pay_long_link_base_url', form)
  const apiKey = Form.useWatch('openai_pay_long_link_api_key', form)
  const [testing, setTesting] = useState(false)
  const [result, setResult] = useState<PaymentLinkConnectionResult | null>(null)
  const [error, setError] = useState('')

  useEffect(() => {
    setResult(null)
    setError('')
  }, [baseUrl, apiKey])

  const testConnection = async () => {
    const normalizedBaseUrl = String(baseUrl || '').trim()
    const normalizedApiKey = String(apiKey || '').trim()
    if (!normalizedBaseUrl || !normalizedApiKey) {
      message.warning('请先填写服务地址和 API Key')
      return
    }
    setTesting(true)
    setResult(null)
    setError('')
    try {
      const response = await apiFetch('/config/payment-link/test', {
        method: 'POST',
        body: JSON.stringify({ base_url: normalizedBaseUrl, api_key: normalizedApiKey }),
      }) as PaymentLinkConnectionResult
      setResult(response)
    } catch (requestError) {
      setError(requestError instanceof Error ? requestError.message : String(requestError || '连接失败'))
    } finally {
      setTesting(false)
    }
  }

  const summary = [
    String(result?.link_type || '').toUpperCase(),
    [result?.country, result?.currency].filter(Boolean).join(' / '),
    result?.effective_concurrency ? `并发 ${result.effective_concurrency}` : '',
  ].filter(Boolean).join(' · ')

  return (
    <div style={{ display: 'grid', gap: 10 }}>
      <Button icon={<ApiOutlined />} onClick={testConnection} loading={testing} style={{ justifySelf: 'start' }}>
        测试连接
      </Button>
      {result ? (
        <Alert
          type="success"
          showIcon
          message={`连接成功 · ${result.api_version === 'legacy' ? '兼容接口' : 'API v1'}`}
          description={summary || result.base_url}
        />
      ) : null}
      {error ? <Alert type="error" showIcon message="连接失败" description={error} /> : null}
    </div>
  )
}

function SettingsPanelToolbar({
  title,
  pinnedSections,
  pinGroups,
  editorOpen,
  onEditorOpenChange,
  onPinnedSectionChange,
  onClearPinned,
  onSave,
  saving,
  saved,
}: {
  title: string
  pinnedSections: string[]
  pinGroups: { label: string, sections: SectionConfig[] }[]
  editorOpen: boolean
  onEditorOpenChange: (open: boolean) => void
  onPinnedSectionChange: (sectionTitle: string, checked: boolean) => void
  onClearPinned: () => void
  onSave: () => void
  saving: boolean
  saved: boolean
}) {
  return (
    <Card size="small" className="settings-panel-toolbar" style={{ marginBottom: 16 }}>
      <div className="settings-panel-toolbar-head">
        <div className="settings-panel-toolbar-copy">
          <Space size={8} wrap>
            <Typography.Text strong>{title}</Typography.Text>
            <Tag color={pinnedSections.length > 0 ? 'blue' : 'default'}>已置顶 {pinnedSections.length}</Tag>
          </Space>
          <Typography.Text type="secondary">常用面板可置顶，未置顶面板默认折叠。</Typography.Text>
        </div>
        <Space size={8} wrap className="settings-panel-toolbar-actions">
          {pinnedSections.length > 0 ? (
            <Button size="small" onClick={onClearPinned}>清空置顶</Button>
          ) : null}
          <Button
            size="small"
            icon={<PushpinOutlined />}
            onClick={() => onEditorOpenChange(!editorOpen)}
          >
            {editorOpen ? '收起置顶' : '编辑置顶'}
          </Button>
          <Button size="small" type="primary" icon={<SaveOutlined />} onClick={onSave} loading={saving}>
            {saved ? '已保存 ✓' : '保存配置'}
          </Button>
        </Space>
      </div>
      {!editorOpen && pinnedSections.length > 0 ? (
        <div className="settings-panel-pinned-summary">
          <span className="settings-panel-pin-label">当前置顶</span>
          <div className="settings-panel-pin-chips">
            {pinnedSections.map((sectionTitle) => (
              <Tag key={sectionTitle} className="settings-panel-pin-chip settings-panel-pin-chip-static">
                {sectionTitle}
              </Tag>
            ))}
          </div>
        </div>
      ) : null}
      {editorOpen ? (
        <div className="settings-panel-pin-groups">
          {pinGroups.map((group) => (
            <div key={group.label} className="settings-panel-pin-group">
              <span className="settings-panel-pin-label">{group.label}</span>
              <div className="settings-panel-pin-chips">
                {group.sections.map((section) => {
                  const checked = pinnedSections.includes(section.title)
                  return (
                    <Tag.CheckableTag
                      key={section.title}
                      checked={checked}
                      onChange={(nextChecked) => onPinnedSectionChange(section.title, nextChecked)}
                      className="settings-panel-pin-chip"
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
          <ConfigFieldList fields={fields || section.fields} />
          {section.title === TASK_PROXY_SECTION_TITLE ? <DynamicProxyConnectionTest /> : null}
          {section.title === PAYMENT_LINK_SERVICE_SECTION_TITLE ? <PaymentLinkConnectionTest /> : null}
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

type TotpSetupState = 'idle' | 'setup'
type AuthStatus = {
  has_password: boolean
  has_totp?: boolean
  instance_id?: string
  bootstrap_token_required?: boolean
  min_password_length?: number
  session_idle_timeout_seconds?: number
  session_absolute_timeout_seconds?: number
}

function SecurityPanel() {
  const { message: msg } = App.useApp()
  const [status, setStatus] = useState<AuthStatus | null>(null)
  const [statusLoading, setStatusLoading] = useState(true)
  const [statusError, setStatusError] = useState('')
  const [loading, setLoading] = useState(false)

  const [setupForm] = Form.useForm()
  const [pwForm] = Form.useForm()
  const [codeForm] = Form.useForm()
  const [disableTotpForm] = Form.useForm()

  const [totpSetupState, setTotpSetupState] = useState<TotpSetupState>('idle')
  const [totpSecret, setTotpSecret] = useState('')
  const [totpUri, setTotpUri] = useState('')
  const [disableTotpOpen, setDisableTotpOpen] = useState(false)
  const minPasswordLength = Math.max(12, Number(status?.min_password_length || 12))
  const sessionIdleHours = Math.max(1, Math.round(Number(status?.session_idle_timeout_seconds || 43200) / 3600))
  const sessionAbsoluteDays = Math.max(1, Math.round(Number(status?.session_absolute_timeout_seconds || 604800) / 86400))

  const loadStatus = async () => {
    setStatusLoading(true)
    setStatusError('')
    try {
      const s = await apiFetch('/auth/status') as AuthStatus
      setStatus(s)
    } catch (error: unknown) {
      setStatus(null)
      setStatusError(error instanceof Error ? error.message : '读取管理员认证状态失败')
    } finally {
      setStatusLoading(false)
    }
  }

  useEffect(() => { loadStatus() }, [])

  const handleInitialize = async (values: { password: string; confirm: string; bootstrap_token?: string }) => {
    if (values.password !== values.confirm) {
      msg.error('两次输入的密码不一致')
      return
    }
    setLoading(true)
    try {
      const bootstrapToken = String(values.bootstrap_token || '').trim()
      const d = await apiFetch('/auth/setup', {
        method: 'POST',
        headers: bootstrapToken ? { 'X-Auth-Bootstrap-Token': bootstrapToken } : undefined,
        body: JSON.stringify({ password: values.password }),
      })
      const accessToken = String(d.access_token || '')
      if (!accessToken) throw new Error('初始化响应缺少访问令牌')
      setToken(accessToken)
      msg.success('管理员认证已初始化')
      setupForm.resetFields()
      await loadStatus()
    } catch (error: unknown) {
      msg.error(error instanceof Error ? error.message : '初始化管理员认证失败')
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
      msg.success('密码已更新，请重新登录')
      pwForm.resetFields()
      invalidateSession()
    } catch (error: unknown) {
      msg.error(error instanceof Error ? error.message : '修改密码失败')
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
    } catch (error: unknown) {
      msg.error(error instanceof Error ? error.message : '生成双因素认证配置失败')
    } finally {
      setLoading(false)
    }
  }

  const cancelTotpSetup = () => {
    codeForm.resetFields()
    setTotpSecret('')
    setTotpUri('')
    setTotpSetupState('idle')
  }

  const handleEnableTotp = async (values: { code: string; current_password: string }) => {
    setLoading(true)
    try {
      await apiFetch('/auth/2fa/enable', {
        method: 'POST',
        body: JSON.stringify({
          secret: totpSecret,
          code: values.code,
          current_password: values.current_password,
        }),
      })
      msg.success('双因素认证已启用，请重新登录')
      cancelTotpSetup()
      invalidateSession()
    } catch (error: unknown) {
      codeForm.setFieldValue('code', '')
      msg.error(error instanceof Error ? error.message : '启用双因素认证失败')
    } finally {
      setLoading(false)
    }
  }

  const closeDisableTotp = () => {
    setDisableTotpOpen(false)
    disableTotpForm.resetFields()
  }

  const handleDisableTotp = async (values: { current_password: string; code: string }) => {
    setLoading(true)
    try {
      await apiFetch('/auth/2fa/disable', {
        method: 'POST',
        body: JSON.stringify({
          current_password: values.current_password,
          code: values.code,
        }),
      })
      msg.success('双因素认证已关闭，请重新登录')
      closeDisableTotp()
      invalidateSession()
    } catch (error: unknown) {
      disableTotpForm.setFieldValue('code', '')
      msg.error(error instanceof Error ? error.message : '关闭双因素认证失败')
    } finally {
      setLoading(false)
    }
  }

  return (
    <div style={{ display: 'flex', flexDirection: 'column', gap: 16 }}>
      <Card
        title="管理员认证"
        extra={
          status
            ? status.has_password
              ? <Tag color="green"><CheckCircleOutlined /> 已初始化</Tag>
              : <Tag color="warning"><CloseCircleOutlined /> 待初始化</Tag>
            : null
        }
      >
        {statusLoading ? (
          <div style={{ minHeight: 96, display: 'flex', alignItems: 'center', justifyContent: 'center' }}>
            <Spin />
          </div>
        ) : statusError ? (
          <Alert
            type="error"
            showIcon
            message="管理员认证状态读取失败"
            description={statusError}
            action={<Button size="small" onClick={() => void loadStatus()}>重试</Button>}
          />
        ) : status?.has_password === false ? (
          <Space direction="vertical" style={{ width: '100%' }}>
            <Alert
              type="warning"
              showIcon
              message="管理员认证尚未初始化"
              description={`请为当前实例${status.instance_id ? ` ${status.instance_id}` : ''} 设置独立的管理员密码。初始化完成后认证不可关闭，后续只能在验证当前密码后更新。${status.bootstrap_token_required ? ' 本实例要求提供初始化令牌。' : ' 未配置初始化令牌时，仅接受本机或可信容器网关来源的初始化请求。'}`}
            />
            <Form form={setupForm} layout="vertical" onFinish={handleInitialize} requiredMark={false} style={{ maxWidth: 360, marginTop: 8 }}>
              <Form.Item name="password" label="管理员密码" rules={[{ required: true, message: '请输入密码' }, { min: minPasswordLength, message: `至少 ${minPasswordLength} 位` }]}>
                <Input.Password placeholder={`至少 ${minPasswordLength} 位`} autoComplete="new-password" />
              </Form.Item>
              <Form.Item name="confirm" label="确认密码" rules={[{ required: true, message: '请再次输入' }]}>
                <Input.Password placeholder="再次输入密码" autoComplete="new-password" />
              </Form.Item>
              {status.bootstrap_token_required ? (
                <Form.Item name="bootstrap_token" label="初始化令牌" rules={[{ required: true, message: '请输入初始化令牌' }]}>
                  <Input.Password placeholder="APP_AUTH_BOOTSTRAP_TOKEN" autoComplete="off" />
                </Form.Item>
              ) : null}
              <Form.Item style={{ marginBottom: 0 }}>
                <Button type="primary" htmlType="submit" loading={loading} icon={<LockOutlined />}>
                  初始化管理员认证
                </Button>
              </Form.Item>
            </Form>
          </Space>
        ) : status?.has_password ? (
          <Typography.Text type="secondary">
            管理员认证已初始化且不可关闭。需要轮换凭据时，请使用下方的修改密码功能。
          </Typography.Text>
        ) : null}
      </Card>

      {status?.has_password && (
        <>
          <Card title="修改密码">
            <Form form={pwForm} layout="vertical" onFinish={handleChangePassword} requiredMark={false} style={{ maxWidth: 360 }}>
              <Form.Item name="current_password" label="当前密码" rules={[{ required: true, message: '请输入当前密码' }]}>
                <Input.Password placeholder="当前密码" autoComplete="current-password" />
              </Form.Item>
              <Form.Item name="new_password" label="新密码" rules={[{ required: true, message: '请输入新密码' }, { min: minPasswordLength, message: `至少 ${minPasswordLength} 位` }]}>
                <Input.Password placeholder={`新密码（至少 ${minPasswordLength} 位）`} autoComplete="new-password" />
              </Form.Item>
              <Form.Item name="confirm" label="确认新密码" rules={[{ required: true, message: '请再次输入' }]}>
                <Input.Password placeholder="再次输入新密码" autoComplete="new-password" />
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
            <Alert
              type="info"
              showIcon
              message={`连续空闲 ${sessionIdleHours} 小时后重新验证`}
              description={`会话在持续使用时自动续期；连续 ${sessionIdleHours} 小时没有已认证操作，才会要求重新输入${status?.has_totp ? '管理员密码和动态验证码' : '管理员密码'}。单次登录最长保留 ${sessionAbsoluteDays} 天，密码或 2FA 配置变更会立即撤销全部会话。`}
              style={{ marginBottom: 16 }}
            />
            {status?.has_totp ? (
              <Space direction="vertical">
                <Typography.Text type="secondary">
                  登录时需输入 Google Authenticator / Authy 等 App 中的 6 位验证码。
                </Typography.Text>
                <Button danger loading={loading} onClick={() => setDisableTotpOpen(true)}>
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
                <Typography.Text strong>2. 验证当前管理员密码和新验证器中的 6 位验证码</Typography.Text>
                <Form form={codeForm} layout="vertical" onFinish={handleEnableTotp} style={{ maxWidth: 360 }}>
                  <Form.Item name="current_password" label="当前管理员密码" rules={[{ required: true, message: '请输入当前管理员密码' }]}>
                    <Input.Password placeholder="当前管理员密码" autoComplete="current-password" />
                  </Form.Item>
                  <Form.Item name="code" label="新验证器验证码" rules={[{ required: true, message: '请输入验证码' }, { pattern: /^\d{6}$/, message: '请输入 6 位数字' }]}>
                    <Input inputMode="numeric" autoComplete="one-time-code" placeholder="000000" maxLength={6} style={{ width: 160, letterSpacing: 4, textAlign: 'center' }} />
                  </Form.Item>
                  <Form.Item style={{ marginBottom: 0 }}>
                    <Space>
                      <Button type="primary" htmlType="submit" loading={loading}>确认启用</Button>
                      <Button onClick={cancelTotpSetup} disabled={loading}>取消</Button>
                    </Space>
                  </Form.Item>
                </Form>
              </Space>
            )}
          </Card>
        </>
      )}

      <Modal
        title="关闭双因素认证"
        open={disableTotpOpen}
        onCancel={() => {
          if (!loading) closeDisableTotp()
        }}
        footer={null}
        destroyOnHidden
        closable={!loading}
        keyboard={!loading}
        maskClosable={!loading}
      >
        <Alert
          type="warning"
          showIcon
          message="此操作需要二次验证"
          description="请输入当前管理员密码和现有验证器中的 6 位验证码。成功后所有现有会话都会失效。"
          style={{ marginBottom: 16 }}
        />
        <Form form={disableTotpForm} layout="vertical" onFinish={handleDisableTotp} requiredMark={false}>
          <Form.Item name="current_password" label="当前管理员密码" rules={[{ required: true, message: '请输入当前管理员密码' }]}>
            <Input.Password placeholder="当前管理员密码" autoComplete="current-password" />
          </Form.Item>
          <Form.Item name="code" label="当前验证器验证码" rules={[{ required: true, message: '请输入验证码' }, { pattern: /^\d{6}$/, message: '请输入 6 位数字' }]}>
            <Input inputMode="numeric" autoComplete="one-time-code" placeholder="000000" maxLength={6} style={{ letterSpacing: 4, textAlign: 'center' }} />
          </Form.Item>
          <Form.Item style={{ marginBottom: 0 }}>
            <Space>
              <Button danger type="primary" htmlType="submit" loading={loading}>确认关闭</Button>
              <Button onClick={closeDisableTotp} disabled={loading}>取消</Button>
            </Space>
          </Form.Item>
        </Form>
      </Modal>
    </div>
  )
}

export default function Settings() {
  const { message: appMessage, modal: appModal } = App.useApp()
  const [form] = Form.useForm()
  const screens = Grid.useBreakpoint()
  const [saving, setSaving] = useState(false)
  const [saved, setSaved] = useState(false)
  const [configLoaded, setConfigLoaded] = useState(false)
  const [configLoadError, setConfigLoadError] = useState('')
  const [configDirty, setConfigDirty] = useState(false)
  const [shareState, setShareState] = useState<ConfigShareState | null>(null)
  const [shareBusy, setShareBusy] = useState(false)
  const [activeTab, setActiveTab] = useState('register')
  const [registerPinEditorOpen, setRegisterPinEditorOpen] = useState(false)
  const [registerPinnedSections, setRegisterPinnedSections] = useState<string[]>(
    () => loadPinnedSections(REGISTER_PINNED_SECTIONS_STORAGE_KEY),
  )
  const [chatgptPinEditorOpen, setChatgptPinEditorOpen] = useState(false)
  const [chatgptPinnedSections, setChatgptPinnedSections] = useState<string[]>(
    () => loadPinnedSections(CHATGPT_PINNED_SECTIONS_STORAGE_KEY),
  )
  const initialTaskProxyValuesRef = useRef<Record<string, unknown> | null>(null)
  const selectedMailProvider = Form.useWatch('mail_provider', form) || 'luckmail'
  const taskProxyMode = String(Form.useWatch('task_proxy_mode', form) || 'dynamic').trim().toLowerCase()
  const dynamicProxyProvider = String(Form.useWatch('dynamic_proxy_provider', form) || 'cliproxy').trim().toLowerCase()
  const taskProxyFailover = parseBooleanConfigValue(Form.useWatch('task_proxy_failover', form))
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
    appModal.confirm({
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
          appMessage.success(enabled ? '已开启共享配置' : '已关闭共享配置')
          reloadAfterShareChange()
        } catch (error: any) {
          appMessage.error(error?.message || (enabled ? '开启共享配置失败' : '关闭共享配置失败'))
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
      appMessage.success('已从共享模板拉取到当前实例')
      reloadAfterShareChange()
    } catch (error: any) {
      appMessage.error(error?.message || '从共享模板拉取失败')
    } finally {
      setShareBusy(false)
    }
  }

  const pushLocalConfigToShared = () => {
    if (!shareState) {
      appMessage.error('共享状态尚未加载，请刷新状态后重试')
      return
    }
    if (shareState.enabled) {
      appMessage.info('当前实例已在共享模式，无需再次发布本地配置')
      return
    }
    if (!configLoaded) {
      appMessage.error('配置尚未加载完成，暂不能发布')
      return
    }
    if (configDirty) {
      appMessage.warning('页面存在未保存修改，请先点击“保存配置”再发布为共享模板')
      return
    }

    appModal.confirm({
      title: '发布本地配置并启用共享',
      content: '将使用本实例已保存的本地配置覆盖共享模板；成功后当前实例立即切换为共享模式，并影响所有已开启共享的实例。共享模板若已被其他实例更新，会因 revision 冲突而拒绝覆盖。',
      okText: '发布并启用共享',
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
              enable_shared: true,
            }),
          }) as { state?: ConfigShareState }
          if (result?.state) setShareState(result.state)
          appMessage.success('本地配置已发布为共享模板，当前实例已切换为共享模式')
          reloadAfterShareChange()
        } catch (error: any) {
          appMessage.error(error?.message || '推送共享模板失败')
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
      appModal.info({
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
    } catch (error: any) {
      appMessage.error(error?.message || '读取共享差异失败')
    } finally {
      setShareBusy(false)
    }
  }

  useEffect(() => {
    if (typeof window === 'undefined') return
    window.localStorage.setItem(CHATGPT_PINNED_SECTIONS_STORAGE_KEY, JSON.stringify(chatgptPinnedSections))
  }, [chatgptPinnedSections])

  useEffect(() => {
    if (typeof window === 'undefined') return
    window.localStorage.setItem(REGISTER_PINNED_SECTIONS_STORAGE_KEY, JSON.stringify(registerPinnedSections))
  }, [registerPinnedSections])

  useEffect(() => {
    setConfigLoaded(false)
    setConfigLoadError('')
    setConfigDirty(false)
    loadShareState().catch(() => undefined)
    apiFetch('/config').then((data) => {
      if (!data || typeof data !== 'object' || Array.isArray(data)) {
        throw new Error('配置接口返回格式异常')
      }
      if (!data.mail_provider) {
        data.mail_provider = 'luckmail'
      }
      if (['icloud_hme', 'icloud_hme_ready', 'icloud_hme_helper_ready', 'helper_ready_api'].includes(String(data.mail_provider || '').trim().toLowerCase())) {
        data.mail_provider = 'hme_ready_api'
      }
      if (data.mail_provider === 'hme_ready_api') data.icloud_hme_mode = 'helper_ready_api'
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
      if (!data.openai_pay_long_link_base_url) {
        data.openai_pay_long_link_base_url = 'http://openai-pay-long-link:8788'
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
        data.tempmail_api_url = DEFAULT_TEMPMAIL_API_URL
      }
      if (!data.icloud_hme_helper_api_url) {
        data.icloud_hme_helper_api_url = DEFAULT_HME_READY_API_URL
      }
      if (!data.oaipay_api_url) {
        data.oaipay_api_url = DEFAULT_OAIPAY_API_URL
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
      data.tempmail_archive_cleanup_enabled = parseBooleanConfigValue(data.tempmail_archive_cleanup_enabled)
      data.tempmail_archive_cleanup_pause_active_tasks =
        data.tempmail_archive_cleanup_pause_active_tasks === ''
          ? true
          : parseBooleanConfigValue(data.tempmail_archive_cleanup_pause_active_tasks)
      data.proxy_pool_cooldown_enabled = data.proxy_pool_cooldown_enabled === '' ? true : parseBooleanConfigValue(data.proxy_pool_cooldown_enabled)
      if (!data.task_proxy_mode) {
        data.task_proxy_mode = 'dynamic'
      }
      if (!data.dynamic_proxy_provider) {
        data.dynamic_proxy_provider = 'cliproxy'
      }
      if (!data.task_proxy_max_candidates) {
        data.task_proxy_max_candidates = data.proxy_pool_max_candidates || '5'
      }
      if (!data.task_proxy_min_score) {
        data.task_proxy_min_score = data.proxy_scan_min_score || '50'
      }
      data.task_proxy_failover = data.task_proxy_failover === '' ? false : parseBooleanConfigValue(data.task_proxy_failover)
      if (!data.chatgpt_local_status_probe_concurrency) {
        data.chatgpt_local_status_probe_concurrency = '1'
      }
      data.chatgpt_local_status_probe_unique_exit_ip_enabled =
        data.chatgpt_local_status_probe_unique_exit_ip_enabled === '' ||
        data.chatgpt_local_status_probe_unique_exit_ip_enabled === undefined
          ? false
          : parseBooleanConfigValue(data.chatgpt_local_status_probe_unique_exit_ip_enabled)
      if (data.chatgpt_local_status_probe_delay_seconds === undefined || data.chatgpt_local_status_probe_delay_seconds === '') {
        data.chatgpt_local_status_probe_delay_seconds = '0'
      }
      if (data.chatgpt_local_status_probe_delay_max_seconds === undefined || data.chatgpt_local_status_probe_delay_max_seconds === '') {
        data.chatgpt_local_status_probe_delay_max_seconds = '0'
      }
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
      if (!data.miyaip_pool) {
        data.miyaip_pool = '1'
      }
      if (!data.miyaip_gateway_server) {
        data.miyaip_gateway_server = 'us'
      }
      if (!data.miyaip_protocol) {
        data.miyaip_protocol = 'http'
      }
      if (!data.miyaip_request_timeout_seconds) {
        data.miyaip_request_timeout_seconds = '15'
      }
      data.dynamic_proxy_require_country_match = data.dynamic_proxy_require_country_match === '' ? true : parseBooleanConfigValue(data.dynamic_proxy_require_country_match)
      data.dynamic_proxy_probe_enabled = data.dynamic_proxy_probe_enabled === '' ? true : parseBooleanConfigValue(data.dynamic_proxy_probe_enabled)
      data.cfworker_domains = parseStoredDomainList(data.cfworker_domains)
      data.cfworker_enabled_domains = parseStoredDomainList(data.cfworker_enabled_domains)
      data.cfworker_random_subdomain = parseBooleanConfigValue(data.cfworker_random_subdomain)
      data.contribution_enabled = parseBooleanConfigValue(data.contribution_enabled)
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
      initialTaskProxyValuesRef.current = Object.fromEntries(
        TASK_PROXY_CONFIG_KEYS.map((key) => [key, data[key]]),
      )
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
      const rawValues = form.getFieldsValue(true)
      const values = { ...rawValues }
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
      if (values.mail_provider === 'hme_ready_api') values.icloud_hme_mode = 'helper_ready_api'
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
      values.dynamic_proxy_provider = String(values.dynamic_proxy_provider || 'cliproxy').trim().toLowerCase()
      if (!['cliproxy', 'miyaip'].includes(values.dynamic_proxy_provider)) {
        values.dynamic_proxy_provider = 'cliproxy'
      }
      const boundedIntegerConfig = (value: unknown, fallback: number, minimum: number, maximum?: number) => {
        const parsed = Number(value)
        const bounded = Math.max(minimum, Number.isSafeInteger(parsed) ? parsed : fallback)
        return String(maximum === undefined ? bounded : Math.min(maximum, bounded))
      }
      const boundedNumberConfig = (value: unknown, fallback: number, minimum: number, maximum: number) => {
        const parsed = Number(value)
        return String(Math.max(minimum, Math.min(maximum, Number.isFinite(parsed) ? parsed : fallback)))
      }
      values.chatgpt_register_browser_default_concurrency = boundedIntegerConfig(
        values.chatgpt_register_browser_default_concurrency, 2, 1,
      )
      values.chatgpt_register_browser_max_concurrency = boundedIntegerConfig(
        values.chatgpt_register_browser_max_concurrency, 2, 1,
      )
      values.chatgpt_register_delay_seconds = boundedNumberConfig(values.chatgpt_register_delay_seconds, 15, 0, 3600)
      values.chatgpt_register_delay_max_seconds = boundedNumberConfig(values.chatgpt_register_delay_max_seconds, 30, 0, 3600)
      values.chatgpt_runtime_browser_capacity_mode = String(
        values.chatgpt_runtime_browser_capacity_mode || 'adaptive',
      ).trim().toLowerCase()
      values.chatgpt_runtime_auth_browser_max_concurrency = boundedIntegerConfig(
        values.chatgpt_runtime_auth_browser_max_concurrency, 6, 1,
      )
      values.chatgpt_runtime_auth_browser_registration_reserve = boundedIntegerConfig(
        values.chatgpt_runtime_auth_browser_registration_reserve, 4, 0,
      )
      values.chatgpt_runtime_auth_browser_recheck_reserve = boundedIntegerConfig(
        values.chatgpt_runtime_auth_browser_recheck_reserve, 2, 0,
      )
      values.chatgpt_web_session_hold_max_sessions = boundedIntegerConfig(
        values.chatgpt_web_session_hold_max_sessions, 2, 1, 32,
      )
      values.chatgpt_runtime_auth_browser_launch_interval_seconds = boundedNumberConfig(
        values.chatgpt_runtime_auth_browser_launch_interval_seconds, 4, 0, 60,
      )
      values.chatgpt_runtime_auth_browser_pid_budget = boundedIntegerConfig(
        values.chatgpt_runtime_auth_browser_pid_budget, 128, 0, 4096,
      )
      values.chatgpt_runtime_pid_emergency_reserve = boundedIntegerConfig(
        values.chatgpt_runtime_pid_emergency_reserve, 256, 0, 4096,
      )
      values.chatgpt_runtime_host_memory_reserve_mib = boundedIntegerConfig(
        values.chatgpt_runtime_host_memory_reserve_mib, 2048, 0, 262144,
      )
      values.chatgpt_runtime_cpu_psi_avg10_limit = boundedNumberConfig(
        values.chatgpt_runtime_cpu_psi_avg10_limit, 15, 0, 100,
      )
      values.chatgpt_runtime_registration_transition_timeout_seconds = boundedIntegerConfig(
        values.chatgpt_runtime_registration_transition_timeout_seconds, 40, 20, 120,
      )
      values.chatgpt_runtime_solver_mode = String(values.chatgpt_runtime_solver_mode || 'auto').trim().toLowerCase()
      values.chatgpt_runtime_solver_warm_browsers = boundedIntegerConfig(
        values.chatgpt_runtime_solver_warm_browsers, 0, 0, 15,
      )
      values.chatgpt_runtime_solver_max_browsers = boundedIntegerConfig(
        values.chatgpt_runtime_solver_max_browsers, 4, 1, 15,
      )
      values.chatgpt_runtime_solver_idle_timeout_seconds = boundedIntegerConfig(
        values.chatgpt_runtime_solver_idle_timeout_seconds, 300, 30, 86400,
      )
      if (Number(values.chatgpt_register_browser_default_concurrency) > Number(values.chatgpt_register_browser_max_concurrency)) {
        setActiveTab('register')
        message.error('浏览器注册默认并发不能大于最大并发')
        return
      }
      if (
        Number(values.chatgpt_runtime_auth_browser_registration_reserve)
        + Number(values.chatgpt_runtime_auth_browser_recheck_reserve)
        > Number(values.chatgpt_runtime_auth_browser_max_concurrency)
      ) {
        setActiveTab('register')
        message.error('注册与失效测活保留槽位之和不能超过浏览器总上限')
        return
      }
      if (Number(values.chatgpt_register_delay_max_seconds) < Number(values.chatgpt_register_delay_seconds)) {
        setActiveTab('register')
        message.error('注册最大启动延时不能小于最小启动延时')
        return
      }
      if (Number(values.chatgpt_runtime_solver_warm_browsers) > Number(values.chatgpt_runtime_solver_max_browsers)) {
        setActiveTab('register')
        message.error('Solver 暖浏览器数不能大于最大浏览器数')
        return
      }
      const localProbeConcurrency = Number.parseInt(String(values.chatgpt_local_status_probe_concurrency || '1'), 10)
      values.chatgpt_local_status_probe_concurrency = String(
        Math.max(1, Math.min(10, Number.isFinite(localProbeConcurrency) ? localProbeConcurrency : 1)),
      )
      values.chatgpt_local_status_probe_unique_exit_ip_enabled = parseBooleanConfigValue(
        values.chatgpt_local_status_probe_unique_exit_ip_enabled === undefined
          ? false
          : values.chatgpt_local_status_probe_unique_exit_ip_enabled,
      )
      const normalizeLocalProbeDelay = (value: unknown) => {
        const parsed = Number(value)
        if (!Number.isFinite(parsed)) return '0'
        return String(Math.max(0, Math.min(3600, parsed)))
      }
      values.chatgpt_local_status_probe_delay_seconds = normalizeLocalProbeDelay(
        values.chatgpt_local_status_probe_delay_seconds,
      )
      values.chatgpt_local_status_probe_delay_max_seconds = normalizeLocalProbeDelay(
        values.chatgpt_local_status_probe_delay_max_seconds,
      )
      if (
        Number(values.chatgpt_local_status_probe_delay_max_seconds) <
        Number(values.chatgpt_local_status_probe_delay_seconds)
      ) {
        setActiveTab('register')
        message.error('本地状态同步最大延时不能小于最小延时')
        return
      }
      values.task_proxy_max_candidates = String(
        Math.max(1, Math.min(100, Number.parseInt(String(values.task_proxy_max_candidates || '5'), 10) || 5)),
      )
      values.task_proxy_min_score = String(
        Math.max(0, Math.min(100, Number.parseInt(String(values.task_proxy_min_score || '50'), 10) || 50)),
      )
      values.dynamic_proxy_template = String(values.dynamic_proxy_template || '').trim()
      values.miyaip_crc = String(values.miyaip_crc || '').trim()
      values.miyaip_key_name = String(values.miyaip_key_name || '').trim()
      const dynamicProxyCountry = String(values.dynamic_proxy_default_country || '').trim().toUpperCase()
      if (values.task_proxy_mode === 'dynamic') {
        if (values.dynamic_proxy_provider === 'cliproxy' && !values.dynamic_proxy_template) {
          setActiveTab('register')
          message.error('Cliproxy 渠道必须填写动态节点地址')
          return
        }
        if (values.dynamic_proxy_provider === 'miyaip' && (!values.miyaip_crc || !values.miyaip_key_name)) {
          setActiveTab('register')
          message.error('MiyaIP 渠道必须填写代理密码和主 Key')
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
      if (values.chatgpt_local_status_probe_unique_exit_ip_enabled && values.task_proxy_mode === 'direct') {
        setActiveTab('register')
        message.error('直连模式不能满足本地状态同步的独立出口 IP 要求，请关闭该开关或改用代理模式')
        return
      }
      if (
        values.chatgpt_local_status_probe_unique_exit_ip_enabled &&
        values.task_proxy_mode === 'specified' &&
        !values.task_proxy_failover
      ) {
        setActiveTab('register')
        message.error('指定代理模式开启独立出口 IP 时必须开启失败切换，或关闭独立出口要求')
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
      values.miyaip_pool = boundedIntegerConfig(values.miyaip_pool, 1, 1, 999999)
      values.miyaip_gateway_server = String(values.miyaip_gateway_server || 'us').trim().toLowerCase()
      if (!['us', 'as', 'eu'].includes(values.miyaip_gateway_server)) {
        setActiveTab('register')
        message.error('MiyaIP 网关必须是美洲、亚洲或欧洲')
        return
      }
      values.miyaip_protocol = String(values.miyaip_protocol || 'http').trim().toLowerCase()
      if (!['http', 'socks5'].includes(values.miyaip_protocol)) {
        setActiveTab('register')
        message.error('MiyaIP 代理协议必须是 HTTP 或 SOCKS5')
        return
      }
      values.miyaip_request_timeout_seconds = boundedIntegerConfig(
        values.miyaip_request_timeout_seconds, 15, 2, 60,
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
      values.chatgpt_access_token_only_checkout_amount_check_enabled = parseBooleanConfigValue(
        values.chatgpt_access_token_only_checkout_amount_check_enabled,
      )
      values.chatgpt_access_token_only_zero_amount_stop_enabled = parseBooleanConfigValue(
        values.chatgpt_access_token_only_zero_amount_stop_enabled,
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
      values.openai_pay_long_link_base_url = String(values.openai_pay_long_link_base_url || '').trim().replace(/\/+$/, '')
      values.openai_pay_long_link_api_key = String(values.openai_pay_long_link_api_key || '').trim()
      if (values.openai_pay_long_link_base_url) {
        try {
          const serviceUrl = new URL(values.openai_pay_long_link_base_url)
          if (
            !['http:', 'https:'].includes(serviceUrl.protocol)
            || serviceUrl.username
            || serviceUrl.password
            || serviceUrl.search
            || serviceUrl.hash
          ) {
            throw new Error('invalid service URL')
          }
        } catch {
          setActiveTab('chatgpt')
          message.error('支付长链服务地址必须是无凭据、无查询参数的 HTTP(S) URL')
          return
        }
      }

      const initialTaskProxyValues = initialTaskProxyValuesRef.current
      const sameConfigValue = (left: unknown, right: unknown) => {
        const normalize = (value: unknown) => {
          if (value === undefined || value === null) return ''
          if (typeof value === 'boolean') return value ? 'true' : 'false'
          return String(value).trim()
        }
        return normalize(left) === normalize(right)
      }
      const changedTaskProxyKeys = new Set(
        TASK_PROXY_CONFIG_KEYS.filter((key) => (
          !initialTaskProxyValues
          || !sameConfigValue(rawValues[key], initialTaskProxyValues[key])
        )),
      )
      const payload = { ...values }
      for (const key of TASK_PROXY_CONFIG_KEYS) {
        if (!changedTaskProxyKeys.has(key)) delete payload[key]
      }

      await apiFetch('/config', {
        method: 'PUT',
        body: JSON.stringify({
          data: payload,
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
        dynamic_proxy_provider: values.dynamic_proxy_provider,
        dynamic_proxy_template: values.dynamic_proxy_template,
        miyaip_crc: values.miyaip_crc,
        miyaip_key_name: values.miyaip_key_name,
        miyaip_pool: values.miyaip_pool,
        miyaip_gateway_server: values.miyaip_gateway_server,
        miyaip_protocol: values.miyaip_protocol,
        miyaip_request_timeout_seconds: values.miyaip_request_timeout_seconds,
        dynamic_proxy_default_country: values.dynamic_proxy_default_country,
        dynamic_proxy_require_country_match: values.dynamic_proxy_require_country_match,
        dynamic_proxy_probe_enabled: values.dynamic_proxy_probe_enabled,
        dynamic_proxy_probe_timeout_seconds: values.dynamic_proxy_probe_timeout_seconds,
        dynamic_proxy_ip_retention_minutes: values.dynamic_proxy_ip_retention_minutes,
        contribution_enabled: values.contribution_enabled,
        chatgpt_access_token_only_checkout_amount_check_enabled: values.chatgpt_access_token_only_checkout_amount_check_enabled,
        chatgpt_access_token_only_checkout_country: values.chatgpt_access_token_only_checkout_country,
        chatgpt_access_token_only_checkout_currency: values.chatgpt_access_token_only_checkout_currency,
        chatgpt_access_token_only_zero_amount_stop_enabled: values.chatgpt_access_token_only_zero_amount_stop_enabled,
        chatgpt_access_token_only_zero_amount_stop_threshold: values.chatgpt_access_token_only_zero_amount_stop_threshold,
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
        openai_pay_long_link_base_url: values.openai_pay_long_link_base_url,
        openai_pay_long_link_api_key: values.openai_pay_long_link_api_key,
        external_subscription_api_enabled: values.external_subscription_api_enabled,
        external_subscription_api_token: values.external_subscription_api_token,
        external_access_token_api_enabled: values.external_access_token_api_enabled,
        external_access_token_api_token: values.external_access_token_api_token,
        external_access_token_allow_refresh: values.external_access_token_allow_refresh,
        external_access_token_default_lease_seconds: values.external_access_token_default_lease_seconds,
        external_access_token_max_limit: values.external_access_token_max_limit,
        external_access_token_precheck_cooldown_seconds: values.external_access_token_precheck_cooldown_seconds,
      })
      initialTaskProxyValuesRef.current = Object.fromEntries(
        TASK_PROXY_CONFIG_KEYS.map((key) => [key, values[key]]),
      )
      message.success('保存成功')
      setConfigDirty(false)
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
  const normalizedRegisterPinnedSections =
    activeTab === 'register' ? normalizePinnedSections(registerPinnedSections, visibleSections) : []
  const normalizedActivePinnedSections = activeTab === 'register'
    ? normalizedRegisterPinnedSections
    : normalizedChatgptPinnedSections
  const orderedVisibleSections =
    activeTab === 'mailbox'
      ? orderMailboxSections(visibleSections, selectedMailProvider)
      : activeTab === 'chatgpt' || activeTab === 'register'
        ? orderPinnedSections(visibleSections, normalizedActivePinnedSections)
        : visibleSections
  const chatgptPinGroups = activeTab === 'chatgpt'
    ? buildSectionPinGroups(visibleSections, CHATGPT_PIN_GROUPS)
    : []
  const registerPinGroups = activeTab === 'register'
    ? buildSectionPinGroups(visibleSections, REGISTER_PIN_GROUPS)
    : []
  const toggleChatgptPinnedSection = (sectionTitle: string, checked: boolean) => {
    setChatgptPinnedSections((prev) => {
      const withoutCurrent = prev.filter((title) => title !== sectionTitle)
      return checked ? [...withoutCurrent, sectionTitle] : withoutCurrent
    })
  }
  const toggleRegisterPinnedSection = (sectionTitle: string, checked: boolean) => {
    setRegisterPinnedSections((prev) => {
      const withoutCurrent = prev.filter((title) => title !== sectionTitle)
      return checked ? [...withoutCurrent, sectionTitle] : withoutCurrent
    })
  }
  const getMailboxSectionCollapseState = (sectionTitle: string) => {
    if (activeTab !== 'mailbox') return { defaultCollapsed: false, autoExpand: false }
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
                ? `保存本页会更新共享模板，并影响所有开启共享的实例；最后更新：${shareState?.shared?.updated_by || '-'} / ${formatBeijingDateTime(shareState?.shared?.updated_at)}`
                : `当前实例只使用本地配置；脱离基线：rev ${shareState?.baseline_revision || '0'}，脱离时间：${formatBeijingDateTime(shareState?.detached_at)}。先保存修改，再发布本地配置即可重新加入共享。`}
            </Typography.Text>
            <Typography.Text type="secondary">
              本地保留不共享：CLIProxyAPI、外部分发 API Token、支付会话近期运行态等实例专属配置。
            </Typography.Text>
          </Space>
          <Space size={8} wrap>
            <Switch
              checked={Boolean(shareState?.enabled)}
              checkedChildren="共享"
              unCheckedChildren="本地"
              loading={shareBusy}
              disabled={!shareState}
              onChange={toggleShareMode}
            />
            <Button size="small" icon={<SyncOutlined />} loading={shareBusy} onClick={() => loadShareState()}>
              刷新状态
            </Button>
            <Button size="small" loading={shareBusy} disabled={!shareState} onClick={pullSharedConfig}>
              从共享拉取
            </Button>
            <Button size="small" loading={shareBusy} disabled={!shareState} onClick={showShareDiff}>
              查看差异
            </Button>
            <Button
              size="small"
              danger
              icon={<CloudUploadOutlined />}
              loading={shareBusy}
              disabled={!shareState || Boolean(shareState.enabled) || !configLoaded}
              onClick={pushLocalConfigToShared}
            >
              发布本地并启用共享
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
            <Form
              form={form}
              layout="vertical"
              onValuesChange={() => {
                if (configLoaded) setConfigDirty(true)
              }}
            >
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
                  {activeTab === 'register' ? (
                    <SettingsPanelToolbar
                      title="注册配置面板"
                      pinnedSections={normalizedRegisterPinnedSections}
                      pinGroups={registerPinGroups}
                      editorOpen={registerPinEditorOpen}
                      onEditorOpenChange={setRegisterPinEditorOpen}
                      onPinnedSectionChange={toggleRegisterPinnedSection}
                      onClearPinned={() => setRegisterPinnedSections([])}
                      onSave={() => void save()}
                      saving={saving}
                      saved={saved}
                    />
                  ) : activeTab === 'chatgpt' ? (
                    <SettingsPanelToolbar
                      title="ChatGPT 配置面板"
                      pinnedSections={normalizedChatgptPinnedSections}
                      pinGroups={chatgptPinGroups}
                      editorOpen={chatgptPinEditorOpen}
                      onEditorOpenChange={setChatgptPinEditorOpen}
                      onPinnedSectionChange={toggleChatgptPinnedSection}
                      onClearPinned={() => setChatgptPinnedSections([])}
                      onSave={() => void save()}
                      saving={saving}
                      saved={saved}
                    />
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
                              ? taskProxyFieldsForMode(
                                  section.fields,
                                  taskProxyMode,
                                  taskProxyFailover,
                                  dynamicProxyProvider,
                                )
                              : undefined
                          }
                          defaultCollapsed={activeTab === 'chatgpt' || activeTab === 'register' || mailboxCollapseState.defaultCollapsed}
                          autoExpand={
                            ((activeTab === 'chatgpt' || activeTab === 'register')
                              && normalizedActivePinnedSections.includes(section.title))
                            || mailboxCollapseState.autoExpand
                          }
                        />
                      )
                    })()
                  ))}
                  {activeTab === 'mailbox' && selectedMailProvider === 'applemail' ? <AppleMailPoolImportSection form={form} /> : null}
                  {activeTab === 'mailbox' && selectedMailProvider === 'cfworker' ? <CFWorkerDomainPoolSection form={form} /> : null}
                  {activeTab === 'mailbox' && selectedMailProvider === 'outlook' ? <OutlookImportSection /> : null}
                  {activeTab !== 'chatgpt' && activeTab !== 'register' ? (
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
