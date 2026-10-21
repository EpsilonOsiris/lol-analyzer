"""OP.GG's official MCP endpoint (https://mcp-api.op.gg/mcp, public, no key): lane matchups and synergies.

What was verified against the live endpoint (October 2026) and why the code does what it does
-----------------------------------------------------------------------------------------------
* Transport: Streamable HTTP JSON-RPC. `initialize` (protocolVersion "2025-03-26") answers with the
  header `mcp-session-id`; then `notifications/initialized`, then `tools/call`. Answers were plain
  JSON; the client also reads `text/event-stream` (`data:` lines).
* Tool results are text in two shapes: plain JSON (`lol_get_lane_matchup_guide`) or a compact
  "class" notation (`lol_get_champion_analysis`, `lol_get_champion_synergies`,
  `lol_list_lane_meta_champions`, `lol_list_champions`) whose field order comes from the
  `class Name: a,b,c` header lines. `parse_tool_text` reads both.
* A matchup "my champion vs that opponent in this lane": `lol_get_lane_matchup_guide(position,
  my_champion, opponent_champion)` returns `data.counters[]` = {champion_id, champion_name, play,
  win}. The entry whose champion is the OPPONENT asked for is the pair: `play` games and `win`
  wins OF `my_champion` (the first one). `lol_get_champion_analysis` only has the 3 best and 3
  worst counters (`strong_counters` / `weak_counters`), so it cannot answer for an arbitrary pair.
* Perspective, checked by symmetry on the live data: JINX vs LUCIAN = 102 of 193 wins and
  LUCIAN vs JINX = 91 of 193 (102 + 91 = 193 exactly); CAITLYN vs ASHE 136/278 (48.9%) against
  ASHE vs CAITLYN 63/124 (50.8% for Ashe, so 49.2% for Caitlyn); JINX vs EZREAL 292/570 (51.2%)
  against EZREAL vs JINX 143/283 (50.5%). The sample sizes of the two directions are not always
  identical (the server returns a smaller table for some opponents) but the rates agree.
* Tiers: `lol_get_lane_matchup_guide` has NO tier parameter (a `tier` argument is ignored: same
  answer). `lol_get_champion_analysis` does accept `tier` (emerald, challenger, ...; "all" or
  omitted = every tier) but only for its top-3 counter lists. So pair matchups are always the
  all-tier aggregate; the panel says so ("todos los rangos").
* Champion names: every tool wants UPPER_SNAKE_CASE of the ENGLISH display name from
  `lol_list_champions` (`name`): "Kai'Sa" -> KAISA, "Lee Sin" -> LEE_SIN, "Nunu & Willump" ->
  NUNU_WILLUMP, "Dr. Mundo" -> DR_MUNDO, "Renata Glasc" -> RENATA_GLASC, "Wukong" -> WUKONG.
  The Data Dragon id (MonkeyKing, LeeSin) is rejected ("Invalid position or champion").
  `lol_list_champions` also gives `champion_id` = Riot's numeric champion id (Spectator), so the
  names are found from the id without Data Dragon. (Entries with ids >= 60000 are skins.)
* Synergy: `lol_get_champion_synergies(champion=ADC, my_position=adc, synergy_position=support)`
  lists the ~10 most played supports with `play` and `win` of the duo (names, not ids).
* Roles: `lol_list_lane_meta_champions` has, per lane, each champion's `role_rate` (share of its
  games played in that lane) and `play`, by English name.

Every answer is cached in SQLite (`opgg_cache`) for 24 h and dropped when the patch changes. Any
failure raises `OpggError`; callers turn it into "Sin datos de OP.GG" without breaking the scout.
"""
from __future__ import annotations

import itertools
import json
import re
import threading
import time

import requests

