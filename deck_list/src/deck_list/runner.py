"""Write down a Magic deck list card by card, with card names autocompleted from a local copy of Scryfall.

Services run from /home/user/workspace (the repo root). Conventions:

- Persistent state (anything written and read across runs -- cursors,
  caches, snapshots, user records): read and write it under ``DATA_DIR``
  (defined below), never a hardcoded ``data/.apps/deck-list/`` at the
  call site. ``DATA_DIR`` defaults to ``data/.apps/deck-list/`` but
  honors the ``DECK_LIST_DATA_DIR`` env var, so an editing agent can point a
  throwaway instance at a *copy* of the data instead of the live store
  (see the update-app skill). Do NOT use ``Path(__file__)``-based
  paths for state -- the bug to avoid is one process writing to
  ``/home/user/workspace/data/.apps/...`` while another reads from
  ``/home/user/workspace/system/apps/<pkg>/data/...``.
- Static assets shipped alongside this file (templates, default
  configs, bundled JSON): ``Path(__file__).parent / "assets/..."`` is
  fine and is the right pattern.
- Listen port: bind ``PORT`` (defined below), which defaults to this
  app's assigned port but honors the ``DECK_LIST_PORT`` env var, so
  an editing agent can boot a throwaway instance on a *spare* port
  alongside the live one (see the update-app skill). Never hardcode
  the port at the ``run_simple`` call.

This is a synchronous Flask app served by the threaded Werkzeug server.
The app owns its own browser origin (the forwarder routes
``http://deck-list.<workspace-host>/`` straight to this port), so it serves
at ``/`` and root-absolute URLs, cookies, and service workers all work
unmodified -- nothing rewrites anything. Use ``flask_sock`` if you need
WebSockets.
"""

import base64
import threading
import hashlib
import os
from pathlib import Path

from app_schema.api_schema import (
    DEFAULT_API_SCHEMA_PATH,
    RouteModels,
    RouteTable,
    SchemalessRoute,
    register_api_schema_route,
)
from app_schema.routes import error_response, model_response, parse_request_body
from flask import Flask, Response, abort, request, send_file

from deck_list import autotag, decks, groups, history, tags
from deck_list.cards import Card, CardIndex, CycleIndex
from imbue.imbue_common.frozen_model import FrozenModel
from imbue.imbue_common.logging import setup_logging
from loguru import logger
from pydantic import Field
from werkzeug.serving import run_simple

# Persistent state for this app lives under DATA_DIR. It defaults to
# ``data/.apps/deck-list/`` but is overridable via the ``DECK_LIST_DATA_DIR`` env var
# so a throwaway instance can run against a *copy* of the data while editing --
# see the update-app skill. Always read/write state through DATA_DIR;
# never hardcode ``data/.apps/deck-list/`` at a call site, or the override is
# bypassed. A writing call site should ``DATA_DIR.mkdir(parents=True,
# exist_ok=True)`` before writing.
DATA_DIR = Path(os.environ.get("DECK_LIST_DATA_DIR", "data/.apps/deck-list"))

# The local Scryfall copy the scryfall-mirror routine refreshes daily (read only).
SCRYFALL_DB = Path(os.environ.get("DECK_LIST_SCRYFALL_DB", "data/.apps/deckbuilder/scryfall/scryfall.db"))

# Listen port. Defaults to this app's assigned port but is overridable via
# the ``DECK_LIST_PORT`` env var so an editing agent can boot a throwaway
# instance on a spare port next to the live one (see the update-app skill).
# Never hardcode the port at the ``run_simple`` call, or the override is bypassed.
PORT = int(os.environ.get("DECK_LIST_PORT", "8081"))

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
    "style-src 'self' 'unsafe-inline'; img-src 'self' data: blob: https://cards.scryfall.io; frame-src http: https:; "
    "object-src 'none'; base-uri 'self'; form-action 'self'; "
    "report-uri /_csp-report"
)
BROWSER_EXTENSION_SCHEMES = ("chrome-extension:", "moz-extension:", "safari-web-extension:")


