import json
import random

import pytest
import requests
from lane_fixtures import BLUE_MID, bought, make_lane_match, make_lane_timeline, mid_edge

from lolan import sampler, samples
from lolan.opgg import OpggError
from lolan.riot import (InvalidApiKeyError, Limiters, NotFoundError, RiotClient, RiotConnectionError,
                        RiotError)
from lolan.sampler import Downloader, DownloadState, OpggLeaderboards, RiotRegionApi
from lolan.store import Store

GV = "16.21.824.8524"


class Clock:
    def __init__(self, now=1_700_000_000.0):
        self.now = now
        self.sleeps = []

    def __call__(self):
        return self.now

    def sleep(self, seconds):
        self.sleeps.append(seconds)
        self.now += seconds


class FakeApi:
    """One region's Riot API: players are puuid -> match ids; matches are built on demand."""

    def __init__(self, players=None, versions=None, queues=None, durations=None, league=None, wait=0.0, log=None):
        self.players = players or {}               # puuid -> [match ids]
        self.versions = versions or {}             # match id -> game version
        self.queues = queues or {}
        self.durations = durations or {}
        self.league = league or {}                 # "master" / "DIAMOND:I" -> entries
        self.wait = wait
        self.log = log if log is not None else []
        self.unknown_names = set()
        self.fail = {}                             # call name -> exception raised from that call on
        self.calls = 0

    def _call(self, name, *args):
        self.log.append((name, *args))
        self.calls += 1
        exc = self.fail.get(name)
        if exc is not None:
            raise exc

    def wait_estimate(self):
        return self.wait

    def top_league(self, tier):
        self._call("top_league", tier)
        return {"tier": tier.upper(), "entries": list(self.league.get(tier, []))}

    def league_page(self, tier, division, page):
        self._call("league_page", tier, division, page)
        return list(self.league.get(f"{tier}:{division}", []))

    def puuid(self, name, tag):
        self._call("puuid", name, tag)
        if name in self.unknown_names:
            raise NotFoundError("no")
        return f"P-{name}"

    def match_ids(self, puuid, count, start_time):
        self._call("match_ids", puuid, count, start_time)
        return list(self.players.get(puuid, []))

    def match(self, match_id):
        self._call("match", match_id)
        return make_lane_match(match_id, version=self.versions.get(match_id, GV),
                               queue=self.queues.get(match_id, 420),
                               duration=self.durations.get(match_id, 1200))

    def timeline(self, match_id):
        self._call("timeline", match_id)
        gold, xp = mid_edge(100, 30)
        return make_lane_timeline(gold=gold, xp=xp, events=[bought(BLUE_MID, 3031, 600_000)])


class FakeOpgg:
    def __init__(self, boards):
        self.boards = boards                       # champion -> [(name, tag, tier)]
        self.calls = []

    def leaderboard(self, code, champ):
        self.calls.append((code, champ))
        board = self.boards.get((code, champ), self.boards.get(champ))
        if isinstance(board, Exception):
            raise board
        return list(board or [])


class FakeDD:
    def __init__(self, versions=("16.21.1", "16.20.1", "16.19.1", "16.18.1")):
        self._versions = list(versions)
        self.prepared = []

    def versions(self):
        return self._versions

    def prepare(self, game_versions=(None,), champions=(), wait=0.0, minimap=True, **kwargs):
        self.prepared.append(tuple(game_versions))

    def read_json(self, rel):
        if rel == "16.21.1/data/es_MX/item.json":
            return {"data": {"3031": {"gold": {"purchasable": True, "total": 3400}, "tags": ["Damage"]}}}
        return None


def make_dl(tmp_path, apis, pool=None, opgg=None, clock=None, dd=None, **kwargs):
    clock = clock or Clock()
    store = samples.LineStore(tmp_path / "lineas")
    kwargs.setdefault("say", lambda *a: None)
    dl = Downloader(store, apis, pool=pool, opgg=opgg, dd=dd, clock=clock, sleep=clock.sleep, **kwargs)
    return dl, store, clock


def ids(prefix, n):
    return [f"{prefix}_{i}" for i in range(n)]


