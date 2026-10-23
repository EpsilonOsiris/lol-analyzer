"""Champion select as the Partida page sees it: a compact, anonymous state from the League client.

`normalize` turns the raw champion-select session (/lol-champ-select/v1/session) plus the gameflow
session (queue and map) into a small JSON-able state: timer, allies (role, champion, hover or
locked, me), enemies (revealed champions only), bans and the bench of ARAM-like modes.
`DraftFeed` is the observer the League-client poller (lcu.py) calls on every poll; it keeps the
latest phase + state and wakes the page's event stream when they change. `build_lineup` turns the
state into the input of the Composiciones draft recommender (comps.recommend_draft).

PRIVACY RULE. In ranked solo/duo and for enemies the client hides who the players are
(nameVisibilityType "HIDDEN": empty names, no puuid). For a HIDDEN player this module keeps
nothing that identifies them: no name, puuid, summoner id or obfuscated id, only champion data.
Enemies are only ever read for their champion. No code may try to work out who a hidden player is.
Even for visible allies the state keeps only the Riot ID (to match registered clan members); puuids
and summoner ids are never copied.

Unverified against a real draft (only Practice Tool and ARAM were captured): the values of
assignedPosition in Summoner's Rift draft modes (community docs: top, jungle, middle, bottom,
utility; "" in blind pick), when enemy picks are revealed and what the bans look like. All of it
is read defensively.
"""
from __future__ import annotations

import threading
from collections import Counter

from . import comps

CHAMP_SELECT = "ChampSelect"
RIFT_MAP = 11                       # Summoner's Rift; ARAM is 12 (the page only drafts on 11)
ROLE_ALIASES = {"top": "TOP", "jungle": "JUNGLE", "middle": "MIDDLE", "mid": "MIDDLE",
                "bottom": "BOTTOM", "bot": "BOTTOM", "adc": "BOTTOM", "utility": "UTILITY",
                "support": "UTILITY"}

# Spanish text of the gameflow phases and of the champion-select timer phases.
PHASE_TEXT = {
    None: "El cliente de League no está abierto.",
    "None": "El cliente de League está en el menú.",
    "Lobby": "Estás en la sala.",
    "Matchmaking": "Buscando partida.",
    "ReadyCheck": "Partida encontrada: falta aceptarla.",
    "ChampSelect": "Selección de campeones.",
    "GameStart": "La partida está por empezar.",
    "InProgress": "Partida en curso.",
    "Reconnect": "Partida en curso (reconectando).",
    "WaitingForStats": "Partida terminada.",
    "PreEndOfGame": "Partida terminada.",
    "EndOfGame": "Partida terminada.",
}
TIMER_TEXT = {"PLANNING": "Planeación", "BAN_PICK": "Elección y bans", "FINALIZATION": "Últimos ajustes",
              "GAME_STARTING": "La partida está por empezar"}
POOL_MIN_GAMES = 2                  # games with a champion in a line before it counts as part of a person's pool
GAME_PHASES = {"GameStart", "InProgress", "Reconnect"}
END_PHASES = {"WaitingForStats", "PreEndOfGame", "EndOfGame"}


def _int(value, default=0) -> int:
    return value if isinstance(value, int) and not isinstance(value, bool) else default


def _dict(value) -> dict:
    return value if isinstance(value, dict) else {}


def _list(value) -> list:
    return value if isinstance(value, list) else []


def map_and_queue(session, gameflow) -> dict:
    """{"map_id", "queue_id", "game_mode", "queue_name", "is_custom"} from the two sessions (None when unknown)."""
    gd = _dict(_dict(gameflow).get("gameData"))
    queue = _dict(gd.get("queue"))
    map_id = _dict(_dict(gameflow).get("map")).get("id")
    if map_id is None:
        map_id = queue.get("mapId")
    queue_id = _dict(session).get("queueId")
    if not _int(queue_id):
        queue_id = queue.get("id")
    custom = _dict(session).get("isCustomGame")
    if custom is None:
        custom = gd.get("isCustomGame")
    return {"map_id": map_id if isinstance(map_id, int) else None,
            "queue_id": queue_id if isinstance(queue_id, int) else None,
            "game_mode": queue.get("gameMode") if isinstance(queue.get("gameMode"), str) else None,
            "queue_name": queue.get("description") if isinstance(queue.get("description"), str) else None,
            "is_custom": bool(custom)}


def role_of(position) -> str | None:
    """TOP / JUNGLE / MIDDLE / BOTTOM / UTILITY from the client's assignedPosition; None when empty or unknown."""
    return ROLE_ALIASES.get(str(position or "").strip().lower())


def _pick_actions(session) -> list[dict]:
    return [a for group in _list(_dict(session).get("actions")) for a in _list(group)
            if isinstance(a, dict) and a.get("type") == "pick"]


