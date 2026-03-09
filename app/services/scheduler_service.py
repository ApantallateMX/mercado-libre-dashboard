"""
app/services/scheduler_service.py

Job scheduler for periodic agent tasks, built on APScheduler's AsyncIOScheduler.

If APScheduler is not installed the module still imports cleanly — all methods
become no-ops and a warning is logged.  Install with:
    pip install apscheduler

Jobs are tracked in the `agent_jobs` SQLite table so the UI can list them.

Usage:
    from app.services.scheduler_service import scheduler_service

    await scheduler_service.start()

    async def my_task():
        ...

    await scheduler_service.add_job(
        job_id="my_job",
        func=my_task,
        trigger="cron",
        agent_name="stock_agent",
        task_description="Scan low-stock items",
        hour=8, minute=0,           # APScheduler cron kwargs
    )
"""

from __future__ import annotations

import json
import logging
from datetime import datetime
from pathlib import Path
from typing import Any, Callable

import aiosqlite

from app.config import DATABASE_PATH

logger = logging.getLogger(__name__)

# ── Optional APScheduler import ──────────────────────────────────────────────
try:
    from apscheduler.schedulers.asyncio import AsyncIOScheduler
    from apscheduler.triggers.cron import CronTrigger
    from apscheduler.triggers.interval import IntervalTrigger
    _APSCHEDULER_AVAILABLE = True
except ImportError:
    AsyncIOScheduler = None  # type: ignore[assignment,misc]
    CronTrigger = None       # type: ignore[assignment,misc]
    IntervalTrigger = None   # type: ignore[assignment,misc]
    _APSCHEDULER_AVAILABLE = False
    logger.warning(
        "APScheduler not installed — SchedulerService will be a no-op. "
        "Install with: pip install apscheduler"
    )


# ─────────────────────────────────────────────────────────────────────────────
# SchedulerService
# ─────────────────────────────────────────────────────────────────────────────

