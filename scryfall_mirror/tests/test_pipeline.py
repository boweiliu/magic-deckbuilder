"""Load scenarios over tiny synthetic snapshots, plus a spot check against the real download if present."""

import gzip
import json
import random
from pathlib import Path

import pytest

from scryfall_mirror import fetch
from scryfall_mirror import ingest
from scryfall_mirror import store
from scryfall_mirror.cli import DEFAULT_DATA


def card(cid, name, usd="1.00", **extra):
    return {"id": cid, "oracle_id": f"o-{cid}", "name": name, "layout": "normal", "cmc": 1.0, "prices": {"usd": usd}, **extra}


DAY1 = [card("a", "Alpha"), card("b", "Beta"), card("c", "Gamma")]
DAY2 = [card("a", "Alpha", usd="2.00"), card("b", "Beta (errata)")]  # c removed, a's price moved


def write(raw, kind, stamp, records, extra_lines=()):
    path = Path(raw, kind, f"{kind.replace('_', '-')}-{stamp}.jsonl.gz")
    path.parent.mkdir(parents=True, exist_ok=True)
    with gzip.open(path, "wt") as fh:
        for r in records:
            fh.write(json.dumps(r) + "\n")
        for line in extra_lines:
            fh.write(line + "\n")
    return path


def run(tmp_path, *steps, force=False):
    raw, db, log = tmp_path / "raw", tmp_path / "s.db", tmp_path / "s.db.ingest.jsonl"
    conn = store.connect(db)
    for stamp, records in steps:
        write(raw, "default_cards", stamp, records)
        ingest.load(conn, raw, log, kinds=("default_cards",), force=force, out=lambda s: None)
    return conn


def names(conn):
    return dict(conn.execute("SELECT id, name FROM cards ORDER BY id"))


SCENARIOS = [
    ("one day", [("20260101000000", DAY1)], {"a": "Alpha", "b": "Beta", "c": "Gamma"}, 3),
    ("newer day replaces and removes", [("20260101000000", DAY1), ("20260102000000", DAY2)], {"a": "Alpha", "b": "Beta (errata)"}, 4),
    ("same day twice is skipped", [("20260101000000", DAY1), ("20260101000000", DAY1)], {"a": "Alpha", "b": "Beta", "c": "Gamma"}, 3),
]


@pytest.mark.parametrize("name,steps,expected,price_rows", SCENARIOS, ids=[s[0] for s in SCENARIOS])
def test_scenarios(tmp_path, name, steps, expected, price_rows):
    conn = run(tmp_path, *steps)
    assert names(conn) == expected
    # prices: one row per card per snapshot where the price changed, so b's unchanged price is not repeated
    assert conn.execute("SELECT COUNT(*) FROM prices").fetchone()[0] == price_rows


def test_older_file_never_regresses_newer_load(tmp_path):
    conn = run(tmp_path, ("20260102000000", DAY2))
    raw = tmp_path / "raw"
    for p in fetch.raw_files(raw, "default_cards"):
        p.unlink()
    write(raw, "default_cards", "20260101000000", DAY1)
    ingest.load(conn, raw, tmp_path / "log", kinds=("default_cards",), out=lambda s: None)
    assert names(conn) == {"a": "Alpha", "b": "Beta (errata)"}


def test_force_reload_gives_same_rows(tmp_path):
    conn = run(tmp_path, ("20260101000000", DAY1))
    before = list(conn.execute("SELECT * FROM cards ORDER BY id"))
    ingest.load(conn, tmp_path / "raw", tmp_path / "log", kinds=("default_cards",), force=True, out=lambda s: None)
    assert list(conn.execute("SELECT * FROM cards ORDER BY id")) == before