DEFAULT_DECK = "ulalek"
# A brand-new deck under this name starts with its commander filled in.
DEFAULT_COMMANDERS = {"ulalek": "Ulalek, Fused Atrocity"}
STATIC_DIR = Path(__file__).parent / "static"
STATIC_FILES = {
    "app.js": "text/javascript", "app.css": "text/css", "icon.svg": "image/svg+xml", "zones.js": "text/javascript",
    "categories.js": "text/javascript", "categories.css": "text/css", "api.js": "text/javascript",
    "groups.js": "text/javascript", "groups.css": "text/css",
}

card_index = CardIndex(SCRYFALL_DB)
cycle_index = CycleIndex(SCRYFALL_DB)
tagger_index = autotag.TaggerIndex(SCRYFALL_DB)
# One change at a time, so two windows on the same deck never overwrite each other.
deck_lock = threading.Lock()
pusher = history.Pusher(DATA_DIR / "decks")


class HealthResponse(FrozenModel):
    """What the health probe answers."""

    status: str = Field(description="Always 'ok'")


class CardInfo(FrozenModel):
    """One card as the page shows it."""

    name: str = Field(description="The card's full name")
    mana_cost: str = Field(description="Mana cost in Scryfall's {X}{U} notation; faces joined with ' // '")
    mana_value: float = Field(description="Mana value (converted mana cost); a two-faced card counts its front face")
    type_line: str = Field(description="Type line")
    image: str | None = Field(description="Scryfall image link of the front face")
    image_back: str | None = Field(description="Scryfall image link of the back face, for two-faced cards")
    color_identity: list[str] = Field(description="Color identity letters, e.g. ['U', 'B']")
    scryfall_uri: str | None = Field(description="The card's page on Scryfall")
    commander_legality: str | None = Field(description="Scryfall's commander legality: legal, banned, not_legal, ...")
    cycles: int = Field(default=0, description="How many cycles (Scryfall Tagger's cycle-* tags) the card is in")


class SuggestResponse(FrozenModel):
    """Card names matching what was typed, best first."""

    cards: list[CardInfo] = Field(description="Matches: name starts with the text first, then popular before obscure")


class DeckEntry(FrozenModel):
    """One line of a deck list with what is known about the card."""

    name: str = Field(description="Card name as written in the list")
    qty: int = Field(description="How many copies")
    card: CardInfo | None = Field(description="The card, or null when no card has that name")


class CategoryInfo(FrozenModel):
    """One category a card can be tagged with."""

    id: str = Field(description="Stable id the tags refer to")
    name: str = Field(description="What the person calls it")
    color: str = Field(description="Its colour, #rrggbb")


class GroupInfo(FrozenModel):
    """Cards the person treats as one unit."""

    id: str = Field(description="Stable id")
    name: str = Field(description="What the person calls it")
    cards: list[str] = Field(description="Card names, as in the deck; at least two")


class DeckResponse(FrozenModel):
    """A deck list."""

    deck: str = Field(description="The deck's name")
    commander: list[DeckEntry] = Field(description="The commander (usually one)")
    cards: list[DeckEntry] = Field(description="The other cards, in the order they were added")
    version: str | None = Field(description="Id of the saved change this list reflects (its commit), null before history starts")
    backed_up: bool = Field(description="Whether the latest change is also copied to GitHub")
    categories: list[CategoryInfo] = Field(description="The deck's categories, in drag-zone order (clockwise from the top)")
    tags: dict[str, list[str]] = Field(description="Card name to the ids of the categories it carries")
    groups: list[GroupInfo] = Field(default=[], description="Groups of cards shown as one unit; a card is in at most one")


class CycleInfo(FrozenModel):
    """A cycle of cards, as Scryfall's Tagger lists it."""

    id: str = Field(description="The Tagger tag, e.g. cycle-mh3-landscape")
    name: str = Field(description="A readable name, e.g. Landscape (MH3)")
    cards: list[CardInfo] = Field(description="Its cards, by name")


class CyclesResponse(FrozenModel):
    """The cycles a card is in."""

    cycles: list[CycleInfo] = Field(description="Smallest first")


class AddGroup(FrozenModel):
    """Add several cards at once and group them."""

    cards: list[str] = Field(description="Card names; ones already in the deck are only grouped")
    name: str = Field(description="The group's name (up to 40 characters)")


class GroupMake(FrozenModel):
    """Group cards already in the deck; they leave any group they were in."""

    cards: list[str] = Field(description="Card names, at least two")
    name: str = Field(default="Group", description="The group's name")


