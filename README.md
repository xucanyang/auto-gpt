# Auto GPT

ChatGPT 账号自动注册、认证材料维护、订阅检测和资源池管理平台。

当前版本：`v2.47.0`

## 项目定位

本项目面向受控环境中的 ChatGPT 账号全生命周期管理，将注册、认证材料补抓、状态检测、订阅与支付能力、批量任务和外部系统同步集中到同一个 Web 管理端。

## 核心能力

- **自动注册**：支持协议与浏览器执行器、邮箱验证码、手机验证码、代理和并发控制。
- **认证材料管理**：维护 Access Token、Refresh Token、Session、Cookie 和浏览器登录态，并记录认证生命周期。
- **账号资源池**：提供筛选、固定分组、批量操作、状态刷新、冻结与释放等管理能力。
- **订阅与支付检测**：检测套餐、0 元资格、Checkout 链接格式和可用支付方式，并支持相关支付任务。
- **外部系统同步**：可按实例配置 CLIProxyAPI、Sub2API、OAIPay、CodexProxy 等目标，保持本地库存与外部状态边界。
- **任务与审计**：提供任务日志、停止与恢复、失败原因、并发控制及操作审计。

## 技术栈

| 层级 | 技术 |
| --- | --- |
| 后端 | FastAPI、SQLite、SQLModel |
| 前端 | React、TypeScript、Vite |
| 网络 | curl_cffi、HTTPX |
| 浏览器 | Camoufox、Patchright、Playwright |
| 部署 | Docker、Docker Compose |

## 部署边界

- `docker-compose.yml` 提供单实例编排入口。
- `docker-compose.multi.yml` 用于同一镜像、多实例数据隔离的生产编排。
- `.env`、`data/`、`shared_config/`、`external_logs/` 和数据库文件均属于运行态数据，不会提交到 Git。
- Compose 文件包含网络、挂载目录和外部服务约定，部署前必须按目标环境检查并调整。
- 测试应遵循 [Docker 隔离测试规范](docs/testing-in-docker.md)。

## 代码结构

```text
api/         API 与任务入口
core/        配置、存储和通用基础能力
platforms/   平台注册与认证实现
services/    账号、支付、同步和浏览器服务
frontend/    Web 管理端
docs/        设计、部署和测试文档
```

## 获取代码

```bash
git clone https://github.com/xucanyang/auto-gpt.git
cd auto-gpt
```

显著变更记录见 [changelog.md](changelog.md)。本项目由 `any-auto-register` 系列历史代码演进而来，当前仓库为独立维护的 ChatGPT 专项分支。

请仅在合法、授权且符合相关服务条款的环境中使用。
