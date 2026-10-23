from datetime import date

import pytest
from conftest import ME_PUUID, make_match, make_timeline

from lolan.riot import InvalidApiKeyError, NotFoundError
from lolan.store import Store
from lolan import analyze as an
from lolan.sync import parse_riot_id, since_to_epoch, summary_lines, sync, sync_players


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

    def get_riot_id(self, puuid):
        return "José Luis#LAN"

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
    assert store.get_player(riot_id="josé luis#lan").puuid == ME_PUUID
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


def test_queue_filter_is_passed_to_match_list(store):
    client = FakeClient(["LA1_1"])
    run(client, store, queue=420)
    assert client.list_kwargs["queue"] == 420


# ------------------------------------------------------------------- clan
class ClanClient:
    """Fake API for several players. Matches come from make_match, so they always contain the
    default player (puuid-me) and teammate puuid-4: a game listed for both is a shared game."""

    def __init__(self, accounts, ids, not_found=(), bad_key_for=None):
        self.accounts = accounts            # "Nombre#TAG" -> puuid
        self.ids = ids                      # puuid -> match ids
        self.not_found = set(not_found)
        self.bad_key_for = bad_key_for
        self.matches_fetched = []
        self.resolved = []

    def get_puuid(self, name, tag):
        key = f"{name}#{tag}"
        self.resolved.append(key)
        if key == self.bad_key_for:
            raise InvalidApiKeyError("expired")
        if key in self.not_found or key not in self.accounts:
            raise NotFoundError("404")
        return self.accounts[key]

    def get_riot_id(self, puuid):
        key = next((k for k, v in self.accounts.items() if v == puuid), None)
        self.resolved.append(key)
        if key is not None and key == self.bad_key_for:
            raise InvalidApiKeyError("expired")
        if key is None or key in self.not_found:
            raise NotFoundError("404")
        return key

    def list_match_ids(self, puuid, start_time=None, queue=None):
        return list(self.ids[puuid])

    def get_match(self, match_id):
        self.matches_fetched.append(match_id)
        return make_match(match_id=match_id, created=1_700_000_000_000 + int(match_id.split("_")[1]))

    def get_timeline(self, match_id):
        return make_timeline()


ACCOUNTS = {"Yo#LAN": ME_PUUID, "Amigo#LAN": "puuid-4", "Otro#LAN": "puuid-5"}


def clan_run(client, store, riot_ids, **kw):
    lines = []
    outcomes = sync_players(client, store, riot_ids, main_riot_id="Yo#LAN", out=lines.append, **kw)
    return outcomes, lines


def test_shared_match_is_downloaded_once_and_counts_for_both(store):
    client = ClanClient(ACCOUNTS, {ME_PUUID: ["LA1_1", "LA1_2", "LA1_3"],
                                   "puuid-4": ["LA1_2", "LA1_3", "LA1_4"]})
    outcomes, lines = clan_run(client, store, ["Yo#LAN", "Amigo#LAN"])
    assert client.matches_fetched == ["LA1_1", "LA1_2", "LA1_3", "LA1_4"]       # 4, not 6
    assert [o.result.downloaded for o in outcomes] == [3, 1]
    assert [o.result.already_stored for o in outcomes] == [0, 2]
    assert store.count_matches() == 4
    assert store.count_matches(ME_PUUID) == 4 and store.count_matches("puuid-4") == 4  # links cover all participants
    mine = an.load_games(store, ME_PUUID)
    theirs = an.load_games(store, "puuid-4")
    assert {g["match_id"] for g in theirs} == {"LA1_1", "LA1_2", "LA1_3", "LA1_4"}
    assert {g["champion"] for g in mine} == {"Ahri"} and {g["champion"] for g in theirs} == {"Champ4"}
    # Headers per player and a final summary.
    assert "=== Yo#LAN (1/2) ===" in lines and "=== Amigo#LAN (2/2) ===" in lines
    assert lines[-3] == "Resumen por jugador:"
    assert "Amigo#LAN: 1 descargadas, 2 ya guardadas" in lines[-1]
    # Both are registered; only "Yo" is main.
    assert {p.riot_id: p.is_main for p in store.list_players()} == {"Yo#LAN": True, "Amigo#LAN": False}


def test_second_run_downloads_nothing(store):
    client = ClanClient(ACCOUNTS, {ME_PUUID: ["LA1_1"], "puuid-4": ["LA1_1"]})
    clan_run(client, store, ["Yo#LAN", "Amigo#LAN"])
    client2 = ClanClient(ACCOUNTS, {ME_PUUID: ["LA1_1"], "puuid-4": ["LA1_1"]})
    outcomes, _ = clan_run(client2, store, ["Yo#LAN", "Amigo#LAN"])
    assert client2.matches_fetched == [] and all(o.result.downloaded == 0 for o in outcomes)


def test_clan_continues_after_one_player_is_not_found(store):
    client = ClanClient(ACCOUNTS, {ME_PUUID: ["LA1_1"], "puuid-5": ["LA1_9"]},
                        not_found=["Amigo#LAN"])
    outcomes, lines = clan_run(client, store, ["Yo#LAN", "Amigo#LAN", "Otro#LAN"])
    assert [o.riot_id for o in outcomes] == ["Yo#LAN", "Amigo#LAN", "Otro#LAN"]
    assert outcomes[0].result.downloaded == 1 and outcomes[2].result.downloaded == 1
    assert outcomes[1].result is None and "Amigo#LAN" in outcomes[1].error
    assert any(line.startswith("No se pudo sincronizar a Amigo#LAN") for line in lines)
    assert any("NO SINCRONIZADO" in line and "Amigo#LAN" in line for line in lines)
    assert {p.riot_id for p in store.list_players()} == {"Yo#LAN", "Otro#LAN"}


