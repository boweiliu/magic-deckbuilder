"""Cards from the local Scryfall copy, with the rules text a text-only card view needs."""

import json
import sqlite3
from pathlib import Path
from typing import Any

from imbue.imbue_common.frozen_model import FrozenModel
from pydantic import Field

# Layouts that are never a card you put in a deck.
NOT_DECK_CARDS = ("token", "double_faced_token", "emblem", "art_series", "vanguard", "scheme", "planar")

_LOOKUP_SQL = f"""
SELECT c.raw FROM oracle_cards o JOIN cards c ON c.id = o.card_id
WHERE o.oracle_id IN (SELECT oracle_id FROM cards WHERE name = ? OR (name >= ? AND name < ?)) AND c.layout NOT IN ({",".join("?" * len(NOT_DECK_CARDS))})
ORDER BY c.name = ? DESC LIMIT 1
"""


class Face(FrozenModel):
    """One face of a card (a one-faced card has one)."""

    name: str = Field(description="Face name")
    mana_cost: str = Field(description="Mana cost in Scryfall's {2}{U} notation")
    type_line: str = Field(description="Type line")
    oracle_text: str = Field(description="Rules text, lines separated by newlines")
    power: str | None = Field(description="Power, for creatures")
    toughness: str | None = Field(description="Toughness, for creatures")
    loyalty: str | None = Field(description="Starting loyalty, for planeswalkers")
    defense: str | None = Field(description="Defense, for battles")
    image: str | None = Field(description="Scryfall image link of this face (the card's image for one-image cards)")


class CardData(FrozenModel):
    """Everything the board and the preview show about one card."""

    name: str = Field(description="The card's full name")
    found: bool = Field(description="False when the local card copy has no card by that name")
    mana_value: float = Field(description="Mana value")
    type_line: str = Field(description="Full type line; faces joined with ' // '")
    colors: list[str] = Field(description="Color identity letters")
    faces: list[Face] = Field(description="The card's faces, front first")
    scryfall_uri: str | None = Field(description="The card's page on Scryfall")


def _face(face: dict[str, Any], fallback_image: str | None) -> Face:
    images = face.get("image_uris") or {}
    return Face(
        name=face.get("name", ""), mana_cost=face.get("mana_cost", ""), type_line=face.get("type_line", ""),
        oracle_text=face.get("oracle_text", ""), power=face.get("power"), toughness=face.get("toughness"),
        loyalty=face.get("loyalty"), defense=face.get("defense"), image=images.get("normal") or fallback_image,
    )


def card_from_raw(raw: dict[str, Any]) -> CardData:
    card_image = (raw.get("image_uris") or {}).get("normal")
    raw_faces = raw.get("card_faces") or [raw]
    # Split and adventure cards print both halves on one image; transform cards have one image per face.
    faces = [_face(f, card_image) for f in raw_faces]
    return CardData(
        name=raw["name"], found=True, mana_value=raw.get("cmc") or 0.0, type_line=raw.get("type_line", ""),
        colors=raw.get("color_identity", []), faces=faces, scryfall_uri=raw.get("scryfall_uri"),
    )


def _missing(name: str) -> CardData:
    return CardData(name=name, found=False, mana_value=0.0, type_line="", colors=[], faces=[
        Face(name=name, mana_cost="", type_line="", oracle_text="(not found in the card data)", power=None,
             toughness=None, loyalty=None, defense=None, image=None),
    ], scryfall_uri=None)


def lookup_many(db_path: Path, names: list[str]) -> dict[str, CardData]:
    """Each name's card; a front-face name finds its two-faced card. Read only."""
    conn = sqlite3.connect(f"file:{db_path}?mode=ro", uri=True)
    try:
        found: dict[str, CardData] = {}
        for name in names:
            # "Name // " up to "Name /0" ('0' sorts right after '/'): two-faced cards whose front face is the name,
            # as a range so the name index answers it.
            row = conn.execute(_LOOKUP_SQL, (name, name + " // ", name + " /0", *NOT_DECK_CARDS, name)).fetchone()
            found[name] = card_from_raw(json.loads(row[0])) if row else _missing(name)
        return found
    finally:
        conn.close()