def board(*names, tier="MASTER"):
    return [(n, "TAG", tier) for n in names]


# ------------------------------------------------------------------ regions
def test_parse_regions_and_errors():
    assert sampler.parse_regions("kr, EUW1 ,kr") == ["kr", "euw"]
    assert sampler.parse_regions("na;lan") == ["na", "lan"]
    for bad in ("", "zz", "kr,mars"):
        with pytest.raises(ValueError):
            sampler.parse_regions(bad)
    assert sampler.DEFAULT_REGIONS == ("kr", "euw")
    assert "la1" not in {sampler.REGIONS[r][0] for r in sampler.DEFAULT_REGIONS}


# ------------------------------------------------------------------ queue order
def test_leaderboard_players_go_round_robin_across_pool_champions(tmp_path):
    api = FakeApi()
    opgg = FakeOpgg({103: board("a1", "a2", "a3"), 108: board("b1", "b2", "b3"), 110: board("c1", "c2", "c3")})
    dl, _store, _ = make_dl(tmp_path, {"kr": api}, pool={103: 9, 108: 5, 110: 1}, opgg=opgg, solo_pool=True)
    result = dl.run()
    assert result.stopped == "agotado"
    resolved = [c[1] for c in api.log if c[0] == "puuid"]
    assert resolved == ["a1", "b1", "c1", "a2", "b2", "c2", "a3", "b3", "c3"]
    assert opgg.calls == [("KR", 103), ("KR", 108), ("KR", 110)]


def test_solo_pool_never_asks_for_league_breadth(tmp_path):
    entries = [{"puuid": f"L{i}"} for i in range(4)]
    api = FakeApi(league={"DIAMOND:I": entries, "master": entries})
    dl, _s, _ = make_dl(tmp_path, {"kr": api}, pool={103: 3}, opgg=FakeOpgg({103: board("a1", "a2")}), solo_pool=True)
    assert dl.run().stopped == "agotado"
    assert not [c for c in api.log if c[0] in ("league_page", "top_league")]


def test_leaderboards_come_before_league_breadth(tmp_path):
    entries = [{"puuid": f"L{i}", "inactive": False} for i in range(4)]
    api = FakeApi(league={"DIAMOND:I": entries})
    dl, _s, _ = make_dl(tmp_path, {"kr": api}, pool={103: 3}, opgg=FakeOpgg({103: board("a1", "a2")}),
                        rng=random.Random(0))
    for _ in range(60):                                  # a few players, then the league
        dl._step("kr")
        dl.state.region("kr")["current"] = None
        if any(c[0] == "league_page" for c in api.log):
            break
    calls = [c for c in api.log if c[0] in ("puuid", "league_page", "match_ids")]
    first_league = next(i for i, c in enumerate(calls) if c[0] == "league_page")
    assert [c[1] for c in calls[:first_league] if c[0] == "puuid"] == ["a1", "a2"]
    assert [c[1] for c in calls[first_league:] if c[0] == "match_ids"][:1] != []
    league_ids = [c[1] for c in calls[first_league:] if c[0] == "match_ids"]
    assert league_ids and all(p.startswith("L") for p in league_ids)


def test_only_diamond_plus_and_unknown_accounts_are_skipped(tmp_path):
    api = FakeApi()
    api.unknown_names = {"gone"}
    opgg = FakeOpgg({103: board("gone", "ok")})
    dl, _s, _ = make_dl(tmp_path, {"kr": api}, pool={103: 1}, opgg=opgg, solo_pool=True)
    dl.run()
    assert [c[1] for c in api.log if c[0] == "match_ids"] == ["P-ok"]


def test_opgg_failure_does_not_stop_the_league_breadth(tmp_path):
    entries = [{"puuid": "L1"}, {"puuid": "L2"}]
    api = FakeApi(players={"L1": ["KR_1"], "L2": ["KR_2"]}, league={"DIAMOND:I": entries})
    opgg = FakeOpgg({103: OpggError("OP.GG no responde")})
    dl, store, _ = make_dl(tmp_path, {"kr": api}, pool={103: 1}, opgg=opgg, max_matches=2)
    assert dl.run().stopped == "max"
    assert set(store.processed()) == {"KR_1", "KR_2"}
    assert dl.state.data["last_error"]["kind"] == "opgg"


