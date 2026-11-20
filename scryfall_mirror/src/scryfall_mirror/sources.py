"""Pure projections of Scryfall bulk records into database rows. No I/O."""

import json
from dataclasses import dataclass
from dataclasses import field

PRICE_KEYS = ("usd", "usd_foil", "usd_etched", "eur", "eur_foil", "tix")


class Drop(Exception):
    """The record cannot be stored at all (no usable identity)."""


@dataclass
class ParsedCard:
    card: dict
    illustrations: list = field(default_factory=list)
    prices: tuple | None = None


def _faces(rec):
    faces = rec.get("card_faces")
    return faces if isinstance(faces, list) else []


def _first(rec, key):
    """Top-level value, or the first face's when the card keeps it on its faces."""
    if rec.get(key) is not None:
        return rec[key]
    for face in _faces(rec):
        if face.get(key) is not None:
            return face[key]
    return None


def _joined(rec, key, sep):
    if rec.get(key) is not None:
        return rec[key]
    values = [f.get(key) for f in _faces(rec)]
    return sep.join(v or "" for v in values) if any(v is not None for v in values) else None


def _image(images):
    return images.get("normal") if isinstance(images, dict) else None


def _number(rec, key, problem):
    value = rec.get(key)
    if value is None or (isinstance(value, (int, float)) and not isinstance(value, bool)):
        return value
    problem({"id": rec.get("id"), "field": key, "reason": f"expected number, got {type(value).__name__}", "sample": str(value)[:80]})
    return None


def _json(value):
    return None if value is None else json.dumps(value, ensure_ascii=False)


def parse_card(rec, problem):
    card_id = rec.get("id")
    if not isinstance(card_id, str) or not card_id:
        raise Drop("no id")
    faces = _faces(rec)
    image_back = _image(faces[1].get("image_uris")) if len(faces) > 1 and "image_uris" not in rec else None
    card = {
        "id": card_id,
        "oracle_id": _first(rec, "oracle_id"),
        "name": rec.get("name"),
        "lang": rec.get("lang"),
        "set_code": rec.get("set"),
        "collector_number": rec.get("collector_number"),
        "released_at": rec.get("released_at"),
        "layout": rec.get("layout"),
        "mana_cost": _joined(rec, "mana_cost", " // "),
        "cmc": _number(rec, "cmc", problem),
        "type_line": _joined(rec, "type_line", " // "),
        "oracle_text": _joined(rec, "oracle_text", "\n//\n"),
        "colors": _json(_first(rec, "colors")),
        "color_identity": _json(rec.get("color_identity")),
        "rarity": rec.get("rarity"),
        "image_normal": _image(rec.get("image_uris")) or (_image(faces[0].get("image_uris")) if faces else None),
        "image_back": image_back,
        "scryfall_uri": rec.get("scryfall_uri"),
        "raw": json.dumps(rec, ensure_ascii=False, separators=(",", ":")),
    }
    if rec.get("illustration_id"):
        illustrations = [(card_id, 0, rec["illustration_id"])]
    else:
        illustrations = [(card_id, i, f["illustration_id"]) for i, f in enumerate(faces) if f.get("illustration_id")]
    prices = rec.get("prices")
    price_row = (card_id, *(prices.get(k) for k in PRICE_KEYS)) if isinstance(prices, dict) else None
    return ParsedCard(card, illustrations, price_row)


def parse_ruling(rec, problem):
    if not rec.get("oracle_id"):
        raise Drop("no oracle_id")
    return (rec["oracle_id"], rec.get("source"), rec.get("published_at"), rec.get("comment"))


def parse_tag(rec, problem):
    """A tag row and its taggings: (tag_id, oracle_id or illustration_id, weight)."""
    if not rec.get("id"):
        raise Drop("no id")
    tag = {k: rec.get(k) for k in ("id", "label", "slug", "type", "uri", "description")}
    for key in ("parent_ids", "child_ids", "aliases"):
        tag[key] = _json(rec.get(key) or [])
    target = "oracle_id" if rec.get("type") == "oracle" else "illustration_id"
    taggings = []
    for t in rec.get("taggings") or []:
        if isinstance(t, dict) and t.get(target):
            taggings.append((rec["id"], t[target], t.get("weight")))
        else:
            problem({"id": rec["id"], "field": "taggings", "reason": f"tagging without {target}", "sample": str(t)[:80]})
    return tag, taggings
