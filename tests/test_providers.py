from types import SimpleNamespace
from unittest.mock import MagicMock, patch

from domain_update.config import AppConfig
from domain_update.models import DnsStatus
from domain_update.providers import AlibabaProvider, CloudflareProvider


def cloudflare_config() -> AppConfig:
    return AppConfig(
        cloudfare_token="token",
        cloudfare_zone_id="zone-id",
        cloudfare_record_name="home.example.com",
    )


@patch("domain_update.providers.Cloudflare")
def test_cloudflare_update_uses_configured_record_name(client_type: MagicMock) -> None:
    client = client_type.return_value
    provider = CloudflareProvider(cloudflare_config())
    current = DnsStatus(
        provider="cloudflare",
        record_name="home.example.com",
        record_type="AAAA",
        value="240e::1",
        record_id="record-id",
        proxied=True,
        ttl=300,
    )

    result = provider.set_ipv6("240e::2", current)

    assert result.ok
    assert result.data == "updated"
    client.dns.records.edit.assert_called_once_with(
        dns_record_id="record-id",
        zone_id="zone-id",
        content="240e::2",
        name="home.example.com",
        proxied=True,
        type="AAAA",
        ttl=300,
    )


@patch("domain_update.providers.Cloudflare")
def test_cloudflare_reports_missing_record_without_failure(client_type: MagicMock) -> None:
    client_type.return_value.dns.records.list.return_value = []

    result = CloudflareProvider(cloudflare_config()).get_status()

    assert result.ok
    assert result.data is None


@patch("domain_update.providers.Cloudflare")
def test_cloudflare_filters_record_type(client_type: MagicMock) -> None:
    client_type.return_value.dns.records.list.return_value = [
        SimpleNamespace(
            name="home.example.com",
            type="AAAA",
            content="240e::1",
            id="record-id",
        )
    ]

    result = CloudflareProvider(cloudflare_config()).get_status()

    assert result.ok
    assert result.data is not None
    assert result.data.value == "240e::1"


@patch("domain_update.providers.AlidnsClient")
def test_alibaba_update_uses_queried_record_identity(client_type: MagicMock) -> None:
    config = AppConfig(
        provider="alibaba",
        alibaba_cloud_access_key_id="key-id",
        alibaba_cloud_access_key_secret="secret",
        alibaba_cloud_record_id="record-id",
        alibaba_cloud_rr="wrong-config-value",
    )
    provider = AlibabaProvider(config)
    current = DnsStatus(
        provider="alibaba",
        record_name="actual-rr",
        record_type="AAAA",
        value="240e::1",
        record_id="record-id",
    )

    result = provider.set_ipv6("240e::2", current)

    assert result.ok
    request = client_type.return_value.update_domain_record_with_options.call_args.args[0]
    assert request.rr == "actual-rr"
    assert request.type == "AAAA"
