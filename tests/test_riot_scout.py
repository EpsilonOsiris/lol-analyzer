"""Riot client additions for scouting: per-host limiters, the API slot, new endpoints, test URL override."""
import threading

import pytest
from test_riot import FakeClock, FakeResponse, make_client

from lolan import riot
from lolan.riot import ApiSlot, Limiters, RateLimiter, RiotClient, TEST_URL_ENV


# ------------------------------------------------------------------ limiters
def test_one_limiter_per_host_and_each_host_counts_on_its_own():
    clock = FakeClock()
    limiters = Limiters(clock=clock, sleep=clock.sleep)
    la1, americas = limiters.for_host("la1"), limiters.for_host("americas")
    assert la1 is limiters.for_host("la1") is limiters.for_host(" LA1 ")
    assert la1 is not americas
    for _ in range(20):
        la1.acquire()
    assert la1.wait_estimate() > 1.0
    assert americas.wait_estimate() == 0.0              # the other host's bucket is untouched
    americas.acquire()
    assert clock.sleeps == []


def test_wait_estimate_does_not_use_up_a_request():
    clock = FakeClock()
    lim = RateLimiter(clock=clock, sleep=clock.sleep)
    for _ in range(20):
        lim.acquire()
    first = lim.wait_estimate()
    assert first == pytest.approx(1.0 + riot.WINDOW_MARGIN)
    assert lim.wait_estimate() == first
    clock.now += first
    assert lim.wait_estimate() == 0.0


def test_limiter_is_safe_across_threads():
    lim = RateLimiter(limits=((1000, 1.0),))
    done = []

    def worker():
        for _ in range(50):
            lim.acquire()
        done.append(1)

    threads = [threading.Thread(target=worker) for _ in range(8)]
    for t in threads:
        t.start()
    for t in threads:
        t.join(10)
    assert len(done) == 8 and len(lim._windows[0][2]) == 400


def test_api_slot_allows_one_holder_at_a_time():
    slot = ApiSlot()
    assert slot.acquire("sync") and slot.holder == "sync"
    assert not slot.acquire("scout") and not slot.acquire("sync")
    slot.release("scout")                                # not the holder: nothing happens
    assert slot.holder == "sync"
    slot.release("sync")
    assert slot.holder is None and slot.acquire("scout")


# --------------------------------------------------------------- new endpoints
def test_spectator_league_mastery_and_match_ids_urls():
    client, session, _ = make_client([
        FakeResponse(200, {"gameId": 1}), FakeResponse(404), FakeResponse(200, [{"tier": "GOLD"}]),
        FakeResponse(404), FakeResponse(200, {"championPoints": 5}), FakeResponse(404),
        FakeResponse(200, ["LA1_1", "LA1_2"]),
    ])
    assert client.get_active_game("P/1") == {"gameId": 1}
    assert client.get_active_game("P2") is None                                  # 404 = not in a game
    assert client.get_league_entries("P3") == [{"tier": "GOLD"}]
    assert client.get_league_entries("P4") == []                                 # unranked
    assert client.get_champion_mastery("P5", 266) == {"championPoints": 5}
    assert client.get_champion_mastery("P6", 266) is None                        # never played
    assert client.get_match_ids("P7", count=10, type="ranked") == ["LA1_1", "LA1_2"]
    urls = [c["url"] for c in session.calls]
    base = "https://americas.api.riotgames.com"
    assert urls[0] == f"{base}/lol/spectator/v5/active-games/by-summoner/P%2F1"
    assert urls[2] == f"{base}/lol/league/v4/entries/by-puuid/P3"
    assert urls[4] == f"{base}/lol/champion-mastery/v4/champion-masteries/by-puuid/P5/by-champion/266"
    assert urls[6] == f"{base}/lol/match/v5/matches/by-puuid/P7/ids"
    assert session.calls[6]["params"] == {"start": 0, "count": 10, "type": "ranked"}


def test_other_failures_are_not_swallowed():
    client, _, _ = make_client([FakeResponse(403)])
    with pytest.raises(riot.InvalidApiKeyError):
        client.get_active_game("P")


# ------------------------------------------------------------ test URL override
def test_default_hosts_and_loopback_only_override(monkeypatch):
    monkeypatch.delenv(TEST_URL_ENV, raising=False)
    assert RiotClient("K", region="la1").base_url == "https://la1.api.riotgames.com"
    assert RiotClient("K", region="americas").base_url == "https://americas.api.riotgames.com"
    monkeypatch.setenv(TEST_URL_ENV, "http://127.0.0.1:9911/")
    assert RiotClient("K", region="la1").base_url == "http://127.0.0.1:9911"
    monkeypatch.setenv(TEST_URL_ENV, "https://evil.example.com")
    assert RiotClient("K", region="la1").base_url == "https://la1.api.riotgames.com"      # never leaves the PC
    monkeypatch.setenv(TEST_URL_ENV, "ftp://127.0.0.1")
    assert RiotClient("K", region="la1").base_url == "https://la1.api.riotgames.com"