def test_clan_stops_on_invalid_key_but_keeps_what_was_saved(store):
    client = ClanClient(ACCOUNTS, {ME_PUUID: ["LA1_1"], "puuid-4": ["LA1_2"], "puuid-5": ["LA1_3"]},
                        bad_key_for="Amigo#LAN")
    lines = []
    with pytest.raises(InvalidApiKeyError):
        sync_players(client, store, ["Yo#LAN", "Amigo#LAN", "Otro#LAN"], main_riot_id="Yo#LAN",
                     out=lines.append)
    assert "Otro#LAN" not in client.resolved                  # nobody after the failure is tried
    assert store.existing_match_ids() == {"LA1_1"}            # the first player's work is saved
    assert any("Yo#LAN: 1 descargadas" in line for line in lines)   # partial summary was printed


def test_renamed_player_updates_riot_id_and_options_apply_to_each(store):
    store.add_player("puuid-4", "Viejo#LAN")
    client = ClanClient({"Amigo#LAN": "puuid-4"}, {"puuid-4": ["LA1_1", "LA1_2", "LA1_3"]})
    outcomes, _ = clan_run(client, store, ["Amigo#LAN"], max_matches=2, with_timeline=False)
    assert outcomes[0].result.downloaded == 2
    assert [p.riot_id for p in store.list_players()] == ["Amigo#LAN"]      # same puuid, new name


def test_summary_lines_shape():
    class R:
        downloaded, already_stored, missing_timelines, failed = 2, 1, 1, 0
    from lolan.sync import PlayerOutcome
    lines = summary_lines([PlayerOutcome("A#B", R()), PlayerOutcome("C#D", error="no existe")])
    assert lines[0] == "Resumen por jugador:"
    assert "A#B: 2 descargadas, 1 ya guardadas, 1 sin timeline" in lines[1]
    assert "C#D: NO SINCRONIZADO (no existe)" in lines[2]


def test_single_sync_reports_unknown_account_in_spanish(store):
    client = ClanClient({}, {})
    with pytest.raises(NotFoundError, match="No se encontró la cuenta Nadie#LAN"):
        sync(client, store, "Nadie#LAN", out=lambda *_: None)


def test_registered_player_is_followed_by_puuid_after_a_rename(store):
    store.add_player("puuid-4", "Amigo#LAN")
    # Riot now only knows the account under its new name.
    client = ClanClient({"Amigo Nuevo#LAN": "puuid-4"}, {"puuid-4": ["LA1_1"]})
    lines = []
    sync(client, store, "Amigo#LAN", out=lines.append)
    assert any("ahora se llama Amigo Nuevo#LAN" in line for line in lines)
    assert store.get_player(puuid="puuid-4").riot_id == "Amigo Nuevo#LAN"
    assert client.matches_fetched == ["LA1_1"]


# ------------------------------------------------------- predictions closed by sync
class NamedClient(ClanClient):
    """Like ClanClient, but the default player carries a Riot ID, as in real matches."""

    def get_match(self, match_id):
        m = super().get_match(match_id)
        me = next(p for p in m["info"]["participants"] if p["puuid"] == ME_PUUID)
        me["riotIdGameName"], me["riotIdTagline"] = "Yo", "LAN"
        return m


def put_prediction(store, game_id):
    from lolan import winprob
    winprob.record_prediction(store, "LA1", game_id, "Yo#LAN",
                              {"lane": 0.4, "syn": 0.0, "rank": 0.0, "lanes_used": 5}, 0.6, "v1-por-defecto")


def test_sync_players_closes_the_predictions_of_the_games_it_saved(store):
    put_prediction(store, 1)
    put_prediction(store, 2)
    put_prediction(store, 9)                                  # a game that is not synced
    client = NamedClient(ACCOUNTS, {ME_PUUID: ["LA1_1", "LA1_2"]})
    _, lines = clan_run(client, store, ["Yo#LAN"])
    assert {r["match_id"] for r in store.resolved_predictions()} == {"LA1_1", "LA1_2"}
    assert {r["match_id"] for r in store.pending_predictions()} == {"LA1_9"}
    assert lines[-1] == "Predicciones cerradas: 2."


def test_predictions_are_closed_even_when_the_run_aborts(store):
    put_prediction(store, 1)
    client = NamedClient(ACCOUNTS, {ME_PUUID: ["LA1_1"], "puuid-4": ["LA1_2"]}, bad_key_for="Amigo#LAN")
    lines = []
    with pytest.raises(InvalidApiKeyError):
        sync_players(client, store, ["Yo#LAN", "Amigo#LAN"], main_riot_id="Yo#LAN", out=lines.append)
    assert [r["match_id"] for r in store.resolved_predictions()] == ["LA1_1"]
    assert lines[-1] == "Predicciones cerradas: 1."


def test_a_broken_prediction_history_does_not_break_sync(store, monkeypatch):
    from lolan import winprob

    def boom(*_a, **_k):
        raise RuntimeError("tabla rota")
    monkeypatch.setattr(winprob, "resolve_outcomes", boom)
    client = NamedClient(ACCOUNTS, {ME_PUUID: ["LA1_1"]})
    outcomes, lines = clan_run(client, store, ["Yo#LAN"])
    assert outcomes[0].result.downloaded == 1 and store.count_matches() == 1
    assert not any("Predicciones" in line for line in lines)
