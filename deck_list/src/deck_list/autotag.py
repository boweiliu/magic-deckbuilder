"""Guess which of a deck's categories its cards belong in, for the person to review before anything is saved.

A category gets guesses when its name matches a rule: "lands" goes by the card's type line, and
"ramp", "draw", "interaction", "wipes" and "tutor" go by Scryfall Tagger's oracle tags (a tag and
every tag under it, so "ramp" also covers mana rocks and land ramp). Guesses only ever add a tag;
a card that already carries the category is never suggested for it again, and no tag is removed.
"""

import json
import sqlite3
import threading
from typing import NamedTuple
from pathlib import Path

from deck_list.cards import Card


class Rule(NamedTuple):
    words: tuple[str, ...]  # a category whose name contains any of these gets this rule
    why: str  # what the guess is based on, as the page says it
    tagger_roots: tuple[str, ...]  # Tagger tags whose cards match (with every tag under them)
    skip_prefixes: tuple[str, ...]  # Tagger tags under a root that do not count
    land: bool  # match by type line instead: any face is a land


RULES = (
    Rule(("land",), "the card is a land (on either face)", (), (), True),
    Rule(("ramp",), "Scryfall tags it as ramp", ("ramp",), (), False),
    Rule(("draw", "advantage"), "Scryfall tags it as card draw or card advantage", ("card-advantage",), (), False),
    Rule(("interaction", "removal", "counter"), "Scryfall tags it as spot removal or a counterspell",
         ("spot-removal", "counterspell"), (), False),
    Rule(("wipe", "sweeper", "wrath"), "Scryfall tags it as a sweeper", ("sweeper",), (), False),
    Rule(("tutor",), "Scryfall tags it as a tutor (not counting land searches)", ("tutor",),
         ("tutor-land", "tutor-basic"), False),
)


def rule_for(category_name: str) -> Rule | None:
    name = category_name.lower()
    return next((r for r in RULES if any(w in name for w in r.words)), None)


def is_land(card: Card) -> bool:
    return any("Land" in face for face in card.type_line.split(" // "))


class TaggerIndex:
    """Which cards carry a Tagger tag or any tag under it, reloaded when the daily refresh replaces the database."""

    def __init__(self, db_path: Path) -> None:
        self._db_path = db_path
        self._lock = threading.Lock()
        self._mtime: float | None = None
        self._cache: dict[tuple[tuple[str, ...], tuple[str, ...]], frozenset[str]] = {}

    def cards_with(self, roots: tuple[str, ...], skip_prefixes: tuple[str, ...] = ()) -> frozenset[str]:
        mtime = self._db_path.stat().st_mtime
        key = (roots, skip_prefixes)
        with self._lock:
            if mtime != self._mtime:
                self._cache, self._mtime = {}, mtime
            if key not in self._cache:
                self._cache[key] = self._load(roots, skip_prefixes)
            return self._cache[key]

    def _load(self, roots: tuple[str, ...], skip_prefixes: tuple[str, ...]) -> frozenset[str]:
        conn = sqlite3.connect(f"file:{self._db_path}?mode=ro", uri=True)
        try:
            tags = {tid: (slug, json.loads(children or "[]"))
                    for tid, slug, children in conn.execute("SELECT id, slug, child_ids FROM tags WHERE type = 'oracle'")}
            by_slug = {slug: tid for tid, (slug, _) in tags.items()}
            seen: set[str] = set()
            todo = [by_slug[r] for r in roots if r in by_slug]
            while todo:
                tid = todo.pop()
                if tid in seen or tid not in tags or tags[tid][0].startswith(skip_prefixes):
                    continue
                seen.add(tid)
                todo.extend(tags[tid][1])
            if not seen:
                return frozenset()
            marks = ",".join("?" * len(seen))
            rows = conn.execute(
                f"SELECT DISTINCT oc.name FROM oracle_taggings ot JOIN oracle_cards oc ON oc.oracle_id = ot.oracle_id "
                f"WHERE ot.tag_id IN ({marks})", sorted(seen))
            return frozenset(r[0] for r in rows)
        finally:
            conn.close()


class Guess(NamedTuple):
    category_id: str
    why: str
    cards: tuple[str, ...]


def guesses(categories, cards: list[Card], current: dict[str, tuple[str, ...]], tagger: TaggerIndex) -> tuple[list[Guess], list[str]]:
    """Per category, the deck's cards the rule matches that do not carry the category yet; and the categories with no rule."""
    found, no_rule = [], []
    for category in categories:
        rule = rule_for(category.name)
        if rule is None:
            no_rule.append(category.id)
            continue
        matching = set() if rule.land else tagger.cards_with(rule.tagger_roots, rule.skip_prefixes)
        picked = tuple(sorted(
            c.name for c in cards
            if category.id not in current.get(c.name, ()) and (is_land(c) if rule.land else c.name in matching)
        ))
        found.append(Guess(category.id, rule.why, picked))
    return found, no_rule
