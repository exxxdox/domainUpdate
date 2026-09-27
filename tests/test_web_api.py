"""Web API 测试。

原 UI 由 Streamlit 提供，改为自带 HTTP 服务后，路由、请求体校验、
跨站防护与“密钥不回显”这些原本由框架兜底的行为都必须自己验证。
"""

import json
from datetime import datetime, timezone
from pathlib import Path
from typing import Any
from unittest.mock import MagicMock, patch

from domain_update.config import AppConfig, ConfigStore
from domain_update.history import CheckHistoryStore, CheckRecord
from domain_update.models import DnsStatus, Result
from domain_update.scheduler import SchedulerSnapshot
from domain_update.service import DomainUpdateService
from domain_update.web.api import WebApi

JSON_HEADERS = {"content-type": "application/json", "host": "127.0.0.1:8501"}


class StubScheduler:
    """只实现 API 用到的两个方法，避免测试里真的起后台线程。"""

    def __init__(self, snapshot: SchedulerSnapshot | None = None) -> None:
        self._snapshot = snapshot
        self.configured: list[AppConfig] = []

    def configure(self, config: AppConfig) -> Result[SchedulerSnapshot]:
        self.configured.append(config)
        return Result.success("ok", self._snapshot or _snapshot())

    def snapshot(self) -> SchedulerSnapshot:
        if self._snapshot is None:
            raise RuntimeError("调度器不可用")
        return self._snapshot


def _snapshot(**overrides: Any) -> SchedulerSnapshot:
    values: dict[str, Any] = {
        "enabled": True,
        "running": True,
        "interval_minutes": 15,
        "next_run": datetime(2026, 9, 27, 13, 0, tzinfo=timezone.utc),
        "last_run": datetime(2026, 9, 27, 12, 0, tzinfo=timezone.utc),
        "last_ok": True,
        "last_message": "Cloudflare AAAA 记录更新成功",
    }
    values.update(overrides)
    return SchedulerSnapshot(**values)


def make_api(
    tmp_path: Path,
    config: AppConfig | None = None,
    scheduler: StubScheduler | None = None,
) -> WebApi:
    store = ConfigStore(tmp_path)
    store.save(
        config
        or AppConfig(
            cloudfare_token="secret-token",
            cloudfare_zone_id="zone-id",
            cloudfare_record_name="home.example.com",
            alibaba_cloud_access_key_secret="secret-key",
            gotify_token="gotify-token",
            gotify_address="notify.example.com",
        )
    )
    return WebApi(
        service=DomainUpdateService(store),
        scheduler=scheduler if scheduler is not None else StubScheduler(_snapshot()),
    )


def post(api: WebApi, path: str, payload: dict[str, Any] | None = None) -> Any:
    return api.handle(
        "POST",
        path,
        raw_body=json.dumps(payload or {}).encode("utf-8"),
        headers=JSON_HEADERS,
    )


def test_health_endpoint_does_not_touch_configuration(tmp_path: Path) -> None:
    api = make_api(tmp_path)

    response = api.handle("GET", "/healthz")

    assert response.status == 200
    assert response.payload["ok"] is True


def test_state_never_returns_saved_secrets(tmp_path: Path) -> None:
    response = make_api(tmp_path).handle("GET", "/api/state")
    serialized = json.dumps(response.payload, ensure_ascii=False)

    assert response.status == 200
    config = response.payload["data"]["config"]
    # 密钥只能以“是否已保存”的形式出现，任何字段都不得回显明文。
    assert config["cloudflare_token_saved"] is True
    assert config["alibaba_access_key_secret_saved"] is True
    assert config["gotify_token_saved"] is True
    for secret in ("secret-token", "secret-key", "gotify-token"):
        assert secret not in serialized


def test_state_exposes_provider_and_non_secret_fields(tmp_path: Path) -> None:
    response = make_api(tmp_path).handle("GET", "/api/state")

    config = response.payload["data"]["config"]
    assert config["provider"] == "cloudflare"
    assert config["cloudflare_zone_id"] == "zone-id"
    assert config["cloudflare_record_name"] == "home.example.com"
    assert config["gotify_address"] == "notify.example.com"
    assert config["schedule_interval_minutes"] == 10


def test_state_reports_scheduler_snapshot(tmp_path: Path) -> None:
    api = make_api(tmp_path, scheduler=StubScheduler(_snapshot(interval_minutes=42)))

    schedule = api.handle("GET", "/api/state").payload["data"]["schedule"]

    assert schedule["running"] is True
    assert schedule["interval_minutes"] == 42
    assert schedule["next_run"] == "2026-09-27T13:00:00+00:00"


def test_state_survives_broken_scheduler(tmp_path: Path) -> None:
    # 调度器异常不应让整个页面不可用，服务商状态仍要能展示。
    api = make_api(tmp_path, scheduler=StubScheduler(None))

    data = api.handle("GET", "/api/state").payload["data"]

    assert data["schedule"] is None
    assert data["config"]["provider"] == "cloudflare"