MCP_URL = "https://mcp-api.op.gg/mcp"
PROTOCOL = "2025-03-26"
TIMEOUT_S = 10.0
MAX_PARALLEL = 2
DOWN_PAUSE_S = 60.0              # after a transport failure, do not try again for this long
CACHE_TTL_S = 24 * 3600
POSITIONS = ("top", "jungle", "mid", "adc", "support")
UNKNOWN = "unknown"              # cached "OP.GG has no row for this pair"
_HEADERS = {"Content-Type": "application/json", "Accept": "application/json, text/event-stream"}


class OpggError(Exception):
    """OP.GG could not answer (network, protocol, unexpected format)."""


class OpggRejected(OpggError):
    """OP.GG answered but refused the arguments (unknown champion / position)."""


# ------------------------------------------------------------------- parsing
_CLASS_LINE = re.compile(r"^\s*class\s+(\w+)\s*:\s*(.*)$")
_IDENT = re.compile(r"[A-Za-z_][A-Za-z0-9_]*")
_NUMBER = re.compile(r"-?\d+(?:\.\d+)?(?:[eE][-+]?\d+)?")


class _Reader:
    """Recursive-descent reader of `Name(arg, [..], "text", 1.5)` with the field names of `classes`."""

    def __init__(self, text: str, classes: dict):
        self.s, self.i, self.classes = text, 0, classes

    def _ws(self):
        while self.i < len(self.s) and self.s[self.i] in " \t\r\n":
            self.i += 1

    def _peek(self):
        self._ws()
        return self.s[self.i] if self.i < len(self.s) else ""

    def _expect(self, ch):
        if self._peek() != ch:
            raise OpggError(f"formato inesperado de OP.GG (se esperaba {ch!r} en la posición {self.i})")
        self.i += 1

    def value(self):
        ch = self._peek()
        if ch == '"':
            return self._string()
        if ch == "[":
            self.i += 1
            items = []
            while self._peek() != "]":
                if not self._peek():
                    raise OpggError("formato inesperado de OP.GG (lista sin cerrar)")
                items.append(self.value())
                if self._peek() == ",":
                    self.i += 1
            self.i += 1
            return items
        m = _NUMBER.match(self.s, self.i)
        if m and (ch == "-" or ch.isdigit()):
            self.i = m.end()
            text = m.group(0)
            return float(text) if any(c in text for c in ".eE") else int(text)
        m = _IDENT.match(self.s, self.i)
        if not m:
            raise OpggError(f"formato inesperado de OP.GG (posición {self.i})")
        name = m.group(0)
        self.i = m.end()
        if self._peek() == "(":
            self.i += 1
            args = []
            while self._peek() != ")":
                if not self._peek():
                    raise OpggError("formato inesperado de OP.GG (paréntesis sin cerrar)")
                args.append(self.value())
                if self._peek() == ",":
                    self.i += 1
            self.i += 1
            fields = self.classes.get(name)
            if fields is None:
                return args
            out = {f: args[k] for k, f in enumerate(fields) if k < len(args)}
            for k in range(len(fields), len(args)):
                out[f"_{k}"] = args[k]
            return out
        return {"true": True, "false": False, "null": None, "None": None, "True": True, "False": False}.get(name, name)

    def _string(self):
        start = self.i
        self.i += 1
        while self.i < len(self.s):
            ch = self.s[self.i]
            if ch == "\\":
                self.i += 2
                continue
            if ch == '"':
                self.i += 1
                raw = self.s[start:self.i]
                try:
                    return json.loads(raw)
                except ValueError:
                    return raw[1:-1]
            self.i += 1
        raise OpggError("formato inesperado de OP.GG (texto sin cerrar)")


def parse_compact(text: str):
    """Value of OP.GG's compact notation: `class` header lines, then one `Name(...)` expression."""
    classes: dict = {}
    body = []
    for line in str(text).splitlines():
        m = _CLASS_LINE.match(line)
        if m:
            classes[m.group(1)] = [f.strip() for f in m.group(2).split(",") if f.strip()]
        elif line.strip():
            body.append(line)
    if not body:
        raise OpggError("OP.GG no devolvió datos")
    reader = _Reader("\n".join(body), classes)
    value = reader.value()
    if reader._peek() or not isinstance(value, (dict, list)):
        raise OpggError("OP.GG devolvió un formato desconocido")
    return value


