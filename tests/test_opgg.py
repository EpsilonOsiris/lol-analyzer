"""OP.GG MCP client: parsers of both result formats, sessions, cache and patch invalidation, name mapping."""
import json

import pytest
import requests
from opgg_fixtures import FakeMcp, FakeResponse

from lolan import opgg
from lolan.opgg import McpClient, OpggData, OpggError, OpggRejected, champion_arg, parse_tool_text, patch_of
from lolan.store import Store

# ----------------------------------------------------------------- parsers
ANALYSIS = '''class LolGetChampionAnalysis: data
class Data: summary,strong_counters,weak_counters
class Summary: average_stats
class AverageStats: play,win_rate,pick_rate,tier
class StrongCounter: champion_name,play,win,my_win_rate,counter_win_rate,win_rate

LolGetChampionAnalysis(Data(Summary(AverageStats(432867,0.52,0.17,1)),[StrongCounter("Ezreal",17007,9281,0.55,0.45,0.55),StrongCounter("Samira",6263,3457,0.55,0.45,0.55)],[StrongCounter("Hwei",1707,787,0.46,0.54,0.54)]))'''


def test_compact_notation_uses_the_class_header_for_field_names():
    data = parse_tool_text(ANALYSIS)["data"]
    assert data["summary"]["average_stats"] == {"play": 432867, "win_rate": 0.52, "pick_rate": 0.17, "tier": 1}
    assert [c["champion_name"] for c in data["strong_counters"]] == ["Ezreal", "Samira"]
    assert data["strong_counters"][0]["win"] == 9281 and data["weak_counters"][0]["my_win_rate"] == 0.46


def test_compact_notation_strings_numbers_and_unknown_classes():
    text = 'class A: x,y\nA("Kai\'Sa \\"q\\"",-1.5e0,[B(1,2),true,null])'
    assert parse_tool_text(text) == {"x": "Kai'Sa \"q\"", "y": -1.5, "_2": [[1, 2], True, None]}
    assert parse_tool_text("class A: a\nA([])") == {"a": []}


def test_json_notation():
    assert parse_tool_text('{"data": {"counters": [{"champion_name": "Zed", "play": 3, "win": 1}]}}')["data"]["counters"][0]["win"] == 1
    assert parse_tool_text(' [1, 2] ') == [1, 2]


@pytest.mark.parametrize("bad", ["", "   ", "{not json", "class A: a\nA(1,", 'class A: a\nA("abc', "just words ((", "class A: a"])
def test_garbage_raises_opgg_error(bad):
    with pytest.raises(OpggError):
        parse_tool_text(bad)


def test_champion_names_and_patch():
    assert champion_arg("Kai'Sa") == "KAISA" and champion_arg("Lee Sin") == "LEE_SIN"
    assert champion_arg("Nunu & Willump") == "NUNU_WILLUMP" and champion_arg("Dr. Mundo") == "DR_MUNDO"
    assert champion_arg("Renata Glasc") == "RENATA_GLASC" and champion_arg("Wukong") == "WUKONG"
    assert patch_of("16.21.1") == "16.21" and patch_of(None) is None and patch_of("x") is None


# ------------------------------------------------------------------ client
def client(fake, **kw):
    return McpClient(session=fake, **kw)


def test_session_is_opened_once_and_sent_on_every_call():
    fake = FakeMcp()
    c = client(fake)
    assert "Jinx" in c.call("lol_list_champions", {})
    c.call("lol_list_lane_meta_champions", {})
    methods = [m for m, _t, _s in fake.log]
    assert methods == ["initialize", "notifications/initialized", "tools/call", "tools/call"]
    assert fake.log[1][2] == "SESSION-1" and fake.log[2][2] == "SESSION-1" and fake.log[3][2] == "SESSION-1"
    assert c.initializations == 1 and c.calls == 2


def test_event_stream_answers_are_read():
    fake = FakeMcp(sse=True)
    assert "Thresh" in client(fake).call("lol_list_champions", {})


def test_expired_session_is_opened_again_once():
    fake = FakeMcp()
    c = client(fake)
    c.call("lol_list_champions", {})
    fake.expire_next = True
    assert "Jinx" in c.call("lol_list_champions", {})
    assert c.initializations == 2 and fake.log[-1][2] == "SESSION-2"


