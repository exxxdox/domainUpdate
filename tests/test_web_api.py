"""Web API 测试。

原 UI 由 Streamlit 提供，改为自带 HTTP 服务后，路由、请求体校验、
跨站防护与“密钥不回显”这些原本由框架兜底的行为都必须自己验证。
"""

import json
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Any
from unittest.mock import MagicMock, patch

import pytest

from domain_update.config import AppConfig, ConfigStore
from domain_update.history import DEFAULT_MAX_RECORDS, CheckHistoryStore, CheckRecord
from domain_update.models import DnsStatus, Result
from domain_update.scheduler import SchedulerSnapshot
from domain_update.service import DomainUpdateService
from domain_update.web.api import (
    HISTORY_MAX_PAGE_SIZE,
    HISTORY_PAGE_SIZE,
    HISTORY_PREVIEW_LIMIT,
    WebApi,
)

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
    # 主机记录 RR 字段已删除，接口不应再暴露一个已经无效的配置项。
    assert "alibaba_rr" not in config


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


# ---- 检查记录分页 -----------------------------------------------------------
#
# 首页只给概览，完整列表由 /api/history 分页提供。


def seed_history(tmp_path: Path, count: int) -> None:
    """按分钟递增写入 count 条记录，时间越晚的索引越大、排序后越靠前。"""
    store = CheckHistoryStore(tmp_path)
    base = datetime(2026, 9, 27, 12, 0, tzinfo=timezone.utc)
    for index in range(count):
        store.append(
            CheckRecord(
                timestamp=base + timedelta(minutes=index),
                source="scheduled",
                ok=True,
                action="unchanged",
                message=f"第{index}次",
                ipv6="240e::1",
            )
        )


def history_page(api: WebApi, query: str = "") -> Any:
    """分页接口是 GET，没有请求体，参数走查询串。"""
    return api.handle("GET", f"/api/history{query}")


def test_history_page_returns_first_page_by_default(tmp_path: Path) -> None:
    seed_history(tmp_path, 60)

    response = history_page(make_api(tmp_path))
    data = response.payload["data"]

    assert response.status == 200
    assert data["page"] == 1
    assert data["page_size"] == HISTORY_PAGE_SIZE
    assert data["total"] == 60
    assert data["total_pages"] == 2
    assert len(data["records"]) == HISTORY_PAGE_SIZE


def test_history_page_returns_last_partial_page(tmp_path: Path) -> None:
    seed_history(tmp_path, 60)

    data = history_page(make_api(tmp_path), "?page=2").payload["data"]

    assert len(data["records"]) == 10


def test_history_page_records_are_newest_first_and_do_not_overlap(
    tmp_path: Path,
) -> None:
    seed_history(tmp_path, 60)
    api = make_api(tmp_path)

    first = history_page(api, "?page=1").payload["data"]["records"]
    second = history_page(api, "?page=2").payload["data"]["records"]

    # time 是同一格式的 ISO 8601，可以直接比字符串。
    assert first[0]["time"] > first[-1]["time"]
    assert first[-1]["time"] > second[0]["time"]
    assert {row["time"] for row in first}.isdisjoint({row["time"] for row in second})


def test_history_page_clamps_page_beyond_last_page(tmp_path: Path) -> None:
    seed_history(tmp_path, 60)

    data = history_page(make_api(tmp_path), "?page=99").payload["data"]

    # 夹到最后一页而不是回空表：用户停在第 N 页时，定时任务写入新记录会让内容整体后移，
    # 空表会被误读成“历史丢了”。
    assert data["page"] == data["total_pages"] == 2
    assert len(data["records"]) == 10


def test_history_page_clamps_page_size_to_maximum(tmp_path: Path) -> None:
    seed_history(tmp_path, 3)

    data = history_page(
        make_api(tmp_path), f"?page_size={HISTORY_MAX_PAGE_SIZE + 1}"
    ).payload["data"]

    assert data["page_size"] == HISTORY_MAX_PAGE_SIZE


@pytest.mark.parametrize("value", ["0", "-1", "abc", ""])
def test_history_page_falls_back_on_invalid_page_size(
    tmp_path: Path, value: str
) -> None:
    """page_size 非法时必须回落到默认值，而不是让上层报 500。

    这是 ZeroDivisionError 的回归锁：page_size=0 会让 total_pages 的计算除零，
    被 handle() 的兜底捕获成“服务内部错误”，客户端拿不到任何有用信息。
    """
    seed_history(tmp_path, 3)

    response = history_page(make_api(tmp_path), f"?page_size={value}")

    assert response.status == 200
    assert response.payload["data"]["page_size"] == HISTORY_PAGE_SIZE


@pytest.mark.parametrize("value", ["0", "-5", "abc"])
def test_history_page_falls_back_on_invalid_page(tmp_path: Path, value: str) -> None:
    seed_history(tmp_path, 3)

    data = history_page(make_api(tmp_path), f"?page={value}").payload["data"]

    assert data["page"] == 1


