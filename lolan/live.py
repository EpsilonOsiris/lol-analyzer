"""Live game ("En vivo"): reads the Live Client Data API the game client serves on this PC.

While a game runs, the League client answers on https://127.0.0.1:2999/liveclientdata/ with what
the player sees on the scoreboard (TAB), the kill feed and their own stats. Nothing here leaves
the PC and no API key is involved.

What is deliberately NOT read or shown (Riot's policy: no information that gives a competitive edge
and no apps that tell the player what to do): enemy summoner spell / ultimate cooldowns, objective
or inhibitor respawn timers, and any alert that tells the player to act.

This module has three parts:
  * LiveClient: the HTTPS client (certificate checked against Riot's root, see make_ssl_context).
  * normalize / enrich: pure functions that turn the raw JSON into the compact state the page uses.
  * LivePoller: a daemon thread that polls the client and keeps the latest state.
"""
from __future__ import annotations

import copy
import json
import os
import ssl
import sys
import threading
import warnings
import time
from pathlib import Path
from urllib.parse import urlsplit

import requests
from requests.adapters import HTTPAdapter
from urllib3.exceptions import InsecureRequestWarning

from .goldmodel import unspent_estimate

DEFAULT_URL = "https://127.0.0.1:2999"
# FOR TESTS ONLY (automated tests and the local smoke test with a fake game server). Normal use
# never sets it. Only a loopback address is honoured, so it can never point the panel elsewhere.
TEST_URL_ENV = "LOLAN_TEST_LIVE_URL"
CA_FILE = Path(__file__).resolve().parent / "certs" / "riotgames.pem"
LOOPBACK_HOSTS = {"127.0.0.1", "localhost", "::1"}

TIMEOUT_S = 1.0
FAST_S = 1.0          # in a game with someone watching the page
IDLE_S = 5.0          # no game, or nobody watching
LOADING_S = 2.0       # the client answers but the game has not started (loading screen)
DOWN_AFTER = 3        # failed polls in a row before a running game counts as over
FEED_MAX = 40
CHECKPOINT_MINUTES = (7, 14, 21, 28)
CHECKPOINT_WINDOW_S = 20

ROLE_ORDER = ["TOP", "JUNGLE", "MIDDLE", "BOTTOM", "UTILITY"]
ROLE_LABELS = {"TOP": "Top", "JUNGLE": "Jungla", "MIDDLE": "Mid", "BOTTOM": "ADC", "UTILITY": "Soporte"}
MODE_LABELS = {
    "CLASSIC": "Clásico", "ARAM": "ARAM", "PRACTICETOOL": "Herramienta de práctica",
    "TUTORIAL": "Tutorial", "URF": "URF", "ARURF": "ARURF", "ONEFORALL": "Uno para todos",
    "ULTBOOK": "Libro de ultis", "NEXUSBLITZ": "Nexus Blitz", "CHERRY": "Arena", "STRAWBERRY": "Ruptura",
}
MAP_LABELS = {"Map11": "Grieta del Invocador", "Map12": "Abismo de los Lamentos",
              "Map21": "Nexus Blitz", "Map22": "Arena", "Map30": "Arena"}
DRAGON_LABELS = {"Fire": "Infernal", "Earth": "Montaña", "Water": "Océano", "Air": "Nube",
                 "Hextech": "Hextech", "Chemtech": "Quimtech", "Elder": "Anciano"}
MULTIKILL_LABELS = {2: "Doble kill", 3: "Triple kill", 4: "Cuádruple kill", 5: "Penta kill"}
STAT_KEYS = {  # championStats key -> (label, decimals, suffix)
    "maxHealth": ("Vida", 0, ""), "attackDamage": ("Daño de ataque", 0, ""),
    "abilityPower": ("Poder de habilidad", 0, ""), "armor": ("Armadura", 0, ""),
    "magicResist": ("Resistencia mágica", 0, ""), "attackSpeed": ("Velocidad de ataque", 2, ""),
    "critChance": ("Prob. de crítico", 0, "%"), "abilityHaste": ("Celeridad de habilidad", 0, ""),
    "moveSpeed": ("Velocidad de movimiento", 0, ""),
}
RAW_CHAMPION_PREFIX = "game_character_displayname_"


def fmt_clock(seconds) -> str:
    try:
        total = int(max(0, float(seconds)))
    except (TypeError, ValueError):
        total = 0
    return f"{total // 60}:{total % 60:02d}"


