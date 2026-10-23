"""/partida lane cards: a real normal draft (sanitized capture), enemy lines from OP.GG, samples at once, OP.GG in the background."""
import json
import shutil
import subprocess
from pathlib import Path

import opgg_fixtures as fx
import pytest
import requests
from conftest import BASE_TIME_MS, ME_PUUID, make_comp_match, make_timeline
from lanecards_fixtures import lane_rows, write_lines
from opgg_fixtures import FakeMcp
from test_draft_live import load
from test_web_comps import add_comp_games

from lolan import draft_live as dl
from lolan import duelos, lanecards, web
from lolan.opgg import McpClient
from lolan.store import Store

NOW = 1_800_000_000
STATIC = Path(web.__file__).parent / "static"
TESTS = Path(__file__).parent
node = shutil.which("node")

DD = {"Diana": 131, "Akali": 84, "Kaisa": 145, "Mordekaiser": 82, "XinZhao": 5, "Taric": 44, "Kayn": 141,
      "Aphelios": 523, "Nautilus": 111, "Renekton": 58, "Zed": 238, "Ahri": 103, "Orianna": 61, "Jinx": 222,
      "Vayne": 67, "Ezreal": 81, "Garen": 86, "Ashe": 22}
OPGG_NAMES = {131: "Diana", 84: "Akali", 145: "Kai'Sa", 82: "Mordekaiser", 5: "Xin Zhao", 44: "Taric", 141: "Kayn",
              523: "Aphelios", 111: "Nautilus", 58: "Renekton", 238: "Zed", 103: "Ahri", 61: "Orianna"}
OPGG_RATES = {"Diana": {"mid": 0.6, "jungle": 0.35}, "Akali": {"mid": 0.8, "top": 0.15},
              "Kai'Sa": {"adc": 0.7, "mid": 0.3}, "Mordekaiser": {"top": 0.9}, "Xin Zhao": {"jungle": 0.98},
              "Taric": {"support": 1.0}, "Kayn": {"jungle": 0.95}, "Aphelios": {"adc": 1.0},
              "Nautilus": {"support": 0.9, "jungle": 0.05}, "Renekton": {"top": 0.95}, "Zed": {"mid": 0.85},
              "Ahri": {"mid": 0.98}, "Orianna": {"mid": 0.98}}
OPGG_MATCHUPS = {("mid", "Diana", "Akali"): (2500, 1300), ("top", "Renekton", "Mordekaiser"): (1800, 900),
                 ("jungle", "Kayn", "Xin Zhao"): (1200, 600), ("adc", "Aphelios", "Kai'Sa"): (3000, 1500),
                 ("support", "Nautilus", "Taric"): (900, 440)}
OPGG_EXTRAS = {("mid", "Diana", "Akali"): {"lane": "Akali", "solo": "Akali", "tip": "Stay away from Akali's shroud."}}


@pytest.fixture
def root(tmp_path, monkeypatch):
    monkeypatch.setenv("LOLAN_HOME", str(tmp_path))
    for var in ("RIOT_API_KEY", "RIOT_ID", "PLATFORM", "REGION", "CLAN_PREFIX", "CLAN_ALIASES"):
        monkeypatch.delenv(var, raising=False)
    (tmp_path / ".env").write_text("RIOT_API_KEY=RGAPI-test\nRIOT_ID=Epsilon Me#LAN\n", encoding="utf-8")
    base = tmp_path / "data" / "ddragon" / "16.20.1"
    (base / "data" / "es_MX").mkdir(parents=True)
    (base.parent / "versions.json").write_text('["16.20.1"]', encoding="utf-8")
    data = {name: {"key": str(key), "name": name} for name, key in DD.items()}
    (base / "data" / "es_MX" / "champion.json").write_text(json.dumps({"data": data}), encoding="utf-8")
    (base / "img" / "champion").mkdir(parents=True)
    (base / "img" / "champion" / "Diana.png").write_bytes(b"\x89PNG")
    monkeypatch.setattr(fx, "NAMES", OPGG_NAMES)
    monkeypatch.setattr(fx, "ROLE_RATES", OPGG_RATES)
    monkeypatch.setattr(fx, "MATCHUPS", OPGG_MATCHUPS)
    monkeypatch.setattr(fx, "LANE_EXTRAS", OPGG_EXTRAS)
    return tmp_path


def write_samples(root):
    lineas = root / "data" / "lineas"
    write_lines(lineas,
                lane_rows(131, 84, 20, 9, source="diamante"), lane_rows(84, 131, 20, 11, source="diamante"),
                lane_rows(131, 84, 3, 2, person="Epsilon Me", prefix="P"),
                lane_rows(238, 84, 30, 20, source="diamante"), lane_rows(103, 84, 30, 12, source="diamante"),
                lane_rows(58, 82, 6, 4, role="TOP"), lane_rows(82, 58, 6, 2, role="TOP"))