# ------------------------------------------------------------------ matches
def test_matches_become_diamante_rows_with_the_players_tier_and_no_identities(tmp_path):
    api = FakeApi(players={"P-a1": ["KR_1", "KR_2"]})
    opgg = FakeOpgg({103: board("a1", tier="DIAMOND")})
    dl, store, _ = make_dl(tmp_path, {"kr": api}, pool={103: 1, 108: 1}, opgg=opgg, solo_pool=True, dd=FakeDD())
    result = dl.run()
    assert result.stopped == "agotado" and result.fetched == 2 and result.rows == 20
    rows = store.read_rows()
    assert len(rows) == 20 and {r["source"] for r in rows} == {"diamante"}
    assert {r["tier"] for r in rows} == {"DIAMOND"} and {r["person"] for r in rows} == {None}
    assert {r["platform"] for r in rows} == {"kr"} and {r["patch"] for r in rows} == {"16.21"}
    mid = next(r for r in rows if r["role"] == "MIDDLE" and r["side"] == 100)
    assert mid["items"] == [10.0, None, None]            # Data Dragon item data for the patch was used
    assert (("16.21.824.8524",) in dl.dd.prepared) and store.processed()["KR_1"] == ["diamante", "ok"]
    # nothing that identifies a stranger is written anywhere under data/lineas (but the state)
    blob = "".join(p.read_text(encoding="utf-8") for p in (tmp_path / "lineas").rglob("*")
                   if p.is_file() and p.name != "descarga.json")
    for secret in ("puuid-", "Jugador", "P-a1", '"a1"'):
        assert secret not in blob
    state = (tmp_path / "lineas" / "descarga.json").read_text(encoding="utf-8")
    assert "P-a1" not in state and '"a1"' not in state      # processed players leave the queue


def test_processed_and_repeated_matches_are_not_downloaded_again(tmp_path):
    api = FakeApi(players={"P-a1": ["KR_old", "KR_1", "KR_2"], "P-b1": ["KR_2", "KR_3"]})
    opgg = FakeOpgg({103: board("a1", "b1")})
    dl, store, _ = make_dl(tmp_path, {"kr": api}, pool={103: 1}, opgg=opgg, solo_pool=True)
    store.mark_processed({"KR_old": ["diamante", "ok"]})
    dl.known = set(store.processed())
    dl.run()
    fetched = [c[1] for c in api.log if c[0] == "match"]
    assert fetched == ["KR_1", "KR_2", "KR_3"]
    again, _s2, _ = make_dl(tmp_path, {"kr": FakeApi(players=api.players)}, pool={103: 1}, opgg=opgg, solo_pool=True)
    again.state.data["seen"] = {}
    again.run()
    assert not [c for c in again.apis["kr"].log if c[0] in ("match", "timeline")]


def test_old_patches_wrong_queues_and_short_games_are_skipped_before_the_timeline(tmp_path):
    api = FakeApi(players={"P-a1": ["KR_new", "KR_old", "KR_aram", "KR_short"]},
                  versions={"KR_old": "16.17.1.1"}, queues={"KR_aram": 450}, durations={"KR_short": 700})
    opgg = FakeOpgg({103: board("a1")})
    dl, store, _ = make_dl(tmp_path, {"kr": api}, pool={103: 1}, opgg=opgg, solo_pool=True, dd=FakeDD())
    dl.run()
    assert [c[1] for c in api.log if c[0] == "timeline"] == ["KR_new"]
    done = store.processed()
    assert done["KR_old"] == ["diamante", "parche_antiguo"] and done["KR_aram"] == ["diamante", "cola"]
    assert done["KR_short"][1] in ("remake", "corta") and done["KR_new"] == ["diamante", "ok"]
    assert {r["patch"] for r in store.read_rows()} == {"16.21"}


