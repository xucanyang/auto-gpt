# Docker 测试规范

## 适用范围

本规范适用于 `/opt/auto-gpt` 的 Python、浏览器和注册链路测试。它解决两个边界问题：

1. 测试依赖必须与生产运行依赖一致，不能因为宿主机 Python 环境漂移而在收集阶段失败。
2. 测试不能触碰 `auto-gpt`、`auto-gpt-plus`、`auto-plus2` 的真实运行数据、共享配置或线上任务。

生产 Docker 编排和测试 Docker 编排是两套不同用途的配置。`docker-compose.multi.yml` 只用于常驻业务实例，不是测试编排文件。

## 审计结论

### `argon2` 是测试环境漂移

[`requirements.txt`](../requirements.txt) 声明了 `argon2-cffi`，生产 [Dockerfile](../Dockerfile) 在构建阶段安装运行依赖。审计时发现：

- 宿主机 `/usr/bin/python3` 没有 `argon2`，直接执行 pytest 会在收集 `tests/test_admin_auth_security.py` 时中断。
- `auto-gpt:latest` 内已安装 `argon2-cffi`，说明线上运行镜像的认证依赖并未缺失。
- 生产镜像没有 `pytest`，因此不能把 `docker exec auto-gpt pytest` 当成正式测试入口。

根因是测试命令绕过了项目声明的运行环境，而不是 Argon2 认证实现本身故障。后续必须通过专用测试镜像统一解释器和依赖。

### Sentinel 红灯是测试契约漂移

[`tests/test_sentinel_browser.py`](../tests/test_sentinel_browser.py) 的资源门控场景行为是正确的：后续浏览器槽会分别在内存或 PID 余量不足时等待，资源恢复后再继续。测试固定模拟 cgroup 场景；生产多实例通过 `AUTH_BROWSER_MAX_CONCURRENCY_MAIN/PLUS/PLUS2` 选择实际槽位。Camoufox `152.0.4-beta.28` 的 Screen、Canvas、Audio、字体、语音和媒体设备仍有进程级配置依赖，因此 ChatGPT 深指纹注册采用“一账号画像、一 Camoufox 进程、一 BrowserContext”；每个槽均按完整浏览器的 `BROWSER_SECOND_SLOT_RESERVE_MIB`、`AUTH_BROWSER_PID_RESERVE` 和宿主机余量检查，不再使用旧的共享 Context 小预算。父进程仍预分配 endpoint/token，隔离 worker 的 OTP callback、停止和硬超时协议不变。

注册、Auth 与 Sentinel 共用的普通浏览器槽采用严格 FIFO：每个请求进入时取得单调递增票号，只有队首可以检查信号量和实时资源；队首通过后先认领全局启动错峰时间，再唤醒下一票，因此同一次资源恢复不会让多个等待者同时 `acquired`。`priority` 仅保留为注册等待者统计，不允许后到注册越过先到 Auth。任何位置的等待者被停止、跳过或异常中断时都必须摘除自己的票号并唤醒新队首，测试必须覆盖中间票取消，不能只验证正常完成顺序。

历史红灯来自日志文案断言漂移：旧测试查找 `第二槽内存余量不足`，而 [Sentinel 实现](../services/chatgpt_core/sentinel_browser.py) 输出的是稳定结构化字段；当前测试已改为断言：

```text
browser_slot=waiting reason=memory current=... limit=... reserve=... operation=...
```

Docker 化不能修复这类合同漂移。测试应断言稳定事件码和字段，例如 `browser_slot=waiting`、`reason=memory`，不能把本地化展示文案当成接口合同。

## 强制边界

### 禁止的做法

- 禁止在常驻业务容器内执行完整 pytest。
- 禁止对运行中的业务容器临时 `pip install pytest`，再把结果当作可复现验证。
- 禁止复用 `docker-compose.multi.yml` 的生产服务来跑测试。
- 禁止挂载 `/opt/auto-gpt/data`、`/opt/auto-gpt/shared_config`、Plus/Plus2 数据目录、真实 `.env`、外部日志或线上资源池。
- 禁止测试默认访问真实邮箱、手机号、代理池、注册接口或线上数据库。
- 禁止用 `pytest tests -q || true` 作为发布门禁；收集失败和依赖失败必须返回非零。

