"""A scripted OP.GG MCP server for the tests (no network): the transport the client posts to.

It speaks the same shapes as the real endpoint: JSON-RPC over HTTP with `mcp-session-id`, results
as text either in the compact "class" notation or as plain JSON (the lane matchup guide).
"""
from __future__ import annotations

import json

# Riot champion id -> English name (the same fictional-free champions as scout_fixtures)
NAMES = {222: "Jinx", 86: "Garen", 113: "Sejuani", 117: "Lulu", 103: "Ahri", 58: "Renekton", 64: "Lee Sin",
         238: "Zed", 145: "Kai'Sa", 412: "Thresh", 266: "Aatrox", 99: "Lux", 20: "Nunu & Willump"}

# champion -> {opgg position: share of its games}
ROLE_RATES = {
    "Garen": {"top": 0.9, "mid": 0.05, "jungle": 0.02}, "Sejuani": {"jungle": 0.95, "top": 0.04},
    "Ahri": {"mid": 0.98}, "Jinx": {"adc": 1.0}, "Lulu": {"support": 0.8, "mid": 0.1},
    "Renekton": {"top": 0.95}, "Lee Sin": {"jungle": 0.98}, "Zed": {"mid": 0.85, "jungle": 0.05},
    "Kai'Sa": {"adc": 0.7, "mid": 0.3}, "Thresh": {"support": 1.0},
}
POS_OF_ROLE = {"TOP": "top", "JUNGLE": "jungle", "MIDDLE": "mid", "BOTTOM": "adc", "UTILITY": "support"}

# (position, my champion, opponent) -> (games, wins of my champion)
MATCHUPS = {
    ("top", "Garen", "Renekton"): (2400, 1296),        # 54%: favorable
    ("jungle", "Sejuani", "Lee Sin"): (1500, 705),     # 47%: unfavorable
    ("mid", "Ahri", "Zed"): (3000, 1530),              # 51%: even
    ("adc", "Jinx", "Kai'Sa"): (1113, 602),            # 54.1%
    ("support", "Lulu", "Thresh"): (150, 90),          # few games: no % shown
}
SYNERGIES = {"Jinx": [("Thresh", 708, 378), ("Lulu", 607, 305)], "Kai'Sa": [("Thresh", 900, 441), ("Lulu", 300, 150)]}


class FakeResponse:
    def __init__(self, status=200, body=None, headers=None, text=None, sse=False):
        self.status_code = status
        self.headers = {"content-type": "text/event-stream" if sse else "application/json", **(headers or {})}
        if text is not None:
            self.text = text
        elif body is None:
            self.text = ""
        elif sse:
            self.text = "event: message\ndata: " + json.dumps(body) + "\n\n"
        else:
            self.text = json.dumps(body)


def champions_text() -> str:
    rows = ",".join(f'Champion({cid},"{name.replace(chr(39), "").replace(" ", "").replace("&", "")}","{name}")'
                    for cid, name in NAMES.items())
    rows += ',Champion(60222,"Jade_Jinx","Jinx")'
    return ("class LolListChampions: data\nclass Data: champions\nclass Champion: champion_id,key,name\n\n"
            f"LolListChampions(Data([{rows}]))")


def lane_meta_text() -> str:
    lists = []
    for pos in ("top", "mid", "jungle", "adc", "support"):
        rows = [f'Top("{c}",1000,0.5,{rates[pos]})' for c, rates in ROLE_RATES.items() if pos in rates]
        lists.append("[" + ",".join(rows) + "]")
    return ("class LolListLaneMetaChampions: data\nclass Data: positions\nclass Positions: top,mid,jungle,adc,support\n"
            "class Top: champion,play,win_rate,role_rate\n\n"
            f"LolListLaneMetaChampions(Data(Positions({','.join(lists)})))")


def synergies_text(champion: str) -> str:
    rows = ",".join(f'Synergie("{champion}","ADC","{n}","SUPPORT",0,{g},{w},{w / g:.2f})'
                    for n, g, w in SYNERGIES.get(champion, []))
    return ("class LolGetChampionSynergies: data\nclass Data: synergies\n"
            "class Synergie: champion_name,position,synergy_champion_name,synergy_position,score,play,win,win_rate\n\n"
            f"LolGetChampionSynergies(Data([{rows}]))")