def test_state_reports_history_summary_and_rows(tmp_path: Path) -> None:
    store = CheckHistoryStore(tmp_path)
    store.append(
        CheckRecord(
            timestamp=datetime(2026, 9, 27, 12, 0, tzinfo=timezone.utc),
            source="scheduled",
            ok=True,
            action="updated",
            message="Cloudflare AAAA 记录更新成功",
            ipv6="240e::9",
            previous_value="240e::0",
        )
    )
    store.append(
        CheckRecord(
            timestamp=datetime(2026, 9, 27, 12, 10, tzinfo=timezone.utc),
            source="manual",
            ok=False,
            action="failed",
            message="公网 IPv6 获取失败",
        )
    )
    api = make_api(tmp_path)

    history = api.handle("GET", "/api/state").payload["data"]["history"]

    assert history["summary"]["total"] == 2
    assert history["summary"]["succeeded"] == 1
    assert history["summary"]["failed"] == 1
    assert history["summary"]["changed"] == 1
    assert history["total"] == 2
    # 记录按时间倒序，最新的失败记录排在最前。
    newest, oldest = history["records"][0], history["records"][1]
    assert newest["source"] == "手动"
    assert newest["result"] == "失败"
    # 失败的检查没有对记录做任何动作，动作列显示“—”而不是重复一遍“失败”。
    assert newest["action"] == "—"
    assert oldest["source"] == "定时"
    assert oldest["action"] == "更新记录"
    assert oldest["previous_value"] == "240e::0"


def test_state_reports_empty_history_without_error(tmp_path: Path) -> None:
    history = make_api(tmp_path).handle("GET", "/api/state").payload["data"]["history"]

    assert history["summary"]["total"] == 0
    assert history["records"] == []


def test_state_reports_config_error_instead_of_crashing(tmp_path: Path) -> None:
    # 配置文件损坏时页面仍要能打开，让用户重新保存一份有效配置。
    (tmp_path / "config.json").write_text("{ broken", encoding="utf-8")
    api = WebApi(
        service=DomainUpdateService(ConfigStore(tmp_path)),
        scheduler=StubScheduler(_snapshot()),
    )

    response = api.handle("GET", "/api/state")

    assert response.status == 200
    assert response.payload["data"]["config"] is None
    assert response.payload["data"]["config_error"]


def test_config_save_persists_changes(tmp_path: Path) -> None:
    api = make_api(tmp_path)

    response = post(
        api,
        "/api/config",
        {
            "provider": "cloudflare",
            "cloudflare_zone_id": "new-zone",
            "cloudflare_record_name": "new.example.com",
            "schedule_enabled": True,
            "schedule_interval_minutes": 30,
        },
    )

    assert response.status == 200
    saved = ConfigStore(tmp_path).load().data
    assert saved is not None
    assert saved.cloudfare_zone_id == "new-zone"
    assert saved.cloudfare_record_name == "new.example.com"
    assert saved.schedule_enabled is True
    assert saved.check_interval_minutes == 30


def test_config_save_keeps_secret_when_field_left_blank(tmp_path: Path) -> None:
    api = make_api(tmp_path)

    post(api, "/api/config", {"provider": "cloudflare", "cloudflare_token": "   "})

    saved = ConfigStore(tmp_path).load().data
    assert saved is not None
    # 页面不回显密钥，因此留空必须表示“保留原值”，否则用户会误清空凭据。
    assert saved.cloudfare_token == "secret-token"


def test_config_save_clears_secret_when_flagged(tmp_path: Path) -> None:
    api = make_api(tmp_path)

    # 只能清除“非当前服务商”的密钥，当前服务商的必填密钥由 validate 兜住（见下一个用例）。
    response = post(
        api,
        "/api/config",
        {"provider": "cloudflare", "clear_alibaba_access_key_secret": True},
    )

    assert response.status == 200
    saved = ConfigStore(tmp_path).load().data
    assert saved is not None
    assert saved.alibaba_cloud_access_key_secret == ""


def test_config_save_refuses_to_clear_active_provider_secret(tmp_path: Path) -> None:
    api = make_api(tmp_path)

    response = post(
        api,
        "/api/config",
        {"provider": "cloudflare", "clear_cloudflare_token": True},
    )

    # 清掉当前服务商的 API Token 会让配置无法使用，必须连原配置一起保持不变。
    assert response.status == 400
    saved = ConfigStore(tmp_path).load().data
    assert saved is not None
    assert saved.cloudfare_token == "secret-token"


def test_config_save_rejects_invalid_payload(tmp_path: Path) -> None:
    api = make_api(tmp_path)

    response = post(api, "/api/config", {"provider": "route53"})

    assert response.status == 400
    assert response.payload["ok"] is False
    # 失败时保留原配置，不能写入半截结果。
    saved = ConfigStore(tmp_path).load().data
    assert saved is not None
    assert saved.provider == "cloudflare"


