"""Data Dragon additions for scouting: champions by number, summoner spells and runes (stub data)."""
import json

from scout_fixtures import PNG_BYTES, seed_ddragon

from lolan.ddragon import BASE_URL, DDragon, FetchError

VERSION = "16.20.1"


class Cdn:
    def __init__(self):
        self.calls = []

    def __call__(self, url):
        self.calls.append(url)
        if url.endswith("/api/versions.json"):
            return json.dumps([VERSION]).encode()
        if url.endswith("/champion.json"):
            return json.dumps({"data": {"Aatrox": {"id": "Aatrox", "key": "266", "name": "Aatrox"},
                                        "MonkeyKing": {"id": "MonkeyKing", "key": "62", "name": "Wukong"}}}).encode()
        if url.endswith("/summoner.json"):
            return json.dumps({"data": {"SummonerFlash": {"id": "SummonerFlash", "key": "4", "name": "Destello",
                                                          "image": {"full": "SummonerFlash.png"}}}}).encode()
        if url.endswith("/runesReforged.json"):
            return json.dumps([{"id": 8100, "key": "Domination", "name": "Dominación",
                                "icon": "perk-images/Styles/7200_Domination.png", "slots": [{"runes": [
                                    {"id": 8112, "key": "Electrocute", "name": "Electrocutar",
                                     "icon": "perk-images/Styles/Domination/Electrocute/Electrocute.png"}]}]}]).encode()
        if url.endswith(".png"):
            return PNG_BYTES
        raise FetchError("404", not_found=True)


def test_prepare_downloads_champion_spell_and_rune_icons(tmp_path):
    cdn = Cdn()
    dd = DDragon(tmp_path, fetcher=cdn)
    dd.prepare((None,), (), wait=10, minimap=False, champion_keys=[266, 62], spells=[4], perks=[8112, 8100])
    assert dd.busy is False
    assert (tmp_path / VERSION / "img" / "champion" / "Aatrox.png").is_file()
    assert (tmp_path / VERSION / "img" / "champion" / "MonkeyKing.png").is_file()
    assert (tmp_path / VERSION / "img" / "spell" / "SummonerFlash.png").is_file()
    assert (tmp_path / VERSION / "img" / "rune" / "perk-images" / "Styles" / "Domination" / "Electrocute"
            / "Electrocute.png").is_file()
    assert f"{BASE_URL}/cdn/{VERSION}/img/spell/SummonerFlash.png" in cdn.calls
    assert f"{BASE_URL}/cdn/img/perk-images/Styles/7200_Domination.png" in cdn.calls     # runes are unversioned


def test_lookups_by_number_with_stub_data(tmp_path):
    seed_ddragon(tmp_path.parent / (tmp_path.name + "_dd"), icons=False)
    base = tmp_path.parent / (tmp_path.name + "_dd") / "data" / "ddragon"
    dd = DDragon(base)
    assert dd.champion_by_key(VERSION, 64) == ("LeeSin", "Lee Sin")
    assert dd.champion_by_key(VERSION, "145") == ("Kaisa", "Kai'Sa")
    assert dd.champion_by_key(VERSION, 99999) is None and dd.champion_by_key(None, 64) is None
    assert dd.champion_by_key(VERSION, "abc") is None
    spell = dd.spell_info(VERSION, 4)
    assert spell["name"] == "Destello" and spell["file"] == "SummonerFlash.png" and spell["icon_rel"] is None
    assert dd.spell_info(VERSION, 9999) is None
    keystone = dd.perk_info(VERSION, 8008)
    assert keystone["name"] == "Paso Fugaz" and keystone["icon_rel"] is None
    tree = dd.perk_info(VERSION, 8200)
    assert tree["name"] == "Brujería" and tree["icon"].endswith("7202_Sorcery.png")
    assert dd.perk_info(VERSION, 1) is None


def test_icon_paths_appear_once_cached_and_are_served_safely(tmp_path):
    seed_ddragon(tmp_path, icons=True)
    dd = DDragon(tmp_path / "data" / "ddragon")
    assert dd.spell_info(VERSION, 4)["icon_rel"] == f"{VERSION}/img/spell/SummonerFlash.png"
    rel = dd.perk_info(VERSION, 8008)["icon_rel"]
    assert rel == f"{VERSION}/img/rune/perk-images/Styles/Precision/LethalTempo/LethalTempoTemp.png"
    assert dd.resolve_path(rel) is not None
    assert dd.resolve_path(f"{VERSION}/img/rune/../../versions.json") is None


def test_offline_lookups_degrade_to_none(tmp_path):
    dd = DDragon(tmp_path)
    assert dd.champion_by_key(VERSION, 64) is None and dd.spell_info(VERSION, 4) is None
    assert dd.perk_info(VERSION, 8008) is None