class SchedulerService:
    """
    Thin wrapper around APScheduler's AsyncIOScheduler that also keeps a
    persistent job registry in the `agent_jobs` SQLite table.

    All public methods are safe to call even when APScheduler is not installed;
    they will return empty/False values and log a warning.
    """

    def __init__(self, db_path: str = DATABASE_PATH):
        self._db_path = db_path
        self._scheduler: AsyncIOScheduler | None = (
            AsyncIOScheduler() if _APSCHEDULER_AVAILABLE else None
        )

    # ── Lifecycle ────────────────────────────────────────────────────────────

    async def start(self) -> None:
        """
        Start the underlying scheduler and ensure the `agent_jobs` table exists.
        Safe to call multiple times.
        """
        await self._init_db()
        if not _APSCHEDULER_AVAILABLE:
            logger.warning("SchedulerService.start(): APScheduler not available, skipping")
            return
        if self._scheduler and not self._scheduler.running:
            self._scheduler.start()
            logger.info("SchedulerService started")

    async def stop(self) -> None:
        """Gracefully shut down the scheduler."""
        if self._scheduler and _APSCHEDULER_AVAILABLE and self._scheduler.running:
            self._scheduler.shutdown(wait=False)
            logger.info("SchedulerService stopped")

    @property
    def is_running(self) -> bool:
        """True if the underlying APScheduler instance is active."""
        if not _APSCHEDULER_AVAILABLE or self._scheduler is None:
            return False
        return self._scheduler.running

    # ── Job management ───────────────────────────────────────────────────────

    async def add_job(
        self,
        job_id: str,
        func: Callable,
        trigger: str,
        agent_name: str,
        task_description: str,
        **trigger_kwargs: Any,
    ) -> bool:
        """
        Register a job with APScheduler and persist its metadata to the DB.

        Args:
            job_id:           Unique identifier (used to pause/remove later).
            func:             Async callable to execute on schedule.
            trigger:          APScheduler trigger type: 'cron' or 'interval'.
            agent_name:       Agent that owns this job (for display/filtering).
            task_description: Human-readable description of the task.
            **trigger_kwargs: Passed directly to the APScheduler trigger
                              (e.g. hour=8, minute=0 for cron).

        Returns:
            True on success, False if APScheduler is not available.
        """
        if not _APSCHEDULER_AVAILABLE or self._scheduler is None:
            logger.warning("add_job(%s): APScheduler not available", job_id)
            return False

        # Build the trigger object
        if trigger == "cron":
            apscheduler_trigger = CronTrigger(**trigger_kwargs)
            schedule_str = json.dumps({"type": "cron", **trigger_kwargs})
        elif trigger == "interval":
            apscheduler_trigger = IntervalTrigger(**trigger_kwargs)
            schedule_str = json.dumps({"type": "interval", **trigger_kwargs})
        else:
            logger.error("add_job(%s): unknown trigger type '%s'", job_id, trigger)
            return False

        # Remove existing job with same id if present
        try:
            self._scheduler.remove_job(job_id)
        except Exception:
            pass

        self._scheduler.add_job(
            func,
            trigger=apscheduler_trigger,
            id=job_id,
            replace_existing=True,
            misfire_grace_time=300,
        )

        # Persist to DB
        await self._upsert_job(
            job_id=job_id,
            agent_name=agent_name,
            task=task_description,
            schedule=schedule_str,
            enabled=True,
        )

        logger.info("Job '%s' registered (agent=%s, trigger=%s)", job_id, agent_name, trigger)
        return True

    async def remove_job(self, job_id: str) -> bool:
        """Remove a job from the scheduler and delete its DB record."""
        if not _APSCHEDULER_AVAILABLE or self._scheduler is None:
            return False
        try:
            self._scheduler.remove_job(job_id)
        except Exception as exc:
            logger.debug("remove_job(%s): %s", job_id, exc)

        async with aiosqlite.connect(self._db_path) as db:
            await db.execute("DELETE FROM agent_jobs WHERE job_id = ?", (job_id,))
            await db.commit()
        return True

    async def pause_job(self, job_id: str) -> bool:
        """Pause a job (keeps its registration, stops execution)."""
        if not _APSCHEDULER_AVAILABLE or self._scheduler is None:
            return False
        try:
            self._scheduler.pause_job(job_id)
        except Exception as exc:
            logger.warning("pause_job(%s): %s", job_id, exc)
            return False

        async with aiosqlite.connect(self._db_path) as db:
            await db.execute(
                "UPDATE agent_jobs SET enabled = 0 WHERE job_id = ?", (job_id,)
            )
            await db.commit()
        return True

    async def resume_job(self, job_id: str) -> bool:
        """Resume a previously paused job."""
        if not _APSCHEDULER_AVAILABLE or self._scheduler is None:
            return False
        try:
            self._scheduler.resume_job(job_id)
        except Exception as exc:
            logger.warning("resume_job(%s): %s", job_id, exc)
            return False

        async with aiosqlite.connect(self._db_path) as db:
            await db.execute(
                "UPDATE agent_jobs SET enabled = 1 WHERE job_id = ?", (job_id,)
            )
            await db.commit()
        return True

    async def record_run(
        self, job_id: str, result: str = "ok", error: str | None = None
    ) -> None:
        """
        Update last_run + last_result after a job execution.
        Call this from inside your job function:

            await scheduler_service.record_run("my_job", result="ok")
        """
        last_result = result if not error else f"error: {error}"
        async with aiosqlite.connect(self._db_path) as db:
            await db.execute(
                "UPDATE agent_jobs SET last_run = CURRENT_TIMESTAMP, last_result = ? "
                "WHERE job_id = ?",
                (last_result, job_id),
            )
            await db.commit()

    async def get_jobs(self) -> list[dict]:
        """
        Return all registered jobs from the DB, enriched with live APScheduler
        state (next_run_time, is_paused) when available.
        """
        async with aiosqlite.connect(self._db_path) as db:
            db.row_factory = aiosqlite.Row
            cursor = await db.execute(
                "SELECT * FROM agent_jobs ORDER BY created_at"
            )
            rows = await cursor.fetchall()

        jobs = [dict(r) for r in rows]

        # Enrich with live scheduler data if available
        if _APSCHEDULER_AVAILABLE and self._scheduler and self._scheduler.running:
            live_jobs = {j.id: j for j in self._scheduler.get_jobs()}
            for job in jobs:
                live = live_jobs.get(job["job_id"])
                if live:
                    nrt = live.next_run_time
                    job["next_run_time"] = nrt.isoformat() if nrt else None
                    job["paused"] = not job["enabled"]
                else:
                    job["next_run_time"] = None
                    job["paused"] = True
        else:
            for job in jobs:
                job["next_run_time"] = None
                job["paused"] = not job.get("enabled", False)

        return jobs

    # ── Internal DB helpers ──────────────────────────────────────────────────

    async def _init_db(self) -> None:
        """Create the agent_jobs table if it does not exist."""
        db_path = Path(self._db_path)
        if db_path.parent != Path("."):
            db_path.parent.mkdir(parents=True, exist_ok=True)

        async with aiosqlite.connect(self._db_path) as db:
            await db.execute("""
                CREATE TABLE IF NOT EXISTS agent_jobs (
                    id          INTEGER PRIMARY KEY AUTOINCREMENT,
                    job_id      TEXT    NOT NULL UNIQUE,
                    agent_name  TEXT    NOT NULL DEFAULT '',
                    task        TEXT    NOT NULL DEFAULT '',
                    schedule    TEXT    NOT NULL DEFAULT '{}',
                    enabled     INTEGER NOT NULL DEFAULT 1,
                    last_run    TIMESTAMP,
                    last_result TEXT    DEFAULT '',
                    created_at  TIMESTAMP DEFAULT CURRENT_TIMESTAMP
                )
            """)
            await db.commit()

    async def _upsert_job(
        self,
        job_id: str,
        agent_name: str,
        task: str,
        schedule: str,
        enabled: bool,
    ) -> None:
        async with aiosqlite.connect(self._db_path) as db:
            await db.execute("""
                INSERT INTO agent_jobs (job_id, agent_name, task, schedule, enabled)
                VALUES (?, ?, ?, ?, ?)
                ON CONFLICT (job_id) DO UPDATE SET
                    agent_name = excluded.agent_name,
                    task       = excluded.task,
                    schedule   = excluded.schedule,
                    enabled    = excluded.enabled
            """, (job_id, agent_name, task, schedule, 1 if enabled else 0))
            await db.commit()


# ── Module-level singleton ───────────────────────────────────────────────────
scheduler_service = SchedulerService()