# ------------------------------------------------------------------ TLS + client
def _stdlib_context_class():
    """The standard-library SSLContext class, even when truststore.inject_into_ssl() replaced it.

    The panel injects truststore (so antivirus roots work for Riot's web API), but truststore
    verifies with the operating system store and ignores certificate files. The game client's
    certificate is only trusted by Riot's own root (lolan/certs/riotgames.pem), so we need the
    original class.
    """
    for cls in ssl.SSLContext.__mro__:
        if cls.__module__ == "ssl" and cls.__name__ == "SSLContext":
            return cls
    return ssl.SSLContext


_StdContext = _stdlib_context_class()


class _LoopbackContext(_StdContext):
    """The standard context with setters that survive truststore.inject_into_ssl().

    The stdlib setters call `super(SSLContext, SSLContext)`, which recurses forever once the name
    `ssl.SSLContext` points at truststore's subclass. These write straight to the C implementation.
    """

    def _raw_setter(name):
        base = getattr(_StdContext, name)

        def setter(self, value):
            getattr(ssl._SSLContext, name).__set__(self, value)
        return base.setter(setter)

    verify_mode = _raw_setter("verify_mode")
    verify_flags = _raw_setter("verify_flags")
    options = _raw_setter("options")
    minimum_version = _raw_setter("minimum_version")
    maximum_version = _raw_setter("maximum_version")
    del _raw_setter


def make_ssl_context(ca_file=CA_FILE) -> ssl.SSLContext:
    """Context that trusts ONLY `ca_file`: the certificate chain is verified, the name is not.

    The game client's certificate is not issued for "127.0.0.1", so the host name check is off;
    the chain check (CERT_REQUIRED against Riot's root) stays on.
    """
    ctx = _LoopbackContext(ssl.PROTOCOL_TLS_CLIENT)
    ctx.check_hostname = False
    ctx.verify_mode = ssl.CERT_REQUIRED
    ctx.load_verify_locations(cafile=str(ca_file))
    return ctx


class LoopbackAdapter(HTTPAdapter):
    """Adapter for the local game client: Riot's root only, no host-name check."""

    def __init__(self, ssl_context, **kwargs):
        self._ssl_context = ssl_context
        super().__init__(**kwargs)

    def init_poolmanager(self, *args, **kwargs):
        kwargs["ssl_context"] = self._ssl_context
        kwargs["assert_hostname"] = False
        super().init_poolmanager(*args, **kwargs)

    def cert_verify(self, conn, url, verify, cert):
        super().cert_verify(conn, url, verify, cert)
        conn.ca_certs = None          # requests would add certifi's bundle: only Riot's root counts
        conn.ca_cert_dir = None


def live_base_url() -> str:
    """https://127.0.0.1:2999, or the test override when it is a loopback address."""
    override = os.environ.get(TEST_URL_ENV, "").strip()
    if override:
        parts = urlsplit(override)
        if parts.scheme in ("http", "https") and parts.hostname in LOOPBACK_HOSTS:
            return override.rstrip("/")
    return DEFAULT_URL


def make_system_ssl_context() -> ssl.SSLContext:
    """Fallback context for PCs where an antivirus re-signs even local TLS (Norton does).

    The chain is still verified, against the operating system store (which trusts the antivirus
    root), and the host name is still not checked. Used only for the loopback address, and only
    after the check against Riot's own root failed.
    """
    import truststore

    ctx = truststore.SSLContext(ssl.PROTOCOL_TLS_CLIENT)
    ctx.check_hostname = False
    ctx.verify_mode = ssl.CERT_REQUIRED
    return ctx


def make_unverified_loopback_context() -> ssl.SSLContext:
    """Last resort for the game on 127.0.0.1 when no store accepts the certificate.

    Norton re-signs even loopback TLS, and when it can't validate the game's self-signed
    certificate it signs it with its "Untrusted Root", which neither Riot's root nor Windows
    trust. Riot's docs allow ignoring the certificate for this local API; the connection never
    leaves the PC (LiveClient refuses non-loopback addresses), so this is only ever used there.
    """
    ctx = _LoopbackContext(ssl.PROTOCOL_TLS_CLIENT)
    ctx.check_hostname = False
    ctx.verify_mode = ssl.CERT_NONE
    return ctx


