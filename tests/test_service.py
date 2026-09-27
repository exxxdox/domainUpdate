from pathlib import Path
from unittest.mock import MagicMock, patch

from domain_update.config import AppConfig, ConfigStore
from domain_update.history import CheckHistoryStore, CheckRecord
from domain_update.models import DnsStatus, Result
from domain_update.service import DomainUpdateService


@patch("domain_update.service.create_provider")
@patch("domain_update.service.get_public_ipv6")
def test_query_failure_never_writes_dns(
    ipv6_getter: MagicMock,
    provider_factory: MagicMock,
    tmp_path: Path,
) -> None:
    store = ConfigStore(tmp_path)
    store.save(
        AppConfig(
            cloudfare_token="token",
            cloudfare_zone_id="zone",
            cloudfare_record_name="home.example.com",
        )
    )
    ipv6_getter.return_value = Result.success("ok", "240e::1")
    provider = provider_factory.return_value
    provider.get_status.return_value = Result.failure("query failed")

    result = DomainUpdateService(store).check_and_update()

    assert not result.ok
    provider.set_ipv6.assert_not_called()


@patch("domain_update.service.create_provider")
@patch("domain_update.service.get_public_ipv6")
def test_successful_check_is_recorded_with_source(
    ipv6_getter: MagicMock,
    provider_factory: MagicMock,
    tmp_path: Path,
) -> None:
    store = ConfigStore(tmp_path)
    store.save(
        AppConfig(
            cloudfare_token="token",
            cloudfare_zone_id="zone",
            cloudfare_record_name="home.example.com",
        )
    )
    ipv6_getter.return_value = Result.success("ok", "240e::9")
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

    DomainUpdateService(store).check_and_update(source="scheduled")

    records = CheckHistoryStore(tmp_path).load().data
    assert len(records) == 1
    record = records[0]
    assert record.source == "scheduled"
    assert record.ok
    assert record.action == "updated"
    assert record.ipv6 == "240e::9"
    assert record.previous_value == "240e::0"


@patch("domain_update.service.create_provider")
@patch("domain_update.service.get_public_ipv6")
def test_failed_check_is_recorded_and_defaults_to_manual_source(
    ipv6_getter: MagicMock,
    provider_factory: MagicMock,
    tmp_path: Path,
) -> None:
    store = ConfigStore(tmp_path)
    store.save(
        AppConfig(
            cloudfare_token="token",
            cloudfare_zone_id="zone",
            cloudfare_record_name="home.example.com",
        )
    )
    ipv6_getter.return_value = Result.success("ok", "240e::9")
    provider_factory.return_value.get_status.return_value = Result.failure("查询失败")

    DomainUpdateService(store).check_and_update()

    records = CheckHistoryStore(tmp_path).load().data
    assert len(records) == 1
    assert records[0].source == "manual"
    assert not records[0].ok
    assert "查询失败" in records[0].message


@patch("domain_update.service.create_provider")
@patch("domain_update.service.get_public_ipv6")
def test_history_write_failure_does_not_break_update(
    ipv6_getter: MagicMock,
    provider_factory: MagicMock,
    tmp_path: Path,
) -> None:
    class BrokenHistory(CheckHistoryStore):
        """不落地文件、写入恒失败的替身，用于验证历史属于旁路能力。"""

        def __init__(self) -> None:
            pass

        def append(self, record: CheckRecord) -> Result[CheckRecord]:
            return Result.failure("磁盘只读")

    store = ConfigStore(tmp_path)
    store.save(
        AppConfig(
            cloudfare_token="token",
            cloudfare_zone_id="zone",
            cloudfare_record_name="home.example.com",
        )
    )
    ipv6_getter.return_value = Result.success("ok", "240e::9")
    provider = provider_factory.return_value
    provider.get_status.return_value = Result.success("ok", None)
    provider.set_ipv6.return_value = Result.success("创建成功", "created")

    # 记录只是旁路能力，写不进去也必须返回 DNS 更新的真实结果。
    result = DomainUpdateService(store, history_store=BrokenHistory()).check_and_update()

    assert result.ok
    assert result.data is not None
    assert result.data.action == "created"