class GroupUngroup(FrozenModel):
    """Dissolve a group; its cards stay in the deck."""

    id: str = Field(description="Group id")


class GroupRename(FrozenModel):
    """Rename a group."""

    id: str = Field(description="Group id")
    name: str = Field(description="New name")


class DeckSummary(FrozenModel):
    """One saved deck, for the deck picker."""

    name: str = Field(description="The deck's name (lowercase letters, digits and hyphens)")
    cards: int = Field(description="How many cards, not counting the commander")


class DeckListResponse(FrozenModel):
    """Every saved deck."""

    decks: list[DeckSummary] = Field(description="Alphabetical by name")


class NewDeck(FrozenModel):
    """Start a new, empty deck."""

    copy_from: str | None = Field(
        default=None, description="A deck whose commander and categories (not its cards or tags) the new one starts with",
    )


class CardChange(FrozenModel):
    """One card to add or remove."""

    name: str = Field(description="Card name (matched to the real card name when one exists)")
    as_commander: bool = Field(default=False, description="Add as the commander instead of to the 99")


class TagChange(FrozenModel):
    """Put a category on a card, or take it off."""

    card: str = Field(description="Card name, as in the deck")
    category: str = Field(description="Category id")
    on: bool = Field(description="True to add the tag, false to remove it")


class AutotagGuess(FrozenModel):
    """Cards a category's rule matches that do not carry it yet."""

    category: str = Field(description="Category id")
    why: str = Field(description="What the guess is based on")
    cards: list[str] = Field(description="Card names, alphabetical")


class AutotagResponse(FrozenModel):
    """Suggested tags for the deck, for the person to review; nothing is saved until they add them."""

    guesses: list[AutotagGuess] = Field(description="One per category that has a rule, in category order")
    no_rule: list[str] = Field(description="Ids of the categories there is no way to guess")


class TagPair(FrozenModel):
    """One tag to put on one card."""

    card: str = Field(description="Card name, as in the deck")
    category: str = Field(description="Category id")


class AddTags(FrozenModel):
    """Several tags put on at once; ones already there are skipped and nothing is taken off."""

    tags: list[TagPair] = Field(description="The tags to add")


class CategoryAdd(FrozenModel):
    """A new category, added at the end."""

    name: str = Field(description="Its name (up to 24 characters)")
    color: str | None = Field(default=None, description="#rrggbb, or null for the next unused colour")


class CategoryEdit(FrozenModel):
    """Rename or recolour a category."""

    id: str = Field(description="Category id")
    name: str | None = Field(default=None, description="New name, or null to keep it")
    color: str | None = Field(default=None, description="New colour #rrggbb, or null to keep it")


class CategoryMove(FrozenModel):
    """Move a category to another place in the order (which is the drag-zone order)."""

    id: str = Field(description="Category id")
    position: int = Field(description="New place, 0 for first")


class CategoryDelete(FrozenModel):
    """Delete a category; every card loses that tag."""

    id: str = Field(description="Category id")


class ClientLogEntry(FrozenModel):
    """Something the page noticed: a failed or retried server call, or an error in the page."""

    time: str = Field(description="When the page noticed it (ISO time, the browser's clock)")
    page: str = Field(description="Which page, with its query (e.g. /?deck=ulalek)")
    message: str = Field(description="What happened")
    detail: str = Field(default="", description="Details as JSON text")


class ClientLog(FrozenModel):
    """A batch of things the page noticed, sent together."""

    entries: list[ClientLogEntry] = Field(description="Oldest first; at most 50 are logged per call")


class ClientLogResponse(FrozenModel):
    """How many entries were written to the log."""

    logged: int = Field(description="Entries written")


