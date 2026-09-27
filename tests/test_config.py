from pathlib import Path

from domain_update.config import AppConfig, ConfigStore


def test_config_store_creates_default_file_on_first_run(
    tmp_path: Path, monkeypatch
) -> None:
    # 配置只认配置文件：环境变量一律不再参与，否则同一个字段会有两个来源。
    monkeypatch.setenv("DOMAIN_UPDATE_PROVIDER", "alibaba")
    monkeypatch.setenv("ALIBABA_CLOUD_RECORDID", "record-id")

    result = ConfigStore(tmp_path).load()

    assert result.ok
    assert result.data is not None
    assert result.data == AppConfig()
    # 首次启动就落一份默认配置，页面才能直接编辑并保存。
    assert (tmp_path / "config.json").exists()


def test_alibaba_config_no_longer_requires_rr() -> None:
    # 主机记录 RR 已删除：阿里云写入路径只认 Record ID + 查询结果，
    # 配置里不再保存这一字段，校验也不该再要求它。
    config = AppConfig(
        provider="alibaba",
        alibaba_cloud_access_key_id="key-id",
        alibaba_cloud_access_key_secret="secret",
        alibaba_cloud_record_id="record-id",
    )

    assert config.validate().ok
    assert not hasattr(config, "alibaba_cloud_rr")


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