def _bans(session) -> dict:
    bans = {"ally": [], "enemy": []}
    raw = _dict(_dict(session).get("bans"))

    def add(side, champion_id):
        if _int(champion_id) > 0 and champion_id not in bans[side]:
            bans[side].append(champion_id)

    for c in _list(raw.get("myTeamBans")):
        add("ally", c)
    for c in _list(raw.get("theirTeamBans")):
        add("enemy", c)
    for group in _list(_dict(session).get("actions")):
        for a in _list(group):
            if isinstance(a, dict) and a.get("type") == "ban" and a.get("completed"):
                add("ally" if a.get("isAllyAction") else "enemy", a.get("championId"))
    return bans


def normalize(session, gameflow=None) -> dict | None:
    """Compact state of a raw champion-select session; None when `session` is not one.

    {"queue_id", "map_id", "game_mode", "queue_name", "is_custom", "lanes", "timer", "me",
     "allies", "enemies", "bans", "bench"}. `lanes` is True when the Composiciones draft applies:
    Summoner's Rift (map 11) without a bench (ARAM-like modes, which also have no positions or bans,
    and any map other than 11 are not lane drafts). With the map unknown (no gameflow session yet)
    it counts as a rift draft unless there is a bench.

    Allies: {"cell_id", "role", "champion_id", "hover_id", "locked", "is_me", "visible", "riot_id"}
    (riot_id only for VISIBLE players). `locked`: the pick is final; in lane drafts an incomplete
    pick action of the cell means the champion is still only hovered; in the other modes a champion
    is simply assigned. Enemies: [{"champion_id"}] and nothing else (see the module docstring).
    """
    if not isinstance(session, dict) or not isinstance(session.get("myTeam"), list):
        return None
    info = map_and_queue(session, gameflow)
    bench_on = bool(session.get("benchEnabled"))
    lanes = not bench_on and info["map_id"] in (RIFT_MAP, None)
    picks = {a.get("actorCellId"): a for a in _pick_actions(session)}
    timer = _dict(session.get("timer"))
    left = timer.get("adjustedTimeLeftInPhase")
    local = session.get("localPlayerCellId")

    allies = []
    for m in sorted((x for x in session["myTeam"] if isinstance(x, dict)), key=lambda x: _int(x.get("cellId"), 99)):
        cell = _int(m.get("cellId"), -1)
        champion = _int(m.get("championId"))
        action = picks.get(cell, {})
        pending = lanes and bool(action) and not action.get("completed")
        hover = _int(m.get("championPickIntent")) or _int(action.get("championId") if pending else 0)
        locked = champion > 0 and not pending
        if champion > 0 and not locked:
            hover = hover or champion
        visible = m.get("nameVisibilityType") == "VISIBLE"
        riot_id = None
        if visible and str(m.get("gameName") or "").strip():
            name, tag = str(m["gameName"]).strip(), str(m.get("tagLine") or "").strip()
            riot_id = f"{name}#{tag}" if tag else name
        allies.append({"cell_id": cell, "role": role_of(m.get("assignedPosition")) if lanes else None,
                       "champion_id": champion if locked else 0,
                       "hover_id": 0 if locked else hover, "locked": locked,
                       "is_me": cell == local, "visible": visible, "riot_id": riot_id})

    enemies = []
    for m in sorted((x for x in _list(session.get("theirTeam")) if isinstance(x, dict)),
                    key=lambda x: _int(x.get("cellId"), 99)):
        champion = _int(m.get("championId"))
        action = picks.get(_int(m.get("cellId"), -1), {})
        if not champion and action.get("completed") and not action.get("isAllyAction", False):
            champion = _int(action.get("championId"))
        enemies.append({"champion_id": champion})

    bench = [_int(_dict(c).get("championId")) for c in _list(session.get("benchChampions"))]
    return {**info, "lanes": lanes,
            "timer": {"phase": timer.get("phase") if isinstance(timer.get("phase"), str) else None,
                      "seconds_left": None if not isinstance(left, (int, float)) else max(0, round(left / 1000)),
                      "infinite": bool(timer.get("isInfinite"))},
            "me": local if isinstance(local, int) else None,
            "allies": allies, "enemies": enemies, "bans": _bans(session),
            "bench": {"enabled": bench_on, "champions": [c for c in bench if c > 0],
                      "rerolls": _int(session.get("rerollsRemaining")),
                      "can_reroll": bool(session.get("allowRerolling"))}}


def signature(state):
    """The state without the ticking timer (to know when the draft has to be recomputed)."""
    if not isinstance(state, dict):
        return state
    return {k: v for k, v in state.items() if k != "timer"}


