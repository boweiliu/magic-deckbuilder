"""Card names from the local Scryfall copy, for autocomplete and for looking up a deck's cards."""

import json
import re
import sqlite3
import threading
from typing import NamedTuple
from pathlib import Path

# Layouts that are never a card you put in a deck.
NOT_DECK_CARDS = ("token", "double_faced_token", "emblem", "art_series", "vanguard", "scheme", "planar")

_LOAD_SQL = f"""
SELECT c.name, c.mana_cost, c.cmc, c.type_line, c.image_normal, c.image_back, c.color_identity, c.scryfall_uri,
       json_extract(c.raw, '$.legalities.commander'), json_extract(c.raw, '$.edhrec_rank'),
       json_extract(c.raw, '$.card_faces')
FROM oracle_cards o JOIN cards c ON c.id = o.card_id
WHERE c.layout NOT IN ({",".join("?" * len(NOT_DECK_CARDS))})
"""


def normalize(text: str) -> str:
    """Lowercase, apostrophes and punctuation dropped, so "jaces" finds "Jace's"."""
    return re.sub(r"\s+", " ", re.sub(r"[^a-z0-9 ]", "", text.lower().replace("-", " "))).strip()


class Card(NamedTuple):
    name: str
    mana_cost: str
    mana_value: float
    type_line: str
    image: str | None
    image_back: str | None
    color_identity: tuple[str, ...]
    scryfall_uri: str | None
    commander_legality: str | None
    edhrec_rank: int | None
    face_names: tuple[str, ...]
    key: str
    face_keys: tuple[str, ...]


def _card(row: tuple) -> Card:
    name, mana_cost, cmc, type_line, image, image_back, identity, uri, legality, rank, faces_json = row
    faces = tuple(f["name"] for f in json.loads(faces_json) if f.get("name")) if faces_json else ()
    return Card(
        name=name, mana_cost=mana_cost or "", mana_value=cmc or 0.0, type_line=type_line or "", image=image, image_back=image_back,
        color_identity=tuple(json.loads(identity or "[]")), scryfall_uri=uri, commander_legality=legality,
        # Scryfall gives basic lands no popularity rank; they are the most played cards of all.
        edhrec_rank=0 if (type_line or "").startswith("Basic Land") else rank, face_names=faces, key=normalize(name), face_keys=tuple(normalize(f) for f in faces),
    )


class CardIndex:
    """Every deck-eligible card, reloaded when the daily refresh replaces the database."""

    def __init__(self, db_path: Path) -> None:
        self._db_path = db_path
        self._lock = threading.Lock()
        self._mtime: float | None = None
        self._cards: list[Card] = []
        self._by_key: dict[str, Card] = {}

    def _fresh(self) -> list[Card]:
        mtime = self._db_path.stat().st_mtime
        with self._lock:
            if mtime != self._mtime:
                conn = sqlite3.connect(f"file:{self._db_path}?mode=ro", uri=True)
                try:
                    cards = [_card(r) for r in conn.execute(_LOAD_SQL, NOT_DECK_CARDS)]
                finally:
                    conn.close()
                by_key: dict[str, Card] = {}
                for card in cards:
                    for key in (card.key, *card.face_keys):
                        by_key.setdefault(key, card)
                self._cards, self._by_key, self._mtime = cards, by_key, mtime
            return self._cards

    def lookup(self, name: str) -> Card | None:
        self._fresh()
        return self._by_key.get(normalize(name))

    def suggest(self, query: str, limit: int = 12) -> list[Card]:
        q = normalize(query)
        if not q:
            return []
        scored = []
        for card in self._fresh():
            keys = (card.key, *card.face_keys)
            if card.key == q or q in card.face_keys:
                tier = -1
            elif card.key.startswith(q):
                tier = 0
            elif any(k.startswith(q) for k in card.face_keys):
                tier = 1
            elif any((" " + k).find(" " + q) >= 0 for k in keys):
                tier = 2
            elif any(q in k for k in keys):
                tier = 3
            else:
                continue
            not_legal = card.commander_legality not in ("legal", "restricted")
            rank = 10**9 if card.edhrec_rank is None else card.edhrec_rank
            scored.append((not_legal, tier, rank, card.name, card))
        scored.sort(key=lambda s: s[:4])
        return [s[4] for s in scored[:limit]]


# Scryfall's Tagger marks the cards of a cycle (the five eggs, the ten landscapes, the basic
# lands) with one shared oracle tag whose slug starts with "cycle-".
_CYCLES_SQL = """
SELECT t.slug, oc.name FROM tags t
JOIN oracle_taggings ot ON ot.tag_id = t.id
JOIN oracle_cards oc ON oc.oracle_id = ot.oracle_id
WHERE t.type = 'oracle' AND t.slug LIKE 'cycle-%'
ORDER BY t.slug, oc.name
"""


class Cycle(NamedTuple):
    id: str
    name: str
    cards: tuple[str, ...]


def cycle_name(slug: str, set_codes: frozenset[str]) -> str:
    """'cycle-mh3-landscape' -> 'Landscape (MH3)'; 'cycle-basic-snow-land' -> 'Basic snow land'."""
    words = slug.removeprefix("cycle-").split("-")
    where = words.pop(0) if len(words) > 1 and words[0] in set_codes else None
    name = " ".join(words).capitalize()
    return f"{name} ({where.upper()})" if where else name


class CycleIndex:
    """Which cycles each card is in, reloaded when the daily refresh replaces the database."""

    def __init__(self, db_path: Path) -> None:
        self._db_path = db_path
        self._lock = threading.Lock()
        self._mtime: float | None = None
        self._by_card: dict[str, tuple[Cycle, ...]] = {}

    def _fresh(self) -> dict[str, tuple[Cycle, ...]]:
        mtime = self._db_path.stat().st_mtime
        with self._lock:
            if mtime != self._mtime:
                conn = sqlite3.connect(f"file:{self._db_path}?mode=ro", uri=True)
                try:
                    set_codes = frozenset(r[0] for r in conn.execute("SELECT DISTINCT set_code FROM cards"))
                    members: dict[str, list[str]] = {}
                    for slug, name in conn.execute(_CYCLES_SQL):
                        members.setdefault(slug, []).append(name)
                finally:
                    conn.close()
                by_card: dict[str, list[Cycle]] = {}
                for slug, names in members.items():
                    if len(names) < 2:
                        continue
                    cycle = Cycle(slug, cycle_name(slug, set_codes), tuple(names))
                    for name in names:
                        by_card.setdefault(name, []).append(cycle)
                self._by_card = {name: tuple(sorted(cs, key=lambda c: len(c.cards))) for name, cs in by_card.items()}
                self._mtime = mtime
            return self._by_card

    def of(self, name: str) -> tuple[Cycle, ...]:
        """The cycles a card (by its exact name) is in, smallest first."""
        return self._fresh().get(name, ())