def test_match_list_asks_for_the_last_patches_window(tmp_path):
    api = FakeApi(players={"P-a1": []})
    dl, _s, clock = make_dl(tmp_path, {"kr": api}, pool={103: 1}, opgg=FakeOpgg({103: board("a1")}), solo_pool=True)
    dl.run()
    _name, _puuid, count, start = next(c for c in api.log if c[0] == "match_ids")
    assert count == 20 and start == int(clock.now) - sampler.WINDOW_DAYS * 86400


# ------------------------------------------------------------------ scheduling
def test_regions_alternate_and_the_busy_one_waits(tmp_path):
    a = FakeApi(players={f"P-a{i}": [f"KR_{i}"] for i in range(3)})
    b = FakeApi(players={f"P-b{i}": [f"EUW1_{i}"] for i in range(3)})
    opgg = FakeOpgg({103: board("a0", "a1", "a2")})
    dl, _s, _ = make_dl(tmp_path, {"kr": a, "euw": b}, pool={103: 1}, opgg=opgg)
    # euw has no OP.GG players here; breadth entries come from its league
    b.league = {"DIAMOND:I": [{"puuid": f"P-b{i}"} for i in range(3)]}
    a.wait = 50.0                                       # kr's limiter is saturated
    assert dl._pick_region() == "euw"
    a.wait = 0.0
    dl.by_region["euw"] = 5
    assert dl._pick_region() == "kr"                    # ties go to the region with fewer matches
    dl.by_region["euw"] = 0
    assert dl._pick_region() == "kr"                    # ... then to the first listed


def test_two_regions_both_get_downloaded(tmp_path):
    a = FakeApi(players={"P-a0": ["KR_0", "KR_1"]})
    b = FakeApi(players={"P-b0": ["EUW1_0", "EUW1_1"]})
    opgg = FakeOpgg({("KR", 103): board("a0"), ("EUW", 103): board("b0")})
    dl, store, _ = make_dl(tmp_path, {"kr": a, "euw": b}, pool={103: 1}, opgg=opgg, solo_pool=True)
    dl.run()
    assert set(store.processed()) == {"KR_0", "KR_1", "EUW1_0", "EUW1_1"}
    assert {r["platform"] for r in store.read_rows()} == {"kr", "euw1"}
    assert dl.by_region["kr"] == 2 and dl.by_region["euw"] == 2
    assert set(opgg.calls) == {("KR", 103), ("EUW", 103)}


def test_stops_after_max_matches_and_after_the_hours(tmp_path):
    api = FakeApi(players={"P-a1": ids("KR", 6)})
    dl, store, _ = make_dl(tmp_path, {"kr": api}, pool={103: 1}, opgg=FakeOpgg({103: board("a1")}),
                           solo_pool=True, max_matches=3)
    assert dl.run().stopped == "max" and dl.fetched == 3 and len(store.processed()) == 3

    clock = Clock()

    class Slow(FakeApi):
        def match(self, match_id):
            clock.sleep(1800)                           # half an hour per match
            return super().match(match_id)

    dl2, _s2, _ = make_dl(tmp_path / "h", {"kr": Slow(players={"P-a1": ids("KR", 9)})}, pool={103: 1},
                          opgg=FakeOpgg({103: board("a1")}), solo_pool=True, hours=1, clock=clock)
    result = dl2.run()
    assert result.stopped == "horas" and dl2.fetched == 2


# ------------------------------------------------------------------ never disturb a game
def test_pauses_while_the_client_is_in_champ_select_or_a_game(tmp_path, monkeypatch):
    monkeypatch.setattr(sampler, "PHASE_CHECK_S", 0)
    api = FakeApi(players={"P-a1": ["KR_1", "KR_2"]})
    phases = iter(["Lobby", "ChampSelect", "InProgress", "InProgress", "EndOfGame", None, None, None, None, None])
    said = []
    dl, _s, clock = make_dl(tmp_path, {"kr": api}, pool={103: 1}, opgg=FakeOpgg({103: board("a1")}), solo_pool=True,
                            phase_fn=lambda: next(phases, None), say=said.append)
    dl.run()
    assert clock.sleeps.count(sampler.PHASE_POLL_S) == 3     # ChampSelect, InProgress, InProgress
    assert sum("Pausado" in s for s in said) == 1 and sum("Reanudando" in s for s in said) == 1
    assert dl.paused_s == 3 * sampler.PHASE_POLL_S
    assert dl.fetched == 2                                   # and it went on afterwards


