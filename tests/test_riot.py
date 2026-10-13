import pytest
import requests

from lolan.riot import (InvalidApiKeyError, MissingApiKeyError, NotFoundError, RateLimiter,
                        RiotClient, RiotError)


class FakeClock:
    def __init__(self):
        self.now = 1000.0
        self.sleeps = []

    def __call__(self):
        return self.now

    def sleep(self, seconds):
        self.sleeps.append(seconds)
        self.now += seconds


class FakeResponse:
    def __init__(self, status=200, payload=None, headers=None):
        self.status_code = status
        self._payload = payload
        self.headers = headers or {}

    def json(self):
        return self._payload


class FakeSession:
    def __init__(self, responses):
        self.responses = list(responses)
        self.calls = []

    def get(self, url, params=None, headers=None, timeout=None):
        self.calls.append({"url": url, "params": params, "headers": headers})
        item = self.responses.pop(0)
        if isinstance(item, Exception):
            raise item
        return item


def make_client(responses):
    clock = FakeClock()
    session = FakeSession(responses)
    client = RiotClient("KEY", region="americas", session=session,
                        sleep=clock.sleep, clock=clock)
    return client, session, clock


# ------------------------------------------------------------ rate limiter
def test_limiter_allows_20_per_second_then_waits():
    clock = FakeClock()
    lim = RateLimiter(clock=clock, sleep=clock.sleep)
    for _ in range(20):
        lim.acquire()
    assert clock.sleeps == []
    lim.acquire()  # 21st must wait for the 1 s window
    assert len(clock.sleeps) >= 1
    assert sum(clock.sleeps) == pytest.approx(1.0)


def test_limiter_100_per_120_seconds():
    clock = FakeClock()
    lim = RateLimiter(clock=clock, sleep=clock.sleep)
    start = clock.now
    for i in range(100):
        lim.acquire()
        if i % 10 == 9:
            clock.now += 1.0  # keep under the 20/s limit
    assert clock.now - start < 120
    before = clock.now
    lim.acquire()  # 101st: must wait until the oldest request leaves the 120 s window
    assert clock.now - start >= 120
    assert clock.now > before


# --------------------------------------------------------------- HTTP logic
def test_token_header_and_url_encoding():
    client, session, _ = make_client([FakeResponse(200, {"puuid": "abc"})])
    assert client.get_puuid("José Luis", "LAN/1") == "abc"
    call = session.calls[0]
    assert call["headers"] == {"X-Riot-Token": "KEY"}
    assert call["url"] == ("https://americas.api.riotgames.com/riot/account/v1/accounts/"
                           "by-riot-id/Jos%C3%A9%20Luis/LAN%2F1")


def test_429_honors_retry_after():
    client, session, clock = make_client([
        FakeResponse(429, headers={"Retry-After": "7"}),
        FakeResponse(200, {"ok": True}),
    ])
    assert client.get_match("LA1_1") == {"ok": True}
    assert len(session.calls) == 2
    assert 7.0 in clock.sleeps


def test_5xx_retried_with_backoff_then_gives_up():
    client, session, clock = make_client([FakeResponse(503), FakeResponse(200, {"x": 1})])
    assert client.get_match("LA1_1") == {"x": 1}
    assert len(clock.sleeps) == 1

    client, session, clock = make_client([FakeResponse(500)] * 5)
    with pytest.raises(RiotError):
        client.get_match("LA1_1")
    assert len(session.calls) == 5


def test_connection_error_retried():
    client, session, _ = make_client([requests.ConnectionError("boom"), FakeResponse(200, {"x": 2})])
    assert client.get_match("LA1_1") == {"x": 2}


@pytest.mark.parametrize("status", [401, 403])
def test_invalid_key_message(status):
    client, _, _ = make_client([FakeResponse(status)])
    with pytest.raises(InvalidApiKeyError) as exc:
        client.get_match("LA1_1")
    assert "inválida o expiró" in str(exc.value) and "24 h" in str(exc.value)
    assert "KEY" not in str(exc.value)


def test_404_raises_not_found():
    client, _, _ = make_client([FakeResponse(404)])
    with pytest.raises(NotFoundError):
        client.get_timeline("LA1_1")


def test_missing_key():
    with pytest.raises(MissingApiKeyError):
        RiotClient("")


def test_match_id_pagination_stops_on_empty_page_and_passes_filters():
    page1 = [f"LA1_{i}" for i in range(100)]
    page2 = ["LA1_100", "LA1_101"]
    client, session, _ = make_client([
        FakeResponse(200, page1), FakeResponse(200, page2), FakeResponse(200, []),
    ])
    ids = client.list_match_ids("PUUID", start_time=1700000000, queue=420)
    assert len(ids) == 102
    assert [c["params"]["start"] for c in session.calls] == [0, 100, 102]
    assert session.calls[0]["params"]["startTime"] == 1700000000
    assert session.calls[0]["params"]["queue"] == 420 and session.calls[0]["params"]["count"] == 100
