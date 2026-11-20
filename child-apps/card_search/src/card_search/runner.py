"""View, run and tweak Magic card searches (SQL over the local Scryfall copy with tags and rules-text regexes) that you work out with Claude in chat; cards show with the matching ability highlighted.

Services run from /home/user/workspace (the repo root). Conventions:

- Persistent state (anything written and read across runs -- cursors,
  caches, snapshots, user records): read and write it under ``DATA_DIR``
  (defined below), never a hardcoded ``data/.apps/card-search/`` at the
  call site. ``DATA_DIR`` defaults to ``data/.apps/card-search/`` but
  honors the ``CARD_SEARCH_DATA_DIR`` env var, so an editing agent can point a
  throwaway instance at a *copy* of the data instead of the live store
  (see the update-app skill). Do NOT use ``Path(__file__)``-based
  paths for state -- the bug to avoid is one process writing to
  ``/home/user/workspace/data/.apps/...`` while another reads from
  ``/home/user/workspace/system/apps/<pkg>/data/...``.
- Static assets shipped alongside this file (templates, default
  configs, bundled JSON): ``Path(__file__).parent / "assets/..."`` is
  fine and is the right pattern.
- Listen port: bind ``PORT`` (defined below), which defaults to this
  app's assigned port but honors the ``CARD_SEARCH_PORT`` env var, so
  an editing agent can boot a throwaway instance on a *spare* port
  alongside the live one (see the update-app skill). Never hardcode
  the port at the ``run_simple`` call.

This is a synchronous Flask app served by the threaded Werkzeug server.
The app owns its own browser origin (the forwarder routes
``http://card-search.<workspace-host>/`` straight to this port), so it serves
at ``/`` and root-absolute URLs, cookies, and service workers all work
unmodified -- nothing rewrites anything. Use ``flask_sock`` if you need
WebSockets.
"""

import base64
import hashlib
import json
import os
import re
from pathlib import Path
from typing import Literal

from app_schema.api_schema import (
    DEFAULT_API_SCHEMA_PATH,
    RouteModels,
    RouteTable,
    SchemalessRoute,
    register_api_schema_route,
)
from app_schema.routes import error_response, model_response, parse_request_body
from flask import Flask, Response, abort, request, send_file

from card_search import searches
from card_search.query import QueryError
from card_search.query import run as run_query
from imbue.imbue_common.frozen_model import FrozenModel
from imbue.imbue_common.logging import setup_logging
from loguru import logger
from pydantic import Field
from werkzeug.serving import run_simple

# Persistent state for this app lives under DATA_DIR. It defaults to
# ``data/.apps/card-search/`` but is overridable via the ``CARD_SEARCH_DATA_DIR`` env var
# so a throwaway instance can run against a *copy* of the data while editing --
# see the update-app skill. Always read/write state through DATA_DIR;
# never hardcode ``data/.apps/card-search/`` at a call site, or the override is
# bypassed. A writing call site should ``DATA_DIR.mkdir(parents=True,
# exist_ok=True)`` before writing.
DATA_DIR = Path(os.environ.get("CARD_SEARCH_DATA_DIR", "data/.apps/card-search"))

# The local Scryfall copy the scryfall-mirror routine refreshes daily (read only).
SCRYFALL_DB = Path(os.environ.get("CARD_SEARCH_SCRYFALL_DB", "data/.apps/deckbuilder/scryfall/scryfall.db"))

# Listen port. Defaults to this app's assigned port but is overridable via
# the ``CARD_SEARCH_PORT`` env var so an editing agent can boot a throwaway
# instance on a spare port next to the live one (see the update-app skill).
# Never hardcode the port at the ``run_simple`` call, or the override is bypassed.
PORT = int(os.environ.get("CARD_SEARCH_PORT", "8093"))

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


STATIC_DIR = Path(__file__).parent / "static"
STATIC_FILES = {"app.js": "text/javascript", "app.css": "text/css", "icon.svg": "image/svg+xml"}


def _searches_dir() -> Path:
    return DATA_DIR / "searches"


class HealthResponse(FrozenModel):
    """What the health probe answers."""

    status: str = Field(description="Always 'ok'")


class QuerySpec(FrozenModel):
    """One query of a search (usually one per tier)."""

    label: str = Field(description="Short name, e.g. 'Free'")
    description: str = Field(default="", description="One sentence on what the query finds")
    sql: str = Field(description="The SQL, returning a column named oracle_id", min_length=1, max_length=20000)
    highlight: str | None = Field(default=None, description="Python regex marking the matching line(s) of a card's rules text")
    by: str = Field(default="claude", description="Who wrote this version: 'claude' (the chat) or 'you' (an edit on the page)")


