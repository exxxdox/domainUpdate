"""控制台的 JSON API。

原实现由 Streamlit 承载路由、会话状态与请求校验，改为自带 HTTP 服务后这些都需要显式实现。
集中放在本模块的目的：安全策略（跨站防护、请求体校验）与状态管理可以脱离 socket 单元测试，
`server.py` 只负责把 http.server 的请求翻译成这里的 `handle()` 调用。
"""

from __future__ import annotations

import json
import logging
import threading
from dataclasses import asdict, dataclass, field, fields, replace
from datetime import datetime
from typing import Any, Mapping, Protocol
from urllib.parse import parse_qs, urlsplit

from domain_update.auth import AuthGuard, LoginThrottle
from domain_update.config import AppConfig
from domain_update.history import DEFAULT_MAX_RECORDS, CheckRecord, summarize
from domain_update.models import DnsStatus
from domain_update.service import DomainUpdateService


# 首页表格只做概览，完整列表由 /api/history 分页提供。两个条数必须分成两个常量：
# 合成一个的话，调首页展示长度就会顺手改掉分页的页大小。
HISTORY_PREVIEW_LIMIT = 5
# 分页默认页大小与上限。上限防的是一次请求让浏览器建出过多行，不是防内存占用。
HISTORY_PAGE_SIZE = 50
HISTORY_MAX_PAGE_SIZE = 200

PROVIDER_LABELS = {
    "cloudflare": "Cloudflare",
    "alibaba": "阿里云",
}

# 记录里来源与动作都存英文枚举，展示时统一在这里翻译，避免前端散落 if。
HISTORY_SOURCE_LABELS = {
    "scheduled": "定时",
    "manual": "手动",
    "cli": "命令行",
}
HISTORY_ACTION_LABELS = {
    "created": "创建记录",
    "updated": "更新记录",
    "unchanged": "无变化",
    "failed": "失败",
}

# 这些字段的值绝不回传给浏览器，只能以“是否已保存”的布尔形式出现。
SECRET_FIELDS = frozenset(
    {"cloudfare_token", "alibaba_cloud_access_key_secret", "gotify_token"}
)
# 每个密钥字段对应的清空开关：页面不回显密钥，用户只能靠开关删除已保存的凭据。
CLEAR_FLAGS = {
    "cloudfare_token": "clear_cloudflare_token",
    "alibaba_cloud_access_key_secret": "clear_alibaba_access_key_secret",
    "gotify_token": "clear_gotify",
}

# AppConfig 里只有这两个字段不是文本；其余字段统一按文本归一，
# 否则请求体给出数字/null 会一路传到 config.validate() 的 .strip() 上抛
# AttributeError，被 handle() 的兜底分支当成服务端故障报成 500。
_NON_TEXT_FIELDS = frozenset({"schedule_enabled", "check_interval_minutes"})

JSON_CONTENT_TYPE = "application/json"

# HTTP 接口对外使用可读命名，配置文件里保留历史拼写（cloudfare_*、check_interval_*）。
# 读写两个方向都从这张表派生：一旦某侧改成硬编码，同一字段就会在请求与响应里叫不同名字。
_API_NAME_FOR_APP_FIELD = {
    "cloudfare_token": "cloudflare_token",
    "cloudfare_zone_id": "cloudflare_zone_id",
    "cloudfare_record_name": "cloudflare_record_name",
    "check_interval_minutes": "schedule_interval_minutes",
    "alibaba_cloud_access_key_id": "alibaba_access_key_id",
    "alibaba_cloud_access_key_secret": "alibaba_access_key_secret",
    "alibaba_cloud_record_id": "alibaba_record_id",
    "alibaba_cloud_ip_type": "alibaba_ip_type",
}


def _api_field_name(app_field_name: str) -> str:
    return _API_NAME_FOR_APP_FIELD.get(app_field_name, app_field_name)


logger = logging.getLogger(__name__)


class SchedulerLike(Protocol):
    def configure(self, config: AppConfig) -> Any: ...

    def snapshot(self) -> Any: ...


@dataclass(frozen=True)
class ApiResponse:
    status: int
    payload: dict[str, Any]
    # 少数接口需要额外的响应头：登录下发 Set-Cookie、限流回 Retry-After。
    # 默认空字典，既有构造方式（位置参数两个）与调用点完全不受影响。
    headers: dict[str, str] = field(default_factory=dict)


