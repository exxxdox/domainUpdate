from pathlib import Path

from domain_update.config import AppConfig, ConfigStore


def test_config_store_initializes_from_environment(
    tmp_path: Path, monkeypatch
) -> None:
    monkeypatch.setenv("DOMAIN_UPDATE_PROVIDER", "alibaba")
    monkeypatch.setenv("ALIBABA_CLOUD_ACCESS_KEY_ID", "key-id")
    monkeypatch.setenv("ALIBABA_CLOUD_ACCESS_KEY_SECRET", "secret")
    monkeypatch.setenv("ALIBABA_CLOUD_RECORDID", "record-id")
    monkeypatch.setenv("ALIBABA_CLOUD_RR", "home")

    result = ConfigStore(tmp_path).load()

    assert result.ok
    assert result.data is not None
    assert result.data.provider == "alibaba"
    assert result.data.alibaba_cloud_record_id == "record-id"
    assert (tmp_path / "config.json").exists()


def test_config_store_round_trip(tmp_path: Path) -> None:
    store = ConfigStore(tmp_path)
    config = AppConfig(
        provider="cloudflare",
        schedule_enabled=True,
        check_interval_minutes=15,
        cloudfare_token="token",
        cloudfare_zone_id="zone",
        cloudfare_record_name="home.example.com",
    )

    saved = store.save(config)
    loaded = store.load()

    assert saved.ok
    assert loaded.ok
    assert loaded.data == config