def test_history_page_reports_total_pages_one_for_empty_store(tmp_path: Path) -> None:
    data = history_page(make_api(tmp_path)).payload["data"]

    # 空集也取 1：page 的最小值是 1，取 0 会破坏 1 <= page <= total_pages 这条不变式。
    assert data["total"] == 0
    assert data["total_pages"] == 1
    assert data["records"] == []


def test_history_page_row_shape_matches_state_preview(tmp_path: Path) -> None:
    seed_history(tmp_path, 3)
    api = make_api(tmp_path)

    page_row = history_page(api).payload["data"]["records"][0]
    state_row = api.handle("GET", "/api/state").payload["data"]["history"]["records"][0]

    # 两个入口共用 _history_row()，列名与占位符必须完全一致。
    assert page_row == state_row


def test_history_page_reports_store_error_without_500(tmp_path: Path) -> None:
    api = make_api(tmp_path)
    failure = Result.failure("检查记录读取失败（OSError）")

    with patch.object(CheckHistoryStore, "load", return_value=failure):
        response = history_page(api)

    assert response.status == 200
    assert response.payload["data"]["records"] == []
    assert response.payload["data"]["error"] == "检查记录读取失败（OSError）"


def test_history_page_rejects_post(tmp_path: Path) -> None:
    assert post(make_api(tmp_path), "/api/history").status == 405


def test_history_page_ignores_unknown_query_params(tmp_path: Path) -> None:
    seed_history(tmp_path, 3)

    response = history_page(make_api(tmp_path), "?foo=1&page=1")

    assert response.status == 200


def test_state_history_preview_is_limited_to_preview_size(tmp_path: Path) -> None:
    seed_history(tmp_path, 12)

    history = make_api(tmp_path).handle("GET", "/api/state").payload["data"]["history"]

    # 首页只做概览，完整列表走 /api/history 分页；total 仍是全量。
    assert len(history["records"]) == HISTORY_PREVIEW_LIMIT
    assert history["total"] == 12


def test_state_limits_report_preview_size_and_max_records(tmp_path: Path) -> None:
    limits = make_api(tmp_path).handle("GET", "/api/state").payload["data"]["limits"]

    assert limits["history_preview_size"] == HISTORY_PREVIEW_LIMIT
    assert limits["history_max_records"] == DEFAULT_MAX_RECORDS


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


@patch("domain_update.service.send_gotify")
def test_notify_test_uses_saved_credentials(gotify: MagicMock, tmp_path: Path) -> None:
    gotify.return_value = Result.success("通知已发送", True)
    api = make_api(tmp_path)

    response = post(api, "/api/notify/test")

    assert response.status == 200
    assert "测试消息已发送" in response.payload["message"]
    sent_config = gotify.call_args.args[0]
    # 表单留空时用已保存的配置发送：测试按钮不该强迫用户先保存一次。
    assert sent_config.gotify_address == "notify.example.com"
    assert sent_config.gotify_token == "gotify-token"


@patch("domain_update.service.send_gotify")
def test_notify_test_prefers_form_values_without_saving(
    gotify: MagicMock, tmp_path: Path
) -> None:
    gotify.return_value = Result.success("通知已发送", True)
    api = make_api(tmp_path)

    response = post(
        api,
        "/api/notify/test",
        {"gotify_address": "new.example.com", "gotify_token": "new-token"},
    )

    assert response.status == 200
    sent_config = gotify.call_args.args[0]
    assert sent_config.gotify_address == "new.example.com"
    assert sent_config.gotify_token == "new-token"
    # 测试只读不写：验证新凭据不得覆盖已保存的配置。
    saved = ConfigStore(tmp_path).load().data
    assert saved is not None
    assert saved.gotify_token == "gotify-token"


@patch("domain_update.service.send_gotify")
def test_notify_test_keeps_saved_token_when_field_left_blank(
    gotify: MagicMock, tmp_path: Path
) -> None:
    gotify.return_value = Result.success("通知已发送", True)
    api = make_api(tmp_path)

    post(
        api,
        "/api/notify/test",
        {"gotify_address": "new.example.com", "gotify_token": "   "},
    )

    # 与保存配置同一条规则：密钥框留空表示“保留原值”，否则改地址就会误清 Token。
    assert gotify.call_args.args[0].gotify_token == "gotify-token"


@patch("domain_update.service.send_gotify")
def test_notify_test_requires_configuration(gotify: MagicMock, tmp_path: Path) -> None:
    store = ConfigStore(tmp_path)
    store.save(
        AppConfig(
            cloudfare_token="secret-token",
            cloudfare_zone_id="zone-id",
            cloudfare_record_name="home.example.com",
        )
    )
    api = WebApi(
        service=DomainUpdateService(store), scheduler=StubScheduler(_snapshot())
    )

    response = post(api, "/api/notify/test")

    # 缺配置是用户输入问题（400），不应被当成上游故障。
    assert response.status == 400
    assert "Gotify" in response.payload["message"]
    gotify.assert_not_called()