def test_no_riot_call_is_made_while_paused(tmp_path, monkeypatch):
    monkeypatch.setattr(sampler, "PHASE_CHECK_S", 0)
    api = FakeApi(players={"P-a1": ["KR_1"]})
    clock = Clock()
    seen_during_pause = []
    phases = iter(["InProgress", "InProgress", None])

    def phase():
        seen_during_pause.append(api.calls)
        return next(phases, None)

    dl, _s, _ = make_dl(tmp_path, {"kr": api}, pool={103: 1}, opgg=FakeOpgg({103: board("a1")}), solo_pool=True,
                        clock=clock, phase_fn=phase)
    dl.run()
    assert seen_during_pause[:3] == [0, 0, 0]               # nothing was requested until the game ended


def test_no_client_means_no_pause(tmp_path, monkeypatch):
    monkeypatch.setattr(sampler, "PHASE_CHECK_S", 0)
    api = FakeApi(players={"P-a1": ["KR_1"]})
    dl, _s, clock = make_dl(tmp_path, {"kr": api}, pool={103: 1}, opgg=FakeOpgg({103: board("a1")}), solo_pool=True,
                            phase_fn=lambda: None)
    dl.run()
    assert clock.sleeps == [] and dl.fetched == 1


# ------------------------------------------------------------------ resume, key expiry, errors
def test_run_resumes_from_the_state_file(tmp_path):
    players = {"P-a1": ids("KR_A", 4), "P-a2": ids("KR_B", 2)}
    opgg = FakeOpgg({103: board("a1", "a2")})
    first = FakeApi(players=players)
    dl, store, _ = make_dl(tmp_path, {"kr": first}, pool={103: 1}, opgg=opgg, solo_pool=True, max_matches=2)
    assert dl.run().stopped == "max"
    state = json.loads((tmp_path / "lineas" / "descarga.json").read_text(encoding="utf-8"))
    assert state["regions"]["kr"]["current"]["ids"] == ["KR_A_2", "KR_A_3"]
    assert state["regions"]["kr"]["lb_queue"]["103"] == [["a2", "TAG", "MASTER"]]

    second = FakeApi(players=players)
    dl2, store2, _ = make_dl(tmp_path, {"kr": second}, pool={103: 1}, opgg=opgg, solo_pool=True)
    assert dl2.run().stopped == "agotado"
    assert [c[1] for c in second.log if c[0] == "match"] == ["KR_A_2", "KR_A_3", "KR_B_0", "KR_B_1"]
    assert [c[1] for c in second.log if c[0] == "match_ids"] == ["P-a2"]   # the first player was not listed again
    assert not [c for c in second.log if c[0] == "puuid" and c[1] == "a1"]
    assert opgg.calls.count(("KR", 103)) == 1                       # fresh leaderboards are not fetched again
    assert len(store2.processed()) == 6 and dl2.state.totals["matches"] == 6 and dl2.state.totals["runs"] == 2


def test_invalid_key_stops_cleanly_keeping_everything(tmp_path):
    api = FakeApi(players={"P-a1": ids("KR", 5)})
    opgg = FakeOpgg({103: board("a1")})
    said = []
    dl, store, _ = make_dl(tmp_path, {"kr": api}, pool={103: 1}, opgg=opgg, solo_pool=True, say=said.append)
    original = api.match
    state = {"n": 0}

    def match(match_id):
        state["n"] += 1
        if state["n"] == 3:
            raise InvalidApiKeyError("expiró")
        return original(match_id)

    api.match = match
    result = dl.run()
    assert result.stopped == "clave" and "expiró" in result.message and not result.ok
    assert result.fetched == 2 and set(store.processed()) == {"KR_0", "KR_1"}      # saved before stopping
    assert len(store.read_rows()) == 20
    saved = json.loads((tmp_path / "lineas" / "descarga.json").read_text(encoding="utf-8"))
    assert saved["last_error"]["kind"] == "clave"
    assert saved["regions"]["kr"]["current"]["ids"][0] == "KR_2"                  # the failed match is next
    lines = sampler.run_summary(result, dl.state, dl.coverage())
    assert any("quedó guardado" in line for line in lines)

    api.match = original                                                            # a new key: it goes on
    dl2, _s2, _ = make_dl(tmp_path, {"kr": api}, pool={103: 1}, opgg=opgg, solo_pool=True)
    assert dl2.run().stopped == "agotado" and len(dl2.store.processed()) == 5