def test_session_error_inside_json_rpc_also_reopens():
    fake = FakeMcp()
    c = client(fake)
    seen = []

    def fail_once(args):
        seen.append(1)
        if len(seen) == 1:
            return FakeResponse(200, {"jsonrpc": "2.0", "id": 1, "error": {"code": -32001, "message": "Invalid session"}})
        return None

    fake.fail_tools["lol_list_champions"] = fail_once
    assert "Jinx" in c.call("lol_list_champions", {})
    assert c.initializations == 2


def test_refused_arguments_are_a_distinct_error():
    fake = FakeMcp()
    with pytest.raises(OpggRejected):
        client(fake).call("lol_get_lane_matchup_guide", {"position": "mid", "my_champion": "NOPE", "opponent_champion": "ZED"})
    fake.fail_tools["lol_list_champions"] = lambda a: FakeResponse(200, {"jsonrpc": "2.0", "id": 1, "result": {
        "isError": True, "content": [{"type": "text", "text": "boom"}]}})
    with pytest.raises(OpggRejected, match="boom"):
        client(fake).call("lol_list_champions", {})


def test_server_error_rests_the_client_for_a_minute():
    fake = FakeMcp()
    now = [100.0]
    c = client(fake, clock=lambda: now[0])
    fake.fail_tools["lol_list_champions"] = lambda a: FakeResponse(503, None)
    with pytest.raises(OpggError, match="503"):
        c.call("lol_list_champions", {})
    before = len(fake.log)
    with pytest.raises(OpggError, match="no responde"):
        c.call("lol_list_champions", {})
    assert len(fake.log) == before and not c.available()           # nothing was even sent
    now[0] += opgg.DOWN_PAUSE_S + 1
    fake.fail_tools.clear()
    assert "Jinx" in c.call("lol_list_champions", {})


def test_network_failure_and_refused_connection():
    fake = FakeMcp()
    fake.raise_on_post = requests.ConnectionError("dns")
    c = client(fake)
    with pytest.raises(OpggError, match="ConnectionError"):
        c.call("lol_list_champions", {})
    assert not c.available()
    fake2 = FakeMcp()
    fake2.status_on_init = 403
    with pytest.raises(OpggError, match="403"):
        client(fake2).call("lol_list_champions", {})


# --------------------------------------------------------------------- data
def make_data(tmp_path, fake=None, patch="16.21", clock=lambda: 1_800_000_000):
    fake = fake or FakeMcp()
    db = tmp_path / "lol.db"
    return OpggData(client(fake), lambda: Store(db), patch=patch, clock=clock), fake


def test_champion_ids_map_to_english_names_without_skins(tmp_path):
    data, _fake = make_data(tmp_path)
    assert data.name_of(145) == "Kai'Sa" and data.name_of(64) == "Lee Sin" and data.name_of(60222) is None


def test_lane_meta_gives_role_rates_by_normalized_name(tmp_path):
    data, _ = make_data(tmp_path)
    meta = data.lane_meta()
    assert meta["kaisa"] == {"adc": 0.7, "mid": 0.3} and meta["leesin"] == {"jungle": 0.98}


def test_matchup_is_the_row_of_the_opponent_from_my_champions_side(tmp_path):
    data, fake = make_data(tmp_path)
    assert data.matchup("adc", 222, 145) == {"games": 1113, "wins": 602}        # Jinx vs Kai'Sa
    sent = [a for t, a in fake.tool_calls if t == "lol_get_lane_matchup_guide"][0]
    assert sent == {"position": "adc", "my_champion": "JINX", "opponent_champion": "KAISA", "lang": "en_US"}


def test_matchup_perspective_is_symmetric_in_the_data_we_read(tmp_path):
    fake = FakeMcp()
    fake.matchups[("adc", "Kai'Sa", "Jinx")] = (1113, 511)
    data, _ = make_data(tmp_path, fake)
    a, b = data.matchup("adc", 222, 145), data.matchup("adc", 145, 222)
    assert a["games"] == b["games"] and a["wins"] + b["wins"] == a["games"]


def test_missing_pair_or_refused_names_give_none_and_are_remembered(tmp_path):
    data, fake = make_data(tmp_path)
    assert data.matchup("top", 222, 58) is None                # no such lane pair in the table
    assert data.matchup("top", 222, 58) is None
    assert fake.count("lol_get_lane_matchup_guide") == 1       # "no data" is cached too
    assert data.matchup("top", 222, 999) is None               # unknown champion id: no call at all


