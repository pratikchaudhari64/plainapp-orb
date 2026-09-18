"""AI orchestration and note-section write-back for PlainWatch."""

from __future__ import annotations

import os
import logging
import re
from pathlib import Path
from typing import Any

from dotenv import load_dotenv

load_dotenv(Path(__file__).resolve().parent / ".env")

KEYWORDS_MARKER = os.getenv("KEYWORDS_MARKER")
TIMELINE_MARKER = os.getenv("TIMELINE_MARKER")

KEYWORD_ACTION_COOLDOWN = int(os.getenv("KEYWORD_ACTION_COOLDOWN", "86400"))
TIMELINE_ACTION_COOLDOWN = int(os.getenv("TIMELINE_ACTION_COOLDOWN", "86400"))

if not KEYWORDS_MARKER or not TIMELINE_MARKER:
    raise ValueError("KEYWORDS_MARKER and TIMELINE_MARKER must be set")

# read these markers from the environment and define block delimiters for the timeline section

TIMELINE_BLOCK_START = os.getenv("TIMELINE_BLOCK_START")
TIMELINE_BLOCK_END = os.getenv("TIMELINE_BLOCK_END")

if not TIMELINE_BLOCK_START or not TIMELINE_BLOCK_END:
    raise ValueError(
        "TIMELINE_BLOCK_START and TIMELINE_BLOCK_END must be set"
    )


logger = logging.getLogger(__name__)

import db
from datetime import datetime, timezone, timedelta