def test_invalid_key_while_listing_keeps_the_player(tmp_path):
    api = FakeApi(players={"P-a1": ["KR_1"]})
    api.fail["match_ids"] = InvalidApiKeyError("caducó")
    opgg = FakeOpgg({103: board("a1")})
    dl, _s, _ = make_dl(tmp_path, {"kr": api}, pool={103: 1}, opgg=opgg, solo_pool=True)
    assert dl.run().stopped == "clave"
    saved = json.loads((tmp_path / "lineas" / "descarga.json").read_text(encoding="utf-8"))
    assert saved["regions"]["kr"]["current"]["puuid"] == "P-a1"


def test_connection_loss_and_error_streaks_stop_the_run(tmp_path):
    api = FakeApi(players={"P-a1": ["KR_1"]})
    api.fail["match"] = RiotConnectionError("sin internet")
    dl, _s, _ = make_dl(tmp_path, {"kr": api}, pool={103: 1}, opgg=FakeOpgg({103: board("a1")}), solo_pool=True)
    assert dl.run().stopped == "conexion"

    api2 = FakeApi(players={"P-a1": ids("KR", 20)})
    api2.fail["match"] = RiotError("boom")
    dl2, _s2, _ = make_dl(tmp_path / "e", {"kr": api2}, pool={103: 1}, opgg=FakeOpgg({103: board("a1")}), solo_pool=True)
    result = dl2.run()
    assert result.stopped == "errores" and api2.log.count(("match", "KR_0")) == 1     # each failed match is given up


def test_one_failed_match_does_not_stop_the_rest(tmp_path):
    api = FakeApi(players={"P-a1": ["KR_1", "KR_2", "KR_3"]})
    original = api.match

    def match(match_id):
        if match_id == "KR_2":
            raise RiotError("Respuesta inesperada")
        return original(match_id)

    api.match = match
    dl, store, _ = make_dl(tmp_path, {"kr": api}, pool={103: 1}, opgg=FakeOpgg({103: board("a1")}), solo_pool=True)
    assert dl.run().stopped == "agotado"
    assert set(store.processed()) == {"KR_1", "KR_3"} and dl.state.data["last_error"]["kind"] == "riot"


def test_missing_match_or_timeline_is_marked_and_not_retried(tmp_path):
    api = FakeApi(players={"P-a1": ["KR_1", "KR_2", "KR_3"]})
    original_m, original_t = api.match, api.timeline

    def match(match_id):
        if match_id == "KR_1":
            raise NotFoundError("no")
        return original_m(match_id)

    def timeline(match_id):
        if match_id == "KR_2":
            raise NotFoundError("no")
        return original_t(match_id)

    api.match, api.timeline = match, timeline
    dl, store, _ = make_dl(tmp_path, {"kr": api}, pool={103: 1}, opgg=FakeOpgg({103: board("a1")}), solo_pool=True)
    dl.run()
    assert store.processed() == {"KR_1": ["diamante", "no_encontrada"], "KR_2": ["diamante", "sin_timeline"],
                                 "KR_3": ["diamante", "ok"]}


# ------------------------------------------------------------------ 429 through the real client
class Resp:
    def __init__(self, status, payload=None, headers=None):
        self.status_code, self._payload, self.headers = status, payload, headers or {}

    def json(self):
        return self._payload


class Session:
    def __init__(self, routes):
        self.routes, self.calls = routes, []

    def get(self, url, params=None, headers=None, timeout=None):
        self.calls.append((url, params))
        for fragment, answers in self.routes.items():
            if fragment in url:
                item = answers.pop(0) if len(answers) > 1 else answers[0]
                if isinstance(item, Exception):
                    raise item
                return item
        raise AssertionError(url)


