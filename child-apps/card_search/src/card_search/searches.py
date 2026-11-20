"""Saved searches: one JSON file per search, written by the chat agent and by the person's edits on the page.

A search is a title, notes, and an ordered list of queries (usually one per tier). The chat agent writes
whole searches (PUT); the page saves the person's edit to one query. Every change bumps the version and
keeps the version it replaced under ``history``, so nothing either side wrote is ever lost.
"""

import datetime
import json
import os
import re
import threading
from pathlib import Path

from loguru import logger

_lock = threading.Lock()
_ID = re.compile(r"^[a-z0-9][a-z0-9-]{0,59}$")

# Who made a change: the chat agent that writes searches, or the person editing on the page.
BY_AGENT = "claude"
BY_PERSON = "you"


class SearchNotFound(KeyError):
    """No saved search has that id."""


class BadSearchId(ValueError):
    """A search id must be lowercase letters, digits and dashes."""


class NoSuchQuery(IndexError):
    """The search has no query at that position."""


def _now() -> str:
    return datetime.datetime.now(datetime.UTC).isoformat(timespec="seconds")


def _path(searches_dir: Path, search_id: str) -> Path:
    if not _ID.match(search_id):
        raise BadSearchId(search_id)
    return searches_dir / f"{search_id}.json"


def _write(searches_dir: Path, record: dict) -> None:
    searches_dir.mkdir(parents=True, exist_ok=True)
    path = _path(searches_dir, record["id"])
    tmp = path.with_name(path.name + ".tmp")
    tmp.write_text(json.dumps(record, indent=2))
    os.replace(tmp, path)


def _upgrade(record: dict) -> dict:
    """Searches saved by the earlier background-agent version of the app: request/summary become title/notes."""
    if "title" in record:
        return record
    queries = [dict(q, by=BY_AGENT) for q in record.get("queries") or []]
    return {
        "id": record["id"], "title": record.get("request", ""), "notes": record.get("summary") or record.get("error") or "",
        "queries": queries, "version": 1, "created_at": record.get("started_at", ""),
        "updated_at": record.get("finished_at") or record.get("started_at", ""), "updated_by": BY_AGENT,
        "history": [], "original": record,
    }


def read(searches_dir: Path, search_id: str) -> dict:
    try:
        path = _path(searches_dir, search_id)
    except BadSearchId as e:
        raise SearchNotFound(search_id) from e
    if not path.exists():
        raise SearchNotFound(search_id)
    return _upgrade(json.loads(path.read_text()))


def list_all(searches_dir: Path) -> list[dict]:
    """Every search, most recently changed first."""
    if not searches_dir.exists():
        return []
    records = []
    for path in searches_dir.glob("*.json"):
        try:
            records.append(_upgrade(json.loads(path.read_text())))
        except (OSError, json.JSONDecodeError, KeyError):
            logger.warning("Skipping unreadable search file {}", path)
    return sorted(records, key=lambda r: r.get("updated_at", ""), reverse=True)


def _snapshot(record: dict) -> dict:
    return {k: record[k] for k in ("version", "updated_at", "updated_by", "title", "notes", "queries")}


def put(searches_dir: Path, search_id: str, title: str, notes: str, queries: list[dict]) -> dict:
    """Create a search, or replace it with a new version (the old one goes to history). The chat agent's write."""
    with _lock:
        try:
            record = read(searches_dir, search_id)
            record["history"].append(_snapshot(record))
            record["version"] += 1
        except SearchNotFound:
            _path(searches_dir, search_id)
            record = {"id": search_id, "version": 1, "created_at": _now(), "history": []}
        record.update(
            title=title, notes=notes, queries=[dict(q, by=BY_AGENT) for q in queries],
            updated_at=_now(), updated_by=BY_AGENT,
        )
        _write(searches_dir, record)
    return record


def edit_query(searches_dir: Path, search_id: str, index: int, sql: str, highlight: str | None) -> dict:
    """Save the person's edit to one query as a new version (the old one goes to history)."""
    with _lock:
        record = read(searches_dir, search_id)
        if not 0 <= index < len(record["queries"]):
            raise NoSuchQuery(index)
        query = record["queries"][index]
        if query["sql"] == sql and query.get("highlight") == highlight:
            return record
        record["history"].append(_snapshot(record))
        record["queries"] = [dict(q) for q in record["queries"]]
        record["queries"][index] = dict(query, sql=sql, highlight=highlight, by=BY_PERSON)
        record.update(version=record["version"] + 1, updated_at=_now(), updated_by=BY_PERSON)
        _write(searches_dir, record)
    return record


def agent_version(record: dict, index: int) -> dict | None:
    """The newest version of query ``index`` the chat agent wrote, for the page's "back to Claude's" button."""
    for snapshot in [record, *reversed(record["history"])]:
        queries = snapshot["queries"]
        if index < len(queries) and queries[index].get("by", BY_AGENT) == BY_AGENT:
            return queries[index]
    return None
