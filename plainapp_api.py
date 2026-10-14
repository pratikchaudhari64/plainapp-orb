"""Reusable PlainApp API client for PlainWatch programs."""

from __future__ import annotations

import os
import re
import logging
from functools import wraps
from typing import Any

import requests
from dotenv import load_dotenv


load_dotenv()

logger = logging.getLogger(__name__)


class PlainAppUnavailable(RuntimeError):
    """Raised when PlainApp cannot accept API requests."""


def require_plainapp_health(method):
    """Prevent an API method from running while PlainApp is unavailable."""
    @wraps(method)
    def wrapper(self, *args, **kwargs):
        health = self.health_check()
        if health["status_code"] != 200:
            logger.warning("PlainApp request blocked: %s", health["message"])
            raise PlainAppUnavailable(health["message"])
        return method(self, *args, **kwargs)

    return wrapper


class PlainAppAPI:
    """Small GraphQL client exposing PlainWatch-facing PlainApp operations."""

    def __init__(self) -> None:
        base_url = os.getenv("PLAINAPP_URL")
        self.client_id = os.getenv("CLIENT_ID")
        self.bearer_token = os.getenv("BEARER_TOKEN")

        if not base_url or not self.client_id or not self.bearer_token:
            raise RuntimeError(
                "Set PLAINAPP_URL, CLIENT_ID, and BEARER_TOKEN in .env."
            )

        self.graphql_url = f"{base_url.rstrip('/')}/graphql"

    def _headers(self) -> dict[str, str]:
        return {
            "c-id": self.client_id,
            "Authorization": f"Bearer {self.bearer_token}",
            "Content-Type": "application/json",
        }

    @require_plainapp_health
    def make_graphql_request(
        self,
        query: str,
        variables: dict[str, Any] | None = None,
    ) -> dict[str, Any]:
        """Execute one GraphQL request and return its decoded response."""
        payload: dict[str, Any] = {"query": query}
        if variables is not None:
            payload["variables"] = variables

        response = requests.post(
            self.graphql_url,
            headers=self._headers(),
            json=payload,
            timeout=10,
        )
        response.raise_for_status()

        result = response.json()
        if result.get("errors"):
            raise RuntimeError(result["errors"])
        return result

    #TODO: could be turned into a decorator
    def health_check(self) -> dict[str, Any]:
        """Check whether the PlainApp HTTP server is reachable."""
        try:
            response = requests.get(
                f"{self.graphql_url.rsplit('/', 1)[0]}/health",
                timeout=5,
            )
        except requests.RequestException as e:
            error_text = str(e)
            host_match = re.search(
                r"HTTPConnectionPool\(host='([^']+)'",
                error_text,
            )
            host = host_match.group(1) if host_match else "unknown host"

            if "ConnectTimeoutError" in error_text:
                message = f"PlainApp host {host} is unreachable or timed out."
            elif "Connection refused" in error_text:
                message = f"PlainApp host {host} is reachable, but PlainApp is off."
            else:
                message = f"PlainApp health check failed for host {host}."

            return {
                "ok": False,
                "status_code": None,
                "message": message,
            }
        
        return {
            "ok": response.ok,
            "status_code": response.status_code,
            "message": response.text,
        }

    def inspect_type_properties(self, type_name: str) -> dict[str, Any]:
        """Return the properties exposed by a GraphQL type on this server.

        Examples of type names are ``Note``, ``FeedEntry``, ``Feed``, and
        ``NoteInput``. This uses GraphQL introspection, not PlainApp raw SQL.
        """
        query = """
        query InspectType($typeName: String!) {
            __type(name: $typeName) {
                kind
                name
                description
                fields {
                    name
                    description
                    type { ...TypeReference }
                }
                inputFields {
                    name
                    description
                    type { ...TypeReference }
                }
                enumValues { name description }
            }
        }

        fragment TypeReference on __Type {
            kind
            name
            ofType {
                kind
                name
                ofType {
                    kind
                    name
                    ofType { kind name }
                }
            }
        }
        """
        type_info = self.make_graphql_request(
            query,
            {"typeName": type_name},
        )["data"]["__type"]
        if type_info is None:
            raise ValueError(f"GraphQL type does not exist: {type_name}")
        return type_info

    def list_database_tables(self) -> list[str]:
        """Return the names of all tables in PlainApp's SQLite database."""
        query = """
        query GetDbTables {
            dbTables
        }
        """
        return self.make_graphql_request(query)["data"]["dbTables"]

    def list_raw_table_rows(
        self,
        table: str,
        offset: int = 0,
        limit: int = 100,
    ) -> list[dict[str, Any]]:
        """Return one page of rows from a database table.

        Args:
            table: Exact database table name, as returned by
                :meth:`list_database_tables`.
            offset: Number of rows to skip before the first returned row.
            limit: Maximum number of rows to return. This is sent to PlainApp
                as the documented ``limit`` argument.

        Returns:
            A page of row objects mapping column names to values.
        """
        if offset < 0:
            raise ValueError("offset must be zero or greater")
        if limit <= 0:
            raise ValueError("limit must be greater than zero")

        query = """
        query GetDbTableRows($table: String!, $offset: Int!, $limit: Int!) {
            dbTableRows(table: $table, offset: $offset, limit: $limit)
        }
        """
        return self.make_graphql_request(
            query,
            {
                "table": table,
                "offset": offset,
                "limit": limit,
            },
        )["data"]["dbTableRows"]

    def fetch_all_raw_table_rows(
        self,
        table: str,
        offset: int = 0,
        limit: int = 100,
    ) -> list[dict[str, Any]]:
        """Fetch all rows from a database table using paginated raw access.

        Args:
            table: Exact database table name, as returned by
                :meth:`list_database_tables`.
            offset: Number of rows to skip before the first returned row.
            limit: Maximum number of rows requested per API call.

        Returns:
            All row objects from the requested offset onward.
        """
        if offset < 0:
            raise ValueError("offset must be zero or greater")
        if limit <= 0:
            raise ValueError("limit must be greater than zero")

        rows: list[dict[str, Any]] = []
        while True:
            page = self.list_raw_table_rows(table, offset=offset, limit=limit)
            rows.extend(page)

            if len(page) < limit:
                return rows
            offset += limit

    def sync_feeds(self, feed_id: str | None = None) -> Any:
        """Ask PlainApp to refresh one feed or all feeds."""
        query = """
        mutation SyncFeeds($id: ID) {
            syncFeeds(id: $id)
        }
        """
        return self.make_graphql_request(
            query,
            {"id": feed_id},
        )["data"]["syncFeeds"]

    def fetch_feed_content(self, feed_entry_id: str) -> dict[str, Any]:
        """Fetch and return the full article body for one feed entry."""
        query = """
        mutation FetchFeedContent($id: ID!) {
            fetchFeedContent(id: $id) { id content }
        }
        """
        return self.make_graphql_request(
            query,
            {"id": feed_entry_id},
        )["data"]["fetchFeedContent"]

    def list_feed_entries(
        self,
        query_text: str = "",
        page_size: int = 100,
    ) -> list[dict[str, Any]]:
        """Fetch all feed entries through PlainApp's offset pagination."""
        query = """
        query FeedEntries($offset: Int!, $limit: Int!, $query: String!) {
            feedEntries(offset: $offset, limit: $limit, query: $query) {
                id feedId title description content url publishedAt
                tags { id name count }
            }
        }
        """
        entries: list[dict[str, Any]] = []
        offset = 0

        while True:
            page = self.make_graphql_request(
                query,
                {"offset": offset, "limit": page_size, "query": query_text},
            )["data"]["feedEntries"]
            entries.extend(page)
            if len(page) < page_size:
                return entries
            offset += page_size

    def list_notes(
        self,
        query_text: str = "",
        page_size: int = 100,
    ) -> list[dict[str, Any]]:
        """Fetch all notes through PlainApp's 
        offset pagination.
        This only fetches non-trashed notes.
        The API provides no other route to fetch all notes including trashed.
        """
        query = """
        query GetNotes($offset: Int!, $limit: Int!, $query: String!) {
            notes(offset: $offset, limit: $limit, query: $query) {
                id title content createdAt updatedAt deletedAt
                tags { id name }
            }
        }
        """
        notes: list[dict[str, Any]] = []
        offset = 0

        while True:
            page = self.make_graphql_request(
                query,
                {"offset": offset, "limit": page_size, "query": query_text},
            )["data"]["notes"]
            notes.extend(page)
            if len(page) < page_size:
                return notes
            offset += page_size

    def get_note(self, note_id: str) -> dict[str, Any]:
        """Fetch one note by PlainApp ID."""
        query = """
        query Note($id: ID!) {
            note(id: $id) {
                id title content createdAt updatedAt tags { id name }
            }
        }
        """
        note = self.make_graphql_request(
            query,
            {"id": note_id},
        )["data"]["note"]

        return note

    def save_note(
        self,
        note_id: str,
        title: str,
        content: str,
    ) -> dict[str, Any]:
        """Create a note when note_id is empty, otherwise update it."""
        query = """
        mutation SaveNote($id: ID!, $input: NoteInput!) {
            saveNote(id: $id, input: $input) { id title updatedAt }
        }
        """
        variables = {
            "id": note_id,
            "input": {"title": title, "content": content},
        }
        return self.make_graphql_request(query, variables)["data"]["saveNote"]

    def create_note(self, title: str, content: str) -> dict[str, Any]:
        """Create a new PlainApp note and return its identity."""
        return self.save_note("", title, content)

    def update_note(self, note_id: str, title: str, content: str) -> dict[str, Any]:
        """Update an existing PlainApp note."""
        return self.save_note(note_id, title, content)