### 必须遵守的规则

- 正式测试通过一次性测试容器运行，容器结束后自动销毁。
- 测试镜像和生产镜像必须从同一提交、同一 Python 基础镜像和同一运行依赖层构建。
- 测试使用临时数据库、临时共享配置和临时运行目录。
- 默认关闭网络；需要网络的测试必须显式使用隔离 profile。
- 测试通过后，发布应复用已经验证过的同一个镜像 digest，不能重新构建另一份镜像。

## 目标文件和职责

以下文件是正式测试入口，均已落地。测试镜像使用生产 `Dockerfile` 的 `test` stage，测试 Compose 不读取生产环境文件、数据卷或端口。

| 文件 | 职责 | 当前状态 |
| --- | --- | --- |
| `requirements.txt` | 生产运行依赖 | 已存在 |
| `requirements-test.txt` | 在生产依赖之上固定 pytest 及测试插件 | 已建立 |
| `constraints.txt` 或锁文件 | 固定生产与测试构建使用的解析版本 | 待建立，生产依赖仍有下限约束 |
| `Dockerfile` 的 `test` stage 或 `Dockerfile.test` | 构建可复现测试镜像 | `Dockerfile` 的 `test` stage 已建立 |
| `docker-compose.test.yml` | 一次性、隔离的测试服务和浏览器测试 profile | 默认断网的 `test` 服务已建立 |
| `scripts/test-in-docker.sh` | 统一本地/CI 测试入口并拒绝错误挂载 | 已建立 |
| `pytest.ini` 或 `pyproject.toml` | 注册 `browser`/`live` marker 并启用严格 marker 检查 | `pytest.ini` 已建立 |
| `docs/testing-in-docker.md` | 测试环境、分层和门禁规范 | 本文 |

## 测试镜像要求

推荐在 [Dockerfile](../Dockerfile) 中抽出共享的运行依赖层，再分别生成 `runtime` 和 `test` 目标；也可以使用单独的 `Dockerfile.test`，但不能重新维护一套不同的生产依赖。

测试目标至少要满足：

1. 继承生产运行依赖，包含 `argon2-cffi`、Playwright、Camoufox 及其系统库。
2. 从 `requirements-test.txt` 安装固定版本的 `pytest` 和实际使用的插件。
3. 构建时执行以下预检，任何一步失败都停止构建：

   ```bash
   python -c "import argon2, pytest; print('test dependencies ok')"
   python -m pip check
   python -m pytest --collect-only -q
   ```

4. 镜像标签包含提交 SHA，例如 `auto-gpt:test-<git-sha>`，不要用不可追溯的 `latest` 作为测试证据。
5. 将 Python、pip、关键依赖版本写入测试输出，便于定位再次发生的环境漂移。

当前 `requirements.txt` 含有若干下限约束而非完整锁定版本。正式测试和生产发布应使用同一份解析后的 constraints/lock 文件；在锁文件落地前，至少保存测试镜像 digest 和 `pip freeze`，不能把一次临时安装结果当作稳定基线。

测试命令统一使用 `python -m pytest`，保证 pytest 和应用代码使用同一个解释器。

## 测试 Compose 隔离要求

`docker-compose.test.yml` 不得继承生产 Compose 的 `env_file`、数据卷、端口和重启策略。测试服务应采用类似下面的隔离变量：

```text
DATABASE_URL=sqlite:////tmp/auto-gpt-test.db
SHARED_CONFIG_DB=/tmp/shared_config.db
APP_RUNTIME_DIR=/tmp/runtime
APP_ENABLE_SOLVER=0
PYTHONDONTWRITEBYTECODE=1
```

具体要求：