def region_api(session, clock):
    def factory(key, region, limiter):
        return RiotClient(key, region=region, session=session, sleep=clock.sleep, clock=clock, limiter=limiter)
    return RiotRegionApi(sampler.Region("kr"), "KEY", Limiters(clock=clock, sleep=clock.sleep), factory=factory)


def test_riot_region_api_uses_separate_hosts_and_limiters():
    clock = Clock()
    session = Session({"/ids": [Resp(200, ["KR_1"])], "leagues": [Resp(200, {"entries": []})]})
    api = region_api(session, clock)
    assert api._platform.limiter is not api._route.limiter
    assert api.match_ids("pp", 20, 1_700_000_000) == ["KR_1"]
    api.top_league("master")
    urls = [u for u, _p in session.calls]
    assert urls[0].startswith("https://asia.api.riotgames.com/") and "queue=" not in urls[0]
    assert session.calls[0][1] == {"start": 0, "count": 20, "queue": 420, "startTime": 1_700_000_000}
    assert urls[1].startswith("https://kr.api.riotgames.com/lol/league/v4/masterleagues")


def test_429_waits_for_retry_after_and_goes_on():
    clock = Clock()
    session = Session({"/matches/KR_1": [Resp(429, headers={"Retry-After": "7"}), Resp(200, {"info": {}})]})
    api = region_api(session, clock)
    assert api.match("KR_1") == {"info": {}}
    assert 7.0 in clock.sleeps


def test_persistent_429_is_an_error_not_a_crash(tmp_path):
    clock = Clock()
    session = Session({"/ids": [Resp(200, ["KR_1", "KR_2"])],
                       "/matches/KR_1": [Resp(429, headers={"Retry-After": "1"})],
                       "/matches/KR_2": [Resp(200, make_lane_match("KR_2"))],
                       "/timeline": [Resp(200, make_lane_timeline())]})
    api = region_api(session, clock)
    store = samples.LineStore(tmp_path / "lineas")
    dl = Downloader(store, {"kr": api}, pool={103: 1}, opgg=FakeOpgg({103: board("a1")}), solo_pool=True,
                    clock=clock, sleep=clock.sleep, say=lambda *a: None)
    api.puuid = lambda name, tag: "pp"
    result = dl.run()
    assert result.stopped == "agotado"                      # KR_1 was given up on, KR_2 downloaded
    assert set(store.processed()) == {"KR_2"} and dl.state.data["last_error"]["kind"] == "riot"
    assert clock.sleeps.count(1.0) >= 10                      # Retry-After honoured on every retry


# ------------------------------------------------------------------ rebuild
def test_aggregates_are_rebuilt_every_so_many_matches_and_at_the_end(tmp_path, monkeypatch):
    monkeypatch.setattr(sampler, "FLUSH_EVERY", 2)
    monkeypatch.setattr(sampler, "REBUILD_EVERY", 4)
    api = FakeApi(players={"P-a1": ids("KR", 9)})
    dl, store, _ = make_dl(tmp_path, {"kr": api}, pool={103: 1}, opgg=FakeOpgg({103: board("a1")}), solo_pool=True)
    calls = []
    real = store.rebuild
    monkeypatch.setattr(store, "rebuild", lambda *a, **k: calls.append(1) or real(*a, **k))
    dl.run()
    assert len(calls) == 3                                   # after 4 and 8 matches, and the last one at the end
    assert (tmp_path / "lineas" / "duelos" / "MIDDLE" / "103.json").is_file()
    assert (tmp_path / "lineas" / "indice.json").is_file()


