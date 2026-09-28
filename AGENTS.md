# CLAUDE.md

This file provides guidance to Claude Code (claude.ai/code) when working with code in this repository.

## 项目是什么

Web 控制台：读取公网 IPv6，把 AAAA 记录同步到 Cloudflare 或阿里云。前端是原生
HTML/CSS/JS，后端只用 Python 标准库 `http.server`，没有 Web 框架。

所有改动都受一条硬约束支配：**镜像体积与依赖数量**。历史上曾有 Streamlit 与
Cloudflare 官方 SDK，两者被移除后镜像从约 590MB 降到 116MB（拉取数据量约 27MB）。
新增依赖前先确认能否用标准库或直接 REST 调用等价替代（`providers.py` 直调 Cloudflare
REST 就是这个取舍的结果）。

## 常用命令

```bash
sh script.sh init          # uv sync --frozen，按锁文件安装
sh script.sh run           # uv run python launcher.py，等价于生产启动
uv run pytest              # 全部测试（234 个，约 19 秒）
uv run pytest tests/test_web_api.py -k history   # 按文件 + 关键字过滤
uv run python main.py      # 命令行单次检查+更新入口（等价 /api/update，来源记为 cli）
docker compose up -d --build && docker compose logs -f domain-update
```

本地开发用 [uv](https://docs.astral.sh/uv/) 管理 Python 与依赖：
`uv sync --frozen` + `uv run python launcher.py`（`sh script.sh init` / `run` 是同一组动作）。
依赖变更统一走 `uv add` / `uv lock`，不手改 `uv.lock`，也不用 pip freeze。

## 分层结构

调用链是单向的，改动时不要跨层回指：

```
models.py      Result[T] 信封（ok/message/data）+ 领域数据类，最底层，不依赖任何模块
config.py      AppConfig（不可变 dataclass）+ 校验 + ConfigStore 原子写 JSON
history.py     CheckRecord/CheckSummary + CheckHistoryStore（JSONL 追加 + 滚动截断）
network.py     公网 IPv6 探测（api6.ipify.org，trust_env=False 忽略代理）
notifier.py    Gotify 推送（Token 走 X-Gotify-Key 请求头，日志脱敏）
providers.py   DnsProvider 协议 + CloudflareProvider / AlibabaProvider
service.py     DomainUpdateService：编排「校验 → 探测 → 查记录 → 写记录 → 通知 → 记历史」
scheduler.py   UpdateScheduler：进程内 APScheduler 单例，定时调用 service
auth.py        登录鉴权：env 读凭据 + 无状态签名 Cookie + 登录失败节流，纯逻辑无 I/O
web/api.py     WebApi：路由表 + 鉴权闸门 + 跨站防护 + 请求校验 + 状态组装
web/server.py  ConsoleServer：把 http.server 请求翻译成 WebApi.handle()，托管静态资源
launcher.py    生产入口：配日志 → 配线程栈 → 恢复调度 → 启动 HTTP 服务
main.py        命令行入口，复用 DomainUpdateService
```

`service.py` 是唯一编排点：页面、定时任务、命令行三条路径都只调它，行为因此一致。
新增业务能力放在这一层，不要在 `web/api.py` 里直接调 provider 或 requests。

## HTTP 接口

所有响应统一信封 `{"ok": bool, "message": str, "data": ...}`，`message` 是可直接展示的中文。

| 方法 | 路径 | 说明 |
|---|---|---|
| `GET` | `/healthz` | 健康检查，返回 `{"status": "ok"}`；不读配置、不发网络请求（容器探活用）。不需要登录（另两个公开接口是登录与登出），加鉴权会让容器永远 unhealthy |
| `POST` | `/api/login` | 校验 `username`/`password`，成功回 `Set-Cookie`；失败 401，同一来源累计 5 次失败后 429（带 `Retry-After`）。**口令正确即使该来源已被限流也放行**。未配置凭据时回 200 且不下发 Cookie |
| `POST` | `/api/logout` | 下发立即过期的同名 Cookie。**公开接口**：清 Cookie 不需要权限，会话刚好过期时反而最需要能退出 |
| `GET` | `/api/state` | 脱敏配置、公网地址、DNS 状态、调度器状态、检查记录报告（最近 5 条预览，汇总基于全部） |
| `GET` | `/api/history` | 检查记录分页，供首页浮层浏览全部。`page` 缺省 1，`page_size` 缺省 50、上限 200；非法或越界一律夹取回落，不报错。响应含 `page`、`page_size`、`total`、`total_pages` |
| `POST` | `/api/config` | 保存配置，成功后同步定时检查 |
| `POST` | `/api/ipv6` | 检测公网 IPv6 |
| `POST` | `/api/dns` | 查询当前 DNS 记录 |
| `POST` | `/api/update` | 检查并更新，结果记入检查记录（来源 `manual`） |
| `POST` | `/api/notify/test` | 用「已保存配置 + 请求体当前值」发一条 Gotify 测试消息，不写配置 |

请求体字段名与响应字段名一致（由 `_API_NAME_FOR_APP_FIELD` 同源派生），`POST` 必须带
`Content-Type: application/json`。

## 部署参数（环境变量）

| 环境变量 | 默认值 | 用途 |
|---|---|---|
| `WEB_PORT` | `8501` | Web 监听端口，须为 1-65535 整数；非法值记 WARNING 并退回默认 |
| `WEB_USERNAME` | 空 | 控制台登录用户名 |
| `WEB_PASSWORD` | 空 | 控制台登录密码。**只配一项 = 配置错误，拒绝启动**（退出码 2）；两个都空才是「明确不启用登录」 |
| `DOMAIN_UPDATE_DATA_DIR` | `.data` | 数据目录，存放 `config.json` 与 `check_history.jsonl` |
| `DOMAIN_UPDATE_LOG_LEVEL` | `INFO` | 日志级别，非法值退回 `INFO` |

应用配置（服务商、定时检查、Gotify、页面里填的服务商密钥）**只**来自页面写入的
`config.json`，程序不读任何覆盖这些字段的环境变量——同一个字段不允许有两个来源。
`WEB_USERNAME`/`WEB_PASSWORD` 不违反这条：它们是**部署参数**，与 `WEB_PORT` 同类，
只用于能否访问控制台，从不进入 `config.json`。

`compose.yaml` 用 `${WEB_PORT:-8501}`、`${WEB_USERNAME:-}`、`${WEB_PASSWORD:-}` 从项目目录的
`.env` 取值；`.env` 已被 `.gitignore` 与 `.dockerignore` 排除。本地开发由 `sh script.sh run`
读取同一个文件，两边行为一致。模板见 `.env.example`。

## DNS 写入语义

- **Cloudflare**：记录不存在时首次更新会创建（`proxied=False`、`ttl=60`）；已存在则仅在 IPv6
  变化时 `PATCH`，并保留原 `proxied` 与 `ttl`——IPv6 变化不应改变暴露方式与缓存策略。
  查询结果按 `name` + `type` 二次过滤，不把服务端过滤当唯一防线。
- **阿里云**：以 Record ID 为主键，记录不存在直接拒绝，不会新建；更新时 `rr`/`type` 取查询结果
  而非配置，避免错误配置把已有记录重命名。
- 值未变化返回 `unchanged` 且不发写请求，省掉无意义的 API 调用与审计噪音。
- 探测失败、返回非公网 IPv6、或 DNS 查询失败时**绝不写 DNS**；查询失败不得被误判成「记录不存在」
  而尝试创建重复记录。
- IPv6 探测走 `https://api6.ipify.org`，`session.trust_env = False`（等价 `curl --noproxy "*"`），
  代理出口地址不得写进 DNS。容器必须具备原生 IPv6 出站能力。

## 必须遵守的既定设计

这些约束在代码里散落着注释，改错会静默破坏功能或安全边界：

**结果与文案**
- 所有可展示消息由服务端产出中文并放进 `Result.message`，前端只显示不再自己编文案。
- 密钥字段绝不回传：`api.SECRET_FIELDS` 里的字段只以 `<name>_saved` 布尔形式出现在
  `/api/state`。留空提交表示保留原值，清空必须显式传 `clear_*` 开关（当前服务商的必填密钥不允许清空）。

**字段命名映射**
- 配置文件里保留历史拼写（`cloudfare_*`、`check_interval_minutes`），HTTP 接口用可读名
  （`cloudflare_*`、`schedule_interval_minutes`）。读写两个方向都由
  `api._API_NAME_FOR_APP_FIELD` 派生，**任何一侧改成硬编码都会让同一字段在请求与响应里叫不同名字**。

**持久化**
- 只有一个数据目录（`DOMAIN_UPDATE_DATA_DIR`，默认 `.data`），放 `config.json` 与
  `check_history.jsonl`。写文件一律 tempfile + `os.replace` 原子替换，并 `os.chmod 0o600`。
- 历史是旁路能力：写入失败只记日志，不改变 DNS 更新的返回值（`service._record_check`）。
- 日志绝不记录配置文件内容、Zone ID、URL 查询参数；`notifier._redact` 是防回归兜底。

**并发**
- `service._UPDATE_LOCK` 是模块级锁，阻止页面手动操作与定时任务同时写同一条 DNS 记录。
- 调度器必须是进程内单例（`get_scheduler()`），`configure()` 按 (`enabled`, `interval`) 签名
  幂等，否则页面每次刷新都会重置下次执行时间。

**HTTP 安全边界**（`web/server.py` + `web/api.py` + `auth.py`）
- 登录凭据只来自 `WEB_USERNAME`/`WEB_PASSWORD`，无注册、无找回。会话是**无状态签名 Cookie**
  （`auth.AuthGuard`）：密钥由凭据派生，改口令即让所有已发 Cookie 失效，没有服务端会话表。
  Cookie 为 `HttpOnly; SameSite=Lax; Path=/`，**没有 `Secure`**——部署是明文 HTTP，
  加上它直连场景根本登不进去，公网必须套 HTTPS 反代。
- **两个凭据变量都缺**时服务照常启动但不鉴权（只在日志里记一条 WARNING）。这是刻意的取舍：
  升级既有部署不该因为少两个环境变量就起不来。**恰好只缺一个**是另一回事——那几乎一定是
  变量名写错，`launcher.py` 会记 ERROR 并以退出码 2 拒绝启动，绝不按「未配置」放行：
  否则用户以为开了登录，实际控制台完全裸奔，而全库只有一行 WARNING。
- 接口闸门在 `WebApi.handle()` 内、读完请求体之后。**不要挪到 `server._handle_api` 里
  `_read_body()` 之前**：那样未登录的大请求体会留在套接字里，keep-alive 下被当成下一个
  请求的请求行解析（同 413 那条注释描述的现象）。静态资源的闸门在 `server._handle`。
- 公开集合固定在 `api._PUBLIC_ROUTES` 与 `server._PUBLIC_STATIC`。`/healthz` 必须公开：
  Dockerfile 的 HEALTHCHECK 用 `urllib.request`，它跟随重定向，挡住会让容器永远 unhealthy，
  而 Docker 不会因 unhealthy 重启容器，故障是静默的。`/api/logout` 公开是因为清 Cookie
  不需要权限。登录页自己的 `login.html`/`login.js`/`app.css` 也必须公开，否则无限重定向。
- 登录失败按来源地址限流（`auth.LoginThrottle`，5 次 / 15 分钟，429 带 `Retry-After`）。
  来源地址只取 `client_address[0]`，**绝不读 `X-Forwarded-For`**（客户端可伪造，等于关掉限流）。
  **凭据校验必须在限流判定之前**：反代后面所有请求共用一个来源地址，先判限流的话，
  任意一个人连错 5 次就能把所有人（含管理员）锁在门外 15 分钟，攻击者每 15 分钟几个请求
  即可长期维持；而「口令正确就放行」不削弱抗爆破——攻击者没有正确口令，走不到这条分支。
  计数表有硬上限 `MAX_TRACKED_CLIENTS`，防止大量不同来源把它撑大、拖慢每次写入。
- CSP 为 `default-src 'none'`：静态页面**不能**出现内联 `style` 属性、内联事件属性、
  内联 `<style>` 块、无 `src` 的 `<script>`，浏览器会静默丢弃。页面也不加载任何外部资源。
  另注意 `form-action 'none'`：登录表单不写 `action`/`method`，提交由 `login.js` 走 fetch。
- 新增静态文件必须同时登记进 `STATIC_FILES` 与 `CONTENT_TYPES`（不用 mimetypes 猜类型，
  Alpine 基础镜像没有 `/etc/mime.types`）。不做首页回落，映射不到即 404。
- `POST` 必须 `Content-Type: application/json`（浏览器表单发不出这个类型，以此挡住跨站表单
  提交），且 `Origin` 与 `Host` 不同源即 403，不返回任何 CORS 头。
- 请求体上限 `MAX_BODY_BYTES`（64KB），超限回 413 并关闭连接（不能只回 413 而不处置
  套接字里的残留字节，keep-alive 下会串帧）。
- 需要额外响应头（`Set-Cookie`、`Retry-After`）时把 `ApiResponse.headers` 填上，
  `server._send_json` 会在安全头之后发出，并挡掉 `Content-Type`/`Content-Length`/`Connection`
  等本层已决定的名字。
- 新增路由只需往 `api._ROUTES` 加一行，并在 `WebApi` 上加同名处理函数；**同时确认它不在
  `_PUBLIC_ROUTES` 里**——`tests/test_web_api.py` 有一条遍历整张路由表的用例专门盯这件事。

**容器特有**
- `launcher.py` 里顺序不可调换：先 `configure_logging()`（否则启动期故障看不到），再
  `configure_thread_stack()`（`threading.stack_size` 只影响之后新建的线程），然后才是调度器
  与 HTTP 服务。
- `thread_stack.py` 把默认线程栈抬到 8MiB：Alpine 是 musl，默认栈 1MiB，深嵌套 JSON 会让
  CPython 的 C 递归撞栈并 SIGSEGV（不是 RecursionError），整个进程退出。
- `server.install_stop_handler` 必须保留：容器里本进程是 PID 1，Linux 忽略未注册的 SIGTERM，
  去掉它 `docker stop` 会等满宽限期再被 SIGKILL（退出码 137）。
- compose 用 host 网络（容器直接用宿主 IPv6 出口），因此 `network_mode` 与 `ports` 互斥；
  数据目录必须是命名卷而不是 bind mount（否则 uid 10001 的 appuser 写不进去）。

## 测试约定

- 测试与模块一一对应（`tests/test_<module>.py`），pytest 配置里 `pythonpath = ["."]`。
- 没有前端测试框架。前端的静态契约由 `tests/test_static_contract.py` 兜住几类静默故障：
  `el()` 用到的 id 在对应 HTML 中不存在（表现为整页白屏或登录页完全不响应）、会被 CSP 丢弃的
  内联写法、HTML 引用但 `STATIC_FILES` 未登记的静态资源、登录表单写了 `action`/`method`。
  新增页面时必须把文件加进 `SCANNED_FILES` 与 `HTML_ENTRIES`，否则契约对它静默失效。
- `tests/test_web_server.py` 起真实套接字：请求体超限时如何处置连接里的字节，只能在连接层
  观测，`WebApi.handle()` 那层看不到。涉及 HTTP 连接行为的改动要在这里加测。
- 网络相关测试用 monkeypatch 替换 `requests`，不打真实外部接口。

## 其他

- `utils/` 目录只剩历史 `__pycache__`，没有源码；`Dockerfile` 仍 `COPY utils`。清理时需同时
  改 Dockerfile，否则构建会因缺目录失败。
- 代码注释与提交信息：注释解释「为什么这样写」（项目里注释密度高且都是这类内容），
  提交信息用英文、conventional commits 格式。
