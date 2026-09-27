"""进程内单例调度器，供启动器与 Streamlit 会话共享。"""

from __future__ import annotations

import logging
import threading
from dataclasses import dataclass
from datetime import datetime

from apscheduler.schedulers.background import BackgroundScheduler

from domain_update.config import AppConfig
from domain_update.models import Result
from domain_update.service import DomainUpdateService


JOB_ID = "dns-ipv6-check"

logger = logging.getLogger(__name__)


@dataclass(frozen=True)
class SchedulerSnapshot:
    enabled: bool
    running: bool
    interval_minutes: int
    next_run: datetime | None
    last_run: datetime | None
    last_ok: bool | None
    last_message: str


class UpdateScheduler:
    def __init__(self) -> None:
        self._scheduler = BackgroundScheduler(daemon=True)
        self._lock = threading.RLock()
        self._signature: tuple[bool, int] | None = None
        self._enabled = False
        self._interval_minutes = 10
        self._last_run: datetime | None = None
        self._last_ok: bool | None = None
        self._last_message = "尚未执行"

    def configure(self, config: AppConfig) -> Result[SchedulerSnapshot]:
        """按持久化配置幂等调整任务，避免 Streamlit 重跑不断重置下次时间。"""
        signature = (config.schedule_enabled, config.check_interval_minutes)
        with self._lock:
            if signature == self._signature:
                return Result.success("定时检查配置未变化", self.snapshot())

            self._enabled = config.schedule_enabled
            self._interval_minutes = config.check_interval_minutes
            if not config.schedule_enabled:
                if self._scheduler.get_job(JOB_ID) is not None:
                    self._scheduler.remove_job(JOB_ID)
                self._signature = signature
                return Result.success("定时检查已关闭", self.snapshot())

            if not self._scheduler.running:
                self._scheduler.start()
            self._scheduler.add_job(
                self._run_check,
                trigger="interval",
                minutes=config.check_interval_minutes,
                id=JOB_ID,
                replace_existing=True,
                max_instances=1,
                coalesce=True,
            )
            self._signature = signature
            return Result.success("定时检查已启用", self.snapshot())

    def _run_check(self) -> None:
        # 标记来源，页面的检查记录报告才能区分定时与手动执行。
        result = DomainUpdateService().check_and_update(source="scheduled")
        with self._lock:
            self._last_run = datetime.now().astimezone()
            self._last_ok = result.ok
            self._last_message = result.message
        logger.info("定时检查执行完毕：成功=%s 消息=%s", result.ok, result.message)

    def snapshot(self) -> SchedulerSnapshot:
        with self._lock:
            job = self._scheduler.get_job(JOB_ID)
            return SchedulerSnapshot(
                enabled=self._enabled,
                running=self._scheduler.running and job is not None,
                interval_minutes=self._interval_minutes,
                next_run=job.next_run_time if job is not None else None,
                last_run=self._last_run,
                last_ok=self._last_ok,
                last_message=self._last_message,
            )


_INSTANCE: UpdateScheduler | None = None
_INSTANCE_LOCK = threading.Lock()


def get_scheduler() -> UpdateScheduler:
    global _INSTANCE
    with _INSTANCE_LOCK:
        if _INSTANCE is None:
            # 同一进程只能存在一个定时器，否则多个浏览器会话会重复更新 DNS。
            _INSTANCE = UpdateScheduler()
        return _INSTANCE