def test_pool_coverage_counts_rows_of_pool_champions(tmp_path):
    api = FakeApi(players={"P-a1": ids("KR", 3)})
    dl, store, _ = make_dl(tmp_path, {"kr": api}, pool={103: 5, 108: 2, 999: 1}, opgg=FakeOpgg({103: board("a1")}),
                           solo_pool=True)
    dl.run()
    assert dl.state.totals["by_champion"] == {"103": 3, "108": 3}
    assert dl.coverage() == [("#999", 0), ("Champ3", 3), ("Champ8", 3)]       # least covered first
    lines = sampler.status_lines(DownloadState(tmp_path / "lineas" / "descarga.json"), store)
    assert any("Región KR" in ln for ln in lines) and any(ln.startswith("Partidas descargadas: 3") for ln in lines)


# ------------------------------------------------------------------ OP.GG, pool, status
LEADERBOARD_TEXT = '''class LolListChampionLeaderboard: region,champion,leaderboard
class Leaderboard: rank,summoner
class Summoner: game_name,tagline,league_stats
class LeagueStat: game_type,tier_info
class TierInfo: tier,division,lp

LolListChampionLeaderboard("KR","131",[Leaderboard(1,Summoner("달중독","KR1",[LeagueStat("SOLORANKED",TierInfo("DIAMOND",2,11)),LeagueStat("FLEXRANKED",TierInfo("SILVER",1,14))])),Leaderboard(2,Summoner("Emer","EU",[LeagueStat("SOLORANKED",TierInfo("EMERALD",1,5))])),Leaderboard(3,Summoner("Flexy","F",[LeagueStat("FLEXRANKED",TierInfo("MASTER",1,5))])),Leaderboard(4,Summoner("UmmMasitda","KR1",[LeagueStat("SOLORANKED",TierInfo("MASTER",1,117))]))])'''
CHAMPIONS_TEXT = '''{"data": {"champions": [{"champion_id": 131, "key": "Diana", "name": "Diana"}, {"champion_id": 62, "key": "MonkeyKing", "name": "Wukong"}]}}'''


class FakeMcp:
    def __init__(self):
        self.calls = []

    def call(self, tool, arguments):
        self.calls.append((tool, arguments))
        return CHAMPIONS_TEXT if tool == "lol_list_champions" else LEADERBOARD_TEXT


def test_opgg_leaderboard_keeps_only_solo_diamond_plus_and_never_uses_their_puuids():
    mcp = FakeMcp()
    lb = OpggLeaderboards(mcp)
    assert lb.leaderboard("KR", 131) == [("달중독", "KR1", "DIAMOND"), ("UmmMasitda", "KR1", "MASTER")]
    tool, args = mcp.calls[-1]
    assert tool == "lol_list_champion_leaderboard" and args["region"] == "KR" and args["champion"] == "DIANA"
    assert not any("puuid" in f for f in args["desired_output_fields"])
    assert lb.leaderboard("KR", 77777) == []                 # unknown to OP.GG: nothing, no call


def test_clan_pool_counts_champions_a_clan_member_plays_in_a_role(tmp_path):
    store = Store(":memory:")
    store.add_player("puuid-3", "Epsilon Osiris#LAN", is_main=True)
    for i in range(3):          # Epsilon Osiris: 3 games with champion 103 mid
        store.save_match(f"LA1_{i}", make_lane_match(f"LA1_{i}", names={3: "Epsilon Osiris"}, puuids={3: "puuid-3"},
                                                     created=1_700_000_000_000 + i), None)
    for i in range(3, 5):       # ... and only 2 as champion 108 (red mid): below the threshold
        store.save_match(f"LA1_{i}", make_lane_match(f"LA1_{i}", names={8: "Epsilon Osiris"},
                                                     puuids={3: "other-3", 8: "puuid-3"},
                                                     created=1_700_000_000_000 + i), None)
    store.save_match("LA1_x", make_lane_match("LA1_x", names={4: "Epsilon Cefo"}, puuids={3: "zzz", 4: "puuid-4"}), None)
    pool = sampler.clan_pool(store)
    assert pool == {103: 3}
    assert 108 not in pool and 104 not in pool
    assert sampler.clan_pool(store, min_games=2).get(108) == 2


def test_status_without_state(tmp_path):
    lines = sampler.status_lines(DownloadState(tmp_path / "descarga.json"), samples.LineStore(tmp_path))
    assert lines == ["Todavía no hay descarga: ejecuta python -m lolan muestras."]
