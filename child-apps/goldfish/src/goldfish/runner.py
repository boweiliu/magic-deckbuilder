"""Play your Magic deck solo (goldfishing) to see how its draws and first turns go, on a laptop or a phone, with readable card previews.

Services run from /home/user/workspace (the repo root). Conventions:

- Persistent state (anything written and read across runs -- cursors,
  caches, snapshots, user records): read and write it under ``DATA_DIR``
  (defined below), never a hardcoded ``data/.apps/goldfish/`` at the
  call site. ``DATA_DIR`` defaults to ``data/.apps/goldfish/`` but
  honors the ``GOLDFISH_DATA_DIR`` env var, so an editing agent can point a
  throwaway instance at a *copy* of the data instead of the live store
  (see the update-app skill). Do NOT use ``Path(__file__)``-based
  paths for state -- the bug to avoid is one process writing to
  ``/home/user/workspace/data/.apps/...`` while another reads from
  ``/home/user/workspace/system/apps/<pkg>/data/...``.
- Static assets shipped alongside this file (templates, default
  configs, bundled JSON): ``Path(__file__).parent / "assets/..."`` is
  fine and is the right pattern.
- Listen port: bind ``PORT`` (defined below), which defaults to this
  app's assigned port but honors the ``GOLDFISH_PORT`` env var, so
  an editing agent can boot a throwaway instance on a *spare* port
  alongside the live one (see the update-app skill). Never hardcode
  the port at the ``run_simple`` call.

This is a synchronous Flask app served by the threaded Werkzeug server.
The app owns its own browser origin (the forwarder routes
``http://goldfish.<workspace-host>/`` straight to this port), so it serves
at ``/`` and root-absolute URLs, cookies, and service workers all work
unmodified -- nothing rewrites anything. Use ``flask_sock`` if you need
WebSockets.
"""

import base64
import hashlib
import json
import os
import threading
import time
import urllib.error
import urllib.request
from pathlib import Path
from typing import Any

from app_schema.api_schema import (
    DEFAULT_API_SCHEMA_PATH,
    RouteModels,
    RouteTable,
    SchemalessRoute,
    register_api_schema_route,
)
from app_schema.routes import error_response, model_response, parse_request_body
from flask import Flask, Response, abort, request, send_file

from goldfish import decks
from goldfish.cards import CardData, lookup_many
from imbue.imbue_common.frozen_model import FrozenModel
from imbue.imbue_common.logging import setup_logging
from loguru import logger
from pydantic import Field
from werkzeug.serving import run_simple

# Persistent state for this app lives under DATA_DIR. It defaults to
# ``data/.apps/goldfish/`` but is overridable via the ``GOLDFISH_DATA_DIR`` env var
# so a throwaway instance can run against a *copy* of the data while editing --
# see the update-app skill. Always read/write state through DATA_DIR;
# never hardcode ``data/.apps/goldfish/`` at a call site, or the override is
# bypassed. A writing call site should ``DATA_DIR.mkdir(parents=True,
# exist_ok=True)`` before writing.
DATA_DIR = Path(os.environ.get("GOLDFISH_DATA_DIR", "data/.apps/goldfish"))

# Listen port. Defaults to this app's assigned port but is overridable via
# the ``GOLDFISH_PORT`` env var so an editing agent can boot a throwaway
# instance on a spare port next to the live one (see the update-app skill).
# Never hardcode the port at the ``run_simple`` call, or the override is bypassed.
PORT = int(os.environ.get("GOLDFISH_PORT", "8098"))

# The Deck List app's saved decks and the local Scryfall copy. Both are read only:
# this app never writes to either.
DECKS_DIR = Path(os.environ.get("GOLDFISH_DECKS_DIR", "data/.apps/deck-list/decks"))
SCRYFALL_DB = Path(os.environ.get("GOLDFISH_SCRYFALL_DB", "data/.apps/deckbuilder/scryfall/scryfall.db"))
STATIC_DIR = Path(__file__).parent / "static"
STATIC_FILES = {"app.js": "text/javascript", "app.css": "text/css", "icon.svg": "image/svg+xml"}
game_lock = threading.Lock()
# Scryfall's own mana and tap symbol images; its list is kept in DATA_DIR and refetched monthly.
SYMBOLOGY_URL = "https://api.scryfall.com/symbology"
SYMBOLOGY_MAX_AGE_SECONDS = 30 * 24 * 3600
symbology_lock = threading.Lock()

# The browser-side modules the workspace shell builds and every app serves from
# its own origin: the app contract (how a page talks to the shell framing it) and
# the element context menu (the right-click menu whose last rows hand the
# clicked element to a chat). A module import is a fetch without cookies, which
# the forwarder refuses across origins, so they are served here rather than from
# the shell. Relative to the repo root the service runs from, like DATA_DIR.
SHELL_STATIC_MODULES_DIR = Path("system/apps/system_interface/imbue/system_interface/static/_static")
SHELL_STATIC_MODULE_NAMES = ('app_contract.js', 'context_menu.js')

