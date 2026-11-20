"""Categories and tags are changed one at a time on the server, saved beside the deck, and each change is a commit."""

import json
import subprocess
from pathlib import Path

import pytest

from deck_list import history, runner
from deck_list.cards import Card, normalize


def _card(name: str, type_line: str) -> Card:
    return Card(name=name, mana_cost="", mana_value=1.0, type_line=type_line, image=None, image_back=None, color_identity=(),
                scryfall_uri=None, commander_legality="legal", edhrec_rank=1, face_names=(), key=normalize(name), face_keys=())


class SmallCardIndex:
    """A handful of real card names, so these checks don't load the whole Scryfall copy."""

    def __init__(self) -> None:
        cards = [_card("Sol Ring", "Artifact"), _card("Mind Stone", "Artifact"), _card("Island", "Basic Land - Island")]
        self._by_key = {c.key: c for c in cards}

    def lookup(self, name: str) -> Card | None:
        return self._by_key.get(normalize(name))


class NoCycles:
    def of(self, name: str) -> tuple:
        return ()


@pytest.fixture
def client(tmp_path, monkeypatch):
    decks_dir = tmp_path / "decks"
    decks_dir.mkdir()
    subprocess.run(["git", "init", "-q", "-b", "main", str(decks_dir)], check=True)
    monkeypatch.setattr(runner, "DATA_DIR", tmp_path)
    monkeypatch.setattr(runner, "pusher", history.Pusher(decks_dir))  # no origin: pushes fail quietly and retry
    monkeypatch.setattr(runner, "card_index", SmallCardIndex())
    monkeypatch.setattr(runner, "cycle_index", NoCycles())
    c = runner.app.test_client()
    for name in ("Sol Ring", "Mind Stone"):
        assert c.post("/api/decks/t/add", json={"name": name}).status_code == 200
    for name in ("Ramp", "Draw", "Fixing"):
        assert c.post("/api/decks/t/categories/add", json={"name": name}).status_code == 200
    return c, decks_dir


def cat_id(c, name: str) -> str:
    return next(cat["id"] for cat in c.get("/api/decks/t").get_json()["categories"] if cat["name"] == name)


def log(decks_dir: Path) -> list[str]:
    out = subprocess.run(["git", "-C", str(decks_dir), "log", "--format=%s"], capture_output=True, text=True).stdout
    return out.splitlines()[::-1]


def test_a_new_deck_has_no_categories_or_tags(client):
    c, decks_dir = client
    deck = c.post("/api/decks/fresh/add", json={"name": "Sol Ring"}).get_json()
    assert deck["categories"] == [] and deck["tags"] == {}
    assert not (decks_dir / "fresh.tags.json").exists()


def test_tagging_saves_beside_the_deck_and_commits_each_change(client):
    c, decks_dir = client
    ramp, draw = cat_id(c, "Ramp"), cat_id(c, "Draw")
    deck_text = (decks_dir / "t.txt").read_text()
    c.post("/api/decks/t/tag", json={"card": "sol ring", "category": draw, "on": True})
    after = c.post("/api/decks/t/tag", json={"card": "Sol Ring", "category": ramp, "on": True}).get_json()
    assert after["tags"] == {"Sol Ring": [ramp, draw]}  # in category order, not tagging order
    assert (decks_dir / "t.txt").read_text() == deck_text  # the deck file other apps read is untouched
    saved = json.loads((decks_dir / "t.tags.json").read_text())
    assert saved["tags"] == {"Sol Ring": [ramp, draw]}
    off = c.post("/api/decks/t/tag", json={"card": "Sol Ring", "category": draw, "on": False}).get_json()
    assert off["tags"] == {"Sol Ring": [ramp]}
    assert log(decks_dir)[-3:] == ["t: tag Sol Ring +Draw", "t: tag Sol Ring +Ramp", "t: tag Sol Ring -Draw"]


def test_tagging_a_card_that_is_not_in_the_deck_or_twice_is_refused(client):
    c, _ = client
    ramp, draw = cat_id(c, "Ramp"), cat_id(c, "Draw")
    missing = c.post("/api/decks/t/tag", json={"card": "Island", "category": ramp, "on": True})
    assert missing.status_code == 409 and "not in the deck" in missing.get_json()["detail"]
    c.post("/api/decks/t/tag", json={"card": "Sol Ring", "category": ramp, "on": True})
    twice = c.post("/api/decks/t/tag", json={"card": "Sol Ring", "category": ramp, "on": True})
    assert twice.status_code == 409 and "already has Ramp" in twice.get_json()["detail"]
    gone = c.post("/api/decks/t/tag", json={"card": "Sol Ring", "category": "nope", "on": True})
    assert gone.status_code == 409


