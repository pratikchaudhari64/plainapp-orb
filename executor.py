"""PlainWatch executor skeleton.

The executor claims waiting topics and runs Activities under a concurrency
cap. It owns the topic status transition around each Activity.
"""

import asyncio
import logging
import os
from pathlib import Path
from logging.handlers import RotatingFileHandler
from datetime import datetime, timezone

from dotenv import load_dotenv

load_dotenv(Path(__file__).resolve().parent / ".env")

Path(".logs").mkdir(exist_ok=True)

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s %(name)s %(levelname)s %(message)s",
    handlers=[
        RotatingFileHandler(
            ".logs/executor.log",
            maxBytes=5_000_000,
            backupCount=1,
        ),
        logging.StreamHandler(),
    ],
)

import db
from activity import Activity



logger = logging.getLogger(__name__)


MAX_CONCURRENT_ACTIVITIES = int(
    os.getenv("MAX_CONCURRENT_ACTIVITIES", "4")
)
EXECUTOR_INTERVAL_SECONDS = int(
    os.getenv("EXECUTOR_INTERVAL_SECONDS", "3")
)

if MAX_CONCURRENT_ACTIVITIES <= 0:
    raise ValueError("MAX_CONCURRENT_ACTIVITIES must be positive")
if EXECUTOR_INTERVAL_SECONDS <= 0:
    raise ValueError("EXECUTOR_INTERVAL_SECONDS must be positive")


async def run_activity(topic, semaphore, ready_event, approval_event):
    """Wait for a slot and Executor approval before starting an Activity."""
    task = asyncio.current_task()
    task.activity_allowed = False

    if semaphore.locked():
        task.activity_slot_available = False
        ready_event.set()
        return

    await semaphore.acquire()
    task.activity_slot_available = True
    ready_event.set()
    await approval_event.wait()

    if not task.activity_allowed:
        semaphore.release()
        return


    try:
        await Activity(topic).run()
    except Exception as e: #NOTE: Exception not caught within the Activity.run() itself will keep a topic state as IN_PROGRESS - which could block further processing of this topic.
        logger.error("Error occurred while running Activity for topic %s: %s", topic["note_id"], e)
    finally:
        logger.info("Completed Activity for topic %s and released semaphore", topic["note_id"])
        semaphore.release()


async def dispatch_topic(topic, semaphore):
    """Create an Activity task and claim its topic only after admission."""
    ready_event = asyncio.Event()
    approval_event = asyncio.Event()
    task = asyncio.create_task(
        run_activity(topic, semaphore, ready_event, approval_event)
    )
    await ready_event.wait()

    if not task.activity_slot_available:
        logger.warning(f"SLOTS NOT AVAILABLE TO LAUNCH ACTIVITY")
        await task
        return None

    updated = db.execute_query(
        """
        UPDATE topics
        SET status = 'IN_PROGRESS'
        WHERE id = ?
          AND status = 'WAITING_TO_HANDOFF'
        """,
        (topic["id"],),
    )
    task.activity_allowed = updated == 1
    approval_event.set()
    if not task.activity_allowed:
        logger.info(
            "Skipped topic %s: DB update failed; status was no longer WAITING_TO_HANDOFF",
            topic["note_id"],
        )
        return None

    logger.info("Claimed topic %s", topic["note_id"])
    return task


async def run_forever():
    """Continuously dispatch claimed topics."""
    semaphore = asyncio.Semaphore(MAX_CONCURRENT_ACTIVITIES)
    running_tasks = set()

    while True:
        logger.info(f"Initiating Note Dispatch Cycle...")
        # TODO: remove completed tasks and surface their unexpected exceptions.
        running_tasks = {
            task for task in running_tasks if not task.done()
        }
        
        waiting_topics = db.execute_query(
            """
            SELECT id, note_id, to_track_hash, keywords, status, attempts,
                   last_run_at, next_run_at
            FROM topics
            WHERE status = 'WAITING_TO_HANDOFF'
            ORDER BY id
            """
        )
        # move ahead with dispatching only if waiting_topics is not empty?
        if waiting_topics:
            for topic in waiting_topics:
                task = await dispatch_topic(topic, semaphore)
                if task is not None:
                    running_tasks.add(task)
        logger.info(f"Completed 'WAITING-TO-HANDOFF' Note Dispatch Cycle with {len(running_tasks)} running tasks.")

        # Moving eligible IDLE topics to WAITING-TO-HANDOFF
        idle_topics = db.execute_query(
            """
            SELECT id, note_id, to_track_hash, keywords, status, attempts,
                   last_run_at, next_run_at
            FROM topics
            WHERE status = 'IDLE'
            ORDER BY id
            """
        )

        if idle_topics:
            now = datetime.now(timezone.utc)
            for topic in idle_topics:
                try:
                    interval_seconds = int(topic["next_run_at"])
                except (TypeError, ValueError):
                    logger.warning(
                        "Skipping topic %s: invalid next_run_at=%r",
                        topic["note_id"],
                        topic["next_run_at"],
                    )
                    continue

                last_run_at = db.parse_datetime(topic["last_run_at"])
                
                if (
                    last_run_at is not None
                    and (now - last_run_at).total_seconds() < interval_seconds
                ):
                    continue
                
                updated = db.execute_query(
                    """
                    UPDATE topics
                    SET status = 'WAITING_TO_HANDOFF'
                    WHERE id = ?
                      AND status = 'IDLE'
                    """,
                    (topic["id"],),
                )
                if updated == 1:
                    logger.info("Moved idle topic %s to WAITING_TO_HANDOFF", topic["note_id"])

        await asyncio.sleep(EXECUTOR_INTERVAL_SECONDS)


if __name__ == "__main__":
    asyncio.run(run_forever())