ROUTE_TABLE: RouteTable = {
    ("GET", "/"): SchemalessRoute(reason="the app's page"),
    ("GET", "/static/{basename}"): SchemalessRoute(reason="the page's script, styles and icon"),
    ("GET", "/_static/{basename}"): SchemalessRoute(reason="a browser-side module the shell builds"),
    ("GET", "/health"): RouteModels(response_model=HealthResponse),
    ("POST", "/_csp-report"): SchemalessRoute(reason="the browser's report of what the page's policy blocked"),
    ("GET", "/api/cards/suggest"): RouteModels(response_model=SuggestResponse),
    ("GET", "/api/decks"): RouteModels(response_model=DeckListResponse),
    ("GET", "/api/decks/{deck}"): RouteModels(response_model=DeckResponse),
    ("POST", "/api/decks/{deck}/create"): RouteModels(request_model=NewDeck, response_model=DeckResponse),
    ("POST", "/api/decks/{deck}/add"): RouteModels(request_model=CardChange, response_model=DeckResponse),
    ("POST", "/api/decks/{deck}/remove"): RouteModels(request_model=CardChange, response_model=DeckResponse),
    ("POST", "/api/decks/{deck}/tag"): RouteModels(request_model=TagChange, response_model=DeckResponse),
    ("GET", "/api/decks/{deck}/autotag"): RouteModels(response_model=AutotagResponse),
    ("POST", "/api/decks/{deck}/tags/add-many"): RouteModels(request_model=AddTags, response_model=DeckResponse),
    ("POST", "/api/decks/{deck}/categories/add"): RouteModels(request_model=CategoryAdd, response_model=DeckResponse),
    ("POST", "/api/decks/{deck}/categories/edit"): RouteModels(request_model=CategoryEdit, response_model=DeckResponse),
    ("POST", "/api/decks/{deck}/categories/move"): RouteModels(request_model=CategoryMove, response_model=DeckResponse),
    ("POST", "/api/decks/{deck}/categories/delete"): RouteModels(request_model=CategoryDelete, response_model=DeckResponse),
    ("POST", "/api/client-log"): RouteModels(request_model=ClientLog, response_model=ClientLogResponse),
    ("GET", "/api/cards/cycles"): RouteModels(response_model=CyclesResponse),
    ("POST", "/api/decks/{deck}/add-group"): RouteModels(request_model=AddGroup, response_model=DeckResponse),
    ("POST", "/api/decks/{deck}/groups/make"): RouteModels(request_model=GroupMake, response_model=DeckResponse),
    ("POST", "/api/decks/{deck}/groups/ungroup"): RouteModels(request_model=GroupUngroup, response_model=DeckResponse),
    ("POST", "/api/decks/{deck}/groups/rename"): RouteModels(request_model=GroupRename, response_model=DeckResponse),
    ("GET", "/groups"): SchemalessRoute(reason="the deck page with cards grouped into units, cycles added at once"),
    ("GET", "/categories"): SchemalessRoute(reason="the page for managing the deck's categories and their drag zones"),
    ("GET", "/api/decks/{deck}/text"): SchemalessRoute(reason="the deck's saved file as plain text"),
}

app = Flask("deck_list", static_folder=None)
register_api_schema_route(
    app, DEFAULT_API_SCHEMA_PATH, title="Deck List",
    description="Write down a Magic deck list card by card, with card names autocompleted from a local copy of Scryfall",
    route_table=ROUTE_TABLE,
)


@app.after_request
def add_content_security_policy(response: Response) -> Response:
    if response.mimetype == "text/html":
        response.headers.setdefault("Content-Security-Policy", CONTENT_SECURITY_POLICY)
    return response


def _info(card: Card) -> CardInfo:
    return CardInfo(
        name=card.name, mana_cost=card.mana_cost, mana_value=card.mana_value, type_line=card.type_line, image=card.image,
        image_back=card.image_back, color_identity=list(card.color_identity), scryfall_uri=card.scryfall_uri,
        commander_legality=card.commander_legality, cycles=len(cycle_index.of(card.name)),
    )


def _entry(entry: decks.Entry) -> DeckEntry:
    card = card_index.lookup(entry.name)
    return DeckEntry(name=card.name if card else entry.name, qty=entry.qty, card=_info(card) if card else None)


def _deck_model(name: str, deck: decks.Deck, version: str | None = None) -> DeckResponse:
    saved_tags = tags.load(_decks_dir(), name)
    cards = [_entry(e) for e in deck.cards]
    saved_groups = groups.in_deck(groups.load(_decks_dir(), name), {c.name for c in cards})
    return DeckResponse(
        deck=name, commander=[_entry(e) for e in deck.commander], cards=cards,
        version=version, backed_up=pusher.is_backed_up(),
        categories=[CategoryInfo(id=c.id, name=c.name, color=c.color) for c in saved_tags.categories],
        tags={card: list(ids) for card, ids in saved_tags.tags.items()},
        groups=[GroupInfo(id=g.id, name=g.name, cards=list(g.cards)) for g in saved_groups],
    )