def test_categories_can_be_added_renamed_recoloured_moved_and_deleted(client):
    c, decks_dir = client
    ramp, draw = cat_id(c, "Ramp"), cat_id(c, "Draw")
    added = c.post("/api/decks/t/categories/add", json={"name": "  Protection  "}).get_json()
    new = added["categories"][-1]
    assert new["name"] == "Protection" and new["color"].startswith("#")
    assert c.post("/api/decks/t/categories/add", json={"name": "protection"}).status_code == 409
    assert c.post("/api/decks/t/categories/add", json={"name": "X", "color": "red"}).status_code == 409
    renamed = c.post("/api/decks/t/categories/edit", json={"id": new["id"], "name": "Shields"}).get_json()
    assert renamed["categories"][-1]["name"] == "Shields"
    recoloured = c.post("/api/decks/t/categories/edit", json={"id": new["id"], "color": "#123456"}).get_json()
    assert recoloured["categories"][-1]["color"] == "#123456"
    moved = c.post("/api/decks/t/categories/move", json={"id": new["id"], "position": 0}).get_json()
    assert moved["categories"][0]["id"] == new["id"]
    c.post("/api/decks/t/tag", json={"card": "Mind Stone", "category": ramp, "on": True})
    c.post("/api/decks/t/tag", json={"card": "Mind Stone", "category": draw, "on": True})
    deleted = c.post("/api/decks/t/categories/delete", json={"id": ramp}).get_json()
    assert ramp not in [cat["id"] for cat in deleted["categories"]]
    assert deleted["tags"] == {"Mind Stone": [draw]}
    assert log(decks_dir)[-1] == "t: category - Ramp"


def test_tags_need_the_deck_to_exist(client):
    c, _ = client
    ramp, draw = cat_id(c, "Ramp"), cat_id(c, "Draw")
    assert c.post("/api/decks/empty/tag", json={"card": "Sol Ring", "category": ramp, "on": True}).status_code == 409


def test_the_page_carries_the_deck_so_it_shows_without_waiting(client):
    c, _ = client
    ramp = cat_id(c, "Ramp")
    c.post("/api/decks/t/tag", json={"card": "Sol Ring", "category": ramp, "on": True})
    page = c.get("/?deck=t").get_data(as_text=True)
    start = page.index('<script type="application/json" id="deck-data">') + len('<script type="application/json" id="deck-data">')
    data = json.loads(page[start:page.index("</script>", start)])
    assert [e["name"] for e in data["cards"]] == ["Sol Ring", "Mind Stone"]
    assert data["tags"] == {"Sol Ring": [ramp]}
    assert "<!--DECK_DATA-->" not in page


def test_a_bad_deck_name_still_serves_the_page(client):
    c, _ = client
    page = c.get("/?deck=../x")
    assert page.status_code == 200 and 'id="deck-data"' not in page.get_data(as_text=True)


def test_decks_are_listed_with_their_card_counts(client):
    c, _ = client
    c.post("/api/decks/b-list/add", json={"name": "Island"})
    c.post("/api/decks/b-list/add", json={"name": "Island"})
    assert c.get("/api/decks").get_json()["decks"] == [{"name": "b-list", "cards": 2}, {"name": "t", "cards": 2}]


def test_a_new_deck_can_start_with_another_decks_commander_and_categories_but_no_cards(client):
    c, decks_dir = client
    c.post("/api/decks/t/add", json={"name": "Island", "as_commander": True})
    c.post("/api/decks/t/tag", json={"card": "Sol Ring", "category": cat_id(c, "Ramp"), "on": True})
    pool_files = {p.name: p.read_text() for p in decks_dir.glob("t.*")}
    new = c.post("/api/decks/the-99/create", json={"copy_from": "t"}).get_json()
    assert [e["name"] for e in new["commander"]] == ["Island"]
    assert new["cards"] == [] and new["tags"] == {}
    assert [cat["name"] for cat in new["categories"]] == ["Ramp", "Draw", "Fixing"]
    assert {p.name: p.read_text() for p in decks_dir.glob("t.*")} == pool_files  # the source deck is untouched
    assert log(decks_dir)[-2:] == ["the-99: new deck from t", "the-99: categories from t"]
    again = c.post("/api/decks/the-99/create", json={})
    assert again.status_code == 409 and "already" in again.get_json()["detail"]
    assert (decks_dir / "the-99.txt").read_text() == "// Commander\n1 Island\n// Deck\n"


def test_a_new_deck_can_start_blank(client):
    c, decks_dir = client
    blank = c.post("/api/decks/scratch/create", json={"copy_from": None}).get_json()
    assert blank["commander"] == [] and blank["cards"] == [] and blank["categories"] == []
    assert not (decks_dir / "scratch.tags.json").exists()
    assert c.post("/api/decks/x/create", json={"copy_from": "nope"}).status_code == 404
    assert c.post("/api/decks/Bad Name/create", json={}).status_code == 400
