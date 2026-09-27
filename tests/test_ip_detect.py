from unittest.mock import MagicMock, patch

import requests

from domain_update.network import get_public_ipv6


@patch("domain_update.network.requests.Session")
def test_get_public_ipv6_address_disables_environment_proxy(session_type: MagicMock) -> None:
    response = MagicMock()
    response.text = "240e:1234::1\n"
    session = session_type.return_value.__enter__.return_value
    session.get.return_value = response

    result = get_public_ipv6()

    assert result.ok
    assert result.data == "240e:1234::1"
    assert session.trust_env is False
    session.get.assert_called_once_with("https://api6.ipify.org", timeout=(5, 10))
    response.raise_for_status.assert_called_once_with()


@patch("domain_update.network.requests.Session")
def test_get_public_ipv6_address_rejects_non_global_address(session_type: MagicMock) -> None:
    response = MagicMock()
    response.text = "fd00::1"
    session = session_type.return_value.__enter__.return_value
    session.get.return_value = response

    result = get_public_ipv6()

    assert not result.ok
    assert result.data is None


@patch("domain_update.network.requests.Session")
def test_get_public_ipv6_address_handles_request_failure(session_type: MagicMock) -> None:
    session = session_type.return_value.__enter__.return_value
    session.get.side_effect = requests.ConnectionError("IPv6 unavailable")

    result = get_public_ipv6()

    assert not result.ok
    assert result.data is None
