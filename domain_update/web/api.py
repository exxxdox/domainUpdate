"""控制台的 JSON API。

原实现由 Streamlit 承载路由、会话状态与请求校验，改为自带 HTTP 服务后这些都需要显式实现。
集中放在本模块的目的：安全策略（跨站防护、请求体校验）与状态管理可以脱离 socket 单元测试，
`server.py` 只负责把 http.server 的请求翻译成这里的 `handle()` 调用。
"""

from __future__ import annotations

import json
import logging
import threading
from dataclasses import asdict, dataclass, fields, replace
from datetime import datetime
from typing import Any, Mapping, Protocol
from urllib.parse import urlsplit

from domain_update.config import AppConfig
from domain_update.history import DEFAULT_MAX_RECORDS, CheckRecord, summarize
from domain_update.models import DnsStatus
from domain_update.service import DomainUpdateService


# 表格只展示最近若干条，汇总仍基于全部已保留记录。
HISTORY_PAGE_SIZE = 50

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
    "alibaba_cloud_rr": "alibaba_rr",
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


@dataclass
class ConsoleState:
    """页面共享的最近一次操作结果。进程内单实例，容器重启即重置。"""

    ipv6: str | None = None
    ipv6_checked_at: datetime | None = None
    dns: DnsStatus | None = None
    dns_checked_at: datetime | None = None
    update: dict[str, Any] | None = None


def _success(data: Any, message: str = "") -> ApiResponse:
    return ApiResponse(200, {"ok": True, "message": message, "data": data})


def _failure(status: int, message: str) -> ApiResponse:
    return ApiResponse(status, {"ok": False, "message": message, "data": None})


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
    """按服务商取目标记录名：两家把记录名存在不同字段里。"""
    if config is None:
        return ""
    if provider == "cloudflare":
        return config.cloudfare_record_name
    return config.alibaba_cloud_rr


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

    # ---- 路由 ----------------------------------------------------------------

    def handle(
        self,
        method: str,
        path: str,
        raw_body: bytes = b"",
        headers: Mapping[str, str] | None = None,
    ) -> ApiResponse:
        method = method.upper()
        normalized = {
            str(key).lower(): str(value) for key, value in (headers or {}).items()
        }

        cross_site = self._reject_cross_site(normalized)
        if cross_site is not None:
            return cross_site

        route = _ROUTES.get(path.split("?", 1)[0].rstrip("/") or "/")
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
            body = self._parse_body(raw_body) if method == "POST" else {}
        except ValueError:
            return _failure(400, "请求体不是合法 JSON 对象")
        except RecursionError:
            # 嵌套深度超过解释器递归上限时，json.loads 抛的是 RecursionError 而不是
            # ValueError。漏接它会一路穿透到 BaseHTTPRequestHandler：客户端拿不到任何
            # 响应，只有连接被丢弃，堆栈也绕过统一日志直接打到 stderr。
            return _failure(400, "请求体 JSON 嵌套层级过深")

        handler = getattr(self, handler_name)
        try:
            return handler(body)
        except Exception:
            # 未预期异常既不能让连接直接断掉，也不能把堆栈回给浏览器。
            logger.exception("处理请求失败：%s %s", method, path)
            return _failure(500, "服务内部错误，请查看容器日志")

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
                record_name=_record_name_for(config, status.provider),
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
                "history_page_size": HISTORY_PAGE_SIZE,
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
            "records": [_history_row(record) for record in records[:HISTORY_PAGE_SIZE]],
            "total": len(records),
            # 读取失败只是报告的一部分不可用，不应让整个页面报错。
            "error": None if result.ok else result.message,
        }


_ROUTES: dict[str, tuple[frozenset[str], str]] = {
    "/healthz": (frozenset({"GET"}), "_health"),
    "/api/state": (frozenset({"GET"}), "_state"),
    "/api/config": (frozenset({"POST"}), "_save_config"),
    "/api/ipv6": (frozenset({"POST"}), "_detect_ipv6"),
    "/api/dns": (frozenset({"POST"}), "_query_dns"),
    "/api/update": (frozenset({"POST"}), "_update"),
    "/api/notify/test": (frozenset({"POST"}), "_test_notification"),
}