# The script every page serves (keep it on every page): it connects the page to
# the shell, reports where the page is on the handshake so the shell can reopen
# this app's window at the same place, and installs the element context menu.
# A page visited outside the shell runs it harmlessly: nothing arrives, and the
# menu's Explain and Modify rows grey out.
SHELL_PAGE_SCRIPT_BODY = """
  import { connectToShell } from "/_static/app_contract.js";
  import { installElementContextMenu } from "/_static/context_menu.js";
  let handshake = null;
  const connection = connectToShell({
    onHandshake: (received) => {
      handshake = received;
      connection.location(location.pathname + location.search, document.title);
    },
  });
  installElementContextMenu({ connection, handshake: () => handshake });
"""
SHELL_PAGE_SCRIPT = f'<script type="module">{SHELL_PAGE_SCRIPT_BODY}</script>'


def inline_script_source(body: str) -> str:
    """The Content-Security-Policy source admitting one inline <script> whose body is exactly ``body``."""
    return "'sha256-" + base64.b64encode(hashlib.sha256(body.encode()).digest()).decode() + "'"


# Every HTML page is served under this policy (the app is reachable from the
# internet once the workspace is shared): scripts only from this origin plus the
# inline blocks listed here by hash, fetches, sockets and images only from this
# origin, inline styles, frames of any http(s) page, no plugins. Anything else a
# page pulls in (a CDN script, Google Fonts, an image hosted elsewhere, a
# third-party API called from the browser) is blocked until its origin is added
# to the matching directive; a new inline <script> needs its
# ``inline_script_source`` added.
# The browser posts every block to ``/_csp-report`` below, which logs it on
# stderr (the supervisor log), so a blocked resource is never silent; the
# ``'report-sample'`` makes the browser include where a blocked inline script
# is and how it starts, since the report otherwise names it only as "inline".
CONTENT_SECURITY_POLICY = (
    "default-src 'self'; "
    f"script-src 'self' 'report-sample' {inline_script_source(SHELL_PAGE_SCRIPT_BODY)}; "
    "style-src 'self' 'unsafe-inline'; img-src 'self' data: blob: https://cards.scryfall.io https://svgs.scryfall.io; frame-src http: https:; "
    "object-src 'none'; base-uri 'self'; form-action 'self'; "
    "report-uri /_csp-report"
)
BROWSER_EXTENSION_SCHEMES = ("chrome-extension:", "moz-extension:", "safari-web-extension:")


class HealthResponse(FrozenModel):
    """What the health probe answers."""

    status: str = Field(description="Always 'ok'")


class DeckNamesResponse(FrozenModel):
    """The decks saved in the Deck List app."""

    decks: list[str] = Field(description="Deck names")


class DeckResponse(FrozenModel):
    """A deck to play, one entry per copy, with each card's details."""

    deck: str = Field(description="The deck's name")
    commander: list[str] = Field(description="Commander card names")
    cards: list[str] = Field(description="The other cards, one entry per copy")
    card_data: dict[str, CardData] = Field(description="Details of every card named above, by name")


class CardNames(FrozenModel):
    """Cards to look up by name."""

    names: list[str] = Field(description="Card names")


class CardsResponse(FrozenModel):
    """Each named card's details."""

    card_data: dict[str, CardData] = Field(description="Details of every card named, by name")


class ClientLogEntry(FrozenModel):
    """One thing the page noticed: an error, a failed request, a recovered save."""

    at: str = Field(description="When, as the page's ISO time")
    kind: str = Field(description="page-opened, page-error, request-failed, save-failed, save-recovered, load-failed")
    message: str = Field(description="What happened")
    detail: dict[str, Any] = Field(description="Context: the game's turn and zone sizes, a stack, the try number")


class ClientLog(FrozenModel):
    """A batch of the page's debug entries, with the device they came from."""

    entries: list[ClientLogEntry] = Field(description="Entries, oldest first")
    page: str = Field(description="The page's address")
    user_agent: str = Field(description="The browser's user agent")
    viewport: str = Field(description="Window size and pixel ratio, e.g. 390x844@3")


class ClientLogAccepted(FrozenModel):
    """How many entries were written."""

    written: int = Field(description="Entries appended to the log")


class SymbolsResponse(FrozenModel):
    """Scryfall's image for each card symbol."""

    symbols: dict[str, str] = Field(description="Symbol as written in rules text, e.g. '{T}' or '{W/U}', to its SVG image link; empty when Scryfall could not be reached")