def parse_tool_text(text):
    """Python value of a tool result: plain JSON or the compact class notation."""
    text = str(text or "").strip()
    if not text:
        raise OpggError("OP.GG no devolvió datos")
    if text[0] in "{[":
        try:
            return json.loads(text)
        except ValueError as exc:
            raise OpggError(f"JSON inválido de OP.GG: {exc}") from exc
    return parse_compact(text)


def _decode_body(resp):
    """The JSON-RPC message of an HTTP answer (JSON or server-sent events); None if empty."""
    text = (getattr(resp, "text", "") or "").strip()
    if not text:
        return None
    ctype = str((getattr(resp, "headers", None) or {}).get("content-type", "")).lower()
    if "event-stream" in ctype or text.startswith(("event:", "data:", ":")):
        message = None
        for line in text.splitlines():
            if line.startswith("data:"):
                try:
                    message = json.loads(line[5:].strip())
                except ValueError:
                    continue
        return message
    try:
        return json.loads(text)
    except ValueError as exc:
        raise OpggError("OP.GG respondió algo que no es JSON") from exc


# -------------------------------------------------------------------- client
class McpClient:
    """Minimal MCP client: one shared session, at most `max_parallel` calls at once.

    `session` is anything with `.post(url, json=, headers=, timeout=)` (a `requests.Session`);
    tests pass a scripted fake. The session is created again when the server forgets it (4xx or a
    JSON-RPC error that mentions the session) and after a transport failure the client rests for
    DOWN_PAUSE_S so a dead OP.GG never slows the scout down.
    """

    def __init__(self, url=MCP_URL, session=None, timeout=TIMEOUT_S, max_parallel=MAX_PARALLEL,
                 clock=time.monotonic):
        self.url, self.timeout, self._clock = url, timeout, clock
        self._http = session if session is not None else requests.Session()
        self._sid: str | None = None
        self._ready = False
        self._init_lock = threading.Lock()
        self._sem = threading.BoundedSemaphore(max_parallel)
        self._ids = itertools.count(1)
        self._down_until = 0.0
        self.calls = 0                       # tools/call requests made (for the budget report)
        self.initializations = 0

    # ---- low level
    def _post(self, body: dict, sid: str | None):
        headers = dict(_HEADERS)
        if sid:
            headers["mcp-session-id"] = sid
        try:
            return self._http.post(self.url, json=body, headers=headers, timeout=self.timeout)
        except (requests.RequestException, OSError) as exc:
            self._down_until = self._clock() + DOWN_PAUSE_S
            raise OpggError(f"No se pudo hablar con OP.GG: {type(exc).__name__}") from exc

    def _session(self) -> str | None:
        with self._init_lock:
            if self._ready:
                return self._sid
            resp = self._post({"jsonrpc": "2.0", "id": next(self._ids), "method": "initialize", "params": {
                "protocolVersion": PROTOCOL, "capabilities": {},
                "clientInfo": {"name": "lol-analyzer", "version": "1"}}}, None)
            if resp.status_code >= 400:
                self._down_until = self._clock() + DOWN_PAUSE_S
                raise OpggError(f"OP.GG rechazó la conexión (HTTP {resp.status_code})")
            message = _decode_body(resp)
            if isinstance(message, dict) and message.get("error"):
                raise OpggError("OP.GG rechazó la conexión")
            headers = getattr(resp, "headers", None) or {}
            sid = headers.get("mcp-session-id") or headers.get("Mcp-Session-Id")
            self._sid = sid
            self._post({"jsonrpc": "2.0", "method": "notifications/initialized"}, sid)
            self._ready = True
            self.initializations += 1
            return sid

    def reset(self) -> None:
        with self._init_lock:
            self._ready, self._sid = False, None

    def available(self) -> bool:
        return self._clock() >= self._down_until

    # ---- tools
    def call(self, tool: str, arguments: dict) -> str:
        """Text of the tool's answer. OpggError on any failure (OpggRejected if it refused the arguments)."""
        if not self.available():
            raise OpggError("OP.GG no responde; se vuelve a intentar en un minuto")
        with self._sem:
            for attempt in (0, 1):
                sid = self._session()
                resp = self._post({"jsonrpc": "2.0", "id": next(self._ids), "method": "tools/call",
                                   "params": {"name": tool, "arguments": arguments}}, sid)
                self.calls += 1
                if 400 <= resp.status_code < 500 and attempt == 0:      # expired or unknown session
                    self.reset()
                    continue
                if resp.status_code >= 400:
                    if resp.status_code >= 500:
                        self._down_until = self._clock() + DOWN_PAUSE_S
                    raise OpggError(f"OP.GG respondió HTTP {resp.status_code}")
                message = _decode_body(resp)
                if not isinstance(message, dict):
                    raise OpggError("OP.GG no devolvió respuesta")
                error = message.get("error")
                if error:
                    text = str((error or {}).get("message") or error)
                    if attempt == 0 and "session" in text.lower():
                        self.reset()
                        continue
                    if "invalid" in text.lower():
                        raise OpggRejected(text)
                    raise OpggError(text)
                result = message.get("result") or {}
                parts = [c.get("text", "") for c in result.get("content") or []
                         if isinstance(c, dict) and c.get("type", "text") == "text"]
                text = "\n".join(parts)
                if result.get("isError"):
                    raise OpggRejected(text or "OP.GG devolvió un error")
                return text
        raise OpggError("OP.GG no mantuvo la sesión")