def test_synergy_comes_from_the_adc_list(tmp_path):
    data, fake = make_data(tmp_path)
    assert data.synergy(222, 412) == {"games": 708, "wins": 378}        # Jinx + Thresh
    assert data.synergy(222, 117) == {"games": 607, "wins": 305}
    assert fake.count("lol_get_champion_synergies") == 1                # one list per ADC, no fallback needed
    assert fake.tool_calls[-1][1]["my_position"] == "adc"


def test_synergy_falls_back_to_the_support_list(tmp_path):
    data, fake = make_data(tmp_path)
    assert data.synergy(222, 99) == {"games": 120, "wins": 66}          # Jinx + Lux: only in Lux's list
    assert fake.count("lol_get_champion_synergies") == 2
    assert [(a["champion"], a["my_position"], a["synergy_position"]) for _t, a in fake.tool_calls
            if _t == "lol_get_champion_synergies"] == [("JINX", "adc", "support"), ("LUX", "support", "adc")]


def test_synergy_missing_on_both_sides_is_none_and_remembered(tmp_path):
    data, fake = make_data(tmp_path)
    assert data.synergy(222, 266) is None                                # Jinx + Aatrox: in neither list
    assert data.synergy(222, 266) is None
    assert fake.count("lol_get_champion_synergies") == 2                # the ADC's and the support's, once each


def test_support_synergy_list_is_cached_per_support(tmp_path):
    data, fake = make_data(tmp_path)
    assert data.synergy(222, 99) == {"games": 120, "wins": 66}          # Jinx list (miss) + Lux list
    assert data.synergy(145, 99) == {"games": 90, "wins": 41}           # Kai'Sa list (miss) + cached Lux list
    assert fake.count("lol_get_champion_synergies") == 3
    assert data.synergy(222, 99) == {"games": 120, "wins": 66}
    assert fake.count("lol_get_champion_synergies") == 3


def test_support_list_rejected_is_remembered_as_empty(tmp_path):
    data, fake = make_data(tmp_path)
    fake.fail_tools["lol_get_champion_synergies"] = lambda a: (
        FakeResponse(200, {"jsonrpc": "2.0", "id": 1, "error": {"code": -32600, "message": "Invalid position or champion"}})
        if a["my_position"] == "support" else None)
    assert data.synergy(222, 99) is None                                 # the ADC list misses, the support one is refused
    assert data.synergy(222, 99) is None
    assert fake.count("lol_get_champion_synergies") == 2


def test_answers_are_cached_in_sqlite_for_24_hours(tmp_path):
    now = [1_800_000_000]
    data, fake = make_data(tmp_path, clock=lambda: now[0])
    data.matchup("adc", 222, 145)
    data.lane_meta()
    first = fake.count("lol_get_lane_matchup_guide")
    fresh, _ = make_data(tmp_path, fake, clock=lambda: now[0] + 3600)            # a new process, an hour later
    assert fresh.matchup("adc", 222, 145) == {"games": 1113, "wins": 602}
    fresh.lane_meta()
    assert fake.count("lol_get_lane_matchup_guide") == first and fake.count("lol_list_lane_meta_champions") == 1
    late, _ = make_data(tmp_path, fake, clock=lambda: now[0] + 25 * 3600)
    late.matchup("adc", 222, 145)
    assert fake.count("lol_get_lane_matchup_guide") == first + 1


def test_a_new_patch_invalidates_the_cache(tmp_path):
    data, fake = make_data(tmp_path, patch="16.21")
    data.matchup("adc", 222, 145)
    again, _ = make_data(tmp_path, fake, patch="16.21")
    again.matchup("adc", 222, 145)
    assert fake.count("lol_get_lane_matchup_guide") == 1
    newer, _ = make_data(tmp_path, fake, patch="16.22")
    newer.matchup("adc", 222, 145)
    assert fake.count("lol_get_lane_matchup_guide") == 2
    store = Store(tmp_path / "lol.db")
    assert store.get_opgg_cache("mu:adc:222:145", 99999, "16.22", 1_800_000_000) is not None
    assert store.get_opgg_cache("mu:adc:222:145", 99999, "16.21", 1_800_000_000) is None
    json.loads(store.conn.execute("SELECT json FROM opgg_cache WHERE key='champions'").fetchone()[0])
    store.close()
