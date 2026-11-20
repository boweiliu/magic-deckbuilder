"""Run one card search (a SQL query returning oracle_ids) read-only against the local Scryfall copy.

The page and the chat agent writing searches run queries through this same code, so a query the agent
checked is exactly what the page shows. Command line, for the agent:

    python -m card_search.query --db <scryfall.db> "SELECT oracle_id FROM ..."
"""

import argparse
import json
import os
import re
import sqlite3
import sys
import threading
import time
from functools import lru_cache
from pathlib import Path
from typing import NamedTuple

from loguru import logger

from card_search import abilities

MAX_CARDS = 1000
# The side database of parsed abilities (see abilities.py), kept with the app's data. Relative to the repo root.
ABILITIES_DB = Path(os.environ.get("CARD_SEARCH_DATA_DIR", "data/.apps/card-search")) / "abilities.db"
# A query running longer than this is stopped (a runaway join or regex).
TIME_LIMIT_SECONDS = 20.0

_CARD_SQL = """
SELECT o.oracle_id, c.name, c.mana_cost, c.cmc, c.type_line, c.oracle_text, c.image_normal, c.image_back,
       c.scryfall_uri, c.color_identity, json_extract(c.raw, '$.edhrec_rank')
FROM oracle_cards o JOIN cards c ON c.id = o.card_id
WHERE o.oracle_id IN (SELECT oracle_id FROM ({inner}
))
ORDER BY {order}
LIMIT {limit}
"""


_POPULAR = "COALESCE(json_extract(c.raw, '$.edhrec_rank'), 1000000000), c.name"
# How the cards come back. Most played = lowest EDHREC rank; cards with no rank go last.
ORDERS = {
    "popular": _POPULAR,
    "mv": f"c.cmc, {_POPULAR}",
    "name": "c.name",
}


class QueryError(Exception):
    """The query could not be run (bad SQL, wrong columns, too slow)."""


class CardRow(NamedTuple):
    oracle_id: str
    name: str
    mana_cost: str
    cmc: float | None
    type_line: str
    oracle_text: str
    image: str | None
    image_back: str | None
    scryfall_uri: str | None
    color_identity: list[str]
    edhrec_rank: int | None
    # Rules-text lines the query itself matched (when it returns a line_no column, as abilities searches do).
    match_lines: list[int]


class QueryResult(NamedTuple):
    total: int
    cards: list[CardRow]
    seconds: float


@lru_cache(maxsize=512)
def _compiled(pattern: str) -> re.Pattern[str]:
    return re.compile(pattern)


def _regexp(pattern: str | None, value: str | None) -> int:
    """SQLite's `value REGEXP pattern`: Python re.search; use (?i) and (?m) inline for flags."""
    if pattern is None or value is None:
        return 0
    return 1 if _compiled(pattern).search(value) else 0


def connect(db_path: Path) -> sqlite3.Connection:
    side = abilities.ensure(db_path, ABILITIES_DB)
    conn = sqlite3.connect(f"file:{db_path}?mode=ro", uri=True, check_same_thread=False)
    conn.create_function("REGEXP", 2, _regexp, deterministic=True)
    conn.execute("ATTACH DATABASE ? AS ab", (f"file:{side.absolute()}?mode=ro",))
    return conn


def _strip(sql: str) -> str:
    sql = sql.strip().rstrip(";").strip()
    # Checked without -- comments, so a comment may hold a ';' and may come before the SELECT.
    code = re.sub(r"--[^\n]*", "", sql).strip().rstrip(";")
    if ";" in code:
        raise QueryError("one statement only (no ';' inside the query)")
    if not re.match(r"(?is)^\s*(with|select)\b", code):
        raise QueryError("a search is a SELECT (or WITH ... SELECT) query")
    return sql


def run(db_path: Path, sql: str, limit: int = MAX_CARDS, order: str = "popular") -> QueryResult:
    """The cards whose oracle_id the query returns, in ``order`` (a key of ORDERS; default most played first)."""
    inner = _strip(sql)
    conn = connect(db_path)
    started = time.monotonic()
    timer = threading.Timer(TIME_LIMIT_SECONDS, conn.interrupt)
    timer.start()
    try:
        total = conn.execute(f"SELECT COUNT(DISTINCT oracle_id) FROM ({inner}\n)").fetchone()[0]
        rows = conn.execute(_CARD_SQL.format(inner=inner, limit=int(limit), order=ORDERS[order])).fetchall()
        columns = [d[0] for d in conn.execute(f"SELECT * FROM ({inner}\n) LIMIT 0").description]
        match_lines: dict[str, set[int]] = {}
        if "line_no" in columns:
            for oracle_id, line_no in conn.execute(f"SELECT oracle_id, line_no FROM ({inner}\n)"):
                match_lines.setdefault(oracle_id, set()).add(line_no)
    except sqlite3.OperationalError as e:
        if "interrupted" in str(e):
            raise QueryError(f"the query took longer than {TIME_LIMIT_SECONDS:.0f} seconds") from e
        raise QueryError(str(e)) from e
    except sqlite3.DatabaseError as e:
        raise QueryError(str(e)) from e
    finally:
        timer.cancel()
        conn.close()
    cards = [
        CardRow(r[0], r[1], r[2] or "", r[3], r[4] or "", r[5] or "", r[6], r[7], r[8], json.loads(r[9] or "[]"), r[10],
                sorted(match_lines.get(r[0], ())))
        for r in rows
    ]
    return QueryResult(total, cards, round(time.monotonic() - started, 2))


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    parser.add_argument("--db", required=True)
    parser.add_argument("--show", type=int, default=25, help="how many cards to print (most played first)")
    parser.add_argument("--text", action="store_true", help="print each card's rules text too")
    parser.add_argument("sql")
    args = parser.parse_args(argv)
    # The agent reads this output: plain lines, no log decoration.
    logger.remove()
    logger.add(sys.stdout, format="{message}", level="INFO")
    try:
        result = run(Path(args.db), args.sql)
    except QueryError as e:
        logger.error("error: {}", e)
        return 1
    logger.info("{} cards ({}s)", result.total, result.seconds)
    for card in result.cards[: args.show]:
        logger.info("- {}  {}  [{}]", card.name, card.mana_cost, card.type_line)
        if args.text:
            for i, line in enumerate(card.oracle_text.splitlines()):
                logger.info("  {} {}", ">" if i in card.match_lines else " ", line)
    return 0

if __name__ == "__main__":
    sys.exit(main())