def add_mid_games(root):
    """The main player also plays mid: Zed 4 games, Ahri 3, Orianna 2 (his pool against a mid laner)."""
    store = Store(root / "data" / "lol.db")
    plan = [("Zed", True)] * 3 + [("Zed", False)] + [("Ahri", True)] * 2 + [("Ahri", False)] + [("Orianna", True)] * 2
    for i, (champion, win) in enumerate(plan):
        match = make_comp_match(f"LA1_mid{i}", blue={"MIDDLE": (ME_PUUID, "Epsilon Me", champion)}, win=win,
                                main_role="MIDDLE", created=BASE_TIME_MS + (100 + i) * 3600_000)
        store.save_match(f"LA1_mid{i}", match, make_timeline(minutes=25))
    store.close()


class Rig:
    """The app with a scripted OP.GG (enabled) and the samples; `fake` is the OP.GG server."""

    def __init__(self, root, opgg=True, samples=True):
        add_comp_games(root)
        add_mid_games(root)
        if samples:
            write_samples(root)
        self.app = web.create_app(root)
        self.ext = self.app.extensions["lolan"]
        self.fake = FakeMcp()
        self.svc = duelos.DuelosService(lambda: Store(root / "data" / "lol.db"), root / "data" / "wp.json",
                                        client=McpClient(session=self.fake), patch_func=lambda: "16.21",
                                        clock=lambda: NOW)
        if opgg:
            self.ext["duelos"] = self.svc
            self.ext["partida_opgg"].enabled = True
        self.feed = self.ext["partida"]

    def publish(self, state):
        self.feed.publish("ChampSelect", state)

    def payload(self, query=""):
        return self.app.test_client().get("/api/partida" + query).get_json()

    def settle(self):
        for t in list(self.ext["partida_opgg"].threads):
            t.join(5)


def normal(index):
    gf, snaps = load("normal")
    return dl.normalize(snaps[index], gf)


@pytest.fixture
def rig(root):
    return Rig(root)


def cards_of(data):
    return {c["role"]: c for c in data["lanes"]["cards"]}


# ------------------------------------------------------------------ fixture
def test_the_normal_draft_fixture_is_a_real_capture_without_secrets():
    text = (TESTS / "fixtures" / "champ_select_normal.json").read_text(encoding="utf-8")
    for needle in ("multiUserChat", "mucJwt", "Password", "token", "Reticencia"):
        assert needle not in text
    gf, snaps = load("normal")
    last = dl.normalize(snaps[-1], gf)
    assert last["queue_id"] == 400 and last["lanes"] and last["me"] == 8 and last["bans"]["ally"] == [33, 22, 16, 134, 67] and len(last["bans"]["enemy"]) == 4
    assert {a["role"]: a["champion_id"] for a in last["allies"]} == {
        "JUNGLE": 141, "BOTTOM": 523, "UTILITY": 111, "MIDDLE": 131, "TOP": 58}
    assert [e["champion_id"] for e in last["enemies"]] == [145, 82, 84, 5, 44]       # the client never says their lines
    assert all(m["puuid"] == "" or m["puuid"].startswith("00000000-0000-4000-8000-") for s in snaps
               for m in s["myTeam"] + s["theirTeam"])