class LiveClient:
    """Fetches /liveclientdata/allgamedata. Never raises: returns (kind, data).

    TLS: first the chain is checked against Riot's root (lolan/certs/riotgames.pem). If that is
    refused (an antivirus in between replaced the certificate), the operating system store is
    tried, and as a last resort the certificate is not checked (loopback only, see
    make_unverified_loopback_context); `tls_mode` says which one worked.
    """

    def __init__(self, base_url=None, ca_file=CA_FILE, timeout=TIMEOUT_S, system_fallback=True,
                 unverified_fallback=True):
        self.base_url = (base_url or live_base_url()).rstrip("/")
        self.timeout = timeout
        self.problem: str | None = None       # "tls" when the certificate was refused
        # "riot" | "system" | "unverified" | None (plain http / not yet connected)
        self.tls_mode: str | None = None
        self._sessions: list[tuple[str, requests.Session]] = []
        parts = urlsplit(self.base_url)
        contexts = [("plain", None)]
        if parts.scheme == "https":
            if parts.hostname not in LOOPBACK_HOSTS:
                raise ValueError("El cliente en vivo solo puede leer una dirección local.")
            contexts = [("riot", make_ssl_context(ca_file))]
            if system_fallback:
                try:
                    contexts.append(("system", make_system_ssl_context()))
                except Exception:           # no truststore: Riot's root is all we have
                    pass
            if unverified_fallback:
                contexts.append(("unverified", make_unverified_loopback_context()))
        for mode, ctx in contexts:
            session = requests.Session()
            session.trust_env = False         # no proxies or .netrc for a loopback address
            if ctx is not None:
                session.mount(f"{parts.scheme}://{parts.netloc}/", LoopbackAdapter(ctx))
            if mode == "unverified":
                # urllib3 turns verification back on from `verify`; this session only ever talks
                # to the loopback game client (checked above).
                session.verify = False
            self._sessions.append((mode, session))

    @property
    def session(self):
        return self._sessions[0][1]

    def _get(self):
        error = None
        for i, (mode, session) in enumerate(self._sessions):
            try:
                with warnings.catch_warnings():
                    if mode == "unverified":
                        warnings.simplefilter("ignore", InsecureRequestWarning)
                    resp = session.get(f"{self.base_url}/liveclientdata/allgamedata", timeout=self.timeout)
            except requests.exceptions.SSLError as exc:
                error = exc
                continue                      # certificate refused: try the next way of checking it
            if i:
                self._sessions.insert(0, self._sessions.pop(i))     # keep using what worked
            self.tls_mode = mode if mode != "plain" else None
            return resp
        raise error

    def fetch(self):
        """("down", None) nobody answers; ("loading", None) answers but no game data yet;
        ("ok", dict) the full game data."""
        try:
            resp = self._get()
        except requests.exceptions.SSLError:
            self.problem = "tls"
            return "down", None
        except (requests.RequestException, OSError):
            self.problem = None
            return "down", None
        self.problem = None
        if resp.status_code != 200:
            return "loading", None
        try:
            data = resp.json()
        except ValueError:
            return "loading", None
        if not isinstance(data, dict) or not isinstance(data.get("allPlayers"), list) or not data["allPlayers"]:
            return "loading", None
        return "ok", data

    def close(self) -> None:
        for _mode, session in self._sessions:
            session.close()


# --------------------------------------------------------------- normalization
def _num(v, default=0.0):
    return float(v) if isinstance(v, (int, float)) and not isinstance(v, bool) else default


def _int(v, default=0):
    return int(v) if isinstance(v, (int, float)) and not isinstance(v, bool) else default


def _names_of(p: dict) -> list[str]:
    rid = str(p.get("riotId") or "")
    out = [rid, str(p.get("riotIdGameName") or ""), str(p.get("summonerName") or "")]
    if "#" in rid:
        out.append(rid.rsplit("#", 1)[0])
    return [n.casefold() for n in out if n]


def _champion_id(p: dict) -> str:
    raw = str(p.get("rawChampionName") or "")
    if raw.startswith(RAW_CHAMPION_PREFIX):
        return raw[len(RAW_CHAMPION_PREFIX):]
    return str(p.get("championName") or "")


def _display_name(p: dict) -> str:
    rid = str(p.get("riotId") or "")
    return str(p.get("riotIdGameName") or (rid.rsplit("#", 1)[0] if rid else "") or p.get("summonerName") or "?")


