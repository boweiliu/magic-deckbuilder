"""scryfall-mirror fetch | load | refresh | status"""

import argparse
import json
import sys
from pathlib import Path

from scryfall_mirror import fetch as fetch_mod
from scryfall_mirror import ingest
from scryfall_mirror import store

# src/scryfall_mirror/cli.py -> the workspace root is five levels up.
DEFAULT_DATA = Path(__file__).resolve().parents[5] / "data" / ".apps" / "deckbuilder" / "scryfall"
TABLES = ("cards", "oracle_cards", "card_illustrations", "rulings", "tags", "oracle_taggings", "art_taggings", "prices")


def paths(data_dir):
    data_dir = Path(data_dir)
    return data_dir / "raw", data_dir / "scryfall.db", data_dir / "scryfall.db.ingest.jsonl"


def status(data_dir):
    raw, db, log = paths(data_dir)
    conn = store.connect(db)
    loads = {}
    for kind, file, snapshot, read, loaded, dropped, nulled, at in conn.execute(
            "SELECT * FROM loads l WHERE snapshot = (SELECT MAX(snapshot) FROM loads WHERE kind = l.kind) ORDER BY kind"):
        loads[kind] = {"file": file, "snapshot": snapshot, "read": read, "loaded": loaded, "dropped": dropped, "nulled": nulled, "loaded_at": at}
    return {
        "database": str(db),
        "latest_loads": loads,
        "counts": {t: conn.execute(f"SELECT COUNT(*) FROM {t}").fetchone()[0] for t in TABLES},
        "raw_files": {k: [p.name for p in fetch_mod.raw_files(raw, k)] for k in fetch_mod.KINDS},
        "raw_megabytes": round(sum(p.stat().st_size for p in raw.rglob("*.gz")) / 1e6) if raw.exists() else 0,
        "problems_logged": sum(1 for _ in open(log)) if log.exists() else 0,
        "retention": f"raw files: newest {fetch_mod.RAW_KEEP} per kind; problem log: {ingest.LOG_KEEP_DAYS} days; "
                     "tables: replaced by each new snapshot; prices: kept forever, one row per change",
    }


def main(argv=None):
    parser = argparse.ArgumentParser(prog="scryfall-mirror", description=__doc__)
    parser.add_argument("--data-dir", default=str(DEFAULT_DATA))
    sub = parser.add_subparsers(dest="command", required=True)
    sub.add_parser("fetch", help="download the newest daily files")
    for name in ("load", "refresh"):
        p = sub.add_parser(name, help="load downloaded files" if name == "load" else "fetch, then load")
        p.add_argument("--force", action="store_true", help="reload files already in the ledger")
    sub.add_parser("status").add_argument("--json", action="store_true")
    args = parser.parse_args(argv)
    raw, db, log = paths(args.data_dir)

    if args.command in ("fetch", "refresh"):
        fetch_mod.fetch(raw)
    if args.command in ("load", "refresh"):
        try:
            ingest.load(store.connect(db), raw, log, force=args.force)
        except ingest.BrokenFile as e:
            print(f"stopped: {e}", file=sys.stderr)
            return 1
    if args.command == "status":
        s = status(args.data_dir)
        if args.json:
            print(json.dumps(s, indent=2))
        else:
            print(f"database: {s['database']}")
            for kind, info in s["latest_loads"].items():
                print(f"  {kind}: {info['file']} loaded {info['loaded']}/{info['read']}, dropped {info['dropped']}, nulled {info['nulled']} at {info['loaded_at']}")
            print("rows: " + ", ".join(f"{t} {n}" for t, n in s["counts"].items()))
            print(f"raw files: {s['raw_megabytes']} MB; problems logged: {s['problems_logged']}")
            print(f"retention: {s['retention']}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
