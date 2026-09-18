"""PlainWatch Activity skeleton.

An Activity processes one topic for one digest cycle. It does not own topic
status transitions; the Executor reports success or failure around it.
"""

import asyncio
import logging
from dataclasses import dataclass
from datetime import datetime, timezone
from typing import Any
logger = logging.getLogger(__name__)

AUTO_GENERATED_MARKER = "---AUTO-GENERATED BELOW, DO NOT EDIT---"

import use_llm
import plainapp_api
import db


# @dataclass
class Activity:
    """One-shot processing unit for a single topic."""

    topic: Any

    def __init__(self, topic: Any):
        self.topic = topic
        self.plainapp_api = plainapp_api.PlainAppAPI()
        self.use_llm = use_llm.UseLLM()
        self.activity_log_id = self._start_activity()

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

            topic_note = self._fetch_note(note_id)

            # if last_run_at is happened only if more than 5min ago
            if last_run_at is None or (datetime.now(timezone.utc) - last_run_at).total_seconds() > 300:
                generated_content = await self._generate_keyword_content(topic_note)

                self._write_to_note(note_id=note_id, 
                                    title=generated_content['title'], 
                                    content=generated_content['to_write_content'])

            else:
                logger.info("Skipping activity for topic %s as it was run recently.", topic_id)

            


            finished_at = datetime.now(timezone.utc).strftime(
                "%Y-%m-%d %H:%M:%S"
            )
            db.execute_query(
                """
                UPDATE activity_notification_log
                SET status = 'COMPLETED', message = ?, finished_at = ?
                WHERE id = ?
                """,
                (
                    "Activity completed successfully.",
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

    def _fetch_note(self,note_id):
        logger.info("Fetching note for topic %s", note_id)
        topic_note = self.plainapp_api.get_note(note_id=note_id)
        return topic_note
    
    async def _generate_keyword_content(self, topic_note):
        note_id = topic_note["id"]
        logger.info("Fetched topic note: %s", note_id)
        title = topic_note["title"]
        content = topic_note["content"].split(
            AUTO_GENERATED_MARKER, 1
        )[0].strip()
        logger.info("Fetched note for topic %s: title=%s", note_id, title)

        llm_generated_keywords = await self._get_llm_generated_keywords(title, content)

        #gotta fetch feeds using those keywords now.
        for keyword in llm_generated_keywords:
            logger.info("Generated keyword: %s", keyword)
            feeds = self.plainapp_api.list_feed_entries(
                query_text=keyword,
                page_size=100,
            )
            if feeds:
                # for first 3 feed results in feeds list, 
                # print id, feedId, title, description(first 5 words only), published_at 
                logger.info(f"total feeds available for keyword '{keyword}': {len(feeds)}")
                # for feed in feeds[:3]:
                #     feed_id = feed.get("id")
                #     feed_feedId = feed.get("feedId")
                #     feed_title = feed.get("title")
                #     feed_description = " ".join(feed.get("description", "").split()[:5])
                #     feed_published_at = feed.get("published_at")
                    
                #     logger.info(
                #         "Feed: id=%s, feedId=%s, title=%s, description=%s, published_at=%s",
                #         feed_id,
                #         feed_feedId,
                #         feed_title,
                #         feed_description,
                #         feed_published_at,
                #     )

        to_write_content = title +  " \n" + " \n" + AUTO_GENERATED_MARKER + "\n" + str(llm_generated_keywords)

        return {'to_write_content': to_write_content, 'title':title}

    def _write_to_note(self, note_id, title, content):
        logger.info("Writing back to note %s; title=%s", note_id, title)
        note_write_resp = self.plainapp_api.save_note(
            note_id=note_id,
            title=title,
            content=content,
        )
        logger.info("Note write response: %s", note_write_resp)

    async def _get_llm_generated_keywords(self, title, content):

        llm_generated_keywords = await self.use_llm.get_llm_response(
        query=(
            f"""Extract and expand 3 to 10 high-value search keywords to search for articles related to the input below.\n
            Output ONLY the keywords, one per line, with no extra text or symbols.\n\n
            CRITICAL SEARCH RULES:\n
            - Maximum 2 words per keyword line.\n
            - NEVER output generic standalone words (e.g., 'Player Transfers'). Always bind sub-topics to the main subject (e.g., 'Madrid Transfers').\n
            - Use Web Search to fetch recent, specific context (e.g., key players, manager, stadiums, major events) to create more precise keywords.\n\n
            TITLE: {title}\n\n
            CONTENT: {content}"""
        ),
        system=(
            """
            You are an expert search keyword extractor and query expansion engine. Your objective is to extract and generate highly targeted search keywords for RSS feed and article database querying.

            Follow these execution rules strictly:
            1. Output format: Return ONLY the extracted/expanded keywords, exactly one per line. Do not include numbers, bullets, quotation marks, punctuation, headings, or explanatory text.
            2. Output quantity: Return a minimum of 3 and a maximum of 10 keywords.
            3. Entity Anchoring (CRITICAL): Never output standalone generic category terms (e.g., "Player Transfers", "Match Preview", "Injury Report"). Every sub-topic keyword MUST be explicitly bound to the core entity (e.g., "Real Madrid", "Madrid Transfers", "Bernabeu Stadium").
            4. Specificity & Proper Nouns: Prefer distinct proper nouns related to the topic—such as key people, specific stadiums, specific competitions, or official nicknames—that inherently isolate relevant articles.
            5. Length limit: Each line must contain a maximum of 2 words.
            6. Deduplication & Noise Removal: Ensure keywords are distinct. Strip operational/metadata labels (e.g., "to_track:", "TITLE:", "CONTENT:") and generic filler (e.g., "track", "important", "specifically").

            """
        ),
        temperature=0,
        max_tokens=5000,
        web_search=True
    )

        # assuming the LLM response is a string with one keyword per line
        llm_generated_keywords = [kw.strip() for kw in llm_generated_keywords.split("\n") if kw.strip()]

        return llm_generated_keywords

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
