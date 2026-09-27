from datetime import datetime, timezone
from pathlib import Path

from streamlit.testing.v1 import AppTest

from domain_update.config import AppConfig, ConfigStore
from domain_update.history import CheckHistoryStore, CheckRecord


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


def test_streamlit_app_renders_check_history_report(
    tmp_path: Path, monkeypatch
) -> None:
    monkeypatch.setenv("DOMAIN_UPDATE_DATA_DIR", str(tmp_path))
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

    app_path = Path(__file__).parents[1] / "streamlit_app.py"
    app = AppTest.from_file(app_path, default_timeout=10).run()

    assert not app.exception
    markdown_text = "\n".join(block.value for block in app.markdown)
    assert "检查记录报告" in markdown_text
    metrics = {metric.label: metric.value for metric in app.metric}
    assert metrics["检查次数"] == "2"
    assert metrics["失败"] == "1"
    assert metrics["地址变更"] == "1"
    # 页面按本地时区展示时间，断言与实现同一套换算，避免测试依赖运行机器的时区。
    expected_change_time = datetime(2026, 9, 27, 12, 0, tzinfo=timezone.utc).astimezone()
    assert metrics["最近变更"] == expected_change_time.strftime("%Y-%m-%d %H:%M:%S")