@dataclass
class ConsoleState:
    """页面共享的最近一次操作结果。进程内单实例，容器重启即重置。"""

    ipv6: str | None = None
    ipv6_checked_at: datetime | None = None
    dns: DnsStatus | None = None
    dns_checked_at: datetime | None = None
    update: dict[str, Any] | None = None


def _success(
    data: Any, message: str = "", headers: dict[str, str] | None = None
) -> ApiResponse:
    return ApiResponse(
        200, {"ok": True, "message": message, "data": data}, headers or {}
    )


def _failure(
    status: int, message: str, headers: dict[str, str] | None = None
) -> ApiResponse:
    return ApiResponse(
        status, {"ok": False, "message": message, "data": None}, headers or {}
    )


def _parse_query(path: str) -> dict[str, str]:
    """把查询串解析成标量字典。

    parse_qs 返回的是 str -> list[str]。直接把它当参数交给 handler，取值时
    int(["2"]) 会抛 TypeError，被 handle() 的兜底捕获成 500，所以在这里就扁平化。
    同名参数取最后一个，`?page=1&page=2` 时符合“后者覆盖前者”的直觉。
    """
    _, _, query = path.partition("?")
    if not query:
        return {}
    return {
        key: values[-1]
        for key, values in parse_qs(query, keep_blank_values=True).items()
    }


def _positive_int(raw: str | None, default: int, maximum: int | None = None) -> int:
    """解析正整数参数：缺失、非数字、非正数一律回落到 default。

    不返回 400 是因为这些参数由页面生成，只有手工改地址栏排障的人才会写错；
    夹取比报错少一个分支，也不惩罚排障的人。
    """
    try:
        value = int(raw) if raw is not None else default
    except (TypeError, ValueError):
        return default
    if value <= 0:
        return default
    if maximum is not None and value > maximum:
        return maximum
    return value


def _iso(value: datetime | None) -> str | None:
    """统一用带时区的 ISO 8601 输出。

    格式化交给浏览器完成：容器时区通常不是访问者时区，服务端格式化会把时间显示错。
    """
    return value.isoformat() if isinstance(value, datetime) else None


def build_config(current: AppConfig, body: Mapping[str, Any]) -> AppConfig:
    """把请求体合并进现有配置。

    密钥字段留空表示“保留原值”：页面不回显密钥，用户无法重新输入同一个值，
    若按空字符串处理就会误清空已保存的凭据。
    """
    updates: dict[str, Any] = {}
    for field in fields(AppConfig):
        name = field.name
        clear_flag = CLEAR_FLAGS.get(name)
        if clear_flag is not None and bool(body.get(clear_flag)):
            updates[name] = ""
            continue
        api_name = _api_field_name(name)
        if api_name not in body:
            continue
        value = body[api_name]
        if name in SECRET_FIELDS:
            text = "" if value is None else str(value).strip()
            if not text:
                continue
            updates[name] = text
            continue
        if name in _NON_TEXT_FIELDS:
            updates[name] = value
            continue
        # 非文本字段已排除，这里统一产出 str：null 表示清空，其余类型转字符串。
        updates[name] = "" if value is None else str(value).strip()
    # 复用 from_dict 完成 provider 白名单与间隔整数的校验，避免两套校验逻辑漂移。
    return AppConfig.from_dict({**asdict(current), **updates})


def _record_name_for(config: AppConfig | None, provider: str) -> str:
    """按服务商取配置里的目标记录名，仅用于查询结果缺席时的占位展示。

    只有 Cloudflare 把记录名存在配置里；阿里云以 Record ID 为主键，
    记录名来自接口查询结果（见 service 的 UpdateStatus.record_name）。
    """
    if config is None or provider != "cloudflare":
        return ""
    return config.cloudfare_record_name


def _dns_payload(status: DnsStatus | None) -> dict[str, Any] | None:
    if status is None:
        return None
    return {
        "provider": status.provider,
        "provider_label": PROVIDER_LABELS.get(status.provider, status.provider),
        "record_name": status.record_name,
        "record_type": status.record_type,
        # 记录值可能为空（尚未创建记录），页面据此显示“未创建”而不是留白。
        "value": status.value,
        "record_id": status.record_id,
    }


def _history_row(record: CheckRecord) -> dict[str, str]:
    return {
        "time": _iso(record.timestamp) or "",
        "source": HISTORY_SOURCE_LABELS.get(record.source, "未知"),
        "result": "成功" if record.ok else "失败",
        # 失败的检查没有对记录做任何动作，显示“—”而不是重复一遍“失败”。
        "action": HISTORY_ACTION_LABELS.get(record.action, "—") if record.ok else "—",
        "ipv6": record.ipv6 or "—",
        "previous_value": record.previous_value or "无记录",
        "message": record.message,
    }


