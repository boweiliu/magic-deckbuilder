---
name: import-magic-deckbuilder
description: Install the Magic Deckbuilder (Deck List, a Magic deck list with autocomplete over every card and drag-to-tag categories, plus its optional child apps Goldfish and AI Card Search) on a Studio workspace from its repository, or update an existing install. Use when someone asks for the Magic Deckbuilder, Deck List, or gives this repo's URL.
---

# Install or update the Magic Deckbuilder

The apps live in their own repository and run from a clone at `.external_worktrees/magic-deckbuilder/`. They need no
account, key or connection: card data comes from Scryfall's public daily files. Run everything from the workspace
root (`/home/user/workspace`).

Deck List is the main app. Goldfish and AI Card Search are child apps under `child-apps/`: install them only if the
person wants them (ask once, briefly, saying what each does; the README describes them).

1. **Get the code.** New install: `git clone https://github.com/boweiliu/magic-deckbuilder .external_worktrees/magic-deckbuilder`.
   Update: `git -C .external_worktrees/magic-deckbuilder pull --ff-only`, then restart the programs (step 5's
   `supervisorctl restart` for each installed one) and stop here.
2. **Check disk and ports.** The card data needs about 3 GB: `df -h /home/user/workspace/data`. Deck List uses port
   8081, Goldfish 8098, AI Card Search 8093. If `grep -l -E '8081|8098|8093' system/supervisord.conf.d/*.conf` names
   another program, pick a free port for that app and change it in its `workspace/supervisord/<app>.conf` copy (the
   `--url http://localhost:<port>` part) and add `environment=DECK_LIST_PORT="<port>"` (or `GOLDFISH_PORT`,
   `CARD_SEARCH_PORT`) to the same program block.
3. **Download the card data first.** The apps need it before their first search. It takes several minutes, so run it in
   the background:
   ```bash
   python3 system/scripts/workspace_bare_scripts/run_in_background.py --description "Download all Magic cards from Scryfall" -- \
       uv run --frozen --project .external_worktrees/magic-deckbuilder/scryfall_mirror scryfall-mirror refresh
   ```
   It is done when `uv run --frozen --project .external_worktrees/magic-deckbuilder/scryfall_mirror scryfall-mirror status`
   lists loads for `oracle_cards`, `default_cards` and `oracle_tags`, and `data/.apps/deckbuilder/scryfall/scryfall.db` exists.
4. **Keep it fresh daily:** `cp .external_worktrees/magic-deckbuilder/workspace/routines/scryfall-refresh.toml system/routines/`
   (the routine runs the same refresh at 04:30; the manage-routines skill shows it in the Routines app).
5. **Wire and start the apps.** Copy the program file for each app being installed, then load them:
   ```bash
   cp .external_worktrees/magic-deckbuilder/workspace/supervisord/deck-list.conf system/supervisord.conf.d/
   # child apps, only if wanted:
   cp .external_worktrees/magic-deckbuilder/workspace/supervisord/goldfish.conf system/supervisord.conf.d/
   cp .external_worktrees/magic-deckbuilder/workspace/supervisord/card-search.conf system/supervisord.conf.d/
   supervisorctl reread && supervisorctl update
   ```
   Each program syncs its own environment on first start, which can take a minute. Check:
   `curl -sf http://127.0.0.1:8081/health` (and 8098, 8093) answers `{"status": "ok"}`; if not, read
   `/var/log/supervisor/<program>-stderr.log`.
6. **Check a search works:** `curl -s 'http://127.0.0.1:8081/api/cards/suggest?q=sol%20ri'` lists Sol Ring. If it fails
   because the card database is missing, step 3 has not finished.
7. **Show it:** `uv run --no-sync workspace-layout open deck-list --beside`.
8. **Save the wiring:** commit the copied `system/supervisord.conf.d/*.conf` files and `system/routines/scryfall-refresh.toml`
   in the workspace.

## Using it

- A new workspace starts with one list, `ulalek`, with Ulalek, Fused Atrocity as its commander. To set another
  commander, post to `http://127.0.0.1:8081/api/decks/<deck>/add` with `{"name": "<card name>", "as_commander": true}`
  (remove the old one with `/api/decks/<deck>/remove` and the same body), or edit the `// Commander` section of
  `data/.apps/deck-list/decks/<deck>.txt`. "New list..." in the page's list picker can copy the current commander.
- To back decks up to git, make `data/.apps/deck-list/decks/` a git repository with an `origin` the workspace can push
  to; every change is then committed and pushed in the background. Without that, decks are plain files and nothing is
  pushed anywhere.
- AI Card Search has no agent inside it: the person describes a search in chat and you write it.
  `child-apps/card_search/README.md` says how to test and save one.
