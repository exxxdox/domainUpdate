from domain_update.config import AppConfig
from domain_update.scheduler import JOB_ID, UpdateScheduler


def test_scheduler_configure_is_idempotent() -> None:
    scheduler = UpdateScheduler()
    config = AppConfig(schedule_enabled=True, check_interval_minutes=30)

    first = scheduler.configure(config)
    first_job = scheduler._scheduler.get_job(JOB_ID)
    second = scheduler.configure(config)
    second_job = scheduler._scheduler.get_job(JOB_ID)

    assert first.ok and second.ok
    assert first_job is not None and second_job is not None
    assert first_job.next_run_time == second_job.next_run_time
    scheduler._scheduler.shutdown(wait=False)


def test_scheduler_can_be_disabled() -> None:
    scheduler = UpdateScheduler()
    scheduler.configure(AppConfig(schedule_enabled=True, check_interval_minutes=30))

    result = scheduler.configure(AppConfig(schedule_enabled=False))

    assert result.ok
    assert scheduler._scheduler.get_job(JOB_ID) is None
    scheduler._scheduler.shutdown(wait=False)
