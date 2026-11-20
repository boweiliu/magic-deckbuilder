"""One table of raw Scryfall records and the projected fields each must produce."""

import pytest

from scryfall_mirror import sources
from scryfall_mirror.sources import Drop

IMG = {"normal": "https://cards.scryfall.io/normal/front/a.jpg", "small": "https://cards.scryfall.io/small/front/a.jpg"}
IMG_B = {"normal": "https://cards.scryfall.io/normal/back/a.jpg"}

NORMAL = {
    "id": "c1", "oracle_id": "o1", "name": "Lightning Bolt", "lang": "en", "set": "lea", "collector_number": "161",
    "released_at": "1993-08-05", "layout": "normal", "mana_cost": "{R}", "cmc": 1.0, "type_line": "Instant",
    "oracle_text": "Lightning Bolt deals 3 damage to any target.", "colors": ["R"], "color_identity": ["R"],
    "rarity": "common", "image_uris": IMG, "illustration_id": "i1", "scryfall_uri": "https://scryfall.com/card/lea/161",
    "prices": {"usd": "1.50", "usd_foil": None, "eur": "1.20", "tix": "0.02"},
}
TRANSFORM = {
    "id": "c2", "oracle_id": "o2", "name": "Delver of Secrets // Insectile Aberration", "layout": "transform",
    "cmc": 1.0, "type_line": "Creature — Human Wizard // Creature — Human Insect",
    "card_faces": [
        {"name": "Delver of Secrets", "mana_cost": "{U}", "oracle_text": "At the beginning...", "image_uris": IMG, "illustration_id": "i2a"},
        {"name": "Insectile Aberration", "mana_cost": "", "oracle_text": "Flying", "image_uris": IMG_B, "illustration_id": "i2b"},
    ],
}
REVERSIBLE = {
    "id": "c3", "name": "Zndrsplt // Zndrsplt", "layout": "reversible_card",
    "card_faces": [{"name": "Zndrsplt", "oracle_id": "o3", "cmc": 3.0, "image_uris": IMG}, {"name": "Zndrsplt", "oracle_id": "o3", "image_uris": IMG_B}],
}
SPLIT = {
    "id": "c4", "oracle_id": "o4", "name": "Fire // Ice", "layout": "split", "mana_cost": "{1}{R} // {1}{U}", "cmc": 4.0,
    "image_uris": IMG, "card_faces": [{"name": "Fire", "mana_cost": "{1}{R}", "oracle_text": "Fire deals 2"}, {"name": "Ice", "mana_cost": "{1}{U}", "oracle_text": "Tap target"}],
}

CARD_CASES = [
    ("normal card keeps scalars as-is", NORMAL, {"id": "c1", "oracle_id": "o1", "cmc": 1.0, "set_code": "lea", "mana_cost": "{R}", "image_normal": IMG["normal"], "image_back": None}),
    ("transform takes front image, back image, joined faces", TRANSFORM, {"oracle_id": "o2", "mana_cost": "{U} // ", "image_normal": IMG["normal"], "image_back": IMG_B["normal"], "oracle_text": "At the beginning...\n//\nFlying"}),
    ("reversible card has oracle_id and cmc only on faces", REVERSIBLE, {"oracle_id": "o3", "cmc": None, "image_normal": IMG["normal"]}),
    ("split card keeps top-level mana cost and image", SPLIT, {"mana_cost": "{1}{R} // {1}{U}", "image_normal": IMG["normal"], "image_back": None}),
    ("colors stored as JSON text", NORMAL, {"colors": '["R"]', "color_identity": '["R"]'}),
    ("missing optional fields become null", {"id": "c5", "name": "Odd", "layout": "normal"}, {"oracle_id": None, "released_at": None, "image_normal": None}),
    ("cmc of wrong type is nulled", {**NORMAL, "cmc": "one"}, {"cmc": None}),
]


@pytest.mark.parametrize("name,record,expected", CARD_CASES, ids=[c[0] for c in CARD_CASES])
def test_parse_card(name, record, expected):
    problems = []
    parsed = sources.parse_card(record, problems.append)
    for field, value in expected.items():
        assert parsed.card[field] == value, field


def test_wrong_type_is_logged_not_raised():
    problems = []
    sources.parse_card({**NORMAL, "cmc": "one"}, problems.append)
    assert [(p["field"], p["reason"]) for p in problems] == [("cmc", "expected number, got str")]


def test_card_without_id_is_dropped():
    with pytest.raises(Drop):
        sources.parse_card({"name": "No id"}, lambda p: None)


def test_illustrations_come_from_top_level_or_faces():
    assert sources.parse_card(NORMAL, lambda p: None).illustrations == [("c1", 0, "i1")]
    assert sources.parse_card(TRANSFORM, lambda p: None).illustrations == [("c2", 0, "i2a"), ("c2", 1, "i2b")]


def test_prices_keep_strings_as_given():
    assert sources.parse_card(NORMAL, lambda p: None).prices == ("c1", "1.50", None, None, "1.20", None, "0.02")


def test_tag_parse_keeps_tree_and_weights():
    rec = {"id": "t1", "label": "removal", "slug": "removal", "type": "oracle", "uri": "u", "description": None,
           "parent_ids": [], "child_ids": ["t2"], "aliases": ["kill"],
           "taggings": [{"oracle_id": "o1", "weight": "median"}]}
    tag, taggings = sources.parse_tag(rec, lambda p: None)
    assert tag["child_ids"] == '["t2"]' and tag["aliases"] == '["kill"]'
    assert taggings == [("t1", "o1", "median")]
