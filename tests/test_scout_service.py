"""The scouting service: jobs, the 30 s search, PUUID handling, conflicts with a sync."""
import threading

import pytest
from conftest import ME_PUUID as STORED_ME_PUUID
from scout_fixtures import ALLIES, ME, ME_PUUID, NOW, World, spectator_game

from lolan import scoutsvc
from lolan.riot import ApiSlot, InvalidApiKeyError, Limiters
from lolan.scoutsvc import ScoutService
from lolan.settings import KeyHealth
from lolan.store import Store


@pytest.fixture
def root(tmp_path, monkeypatch):
    monkeypatch.setenv("LOLAN_HOME", str(tmp_path))
    for var in ("RIOT_API_KEY", "RIOT_ID", "PLATFORM", "REGION"):
        monkeypatch.delenv(var, raising=False)
    write_env(tmp_path)
    return tmp_path


def write_env(root, key="RGAPI-one"):
    (root / ".env").write_text(f"RIOT_API_KEY={key}\nRIOT_ID={ME}\n", encoding="utf-8")


class Rig:
    def __init__(self, root):
        self.root = root
        self.world = World()
        self.world.stock(spectator_game())
        self.slot = ApiSlot()
        self.health = KeyHealth()
        self.notified = 0
        self.keys = []

        def factory(key, host):
            self.keys.append((key, host))
            return self.world.client(host)

        self.svc = ScoutService(root, Limiters(), self.slot, factory, health=self.health,
                                notify=self._notify, clock=lambda: NOW)

    def _notify(self):
        self.notified += 1

    def go(self, ref=None, status="loading", seq=0):
        code, body = self.svc.request(ref, status, seq)
        self.svc.join(10)
        return code, body


@pytest.fixture
def rig(root):
    return Rig(root)


def test_manual_search_builds_the_report_and_notifies(rig):
    code, body = rig.go()
    assert code == 202 and body["status"] == "searching"
    snap = rig.svc.snapshot()
    assert snap["status"] == "ready" and snap["target"] == ME and snap["busy"] is False
    assert snap["report"]["game"]["game_id"] == 7700001 and len(snap["report"]["teams"]["enemy"]) == 5
    assert rig.notified >= 2                                          # "buscando" and the report
    assert rig.world.names()[:2] == ["account", "spectator"]
    assert rig.slot.holder is None                                    # the slot is released afterwards


def test_not_in_a_game_says_so(rig):
    rig.world.games.clear()
    rig.go()
    snap = rig.svc.snapshot()
    assert snap["status"] == "nogame" and "no está en una partida" in snap["message"] and snap["report"] is None


def test_the_current_puuid_comes_from_account_v1_not_from_the_registry(rig):
    store = Store(rig.root / "data" / "lol.db")
    store.add_player("OLD-APP-PUUID", ME, is_main=True)               # a puuid from before a key change
    store.close()
    rig.go()
    assert ("spectator", ME_PUUID) in rig.world.calls and ("spectator", "OLD-APP-PUUID") not in rig.world.calls
    assert all("OLD-APP-PUUID" not in str(c) for c in rig.world.calls)


def test_puuid_is_looked_up_once_per_key_and_again_after_a_key_change(rig):
    rig.go()
    rig.go()
    assert rig.world.count("account") == 1
    write_env(rig.root, "RGAPI-two")
    rig.go()
    assert rig.world.count("account") == 2


def test_scouting_a_registered_player_uses_their_game(rig):
    rig.world.accounts["Rana Veloz#T002"] = "P-rana"
    rig.world.games["P-rana"] = spectator_game(game_id=1234)
    store = Store(rig.root / "data" / "lol.db")
    store.add_player("whatever", "Rana Veloz#T002")
    store.close()
    code, _ = rig.go("rana veloz#t002")
    snap = rig.svc.snapshot()
    assert code == 202 and snap["report"]["game"]["game_id"] == 1234
    assert snap["report"]["scouted"] == {"riot_id": "Rana Veloz#T002", "is_main": False}
    assert snap["report"]["viewer_label"] == "Rana Veloz"
    assert [t["riot_id"] for t in rig.svc.targets()] == [ME, "Rana Veloz#T002"]
    code, body = rig.svc.request("Desconocido#XXX", "waiting", 0)
    assert code == 400 and "no está registrado" in body["error"]


def test_manual_search_is_refused_during_a_sync_and_while_running(rig):
    assert rig.slot.acquire("sync")
    code, body = rig.svc.request(None, "waiting", 0)
    assert code == 409 and "sincronización en curso" in body["error"]
    assert rig.world.calls == []
    rig.slot.release("sync")
    started, release = threading.Event(), threading.Event()

    def slow(puuid):
        started.set()
        release.wait(5)

    rig.world.fail["league"] = slow
    code, _ = rig.svc.request(None, "waiting", 0)
    assert code == 202 and started.wait(5)
    assert rig.slot.holder == "scout"
    code, body = rig.svc.request(None, "waiting", 0)
    assert code == 409 and "Ya se está buscando" in body["error"]
    release.set()
    rig.svc.join(10)
    assert rig.slot.holder is None


def test_errors_are_friendly(rig):
    write_env(rig.root, "")
    rig.go()
    assert "Falta tu key" in rig.svc.snapshot()["message"]
    write_env(rig.root)
    rig.world.fail["spectator"] = InvalidApiKeyError("expiró")
    rig.go()
    snap = rig.svc.snapshot()
    assert snap["status"] == "error" and "inválida o expiró" in snap["message"]
    assert rig.health.is_invalid("RGAPI-one")
    rig.world.fail.clear()
    rig.world.accounts.clear()
    rig.go("")
    rig.svc.resolver._cache.clear()
    rig.go()
    assert "No se encontró la cuenta Yo#LAN" in rig.svc.snapshot()["message"]


