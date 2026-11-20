"""Download Scryfall's newest daily bulk files (compressed, never images) and prune old copies."""

import json
import os
import urllib.request
from pathlib import Path

BULK_INDEX = "https://api.scryfall.com/bulk-data"
# Scryfall asks every client to send a User-Agent and Accept header.
HEADERS = {"User-Agent": "Deckbuilder-scryfall-mirror/0.1", "Accept": "application/json"}
KINDS = ("default_cards", "oracle_cards", "rulings", "oracle_tags", "art_tags")
# Daily copies of the raw files kept per kind; disk is the limit (about 130 MB a day for all kinds).
RAW_KEEP = 7


def _get(url):
    return urllib.request.urlopen(urllib.request.Request(url, headers=HEADERS), timeout=120)


def raw_files(raw_dir, kind):
    """This kind's downloaded files, oldest first (Scryfall's names end in a sortable timestamp)."""
    return sorted(Path(raw_dir, kind).glob("*.jsonl.gz"))


def fetch(raw_dir, kinds=KINDS, log=print):
    with _get(BULK_INDEX) as resp:
        index = {d["type"]: d for d in json.load(resp)["data"]}
    for kind in kinds:
        url = index[kind]["jsonl_download_uri"]
        target = Path(raw_dir, kind, url.rsplit("/", 1)[1])
        if target.exists():
            log(f"{kind}: {target.name} already downloaded")
        else:
            target.parent.mkdir(parents=True, exist_ok=True)
            part = target.with_name(target.name + ".part")
            with _get(url) as resp, open(part, "wb") as out:
                while chunk := resp.read(1 << 20):
                    out.write(chunk)
            os.replace(part, target)
            log(f"{kind}: downloaded {target.name} ({target.stat().st_size // 1_000_000} MB)")
        prune(raw_dir, kind, log)


def prune(raw_dir, kind, log=print, keep=RAW_KEEP):
    for old in raw_files(raw_dir, kind)[:-keep]:
        old.unlink()
        log(f"{kind}: removed old copy {old.name}")
