"""检查执行历史：滚动保留最近若干条，供页面生成检查记录报告。"""

from __future__ import annotations

import json
import os
import tempfile
import threading
from dataclasses import asdict, dataclass
from datetime import datetime
from pathlib import Path
from typing import Any, Mapping, Sequence

from domain_update.models import Result


CHECK_HISTORY_FILENAME = "check_history.jsonl"
DEFAULT_MAX_RECORDS = 500
# 只有真正写入过 DNS 的动作才算“地址变更”，无变化不计入。
_CHANGED_ACTIONS = frozenset({"created", "updated"})
# 定时线程与页面会话可能同时读写，所有实例共用一把锁。
_HISTORY_LOCK = threading.RLock()


@dataclass(frozen=True)
class CheckRecord:
    """一次检查的落地记录；字段保持扁平，便于直接渲染成表格。"""

    timestamp: datetime
    source: str
    ok: bool
    action: str
    message: str
    ipv6: str = ""
    previous_value: str | None = None

    def to_dict(self) -> dict[str, Any]:
        payload = asdict(self)
        # 时间统一用带时区的 ISO 8601，跨时区读取时不会丢失偏移。
        payload["timestamp"] = self.timestamp.isoformat()
        return payload

    @classmethod
    def from_dict(cls, value: Mapping[str, Any]) -> "CheckRecord":
        timestamp = value.get("timestamp")
        if not isinstance(timestamp, str):
            raise ValueError("检查记录缺少时间戳")
        previous_value = value.get("previous_value")
        return cls(
            timestamp=datetime.fromisoformat(timestamp),
            source=str(value.get("source", "manual")),
            ok=bool(value.get("ok", False)),
            action=str(value.get("action", "")),
            message=str(value.get("message", "")),
            ipv6=str(value.get("ipv6") or ""),
            # 空值统一成 None，表格里才能显示成“无记录”而不是空字符串。
            previous_value=str(previous_value) if previous_value else None,
        )


@dataclass(frozen=True)
class CheckSummary:
    total: int
    succeeded: int
    failed: int
    changed: int
    last_run_at: datetime | None
    last_change_at: datetime | None


def summarize(records: Sequence[CheckRecord]) -> CheckSummary:
    """按记录内容取最近时间，而不是依赖传入顺序，避免调用方顺序不同就算错。"""
    succeeded = sum(1 for record in records if record.ok)
    changes = [
        record for record in records if record.ok and record.action in _CHANGED_ACTIONS
    ]
    return CheckSummary(
        total=len(records),
        succeeded=succeeded,
        failed=len(records) - succeeded,
        changed=len(changes),
        last_run_at=max((record.timestamp for record in records), default=None),
        last_change_at=max((record.timestamp for record in changes), default=None),
    )


class CheckHistoryStore:
    """JSONL 追加写 + 超限滚动截断，写入失败只返回 Result，不抛给调用方。"""

    def __init__(
        self,
        data_dir: Path | None = None,
        max_records: int = DEFAULT_MAX_RECORDS,
    ) -> None:
        configured_dir = os.environ.get("DOMAIN_UPDATE_DATA_DIR", ".data")
        self.data_dir = data_dir or Path(configured_dir)
        self.path = self.data_dir / CHECK_HISTORY_FILENAME
        self.max_records = max(1, max_records)
        self._lock = _HISTORY_LOCK

    def append(self, record: CheckRecord) -> Result[CheckRecord]:
        with self._lock:
            try:
                self.data_dir.mkdir(parents=True, exist_ok=True)
                with self.path.open("a", encoding="utf-8") as stream:
                    json.dump(record.to_dict(), stream, ensure_ascii=False)
                    stream.write("\n")
                    stream.flush()
                    os.fsync(stream.fileno())
            except OSError as error:
                return Result.failure(f"检查记录写入失败（{type(error).__name__}）")
            self._trim()
            return Result.success("检查记录已写入", record)

    def load(self, limit: int | None = None) -> Result[list[CheckRecord]]:
        with self._lock:
            try:
                records = self._read_all()
            except OSError as error:
                return Result.failure(f"检查记录读取失败（{type(error).__name__}）")
        if limit is not None:
            records = records[: max(0, limit)]
        return Result.success("检查记录读取成功", records)

    def clear(self) -> Result[int]:
        with self._lock:
            try:
                removed = len(self._read_all())
                self.path.unlink(missing_ok=True)
            except OSError as error:
                return Result.failure(f"检查记录清理失败（{type(error).__name__}）")
        return Result.success("检查记录已清空", removed)

    def _read_all(self) -> list[CheckRecord]:
        """按时间倒序返回；单行损坏只跳过该行，避免一条坏记录拖垮整份报告。"""
        if not self.path.exists():
            return []
        records: list[CheckRecord] = []
        with self.path.open("r", encoding="utf-8") as stream:
            for line in stream:
                line = line.strip()
                if not line:
                    continue
                try:
                    records.append(CheckRecord.from_dict(json.loads(line)))
                except (json.JSONDecodeError, ValueError, TypeError):
                    continue
        records.sort(key=lambda item: item.timestamp, reverse=True)
        return records

    def _trim(self) -> None:
        """超出上限时整体重写为最新若干条，用临时文件替换避免半写入。"""
        try:
            records = self._read_all()
        except OSError:
            return
        if len(records) <= self.max_records:
            return
        temporary_path: Path | None = None
        try:
            descriptor, name = tempfile.mkstemp(
                prefix="history-", suffix=".tmp", dir=self.data_dir
            )
            temporary_path = Path(name)
            with os.fdopen(descriptor, "w", encoding="utf-8") as stream:
                for record in records[: self.max_records]:
                    json.dump(record.to_dict(), stream, ensure_ascii=False)
                    stream.write("\n")
                stream.flush()
                os.fsync(stream.fileno())
            os.replace(temporary_path, self.path)
            temporary_path = None
        except OSError:
            # 截断失败不影响刚写入的记录，下次 append 还会再尝试。
            return
        finally:
            if temporary_path is not None:
                try:
                    temporary_path.unlink(missing_ok=True)
                except OSError:
                    pass
