"""Auto-tag guesses tags from card types and Scryfall Tagger tags; adding them never removes or repeats a tag."""

import json
import sqlite3
import subprocess

import pytest

from deck_list import autotag, history, runner
from deck_list.cards import Card, normalize


def _card(name: str, type_line: str) -> Card:
    return Card(name=name, mana_cost="", mana_value=1.0, type_line=type_line, image=None, image_back=None, color_identity=(),
                scryfall_uri=None, commander_legality="legal", edhrec_rank=1, face_names=(), key=normalize(name), face_keys=())


CARDS = [_card("Sol Ring", "Artifact"), _card("Island", "Basic Land - Island"), _card("Arid Archway", "Land - Desert"),
         _card("Fell the Profane // Fell Mire", "Instant // Land"), _card("Demonic Tutor", "Sorcery")]


class SmallCardIndex:
    def __init__(self) -> None:
        self._by_key = {c.key: c for c in CARDS}

    def lookup(self, name: str) -> Card | None:
        return self._by_key.get(normalize(name))


class SmallTagger:
    def cards_with(self, roots, skip_prefixes=()):
        return frozenset({"ramp": {"Sol Ring"}, "tutor": {"Demonic Tutor"}}.get(roots[0], set()))


class NoCycles:
    def of(self, name: str) -> tuple:
        return ()


@pytest.fixture
def client(tmp_path, monkeypatch):
    decks_dir = tmp_path / "decks"
    decks_dir.mkdir()
    subprocess.run(["git", "init", "-q", "-b", "main", str(decks_dir)], check=True)
    monkeypatch.setattr(runner, "DATA_DIR", tmp_path)
    monkeypatch.setattr(runner, "pusher", history.Pusher(decks_dir))
    monkeypatch.setattr(runner, "card_index", SmallCardIndex())
    monkeypatch.setattr(runner, "cycle_index", NoCycles())
    monkeypatch.setattr(runner, "tagger_index", SmallTagger())
    c = runner.app.test_client()
    for card in CARDS:
        assert c.post("/api/decks/t/add", json={"name": card.name}).status_code == 200
    ids = {}
    for name in ("Lands", "Ramp", "Copyable mana", "Tutor"):
        ids[name] = c.post("/api/decks/t/categories/add", json={"name": name}).get_json()["categories"][-1]["id"]
    return c, decks_dir, ids


def test_rules_follow_category_names():
    rules = {name: autotag.rule_for(name) for name in ("lands", "draw or advantage", "spot interaction", "board wipes")}
    assert [(r.land, r.tagger_roots) if r else None for r in rules.values()] == [
        (True, ()), (False, ("card-advantage",)), (False, ("spot-removal", "counterspell")), (False, ("sweeper",))]
    assert autotag.rule_for("copyable mana") is None


def test_guesses_skip_cards_that_already_have_the_tag(client):
    c, _, ids = client
    c.post("/api/decks/t/tag", json={"card": "Island", "category": ids["Lands"], "on": True})
    found = c.get("/api/decks/t/autotag").get_json()
    by_cat = {g["category"]: g["cards"] for g in found["guesses"]}
    assert by_cat == {ids["Lands"]: ["Arid Archway", "Fell the Profane // Fell Mire"], ids["Ramp"]: ["Sol Ring"],
                      ids["Tutor"]: ["Demonic Tutor"]}
    assert found["no_rule"] == [ids["Copyable mana"]]


def test_adding_many_tags_is_one_change_that_keeps_existing_tags(client):
    c, decks_dir, ids = client
    c.post("/api/decks/t/tag", json={"card": "Sol Ring", "category": ids["Tutor"], "on": True})
    deck_text = (decks_dir / "t.txt").read_text()
    after = c.post("/api/decks/t/tags/add-many", json={"tags": [
        {"card": "Sol Ring", "category": ids["Ramp"]}, {"card": "arid archway", "category": ids["Lands"]},
        {"card": "Sol Ring", "category": ids["Tutor"]},  # already there: skipped
    ]}).get_json()
    assert after["tags"]["Sol Ring"] == [ids["Ramp"], ids["Tutor"]]
    assert after["tags"]["Arid Archway"] == [ids["Lands"]]
    assert (decks_dir / "t.txt").read_text() == deck_text
    last = subprocess.run(["git", "-C", str(decks_dir), "log", "-1", "--format=%s"], capture_output=True, text=True).stdout.strip()
    assert last == "t: auto-tag 2 tags (+Ramp x1, +Lands x1)"
    again = c.post("/api/decks/t/tags/add-many", json={"tags": [{"card": "Sol Ring", "category": ids["Ramp"]}]})
    assert again.status_code == 409
    missing = c.post("/api/decks/t/tags/add-many", json={"tags": [{"card": "Black Lotus", "category": ids["Ramp"]}]})
    assert missing.status_code == 409 and "not in the deck" in missing.get_json()["detail"]


def test_tagger_index_follows_tags_under_a_root_and_skips_excluded_ones(tmp_path):
    db = tmp_path / "s.db"
    conn = sqlite3.connect(db)
    conn.executescript("""
        CREATE TABLE tags (id TEXT, slug TEXT, type TEXT, child_ids TEXT);
        CREATE TABLE oracle_taggings (tag_id TEXT, oracle_id TEXT);
        CREATE TABLE oracle_cards (oracle_id TEXT, name TEXT);
    """)
    conn.executemany("INSERT INTO tags VALUES (?, ?, 'oracle', ?)", [
        ("1", "tutor", json.dumps(["2", "3"])), ("2", "tutor-creature", "[]"), ("3", "tutor-land", "[]")])
    conn.executemany("INSERT INTO oracle_taggings VALUES (?, ?)", [("1", "a"), ("2", "b"), ("3", "c")])
    conn.executemany("INSERT INTO oracle_cards VALUES (?, ?)", [("a", "Demonic Tutor"), ("b", "Chord of Calling"), ("c", "Farseek")])
    conn.commit()
    conn.close()
    index = autotag.TaggerIndex(db)
    assert index.cards_with(("tutor",)) == {"Demonic Tutor", "Chord of Calling", "Farseek"}
    assert index.cards_with(("tutor",), ("tutor-land",)) == {"Demonic Tutor", "Chord of Calling"}
    assert index.cards_with(("nope",)) == frozenset()