def _deck_response(name: str, deck: decks.Deck, version: str | None = None) -> Response:
    return model_response(_deck_model(name, deck, version))


def _decks_dir() -> Path:
    return DATA_DIR / "decks"


@app.route("/")
def index() -> Response:
    """The deck list page; ?deck=<name> picks the deck (default: ulalek)."""
    page = (STATIC_DIR / "index.html").read_text().replace("<!--SHELL_PAGE_SCRIPT-->", SHELL_PAGE_SCRIPT)
    # The deck goes inside the page, so the list shows on the first draw instead of after a
    # second round trip (slow on a phone over a shared link). A data block is not run, so the
    # page's security policy needs no change; "</" is escaped so the data cannot end the block.
    deck = request.args.get("deck", "ulalek")
    try:
        with deck_lock:
            model = _deck_model(deck, _saved_or_new(deck), history.head(_decks_dir()))
    except decks.DeckNameError:
        return Response(page.replace("<!--DECK_DATA-->", ""), mimetype="text/html")
    data = model.model_dump_json().replace("</", "<\\/")
    page = page.replace("<!--DECK_DATA-->", f'<script type="application/json" id="deck-data">{data}</script>')
    return Response(page, mimetype="text/html")


@app.route("/static/<basename>")
def static_file(basename: str) -> Response:
    """The page's script, styles and icon."""
    if basename not in STATIC_FILES:
        abort(404)
    return send_file((STATIC_DIR / basename).absolute(), mimetype=STATIC_FILES[basename], max_age=0)


@app.route("/api/cards/suggest")
def suggest() -> Response:
    """Card names matching the typed text, for the add box."""
    query = request.args.get("q", "")[:100]
    return model_response(SuggestResponse(cards=[_info(c) for c in card_index.suggest(query)]))


@app.route("/api/decks", methods=["GET"])
def list_decks() -> Response:
    """Every saved deck with its card count, for switching between decks."""
    summaries = []
    for name in decks.names(_decks_dir()):
        saved = decks.load(_decks_dir(), name)
        summaries.append(DeckSummary(name=name, cards=sum(e.qty for e in saved.cards) if saved else 0))
    return model_response(DeckListResponse(decks=summaries))


@app.route("/api/decks/<deck>/create", methods=["POST"])
def create_deck(deck: str) -> Response:
    """Start a new, empty deck, optionally with another deck's commander and categories."""
    copy_from = parse_request_body(NewDeck).copy_from
    with deck_lock:
        try:
            if decks.load(_decks_dir(), deck) is not None:
                return error_response(f"there is already a deck named {deck}", 409)
            source = decks.load(_decks_dir(), copy_from) if copy_from else None
        except decks.DeckNameError as e:
            return error_response(str(e), 400)
        if copy_from and source is None:
            return error_response(f"there is no deck named {copy_from}", 404)
        if source is not None:
            commander = source.commander
        else:
            default = DEFAULT_COMMANDERS.get(deck)
            commander = (decks.Entry(default, 1),) if default else ()
        created = decks.Deck(commander=commander, cards=())
        decks.save(_decks_dir(), deck, created)
        origin = f" from {copy_from}" if source is not None else ""
        version = history.commit(_decks_dir(), decks.path_for(_decks_dir(), deck), f"{deck}: new deck{origin}")
        if copy_from and source is not None:
            categories = tags.load(_decks_dir(), copy_from).categories
            if categories:
                path = tags.save(_decks_dir(), deck, tags.Tags(categories=categories, tags={}))
                version = history.commit(_decks_dir(), path, f"{deck}: categories{origin}")
    pusher.request()
    return _deck_response(deck, created, version)


@app.route("/api/decks/<deck>", methods=["GET"])
def get_deck(deck: str) -> Response:
    """A deck list; a new deck starts empty, or with its commander when the name is a known one."""
    try:
        saved = _saved_or_new(deck)
    except decks.DeckNameError as e:
        return error_response(str(e), 400)
    return _deck_response(deck, saved, history.head(_decks_dir()))


def _saved_or_new(deck: str) -> decks.Deck:
    saved = decks.load(_decks_dir(), deck)
    if saved is None:
        commander = DEFAULT_COMMANDERS.get(deck)
        saved = decks.Deck(commander=(decks.Entry(commander, 1),) if commander else (), cards=())
    return saved


