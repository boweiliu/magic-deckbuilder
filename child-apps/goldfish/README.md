# Goldfish

Play a deck alone (goldfishing) to see how its draws and first turns go, on a laptop or a phone.
A child app of Deck List. Port 8098, program `goldfish` (`system/supervisord.conf.d/goldfish.conf` in the workspace).

- Decks: read from the Deck List app's files, `data/.apps/deck-list/decks/<deck>.txt`. **Read only**: this app never writes there.
- Cards: `data/.apps/deckbuilder/scryfall/scryfall.db` (read only); rules text per face, image links to `cards.scryfall.io`.
- Game in progress: `data/.apps/goldfish/game.json` (the page's whole state, saved after each move), so a reload or the phone picks it up.
- Page debug log: `data/.apps/goldfish/client-log.jsonl` (page errors, failed requests, failed and recovered saves, each with the device's viewport and the game's turn and zone sizes); non-routine entries also go to the app's stderr log as `page <kind>` warnings.
- Runs with `stop_when_no_windows = false`: it is played through the sharing link on a phone, which the shell does not count as a window.
- All game rules run in the page (`static/app.js`); the server only reads decks and cards and keeps the saved game.
- Preview size, preview mode (text or picture) and board mode are kept per device and per layout (phone vs laptop) in the browser.