# ------------------------------------------------------------ the whole flow
def test_enemy_lines_come_from_opgg_then_every_lane_gets_its_card(rig):
    rig.publish(normal(4))                                                  # everything locked
    first = rig.payload()
    assert first["lanes"]["enemy_roles"] == "pending"
    mine = first["lanes"]["cards"][0]
    assert mine["state"] == "waiting" and "Deduciendo las líneas del rival" in mine["message"]
    assert all("role" not in e for e in first["board"]["enemies"])
    before = rig.feed.snapshot()["version"]
    rig.settle()
    assert rig.feed.snapshot()["version"] > before                          # the page was woken up

    second = rig.payload()
    assert [e["role"] for e in second["board"]["enemies"]] == ["BOTTOM", "TOP", "MIDDLE", "JUNGLE", "UTILITY"]
    assert second["board"]["enemies"][2]["role_label"] == "Mid" and not second["board"]["enemies"][2]["role_low"]
    assert [c["role"] for c in second["lanes"]["cards"]] == ["MIDDLE", "TOP", "JUNGLE", "BOTTOM", "UTILITY"]
    mid = cards_of(second)["MIDDLE"]
    assert mid["mine"] and mid["state"] == "pair" and mid["ally"]["name"] == "Diana" and mid["enemy"]["name"] == "Akali"
    assert mid["sample"]["kind"] == "diamante" and mid["sample"]["games"] == 20 and mid["sample"]["note"] is None
    assert mid["person"]["text"] == "2 de 3 líneas ganadas" and mid["person"]["name"] == "Tú"
    assert "nivel 2 y 6" in mid["spikes"]["ally"]["text"] and mid["spikes"]["duel"].startswith("En este duelo")
    assert mid["opgg"]["status"] == "pending"                               # locked pairs are fetched, not waited for
    top = cards_of(second)["TOP"]
    assert top["sample"]["kind"] == "fallback" and top["sample"]["note"] == lanecards.NO_DIAMOND_PAIR_NOTE
    assert top["spikes"]["ally"]["label"] == "Renekton" and top["spikes"]["enemy"]["label"] == "Mordekaiser"
    assert second["lanes"]["champs"]["84"]["name"] == "Akali" and second["lanes"]["champs"]["131"]["icon"]

    rig.settle()
    third = rig.payload()
    mid = cards_of(third)["MIDDLE"]
    assert mid["opgg"]["status"] == "ok" and mid["opgg"]["wr_text"] == "52%" and mid["opgg"]["tone"] == "fav"
    assert mid["opgg"]["lane"] == "Ventaja en la línea: Akali" and mid["opgg"]["solo"] == "Ventaja de solo kill: Akali"
    assert mid["opgg"]["tip"] == "Stay away from Akali's shroud."
    assert mid["length"]["opgg"] and mid["length"]["ours"] and "OP.GG:" not in mid["length"]["ours"]
    assert all(c["opgg"]["status"] in ("ok", "none") for c in third["lanes"]["cards"])
    assert rig.fake.count("lol_get_lane_matchup_guide") == 5
    json.dumps(third)                                                       # plain JSON
    assert "puuid" not in json.dumps(third)


def test_everything_cached_renders_at_once_with_no_opgg_call(rig):
    data = rig.svc._data()                                                  # what the loading screen / a previous game left
    data.champions()
    data.lane_meta()
    for position, mine, opp in (("mid", 131, 84), ("top", 58, 82), ("jungle", 141, 5), ("adc", 523, 145),
                                ("support", 111, 44)):
        data.matchup_full(position, mine, opp)
    calls = len(rig.fake.tool_calls)
    rig.publish(normal(4))
    out = rig.payload()
    assert [c["role"] for c in out["lanes"]["cards"]] == ["MIDDLE", "TOP", "JUNGLE", "BOTTOM", "UTILITY"]
    assert all(c["opgg"]["status"] == "ok" for c in out["lanes"]["cards"])
    assert len(rig.fake.tool_calls) == calls and not rig.ext["partida_opgg"].threads


def test_the_loading_screen_duelos_reuse_what_champion_select_fetched(rig):
    rig.publish(normal(4))
    rig.payload()
    rig.settle()
    rig.payload()
    rig.settle()
    calls = rig.fake.count("lol_get_lane_matchup_guide")
    assert calls == 5
    fresh = rig.svc._data()                                                 # DuelosService.build asks for the same pairs
    assert fresh.matchup("mid", 131, 84) == {"games": 2500, "wins": 1300}
    assert rig.fake.count("lol_get_lane_matchup_guide") == calls


# ------------------------------------------------------------ before picking
def test_before_i_pick_my_lane_lists_my_best_champions_against_the_revealed_laner(rig):
    rig.publish(normal(1))                                                  # Kai'Sa, Mordekaiser, Akali revealed; my mid empty
    rig.payload()
    rig.settle()
    data = rig.payload()
    cards = cards_of(data)
    mid = cards["MIDDLE"]
    assert mid["state"] == "picking" and mid["mine"] and mid["summary"] == "Mejores contra Akali"
    best = mid["best"]
    assert best["label"] == "Diamante+" and best["note"] is None
    assert [i["name"] for i in best["items"]] == ["Zed", "Orianna", "Ahri"]            # my pool in mid, 2+ games each
    assert best["items"][0]["games"] == 30 and best["items"][0]["own_games"] == 4
    assert best["items"][1]["games"] == 0 and best["items"][1]["tone"] == "few"        # never seen against him: its overall rate
    assert best["items"][2]["games"] == 30 and best["items"][2]["own_games"] == 3
    bottom = cards["BOTTOM"]                                                # Aphelios locked against Kai'Sa: a pair card
    assert bottom["state"] == "pair" and bottom["ally"]["name"] == "Aphelios" and not bottom["ally"]["hover"]
    assert "JUNGLE" not in cards                                            # Kayn's rival is not revealed yet
    assert rig.fake.count("lol_get_lane_matchup_guide") == 1                # only the locked pair (Aphelios) was asked
    rig.publish(normal(3))                                                  # Diana locks in, Xin Zhao and Taric appear
    rig.payload()
    rig.settle()
    after = cards_of(rig.payload())
    assert after["MIDDLE"]["state"] == "pair" and after["MIDDLE"]["best"] is None


