"""Deck lists as plain text files, one card per line, the format Moxfield and Archidekt import.

    // Commander
    1 Ulalek, Fused Atrocity
    // Deck
    1 Sol Ring
"""

import os
import re
from typing import NamedTuple
from pathlib import Path

DECK_NAME = re.compile(r"^[a-z0-9][a-z0-9-]{0,59}$")


class DeckNameError(ValueError):
    """A deck name that is not lowercase letters, digits and hyphens."""


_LINE = re.compile(r"^\s*(\d+)\s*x?\s+(.+?)\s*$", re.IGNORECASE)


class Entry(NamedTuple):
    name: str
    qty: int


class Deck(NamedTuple):
    commander: tuple[Entry, ...]
    cards: tuple[Entry, ...]


def parse(text: str) -> Deck:
    sections: dict[str, list[Entry]] = {"commander": [], "deck": []}
    current = "deck"
    for line in text.splitlines():
        stripped = line.strip()
        if stripped.startswith("//"):
            current = "commander" if "commander" in stripped.lower() else "deck"
            continue
        match = _LINE.match(stripped)
        if match:
            sections[current].append(Entry(match.group(2), int(match.group(1))))
        elif stripped:
            sections[current].append(Entry(stripped, 1))
    return Deck(tuple(sections["commander"]), tuple(sections["deck"]))


def render(deck: Deck) -> str:
    lines = ["// Commander", *(f"{e.qty} {e.name}" for e in deck.commander), "// Deck"]
    lines += [f"{e.qty} {e.name}" for e in deck.cards]
    return "\n".join(lines) + "\n"


def path_for(decks_dir: Path, name: str) -> Path:
    if not DECK_NAME.match(name):
        raise DeckNameError(f"deck names are lowercase letters, digits and hyphens: {name!r}")
    return decks_dir / f"{name}.txt"


def names(decks_dir: Path) -> list[str]:
    """Every saved deck's name, alphabetically."""
    return sorted(p.stem for p in decks_dir.glob("*.txt") if DECK_NAME.match(p.stem))


def load(decks_dir: Path, name: str) -> Deck | None:
    path = path_for(decks_dir, name)
    return parse(path.read_text()) if path.exists() else None


def save(decks_dir: Path, name: str, deck: Deck) -> None:
    path = path_for(decks_dir, name)
    path.parent.mkdir(parents=True, exist_ok=True)
    tmp = path.with_name(path.name + ".tmp")
    tmp.write_text(render(deck))
    os.replace(tmp, path)