def _change(deck: str, apply) -> Response:
    """Apply one change to the saved list, save it, commit it, and start the GitHub copy."""
    with deck_lock:
        try:
            current = decks.load(_decks_dir(), deck)
        except decks.DeckNameError as e:
            return error_response(str(e), 400)
        if current is None:
            commander = DEFAULT_COMMANDERS.get(deck)
            current = decks.Deck(commander=(decks.Entry(commander, 1),) if commander else (), cards=())
        updated, message = apply(current)
        if updated is None:
            return error_response(message, 409)
        decks.save(_decks_dir(), deck, updated)
        version = history.commit(_decks_dir(), decks.path_for(_decks_dir(), deck), f"{deck}: {message}")
    pusher.request()
    return _deck_response(deck, updated, version)


def _canonical(name: str) -> str:
    card = card_index.lookup(name)
    return card.name if card else name.strip()


@app.route("/api/decks/<deck>/add", methods=["POST"])
def add_card(deck: str) -> Response:
    """Add one card (or one more copy of a basic land); a card already in the list is refused."""
    change = parse_request_body(CardChange)
    name = _canonical(change.name)
    card = card_index.lookup(name)

    def apply(current: decks.Deck) -> tuple[decks.Deck | None, str]:
        if change.as_commander:
            return decks.Deck(current.commander + (decks.Entry(name, 1),), current.cards), f"+ commander {name}"
        if any(e.name == name for e in current.commander):
            return None, f"{name} is your commander"
        existing = next((e for e in current.cards if e.name == name), None)
        if existing is None:
            return decks.Deck(current.commander, current.cards + (decks.Entry(name, 1),)), f"+ {name}"
        if card is None or not card.type_line.startswith("Basic"):
            return None, f"{name} is already in the deck"
        cards = tuple(decks.Entry(e.name, e.qty + 1) if e.name == name else e for e in current.cards)
        return decks.Deck(current.commander, cards), f"+ {name} ({existing.qty + 1})"

    return _change(deck, apply)


@app.route("/api/decks/<deck>/remove", methods=["POST"])
def remove_card(deck: str) -> Response:
    """Remove one copy of a card (or the commander)."""
    change = parse_request_body(CardChange)
    name = _canonical(change.name)

    def apply(current: decks.Deck) -> tuple[decks.Deck | None, str]:
        if change.as_commander:
            if not any(e.name == name for e in current.commander):
                return None, f"{name} is not the commander"
            return decks.Deck(tuple(e for e in current.commander if e.name != name), current.cards), f"- commander {name}"
        existing = next((e for e in current.cards if e.name == name), None)
        if existing is None:
            return None, f"{name} is not in the deck"
        cards = tuple(
            decks.Entry(e.name, e.qty - 1) if e.name == name else e for e in current.cards if not (e.name == name and e.qty == 1)
        )
        return decks.Deck(current.commander, cards), f"- {name}"

    return _change(deck, apply)


def _change_tags(deck: str, apply) -> Response:
    """Apply one change to the deck's categories or tags, save it, commit it, and start the GitHub copy."""
    with deck_lock:
        try:
            current_deck = decks.load(_decks_dir(), deck)
        except decks.DeckNameError as e:
            return error_response(str(e), 400)
        if current_deck is None:
            return error_response("add a card to this deck first", 409)
        updated, message = apply(tags.load(_decks_dir(), deck), current_deck)
        if updated is None:
            return error_response(message, 409)
        path = tags.save(_decks_dir(), deck, updated)
        version = history.commit(_decks_dir(), path, f"{deck}: {message}")
    pusher.request()
    return _deck_response(deck, current_deck, version)


@app.route("/api/decks/<deck>/tag", methods=["POST"])
def tag_card(deck: str) -> Response:
    """Put one category on one card, or take it off."""
    change = parse_request_body(TagChange)
    name = _canonical(change.card)

    def apply(current: tags.Tags, deck_now: decks.Deck) -> tags.Result:
        if not any(e.name == name for e in deck_now.cards + deck_now.commander):
            return None, f"{name} is not in the deck"
        return tags.set_tag(current, name, change.category, change.on)

    return _change_tags(deck, apply)


