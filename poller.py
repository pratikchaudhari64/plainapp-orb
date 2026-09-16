"""PlainWatch poller skeleton.

The poller discovers tracking notes, refreshes the local feed cache, and
queues topics that are ready for an Activity run.
"""

import time
import logging
import hashlib
from logging.handlers import RotatingFileHandler
from datetime import datetime, timezone
from pathlib import Path

Path(".logs").mkdir(exist_ok=True)

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s %(name)s %(levelname)s %(message)s",
    handlers=[
        RotatingFileHandler(
            ".logs/poller.log",
            maxBytes=5_000_000,
            backupCount=1,
        ),
        logging.StreamHandler(),
    ],
)

import plainapp_api
import db

logger = logging.getLogger(__name__)
plainapp = plainapp_api.PlainAppAPI()


POLL_INTERVAL_SECONDS = 5 # make it 15 * 60 later
AUTO_GENERATED_MARKER = "---AUTO-GENERATED BELOW, DO NOT EDIT---"
TRACKING_TAG = "tracking_stuff"


def refresh_feed_items():
    """Refresh feed data from PlainApp and upsert it into feed_items."""
    # TODO: call PlainApp feeds.sync(id=None).
    sync_response = plainapp.sync_feeds(feed_id=None)
    logger.info("Feed synchronization response: %s", sync_response)
    
    # TODO: paginate feeds.entries(query="") and upsert by PlainApp item id.
    pass


def fetch_tracking_notes():
    """Return active notes tagged tracking_stuff with a to_track line."""
    all_nonTrashed_notes = plainapp.list_notes()
    
    tracking_notes = [
        note for note in all_nonTrashed_notes
        if any(tag["name"] == TRACKING_TAG for tag in note.get("tags", []))
    ]
    
    return tracking_notes


def hash_seed(content):
    """Hash only the user-controlled portion of a note body."""
    seed_content = content.split(AUTO_GENERATED_MARKER, 1)[0].strip()
    logger.info("Seed content for hashing:\n%s", seed_content)
    return hashlib.sha256(seed_content.encode("utf-8")).hexdigest()


def poll():
    """Reconcile active tracking notes with the local topics table."""
    # 1. Fetch the active tracking notes from PlainApp.
    # refresh_feed_items()
    tracking_notes = fetch_tracking_notes()
    active_note_ids = {note["id"] for note in tracking_notes}

    # 2. Load stored topic IDs and remove topics no longer active.
    stored_topics = db.execute_query("SELECT note_id FROM topics")
    stored_note_ids = {row["note_id"] for row in stored_topics}

    removed_note_ids = stored_note_ids - active_note_ids
    for note_id in removed_note_ids:
        db.execute_query(
            "DELETE FROM topics WHERE note_id = ?",
            (note_id,),
        )
        logger.info("Removed missing or trashed topic: %s", note_id)

    # 3. Add newly discovered notes to the topics table.
    new_note_ids = active_note_ids - stored_note_ids
    for note in tracking_notes:
        if note["id"] not in new_note_ids:
            continue

        db.execute_query(
            """
            INSERT INTO topics (note_id, to_track_hash, status)
            VALUES (?, ?, 'WAITING_TO_HANDOFF')
            """,
            (note["id"], hash_seed(note.get("content", ""))),
        )
        logger.info(
            "Added topic %s with status WAITING_TO_HANDOFF",
            note["id"],
        )

    # TODO 4. Detect changed to_track_hash values and queue topics for reprocessing.

    # 5. Count topics currently ready for handoff.
    idle_topics = db.execute_query(
        "SELECT note_id FROM topics WHERE status = ?",
        ("IDLE",),
    )
    logger.info("Found %s IDLE topics", len(idle_topics))

    return {
        "success": True,
        "removed": len(removed_note_ids),
        "added": len(new_note_ids),
        "idle": len(idle_topics),
    }
    # complete_finished_cycles(datetime.now(timezone.utc))


def run_forever():
    """Run the poller on its fixed cadence."""
    runs = 0
    while True:
        try:
            logger.info("Starting poll iteration #%s", runs + 1)
            poll_resp = poll()
            runs += 1
            logger.info("Completed poll iteration #%s with response: %s", runs, poll_resp)
            logger.info(f"program retrying in next: {POLL_INTERVAL_SECONDS} seconds...")
        except Exception as error:
            logger.error(
                "Poll iteration failed. Error: %s",
                error,
            )
            logger.info(f"program retrying in next: {POLL_INTERVAL_SECONDS} seconds...")
        time.sleep(POLL_INTERVAL_SECONDS)


if __name__ == "__main__":
    logger.info("Starting Poller")
    run_forever()
