"""Load the newest downloaded snapshot of each kind into the database, one transaction per file."""

import datetime
import gzip
import json
import re
from pathlib import Path

from scryfall_mirror import sources
from scryfall_mirror.fetch import KINDS
from scryfall_mirror.fetch import raw_files
from scryfall_mirror.sources import Drop

# A file losing more than this share of its records means Scryfall changed shape; stop rather than load it.
MAX_DROP_SHARE = 0.2
LOG_KEEP_DAYS = 60
TAG_TYPE = {"oracle_tags": "oracle", "art_tags": "illustration"}


class BrokenFile(Exception):
    pass


def snapshot_of(path):
    """Scryfall's timestamp in the file name, e.g. default-cards-20261111100645 -> 20261111100645."""
    match = re.search(r"(\d{14})", Path(path).name)
    return match.group(1) if match else "0"


def _records(path, problems):
    with gzip.open(path, "rt", encoding="utf-8") as fh:
        for lineno, line in enumerate(fh, 1):
            try:
                rec = json.loads(line)
            except json.JSONDecodeError as e:
                problems.append({"where": f"line {lineno}", "field": None, "reason": f"bad JSON: {e.msg}", "sample": line[:80]})
                continue
            if not isinstance(rec, dict):
                problems.append({"where": f"line {lineno}", "field": None, "reason": "not an object", "sample": line[:80]})
                continue
            yield rec


def _apply(conn, kind, path, problems):
    """Replace this kind's rows with the file's. Returns (read, loaded, dropped)."""
    read = loaded = 0
    problem = problems.append
    snapshot = snapshot_of(path)
    if kind == "default_cards":
        conn.execute("DELETE FROM cards")
        conn.execute("DELETE FROM card_illustrations")
        last_prices = {r[0]: tuple(r) for r in conn.execute(
            "SELECT p.card_id, p.usd, p.usd_foil, p.usd_etched, p.eur, p.eur_foil, p.tix FROM prices p"
            " JOIN (SELECT card_id, MAX(snapshot) s FROM prices GROUP BY card_id) m ON p.card_id = m.card_id AND p.snapshot = m.s")}
    elif kind == "oracle_cards":
        conn.execute("DELETE FROM oracle_cards")
    elif kind == "rulings":
        conn.execute("DELETE FROM rulings")
    else:
        conn.execute("DELETE FROM tags WHERE type = ?", (TAG_TYPE[kind],))
        conn.execute(f"DELETE FROM {'oracle_taggings' if kind == 'oracle_tags' else 'art_taggings'}")
    for rec in _records(path, problems):
        read += 1
        try:
            if kind == "default_cards":
                parsed = sources.parse_card(rec, problem)
                cols = list(parsed.card)
                conn.execute(f"INSERT OR REPLACE INTO cards ({','.join(cols)}) VALUES ({','.join('?' * len(cols))})", list(parsed.card.values()))
                conn.executemany("INSERT OR REPLACE INTO card_illustrations VALUES (?,?,?)", parsed.illustrations)
                if parsed.prices and last_prices.get(parsed.prices[0]) != parsed.prices:
                    conn.execute("INSERT OR REPLACE INTO prices VALUES (?,?,?,?,?,?,?,?)", (parsed.prices[0], snapshot, *parsed.prices[1:]))
            elif kind == "oracle_cards":
                if not rec.get("oracle_id") and not any(f.get("oracle_id") for f in rec.get("card_faces") or []):
                    raise Drop("no oracle_id")
                card = sources.parse_card(rec, problem).card
                conn.execute("INSERT OR REPLACE INTO oracle_cards VALUES (?,?,?)", (card["oracle_id"], card["id"], card["name"]))
            elif kind == "rulings":
                conn.execute("INSERT INTO rulings VALUES (?,?,?,?)", sources.parse_ruling(rec, problem))
            else:
                tag, taggings = sources.parse_tag(rec, problem)
                conn.execute(f"INSERT OR REPLACE INTO tags ({','.join(tag)}) VALUES ({','.join('?' * len(tag))})", list(tag.values()))
                table = "oracle_taggings" if kind == "oracle_tags" else "art_taggings"
                conn.executemany(f"INSERT OR REPLACE INTO {table} VALUES (?,?,?)", taggings)
            loaded += 1
        except Drop as e:
            problem({"where": rec.get("id") or rec.get("name"), "field": None, "reason": f"dropped: {e}", "sample": json.dumps(rec)[:80]})
    return read, loaded, read - loaded


def load(conn, raw_dir, log_path, kinds=KINDS, force=False, out=print):
    """Load each kind's newest raw file unless the ledger already has it (or a newer one)."""
    for kind in kinds:
        files = raw_files(raw_dir, kind)
        if not files:
            out(f"{kind}: nothing downloaded yet")
            continue
        path = files[-1]
        newest = conn.execute("SELECT MAX(snapshot) FROM loads WHERE kind = ?", (kind,)).fetchone()[0]
        if not force and newest is not None and newest >= snapshot_of(path):
            out(f"{kind}: {path.name} already loaded")
            continue
        problems = []
        conn.execute("BEGIN")
        try:
            read, loaded, dropped = _apply(conn, kind, path, problems)
            if read == 0 or dropped > MAX_DROP_SHARE * read:
                raise BrokenFile(f"{kind}: {path.name} dropped {dropped} of {read} records; see {log_path}")
            nulled = len(problems) - dropped - sum(1 for p in problems if p["reason"].startswith(("bad JSON", "not an object")))
            now = datetime.datetime.now(datetime.UTC).isoformat(timespec="seconds")
            conn.execute("INSERT OR REPLACE INTO loads VALUES (?,?,?,?,?,?,?,?)",
                         (kind, path.name, snapshot_of(path), read, loaded, dropped, nulled, now))
            conn.execute("COMMIT")
        except BaseException:
            conn.execute("ROLLBACK")
            raise
        finally:
            _write_log(log_path, kind, path.name, problems)
        out(f"{kind}: {path.name} read {read}, loaded {loaded}, dropped {dropped}, fields nulled {nulled}, log {log_path}")


def _write_log(log_path, kind, file, problems):
    """Append this load's problems and drop lines older than LOG_KEEP_DAYS."""
    log_path = Path(log_path)
    cutoff = (datetime.datetime.now(datetime.UTC) - datetime.timedelta(days=LOG_KEEP_DAYS)).isoformat()
    kept = []
    if log_path.exists():
        kept = [line for line in log_path.read_text().splitlines() if json.loads(line).get("at", "") >= cutoff]
    now = datetime.datetime.now(datetime.UTC).isoformat(timespec="seconds")
    kept += [json.dumps({"at": now, "kind": kind, "file": file, **p}) for p in problems]
    log_path.write_text("".join(line + "\n" for line in kept))
