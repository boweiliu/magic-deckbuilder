# scryfall-mirror

Downloads Scryfall's daily bulk files (cards in every English printing, Scryfall's one-per-card pick, rulings,
Tagger function tags and art tags; never images) and loads them into SQLite for the Deckbuilder.
Data lives in `data/.apps/deckbuilder/scryfall/`: `raw/<kind>/*.jsonl.gz` (files exactly as Scryfall serves them),
`scryfall.db`, and `scryfall.db.ingest.jsonl` (problem log).

## Usage (from the repo root)

    uv run --no-sync --project .external_worktrees/magic-deckbuilder/scryfall_mirror scryfall-mirror refresh          # download newest + load (daily)
    uv run --no-sync --project .external_worktrees/magic-deckbuilder/scryfall_mirror scryfall-mirror fetch            # download only
    uv run --no-sync --project .external_worktrees/magic-deckbuilder/scryfall_mirror scryfall-mirror load --force     # reload newest files
    uv run --no-sync --project .external_worktrees/magic-deckbuilder/scryfall_mirror scryfall-mirror status [--json]  # ledger, counts, retention

Each kind's newest downloaded file is loaded in one transaction that replaces that kind's rows; the `loads` ledger
skips a file already loaded and never loads one older than the newest loaded. A file that drops over 20% of its
records stops the load (exit 1) and leaves the previous rows in place.
Retention: newest 7 raw files per kind; problem log 60 days; prices kept forever, one row per card per change.

## Tables

- `cards`: one row per printing (`id`). Projected columns plus `raw`, the full Scryfall record as JSON.
  Two-faced cards: `oracle_id` and images fall back to the first face; `image_back` is the second face's image;
  `mana_cost`, `type_line`, `oracle_text` join faces with ` // ` (text with `\n//\n`). Images are Scryfall's links.
- `oracle_cards`: one row per card (`oracle_id`) naming Scryfall's preferred printing (`card_id`).
- `card_illustrations` (card_id, face, illustration_id): how art tags reach cards.
- `tags` (type `oracle` or `illustration`; `parent_ids`, `child_ids`, `aliases` as JSON), `oracle_taggings`,
  `art_taggings` (with Tagger's `weight`).
- `rulings`, `prices` (card_id, snapshot, usd, usd_foil, usd_etched, eur, eur_foil, tix), `loads` (ledger).

Cards with a function tag, by Scryfall's preferred printing:

    SELECT c.name, c.mana_cost, c.image_normal FROM tags t JOIN oracle_taggings ot ON ot.tag_id = t.id
    JOIN oracle_cards o ON o.oracle_id = ot.oracle_id JOIN cards c ON c.id = o.card_id WHERE t.slug = 'ramp';

Art tags of one printing:

    SELECT t.slug FROM card_illustrations i JOIN art_taggings a USING (illustration_id) JOIN tags t ON t.id = a.tag_id
    WHERE i.card_id = ?;

## Data notes (sample: first 5,000 records, 2026-11-11 files)

    default_cards: dup ids 0; oracle_id only on faces 6 (reversible cards); image_uris only on faces 177, none 6;
      illustration_id missing 217; cmc null 6; langs en 4893, es 52, ja 22 (non-English only where no English print);
      layouts include token 154, art_series 114, transform 43, split 20, adventure 17, planar 17, modal_dfc 14
    oracle_cards: one per oracle_id, includes art_series 321, tokens, planes, schemes, vanguards

## Judgment calls

| Rule | Count |
|---|---|
| Record with no `id` (cards) / `oracle_id` (rulings, oracle_cards) dropped and logged | see `status` |
| `cmc` of non-number type nulled and logged | see `status` |
| Raw files beyond the newest 7 per kind deleted (disk is ~7 GB free) | `status` lists kept files |

Problem log lines: `{"at", "kind", "file", "where", "field", "reason", "sample"}`.
