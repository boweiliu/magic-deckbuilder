"""Groups of cards a person treats as one unit (a cycle, a package), saved next to the deck as ``<deck>.groups.json``.

    {"groups": [{"id": "egg-3f2a", "name": "Egg (ODY)", "cards": ["Darkwater Egg", ...]}]}

Like the tags, groups live in their own file so the plain deck file other apps read is untouched,
and every change is one commit. A card is in at most one group, and a group holds at least two
cards: grouping cards takes them out of the groups they were in, and a group left with fewer
than two cards dissolves.
"""

import json
import os
import re
import secrets
from typing import NamedTuple
from pathlib import Path

from deck_list.decks import path_for

MAX_NAME = 40


class Group(NamedTuple):
    id: str
    name: str
    cards: tuple[str, ...]


def groups_path(decks_dir: Path, deck: str) -> Path:
    return path_for(decks_dir, deck).with_suffix(".groups.json")


def load(decks_dir: Path, deck: str) -> tuple[Group, ...]:
    path = groups_path(decks_dir, deck)
    if not path.exists():
        return ()
    raw = json.loads(path.read_text())
    return tuple(Group(g["id"], g["name"], tuple(g["cards"])) for g in raw.get("groups", []))


def save(decks_dir: Path, deck: str, groups: tuple[Group, ...]) -> Path:
    path = groups_path(decks_dir, deck)
    tmp = path.with_name(path.name + ".tmp")
    tmp.write_text(json.dumps({"groups": [g._asdict() for g in groups]}, indent=1, ensure_ascii=False) + "\n")
    os.replace(tmp, path)
    return path


def in_deck(groups: tuple[Group, ...], cards: set[str]) -> tuple[Group, ...]:
    """The groups as they apply to the deck now: cards no longer in it are left out, small groups dissolve."""
    kept = (Group(g.id, g.name, tuple(c for c in g.cards if c in cards)) for g in groups)
    return tuple(g for g in kept if len(g.cards) >= 2)


# Each change returns the new groups and a commit message, or None and why it was refused.
Result = tuple[tuple[Group, ...] | None, str]


def _clean_name(name: str) -> str:
    return " ".join(name.split())[:MAX_NAME]


def make(groups: tuple[Group, ...], cards: list[str], name: str) -> Result:
    """One new group of these cards, which leave any group they were in."""
    members = tuple(dict.fromkeys(cards))
    name = _clean_name(name) or "Group"
    if len(members) < 2:
        return None, "a group needs at least two cards"
    taken = set(members)
    rest = _big_enough(tuple(Group(g.id, g.name, tuple(c for c in g.cards if c not in taken)) for g in groups))
    slug = re.sub(r"[^a-z0-9]+", "-", name.lower()).strip("-")[:20] or "group"
    group = Group(f"{slug}-{secrets.token_hex(2)}", name, members)
    return rest + (group,), f"group + {name} ({len(members)} cards)"


def _big_enough(groups: tuple[Group, ...]) -> tuple[Group, ...]:
    return tuple(g for g in groups if len(g.cards) >= 2)


def ungroup(groups: tuple[Group, ...], group_id: str) -> Result:
    group = next((g for g in groups if g.id == group_id), None)
    if group is None:
        return None, "that group no longer exists"
    return tuple(g for g in groups if g.id != group_id), f"group - {group.name}"


def rename(groups: tuple[Group, ...], group_id: str, name: str) -> Result:
    group = next((g for g in groups if g.id == group_id), None)
    if group is None:
        return None, "that group no longer exists"
    name = _clean_name(name)
    if not name:
        return None, "a group needs a name"
    if name == group.name:
        return None, "nothing changed"
    return tuple(Group(g.id, name, g.cards) if g.id == group_id else g for g in groups), f"group {group.name} renamed {name}"
