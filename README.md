# IPv6 DNS 控制台

通过网页查看公网 IPv6，把 Cloudflare 或阿里云的 AAAA 记录同步为当前地址。

## 部署

前置条件：

- Linux 宿主机（compose 使用 host 网络，Windows 上的 Docker Desktop 不支持）
- 已安装 Docker 与 Docker Compose
- 宿主机具备原生 IPv6 出站能力

在项目目录执行：

```bash
docker compose up -d --build
```

启动后访问 `http://127.0.0.1:8501`。

查看状态和日志：

```bash
docker compose ps
docker compose logs -f domain-update
```

### 端口

默认监听 `8501`。Compose 使用 host 网络，服务监听 `0.0.0.0`，即宿主机**全部网卡**都能访问该端口。

改端口有两种方式：编辑 `compose.yaml` 里的 `WEB_PORT`，或在项目目录放一个 `.env` 写入
`WEB_PORT=自定义端口`。改完重新执行 `docker compose up -d` 生效。

### 数据持久化

配置保存在 Docker 命名卷 `domain-update-data` 中。删除容器或重新构建镜像不会清除配置；
删除该卷会清除页面保存的凭据和定时设置。

## 首次配置

打开页面后，在左侧选择服务商并填写对应参数。

### Cloudflare

- API Token
- Zone ID
- 完整的 AAAA 记录名称，例如 `home.example.com`

如果记录不存在，首次更新会创建记录；已有记录仅在 IPv6 发生变化时更新。

### 阿里云

- AccessKey ID
- AccessKey Secret
- 解析记录 ID
- 记录类型，目前固定为 `AAAA`

阿里云使用解析记录 ID 更新已有记录，不会自动创建新记录。主机记录名与记录类型取自
接口查询结果，不需要填写。

### 可选通知

填写 Gotify 地址和 Token 后，DNS 记录创建或更新成功时会发送通知。地址可以包含
`http://` 或 `https://`；未填写协议时默认使用 HTTPS。

## 环境变量

下面四项是部署参数，通过环境变量传入（放进项目目录的 `.env` 即可，参考 `.env.example`）：

| 环境变量 | 默认值 | 用途 |
|---|---|---|
| `WEB_PORT` | `8501` | Web 页面监听端口，必须是 1-65535 的整数；非法值会记一条 WARNING 并退回默认端口 |
| `WEB_USERNAME` | 空 | 登录用户名 |
| `WEB_PASSWORD` | 空 | 登录密码。只填一项会被当成配置错误，服务拒绝启动（多半是变量名写错了） |
| `DOMAIN_UPDATE_LOG_LEVEL` | `INFO` | 日志级别（`DEBUG`/`INFO`/`WARNING`/`ERROR`），非法值退回 `INFO` |

在容器里运行时不通过环境变量调整数据目录：镜像已把它固定为 `/app/data`，并由命名卷
`domain-update-data` 持久化。只有直接跑 `python launcher.py`（不经 Docker）时，
`DOMAIN_UPDATE_DATA_DIR` 才有意义，默认 `.data`。

服务商、凭据、定时检查与 Gotify 等应用配置只在页面里设置，并保存到数据目录，不通过环境变量传入。

## 使用前必读

控制台可以改写 DNS 解析记录，请按以下前提使用：

- **登录凭据来自环境变量**，不提供注册入口，也没有找回密码。`WEB_USERNAME` 与 `WEB_PASSWORD`
  都填上才会启用登录；**只填一项会直接拒绝启动**（多半是变量名写错，按「不启用登录」放行
  等于让人以为有保护而实际没有）；两者都不填时服务照常启动，但**不校验登录**，启动日志里
  会有一条 WARNING。改这两个值并重启即换凭据，同时所有已签发的登录状态立即失效。
- **服务本身是明文 HTTP**，会话 Cookie 不带 `Secure`。公网部署必须再套一层带 HTTPS 的反向代理，
  否则同网段抓包即可重放会话。登录失败 5 次后按来源地址限流 15 分钟，但**口令正确时不限流**：
  反向代理后面所有请求的来源地址都是代理本身，若连正确口令也拦，任意一个人连错几次就能把
  所有人挡在门外。口令强度仍是第一道防线。
- `/healthz` 不需要登录，供容器健康检查使用；其余接口与页面都必须先登录。
- **密钥永不下发到页面**。状态接口只返回「是否已保存」，密码框始终为空，留空提交表示保留原值。
- **写操作只接受 JSON 请求体**，配合不返回任何 CORS 头与 `Origin` 校验，可挡住其他站点
  借访问者浏览器改写配置的跨站请求。
- 容器必须具备原生 IPv6 出站能力。检测失败、返回非公网 IPv6 或 DNS 查询失败时，
  程序不会写入 DNS。