class SavedGame(FrozenModel):
    """The game in progress, kept so a reload or another device picks it up."""

    game: dict[str, Any] | None = Field(description="The page's game state as it saved it, null when none is saved")


# What every route exchanges, which the app's API document publishes and every parse and answer
# is checked against (build-app skill, "The app describes its own API"): a route that reads or
# answers JSON names its pydantic models here and uses ``parse_request_body`` and
# ``model_response``; a page, a file or a stream is declared schemaless with the reason.
ROUTE_TABLE: RouteTable = {
    ("GET", "/"): SchemalessRoute(reason="the app's page"),
    ("GET", "/static/{basename}"): SchemalessRoute(reason="the page's script, styles and icon"),
    ("GET", "/_static/{basename}"): SchemalessRoute(reason="a browser-side module the shell builds"),
    ("GET", "/api/decks"): RouteModels(response_model=DeckNamesResponse),
    ("GET", "/api/decks/{deck}"): RouteModels(response_model=DeckResponse),
    ("POST", "/api/cards"): RouteModels(request_model=CardNames, response_model=CardsResponse),
    ("GET", "/api/game"): RouteModels(response_model=SavedGame),
    ("PUT", "/api/game"): RouteModels(request_model=SavedGame, response_model=SavedGame),
    ("GET", "/api/symbols"): RouteModels(response_model=SymbolsResponse),
    ("POST", "/api/client-log"): RouteModels(request_model=ClientLog, response_model=ClientLogAccepted),
    ("GET", "/health"): RouteModels(response_model=HealthResponse),
    ("POST", "/_csp-report"): SchemalessRoute(reason="the browser's report of what the page's policy blocked"),
}

app = Flask("goldfish", static_folder=None)
# The app describes its own API: an OpenAPI document of every route below, served at the
# manifest's api_schema_path.
register_api_schema_route(
    app, DEFAULT_API_SCHEMA_PATH, title='Goldfish', description='Play your Magic deck solo (goldfishing) to see how its draws and first turns go, on a laptop or a phone, with readable card previews', route_table=ROUTE_TABLE
)


@app.after_request
def add_content_security_policy(response: Response) -> Response:
    if response.mimetype == "text/html":
        response.headers.setdefault("Content-Security-Policy", CONTENT_SECURITY_POLICY)
    return response


@app.route("/")
def index() -> Response:
    """The goldfish board; ?deck=<name> picks the deck (default: the saved game's, else ulalek)."""
    page = (STATIC_DIR / "index.html").read_text().replace("<!--SHELL_PAGE_SCRIPT-->", SHELL_PAGE_SCRIPT)
    return Response(page, mimetype="text/html")


@app.route("/static/<basename>")
def static_file(basename: str) -> Response:
    """The page's script, styles and icon."""
    if basename not in STATIC_FILES:
        abort(404)
    return send_file((STATIC_DIR / basename).absolute(), mimetype=STATIC_FILES[basename], max_age=0)


@app.route("/api/decks")
def list_decks() -> Response:
    """The decks saved in the Deck List app."""
    return model_response(DeckNamesResponse(decks=decks.names(DECKS_DIR)))


@app.route("/api/decks/<deck>")
def get_deck(deck: str) -> Response:
    """One deck to play, with each card's rules text and image links."""
    saved = decks.load(DECKS_DIR, deck)
    if saved is None:
        return error_response(f"no saved deck named {deck!r}", 404)
    card_data = lookup_many(SCRYFALL_DB, sorted(set(saved.commander + saved.cards)))
    return model_response(DeckResponse(
        deck=deck, commander=list(saved.commander), cards=list(saved.cards), card_data=card_data,
    ))


@app.route("/api/cards", methods=["POST"])
def get_cards() -> Response:
    """Cards by name, for a saved game holding cards its deck list no longer has."""
    wanted = parse_request_body(CardNames)
    return model_response(CardsResponse(card_data=lookup_many(SCRYFALL_DB, sorted(set(wanted.names[:500])))))


def _game_path() -> Path:
    return DATA_DIR / "game.json"


@app.route("/api/game", methods=["GET"])
def get_game() -> Response:
    """The game in progress, or null."""
    with game_lock:
        path = _game_path()
        game = json.loads(path.read_text()) if path.is_file() else None
    return model_response(SavedGame(game=game))


@app.route("/api/game", methods=["PUT"])
def put_game() -> Response:
    """Replace the saved game with the page's current one."""
    saved = parse_request_body(SavedGame)
    with game_lock:
        DATA_DIR.mkdir(parents=True, exist_ok=True)
        tmp = _game_path().with_suffix(".tmp")
        tmp.write_text(json.dumps(saved.game))
        os.replace(tmp, _game_path())
    return model_response(saved)


