from datetime import datetime, timedelta, timezone
from pathlib import Path

from domain_update.history import (
    CHECK_HISTORY_FILENAME,
    CheckHistoryStore,
    CheckRecord,
    summarize,
)

BASE_TIME = datetime(2026, 9, 27, 12, 0, tzinfo=timezone.utc)


def make_record(
    minutes_ago: int,
    *,
    ok: bool = True,
    action: str = "unchanged",
    source: str = "scheduled",
    message: str = "检查完成",
    ipv6: str = "240e::1",
) -> CheckRecord:
    return CheckRecord(
        timestamp=BASE_TIME - timedelta(minutes=minutes_ago),
        source=source,
        ok=ok,
        action=action,
        message=message,
        ipv6=ipv6,
    )


def test_load_without_history_file_returns_empty_list(tmp_path: Path) -> None:
    result = CheckHistoryStore(tmp_path).load()

    assert result.ok
    assert result.data == []


def test_append_then_load_returns_latest_first(tmp_path: Path) -> None:
    store = CheckHistoryStore(tmp_path)

    store.append(make_record(10, source="scheduled"))
    store.append(make_record(5, source="manual"))

    result = store.load()

    assert result.ok
    assert [record.source for record in result.data] == ["manual", "scheduled"]
    assert result.data[0].ipv6 == "240e::1"
    assert result.data[0].timestamp == BASE_TIME - timedelta(minutes=5)


def test_append_keeps_only_latest_records_beyond_limit(tmp_path: Path) -> None:
    store = CheckHistoryStore(tmp_path, max_records=3)

    for minutes_ago in (5, 4, 3, 2, 1):
        store.append(make_record(minutes_ago, message=f"第{minutes_ago}次"))

    records = CheckHistoryStore(tmp_path, max_records=3).load().data

    assert len(records) == 3
    # 滚动窗口保留最新的三条，最旧的两条被丢弃
    assert [record.message for record in records] == ["第1次", "第2次", "第3次"]


def test_corrupt_lines_are_skipped_instead_of_failing(tmp_path: Path) -> None:
    store = CheckHistoryStore(tmp_path)
    store.append(make_record(5))
    with (tmp_path / CHECK_HISTORY_FILENAME).open("a", encoding="utf-8") as stream:
        stream.write("{不是合法 JSON}\n")
    store.append(make_record(1))

    result = store.load()

    assert result.ok
    assert len(result.data) == 2


def test_clear_removes_all_records(tmp_path: Path) -> None:
    store = CheckHistoryStore(tmp_path)
    store.append(make_record(5))

    cleared = store.clear()

    assert cleared.ok
    assert store.load().data == []


def test_summarize_counts_success_failure_and_changes() -> None:
    records = [
        make_record(40, action="unchanged"),
        make_record(30, action="updated", ipv6="240e::2"),
        make_record(20, ok=False, action="failed"),
        make_record(10, action="created", ipv6="240e::3"),
    ]

    summary = summarize(records)

    assert summary.total == 4
    assert summary.succeeded == 3
    assert summary.failed == 1
    # 只有 created/updated 才算真正改动了记录
    assert summary.changed == 2
    assert summary.last_run_at == BASE_TIME - timedelta(minutes=10)
    assert summary.last_change_at == BASE_TIME - timedelta(minutes=10)


def test_summarize_empty_records_has_no_timestamps() -> None:
    summary = summarize([])

    assert summary.total == 0
    assert summary.succeeded == 0
    assert summary.failed == 0
    assert summary.changed == 0
    assert summary.last_run_at is None
    assert summary.last_change_at is None