def guide_json(position: str, mine: str, opponent: str, table=None) -> str:
    """Plain JSON like lol_get_lane_matchup_guide: counters[] has the opponent among others, win = my wins."""
    table = MATCHUPS if table is None else table
    counters = [{"champion_id": 1, "champion_name": "Annie", "play": 9000, "win": 4600}]
    row = table.get((position, mine, opponent))
    if row:
        ident = next((cid for cid, n in NAMES.items() if n == opponent), 0)
        counters.insert(0, {"champion_id": ident, "champion_name": opponent, "play": row[0], "win": row[1]})
    return json.dumps({"lang": "en_US", "position": position, "my_champion": mine, "opponent_champion": opponent,
                       "data": {"summary": {"id": 1, "average_stats": {"play": 10000}}, "counters": counters,
                                "opponent_champion_tip": "texto"}})


def arg_name(arg: str, names=None) -> str | None:
    """Back from OP.GG's UPPER_SNAKE_CASE to the English name (None: it does not know the champion)."""
    from lolan.opgg import champion_arg
    for name in (names or NAMES.values()):
        if champion_arg(name) == arg:
            return name
    return None


class FakeMcp:
    """What `requests.Session` is to the client: `post(url, json=, headers=, timeout=)`."""

    def __init__(self, sse=False):
        self.sse = sse
        self.sessions: set[str] = set()
        self.log: list[tuple] = []             # (method, tool or None, session id sent)
        self.tool_calls: list[tuple] = []      # (tool, arguments)
        self.counter = 0
        self.fail_tools: dict = {}             # tool -> callable(arguments) -> FakeResponse | None
        self.expire_next = False               # forget every session before the next tools/call
        self.matchups = dict(MATCHUPS)
        self.raise_on_post: Exception | None = None
        self.status_on_init = 200

    def post(self, url, json=None, headers=None, timeout=None):
        if self.raise_on_post is not None:
            raise self.raise_on_post
        body = json
        method = body.get("method")
        sid = (headers or {}).get("mcp-session-id")
        self.log.append((method, (body.get("params") or {}).get("name"), sid))
        if method == "initialize":
            if self.status_on_init != 200:
                return FakeResponse(self.status_on_init, None)
            self.counter += 1
            new = f"SESSION-{self.counter}"
            self.sessions.add(new)
            return FakeResponse(200, {"jsonrpc": "2.0", "id": body["id"], "result": {"protocolVersion": "2025-06-18"}},
                                {"mcp-session-id": new}, sse=self.sse)
        if method == "notifications/initialized":
            return FakeResponse(202, None)
        if method == "tools/call":
            if self.expire_next:
                self.sessions.clear()
                self.expire_next = False
            if sid not in self.sessions:
                return FakeResponse(404, {"jsonrpc": "2.0", "error": {"code": -32001, "message": "Session not found"}})
            tool, args = body["params"]["name"], body["params"]["arguments"]
            self.tool_calls.append((tool, args))
            if tool in self.fail_tools:
                answer = self.fail_tools[tool](args)
                if answer is not None:
                    return answer
            return self._tool(body["id"], tool, args)
        return FakeResponse(400, None)

    def _text(self, rid, text, is_error=False):
        return FakeResponse(200, {"jsonrpc": "2.0", "id": rid, "result": {
            "content": [{"type": "text", "text": text}], **({"isError": True} if is_error else {})}}, sse=self.sse)

    def _reject(self, rid):
        return FakeResponse(200, {"jsonrpc": "2.0", "id": rid, "error": {
            "code": -32600, "message": "Invalid position or champion specified"}}, sse=self.sse)

    def _tool(self, rid, tool, args):
        if tool == "lol_list_champions":
            return self._text(rid, champions_text())
        if tool == "lol_list_lane_meta_champions":
            return self._text(rid, lane_meta_text())
        if tool == "lol_get_lane_matchup_guide":
            mine, opp = arg_name(args["my_champion"]), arg_name(args["opponent_champion"])
            if not mine or not opp:
                return self._reject(rid)
            return self._text(rid, guide_json(args["position"], mine, opp, self.matchups))
        if tool == "lol_get_champion_synergies":
            mine = arg_name(args["champion"])
            if not mine:
                return self._reject(rid)
            return self._text(rid, synergies_text(mine))
        return self._reject(rid)

    def count(self, tool) -> int:
        return sum(1 for t, _a in self.tool_calls if t == tool)