@app.route("/api/decks/<deck>/autotag")
def autotag_guesses(deck: str) -> Response:
    """Guess tags for the deck's cards (lands by type, ramp, draw, interaction, wipes and tutors by Scryfall's tags)."""
    try:
        saved = decks.load(_decks_dir(), deck)
    except decks.DeckNameError as e:
        return error_response(str(e), 400)
    cards = [c for c in (card_index.lookup(e.name) for e in (saved.cards if saved else ())) if c is not None]
    current = tags.load(_decks_dir(), deck)
    found, no_rule = autotag.guesses(current.categories, cards, current.tags, tagger_index)
    return model_response(AutotagResponse(
        guesses=[AutotagGuess(category=g.category_id, why=g.why, cards=list(g.cards)) for g in found], no_rule=no_rule,
    ))


@app.route("/api/decks/<deck>/tags/add-many", methods=["POST"])
def add_many_tags(deck: str) -> Response:
    """Put several tags on at once, as one saved change; tags a card already has are skipped, none are taken off."""
    change = parse_request_body(AddTags)
    pairs = [(_canonical(t.card), t.category) for t in change.tags]

    def apply(current: tags.Tags, deck_now: decks.Deck) -> tags.Result:
        present = {e.name for e in deck_now.cards + deck_now.commander} | _deck_names(deck_now)
        missing = next((card for card, _ in pairs if card not in present), None)
        if missing is not None:
            return None, f"{missing} is not in the deck"
        return tags.add_many(current, pairs)

    return _change_tags(deck, apply)


@app.route("/api/decks/<deck>/categories/add", methods=["POST"])
def add_category(deck: str) -> Response:
    """Add a category at the end of the order."""
    change = parse_request_body(CategoryAdd)
    return _change_tags(deck, lambda current, _: tags.add_category(current, change.name, change.color))


@app.route("/api/decks/<deck>/categories/edit", methods=["POST"])
def edit_category(deck: str) -> Response:
    """Rename or recolour a category."""
    change = parse_request_body(CategoryEdit)
    return _change_tags(deck, lambda current, _: tags.edit_category(current, change.id, change.name, change.color))


@app.route("/api/decks/<deck>/categories/move", methods=["POST"])
def move_category(deck: str) -> Response:
    """Move a category to another place in the order, which moves its drag zone."""
    change = parse_request_body(CategoryMove)
    return _change_tags(deck, lambda current, _: tags.move_category(current, change.id, change.position))


@app.route("/api/decks/<deck>/categories/delete", methods=["POST"])
def delete_category(deck: str) -> Response:
    """Delete a category; every card loses that tag."""
    change = parse_request_body(CategoryDelete)
    return _change_tags(deck, lambda current, _: tags.delete_category(current, change.id))


@app.route("/api/cards/cycles")
def card_cycles() -> Response:
    """The cycles a card is in (the five eggs, the ten landscapes...), each with its cards."""
    card = card_index.lookup(request.args.get("name", "")[:200])
    found = []
    for cycle in cycle_index.of(card.name) if card else ():
        members = [c for c in (card_index.lookup(n) for n in cycle.cards) if c is not None]
        if len(members) >= 2:
            found.append(CycleInfo(id=cycle.id, name=cycle.name, cards=[_info(c) for c in members]))
    return model_response(CyclesResponse(cycles=found))


def _deck_names(deck: decks.Deck) -> set[str]:
    return {_canonical(e.name) for e in deck.cards}


@app.route("/api/decks/<deck>/add-group", methods=["POST"])
def add_group(deck: str) -> Response:
    """Add several cards at once (a whole cycle) and group them; cards already in the deck are only grouped."""
    change = parse_request_body(AddGroup)
    names = list(dict.fromkeys(_canonical(n) for n in change.cards if n.strip()))
    with deck_lock:
        try:
            current = _saved_or_new(deck)
        except decks.DeckNameError as e:
            return error_response(str(e), 400)
        commanders = {_canonical(e.name) for e in current.commander}
        present = _deck_names(current)
        members = [n for n in names if n not in commanders]
        added = [n for n in members if n not in present]
        if not members:
            return error_response("none of those cards can go in the deck", 409)
        updated = decks.Deck(current.commander, current.cards + tuple(decks.Entry(n, 1) for n in added))
        decks.save(_decks_dir(), deck, updated)
        paths = [decks.path_for(_decks_dir(), deck)]
        message = f"+ {len(added)} cards ({', '.join(added)})" if added else "no new cards"
        if len(members) >= 2:
            existing = groups.in_deck(groups.load(_decks_dir(), deck), present | set(added))
            regrouped, group_message = groups.make(existing, members, change.name)
            if regrouped is not None:
                paths.append(groups.save(_decks_dir(), deck, regrouped))
                message += f", {group_message}"
        version = history.commit_files(_decks_dir(), tuple(paths), f"{deck}: {message}")
    pusher.request()
    return _deck_response(deck, updated, version)