def _killer_kind(name: str) -> str:
    low = name.casefold()
    if low.startswith("turret"):
        return "una torre"
    if low.startswith("minion"):
        return "un minion"
    if low.startswith(("sru_", "sru", "monster", "baron", "dragon", "herald")):
        return "un monstruo"
    return "el entorno"


def _structure_owner(name: str) -> str | None:
    """"Turret_T2_R_03_A" / "Barracks_T1_L1" -> the team that OWNED it: ORDER (T1) or CHAOS (T2)."""
    low = str(name or "").casefold()
    if "_t1_" in low or low.endswith("_t1"):
        return "ORDER"
    if "_t2_" in low or low.endswith("_t2"):
        return "CHAOS"
    return None


def _other(side: str) -> str:
    return "CHAOS" if side == "ORDER" else "ORDER"


def normalize(raw: dict, namer=None) -> dict | None:
    """Compact state from an allgamedata response; None when it is not a running game.

    `namer(champion_id, api_name)` gives the champion name to show (Data Dragon's Spanish one).
    """
    if not isinstance(raw, dict):
        return None
    players_raw = [p for p in raw.get("allPlayers") or [] if isinstance(p, dict)]
    game_data = raw.get("gameData") if isinstance(raw.get("gameData"), dict) else {}
    if not players_raw or not isinstance(game_data.get("gameTime"), (int, float)):
        return None
    namer = namer or (lambda cid, api: api or cid)
    active = raw.get("activePlayer") if isinstance(raw.get("activePlayer"), dict) else {}
    now_s = _num(game_data.get("gameTime"))

    by_name: dict[str, int] = {}
    for i, p in enumerate(players_raw):
        for n in _names_of(p):
            by_name.setdefault(n, i)

    def find(name):
        return by_name.get(str(name or "").casefold())

    me_idx = None
    for n in _names_of(active):
        me_idx = find(n)
        if me_idx is not None:
            break
    me_side = str(players_raw[me_idx].get("team") or "ORDER") if me_idx is not None else "ORDER"
    if me_side not in ("ORDER", "CHAOS"):
        me_side = "ORDER"

    def role_of(side):
        return "ally" if side == me_side else "enemy"

    players = []
    for i, p in enumerate(players_raw):
        side = str(p.get("team") or "")
        if side not in ("ORDER", "CHAOS"):
            continue
        scores = p.get("scores") if isinstance(p.get("scores"), dict) else {}
        items = []
        for it in p.get("items") or []:
            if isinstance(it, dict) and isinstance(it.get("itemID"), int) and it["itemID"] > 0:
                items.append({"id": it["itemID"], "name": str(it.get("displayName") or ""),
                              "count": max(1, _int(it.get("count"), 1)), "slot": _int(it.get("slot"))})
        items.sort(key=lambda x: x["slot"])
        position = str(p.get("position") or "").upper()
        position = position if position in ROLE_ORDER else ""
        cid = _champion_id(p)
        players.append({
            "idx": i, "side": side, "team": role_of(side), "name": _display_name(p),
            "riot_id": str(p.get("riotId") or ""),
            "champion": cid, "champion_name": namer(cid, str(p.get("championName") or cid)),
            "position": position, "role_label": ROLE_LABELS.get(position, ""),
            "level": _int(p.get("level"), 1), "kills": _int(scores.get("kills")),
            "deaths": _int(scores.get("deaths")), "assists": _int(scores.get("assists")),
            "cs": _int(scores.get("creepScore")), "ward_score": round(_num(scores.get("wardScore")), 1),
            "items": items, "dead": bool(p.get("isDead")),
            "respawn": round(_num(p.get("respawnTimer")), 1) if p.get("isDead") else 0.0,
            "is_me": i == me_idx, "is_bot": bool(p.get("isBot")),
        })

    def display_of(idx):
        for pl in players:
            if pl["idx"] == idx:
                return pl
        return None

    # ---- events
    events_raw = (raw.get("events") or {}).get("Events") if isinstance(raw.get("events"), dict) else None
    events = sorted((e for e in events_raw or [] if isinstance(e, dict)),
                    key=lambda e: _num(e.get("EventTime")))
    totals = {s: {"towers": 0, "inhibitors": 0, "dragons": [], "heralds": 0, "barons": 0}
              for s in ("ORDER", "CHAOS")}
    feed = []
    me_deaths, me_kills = [], []
    ended, result = False, None

    def label(idx_or_name):
        idx = find(idx_or_name)
        pl = display_of(idx) if idx is not None else None
        return pl["champion_name"] if pl else None

    def actor_side(killer, owner=None, assisters=()):
        pl = display_of(find(killer))
        if pl:
            return pl["side"]
        for a in assisters or ():
            pl = display_of(find(a))
            if pl:
                return pl["side"]
        return _other(owner) if owner else None

    def push(e, kind, text, side, mine=False):
        feed.append({"t": round(_num(e.get("EventTime")), 1), "clock": fmt_clock(e.get("EventTime")),
                     "kind": kind, "text": text, "team": role_of(side) if side else None, "mine": mine})

    my_name_idx = me_idx
    for e in events:
        name = e.get("EventName")
        t = _num(e.get("EventTime"))
        assisters = [a for a in (e.get("Assisters") or []) if isinstance(a, str)]
        if name == "ChampionKill":
            killer_name, victim_name = e.get("KillerName"), e.get("VictimName")
            v_idx, k_idx = find(victim_name), find(killer_name)
            victim = label(victim_name) or str(victim_name or "?")
            killer = label(killer_name)
            ass_champs = [label(a) for a in assisters if label(a)]
            v_pl = display_of(v_idx) if v_idx is not None else None
            side = actor_side(killer_name, assisters=assisters) or (_other(v_pl["side"]) if v_pl else None)
            if killer:
                text = f"{killer} eliminó a {victim}"
            else:
                text = f"{victim} murió a manos de {_killer_kind(str(killer_name or ''))}"
            if ass_champs:
                text += " (con " + ", ".join(ass_champs) + ")"
            push(e, "kill", text, side, mine=my_name_idx is not None and my_name_idx in (v_idx, k_idx))
            if my_name_idx is not None and k_idx == my_name_idx:
                me_kills.append(t)
            if my_name_idx is not None and v_idx == my_name_idx:
                involved = ([display_of(k_idx)] if k_idx is not None else []) + \
                           [display_of(find(a)) for a in assisters if find(a) is not None]
                involved = [pl for pl in involved if pl]
                me_deaths.append({
                    "t": round(t, 1), "clock": fmt_clock(t), "by_champion": k_idx is not None,
                    "involved": len(involved),
                    "jungler_involved": any(pl["position"] == "JUNGLE" and pl["team"] == "enemy"
                                            for pl in involved) if involved else False,
                    "after_own_kill": any(0 <= t - k <= 60 for k in me_kills),
                })
        elif name == "Multikill":
            who = label(e.get("KillerName"))
            streak = _int(e.get("KillStreak"))
            if who and streak in MULTIKILL_LABELS:
                push(e, "multikill", f"{MULTIKILL_LABELS[streak]} de {who}",
                     actor_side(e.get("KillerName")))
        elif name == "FirstBlood":
            who = label(e.get("Recipient"))
            if who:
                push(e, "firstblood", f"Primera sangre: {who}", actor_side(e.get("Recipient")))
        elif name == "Ace":
            side = e.get("AcingTeam") if e.get("AcingTeam") in ("ORDER", "CHAOS") else actor_side(e.get("Acer"))
            who = label(e.get("Acer"))
            push(e, "ace", "Ace" + (f": {who} eliminó al último rival" if who else ""), side)
        elif name in ("TurretKilled", "InhibKilled"):
            owner = _structure_owner(e.get(name))
            side = actor_side(e.get("KillerName"), owner, assisters)
            if side is None:
                continue
            who = label(e.get("KillerName")) or ("El equipo " + ("aliado" if role_of(side) == "ally" else "enemigo"))
            if name == "TurretKilled":
                totals[side]["towers"] += 1
                push(e, "turret", f"{who} destruyó una torre", side)
            else:
                totals[side]["inhibitors"] += 1
                push(e, "inhib", f"{who} destruyó un inhibidor", side)
        elif name in ("DragonKill", "HeraldKill", "BaronKill"):
            side = actor_side(e.get("KillerName"), assisters=assisters)
            if side is None:
                continue
            who = label(e.get("KillerName")) or ("El equipo " + ("aliado" if role_of(side) == "ally" else "enemigo"))
            stolen = str(e.get("Stolen")).lower() == "true"
            suffix = " (robado)" if stolen else ""
            if name == "DragonKill":
                dtype = str(e.get("DragonType") or "")
                totals[side]["dragons"].append(dtype)
                dlabel = DRAGON_LABELS.get(dtype, dtype or "")
                push(e, "dragon", f"{who} mató un dragón ({dlabel}){suffix}" if dlabel else f"{who} mató un dragón{suffix}", side)
            elif name == "HeraldKill":
                totals[side]["heralds"] += 1
                push(e, "herald", f"{who} mató al Heraldo de la Grieta{suffix}", side)
            else:
                totals[side]["barons"] += 1
                push(e, "baron", f"{who} mató al Barón Nashor{suffix}", side)
        elif name == "GameEnd":
            ended = True
            res = str(e.get("Result") or "").lower()
            result = "win" if res == "win" else "loss" if res in ("lose", "loss", "defeat") else None
            push(e, "end", {"win": "Fin de la partida: victoria", "loss": "Fin de la partida: derrota"}
                 .get(result, "Fin de la partida"), None)
    feed.reverse()           # newest first
    del feed[FEED_MAX:]

    teams = {}
    for role in ("ally", "enemy"):
        side = me_side if role == "ally" else _other(me_side)
        members = [p for p in players if p["side"] == side]
        members.sort(key=lambda p: (ROLE_ORDER.index(p["position"]) if p["position"] else 9, p["idx"]))
        t = totals[side]
        teams[role] = {
            "label": "Tu equipo" if role == "ally" else "Equipo enemigo", "side": side, "players": members,
            "totals": {"kills": sum(p["kills"] for p in members), "towers": t["towers"],
                       "inhibitors": t["inhibitors"], "heralds": t["heralds"], "barons": t["barons"],
                       "dragons": [{"type": d, "label": DRAGON_LABELS.get(d, d)} for d in t["dragons"]]},
        }

    mode = str(game_data.get("gameMode") or "")
    me = None
    if me_idx is not None:
        pl = display_of(me_idx)
        stats_raw = active.get("championStats") if isinstance(active.get("championStats"), dict) else {}
        stats = []
        for key, (lab, dec, suf) in STAT_KEYS.items():
            v = stats_raw.get(key)
            if isinstance(v, (int, float)) and not isinstance(v, bool):
                stats.append({"key": key, "label": lab,
                              "value": (f"{v:.{dec}f}" if dec else f"{round(v)}") + suf})
        keystone = ((active.get("fullRunes") or {}).get("keystone") or {}).get("displayName") \
            if isinstance(active.get("fullRunes"), dict) else None
        minutes = now_s / 60.0
        me = {**{k: pl[k] for k in ("name", "riot_id", "champion", "champion_name", "position", "role_label",
                                     "level", "kills", "deaths", "assists", "cs", "ward_score")},
              "gold": round(_num(active.get("currentGold"))),
              "cs_per_min": round(pl["cs"] / minutes, 1) if minutes >= 1 else None,
              "stats": stats, "keystone": keystone or None, "checkpoints": {}}
    return {
        "time": round(now_s, 1), "clock": fmt_clock(now_s), "mode": mode,
        "mode_label": MODE_LABELS.get(mode, mode.title() if mode else ""),
        "map_label": MAP_LABELS.get(str(game_data.get("mapName") or ""), ""),
        "me": me, "teams": teams, "feed": feed, "ended": ended, "result": result,
        "me_events": {"deaths": me_deaths, "kills": me_kills},
    }