if __name__ == "__main__":
    from datetime import datetime, timezone, timedelta
    import json
    import time
    plainapp = PlainAppAPI()
    # print(plainapp.inspect_type_properties("Note"))
    
    # resp = plainapp.list_raw_table_rows(table='feeds',
    #                                     offset = 0,
    #                                     limit = 2)

    resp = plainapp.fetch_all_raw_table_rows(table='feed_entries')
    cnt = 0
    start_time = time.time()
    print(f"Total feeds fetched: {len(resp)}")
    #memory taken by the variable resp in MB
    import sys

    total_bytes = sys.getsizeof(resp) + sum(sys.getsizeof(item) for item in resp)
    print(f"Approx. resp memory: {total_bytes / (1024 * 1024):.2f} MB")
    for feed in resp:
        feed = json.loads(feed)
        print(f"\n {feed}")
        cnt += 1
        if cnt >= 10:
            break
    end_time = time.time()
    print(f"Time taken: {end_time - start_time} seconds")
        

    # resp = plainapp.list_database_tables()
    # print(resp)
    # resp = plainapp.sync_feeds(feed_id="")
    # print(resp)
    
    # feeds = plainapp.list_feed_entries(
    #                 query_text='Football',
    #                 page_size=100,
    #             )
    # print(f"{feeds[0].keys()}")

    # feedentry_id = 'u7p0tv1a75ma'
    # resp = plainapp.fetch_feed_content(feed_entry_id=feedentry_id)
    # print(resp)

    # feeds = plainapp.list_feed_entries(
    #                 query_text='UPI India',
    #                 page_size=100,
    #             )
    # if feeds:
    #     print(len(feeds))
    #     print((feeds[0])['publishedAt'])
    #     # apply a filter to only include feeds with published_at of yesterday's date
    #     filtered_feeds = [
    #         feed for feed in feeds
    #         if "publishedAt" in feed and feed["publishedAt"].startswith((datetime.now(timezone.utc) - timedelta(days=1)).strftime("%Y-%m-%d"))
    #     ]

    #     print(len(filtered_feeds))
    
    
    # note = plainapp.save_note(note_id="1iaxm1xigk3lf", 
    #                           title="Sample Title", 
    #                           content="Sample Content")
    # print(note)


    # notes = plainapp.list_notes()
    # for note in notes:
    #     print(f" {note['id']} | {note['title'][:5]} | {note['tags']} | {note['updatedAt']} | {note['createdAt']} | {note['deletedAt']}")
    
    # save_response = plainapp.save_note(note_id="", 
    #                                 title="Sample Title",
    #                                 content="Sample Content")
    # print(save_response)
    
    # notes = plainapp.list_notes()
    # for note in notes:
    #     print(f" {note['id']} | {note['title'][:5]} | {note['tags']} | {note['updatedAt']} | {note['createdAt']} | {note['deletedAt']}")