class SearchRecord(FrozenModel):
    """One saved search, as it is now."""

    id: str = Field(description="The search's id (lowercase letters, digits, dashes)")
    title: str = Field(description="What the search is for")
    notes: str = Field(description="The rules decisions behind it, what it includes and leaves out")
    queries: list[QuerySpec] = Field(description="The queries, in order")
    version: int = Field(description="Goes up by one on every change, from either side")
    updated_at: str = Field(description="When it last changed (UTC, ISO 8601)")
    updated_by: str = Field(description="Who made the last change: 'claude' or 'you'")
    agent_sql: list[str | None] = Field(description="Per query, the newest SQL the chat agent wrote (for going back to it)")


class SearchSummary(FrozenModel):
    """One line of the search list."""

    id: str = Field(description="The search's id")
    title: str = Field(description="What the search is for")
    version: int = Field(description="Its current version")
    updated_at: str = Field(description="When it last changed")
    updated_by: str = Field(description="Who made the last change")
    query_count: int = Field(description="How many queries it has")


class SearchList(FrozenModel):
    """Every search, most recently changed first."""

    searches: list[SearchSummary] = Field(description="The searches")


class SearchWrite(FrozenModel):
    """A whole search, as the chat agent writes it."""

    title: str = Field(description="What the search is for", min_length=1, max_length=500)
    notes: str = Field(default="", description="The rules decisions behind it", max_length=10000)
    queries: list[QuerySpec] = Field(description="The queries, in order", min_length=1, max_length=20)


class QueryEdit(FrozenModel):
    """The person's edit to one query."""

    sql: str = Field(description="The edited SQL", min_length=1, max_length=20000)
    highlight: str | None = Field(default=None, description="The highlight regex (unchanged unless edited)")


class RunRequest(FrozenModel):
    """A query to run against the local Scryfall copy."""

    sql: str = Field(description="One SELECT (or WITH ... SELECT) returning oracle_id", min_length=1, max_length=20000)
    highlight: str | None = Field(default=None, description="Python regex; lines of rules text it matches are marked")
    order: Literal["popular", "mv", "name"] = Field(
        default="popular",
        description="popular: most played first (EDHREC rank); mv: by mana value, most played first within each; name: A-Z",
    )


class CardOut(FrozenModel):
    """One matching card."""

    oracle_id: str = Field(description="Scryfall oracle id")
    name: str = Field(description="Card name")
    mana_cost: str = Field(description="Mana cost in {2}{U} notation")
    cmc: float | None = Field(description="Mana value")
    type_line: str = Field(description="Type line")
    oracle_text: str = Field(description="Rules text, one ability per line")
    highlight_lines: list[int] = Field(description="Indexes of the rules-text lines to mark: the lines the query matched (its line_no column) plus those the highlight regex matches")
    image: str | None = Field(description="Scryfall image link of the front face")
    image_back: str | None = Field(description="Scryfall image link of the back face")
    scryfall_uri: str | None = Field(description="The card's page on Scryfall")
    edhrec_rank: int | None = Field(description="EDHREC popularity rank (lower is more played)")


class RunResponse(FrozenModel):
    """The cards a query returned, most played first."""

    total: int = Field(description="How many distinct cards match")
    cards: list[CardOut] = Field(description="The matching cards (up to 1000)")
    seconds: float = Field(description="How long the query took")


ROUTE_TABLE: RouteTable = {
    ("GET", "/"): SchemalessRoute(reason="the app's page"),
    ("GET", "/static/{basename}"): SchemalessRoute(reason="the page's script, styles and icon"),
    ("GET", "/_static/{basename}"): SchemalessRoute(reason="a browser-side module the shell builds"),
    ("GET", "/health"): RouteModels(response_model=HealthResponse),
    ("POST", "/_csp-report"): SchemalessRoute(reason="the browser's report of what the page's policy blocked"),
    ("GET", "/api/searches"): RouteModels(response_model=SearchList),
    ("GET", "/api/searches/{search_id}"): RouteModels(response_model=SearchRecord),
    ("PUT", "/api/searches/{search_id}"): RouteModels(request_model=SearchWrite, response_model=SearchRecord),
    ("POST", "/api/searches/{search_id}/queries/{index}"): RouteModels(request_model=QueryEdit, response_model=SearchRecord),
    ("GET", "/api/searches/{search_id}/file"): SchemalessRoute(reason="the search's saved file, every version included"),
    ("POST", "/api/run"): RouteModels(request_model=RunRequest, response_model=RunResponse),
}

app = Flask("card_search", static_folder=None)
register_api_schema_route(
    app, DEFAULT_API_SCHEMA_PATH, title="AI Card Search",
    description="View, run and tweak Magic card searches (SQL over the local Scryfall copy with tags and rules-text regexes) that you work out with Claude in chat; cards show with the matching ability highlighted",
    route_table=ROUTE_TABLE,
)


@app.after_request
def add_content_security_policy(response: Response) -> Response:
    if response.mimetype == "text/html":
        response.headers.setdefault("Content-Security-Policy", CONTENT_SECURITY_POLICY)
    return response


