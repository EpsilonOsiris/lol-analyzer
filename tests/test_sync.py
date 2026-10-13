from datetime import date

import pytest
from conftest import ME_PUUID, make_match, make_timeline

from lolan.riot import InvalidApiKeyError, NotFoundError
from lolan.store import Store
from lolan.sync import parse_riot_id, since_to_epoch, sync


class FakeClient:
    def __init__(self, ids, fail_key_at=None, no_timeline=()):
        self.ids = ids
        self.fail_key_at = fail_key_at
        self.no_timeline = set(no_timeline)
        self.matches_fetched = []
        self.timelines_fetched = []
        self.list_kwargs = None

    def get_puuid(self, name, tag):
        self.riot_id = (name, tag)
        return ME_PUUID

    def list_match_ids(self, puuid, start_time=None, queue=None):
        self.list_kwargs = {"start_time": start_time, "queue": queue}
        return list(self.ids)

    def get_match(self, match_id):
        if self.fail_key_at is not None and len(self.matches_fetched) >= self.fail_key_at:
            raise InvalidApiKeyError("expired")
        self.matches_fetched.append(match_id)
        return make_match(match_id=match_id, created=1_700_000_000_000 + len(self.matches_fetched))

    def get_timeline(self, match_id):
        if match_id in self.no_timeline:
            raise NotFoundError("no timeline")
        self.timelines_fetched.append(match_id)
        return make_timeline()


@pytest.fixture
def store(tmp_path):
    s = Store(tmp_path / "data" / "lol.db")
    yield s
    s.close()


def run(client, store, **kw):
    lines = []
    result = sync(client, store, "José Luis#LAN", out=lines.append, **kw)
    return result, lines


def test_parse_riot_id():
    assert parse_riot_id("José Luis#LAN") == ("José Luis", "LAN")
    with pytest.raises(ValueError):
        parse_riot_id("sin_tag")
    with pytest.raises(ValueError):
        parse_riot_id("Nombre#TAG")


def test_since_to_epoch():
    assert since_to_epoch(None) is None
    assert isinstance(since_to_epoch(date(2024, 1, 31)), int)


def test_downloads_new_matches_and_stores_timelines(store):
    client = FakeClient(["LA1_1", "LA1_2", "LA1_3"])
    result, lines = run(client, store)
    assert result.downloaded == 3
    assert store.existing_match_ids() == {"LA1_1", "LA1_2", "LA1_3"}
    assert client.riot_id == ("José Luis", "LAN")
    assert store.get_puuid() == ME_PUUID
    assert any("faltan" in line for line in lines)  # progress with ETA
    assert all(t is not None for _, _, t in store.iter_matches())


def test_skips_already_stored_matches(store):
    store.save_match("LA1_1", make_match(match_id="LA1_1"), None)
    client = FakeClient(["LA1_1", "LA1_2"])
    result, _ = run(client, store)
    assert client.matches_fetched == ["LA1_2"]
    assert result.already_stored == 1 and result.downloaded == 1
    # Second run: nothing to do.
    client2 = FakeClient(["LA1_1", "LA1_2"])
    result2, _ = run(client2, store)
    assert client2.matches_fetched == [] and result2.downloaded == 0


def test_max_and_no_timeline_and_since(store):
    client = FakeClient(["LA1_1", "LA1_2", "LA1_3"])
    result = sync(client, store, "A#B", max_matches=2, with_timeline=False,
                     since=date(2024, 1, 1), out=lambda *_: None)
    assert result.downloaded == 2
    assert client.timelines_fetched == []
    assert client.list_kwargs["start_time"] == since_to_epoch(date(2024, 1, 1))
    assert store.count_matches() == 2


def test_timeline_404_keeps_the_match(store):
    client = FakeClient(["LA1_1", "LA1_2"], no_timeline=["LA1_1"])
    result, _ = run(client, store)
    assert result.downloaded == 2 and result.missing_timelines == 1
    stored = {mid: tl for mid, _, tl in store.iter_matches()}
    assert stored["LA1_1"] is None and stored["LA1_2"] is not None


def test_resumable_after_key_expires(store):
    client = FakeClient(["LA1_1", "LA1_2", "LA1_3", "LA1_4"], fail_key_at=2)
    with pytest.raises(InvalidApiKeyError):
        run(client, store)
    assert store.count_matches() == 2  # committed one by one

    client2 = FakeClient(["LA1_1", "LA1_2", "LA1_3", "LA1_4"])
    result, _ = run(client2, store)
    assert client2.matches_fetched == ["LA1_3", "LA1_4"]
    assert store.count_matches() == 4 and result.downloaded == 2
