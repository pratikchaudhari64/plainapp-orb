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
    plainapp = PlainAppAPI()
    # print(plainapp.inspect_type_properties("Note"))
    
    note = plainapp.get_note(note_id = "1iaxm1xigk3lf")
    print(note)
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