class Harness:
    """Coordinate AI work and isolated note-section updates."""

    def __init__(self, plainapp_api: Any, use_llm: Any):
        logger.info("Harness initialized...")
        self.plainapp_api = plainapp_api
        self.use_llm = use_llm

    async def execute(self, note_id: str):

        ref_keyword_resp = await self.run_keyword_refresh(note_id=note_id)
        # ref_keyword_resp={}
        # ref_keyword_resp['actions_run'] = ''
        run_timeline_resp = await self.run_timeline_update(note_id=note_id, feed_items=[])

        harness_message = {'actions_run': [ref_keyword_resp['actions_run'], run_timeline_resp['actions_run']]}

        return harness_message

    async def run_keyword_refresh(self, note_id: str) -> dict[str, Any]:
        """Refresh the keyword section without changing the timeline section."""
        logger.info("Executing keyword refresh for note %s", note_id)
        # Check if the keyword refresh action is eligible based on the cooldown period
        check = self._check_kw_eligbility(note_id, action_run_kw='keyword_refresh', cooldown=KEYWORD_ACTION_COOLDOWN)
        if not check:
            logger.info(f"Keyword refresh not eligible for note {note_id}")
            message = {'actions_run': ''}

            return message
        logger.info(f"Keyword refresh eligible for note {note_id}")
        
        note = self._fetch_note(note_id)
        sections = self._parse_note(note)
        title = note["title"]

        logger.info("Refreshing keywords for note %s", note_id)
        new_keywords = await self._get_llm_generated_keywords(
            title,
            sections["seed"],
        )

        for keyword in new_keywords:
            logger.info("Generated keyword: %s", keyword)
            feeds = self.plainapp_api.list_feed_entries(
                query_text=keyword,
                page_size=100,
            )
            if feeds:
                logger.info(
                    "Total feeds available for keyword %r: %d",
                    keyword,
                    len(feeds),
                )

        sections["keywords"] = ", ".join(
            f"`{keyword.strip()}`" for keyword in new_keywords
        )
        self._write_note(note, self._reassemble_note(sections))
        logger.info("Keyword refresh completed for note %s", note_id)

        message = {'actions_run': 'keyword_refresh'}
        return message

    async def run_timeline_update(
        self,
        note_id: str,
        feed_items: list[dict[str, Any]],
    ) -> dict[str, Any]:
        """Refresh the timeline section without changing the keyword section."""

        logger.info("Executing timeline update for note %s", note_id)
        check = self._check_kw_eligbility(note_id, action_run_kw='timeline_update', cooldown=TIMELINE_ACTION_COOLDOWN)
        if not check:
            logger.info(f"Timeline update not eligible for note {note_id}")
            message = {'actions_run': ''}

            return message
        logger.info(f"Timeline update eligible for note {note_id}")
        
        note = self._fetch_note(note_id)
        sections = self._parse_note(note)
        title = note["title"]

        keywords = [
            keyword.strip().strip("`")
            for keyword in sections["keywords"].split(",")
            if keyword.strip()
        ]


        kw_feeds = {}
        for keyword in keywords:
            logger.info("Generated keyword: %s", keyword)
            feeds = self.plainapp_api.list_feed_entries(
                query_text=keyword,
                page_size=100,
            )
            if feeds:

                # apply a filter to only include feeds with published_at of yesterday's date
                filtered_feeds = [
                    feed for feed in feeds
                    if "publishedAt" in feed and feed["publishedAt"].startswith((datetime.now(timezone.utc) - timedelta(days=1)).strftime("%Y-%m-%d"))
                ]

                #syncing content and replacing the current value of it
                for ffeed in filtered_feeds:
                    resp = self.plainapp_api.fetch_feed_content(feed_entry_id=ffeed["id"])
                    ffeed["content"] = resp.get("content", "")

                kw_feeds[keyword] = filtered_feeds

                logger.info(
                    "Total feeds available for keyword %r: %d",
                    keyword,
                    len(feeds),
                )

        timeline = await self._generate_timeline(kw_feeds, current_section=sections)


        self._write_note(note, self._reassemble_note(timeline['new_sections']))
        message = {'actions_run': 'timeline_update'}
        return message

    def _check_kw_eligbility(self, note_id: str, action_run_kw: str, cooldown: int) -> bool:
        """Check if the note is eligible for the specified action (keyword refresh or timeline update)."""
        harness_action_run_for_note = self._fetch_note_activity(note_id, action_run_kw)
        logger.info("Last activity run date for note %s: %s", note_id, harness_action_run_for_note)

        # if no such activity found then it must be the first time the note is being processed
        # hence keywords shall be generated be default
        if harness_action_run_for_note is None:
            logger.info("No previous activity found for note %s, eligible for action %s", note_id, action_run_kw)
            return True
        
        # get difference in seconds between now time and harness_action_run_for_note
        
        now = datetime.now(timezone.utc)
        last_run = datetime.strptime(
            harness_action_run_for_note,
            "%Y-%m-%d %H:%M:%S",
        ).replace(tzinfo=timezone.utc)
        
        difference_in_seconds = (now - last_run).total_seconds()
        # Example eligibility check: only allow refresh if more than cooldown seconds have passed
        if difference_in_seconds < cooldown:
            logger.info("Action %s not eligible for note %s, only %d seconds since last run", action_run_kw, note_id, difference_in_seconds)
            return False
        
        
        return True

    def _fetch_note_activity(self, note_id: str, action_run_kw: str) -> list[dict[str, Any]]:
        """Fetch the activity for a given note."""
        logger.info("Fetching activity for note %s", note_id)

        query = f"""
                    SELECT MAX(finished_at) AS latest_finished_at
                    FROM activity_notification_log
                    WHERE note_id = ?
                      AND json_valid(extra)
                      AND EXISTS (
                          SELECT 1
                          FROM json_each(extra, '$.actions_run')
                          WHERE json_each.value = ?
                      );
                """

        rows = db.execute_query(query, (note_id, action_run_kw))

        if not rows:
            return None

        return rows[0]['latest_finished_at']

    def _latest_action_run_metadata(self, note_id: str, action_run_kw: str) -> dict[str, Any] | None:
        """Fetch the latest action run metadata for a given note and action."""
        query = f"""
                    SELECT *
                    FROM activity_notification_log
                    WHERE note_id = ?
                      AND json_valid(extra)
                      AND EXISTS (
                          SELECT 1
                          FROM json_each(extra, '$.actions_run')
                          WHERE json_each.value = ?
                      )
                    ORDER BY finished_at DESC
                    LIMIT 1;
                """
        rows = db.execute_query(query, (note_id, action_run_kw))
        if not rows:
            return None
        return rows[0]

    def _fetch_note(self, note_id: str) -> dict[str, Any]:
        """Fetch the latest note before any read-modify-write operation."""
        logger.info("Fetching latest note %s", note_id)
        return self.plainapp_api.get_note(note_id=note_id)

    def _parse_note(self, note: dict[str, Any]) -> dict[str, Any]:
        """Parse a note into seed, keywords, and timeline sections."""
        content = note.get("content", "")
        keyword_start = content.find(KEYWORDS_MARKER)
        timeline_start = content.find(TIMELINE_MARKER)

        marker_positions = [
            position
            for position in (keyword_start, timeline_start)
            if position >= 0
        ]
        seed_end = min(marker_positions) if marker_positions else len(content)

        keywords = ""
        timeline = ""
        if keyword_start >= 0 and (
            timeline_start < 0 or keyword_start < timeline_start
        ):
            keywords_start = keyword_start + len(KEYWORDS_MARKER)
            keywords_end = timeline_start if timeline_start >= 0 else len(content)
            keywords = content[keywords_start:keywords_end].strip()

        if timeline_start >= 0:
            timeline_start_content = timeline_start + len(TIMELINE_MARKER)
            timeline = content[timeline_start_content:].strip()

        timeline_blocks = self._parse_timeline_blocks(timeline)

        return {
            "seed": content[:seed_end].strip(),
            "keywords": keywords,
            "timeline": timeline,
            "timeline_blocks": timeline_blocks,
        }

    def _parse_timeline_blocks(self, timeline: str) -> list[str]:
        """Split timeline content into independently preserved blocks.

        Existing timeline text without block markers is retained as one
        legacy block, so a keyword refresh cannot erase it.
        """
        if not timeline:
            return []

        pattern = re.compile(
            re.escape(TIMELINE_BLOCK_START)
            + r"\s*(.*?)\s*"
            + re.escape(TIMELINE_BLOCK_END),
            re.DOTALL,
        )
        matches = list(pattern.finditer(timeline))

        if not matches:
            return [timeline]

        unmarked_content = pattern.sub("", timeline).strip()
        if unmarked_content:
            logger.warning(
                "Unmarked timeline content found; preserving it as one block"
            )
            return [unmarked_content] + [match.group(1).strip() for match in matches]

        return [match.group(1).strip() for match in matches]

    def _reassemble_note(self, sections: dict[str, Any]) -> str:
        """Reassemble parsed sections into PlainApp note content."""
        timeline_blocks = sections.get("timeline_blocks")
        if timeline_blocks is None:
            timeline_blocks = sections["timeline"]

        if isinstance(timeline_blocks, str):
            timeline_blocks = [timeline_blocks] if timeline_blocks.strip() else []

        timeline_content = "\n\n".join(
            f"{TIMELINE_BLOCK_START}\n{block.strip()}\n{TIMELINE_BLOCK_END}"
            for block in timeline_blocks
            if str(block).strip()
        )

        return "\n\n".join(
            (
                sections["seed"].strip(),
                KEYWORDS_MARKER,
                str(sections["keywords"]).strip(),
                TIMELINE_MARKER,
                timeline_content,
            )
        ).strip() + "\n"

    def _write_note(
        self,
        note: dict[str, Any],
        content: str,
    ) -> None:
        """Write the latest assembled content back to the note."""
        logger.info("Writing updated note %s", note["id"])
        response = self.plainapp_api.save_note(
            note_id=note["id"],
            title=note["title"],
            content=content,
        )
        logger.info("Note write response: %s", response)

    async def _get_llm_generated_keywords(
        self,
        title: str,
        content: str,
    ) -> list[str]:
        response = await self.use_llm.get_llm_response(
            query=(
                "Extract and expand 3 to 10 high-value search keywords "
                "for articles related to the input below.\n"
                "Output ONLY the keywords, one per line, with no extra "
                "text or symbols.\n\n"
                "Rules:\n"
                "- Maximum 2 words per keyword.\n"
                "- Bind sub-topics to the main subject.\n"
                "- Prefer specific people, places, events, and entities.\n"
                "- Do not output metadata labels or generic filler.\n\n"
                f"TITLE: {title}\n\nCONTENT: {content}"
            ),
            system=(
                "You are an expert search keyword extractor. Return only "
                "3 to 10 distinct keywords, one per line. Do not use "
                "numbers, bullets, punctuation, headings, or explanations. "
                "Each keyword must contain at most two words and must be "
                "anchored to the topic's core entity."
            ),
            temperature=0,
            max_tokens=5000,
            web_search=True,
        )
        return [line.strip() for line in response.splitlines() if line.strip()]

    async def _generate_timeline(
        self,
        kw_feeds: dict[str, list[dict[str, Any]]],
        current_section: dict[str, Any],
    ) -> dict[str, Any]:
        """Generate timeline sections from keyword-wise feeds."""
        current_seed = current_section.get("seed")
        current_keywords = current_section.get("keywords")
        current_timeline = current_section.get("timeline")
        current_timeline_block = current_section.get("timeline_blocks")

        # initiated as current section's values so that if no new timeline block is generated,
        # the section retains its existing data.
        new_sections = {
            "seed": current_seed,
            "keywords": current_keywords,
            "timeline": current_timeline,
            "timeline_blocks": current_timeline_block,
        }

        todays_block = await self._generate_todays_block(kw_feeds, current_section)

        if todays_block and todays_block != "NO UPDATES":
            # add todays date in form : <mark>highlighted text</mark> as the top line of todays' block
            todays_block = f"<mark>{datetime.now().strftime('%Y-%m-%d')}</mark>\n\n{todays_block}"
            new_sections["timeline_blocks"].append(todays_block)
            message = {'new_sections': new_sections, 'status': 'success'}
            return message
        else:
            message = {'new_sections': new_sections, 'status': 'no_updates'}
            return message



    async def _generate_todays_block(
        self,
        kw_feeds: dict[str, list[dict[str, Any]]],
        current_section: dict[str, Any],
    ) -> dict[str, Any] | None:
        """Generate today's timeline block based on the current timeline block."""

    # first condition, kw_feeds has {keywords: [list of feeds]}
    # if there are no feeds on any keyword, then return None immediately
        if not any(kw_feeds.values()):
            return None

        kw_content_summary = {}
        for kw, feeds in kw_feeds.items():
            if not feeds:
                continue

            total_feed_content = "\n <- NEW FEED CONTENT -> \n ".join(feed.get("content") for feed in feeds)

            # return aggregate content summary for the keyword in first pass.
            
            query = f"""Summarize the following content for User Goal of: {current_section['seed']}.
            Similar keyword identified for the goal is: {kw}.
            Return a concise summary of the relevant information extracted from the full feed content, in about 50-80 words.
            Full feed content: {total_feed_content}"""

            system = f"""You are being provided the agggregated feeds content for the keyword '{kw}'. 
            These feeds are sourced from various RSS feed sources, and will have actual content + certain advertisements embedded within them.
            You job is to look through entire content passed to you, and summarize the information relevant to the keyword provided in the query
            and the user goal provided, ignoring any irrelevant content such as advertisements.
            Also, certain content/information across the full content space will be redundant or repeatable, so focus on extracting unique and relevant information.
            Basically, from the huge content provided, extract and summarize only the information that is relevant to the keyword and the user goal.
            """

            response = await self.use_llm.get_llm_response(
                query=query,
                system=system,
                temperature=0,
                max_tokens=7000,
                web_search=False,
                )

            kw_content_summary[kw] = response

        total_content_summary_of_kw = "\n".join(kw_content_summary.values())

        query = f"""
        Below is today's report of learned events related to the user goal of: {current_section['seed']}.

        And this is the current timeline of events related to the feeds content: {current_section['timeline_blocks']}
        Read through the content thoroughly and return back with a crisp summary that would serve as a quick daily briefing.
        It should align with what the user has asked for, and on comparing with past events, if nothing new is found, just mention "NO UPDATES".
        And should talk and group the information logically based on the topics/themes available.
        Full feed content: {total_content_summary_of_kw}"""
        
        system = f"""You are being provided the aggregated feeds content for the keyword '{kw}'.
        Also, you will are provided wit the current timeline of events related to the feeds content.
        The timeline of events should be considered while summarizing the content so that redundant issues are not highlighted.
        If nothing new to report, just reply "NO UPDATES"
        The final brief should be 20-40 words STRICTLY if there is anything new to report.
        Basically, from the huge content provided, extract and summarize only the information that is relevant to the keyword and the user goal.
        """

        response = await self.use_llm.get_llm_response(
            query=query,
            system=system,
            temperature=0,
            max_tokens=5000,
            web_search=False,
            )


        return response
        