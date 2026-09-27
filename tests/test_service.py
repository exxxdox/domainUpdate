from pathlib import Path
from unittest.mock import MagicMock, patch

from domain_update.config import AppConfig, ConfigStore
from domain_update.models import Result
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
