"""Groups: cards treated as one unit, saved beside the deck; a whole cycle can be added as a group."""

import json
import subprocess

import pytest

from deck_list import history, runner
from deck_list.cards import Card, Cycle, cycle_name, normalize

EGGS = ("Darkwater Egg", "Mossfire Egg", "Skycloud Egg")


def _card(name: str, type_line: str = "Artifact") -> Card:
    return Card(name=name, mana_cost="", mana_value=1.0, type_line=type_line, image=None, image_back=None, color_identity=(),
                scryfall_uri=None, commander_legality="legal", edhrec_rank=1, face_names=(), key=normalize(name), face_keys=())


class SmallCardIndex:
    def __init__(self) -> None:
        cards = [_card(n) for n in ("Sol Ring", "Mind Stone", "Ulalek, Fused Atrocity", *EGGS)]
        self._by_key = {c.key: c for c in cards}

    def lookup(self, name: str) -> Card | None:
        return self._by_key.get(normalize(name))


class SmallCycleIndex:
    def of(self, name: str) -> tuple[Cycle, ...]:
        # "Unprinted Egg" is in the cycle but not a known card, as happens with Tagger data.
        return (Cycle("cycle-ody-egg", "Egg (ODY)", (*EGGS, "Unprinted Egg")),) if name in EGGS else ()


@pytest.fixture
def client(tmp_path, monkeypatch):
    decks_dir = tmp_path / "decks"
    decks_dir.mkdir()
    subprocess.run(["git", "init", "-q", "-b", "main", str(decks_dir)], check=True)
    monkeypatch.setattr(runner, "DATA_DIR", tmp_path)
    monkeypatch.setattr(runner, "pusher", history.Pusher(decks_dir))
    monkeypatch.setattr(runner, "card_index", SmallCardIndex())
    monkeypatch.setattr(runner, "cycle_index", SmallCycleIndex())
    c = runner.app.test_client()
    for name in ("Sol Ring", "Mind Stone"):
        assert c.post("/api/decks/t/add", json={"name": name}).status_code == 200
    return c, decks_dir


def log(decks_dir) -> list[str]:
    out = subprocess.run(["git", "-C", str(decks_dir), "log", "--format=%s"], capture_output=True, text=True).stdout
    return out.splitlines()[::-1]


def test_cycle_names_read_well():
    sets = frozenset({"mh3", "ody", "lea"})
    assert cycle_name("cycle-mh3-landscape", sets) == "Landscape (MH3)"
    assert cycle_name("cycle-basic-snow-land", sets) == "Basic snow land"
    assert cycle_name("cycle-lea-basic-land", sets) == "Basic land (LEA)"


def test_a_cards_cycle_lists_its_known_cards(client):
    c, _ = client
    cycles = c.get("/api/cards/cycles?name=darkwater egg").get_json()["cycles"]
    assert [(x["name"], [card["name"] for card in x["cards"]]) for x in cycles] == [("Egg (ODY)", list(EGGS))]
    assert c.get("/api/cards/cycles?name=Sol Ring").get_json()["cycles"] == []
    suggested = c.get("/api/decks/t").get_json()["cards"]
    assert [e["card"]["cycles"] for e in suggested] == [0, 0]


def test_adding_a_cycle_adds_the_new_cards_and_groups_them_in_one_change(client):
    c, decks_dir = client
    c.post("/api/decks/t/add", json={"name": "Skycloud Egg"})
    deck = c.post("/api/decks/t/add-group", json={"cards": list(EGGS), "name": "Eggs"}).get_json()
    assert [e["name"] for e in deck["cards"]] == ["Sol Ring", "Mind Stone", "Skycloud Egg", "Darkwater Egg", "Mossfire Egg"]
    assert [(g["name"], g["cards"]) for g in deck["groups"]] == [("Eggs", list(EGGS))]
    assert log(decks_dir)[-1] == "t: + 2 cards (Darkwater Egg, Mossfire Egg), group + Eggs (3 cards)"
    assert subprocess.run(["git", "-C", str(decks_dir), "status", "--porcelain"], capture_output=True, text=True).stdout == ""
    saved = json.loads((decks_dir / "t.groups.json").read_text())
    assert saved["groups"][0]["cards"] == list(EGGS)


def test_grouping_moves_cards_out_of_their_old_group_and_small_groups_dissolve(client):
    c, _ = client
    c.post("/api/decks/t/add-group", json={"cards": list(EGGS), "name": "Eggs"})
    deck = c.post("/api/decks/t/groups/make", json={"cards": ["Sol Ring", "Darkwater Egg"], "name": "Mix"}).get_json()
    assert sorted((g["name"], tuple(g["cards"])) for g in deck["groups"]) == [
        ("Eggs", ("Mossfire Egg", "Skycloud Egg")), ("Mix", ("Sol Ring", "Darkwater Egg"))]
    deck = c.post("/api/decks/t/groups/make", json={"cards": ["Mind Stone", "Mossfire Egg"], "name": "Two"}).get_json()
    assert sorted(g["name"] for g in deck["groups"]) == ["Mix", "Two"]  # Eggs had one card left, so it dissolved
    assert c.post("/api/decks/t/groups/make", json={"cards": ["Sol Ring"]}).status_code == 409
    assert c.post("/api/decks/t/groups/make", json={"cards": ["Sol Ring", "Island"]}).status_code == 409


def test_groups_can_be_renamed_and_ungrouped_and_follow_removed_cards(client):
    c, _ = client
    deck = c.post("/api/decks/t/add-group", json={"cards": list(EGGS), "name": "Eggs"}).get_json()
    gid = deck["groups"][0]["id"]
    assert c.post("/api/decks/t/groups/rename", json={"id": gid, "name": "  ODY eggs "}).get_json()["groups"][0]["name"] == "ODY eggs"
    after_remove = c.post("/api/decks/t/remove", json={"name": "Skycloud Egg"}).get_json()
    assert after_remove["groups"][0]["cards"] == ["Darkwater Egg", "Mossfire Egg"]
    gone = c.post("/api/decks/t/remove", json={"name": "Mossfire Egg"}).get_json()
    assert gone["groups"] == []
    deck = c.post("/api/decks/t/add-group", json={"cards": list(EGGS), "name": "Eggs"}).get_json()
    ungrouped = c.post("/api/decks/t/groups/ungroup", json={"id": deck["groups"][0]["id"]}).get_json()
    assert ungrouped["groups"] == [] and len(ungrouped["cards"]) == 5
    assert c.post("/api/decks/t/groups/ungroup", json={"id": "nope"}).status_code == 409


def test_the_commander_is_not_added_or_grouped(client):
    c, _ = client
    c.post("/api/decks/t/add", json={"name": "Ulalek, Fused Atrocity", "as_commander": True})
    refused = c.post("/api/decks/t/add-group", json={"cards": ["Ulalek, Fused Atrocity"], "name": "X"})
    assert refused.status_code == 409


def test_the_grouped_page_is_served(client):
    c, _ = client
    page = c.get("/groups?deck=t").get_data(as_text=True)
    assert 'src="/static/groups.js"' in page and "<!--SHELL_PAGE_SCRIPT-->" not in page
    assert c.get("/static/groups.js").status_code == 200 and c.get("/static/groups.css").status_code == 200