# -------------------------------------------------------------------- enrich
def item_value(items, cost_of, available=True):
    """(gold value of the items, complete?) for [{"id", "count"}]; (None, False) with no price data.

    Each item counts its own total price (components included) times its stack count, so potions
    and control wards are priced per unit and a 0-gold trinket adds nothing. Items missing from the
    price list are skipped and the value is flagged incomplete.
    """
    if not available:
        return None, False
    total, complete = 0, True
    for it in items:
        cost = cost_of(it["id"])
        if cost is None:
            complete = False
            continue
        total += int(cost) * max(1, int(it.get("count") or 1))
    return total, complete


def enrich(state: dict, *, champion_icon=None, item_icon=None, item_cost=None, item_name=None,
           prices_available=False, gold_model=None) -> dict:
    """Copy of `state` with icons, Spanish item names and the "valor en objetos" estimate.

    "Valor en objetos" is the gold already spent on a player's items (sum of Data Dragon's total
    price). Riot only exposes the current gold of the player who is playing, so this is the best
    comparison available for the other nine. With a `gold_model` (goldmodel.GoldModel) it also
    adds "gold_est" (estimated total gold) and "unspent_est" (a rough estimate minus item value).
    """
    out = copy.deepcopy(state)
    minute = out["time"] / 60.0
    cost_of = item_cost or (lambda _id: None)
    for role in ("ally", "enemy"):
        team = out["teams"][role]
        value_sum, any_value, approx = 0, False, False
        for p in team["players"]:
            p["icon"] = champion_icon(p["champion"]) if champion_icon else None
            for it in p["items"]:
                it["icon"] = item_icon(it["id"]) if item_icon else None
                it["name"] = (item_name(it["id"]) if item_name else None) or it["name"]
            p["value"], complete = item_value(p["items"], cost_of, prices_available)
            p["value_approx"] = p["value"] is not None and not complete
            if p["value"] is not None:
                value_sum += p["value"]
                any_value = True
            approx = approx or p["value_approx"]
            p["gold_est"], p["unspent_est"] = None, None
            if gold_model is not None:
                p["gold_est"] = gold_model.estimate(minute, p["cs"], p["kills"], p["assists"],
                                                    p["position"] == "UTILITY")
                if not p["is_me"]:          # for the player themself the real current gold is shown
                    p["unspent_est"] = unspent_estimate(p["gold_est"], p["value"])
        team["totals"]["value"] = value_sum if any_value else None
        team["totals"]["value_approx"] = approx
        team["totals"]["gold_est"] = (sum(p["gold_est"] for p in team["players"])
                                      if gold_model is not None else None)
    # difference against the lane opponent: the one enemy who plays the same position
    enemies = out["teams"]["enemy"]["players"]
    allies = out["teams"]["ally"]["players"]
    for mine, theirs in ((allies, enemies), (enemies, allies)):
        for p in mine:
            opp = [q for q in theirs if p["position"] and q["position"] == p["position"]]
            p["lane_opponent"] = opp[0]["champion_name"] if len(opp) == 1 else None
            p["value_diff"] = (p["value"] - opp[0]["value"]
                               if len(opp) == 1 and p["value"] is not None and opp[0]["value"] is not None
                               else None)
            p["gold_diff"] = (p["gold_est"] - opp[0]["gold_est"]
                              if len(opp) == 1 and p["gold_est"] is not None and opp[0]["gold_est"] is not None
                              else None)
    a, e = out["teams"]["ally"]["totals"], out["teams"]["enemy"]["totals"]
    out["value_diff"] = a["value"] - e["value"] if a["value"] is not None and e["value"] is not None else None
    out["gold_diff"] = (a["gold_est"] - e["gold_est"]
                        if a["gold_est"] is not None and e["gold_est"] is not None else None)
    out["gold_model"] = ({"games": gold_model.games, "source": gold_model.source,
                          "error": gold_model.error_at(minute), "legend": gold_model.legend(minute)}
                         if gold_model is not None else None)
    out["prices_available"] = prices_available
    if out["me"]:
        me_pl = next((p for p in allies if p["is_me"]), None)
        out["me"]["icon"] = me_pl["icon"] if me_pl else None
        out["me"]["value"] = me_pl["value"] if me_pl else None
    return out