def test_config_save_reports_missing_fields(tmp_path: Path) -> None:
    store = ConfigStore(tmp_path)
    store.save(AppConfig(provider="cloudflare"))
    api = WebApi(
        service=DomainUpdateService(store), scheduler=StubScheduler(_snapshot())
    )

    response = post(api, "/api/config", {"provider": "cloudflare"})

    assert response.status == 400
    assert "Cloudflare API Token" in response.payload["message"]


def test_config_save_reconfigures_scheduler(tmp_path: Path) -> None:
    scheduler = StubScheduler(_snapshot())
    api = make_api(tmp_path, scheduler=scheduler)

    post(api, "/api/config", {"provider": "cloudflare", "schedule_enabled": True})

    assert scheduler.configured[-1].schedule_enabled is True


@patch("domain_update.service.send_gotify")
@patch("domain_update.service.create_provider")
@patch("domain_update.service.get_public_ipv6")
def test_update_endpoint_records_manual_history(
    ipv6_getter: MagicMock,
    provider_factory: MagicMock,
    gotify: MagicMock,
    tmp_path: Path,
) -> None:
    ipv6_getter.return_value = Result.success("ok", "240e::9")
    gotify.return_value = Result.success("已跳过", False)
    provider = provider_factory.return_value
    provider.get_status.return_value = Result.success(
        "ok",
        DnsStatus(
            provider="cloudflare",
            record_name="home.example.com",
            record_type="AAAA",
            value="240e::0",
            record_id="record-1",
        ),
    )
    provider.set_ipv6.return_value = Result.success("更新成功", "updated")
    api = make_api(tmp_path)

    response = post(api, "/api/update")

    assert response.status == 200
    data = response.payload["data"]
    # 动作接口统一返回完整状态，"本次动作"的信息在 data["update"] 下。
    assert data["ipv6"] == "240e::9"
    assert data["update"]["action"] == "updated"
    assert data["update"]["ok"] is True
    # 更新成功后 DNS 面板直接反映刚写入的地址，不需要再查一次接口。
    assert data["dns"]["value"] == "240e::9"
    records = CheckHistoryStore(tmp_path).load().data
    assert records is not None and records[0].source == "manual"


@patch("domain_update.service.get_public_ipv6")
def test_ipv6_endpoint_returns_detected_address(
    ipv6_getter: MagicMock, tmp_path: Path
) -> None:
    ipv6_getter.return_value = Result.success("ok", "240e::1")
    api = make_api(tmp_path)

    response = post(api, "/api/ipv6")

    assert response.status == 200
    assert response.payload["data"]["ipv6"] == "240e::1"
    # 检测结果留在服务端状态里，刷新页面后仍能看到上次检测值。
    state = api.handle("GET", "/api/state").payload["data"]
    assert state["ipv6"] == "240e::1"
    assert state["ipv6_checked_at"] is not None


@patch("domain_update.service.get_public_ipv6")
def test_ipv6_endpoint_reports_failure(
    ipv6_getter: MagicMock, tmp_path: Path
) -> None:
    ipv6_getter.return_value = Result.failure("公网 IPv6 获取失败")
    api = make_api(tmp_path)

    response = post(api, "/api/ipv6")

    # 上游网络失败用 502，与用户输入错误（400）区分开。
    assert response.status == 502
    assert response.payload["message"] == "公网 IPv6 获取失败"


def test_post_requires_json_content_type(tmp_path: Path) -> None:
    api = make_api(tmp_path)

    # 浏览器表单只能发这几种 Content-Type，强制 JSON 即可挡住跨站表单提交（CSRF）。
    for content_type in (
        "application/x-www-form-urlencoded",
        "text/plain",
        "multipart/form-data",
    ):
        response = api.handle(
            "POST",
            "/api/config",
            raw_body=b'{"provider":"alibaba"}',
            headers={"content-type": content_type, "host": "127.0.0.1:8501"},
        )
        assert response.status == 415, content_type


def test_post_rejects_cross_site_origin(tmp_path: Path) -> None:
    api = make_api(tmp_path)

    response = api.handle(
        "POST",
        "/api/update",
        raw_body=b"{}",
        headers={
            "content-type": "application/json",
            "host": "127.0.0.1:8501",
            "origin": "http://evil.example.com",
        },
    )

    assert response.status == 403


def test_request_accepts_same_origin(tmp_path: Path) -> None:
    api = make_api(tmp_path)

    response = api.handle(
        "GET",
        "/api/state",
        headers={"host": "127.0.0.1:8501", "origin": "http://127.0.0.1:8501"},
    )

    assert response.status == 200


def test_post_rejects_malformed_json(tmp_path: Path) -> None:
    api = make_api(tmp_path)

    response = api.handle(
        "POST",
        "/api/config",
        raw_body=b"{not json",
        headers=JSON_HEADERS,
    )

    assert response.status == 400


def test_unknown_path_returns_404(tmp_path: Path) -> None:
    assert make_api(tmp_path).handle("GET", "/api/nope").status == 404


def test_wrong_method_returns_405(tmp_path: Path) -> None:
    api = make_api(tmp_path)

    assert api.handle("GET", "/api/update").status == 405
    assert post(api, "/api/state").status == 405