# --------------------------------------------------------------------- feed
class DraftFeed:
    """Latest phase + normalized state of the client; the observer of lcu.ChampSelectRecorder.

    Calling it with (phase, client, session) updates the feed. `wait_for_change` blocks the page's
    event stream until `version` moves past the one it already sent.
    """

    def __init__(self):
        self._cond = threading.Condition()
        self._version = 0
        self._phase = None
        self._state = None
        self._gameflow = None
        self._stopped = False

    def __call__(self, phase, client, session) -> None:
        state = None
        if phase == CHAMP_SELECT:
            if self._gameflow is None and client is not None:
                self._gameflow = client.gameflow_session()        # queue and map: once per champion select
            state = normalize(session, self._gameflow)
        else:
            self._gameflow = None
        self.publish(phase, state)

    def publish(self, phase, state) -> None:
        with self._cond:
            if phase == self._phase and state == self._state:
                return
            self._phase, self._state = phase, state
            self._version += 1
            self._cond.notify_all()

    def touch(self) -> None:
        """Wake the page's event stream without a new state (OP.GG data arrived for the current draft)."""
        with self._cond:
            self._version += 1
            self._cond.notify_all()

    def snapshot(self) -> dict:
        with self._cond:
            return {"version": self._version, "phase": self._phase, "state": self._state}

    def wait_for_change(self, last: int, timeout: float) -> dict:
        with self._cond:
            if self._version == last and not self._stopped:
                self._cond.wait(timeout)
            return {"version": self._version, "phase": self._phase, "state": self._state}

    def stop(self) -> None:
        with self._cond:
            self._stopped = True
            self._cond.notify_all()

    def is_stopped(self) -> bool:
        return self._stopped


# ---------------------------------------------------------------- people and roles
def person_of(riot_id, clan) -> str | None:
    """The registered clan person of a visible Riot ID, or None ("Aleatorio").

    A person is a game name with the clan prefix, or the name of a registered (tracked) player,
    after following the aliases: the same rule as comps.resolve_people.
    """
    if not riot_id:
        return None
    name = riot_id.split("#")[0].strip()
    tracked = {str(v).casefold() for v in clan.tracked.values()}
    if clan.matches_prefix(name) or riot_id.casefold() in tracked:
        return clan.person(name)
    return None


def role_ranking(records, person=None) -> list[str]:
    """Lines of a clan person (or of the main player when `person` is None), most frequent first."""
    if person is None:
        roles = Counter(r["main_role"] for r in records if r["main_role"] in comps.ROLES)
    else:
        roles = Counter(m["role"] for r in records for m in r["team"]
                        if m["person"] == person and m["role"] in comps.ROLES)
    return sorted(roles, key=lambda k: (-roles[k], k))


def person_pool(records, person, role, min_games=POOL_MIN_GAMES) -> list[tuple[str, int]]:
    """[(Data Dragon champion name, games)] a clan person played in `role` in the records, most played first."""
    if not person or role not in comps.ROLES:
        return []
    games = Counter(m["champion"] for r in records for m in r["team"]
                    if m["person"] == person and m["role"] == role and m["champion"])
    return [(c, n) for c, n in sorted(games.items(), key=lambda kv: (-kv[1], kv[0])) if n >= min_games]


def usual_role(records, person=None) -> str | None:
    ranking = role_ranking(records, person)
    return ranking[0] if ranking else None


def attach_people(state, clan, main_person) -> list[dict]:
    """Copies of the allies with `person` ("Aleatorio" is None): the main player in his own slot, registered
    clan members by Riot ID, nobody else."""
    out = []
    for a in state["allies"]:
        person = main_person if a["is_me"] else person_of(a["riot_id"], clan)
        out.append({**a, "person": person})
    return out


def build_lineup(allies, records, enemies_keys, my_role=None) -> dict:
    """Input of comps.recommend_draft from the allies (with `person`, see attach_people).

    `allies[i]["key"]` must hold the Data Dragon name of its champion (locked, else hover) or None.
    Returns {"main_role", "role_source", "lineup", "roles" (cell -> role), "hover_roles", "enemies"}.
    My line: `my_role` (the viewer's choice), else the client's assignedPosition, else the line the main
    player plays most in the records, else BOTTOM. The other allies: their assigned position, else a
    clan person's usual line; a line is never given twice.
    """
    me = next((a for a in allies if a["is_me"]), None)
    if my_role in comps.ROLES:
        main_role, source = my_role, "elegido"
    elif me and me["role"]:
        main_role, source = me["role"], "cliente"
    elif usual_role(records):
        main_role, source = usual_role(records), "historial"
    else:
        main_role, source = "BOTTOM", "supuesto"
    taken = {main_role}
    roles = {}
    if me:
        roles[me["cell_id"]] = main_role
    others = [a for a in allies if not a["is_me"]]
    for a in others:                                   # the client's positions first
        if a["role"] and a["role"] not in taken:
            roles[a["cell_id"]] = a["role"]
            taken.add(a["role"])
    for a in others:
        if a["cell_id"] in roles or not a["person"]:
            continue
        guess = next((r for r in role_ranking(records, a["person"]) if r not in taken), None)
        if guess:
            roles[a["cell_id"]] = guess
            taken.add(guess)
    lineup, hover_roles = {}, set()
    for a in allies:
        role = roles.get(a["cell_id"])
        if role is None:
            continue
        champion = a["key"]
        if a["is_me"] and not a["locked"]:
            champion = None                            # my own hover never fixes my slot
        if champion and not a["locked"]:
            hover_roles.add(role)
        lineup[role] = (a["person"], champion)
    return {"main_role": main_role, "role_source": source, "lineup": lineup, "roles": roles,
            "hover_roles": sorted(hover_roles), "enemies": [k for k in enemies_keys if k]}