def _record(raw: dict) -> SearchRecord:
    agent_sql = []
    for i in range(len(raw["queries"])):
        version = searches.agent_version(raw, i)
        agent_sql.append(version["sql"] if version else None)
    return SearchRecord(
        id=raw["id"], title=raw["title"], notes=raw.get("notes") or "",
        queries=[QuerySpec(**{k: q.get(k) for k in ("label", "description", "sql", "highlight", "by") if q.get(k) is not None})
                 for q in raw["queries"]],
        version=raw["version"], updated_at=raw["updated_at"], updated_by=raw["updated_by"], agent_sql=agent_sql,
    )


@app.route("/")
def index() -> Response:
    """The search page; ?s=<id> opens a saved search."""
    page = (STATIC_DIR / "index.html").read_text().replace("<!--SHELL_PAGE_SCRIPT-->", SHELL_PAGE_SCRIPT)
    return Response(page, mimetype="text/html")


@app.route("/static/<basename>")
def static_file(basename: str) -> Response:
    """The page's script, styles and icon."""
    if basename not in STATIC_FILES:
        abort(404)
    return send_file((STATIC_DIR / basename).absolute(), mimetype=STATIC_FILES[basename], max_age=0)


@app.route("/api/searches", methods=["GET"])
def list_searches() -> Response:
    """Every search, most recently changed first."""
    return model_response(SearchList(searches=[
        SearchSummary(id=r["id"], title=r["title"], version=r["version"], updated_at=r["updated_at"],
                      updated_by=r["updated_by"], query_count=len(r["queries"]))
        for r in searches.list_all(_searches_dir())
    ]))


@app.route("/api/searches/<search_id>", methods=["GET"])
def get_search(search_id: str) -> Response:
    """One search as it is now."""
    try:
        return model_response(_record(searches.read(_searches_dir(), search_id)))
    except searches.SearchNotFound:
        return error_response("no such search", 404)


@app.route("/api/searches/<search_id>", methods=["PUT"])
def put_search(search_id: str) -> Response:
    """Create or replace a whole search (the chat agent's write); the old version is kept."""
    body = parse_request_body(SearchWrite)
    queries = [q.model_dump(exclude={"by"}) for q in body.queries]
    try:
        record = searches.put(_searches_dir(), search_id, body.title, body.notes, queries)
    except searches.BadSearchId:
        return error_response("a search id is lowercase letters, digits and dashes", 400)
    return model_response(_record(record))


@app.route("/api/searches/<search_id>/queries/<int:index>", methods=["POST"])
def edit_query(search_id: str, index: int) -> Response:
    """Save the person's edit to one query; the old version is kept."""
    body = parse_request_body(QueryEdit)
    try:
        record = searches.edit_query(_searches_dir(), search_id, index, body.sql, body.highlight)
    except searches.SearchNotFound:
        return error_response("no such search", 404)
    except searches.NoSuchQuery:
        return error_response("no such query", 404)
    return model_response(_record(record))


@app.route("/api/searches/<search_id>/file")
def get_search_file(search_id: str) -> Response:
    """The search's saved file as it is on disk, every earlier version included."""
    try:
        record = searches.read(_searches_dir(), search_id)
    except searches.SearchNotFound:
        abort(404)
    return Response(json.dumps(record, indent=2), mimetype="application/json")


def _highlight_lines(pattern: re.Pattern[str] | None, text: str) -> list[int]:
    if pattern is None:
        return []
    return [i for i, line in enumerate(text.split("\n")) if pattern.search(line)]


@app.route("/api/run", methods=["POST"])
def run_sql() -> Response:
    """Run one query read-only over the local Scryfall copy."""
    body = parse_request_body(RunRequest)
    pattern = None
    if body.highlight:
        try:
            # Matched line by line, so a (?m) the agent added is harmless.
            pattern = re.compile(body.highlight)
        except re.error:
            pattern = None
    try:
        result = run_query(SCRYFALL_DB, body.sql, order=body.order)
    except QueryError as e:
        return error_response(str(e), 400)
    cards = [
        CardOut(
            oracle_id=c.oracle_id, name=c.name, mana_cost=c.mana_cost, cmc=c.cmc, type_line=c.type_line,
            oracle_text=c.oracle_text, highlight_lines=sorted(set(c.match_lines) | set(_highlight_lines(pattern, c.oracle_text))),
            image=c.image,
            image_back=c.image_back, scryfall_uri=c.scryfall_uri, edhrec_rank=c.edhrec_rank,
        )
        for c in result.cards
    ]
    return model_response(RunResponse(total=result.total, cards=cards, seconds=result.seconds))


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
    run_simple("127.0.0.1", PORT, app, threaded=True, use_reloader=False, use_debugger=False)


if __name__ == "__main__":
    main()
