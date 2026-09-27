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
- 主机记录 RR
- 记录类型，目前固定为 `AAAA`

阿里云使用解析记录 ID 更新已有记录，不会自动创建新记录。

### 可选通知

填写 Gotify 地址和 Token 后，DNS 记录创建或更新成功时会发送通知。地址可以包含
`http://` 或 `https://`；未填写协议时默认使用 HTTPS。

## 环境变量

下面三项是部署参数，通过环境变量传入：

| 环境变量 | 默认值 | 用途 |
|---|---|---|
| `WEB_PORT` | `8501` | Web 页面监听端口，必须是 1-65535 的整数；非法值会记一条 WARNING 并退回默认端口 |
| `DOMAIN_UPDATE_DATA_DIR` | `.data` | 数据目录，存放 `config.json` 与 `check_history.jsonl` |
| `DOMAIN_UPDATE_LOG_LEVEL` | `INFO` | 日志级别，非法值退回 `INFO` |

服务商、凭据、定时检查与 Gotify 等应用配置只在页面里设置，并保存到数据目录，不通过环境变量传入。

## 使用前必读

控制台可以改写 DNS 解析记录，请按以下前提使用：

- **没有内建登录**。服务本身不做身份认证，安全性依赖部署位置：必须用防火墙限制来源，
  或在前端加一层带身份认证和 HTTPS 的反向代理。
- **密钥永不下发到页面**。状态接口只返回「是否已保存」，密码框始终为空，留空提交表示保留原值。
- **写操作只接受 JSON 请求体**，配合不返回任何 CORS 头与 `Origin` 校验，可挡住其他站点
  借访问者浏览器改写配置的跨站请求。
- 容器必须具备原生 IPv6 出站能力。检测失败、返回非公网 IPv6 或 DNS 查询失败时，
  程序不会写入 DNS。