def test_bad_lines_are_logged_and_left_out(tmp_path):
    raw, log = tmp_path / "raw", tmp_path / "log.jsonl"
    write(raw, "default_cards", "20260101000000", DAY1 * 4 + [{"name": "no id"}], extra_lines=["{not json", "[1, 2]"])
    conn = store.connect(tmp_path / "s.db")
    ingest.load(conn, raw, log, kinds=("default_cards",), out=lambda s: None)
    reasons = sorted(json.loads(line)["reason"] for line in log.read_text().splitlines())
    assert reasons[0].startswith("bad JSON") and reasons[1:] == ["dropped: no id", "not an object"]
    assert conn.execute("SELECT records_read, loaded, dropped FROM loads").fetchone() == (13, 12, 1)


def test_mostly_broken_file_stops_and_keeps_old_rows(tmp_path):
    conn = run(tmp_path, ("20260101000000", DAY1))
    write(tmp_path / "raw", "default_cards", "20260102000000", [{"name": "x"}] * 5 + [card("z", "Zed")])
    with pytest.raises(ingest.BrokenFile):
        ingest.load(conn, tmp_path / "raw", tmp_path / "log", kinds=("default_cards",), out=lambda s: None)
    assert names(conn) == {"a": "Alpha", "b": "Beta", "c": "Gamma"}
    assert conn.execute("SELECT COUNT(*) FROM loads").fetchone()[0] == 1


def test_tags_join_to_cards_through_faces(tmp_path):
    raw = tmp_path / "raw"
    dfc = {"id": "d", "name": "Front // Back", "layout": "transform", "oracle_id": "o-d",
           "card_faces": [{"illustration_id": "ill-front"}, {"illustration_id": "ill-back"}]}
    write(raw, "default_cards", "20260101000000", [dfc])
    write(raw, "oracle_tags", "20260101000000", [{"id": "t1", "slug": "removal", "type": "oracle", "child_ids": ["t2"], "taggings": [{"oracle_id": "o-d", "weight": "median"}]}])
    write(raw, "art_tags", "20260101000000", [{"id": "t3", "slug": "moon", "type": "illustration", "taggings": [{"illustration_id": "ill-back", "weight": "strong"}]}])
    conn = store.connect(tmp_path / "s.db")
    ingest.load(conn, raw, tmp_path / "log", kinds=("default_cards", "oracle_tags", "art_tags"), out=lambda s: None)
    assert conn.execute("SELECT c.name FROM cards c JOIN oracle_taggings t USING (oracle_id) JOIN tags g ON g.id = t.tag_id WHERE g.slug = 'removal'").fetchall() == [("Front // Back",)]
    assert conn.execute("SELECT i.card_id, i.face FROM art_taggings a JOIN card_illustrations i USING (illustration_id)").fetchall() == [("d", 1)]


def test_prune_keeps_newest(tmp_path):
    for day in range(1, 11):
        write(tmp_path, "rulings", f"202601{day:02d}000000", [])
    fetch.prune(tmp_path, "rulings", log=lambda s: None, keep=3)
    assert [p.name[8:16] for p in fetch.raw_files(tmp_path, "rulings")] == ["20260108", "20260109", "20260110"]


REAL_DB = DEFAULT_DATA / "scryfall.db"


@pytest.mark.skipif(not REAL_DB.exists(), reason="no real download loaded on this machine")
def test_spot_check_real_cards_match_raw_records():
    conn = store.connect(REAL_DB)
    raw_file = DEFAULT_DATA / "raw" / "default_cards" / conn.execute("SELECT file FROM loads WHERE kind='default_cards' ORDER BY snapshot DESC").fetchone()[0]
    with gzip.open(raw_file, "rt") as fh:
        sample = random.Random(7).sample([json.loads(line) for _, line in zip(range(3000), fh)], 20)
    for rec in sample:
        row = conn.execute("SELECT id, name, set_code, collector_number, released_at, rarity, scryfall_uri, raw FROM cards WHERE id = ?", (rec["id"],)).fetchone()
        assert row[:7] == (rec["id"], rec["name"], rec["set"], rec["collector_number"], rec["released_at"], rec["rarity"], rec["scryfall_uri"])
        assert json.loads(row[7]) == rec
