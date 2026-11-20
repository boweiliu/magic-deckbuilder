# card-search (AI Card Search)

A child app of Deck List. View, run and tweak Magic card searches that you work out with your agent in chat. Two
screens: you talk to the agent in the chat; this app shows the searches, runs them, lets you edit the SQL, and shows the cards with
the matched ability highlighted. There is no agent inside the app: the chat agent writes the searches.

## How the chat agent writes a search

1. Test the SQL with the same runner the page uses (from the workspace root):
   `.external_worktrees/magic-deckbuilder/child-apps/card_search/.venv/bin/python -m card_search.query --db data/.apps/deckbuilder/scryfall/scryfall.db --text --show 30 "SQL"`
   (lines the query matched are marked `>`).
2. Save it: `PUT http://localhost:8093/api/searches/<id>` with `{"title", "notes", "queries": [{"label", "description", "sql", "highlight"}]}`.
   Same id = new version (the old one is kept in the file's `history`). The open page picks it up within 2 s.
3. Read the person's edits: `GET /api/searches/<id>` (`queries[i].by == "you"` is their edit) or the full file at
   `data/.apps/card-search/searches/<id>.json`.

## Writing the SQL

A query returns `oracle_id` (and optionally `line_no`, which makes the page highlight exactly that rules-text
line). Besides Scryfall's tables (`oracle_cards`, `cards`, `tags`, `oracle_taggings`, ...;
see `scryfall_mirror/README.md`) there is `abilities`: every activated and loyalty ability of every deck
card, split into `cost` / `effect`, with `mana` counted (Phyrexian 0, NULL for {X}), `zone`
(battlefield/hand/graveyard), `permanent`, `has_tap`, `line_no`. It is built by `abilities.py` into
`data/.apps/card-search/abilities.db` and rebuilt when the Scryfall copy or the parser changes.
`x REGEXP 'pattern'` is Python `re.search`. A `;` is allowed only inside `--` comments.
For "paper card", check any printing (`EXISTS (SELECT 1 FROM cards p WHERE p.oracle_id = ... AND json_extract(p.raw,'$.games') LIKE '%paper%')`):
the preferred printing in `oracle_cards` is sometimes online-only.