# ------------------------------------------------------------ the 30 s search
def test_automatic_search_needs_a_listener_and_no_game_in_progress(rig):
    assert rig.svc.tick("waiting", 0, listeners=0, now=1000) is False
    assert rig.svc.tick("playing", 1, listeners=1, now=1000) is False
    assert rig.world.calls == [] and rig.svc.auto_on is False
    assert rig.svc.tick("loading", 0, listeners=1, now=1000) is True
    rig.svc.join(10)
    assert rig.svc.snapshot()["status"] == "ready"
    assert rig.svc.auto_on is True


def test_automatic_search_asks_every_30_seconds_one_request(rig):
    rig.world.games.clear()
    assert rig.svc.tick("waiting", 0, 1, now=1000) is True
    rig.svc.join(10)
    assert rig.world.names() == ["account", "spectator"]              # one account lookup, then 1 request per poll
    assert rig.svc.tick("waiting", 0, 1, now=1010) is False           # too soon
    assert rig.svc.tick("waiting", 0, 1, now=1029) is False
    assert rig.svc.tick("waiting", 0, 1, now=1031) is True
    rig.svc.join(10)
    assert rig.world.names() == ["account", "spectator", "spectator"]
    assert rig.svc.snapshot()["status"] == "idle" and rig.notified == 0       # silence while nobody is in a game


def test_the_same_game_is_scouted_once_and_a_new_one_replaces_it(rig):
    rig.svc.tick("loading", 0, 1, now=1000)
    rig.svc.join(10)
    first = rig.svc.snapshot()["report"]
    used = len(rig.world.calls)
    rig.svc.tick("loading", 0, 1, now=1100)                           # polls again, same game id
    rig.svc.join(10)
    assert len(rig.world.calls) == used + 1 and rig.world.names()[-1] == "spectator"
    assert rig.svc.snapshot()["report"] is first
    rig.world.games[ME_PUUID] = spectator_game(game_id=9)
    rig.svc.tick("finished", 1, 1, now=1200)                          # the previous game ended; a new one loads
    rig.svc.join(10)
    assert rig.svc.snapshot()["report"]["game"]["game_id"] == 9


def test_automatic_search_waits_for_a_running_sync(rig):
    assert rig.slot.acquire("sync")
    rig.svc.tick("loading", 0, 1, now=1000)
    rig.svc.join(10)
    snap = rig.svc.snapshot()
    assert snap["status"] == "busy" and snap["report"] is None and "sincronización" in snap["message"]
    assert rig.world.names() == ["account", "spectator"]              # the scout itself did not start
    rig.slot.release("sync")
    rig.svc.tick("loading", 0, 1, now=1031)
    rig.svc.join(10)
    assert rig.svc.snapshot()["status"] == "ready"


def test_a_refused_key_silences_the_automatic_search(rig):
    rig.world.fail["spectator"] = InvalidApiKeyError("expiró")
    rig.svc.tick("waiting", 0, 1, now=1000)
    rig.svc.join(10)
    assert rig.svc.snapshot()["status"] == "error"
    calls = len(rig.world.calls)
    rig.svc.tick("waiting", 0, 1, now=1031)
    rig.svc.join(10)
    assert len(rig.world.calls) == calls                              # no request every 30 s with a dead key


# ------------------------------------------------- the report follows the live game
def test_report_is_kept_for_the_whole_game_and_dropped_for_the_next(rig):
    rig.svc.tick("loading", 0, 1, now=1000)
    rig.svc.join(10)
    assert rig.svc.snapshot()["report"] is not None
    rig.svc.reconcile("playing", 1)                                   # the game starts being played
    assert rig.svc.snapshot()["report"] is not None
    rig.svc.reconcile("playing", 1)
    rig.svc.reconcile("finished", 1)
    assert rig.svc.snapshot()["report"] is not None                   # kept in "finished"
    version = rig.svc.snapshot()["version"]
    rig.svc.reconcile("playing", 2)                                   # next game
    snap = rig.svc.snapshot()
    assert snap["report"] is None and snap["version"] > version and snap["status"] == "idle"


def test_report_found_while_playing_is_tied_to_that_game(rig):
    rig.go(status="playing", seq=3)
    rig.svc.reconcile("playing", 3)
    assert rig.svc.snapshot()["report"] is not None
    rig.svc.reconcile("playing", 4)
    assert rig.svc.snapshot()["report"] is None


def test_report_of_another_player_is_not_dropped_by_my_games(rig):
    rig.world.accounts["Rana Veloz#T002"] = "P-rana"
    rig.world.games["P-rana"] = spectator_game(game_id=55)
    store = Store(rig.root / "data" / "lol.db")
    store.add_player("x", "Rana Veloz#T002")
    store.close()
    rig.go("Rana Veloz#T002", status="playing", seq=1)
    rig.svc.reconcile("playing", 2)
    assert rig.svc.snapshot()["report"] is not None


def test_a_pending_report_whose_game_never_started_is_cleared(rig):
    rig.svc.tick("loading", 0, 1, now=1000)
    rig.svc.join(10)
    assert rig.svc.snapshot()["report"] is not None
    rig.world.games.clear()                                            # dodge / remake / played on another PC
    rig.svc.tick("loading", 0, 1, now=1100)
    rig.svc.join(10)
    assert rig.svc.snapshot()["report"] is None