- 使用 `run --rm`，不设置 `restart`。
- `/tmp`、运行目录和测试报告目录使用临时卷或 `tmpfs`。
- 不暴露 `8000`、`8001`、`8003`、`8317`、`8318`、`8320`、`8889` 等生产端口。
- 单元测试默认 `network_mode: none`；需要 fake service 时只加入测试专用网络。
- 测试环境不读取根目录 `.env` 和 `/root/.openclaw/.../.env`。
- CI 将源码直接构建进测试镜像；本地快速调试若绑定 checkout，只允许只读挂载。
- 测试完成后检查容器没有写入仓库、生产数据目录或共享配置目录。当前 `docker-compose.test.yml` 的 `/tmp` 使用 `rw,exec,nosuid,nodev` tmpfs，以兼容既有 Camoufox 可执行文件合同测试；网络仍为 `none`。

## 测试分层

### 单元和合同测试

每次提交都运行，默认无外网、无真实浏览器、无真实资源池。覆盖配置合并、认证依赖导入、日志字段、API 合同和状态机边界。

`browser` 和 `live` marker 必须在 `pytest.ini` 或 `pyproject.toml` 中注册，并开启严格 marker 检查，防止拼写错误把外部测试误混入默认回归。

### 浏览器集成测试

使用单独的 `browser` profile：

- 固定 Playwright/Camoufox 版本；
- 设置明确的 `mem_limit`、`shm_size` 和进程上限；
- 默认 headless；
- 控制并发，避免宿主机当前负载改变结果；
- 内存门控单元测试继续 mock 内存状态，真实 cgroup 行为只在集成 profile 验证。

### 外部实时烟测

真实 Sentinel、邮箱、手机号、代理和注册接口属于外部烟测，不应作为每次提交的默认门禁。只能在手动或定时 profile 中运行，并使用隔离的测试账号、测试资源和独立凭据。结果必须与普通单元测试分开报告，不能把外部 403、上游限流或资源耗尽误报为代码回归。

## 目标命令

测试镜像和测试 Compose 落地后，标准入口应类似：

```bash
# 构建与生产运行依赖同源的测试镜像
docker build --target test -t auto-gpt:test-"$(git rev-parse --short HEAD)" .

# 先做依赖和收集门禁
docker compose -f docker-compose.test.yml run --rm --no-deps test \
  python -m pytest --collect-only -q

# 默认回归：不访问外部网络、不跑实时注册
docker compose -f docker-compose.test.yml run --rm --no-deps test \
  python -m pytest -q -m "not browser and not live"

# 浏览器集成 profile 单独运行
docker compose -f docker-compose.test.yml --profile browser run --rm browser-test \
  python -m pytest -q -m browser
```

以上命令是当前 checkout 的正式测试规范。发布报告必须记录测试镜像构建结果、收集结果和默认回归退出码，不得把宿主机 pytest 或生产容器中的临时命令作为门禁证据。

## Sentinel 日志合同

Sentinel 浏览器槽位日志必须保留机器可断言字段。推荐合同如下：

```text
event=browser_slot_waiting reason=memory current=<bytes> limit=<bytes> reserve=<bytes> operation=<name>
event=browser_slot_waiting reason=capacity limit=<count> operation=<name>
event=browser_slot_waiting reason=fifo_queue ticket=<id> position=<n> depth=<n> head=<id> operation=<name>
event=browser_slot_waiting reason=fifo_stagger ticket=<id> delay=<seconds> interval=<seconds> operation=<name>
event=browser_slot_acquired ticket=<id> queue_wait=<seconds> limit=<count> operation=<name>
```

测试只断言 `event`、`reason` 和必要的数值关系；`[控制]`、中文描述和字段排列属于展示层，可以调整。若暂时继续使用纯文本日志，至少断言 `browser_slot=waiting` 与 `reason=memory`，并为日志格式变化同步更新合同测试。

## 发布前验收清单

- 测试镜像构建阶段能导入 `argon2` 和 `pytest`。
- `pip check` 通过，`pytest --collect-only -q` 通过。
- 单元/合同测试在无外网、无生产挂载下通过。
- 浏览器 profile 在固定 cgroup 资源下通过，或明确记录资源不足而非吞掉失败。
- Sentinel 测试断言结构化事件，不依赖旧中文文案。
- 测试容器未读取真实 `.env`，未写入生产数据库、共享配置或日志目录。
- 发布使用与测试相同的镜像 digest。
- 发布后仍按 [Docker 镜像发布与回滚](docker-image-release.md) 验证三个常驻业务实例和 HTTP smoke。