def test_champions_already_picked_or_banned_leave_the_best_list(rig):
    state = normal(1)
    state["bans"] = {"ally": [238], "enemy": []}                            # Zed is banned
    rig.publish(state)
    rig.payload()
    rig.settle()
    names = [i["name"] for i in cards_of(rig.payload())["MIDDLE"]["best"]["items"]]
    assert names == ["Orianna", "Ahri"]


def test_a_hover_gets_the_local_card_but_opgg_waits_for_the_lock(rig):
    state = normal(4)
    me = next(a for a in state["allies"] if a["is_me"])
    me.update(champion_id=0, hover_id=131, locked=False)                    # I am only looking at Diana
    rig.publish(state)
    rig.payload()
    rig.settle()
    mid = cards_of(rig.payload())["MIDDLE"]
    assert mid["state"] == "pair" and mid["ally"]["hover"] and mid["sample"]["games"] == 20
    assert mid["opgg"] == {"status": "idle"} and mid["best"]["items"]       # and the list is still there
    assert rig.fake.count("lol_get_lane_matchup_guide") == 4                # the other four lanes, not mine
    me.update(champion_id=131, hover_id=0, locked=True)
    rig.publish(state)
    rig.payload()
    rig.settle()
    assert rig.payload()["lanes"]["cards"][0]["opgg"]["status"] == "ok"
    assert rig.fake.count("lol_get_lane_matchup_guide") == 5


# ------------------------------------------------------------------ fail soft
def test_opgg_down_leaves_the_local_cards_and_says_why(rig):
    rig.fake.raise_on_post = requests.ConnectionError("down")
    rig.publish(normal(4))
    rig.payload()
    rig.settle()
    out = rig.payload()
    assert out["lanes"]["enemy_roles"] == "error"
    mine = out["lanes"]["cards"][0]
    assert mine["state"] == "waiting" and "OP.GG no respondió" in mine["message"]
    assert out["board"]["allies"] and out["draft"]["status"] == "ok"        # board and recommender are untouched


def test_without_opgg_service_nothing_is_called_and_the_page_says_so(root):
    rig = Rig(root, opgg=False)
    rig.ext["duelos"] = None                                                # LOLAN_OPGG=off
    rig.ext["partida_opgg"].enabled = True
    rig.publish(normal(4))
    out = rig.payload()
    assert out["lanes"]["enemy_roles"] == "off" and "Sin OP.GG" in out["lanes"]["cards"][0]["message"]
    assert rig.fake.tool_calls == []


def test_opgg_stays_off_until_the_live_services_start(root):
    rig = Rig(root, opgg=False)
    rig.ext["duelos"] = rig.svc
    rig.publish(normal(4))
    rig.payload()
    assert rig.fake.tool_calls == [] and rig.payload()["lanes"]["enemy_roles"] == "off"


def test_no_sample_files_still_gives_a_page_with_opgg(root):
    rig = Rig(root, samples=False)
    rig.publish(normal(4))
    rig.payload()
    rig.settle()
    out = rig.payload()
    assert out["lanes"]["data"] is False and out["lanes"]["diamond"] is False
    mid = cards_of(out)["MIDDLE"]
    assert mid["sample"]["games"] == 0 and mid["sample"]["kind"] == "fallback"


def test_a_failure_building_the_lanes_does_not_take_the_board_or_the_draft_down(rig, monkeypatch, capsys):
    monkeypatch.setattr(lanecards, "build_lanes", lambda *a, **k: 1 / 0)
    rig.publish(normal(4))
    out = rig.payload()
    assert out["board"]["allies"] and out["draft"]["status"] == "ok" and "error" not in out
    assert out["lanes"]["cards"] == [] and "No se pudieron armar las líneas" in out["lanes"]["error"]
    assert "no se pudieron armar las líneas" in capsys.readouterr().err


def test_aram_has_no_lane_cards(rig):
    gf, snaps = load("aram")
    rig.publish(dl.normalize(snaps[3], gf))
    out = rig.payload()
    assert out["lanes"] is None and out["draft"]["status"] == "no-aplica"
    assert rig.fake.tool_calls == []


