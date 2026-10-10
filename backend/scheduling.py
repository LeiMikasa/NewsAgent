"""One daily job is rebuilt from the persisted subscription on startup."""

from apscheduler.schedulers.background import BackgroundScheduler
from apscheduler.triggers.cron import CronTrigger

from agent.briefs import SHANGHAI

from .service import BackendService

JOB_ID = "daily-ai-brief"


class DailyScheduler:
    def __init__(self, service: BackendService):
        self.service = service
        self.scheduler = BackgroundScheduler(timezone=SHANGHAI,
            job_defaults={"max_instances": 1, "coalesce": True, "misfire_grace_time": 3600})

    def start(self) -> None:
        self.scheduler.start(paused=True)
        try:
            self.apply(self.service.store.subscription())
            self.scheduler.resume()
            self.service.catch_up()
        except Exception:
            self.scheduler.shutdown(wait=True)
            raise

    def apply(self, subscription: dict) -> None:
        if not subscription["enabled"]:
            if self.scheduler.get_job(JOB_ID) is not None:
                self.scheduler.remove_job(JOB_ID)
            return
        hour, minute = map(int, subscription["time"].split(":"))
        self.scheduler.add_job(self.service.run_scheduled,
            CronTrigger(hour=hour, minute=minute, timezone=SHANGHAI), id=JOB_ID, replace_existing=True)

    def next_run_at(self) -> str | None:
        job = self.scheduler.get_job(JOB_ID)
        return job.next_run_time.isoformat() if job is not None and job.next_run_time else None

    def close(self) -> None:
        if self.scheduler.running:
            self.scheduler.shutdown(wait=True)