def _symbology() -> dict[str, Any] | None:
    """Scryfall's symbology list as it answered it, from DATA_DIR when fresh, else fetched and saved there."""
    path = DATA_DIR / "symbology.json"
    with symbology_lock:
        if path.is_file() and time.time() - path.stat().st_mtime < SYMBOLOGY_MAX_AGE_SECONDS:
            return json.loads(path.read_text())
        fetch = urllib.request.Request(SYMBOLOGY_URL, headers={"User-Agent": "goldfish-app/0.1", "Accept": "application/json"})
        try:
            with urllib.request.urlopen(fetch, timeout=10) as answer:
                body = answer.read()
            raw = json.loads(body)
        except (urllib.error.URLError, TimeoutError, json.JSONDecodeError) as e:
            logger.warning("could not fetch Scryfall's symbols: {}", e)
            return json.loads(path.read_text()) if path.is_file() else None
        DATA_DIR.mkdir(parents=True, exist_ok=True)
        path.write_bytes(body)
        return raw


@app.route("/api/symbols")
def symbols() -> Response:
    """Scryfall's image for each mana, tap and other card symbol."""
    raw = _symbology()
    found = {d["symbol"]: d["svg_uri"] for d in (raw or {}).get("data", []) if d.get("svg_uri")}
    return model_response(SymbolsResponse(symbols=found))


CLIENT_LOG_MAX_BYTES = 2_000_000
client_log_lock = threading.Lock()


@app.route("/api/client-log", methods=["POST"])
def client_log() -> Response:
    """Record the page's errors and failed requests, so they can be read from the workspace."""
    batch = parse_request_body(ClientLog)
    with client_log_lock:
        DATA_DIR.mkdir(parents=True, exist_ok=True)
        path = DATA_DIR / "client-log.jsonl"
        if path.is_file() and path.stat().st_size > CLIENT_LOG_MAX_BYTES:
            os.replace(path, path.with_suffix(".jsonl.1"))
        with path.open("a") as f:
            for entry in batch.entries:
                record = {**entry.model_dump(), "page": batch.page, "user_agent": batch.user_agent, "viewport": batch.viewport}
                f.write(json.dumps(record) + "\n")
    for entry in batch.entries:
        if entry.kind != "page-opened":
            logger.warning("page {} ({}): {}", entry.kind, batch.viewport, entry.message)
    return model_response(ClientLogAccepted(written=len(batch.entries)))


@app.route("/_csp-report", methods=["POST"])
def content_security_policy_report() -> Response:
    """Where the browser reports what the page's security policy blocked."""
    body = request.get_json(force=True, silent=True)
    report = body.get("csp-report") if isinstance(body, dict) else None
    if not isinstance(report, dict):
        return Response(status=204)
    blocked, page, source, directive, line, sample = (
        str(report.get(key, ""))[:500]
        for key in ("blocked-uri", "document-uri", "source-file", "effective-directive", "line-number", "script-sample")
    )
    # A block caused by a browser extension is the policy doing its job and is not logged.
    if blocked.startswith(BROWSER_EXTENSION_SCHEMES) or source.startswith(BROWSER_EXTENSION_SCHEMES):
        return Response(status=204)
    # An inline script or style is reported as "inline": say where it is and how it starts instead.
    where = f" at {source}:{line}" if source and line else ""
    starting = f" starting {sample!r}" if sample else ""
    remedy = (
        "add its inline_script_source to CONTENT_SECURITY_POLICY if the page needs it"
        if blocked == "inline"
        else "add its origin to CONTENT_SECURITY_POLICY if the page needs it"
    )
    logger.error(
        "Content-Security-Policy blocked {!r} on {!r} ({}){}{}; {}", blocked, page, directive, where, starting, remedy
    )
    return Response(status=204)


@app.route("/_static/<basename>")
def shell_module(basename: str) -> Response:
    """One of the two browser-side modules the workspace shell builds and every app serves from its own origin."""
    # The two shell-built modules and nothing else: a name that is not one of
    # them is a 404, so this route can never read outside that directory.
    if basename not in SHELL_STATIC_MODULE_NAMES:
        abort(404)
    module_path = SHELL_STATIC_MODULES_DIR / basename
    if not module_path.is_file():
        abort(404)
    # Flask resolves a relative path against the app's own directory, not the cwd.
    return send_file(module_path.absolute(), mimetype="text/javascript")


@app.route("/health")
def health() -> Response:
    """Whether the app is up."""
    return model_response(HealthResponse(status="ok"))


def main() -> None:
    setup_logging()
    run_simple(
        "127.0.0.1", PORT, app, threaded=True, use_reloader=False, use_debugger=False
    )


if __name__ == "__main__":
    main()
