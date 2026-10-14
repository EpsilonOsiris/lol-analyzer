import pytest

from lolan.players import load_roster, normalize_riot_id, slugify
from lolan.store import Store


@pytest.fixture
def store(tmp_path):
    s = Store(tmp_path / "lol.db")
    yield s
    s.close()


def test_normalize_riot_id():
    assert normalize_riot_id("  José Luis#LAN ") == ("José Luis", "LAN")
    assert normalize_riot_id("a#b#c") == ("a#b", "c")
    for bad in ("", None, "sin_tag", "#TAG", "Nombre#", " # "):
        with pytest.raises(ValueError, match="Nombre#TAG"):
            normalize_riot_id(bad)


def test_slugify_is_lowercase_ascii_and_filesystem_safe():
    assert slugify("José Luis") == "jose-luis"
    assert slugify("  Ñandú / Pro?  ") == "nandu-pro"
    assert slugify("日本語") == ""


def test_slugs_are_unique_and_safe(store):
    store.add_player("p1", "Jugador Uno#LAN", is_main=True)
    store.add_player("p2", "Pepe#LAN")
    store.add_player("p3", "Pepe#LA2")          # same game name, different tag
    store.add_player("p4", "Solo#LAN")           # would clash with the solo-queue CSV/folder
    store.add_player("p5", "日本語#JP")           # nothing ASCII left
    store.add_player("p6", "Nul#LAN")
    r = load_roster(store, "Jugador Uno#LAN")
    slugs = {p.riot_id: r.slug(p) for p in r.players}
    assert slugs["Jugador Uno#LAN"] == "jugador-uno"
    assert slugs["Pepe#LAN"] == "pepe-lan" and slugs["Pepe#LA2"] == "pepe-la2"
    assert slugs["Solo#LAN"] == "j-solo"
    assert slugs["日本語#JP"] == "jugador"
    assert slugs["Nul#LAN"] == "j-nul"
    assert len(set(slugs.values())) == len(slugs)


def test_roster_main_resolution(store):
    store.add_player("p1", "Yo#LAN")
    store.add_player("p2", "Amigo#LAN")
    # No flag, no match with .env: nobody is "you".
    r = load_roster(store, "")
    assert r.main is None and not any(p.is_main for p in r.players)
    # RIOT_ID in .env wins (case-insensitive) and is first in the list.
    r = load_roster(store, "amigo#lan")
    assert r.main.puuid == "p2" and r.players[0].puuid == "p2" and r.players[0].is_main
    assert r.main_slug == "amigo"
    # An unregistered .env value falls back to the player flagged as main in the database.
    store.set_main("p1")
    r = load_roster(store, "Renombrado#LAN")
    assert r.main.puuid == "p1"
    # ... but a registered .env value still wins over the flag.
    assert load_roster(store, "Amigo#LAN").main.puuid == "p2"


def test_roster_lookups(store):
    store.add_player("p1", "Yo#LAN", is_main=True)
    store.add_player("p2", "Amigo Fiel#LAN")
    r = load_roster(store, "Yo#LAN")
    assert r.by_ref("amigo fiel#lan").puuid == "p2"
    assert r.by_ref("amigo-fiel").puuid == "p2"          # slug works as a reference too
    assert r.by_slug("AMIGO-FIEL").puuid == "p2" and r.by_slug("zzz") is None
    assert r.by_ref("") is None and r.by_ref("nadie#x") is None
    assert r.by_puuid("p1").riot_id == "Yo#LAN" and len(r) == 2
