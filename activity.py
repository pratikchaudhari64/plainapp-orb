"""PlainWatch Activity skeleton.

An Activity processes one topic for one digest cycle. It does not own topic
status transitions; the Executor reports success or failure around it.
"""

import asyncio
import json
import logging
from dataclasses import dataclass
from datetime import datetime, timezone
from typing import Any
logger = logging.getLogger(__name__)

AUTO_GENERATED_MARKER = "---AUTO-GENERATED BELOW, DO NOT EDIT---"

import use_llm
import plainapp_api
import db
from harness import Harness


# @dataclass
class Activity:
    """One-shot processing unit for a single topic."""

    topic: Any

    def __init__(self, topic: Any):
        self.topic = topic
        self.plainapp_api = plainapp_api.PlainAppAPI()
        self.use_llm = use_llm.UseLLM()
        self.activity_log_id = self._start_activity()
        self.harness = Harness(self.plainapp_api, self.use_llm)

    def _start_activity(self):
        '''
        Start a new activity log entry for the current topic.

        Returns:
            int: The ID of the newly created activity log entry.
        '''
        logger.info(f"Starting activity for topic {self.topic['id']}")

        started_at = datetime.now(timezone.utc).strftime(
            "%Y-%m-%d %H:%M:%S"
        )
        try:
            activity_log_id = db.execute_insert(
                """
                INSERT INTO activity_notification_log
                    (topic_id, note_id, status, message, started_at)
                VALUES (?, ?, 'WORKING', ?, ?)
                """,
                (
                    self.topic["id"],
                    self.topic["note_id"],
                    "Activity started.",
                    started_at,
                ),
            )
        except Exception:
            logger.exception(
                "Could not create activity log for topic %s",
                self.topic["id"],
            )
            raise

        logger.info(
            "CREATED activity with log ID %s for topic %s",
            activity_log_id,
            self.topic["id"],
        )
        return activity_log_id

    async def run(self):
        """Run the feed filtering, LLM processing, and note write-back flow."""
        topic_id = self.topic["id"]
        note_id = self.topic["note_id"]
        last_run_at = db.parse_datetime(self.topic["last_run_at"])
        try:
            
            harness_response = await self.harness.execute(note_id=note_id)

            finished_at = datetime.now(timezone.utc).strftime(
                "%Y-%m-%d %H:%M:%S"
            )
            db.execute_query(
                """
                UPDATE activity_notification_log
                SET status = 'COMPLETED', message = ?, extra = ?, finished_at = ?
                WHERE id = ?
                """,
                (
                    "Activity completed successfully.",
                    json.dumps(harness_response),
                    finished_at,
                    self.activity_log_id,
                ),
            )
        except Exception as e:
            logger.exception("Error running activity for topic %s", note_id)
            finished_at = datetime.now(timezone.utc).strftime(
                "%Y-%m-%d %H:%M:%S"
            )
            db.execute_query(
                """
                UPDATE activity_notification_log
                SET status = 'FAILED', message = ?, finished_at = ?
                WHERE id = ?
                """,
                (str(e), finished_at, self.activity_log_id),
            )

        finally:
            updated = db.execute_query(
                """
                UPDATE topics
                                SET status = 'IDLE', last_run_at = ?
                WHERE id = ?
                  AND status = 'IN_PROGRESS'
                """,
                                (
                                        datetime.now(timezone.utc).strftime("%Y-%m-%d %H:%M:%S"),
                                        topic_id,
                                ),
            )
            logger.info(
                "Finished running activity for topic %s; status reset=%s",
                note_id,
                updated == 1,
            )

    async def test(self, topic):
        """Test the Activity with a mock run."""

        logger.info(f"Testing activity with topic: {topic}")
        await asyncio.sleep(6)
        
        pass


    def _get_relevant_feed_items(self, keywords):
        """Retrieve feed items relevant to the given keywords."""
        # TODO: query feed items based on keywords
        pass

    def _generate_new_update(self, feed_items, existing_user_content):
        """Generate the new update section for the note."""
        # TODO: summarize feed items and combine with existing user content
        pass
    
    def write_digest_to_note(self, digest):
        """Replace generated note content while preserving the user section."""
        # TODO: re-fetch the note before writing to avoid overwriting a changed seed.
        # TODO: split at AUTO_GENERATED_MARKER, append it if absent, and replace below.
        # TODO: save the existing note id with its title and updated content.
        # TODO: use a fresh SQLite connection only before/after the API call.
        pass