# --------------------------------------------------------------- name helpers
def champion_arg(name_en: str) -> str:
    """OP.GG's UPPER_SNAKE_CASE for an English display name (Kai'Sa -> KAISA, Lee Sin -> LEE_SIN)."""
    words = re.sub(r"[^A-Za-z0-9 ]+", "", str(name_en or "")).split()
    return "_".join(w.upper() for w in words)


def patch_of(version) -> str | None:
    """"16.21.1" -> "16.21" (the cache is valid for one patch)."""
    m = re.match(r"^(\d+)\.(\d+)", str(version or ""))
    return f"{m.group(1)}.{m.group(2)}" if m else None


def _norm(name) -> str:
    return re.sub(r"[^a-z0-9]", "", str(name or "").casefold())


def _num(v, default=0):
    return v if isinstance(v, (int, float)) and not isinstance(v, bool) else default


# ----------------------------------------------------------------- the data
class OpggData:
    """The few things the panel asks OP.GG, cached in SQLite (`store_factory() -> Store`)."""

    def __init__(self, client: McpClient, store_factory, patch: str | None = None, clock=time.time,
                 ttl=CACHE_TTL_S):
        self.client, self._store_factory, self.patch = client, store_factory, patch
        self._clock, self.ttl = clock, ttl
        self._memo: dict = {}
        self._lock = threading.Lock()

    # ---- cache
    def _cached(self, key: str, fetch):
        """Value of `fetch()` remembered 24 h (and until the patch changes) under `key`."""
        with self._lock:
            if key in self._memo:
                return self._memo[key]
        store = self._store_factory()
        try:
            hit = store.get_opgg_cache(key, self.ttl, self.patch, self._clock())
        finally:
            store.close()
        if hit is not None:
            value = hit["v"]
        else:
            value = fetch()
            store = self._store_factory()
            try:
                store.put_opgg_cache(key, value, self.patch, self._clock())
            finally:
                store.close()
        with self._lock:
            self._memo[key] = value
        return value

    # ---- champions
    def champions(self) -> dict:
        """{"by_id": {id: english name}} from lol_list_champions."""
        def fetch():
            text = self.client.call("lol_list_champions", {"desired_output_fields": [
                "data.champions[].champion_id", "data.champions[].key", "data.champions[].name"]})
            rows = ((parse_tool_text(text) or {}).get("data") or {}).get("champions") or []
            by_id = {}
            for row in rows:
                cid = row.get("champion_id") if isinstance(row, dict) else None
                if isinstance(cid, int) and 0 < cid < 60000 and row.get("name"):
                    by_id[str(cid)] = row["name"]
            if not by_id:
                raise OpggError("OP.GG no devolvió la lista de campeones")
            return by_id
        return {"by_id": self._cached("champions", fetch)}

    def name_of(self, champion_id) -> str | None:
        return self.champions()["by_id"].get(str(champion_id))

    # ---- lane meta (role rates)
    def lane_meta(self) -> dict:
        """{normalized english name: {"top": rate, ...}}: share of each champion's games per lane."""
        def fetch():
            fields = [f"data.positions.{p}[].{x}" for p in POSITIONS for x in ("champion", "play", "role_rate")]
            text = self.client.call("lol_list_lane_meta_champions", {"position": "all", "desired_output_fields": fields})
            positions = ((parse_tool_text(text) or {}).get("data") or {}).get("positions") or {}
            out: dict = {}
            for pos in POSITIONS:
                for row in positions.get(pos) or []:
                    if isinstance(row, dict) and row.get("champion"):
                        out.setdefault(_norm(row["champion"]), {})[pos] = float(_num(row.get("role_rate"), 0.0))
            if not out:
                raise OpggError("OP.GG no devolvió los roles de los campeones")
            return out
        return self._cached("lanemeta", fetch)

    # ---- matchups and synergies
    def matchup(self, position: str, my_id: int, opp_id: int) -> dict | None:
        """{"games", "wins"} of my champion against the opponent in `position`, or None (no row)."""
        mine, theirs = self.name_of(my_id), self.name_of(opp_id)
        if not mine or not theirs:
            return None

        def fetch():
            try:
                text = self.client.call("lol_get_lane_matchup_guide", {
                    "position": position, "my_champion": champion_arg(mine), "opponent_champion": champion_arg(theirs),
                    "lang": "en_US"})
            except OpggRejected:
                return UNKNOWN
            counters = (((parse_tool_text(text) or {}).get("data") or {}).get("counters")) or []
            for row in counters:
                if isinstance(row, dict) and (row.get("champion_id") == opp_id or _norm(row.get("champion_name")) == _norm(theirs)):
                    games, wins = int(_num(row.get("play"))), int(_num(row.get("win")))
                    if games > 0:
                        return {"games": games, "wins": min(wins, games)}
            return UNKNOWN
        found = self._cached(f"mu:{position}:{my_id}:{opp_id}", fetch)
        return None if found == UNKNOWN else found

    def synergy(self, adc_id: int, support_id: int) -> dict | None:
        """{"games", "wins"} of the ADC + support duo (from the ADC's list of most played supports)."""
        mine, theirs = self.name_of(adc_id), self.name_of(support_id)
        if not mine or not theirs:
            return None

        def fetch():
            fields = [f"data.synergies[].{x}" for x in ("champion_name", "synergy_champion_name", "play", "win")]
            try:
                text = self.client.call("lol_get_champion_synergies", {
                    "champion": champion_arg(mine), "my_position": "adc", "synergy_position": "support",
                    "lang": "en_US", "desired_output_fields": fields})
            except OpggRejected:
                return []
            rows = ((parse_tool_text(text) or {}).get("data") or {}).get("synergies") or []
            return [{"name": r.get("synergy_champion_name"), "games": int(_num(r.get("play"))), "wins": int(_num(r.get("win")))}
                    for r in rows if isinstance(r, dict) and r.get("synergy_champion_name")]
        for row in self._cached(f"syn:adc:{adc_id}", fetch):
            if _norm(row.get("name")) == _norm(theirs) and row.get("games", 0) > 0:
                return {"games": row["games"], "wins": min(row["wins"], row["games"])}
        return None