def test_other_phases_carry_no_lanes(rig):
    rig.feed.publish("Lobby", None)
    assert rig.payload()["lanes"] is None


# ---------------------------------------------------------------- the stream
def test_the_stream_pushes_again_when_opgg_arrives(rig):
    rig.app.config["LIVE_HEARTBEAT_S"] = 0.05
    rig.publish(normal(4))
    resp = rig.app.test_client().get("/api/partida/stream", buffered=False)
    gen = iter(resp.response)
    next(gen)
    first = json.loads(next(gen).decode()[6:])
    assert first["lanes"]["enemy_roles"] in ("pending", "ok")
    seen = {first["lanes"]["enemy_roles"]}
    for _ in range(200):
        msg = next(gen).decode()
        if msg.startswith("data: "):
            out = json.loads(msg[6:])
            seen.add(out["lanes"]["enemy_roles"])
            if out["lanes"]["cards"] and out["lanes"]["cards"][0]["state"] == "pair" \
                    and out["lanes"]["cards"][0]["opgg"]["status"] == "ok":
                break
    else:
        pytest.fail("OP.GG data never reached the stream")
    resp.close()
    assert "ok" in seen


# ----------------------------------------------------------------- the page
def test_the_page_has_the_lane_section_and_the_script_knows_how_to_fill_it(rig):
    html = rig.app.test_client().get("/partida").get_data(as_text=True)
    for needle in ('id="sel-lanes"', 'id="sel-lanes-box"', 'id="sel-lanes-msg"', 'id="sel-lanes-sub"'):
        assert needle in html
    js = (STATIC / "seleccion.js").read_text(encoding="utf-8")
    for needle in ("function renderLanes", "esc(o.tip)", "esc(s.note", "esc(c.ally.name"):
        assert needle in js


HARNESS = TESTS / "sel_harness.js"


def run_harness(payload, tmp_path, open_roles=()):
    path = tmp_path / "payload.json"
    path.write_text(json.dumps(payload), encoding="utf-8")
    out = subprocess.run([node, str(HARNESS), str(STATIC / "seleccion.js"), str(path), ",".join(open_roles)],
                         capture_output=True, text=True, timeout=30, encoding="utf-8")
    assert out.returncode == 0, out.stderr
    return json.loads(out.stdout)


@pytest.mark.skipif(node is None, reason="Node no está instalado")
def test_the_script_renders_my_lane_detailed_and_the_others_compact(rig, tmp_path):
    rig.publish(normal(4))
    rig.payload()
    rig.settle()
    rig.payload()
    rig.settle()
    shown = run_harness(rig.payload(), tmp_path)
    html = shown["lanes"]
    assert html.count('<article class="lane mine">') == 1 and html.count("<details") == 4
    for needle in ("Mid · tú", "Diana", "Akali", "de líneas ganadas", "Diamante+", "fuerte en nivel 2 y 6", "Rival",
                   "2 de 3 líneas ganadas", "Ventaja en la línea: Akali", "Stay away from Akali&#39;s shroud.", "OP.GG",
                   "Duración", "52%"):
        assert needle in html, needle
    assert "consultando OP.GG" not in html and shown["boxHidden"] is False
    assert "Diamante+" in shown["sub"] and shown["enemies"].count("Deducida con los roles de OP.GG") == 5


@pytest.mark.skipif(node is None, reason="Node no está instalado")
def test_the_script_states_every_case_and_escapes_what_it_shows(rig, tmp_path):
    rig.publish(normal(1))
    rig.payload()
    rig.settle()
    data = rig.payload()
    data["lanes"]["cards"][0]["best"]["items"][0]["name"] = "<img src=x onerror=alert(1)>"
    data["lanes"]["cards"][1]["opgg"] = {"status": "ok", "games": 900, "wr": 0.5, "wr_text": None, "tone": "few",
                                         "lane": None, "solo": None, "tip": "<script>alert(1)</script>"}
    shown = run_harness(data, tmp_path, open_roles=["BOTTOM"])
    html = shown["lanes"]
    assert "Mejores contra Akali" in html and "&lt;img src=x onerror=alert(1)&gt;" in html and "<img src=x" not in html
    assert "&lt;script&gt;alert(1)&lt;/script&gt;" in html and "<script>" not in html
    assert "pocos datos (900 partidas)" in html
    assert '<details class="lane" data-role="BOTTOM" open>' in html          # the viewer's choice survives a re-render
    aram = run_harness({**data, "lanes": None}, tmp_path)
    assert aram["boxHidden"] is True and aram["lanes"] == ""
