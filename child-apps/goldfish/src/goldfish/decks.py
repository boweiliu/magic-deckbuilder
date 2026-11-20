"""The Deck List app's saved decks, read only (this app never writes there).

    // Commander
    1 Ulalek, Fused Atrocity
    // Deck
    1 Sol Ring
"""

import re
from pathlib import Path
from typing import NamedTuple

DECK_NAME = re.compile(r"^[a-z0-9][a-z0-9-]{0,59}$")
_LINE = re.compile(r"^\s*(\d+)\s*x?\s+(.+?)\s*$", re.IGNORECASE)


class Deck(NamedTuple):
    commander: tuple[str, ...]
    cards: tuple[str, ...]


def names(decks_dir: Path) -> list[str]:
    return sorted(p.stem for p in decks_dir.glob("*.txt") if DECK_NAME.match(p.stem))


def load(decks_dir: Path, name: str) -> Deck | None:
    """The deck with every copy listed once per copy (4 Island is four entries)."""
    if not DECK_NAME.match(name):
        return None
    path = decks_dir / f"{name}.txt"
    if not path.is_file():
        return None
    sections: dict[str, list[str]] = {"commander": [], "deck": []}
    current = "deck"
    for line in path.read_text().splitlines():
        stripped = line.strip()
        if stripped.startswith("//"):
            current = "commander" if "commander" in stripped.lower() else "deck"
            continue
        match = _LINE.match(stripped)
        if match:
            sections[current] += [match.group(2)] * int(match.group(1))
        elif stripped:
            sections[current].append(stripped)
    return Deck(tuple(sections["commander"]), tuple(sections["deck"]))
