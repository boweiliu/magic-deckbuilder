"""The SQLite database the Deckbuilder reads: schema and connection."""

import sqlite3
from pathlib import Path

SCHEMA = """
CREATE TABLE IF NOT EXISTS cards (
    id TEXT PRIMARY KEY, oracle_id TEXT, name TEXT, lang TEXT, set_code TEXT, collector_number TEXT,
    released_at TEXT, layout TEXT, mana_cost TEXT, cmc REAL, type_line TEXT, oracle_text TEXT,
    colors TEXT, color_identity TEXT, rarity TEXT, image_normal TEXT, image_back TEXT,
    scryfall_uri TEXT, raw TEXT NOT NULL
);
CREATE INDEX IF NOT EXISTS cards_oracle ON cards(oracle_id);
CREATE INDEX IF NOT EXISTS cards_name ON cards(name);
CREATE TABLE IF NOT EXISTS card_illustrations (card_id TEXT, face INTEGER, illustration_id TEXT, PRIMARY KEY (card_id, face));
CREATE INDEX IF NOT EXISTS card_illustrations_ill ON card_illustrations(illustration_id);
CREATE TABLE IF NOT EXISTS oracle_cards (oracle_id TEXT PRIMARY KEY, card_id TEXT, name TEXT);
CREATE TABLE IF NOT EXISTS rulings (oracle_id TEXT, source TEXT, published_at TEXT, comment TEXT);
CREATE INDEX IF NOT EXISTS rulings_oracle ON rulings(oracle_id);
CREATE TABLE IF NOT EXISTS tags (
    id TEXT PRIMARY KEY, label TEXT, slug TEXT, type TEXT, uri TEXT, description TEXT,
    parent_ids TEXT, child_ids TEXT, aliases TEXT
);
CREATE INDEX IF NOT EXISTS tags_slug ON tags(type, slug);
CREATE TABLE IF NOT EXISTS oracle_taggings (tag_id TEXT, oracle_id TEXT, weight TEXT, PRIMARY KEY (tag_id, oracle_id));
CREATE INDEX IF NOT EXISTS oracle_taggings_oracle ON oracle_taggings(oracle_id);
CREATE TABLE IF NOT EXISTS art_taggings (tag_id TEXT, illustration_id TEXT, weight TEXT, PRIMARY KEY (tag_id, illustration_id));
CREATE INDEX IF NOT EXISTS art_taggings_ill ON art_taggings(illustration_id);
-- One row per card per snapshot in which its prices changed.
CREATE TABLE IF NOT EXISTS prices (
    card_id TEXT, snapshot TEXT, usd TEXT, usd_foil TEXT, usd_etched TEXT, eur TEXT, eur_foil TEXT, tix TEXT,
    PRIMARY KEY (card_id, snapshot)
);
-- The ledger: every snapshot file applied, in the same transaction as its rows.
CREATE TABLE IF NOT EXISTS loads (
    kind TEXT, file TEXT, snapshot TEXT, records_read INTEGER, loaded INTEGER, dropped INTEGER, nulled INTEGER,
    loaded_at TEXT, PRIMARY KEY (kind, file)
);
"""


def connect(db_path):
    Path(db_path).parent.mkdir(parents=True, exist_ok=True)
    conn = sqlite3.connect(db_path, isolation_level=None)
    conn.execute("PRAGMA journal_mode=WAL")
    conn.execute("PRAGMA synchronous=NORMAL")
    conn.executescript(SCHEMA)
    return conn
