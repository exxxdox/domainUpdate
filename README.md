# IPv6 DNS 控制台

通过 Web 页面查看公网 IPv6，并将 Cloudflare 或阿里云的 AAAA 记录同步到当前地址。

页面由项目自带的轻量 HTTP 服务提供（Python 标准库 `http.server` + 原生 HTML/CSS/JS），
不依赖 Streamlit 等 Web 框架，镜像体积因此压到约 116MB，拉取数据量约 27MB。

## 功能

- 在 Cloudflare 与阿里云之间切换当前 DNS 服务商
- 查看当前公网 IPv6 和 DNS 记录值
- 手动检查并更新 AAAA 记录
- 按分钟设置后台定时检查
- 在页面中保存连接参数和 Gotify 通知配置
- 容器重启后从持久化卷恢复配置与定时任务
- 检查记录报告：首页汇总指标与最近 5 条，点「查看全部」在浮层中分页浏览全部记录

## Docker 运行

```bash
docker compose up -d --build
```

启动后访问 `http://127.0.0.1:8501`。Compose 使用 host 网络，服务监听 `0.0.0.0`，即宿主机**全部网卡**都能访问 8501 端口。该页面可以修改 DNS 配置，因此必须用防火墙限制来源，或在前端加一层带身份认证和 HTTPS 的反向代理。

配置保存在 Docker 命名卷 `domain-update-data` 中。删除容器或重新构建镜像不会清除配置；删除该卷会清除页面保存的凭据和定时设置。

查看状态和日志：

```bash
docker compose ps
docker compose logs -f domain-update
```

## 安全边界

控制台可以改写 DNS 解析记录，请按以下前提使用：

- **没有内建登录**。服务本身不做身份认证，安全性依赖部署位置：服务监听 `0.0.0.0`，宿主机全部网卡都能访问 8501 端口，必须用防火墙限制来源，或在前端加一层带认证的反向代理。
- **密钥永不下发到页面**。状态接口只返回“是否已保存”的布尔值，密码框始终为空，留空提交表示保留原值。清除凭据需要在「已保存凭据」中显式勾选，且当前服务商的必填密钥不允许清空。
- **写操作只接受 JSON 请求体**。浏览器表单无法发送 `application/json`，配合不返回任何 CORS 头与 `Origin` 校验，可挡住其他站点借访问者浏览器改写配置的跨站请求。
- 页面响应带严格 CSP（`default-src 'none'`）与 `nosniff`、`X-Frame-Options: DENY`，且不加载任何外部资源。

## 本地运行

项目使用 [uv](https://docs.astral.sh/uv/) 管理 Python 与依赖。

```bash
sh script.sh init
sh script.sh run
```

也可以直接运行：

```bash
uv sync --frozen
uv run python launcher.py
```

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
- 主机记录 RR
- 记录类型，目前固定为 `AAAA`

阿里云使用解析记录 ID 更新已有记录，不会自动创建新记录。

### 可选通知

填写 Gotify 地址和 Token 后，DNS 记录创建或更新成功时会发送通知。地址可以包含 `http://` 或 `https://`；未填写协议时默认使用 HTTPS。

## 部署环境变量

应用配置（服务商、凭据、定时检查、Gotify）只在页面里设置，并且只保存到数据目录的 `config.json`。程序不读取任何覆盖这些字段的环境变量，因此同一个字段不会有两个来源。

只有下面三项属于部署参数，通过环境变量传入：

| 环境变量 | 默认值 | 用途 |
|---|---|---|
| `WEB_PORT` | `8501` | Web 页面监听端口，必须是 1-65535 的整数；非法值会记一条 WARNING 并退回默认端口 |
| `DOMAIN_UPDATE_DATA_DIR` | `.data` | 数据目录，存放 `config.json` 与 `check_history.jsonl` |
| `DOMAIN_UPDATE_LOG_LEVEL` | `INFO` | 日志级别，非法值退回 `INFO` |

`compose.yaml` 以 `${WEB_PORT:-8501}` 传入端口，也可以直接改该文件里的值。

## HTTP 接口

页面使用以下同源接口，也可用于脚本调用或排障。所有响应统一为
`{"ok": bool, "message": str, "data": ...}` 信封。

| 方法 | 路径 | 说明 |
|---|---|---|
| `GET` | `/healthz` | 健康检查，返回 `{"status": "ok"}`；不读取配置、不发网络请求 |
| `GET` | `/api/state` | 配置（脱敏）、公网地址、DNS 状态、调度器状态、检查记录报告（最近 5 条，汇总基于全部） |
| `GET` | `/api/history` | 分页返回检查记录，供首页浮层浏览全部。`page` 缺省 1，`page_size` 缺省 50、上限 200；两者非法或越界一律夹取回落，不报错。响应含 `page`、`page_size`、`total`、`total_pages` |
| `POST` | `/api/config` | 保存配置，成功后同步定时检查 |
| `POST` | `/api/ipv6` | 检测公网 IPv6 |
| `POST` | `/api/dns` | 查询当前 DNS 记录 |
| `POST` | `/api/update` | 检查并更新，结果记入检查记录（来源为 `manual`） |
| `POST` | `/api/notify/test` | 用「已保存配置 + 请求体当前值」发送一条 Gotify 测试消息，不写配置 |

请求体字段名与响应中的字段名一致；`POST` 必须带 `Content-Type: application/json`。

## IPv6 检测

程序通过直连 `https://api6.ipify.org` 获取公网出口 IPv6，并忽略 `HTTP_PROXY`、`HTTPS_PROXY` 等环境代理，其行为等价于：

```bash
curl --noproxy "*" https://api6.ipify.org
```

容器必须具备原生 IPv6 出站能力。检测失败、返回非公网 IPv6 或 DNS 查询失败时，程序不会写入 DNS。
