"""A deck's categories and which cards carry them, saved next to the deck as ``<deck>.tags.json``.

The deck list file itself stays plain Moxfield text (other apps read it), so tags live in their
own file in the same folder and get the same treatment: changed one at a time on the server,
each change a commit, pushed to GitHub.

    {"categories": [{"id": "ramp", "name": "Ramp", "color": "#2f6b3a"}, ...],
     "tags": {"Sol Ring": ["ramp"], ...}}

The order of ``categories`` is the order of the drag zones, clockwise from the top.
"""

import json
import os
import re
import secrets
from typing import NamedTuple
from pathlib import Path

from deck_list.decks import path_for

COLOR = re.compile(r"^#[0-9a-fA-F]{6}$")
MAX_NAME = 24

PALETTE = (
    "#2f6b3a", "#2c5a86", "#8a6a12", "#5b3a7a", "#a33b2a", "#6b5a48",
    "#1f6b6b", "#7a2f55", "#3d4f9a", "#b0601e", "#4f6b2a", "#555555",
)


class Category(NamedTuple):
    id: str
    name: str
    color: str


class Tags(NamedTuple):
    categories: tuple[Category, ...]
    tags: dict[str, tuple[str, ...]]


# A deck starts with no categories; the person adds their own on the Categories page.
STARTING = Tags(categories=(), tags={})


def tags_path(decks_dir: Path, deck: str) -> Path:
    return path_for(decks_dir, deck).with_suffix(".tags.json")


def load(decks_dir: Path, deck: str) -> Tags:
    path = tags_path(decks_dir, deck)
    if not path.exists():
        return STARTING
    raw = json.loads(path.read_text())
    return Tags(
        categories=tuple(Category(c["id"], c["name"], c["color"]) for c in raw.get("categories", [])),
        tags={card: tuple(ids) for card, ids in raw.get("tags", {}).items()},
    )


def save(decks_dir: Path, deck: str, tags: Tags) -> Path:
    path = tags_path(decks_dir, deck)
    known = {c.id for c in tags.categories}
    body = {
        "categories": [c._asdict() for c in tags.categories],
        "tags": {card: [i for i in ids if i in known] for card, ids in sorted(tags.tags.items()) if any(i in known for i in ids)},
    }
    tmp = path.with_name(path.name + ".tmp")
    tmp.write_text(json.dumps(body, indent=1, ensure_ascii=False) + "\n")
    os.replace(tmp, path)
    return path


# Each change returns the new tags and a commit message, or None and why it was refused.
Result = tuple[Tags | None, str]


def _find(tags: Tags, category_id: str) -> Category | None:
    return next((c for c in tags.categories if c.id == category_id), None)


def set_tag(tags: Tags, card: str, category_id: str, on: bool) -> Result:
    category = _find(tags, category_id)
    if category is None:
        return None, "that category no longer exists"
    current = tags.tags.get(card, ())
    if (category_id in current) == on:
        return None, f"{card} {'already has' if on else 'does not have'} {category.name}"
    wanted = set(current) | {category_id} if on else set(current) - {category_id}
    ordered = tuple(c.id for c in tags.categories if c.id in wanted)
    return Tags(tags.categories, {**tags.tags, card: ordered}), f"tag {card} {'+' if on else '-'}{category.name}"


def add_many(tags: Tags, pairs: list[tuple[str, str]]) -> Result:
    """Put several (card, category id) tags on at once; ones a card already carries are skipped, nothing is taken off."""
    known = {c.id: c for c in tags.categories}
    updated = {card: set(ids) for card, ids in tags.tags.items()}
    added: dict[str, int] = {}
    for card, category_id in pairs:
        if category_id not in known:
            return None, "that category no longer exists"
        if category_id in updated.setdefault(card, set()):
            continue
        updated[card].add(category_id)
        added[category_id] = added.get(category_id, 0) + 1
    if not added:
        return None, "those cards already have those tags"
    ordered = {card: tuple(c.id for c in tags.categories if c.id in ids) for card, ids in updated.items() if ids}
    summary = ", ".join(f"+{known[i].name} x{n}" for i, n in added.items())
    return Tags(tags.categories, ordered), f"auto-tag {sum(added.values())} tags ({summary})"


def _clean_name(name: str) -> str:
    return " ".join(name.split())[:MAX_NAME]


def add_category(tags: Tags, name: str, color: str | None) -> Result:
    name = _clean_name(name)
    if not name:
        return None, "a category needs a name"
    if any(c.name.lower() == name.lower() for c in tags.categories):
        return None, f"there is already a category called {name}"
    if color is not None and not COLOR.match(color):
        return None, f"{color} is not a colour"
    used = {c.color for c in tags.categories}
    chosen = color or next((c for c in PALETTE if c not in used), PALETTE[len(tags.categories) % len(PALETTE)])
    slug = re.sub(r"[^a-z0-9]+", "-", name.lower()).strip("-")[:20] or "category"
    category = Category(f"{slug}-{secrets.token_hex(2)}", name, chosen)
    return Tags(tags.categories + (category,), tags.tags), f"category + {name}"


def edit_category(tags: Tags, category_id: str, name: str | None, color: str | None) -> Result:
    category = _find(tags, category_id)
    if category is None:
        return None, "that category no longer exists"
    new_name = _clean_name(name) if name is not None else category.name
    if not new_name:
        return None, "a category needs a name"
    if any(c.id != category_id and c.name.lower() == new_name.lower() for c in tags.categories):
        return None, f"there is already a category called {new_name}"
    if color is not None and not COLOR.match(color):
        return None, f"{color} is not a colour"
    updated = Category(category.id, new_name, color or category.color)
    if updated == category:
        return None, "nothing changed"
    what = f"{category.name} renamed {new_name}" if new_name != category.name else f"{category.name} colour {updated.color}"
    return Tags(tuple(updated if c.id == category_id else c for c in tags.categories), tags.tags), f"category {what}"


def move_category(tags: Tags, category_id: str, position: int) -> Result:
    category = _find(tags, category_id)
    if category is None:
        return None, "that category no longer exists"
    rest = [c for c in tags.categories if c.id != category_id]
    position = max(0, min(position, len(rest)))
    if tags.categories.index(category) == position:
        return None, "nothing changed"
    rest.insert(position, category)
    return Tags(tuple(rest), tags.tags), f"category {category.name} moved to {position + 1}"


def delete_category(tags: Tags, category_id: str) -> Result:
    category = _find(tags, category_id)
    if category is None:
        return None, "that category no longer exists"
    remaining = {card: tuple(i for i in ids if i != category_id) for card, ids in tags.tags.items()}
    return Tags(tuple(c for c in tags.categories if c.id != category_id), remaining), f"category - {category.name}"
