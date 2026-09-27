# IPv6 DNS 控制台

通过 Web 页面查看公网 IPv6，并将 Cloudflare 或阿里云的 AAAA 记录同步到当前地址。

## 功能

- 在 Cloudflare 与阿里云之间切换当前 DNS 服务商
- 查看当前公网 IPv6 和 DNS 记录值
- 手动检查并更新 AAAA 记录
- 按分钟设置后台定时检查
- 在页面中保存连接参数和 Gotify 通知配置
- 容器重启后从持久化卷恢复配置与定时任务

## Docker 运行

```bash
docker compose up -d --build
```

启动后访问 `http://127.0.0.1:8501`。Compose 默认只绑定宿主机回环地址，因为该页面可以修改 DNS 配置。需要远程访问时，建议通过带身份认证和 HTTPS 的反向代理开放，而不是直接暴露 8501 端口。

配置保存在 Docker 命名卷 `domain-update-data` 中。删除容器或重新构建镜像不会清除配置；删除该卷会清除页面保存的凭据和定时设置。

查看状态和日志：

```bash
docker compose ps
docker compose logs -f domain-update
```

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

## 兼容旧环境变量

首次启动且尚无持久化配置时，程序会读取项目 `.env` 或容器环境变量并写入配置文件。之后以页面保存的配置为准。

| 环境变量 | 用途 |
|---|---|
| `DOMAIN_UPDATE_PROVIDER` | `cloudflare` 或 `alibaba` |
| `DOMAIN_UPDATE_SCHEDULE_ENABLED` | 是否启用定时检查 |
| `DOMAIN_UPDATE_CHECK_INTERVAL_MINUTES` | 检查间隔，最少 1 分钟 |
| `CLOUDFARE_TOKEN` | Cloudflare API Token，保留旧拼写以兼容现有部署 |
| `CLOUDFARE_ZONE_ID` | Cloudflare Zone ID |
| `CLOUDFARE_RECORD_NAME` | Cloudflare 完整记录名 |
| `ALIBABA_CLOUD_ACCESS_KEY_ID` | 阿里云 AccessKey ID |
| `ALIBABA_CLOUD_ACCESS_KEY_SECRET` | 阿里云 AccessKey Secret |
| `ALIBABA_CLOUD_RECORDID` | 阿里云解析记录 ID |
| `ALIBABA_CLOUD_RR` | 阿里云主机记录 |
| `ALIBABA_CLOUD_IPTYPE` | 记录类型，应为 `AAAA` |
| `GOTIFY_ADDRESS` | Gotify 地址 |
| `GOTIFY_TOKEN` | Gotify Token |

## IPv6 检测

程序通过直连 `https://api6.ipify.org` 获取公网出口 IPv6，并忽略 `HTTP_PROXY`、`HTTPS_PROXY` 等环境代理，其行为等价于：

```bash
curl --noproxy "*" https://api6.ipify.org
```

容器必须具备原生 IPv6 出站能力。检测失败、返回非公网 IPv6 或 DNS 查询失败时，程序不会写入 DNS。