@patch("domain_update.service.send_gotify")
def test_notify_test_reports_send_failure(gotify: MagicMock, tmp_path: Path) -> None:
    gotify.return_value = Result.failure("Gotify 通知发送失败")
    api = make_api(tmp_path)

    response = post(api, "/api/notify/test")

    # 发送失败是上游问题：502，且文案要贴合“测试消息”场景而不是“DNS 已更新”。
    assert response.status == 502
    assert "测试消息发送失败" in response.payload["message"]


def test_config_save_survives_non_text_values(tmp_path: Path) -> None:
    api = make_api(tmp_path)

    response = post(
        api, "/api/config", {"provider": "cloudflare", "cloudflare_zone_id": 123}
    )

    # 文本字段收到数字属于请求体问题，但绝不能一路传到 validate() 的 .strip()
    # 上抛 AttributeError，被 handle() 兜底成 500（服务端故障）。
    assert response.status == 200
    saved = ConfigStore(tmp_path).load().data
    assert saved is not None
    assert saved.cloudfare_zone_id == "123"


def test_config_save_treats_null_text_field_as_empty(tmp_path: Path) -> None:
    api = make_api(tmp_path)

    response = post(
        api, "/api/config", {"provider": "cloudflare", "cloudflare_record_name": None}
    )

    # null 表示清空，而不是变成字符串 "None" 被当成一个域名存下去。
    assert response.status == 400
    saved = ConfigStore(tmp_path).load().data
    assert saved is not None
    assert saved.cloudfare_record_name == "home.example.com"


@patch("domain_update.service.send_gotify")
def test_notify_test_survives_non_text_address(
    gotify: MagicMock, tmp_path: Path
) -> None:
    gotify.return_value = Result.success("通知已发送", True)
    api = make_api(tmp_path)

    response = post(api, "/api/notify/test", {"gotify_address": 123})

    # 地址归一成字符串后仍然走正常发送流程，而不是 500。
    assert response.status == 200
    assert gotify.call_args.args[0].gotify_address == "123"


@patch("domain_update.service.send_gotify")
def test_notify_test_response_never_echoes_token(
    gotify: MagicMock, tmp_path: Path
) -> None:
    gotify.return_value = Result.success("通知已发送", True)
    api = make_api(tmp_path)

    response = post(api, "/api/notify/test", {"gotify_token": "brand-new-token"})

    # 新接口同样复用 _config_payload，密钥只能以 *_saved 布尔出现。
    serialized = json.dumps(response.payload, ensure_ascii=False)
    assert "brand-new-token" not in serialized
    assert "gotify-token" not in serialized
    assert response.payload["data"]["config"]["gotify_token_saved"] is True


def test_notify_test_rejects_cross_site_origin(tmp_path: Path) -> None:
    api = make_api(tmp_path)

    response = api.handle(
        "POST",
        "/api/notify/test",
        raw_body=b"{}",
        headers={
            "content-type": "application/json",
            "host": "127.0.0.1:8501",
            "origin": "http://evil.example.com",
        },
    )

    assert response.status == 403


def test_notify_test_requires_json_content_type(tmp_path: Path) -> None:
    api = make_api(tmp_path)

    response = api.handle(
        "POST",
        "/api/notify/test",
        raw_body=b'{"gotify_address":"notify.example.com"}',
        headers={"content-type": "text/plain", "host": "127.0.0.1:8501"},
    )

    assert response.status == 415


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


def test_post_rejects_deeply_nested_json(tmp_path: Path) -> None:
    """嵌套深度超过解释器递归上限的 JSON 必须回 400，而不是让异常穿透出去。

    json.loads 在这种情况下抛的是 RecursionError 而不是 ValueError。漏接它会一路
    穿透到 BaseHTTPRequestHandler：客户端拿不到任何响应，只有连接被丢弃，堆栈也绕过
    统一日志直接打到 stderr。
    """
    api = make_api(tmp_path)
    # 40000 字节，在服务端的 64KB 请求体上限之内，属于会被正常受理的请求。
    raw_body = ("[" * 20_000 + "]" * 20_000).encode("utf-8")

    response = api.handle("POST", "/api/ipv6", raw_body=raw_body, headers=JSON_HEADERS)

    assert response.status == 400
    assert response.payload["ok"] is False


def test_wrong_method_returns_405(tmp_path: Path) -> None:
    api = make_api(tmp_path)

    assert api.handle("GET", "/api/update").status == 405
    assert post(api, "/api/state").status == 405