def _change_groups(deck: str, apply) -> Response:
    """Apply one change to the deck's groups, save it, commit it, and start the GitHub copy."""
    with deck_lock:
        try:
            current = decks.load(_decks_dir(), deck)
        except decks.DeckNameError as e:
            return error_response(str(e), 400)
        if current is None:
            return error_response("add a card to this deck first", 409)
        present = _deck_names(current)
        updated, message = apply(groups.in_deck(groups.load(_decks_dir(), deck), present), present)
        if updated is None:
            return error_response(message, 409)
        path = groups.save(_decks_dir(), deck, updated)
        version = history.commit(_decks_dir(), path, f"{deck}: {message}")
    pusher.request()
    return _deck_response(deck, current, version)


@app.route("/api/decks/<deck>/groups/make", methods=["POST"])
def make_group(deck: str) -> Response:
    """Group cards already in the deck; they leave any group they were in."""
    change = parse_request_body(GroupMake)
    names = [_canonical(n) for n in change.cards]

    def apply(current: tuple[groups.Group, ...], present: set[str]) -> groups.Result:
        missing = [n for n in names if n not in present]
        if missing:
            return None, f"{missing[0]} is not in the deck"
        return groups.make(current, names, change.name)

    return _change_groups(deck, apply)


@app.route("/api/decks/<deck>/groups/ungroup", methods=["POST"])
def ungroup(deck: str) -> Response:
    """Dissolve a group; its cards stay in the deck."""
    change = parse_request_body(GroupUngroup)
    return _change_groups(deck, lambda current, _: groups.ungroup(current, change.id))


@app.route("/api/decks/<deck>/groups/rename", methods=["POST"])
def rename_group(deck: str) -> Response:
    """Rename a group."""
    change = parse_request_body(GroupRename)
    return _change_groups(deck, lambda current, _: groups.rename(current, change.id, change.name))


@app.route("/groups")
def groups_page() -> Response:
    """The deck page with cards grouped into units: add a whole cycle at once, group and ungroup cards."""
    page = (STATIC_DIR / "groups.html").read_text().replace("<!--SHELL_PAGE_SCRIPT-->", SHELL_PAGE_SCRIPT)
    return Response(page, mimetype="text/html")


@app.route("/api/client-log", methods=["POST"])
def client_log() -> Response:
    """Write what the page noticed (failed or retried saves, page errors) to the app's log."""
    batch = parse_request_body(ClientLog)
    entries = batch.entries[-50:]
    agent = request.headers.get("User-Agent", "")[:120]
    for entry in entries:
        logger.warning(
            "Page reported: {} | page {} at {} | {} | {}",
            entry.message[:300], entry.page[:200], entry.time[:40], entry.detail[:1500], agent,
        )
    return model_response(ClientLogResponse(logged=len(entries)))


@app.route("/categories")
def categories_page() -> Response:
    """The page for adding, renaming, recolouring, reordering and deleting the deck's categories."""
    page = (STATIC_DIR / "categories.html").read_text().replace("<!--SHELL_PAGE_SCRIPT-->", SHELL_PAGE_SCRIPT)
    return Response(page, mimetype="text/html")


@app.route("/api/decks/<deck>/text")
def deck_text(deck: str) -> Response:
    """The deck's saved file, as plain text to copy into Moxfield or Archidekt."""
    try:
        saved = decks.load(_decks_dir(), deck)
    except decks.DeckNameError:
        abort(400)
    return Response(decks.render(saved) if saved else "", mimetype="text/plain")


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
    # Copy anything saved while the app was down (or a push that never finished) to GitHub.
    pusher.request()
    run_simple("127.0.0.1", PORT, app, threaded=True, use_reloader=False, use_debugger=False)


if __name__ == "__main__":
    main()
