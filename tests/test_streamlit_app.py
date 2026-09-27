from pathlib import Path

from streamlit.testing.v1 import AppTest

from domain_update.config import AppConfig, ConfigStore


def test_streamlit_app_renders_configuration_and_actions(
    tmp_path: Path, monkeypatch
) -> None:
    monkeypatch.setenv("DOMAIN_UPDATE_DATA_DIR", str(tmp_path))

    app_path = Path(__file__).parents[1] / "streamlit_app.py"
    app = AppTest.from_file(app_path, default_timeout=10).run()

    assert not app.exception
    assert app.title[0].value == "IPv6 域名控制台"
    button_labels = {button.label for button in app.button}
    assert {"检测公网 IPv6", "查询 DNS 记录", "检查并更新"} <= button_labels
    assert app.radio[0].value == "cloudflare"

    ConfigStore(tmp_path).save(
        AppConfig(
            provider="alibaba",
            alibaba_cloud_access_key_id="key-id",
            alibaba_cloud_access_key_secret="secret",
            alibaba_cloud_record_id="record-id",
            alibaba_cloud_rr="home",
        )
    )
    app.run()

    assert not app.exception
    assert app.radio[0].value == "alibaba"
