"""Deck changes are applied on the server one at a time, each committed to the decks history."""

import subprocess
import threading
from pathlib import Path

import pytest

from deck_list import history, runner

REAL_DB = Path("/home/user/workspace/data/.apps/deckbuilder/scryfall/scryfall.db")
pytestmark = pytest.mark.skipif(not REAL_DB.exists(), reason="needs the local Scryfall copy for card names")


@pytest.fixture
def client(tmp_path, monkeypatch):
    decks_dir = tmp_path / "decks"
    decks_dir.mkdir()
    subprocess.run(["git", "init", "-q", "-b", "main", str(decks_dir)], check=True)
    monkeypatch.setattr(runner, "DATA_DIR", tmp_path)
    monkeypatch.setattr(runner, "pusher", history.Pusher(decks_dir))  # no origin: pushes fail quietly and retry
    monkeypatch.setattr(runner, "card_index", runner.CardIndex(REAL_DB))
    monkeypatch.setattr(runner, "cycle_index", runner.CycleIndex(REAL_DB))
    return runner.app.test_client(), decks_dir


def log(decks_dir: Path) -> list[str]:
    out = subprocess.run(["git", "-C", str(decks_dir), "log", "--format=%s"], capture_output=True, text=True).stdout
    return out.splitlines()[::-1]


def test_changes_are_applied_one_at_a_time_and_each_is_a_commit(client):
    c, decks_dir = client
    assert c.post("/api/decks/t/add", json={"name": "sol ring"}).status_code == 200
    assert c.post("/api/decks/t/add", json={"name": "Island"}).status_code == 200
    assert c.post("/api/decks/t/add", json={"name": "Island"}).status_code == 200
    again = c.post("/api/decks/t/add", json={"name": "Sol Ring"})
    assert again.status_code == 409 and "already in the deck" in again.get_json()["detail"]
    after = c.post("/api/decks/t/remove", json={"name": "Island"}).get_json()
    assert [(e["name"], e["qty"]) for e in after["cards"]] == [("Sol Ring", 1), ("Island", 1)]
    assert (decks_dir / "t.txt").read_text() == "// Commander\n// Deck\n1 Sol Ring\n1 Island\n"
    assert log(decks_dir) == ["t: + Sol Ring", "t: + Island", "t: + Island (2)", "t: - Island"]
    assert after["version"] == subprocess.run(
        ["git", "-C", str(decks_dir), "rev-parse", "--short", "HEAD"], capture_output=True, text=True
    ).stdout.strip()


def test_two_windows_adding_at_once_keep_both_cards(client):
    c, decks_dir = client
    names = ["Sol Ring", "Arcane Signet", "Mind Stone", "Fellwar Stone", "Thought Vessel", "Wayfarer's Bauble"]
    threads = [threading.Thread(target=lambda n=n: runner.app.test_client().post("/api/decks/t/add", json={"name": n})) for n in names]
    for t in threads:
        t.start()
    for t in threads:
        t.join()
    saved = c.get("/api/decks/t").get_json()
    assert sorted(e["name"] for e in saved["cards"]) == sorted(names)
    assert len(log(decks_dir)) == len(names)


def test_new_ulalek_deck_starts_with_its_commander(client):
    c, _ = client
    deck = c.post("/api/decks/ulalek/add", json={"name": "Sol Ring"}).get_json()
    assert [e["name"] for e in deck["commander"]] == ["Ulalek, Fused Atrocity"]


def test_bad_deck_name_is_refused(client):
    c, _ = client
    assert c.post("/api/decks/..%2Fx/add", json={"name": "Sol Ring"}).status_code in (400, 404)
