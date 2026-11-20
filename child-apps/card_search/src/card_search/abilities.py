"""Every activated and loyalty ability of every deck card, split into cost and effect with its mana counted.

Built from the local Scryfall copy into a small side database (rebuilt when the Scryfall copy changes),
which every search connection attaches, so a search can say
``SELECT oracle_id FROM abilities WHERE mana = 0 AND effect REGEXP 'draw'`` instead of parsing rules text.

Table ``abilities``:
  oracle_id, name, line_no (the line's index in the card's rules text), line (the whole line),
  kind ('activated', 'loyalty', or 'granted' for an ability in quotes this card gives to something else), ability_word (e.g. 'Threshold', or NULL), cost, effect,
  mana (mana symbols in the cost: {2} is 2, {U} and hybrid are 1, Phyrexian 0; NULL when the cost has {X}),
  phyrexian (how many Phyrexian symbols), has_tap ({T} in the cost), permanent (1 when the card is a permanent),
  zone ('battlefield', or 'hand' / 'graveyard' when the cost discards, reveals or exiles the card itself from there),
  sorcery_speed (1 when the ability can only be activated as a sorcery; loyalty abilities always are),
  granted_to (for kind 'granted': 'equipped', 'enchanted', 'token' (a token the card makes) or 'other'
  (e.g. "Creatures you control have ..."); NULL for the card's own abilities).
Reminder text in parentheses is ignored, so keywords like Equip or Cycling only appear when written out.
A modal ability ("Choose one —") has its bullet-line modes appended to its effect.
"""

import os
import re
import sqlite3
import threading
from pathlib import Path

_SKIP_LAYOUTS = ("token", "double_faced_token", "emblem", "art_series", "vanguard", "scheme", "planar")
_PERMANENT = re.compile(r"\b(Artifact|Creature|Enchantment|Land|Planeswalker|Battle)\b")
_LOYALTY = re.compile(r"^([+−-]?(?:\d+|X)): (.+)$")
_ABILITY_WORD = re.compile(r"^([A-Z][\w'’ ,!?-]{1,40}) — (.+)$")
_REMINDER = re.compile(r" ?\([^)]*\)")
_FROM_HAND = re.compile(r"(Discard|Reveal) this card|this card from your hand")
_FROM_GRAVEYARD = re.compile(r"this card from your graveyard")
_SORCERY_SPEED = re.compile(r"(?i)\bonly as a sorcery|\bonly any time you could cast a sorcery|\bactivate only during your main phase|\bonly during your main phase")
# A trigger, a static ability or reminder text, never an activated ability's cost.
_NOT_COST = re.compile(r"^(When|Whenever|At |As |If |Each |Choose|You may|Until|During|Once|Then|Only|This |The |Other |Creatures|•|\()")
_SYMBOL = re.compile(r"\{([^}]+)\}")
_QUOTED = re.compile(r"[\"“]([^\"”]+)[\"”]")
_lock = threading.Lock()


def _mana(cost: str) -> tuple[int | None, int]:
    total, phyrexian, has_x = 0, 0, False
    for sym in _SYMBOL.findall(cost):
        if sym in ("T", "Q", "E"):
            continue
        if sym.endswith("/P"):
            phyrexian += 1
        elif sym in ("X", "Y", "Z"):
            has_x = True
        elif sym.isdigit():
            total += int(sym)
        elif re.fullmatch(r"2/[WUBRG]", sym):
            total += 2
        else:
            total += 1
    return (None if has_x else total), phyrexian


def parse_line(line: str) -> dict | None:
    """The ability on one line of rules text, or None when the line is not an activated or loyalty ability."""
    text = _REMINDER.sub("", line).strip()
    loyalty = _LOYALTY.match(text)
    if loyalty:
        return {"kind": "loyalty", "ability_word": None, "cost": loyalty.group(1), "effect": loyalty.group(2),
                "mana": 0, "phyrexian": 0, "has_tap": 0, "zone": "battlefield", "sorcery_speed": 1}
    word = None
    named = _ABILITY_WORD.match(text)
    if named and ": " in named.group(2):
        word, text = named.group(1), named.group(2)
    if ": " not in text:
        return None
    cost, effect = text.split(": ", 1)
    if '"' in cost or "“" in cost or "." in cost or len(cost) > 160 or _NOT_COST.match(cost):
        return None
    if "{" not in cost and not re.match(r"^(Sacrifice|Discard|Remove|Pay|Exile|Tap|Untap|Return|Put|Reveal|Mill|Exert|Collect|Forage|Behold|Blight|Waterbend|Earthbend|Airbend|Spend|Turn)\b", cost):
        return None
    mana, phyrexian = _mana(cost)
    from_graveyard = _FROM_GRAVEYARD.search(cost) or effect.startswith("Return this card from your graveyard")
    zone = "hand" if _FROM_HAND.search(cost) else "graveyard" if from_graveyard else "battlefield"
    return {"kind": "activated", "ability_word": word, "cost": cost, "effect": effect, "mana": mana,
            "phyrexian": phyrexian, "has_tap": int("{T}" in cost), "zone": zone,
            "sorcery_speed": int(bool(_SORCERY_SPEED.search(effect)))}