# -------------------------------------------------------------------- poller
class LivePoller:
    """Polls the game client in a daemon thread and keeps the latest snapshot.

    Status: "waiting" (no game) -> "loading" (client up, game not started) -> "playing" ->
    "finished" (final snapshot kept until the next game). It polls fast (FAST_S) only in a game
    while somebody listens (page or stream open); otherwise it probes every IDLE_S so the menu can
    show "En partida". Nothing here raises into the web server.
    """

    def __init__(self, client=None, namer=None, fast_s=FAST_S, idle_s=IDLE_S, loading_s=LOADING_S,
                 down_after=DOWN_AFTER):
        self._client = client
        self._namer = namer
        self.fast_s, self.idle_s, self.loading_s, self.down_after = fast_s, idle_s, loading_s, down_after
        self._cond = threading.Condition()
        self._wake = threading.Event()
        self._stop = threading.Event()
        self._thread: threading.Thread | None = None
        self._listeners = 0
        self._misses = 0
        self._last_time = 0.0
        self.game_seq = 0               # +1 each time a new game starts being played
        self._checkpoints: dict[int, dict] = {}
        self._snap = {"status": "waiting", "version": 0, "updated": time.time(), "game": None,
                      "problem": None}

    # ---- public state
    def snapshot(self) -> dict:
        with self._cond:
            return self._snap

    @property
    def status(self) -> str:
        return self.snapshot()["status"]

    @property
    def in_game(self) -> bool:
        return self.status == "playing"

    @property
    def listeners(self) -> int:
        with self._cond:
            return self._listeners

    def add_listener(self) -> None:
        with self._cond:
            self._listeners += 1
        self._wake.set()            # leave the slow probe right away

    def remove_listener(self) -> None:
        with self._cond:
            self._listeners = max(0, self._listeners - 1)

    def is_stopped(self) -> bool:
        return self._stop.is_set()

    def wait_for_change(self, version: int, timeout: float) -> dict:
        """The snapshot as soon as its version differs from `version` (or after `timeout`)."""
        with self._cond:
            self._cond.wait_for(lambda: self._snap["version"] != version or self._stop.is_set(), timeout)
            return self._snap

    # ---- thread
    def start(self) -> None:
        with self._cond:
            if self._thread is not None and self._thread.is_alive():
                return
            self._stop.clear()
            self._thread = threading.Thread(target=self._run, daemon=True, name="lolan-live")
            self._thread.start()

    def stop(self) -> None:
        self._stop.set()
        self._wake.set()
        with self._cond:
            self._cond.notify_all()
        if self._thread is not None:
            self._thread.join(2)

    def _run(self) -> None:
        reported = False
        while not self._stop.is_set():
            try:
                interval = self.poll_once()
            except Exception as exc:        # a surprise in the game data must never kill the thread
                if not reported:
                    print(f"Aviso: falló la lectura de la partida en vivo: {exc!r}", file=sys.stderr)
                    reported = True
                interval = self.idle_s
            self._wake.wait(interval)
            self._wake.clear()

    # ---- state machine (one step; the thread calls it in a loop, tests call it directly)
    def _client_or_create(self):
        if self._client is None:
            self._client = LiveClient()
        return self._client

    def _publish(self, status, game, problem=None) -> None:
        with self._cond:
            old = self._snap
            if old["status"] == status and old["game"] is game and old["problem"] == problem:
                return
            self._snap = {"status": status, "version": old["version"] + 1, "updated": time.time(),
                          "game": game, "problem": problem}
            self._cond.notify_all()

    def touch(self) -> None:
        """Push a new version with the same game: wakes the streams when something around the game
        changed (the rivals report)."""
        with self._cond:
            self._snap = {**self._snap, "version": self._snap["version"] + 1, "updated": time.time()}
            self._cond.notify_all()

    def _fast_interval(self) -> float:
        return self.fast_s if self.listeners else self.idle_s

    def poll_once(self) -> float:
        """Read the client once, update the state and return how long to wait before the next read."""
        client = self._client_or_create()
        kind, data = client.fetch()
        status = self.status
        game = self.snapshot()["game"]
        if kind == "ok":
            state = normalize(data, self._namer)
            if state is None:
                kind = "loading"
            else:
                self._misses = 0
                new_game = status in ("waiting", "loading") or (status == "finished" and not state["ended"])
                if new_game or state["time"] < self._last_time - 10:
                    self._checkpoints = {}
                    self.game_seq += 1
                self._last_time = state["time"]
                self._record_checkpoints(state)
                self._publish("finished" if state["ended"] else "playing", state)
                return self._fast_interval() if not state["ended"] else self.idle_s
        if kind == "loading" and status != "playing":
            self._misses = 0
            if status == "waiting":
                self._publish("loading", None)
            return self.loading_s
        # nobody answers (or, in a running game, the client stopped serving game data)
        self._misses += 1
        problem = "tls" if getattr(client, "problem", None) == "tls" else None
        if status == "playing":
            if self._misses >= self.down_after:
                self._publish("finished", game)       # the client closed without a GameEnd event
                return self.idle_s
            return self.fast_s
        if status == "loading":
            self._publish("waiting", None, problem)
        elif status == "waiting":
            self._publish("waiting", None, problem)
        return self.idle_s

    def _record_checkpoints(self, state) -> None:
        me = state.get("me")
        if me:
            for m in CHECKPOINT_MINUTES:
                if m not in self._checkpoints and m * 60 <= state["time"] < m * 60 + CHECKPOINT_WINDOW_S:
                    self._checkpoints[m] = {"cs": me["cs"], "clock": state["clock"]}
            me["checkpoints"] = {str(m): dict(v) for m, v in self._checkpoints.items()}