class WebApi:
    """把 HTTP 请求映射到应用服务，并维护页面共享状态。"""

    def __init__(
        self,
        service: DomainUpdateService | None = None,
        scheduler: SchedulerLike | None = None,
        state: ConsoleState | None = None,
        auth: AuthGuard | None = None,
    ) -> None:
        self._service = service if service is not None else DomainUpdateService()
        if scheduler is None:
            # 延迟导入：只有真正运行服务时才需要调度器单例。
            from domain_update.scheduler import get_scheduler

            scheduler = get_scheduler()
        self._scheduler = scheduler
        # 属性名不能叫 _state：会与 /api/state 的处理函数同名并被覆盖成不可调用对象。
        self._console = state if state is not None else ConsoleState()
        self._lock = threading.Lock()
        # 默认不鉴权，且默认值绝不读环境变量：否则开发机上恰好导出了 WEB_USERNAME
        # 就会让整套测试变红。真正从环境变量造守卫的是 launcher 与 serve()。
        self._auth = auth if auth is not None else AuthGuard.disabled()
        # 失败计数用自己的锁：WebApi._lock 会跨越读历史的磁盘 IO 长时间持有，
        # 借它会把一次登录卡在一次历史读取后面。
        self._throttle = LoginThrottle()

    # ---- 路由 ----------------------------------------------------------------

    def handle(
        self,
        method: str,
        path: str,
        raw_body: bytes = b"",
        headers: Mapping[str, str] | None = None,
        client_ip: str | None = None,
    ) -> ApiResponse:
        method = method.upper()
        normalized = {
            str(key).lower(): str(value) for key, value in (headers or {}).items()
        }

        cross_site = self._reject_cross_site(normalized)
        if cross_site is not None:
            return cross_site

        target = path.split("?", 1)[0].rstrip("/") or "/"

        # 鉴权闸门放在这一层，而不是 server 层调用 handle() 之前：server 会先读完整
        # 请求体，在这里拒绝时套接字里不会残留未读字节。放在前面拒绝会让 keep-alive
        # 连接上剩下的请求体被当成下一个请求的请求行解析（详见 server._handle_api 注释）。
        # 闸门又在路由查找之前：否则未登录方靠 401 与 404 的差别就能枚举出有效接口。
        if target not in _PUBLIC_ROUTES and not self._auth.is_authorized(normalized):
            return self.unauthorized()

        route = _ROUTES.get(target)
        if route is None:
            return _failure(404, "未知接口")

        allowed_methods, handler_name = route
        if method not in allowed_methods:
            return _failure(405, "请求方法不被允许")

        if method == "POST":
            content_type = normalized.get("content-type", "")
            # 浏览器表单只能发 urlencoded / text/plain / multipart，强制 JSON 即可挡住跨站表单提交。
            if not content_type.lower().startswith(JSON_CONTENT_TYPE):
                return _failure(415, "请求必须是 application/json")
        try:
            # 输入来源按方法分流：POST 是 JSON 请求体（树），GET 是查询串（str -> str）。
            # 两者形状不同，handler 里取值要按自己的方法区分。不要把 GET 的参数挂到
            # self 上——WebApi 是单实例跑在 ThreadingHTTPServer 上，会并发串号。
            params: dict[str, Any] = (
                self._parse_body(raw_body) if method == "POST" else _parse_query(path)
            )
        except ValueError:
            return _failure(400, "请求体不是合法 JSON 对象")
        except RecursionError:
            # 嵌套深度超过解释器递归上限时，json.loads 抛的是 RecursionError 而不是
            # ValueError。漏接它会一路穿透到 BaseHTTPRequestHandler：客户端拿不到任何
            # 响应，只有连接被丢弃，堆栈也绕过统一日志直接打到 stderr。
            return _failure(400, "请求体 JSON 嵌套层级过深")

        handler = getattr(self, handler_name)
        try:
            if target == "/api/login":
                # 登录是唯一需要来源地址的接口，而来源地址来自连接、不是请求体，
                # 塞进 params 会和表单字段争用同一个命名空间，所以单独走一条分支。
                return self._handle_login(params, client_ip)
            return handler(params)
        except Exception:
            # 未预期异常既不能让连接直接断掉，也不能把堆栈回给浏览器。
            logger.exception("处理请求失败：%s %s", method, path)
            return _failure(500, "服务内部错误，请查看容器日志")

    def is_authorized(self, headers: Mapping[str, str]) -> bool:
        """静态资源的闸门由 server 层调用；接口层的闸门在 handle() 内部。"""
        return self._auth.is_authorized(headers)

    def unauthorized(self) -> ApiResponse:
        return _failure(401, "请先登录")

    def _handle_login(self, params: dict[str, Any], client_ip: str | None) -> ApiResponse:
        """登录入口：**先校验凭据，再按结果决定是否计数与限流**。

        顺序不能反过来。先判限流的话，反向代理后面所有人共用代理地址，任意一个人连错
        5 次就会把包括管理员在内的所有人锁在门外 15 分钟，攻击者靠每 15 分钟几个请求
        就能让登录一直不可用。而「口令正确就放行」不削弱抗爆破：攻击者手里没有正确口令，
        这条分支对他没有收益；合法用户则永远进得来。
        """
        client = client_ip or ""
        response = self._login(params)
        if response.status == 200:
            self._throttle.reset(client)
            # 成功与失败都要留痕：安全事件需要能归因。只记来源，绝不记提交的凭据。
            logger.info("登录成功：来源=%s", client)
            return response

        self._throttle.record_failure(client)
        logger.warning("登录失败：来源=%s", client)
        retry_after = self._throttle.retry_after_seconds(client)
        if not retry_after:
            return response
        return _failure(
            429,
            f"登录失败次数过多，请 {retry_after} 秒后再试",
            {"Retry-After": str(retry_after)},
        )

    def _login(self, params: dict[str, Any]) -> ApiResponse:
        if not self._auth.enabled:
            # 没配凭据时不存在「登录」这回事，回成功让页面直接进控制台更贴合实际状态，
            # 也避免走 issue() 那条「未配置凭据不该签发会话」的断言路径。
            return _success(None, "当前未启用登录，无需登录")
        if not self._auth.authenticate(params.get("username"), params.get("password")):
            return _failure(401, "用户名或密码错误")
        return _success(
            None, "登录成功", {"Set-Cookie": self._auth.login_cookie(self._auth.issue())}
        )

    def _logout(self, params: dict[str, Any]) -> ApiResponse:
        """清 Cookie 不需要任何权限，会话刚好过期时反而最需要能退出，所以它是公开接口。"""
        return _success(None, "已退出登录", {"Set-Cookie": self._auth.logout_cookie()})

    def _reject_cross_site(self, headers: Mapping[str, str]) -> ApiResponse | None:
        """浏览器允许跨站发起简单请求，必须显式拒绝非同源调用。

        否则任意网站都能借访问者的浏览器改写本机的 DNS 配置。
        """
        origin = headers.get("origin")
        if not origin:
            return None
        if urlsplit(origin).netloc.lower() != headers.get("host", "").lower():
            return _failure(403, "拒绝跨站请求")
        return None

    def _parse_body(self, raw_body: bytes) -> dict[str, Any]:
        if not raw_body:
            return {}
        payload = json.loads(raw_body.decode("utf-8"))
        if not isinstance(payload, dict):
            raise ValueError("请求体顶层必须是对象")
        return payload

    # ---- 处理函数 ------------------------------------------------------------

    def _health(self, _body: Mapping[str, Any]) -> ApiResponse:
        # 健康检查不触碰配置与网络，容器编排探活不会因为凭据缺失而失败。
        return _success({"status": "ok"})

    def _state(self, _body: Mapping[str, Any]) -> ApiResponse:
        return _success(self._state_payload())

    def _save_config(self, body: Mapping[str, Any]) -> ApiResponse:
        current, _error = self._load_config()
        # 首次配置或配置损坏时没有可合并的基线，用默认值起步，否则用户无法从错误状态恢复。
        baseline = current if current is not None else AppConfig()
        try:
            config = build_config(baseline, body)
        except (ValueError, TypeError, AttributeError) as error:
            logger.warning("配置构建失败：%s", error)
            return _failure(400, f"配置无效：{error}")

        validation = config.validate()
        if not validation.ok:
            logger.warning("配置校验未通过：%s", validation.message)
            return _failure(400, validation.message)

        saved = self._service.save_config(config)
        if not saved.ok:
            # 失败细节（失败步骤、路径、errno）已在 config.py 记录，
            # 这里补一条接口层上下文，便于把日志行与具体请求对应起来。
            logger.warning("配置保存被拒绝：%s", saved.message)
            return _failure(400, saved.message)

        message = "设置已保存，定时检查状态已同步。"
        try:
            self._scheduler.configure(config)
        except Exception:
            # 定时器同步失败不回滚已落盘的配置，只提示用户状态可能未生效。
            message = "设置已保存，但定时检查状态同步失败，请稍后重试。"
        return _success(self._state_payload(), message)

    def _detect_ipv6(self, _body: Mapping[str, Any]) -> ApiResponse:
        result = self._service.get_public_ipv6()
        if not result.ok or result.data is None:
            return _failure(502, result.message)
        address = str(result.data)
        with self._lock:
            self._console.ipv6 = address
            self._console.ipv6_checked_at = datetime.now().astimezone()
        return _success(self._state_payload(), result.message)

    def _query_dns(self, _body: Mapping[str, Any]) -> ApiResponse:
        result = self._service.get_dns_status()
        if not result.ok or result.data is None:
            return _failure(502, result.message)
        with self._lock:
            self._console.dns = result.data
            self._console.dns_checked_at = datetime.now().astimezone()
        return _success(self._state_payload(), result.message)

    def _test_notification(self, body: Mapping[str, Any]) -> ApiResponse:
        """用“已保存配置 + 表单当前值”发一条 Gotify 测试消息，不写配置。

        页面的测试按钮必须能在保存前用，否则用户得先把可能写错的凭据存下来才能验证。
        """
        current, _error = self._load_config()
        baseline = current if current is not None else AppConfig()
        try:
            config = build_config(baseline, body)
        except (ValueError, TypeError, AttributeError) as error:
            logger.warning("测试通知配置构建失败：%s", error)
            return _failure(400, f"配置无效：{error}")

        # 合并后仍缺字段属于用户输入问题（400），与上游发送失败（502）区分开。
        if not config.gotify_address.strip() or not config.gotify_token.strip():
            return _failure(400, "尚未填写 Gotify 地址或 Token，无法发送测试消息")

        result = self._service.send_test_notification(config)
        if not result.ok:
            return _failure(502, result.message)
        return _success(self._state_payload(), result.message)

    def _update(self, _body: Mapping[str, Any]) -> ApiResponse:
        result = self._service.check_and_update(source="manual")
        if not result.ok or result.data is None:
            with self._lock:
                self._console.update = {
                    "ok": False,
                    "action": "failed",
                    "changed": False,
                    "message": result.message,
                }
            return _failure(502, result.message)

        status = result.data
        config, _error = self._load_config()
        with self._lock:
            self._console.update = {
                "ok": True,
                "action": status.action,
                "changed": status.action in {"created", "updated"},
                "message": result.message,
            }
            self._console.ipv6 = status.ipv6
            self._console.ipv6_checked_at = datetime.now().astimezone()
            # 更新成功后 DNS 的当前值就是刚写入的地址，无需再查一次接口。
            self._console.dns = DnsStatus(
                provider=status.provider,
                # 优先用刚查到的记录身份；阿里云配置里没有记录名，只能靠它。
                record_name=status.record_name or _record_name_for(config, status.provider),
                record_type="AAAA",
                value=status.current_value,
                record_id="",
            )
            self._console.dns_checked_at = datetime.now().astimezone()
        return _success(self._state_payload(), result.message)

    # ---- 状态组装 ------------------------------------------------------------

    def _load_config(self) -> tuple[AppConfig | None, str | None]:
        result = self._service.load_config()
        if not result.ok or result.data is None:
            return None, result.message
        return result.data, None

    def _state_payload(self) -> dict[str, Any]:
        config, config_error = self._load_config()
        with self._lock:
            state = replace(self._console)

        return {
            "config": None if config is None else self._config_payload(config),
            "config_error": config_error,
            "ipv6": state.ipv6,
            "ipv6_checked_at": _iso(state.ipv6_checked_at),
            "dns": _dns_payload(state.dns),
            "dns_checked_at": _iso(state.dns_checked_at),
            "update": state.update,
            "schedule": self._schedule_payload(),
            "history": self._history_payload(),
            "limits": {
                "history_preview_size": HISTORY_PREVIEW_LIMIT,
                "history_max_records": DEFAULT_MAX_RECORDS,
            },
        }

    def _config_payload(self, config: AppConfig) -> dict[str, Any]:
        """输出非敏感字段；密钥统一降级成“是否已保存”。

        字段名与 build_config 读入的名字同源（_api_field_name），
        避免出现“读 cloudflare_zone_id、写 cloudfare_zone_id”这类单边改名。
        """
        payload = {
            _api_field_name(field.name): getattr(config, field.name)
            for field in fields(AppConfig)
            # 密钥字段必须排除：它们只能以 *_saved 布尔形式出现，否则明文会随状态接口回传。
            if field.name not in SECRET_FIELDS
        }
        for name in SECRET_FIELDS:
            payload[f"{_api_field_name(name)}_saved"] = bool(
                getattr(config, name).strip()
            )
        return payload

    def _schedule_payload(self) -> dict[str, Any] | None:
        try:
            snapshot = self._scheduler.snapshot()
        except Exception:
            # 调度器状态不应阻止用户配置或手动更新 DNS，页面会退回配置文件里的开关值。
            return None
        return {
            "enabled": bool(snapshot.enabled),
            "running": bool(snapshot.running),
            "interval_minutes": int(snapshot.interval_minutes),
            "next_run": _iso(snapshot.next_run),
            "last_run": _iso(snapshot.last_run),
            "last_ok": snapshot.last_ok,
            "last_message": snapshot.last_message,
        }

    def _history_payload(self) -> dict[str, Any]:
        result = self._service.history_store.load()
        records: list[CheckRecord] = list(result.data or []) if result.ok else []
        summary = summarize(records)
        return {
            "summary": {
                "total": summary.total,
                "succeeded": summary.succeeded,
                "failed": summary.failed,
                "changed": summary.changed,
                "last_run_at": _iso(summary.last_run_at),
                "last_change_at": _iso(summary.last_change_at),
            },
            "records": [
                _history_row(record) for record in records[:HISTORY_PREVIEW_LIMIT]
            ],
            "total": len(records),
            # 读取失败只是报告的一部分不可用，不应让整个页面报错。
            "error": None if result.ok else result.message,
        }

    def _history_page(self, params: Mapping[str, str]) -> ApiResponse:
        """分页返回检查记录，供首页浮层浏览全部历史。

        必须先夹取 page_size 再算 total_pages：page_size 为 0 会让除法抛
        ZeroDivisionError，被 handle() 的兜底变成 500，客户端只看到“服务内部错误”。

        分页切片放在这里而不是 store 里：store 只有“读取全部”一个语义，
        而“取第几页”是接口层的展示问题，和首页预览的截断是同一类逻辑。
        """
        page_size = _positive_int(
            params.get("page_size"), HISTORY_PAGE_SIZE, HISTORY_MAX_PAGE_SIZE
        )
        result = self._service.history_store.load()
        records: list[CheckRecord] = list(result.data or []) if result.ok else []
        total = len(records)
        # 空集也至少一页：page 的最小值是 1，取 0 会让 1 <= page <= total_pages 不成立，
        # 前端就得为空集单独写一套分支。
        total_pages = max(1, (total + page_size - 1) // page_size)
        # 越界时夹到最后一页而不是回空表：定时任务写入新记录会让内容整体后移，
        # 用户停在第 N 页时看到空表会以为历史丢了。
        page = min(_positive_int(params.get("page"), 1), total_pages)
        start = (page - 1) * page_size
        return _success(
            {
                "records": [
                    _history_row(record)
                    for record in records[start : start + page_size]
                ],
                "page": page,
                "page_size": page_size,
                "total": total,
                "total_pages": total_pages,
                # 与 /api/state 一致：读取失败只让这一块不可用，不整页报错。
                "error": None if result.ok else result.message,
            }
        )


# 无需会话即可访问的接口。三个都有明确理由：
# healthz 供容器探活，加了鉴权容器会永远 unhealthy；login 是拿到会话的入口；
# logout 只是让浏览器丢掉 Cookie，不需要任何权限，否则会话过期后就退不出去了。
_PUBLIC_ROUTES = frozenset({"/healthz", "/api/login", "/api/logout"})

_ROUTES: dict[str, tuple[frozenset[str], str]] = {
    "/healthz": (frozenset({"GET"}), "_health"),
    "/api/login": (frozenset({"POST"}), "_login"),
    "/api/logout": (frozenset({"POST"}), "_logout"),
    "/api/state": (frozenset({"GET"}), "_state"),
    "/api/history": (frozenset({"GET"}), "_history_page"),
    "/api/config": (frozenset({"POST"}), "_save_config"),
    "/api/ipv6": (frozenset({"POST"}), "_detect_ipv6"),
    "/api/dns": (frozenset({"POST"}), "_query_dns"),
    "/api/update": (frozenset({"POST"}), "_update"),
    "/api/notify/test": (frozenset({"POST"}), "_test_notification"),
}