def _granted_to(before: str) -> str:
    """Who gets a quoted ability, from the text before the quote."""
    if re.search(r"(?i)\bequipped\b", before):
        return "equipped"
    if re.search(r"(?i)\benchanted\b", before):
        return "enchanted"
    if re.search(r"(?i)\btokens?\b", before):
        return "token"
    return "other"


def granted_abilities(line: str) -> list[dict]:
    """Activated abilities quoted in a line ('Equipped creature has "{T}: Draw a card."'), as given to something else."""
    found = []
    for match in _QUOTED.finditer(_REMINDER.sub("", line)):
        inner = match.group(1)
        # An emblem's text can quote an ability in single quotes: "Islands you control have '{T}: Draw a card.'"
        nested = re.search(r"'(\{[^']*: [^']*)'", inner)
        ability = parse_line(nested.group(1) if nested else inner)
        if ability and ability["kind"] == "activated":
            ability.update(kind="granted", granted_to=_granted_to(line[: match.start()]))
            found.append(ability)
    return found


def _build(scryfall_db: Path, target: Path) -> None:
    src = sqlite3.connect(f"file:{scryfall_db}?mode=ro", uri=True)
    rows = src.execute(
        "SELECT o.oracle_id, c.name, c.type_line, c.oracle_text FROM oracle_cards o JOIN cards c ON c.id = o.card_id "
        f"WHERE c.layout NOT IN ({','.join('?' * len(_SKIP_LAYOUTS))})", _SKIP_LAYOUTS,
    ).fetchall()
    src.close()
    tmp = target.with_name(target.name + ".tmp")
    tmp.unlink(missing_ok=True)
    out = sqlite3.connect(tmp)
    out.execute(
        "CREATE TABLE abilities (oracle_id TEXT, name TEXT, line_no INTEGER, line TEXT, kind TEXT, ability_word TEXT, "
        "cost TEXT, effect TEXT, mana INTEGER, phyrexian INTEGER, has_tap INTEGER, permanent INTEGER, zone TEXT, sorcery_speed INTEGER, granted_to TEXT)"
    )
    batch = []
    for oracle_id, name, type_line, oracle_text in rows:
        permanent = int(bool(_PERMANENT.search(type_line or "")))
        lines = (oracle_text or "").split("\n")
        for line_no, line in enumerate(lines):
            ability = parse_line(line)
            if ability and ability["effect"].rstrip().endswith("—"):
                # "Choose one —" with its modes on the following bullet lines: they are part of this ability.
                modes = []
                for following in lines[line_no + 1:]:
                    if not following.startswith("•"):
                        break
                    modes.append(_REMINDER.sub("", following).strip())
                ability["effect"] = " ".join([ability["effect"].rstrip(), *modes])
            for found in ([ability] if ability else []) + granted_abilities(line):
                batch.append((oracle_id, name, line_no, line, found["kind"], found["ability_word"], found["cost"],
                              found["effect"], found["mana"], found["phyrexian"], found["has_tap"], permanent,
                              found["zone"], found["sorcery_speed"], found.get("granted_to")))
    out.executemany("INSERT INTO abilities VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)", batch)
    out.execute("CREATE INDEX abilities_oracle ON abilities(oracle_id)")
    out.execute("CREATE INDEX abilities_mana ON abilities(mana)")
    out.commit()
    out.close()
    os.replace(tmp, target)


def ensure(scryfall_db: Path, target: Path) -> Path:
    """The abilities database, rebuilt first when missing or older than the Scryfall copy."""
    with _lock:
        newest_input = max(scryfall_db.stat().st_mtime, Path(__file__).stat().st_mtime)
        if not target.exists() or target.stat().st_mtime < newest_input:
            target.parent.mkdir(parents=True, exist_ok=True)
            _build(scryfall_db, target)
    return target
