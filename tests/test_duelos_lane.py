"""Champion-select side of the duelos: enemy roles from the revealed champions, OP.GG answered from the cache or in the background."""

import requests
from opgg_fixtures import NAMES, ROLE_RATES, FakeMcp

from lolan import duelos as dl
from lolan.opgg import McpClient
from lolan.store import Store

NOW = 1_800_000_000


def rates_of(cid):
    return dict(ROLE_RATES.get(NAMES.get(cid), {})) or None


# ------------------------------------------------------------- enemy roles
def test_a_lone_revealed_champion_is_placed_by_its_own_role_rates():
    out = dl.infer_roles([64, 0, 0, 0, 0], lambda cid: {"jungle": 0.98} if cid == 64 else None)
    assert out[0]["role"] == "JUNGLE" and out[0]["conf"] > 0.9 and not out[0]["low"]
    assert out[1:] == [None] * 4


def test_smite_is_not_required_to_be_a_jungler():
    # champion select does not show the enemy's spells: without the Smite factor a jungler stays a jungler
    rates = {"jungle": 0.9, "top": 0.05}
    blind = dl.role_scores({}, rates, None, smite_known=False)
    assert max(blind, key=blind.get) == "JUNGLE"
    scout_rule = dl.role_scores({}, rates, None)                          # no Smite means not a jungler there
    assert max(scout_rule, key=scout_rule.get) != "JUNGLE"


def test_a_flex_champion_is_low_confidence_until_the_others_settle_the_lane():
    kaisa_alone = dl.infer_roles([145, 0, 0, 0, 0], rates_of)[0]
    assert kaisa_alone["role"] == "BOTTOM" and kaisa_alone["low"]              # 70% adc / 30% mid
    both = dl.infer_roles([0, 0, 238, 145, 0], rates_of)                       # Zed takes mid, so Kai'Sa is the ADC
    assert [both[2]["role"], both[3]["role"]] == ["MIDDLE", "BOTTOM"]
    assert both[3]["conf"] > kaisa_alone["conf"]
    swapped = dl.infer_roles([0, 0, 145, 238, 0], rates_of)                    # the order of the slots does not matter
    assert [swapped[2]["role"], swapped[3]["role"]] == ["BOTTOM", "MIDDLE"]


def test_unknown_champion_is_a_neutral_player_and_extra_slots_are_ignored():
    out = dl.infer_roles([999, 86, 0], rates_of)
    assert out[1]["role"] == "TOP" and out[0]["low"] and out[2] is None
    assert len(dl.infer_roles([86] * 7, rates_of)) == 5


# --------------------------------------------------------------- LaneOpgg
class Rig:
    def __init__(self, tmp_path, fake=None, enabled=True):
        tmp_path.mkdir(parents=True, exist_ok=True)
        self.db = tmp_path / "lol.db"
        Store(self.db).close()
        self.fake = fake or FakeMcp()
        self.woken = []
        svc = dl.DuelosService(lambda: Store(self.db), tmp_path / "wp.json", client=McpClient(session=self.fake),
                               patch_func=lambda: "16.21", clock=lambda: NOW)
        self.opgg = dl.LaneOpgg(lambda: svc, notify=lambda: self.woken.append(1))
        self.opgg.enabled = enabled
        self.svc = svc

    def settle(self):
        for t in list(self.opgg.threads):
            t.join(5)


def test_disabled_or_without_service_nothing_is_called(tmp_path):
    rig = Rig(tmp_path, enabled=False)
    assert rig.opgg.lookup("MIDDLE", 103, 238, True) == {"status": "off"}
    assert rig.opgg.enemy_roles([238]) is None and not rig.opgg.roles_pending()
    rig.opgg.enabled = True
    rig.opgg._service = lambda: None                       # LOLAN_OPGG=off
    assert rig.opgg.lookup("MIDDLE", 103, 238, True) == {"status": "off"} and rig.fake.tool_calls == []


def test_a_pair_is_fetched_in_the_background_then_answered_from_the_cache(tmp_path):
    rig = Rig(tmp_path)
    assert rig.opgg.lookup("MIDDLE", 103, 238, fetch=False) == {"status": "idle"}      # hovers do not call
    assert rig.fake.tool_calls == []
    assert rig.opgg.lookup("MIDDLE", 103, 238, fetch=True) == {"status": "pending"}
    rig.settle()
    assert rig.woken                                                                    # the page was woken up
    got = rig.opgg.lookup("MIDDLE", 103, 238, fetch=True)
    assert got["status"] == "ok" and got["games"] == 3000 and got["lane_winner"] == "theirs"
    calls = len(rig.fake.tool_calls)
    assert rig.opgg.lookup("MIDDLE", 103, 238, fetch=False)["status"] == "ok" and len(rig.fake.tool_calls) == calls


def test_pairs_cached_by_an_earlier_scout_are_answered_with_no_call(tmp_path):
    rig = Rig(tmp_path)
    rig.svc._data().matchup("mid", 103, 238)                  # the loading screen / a previous game stored it
    calls = len(rig.fake.tool_calls)
    assert rig.opgg.lookup("MIDDLE", 103, 238, fetch=False)["status"] == "ok"
    assert len(rig.fake.tool_calls) == calls and not rig.opgg.threads


def test_the_same_pair_is_not_requested_twice_while_in_flight(tmp_path):
    rig = Rig(tmp_path)
    for _ in range(3):
        rig.opgg.lookup("TOP", 86, 58, fetch=True)
    rig.settle()
    assert rig.fake.count("lol_get_lane_matchup_guide") == 1


def test_a_failing_opgg_is_soft_and_retried_after_a_while(tmp_path):
    clock = [100.0]
    rig = Rig(tmp_path)
    rig.opgg._clock = lambda: clock[0]
    rig.fake.raise_on_post = requests.ConnectionError("down")
    assert rig.opgg.lookup("TOP", 86, 58, fetch=True)["status"] == "pending"
    rig.settle()
    assert rig.opgg.lookup("TOP", 86, 58, fetch=True) == {"status": "error"}
    assert rig.woken                                           # the "pending" badge goes away too
    rig.fake.raise_on_post = None
    rig.svc.client._down_until = 0.0
    clock[0] += dl.LaneOpgg.RETRY_S + 1
    assert rig.opgg.lookup("TOP", 86, 58, fetch=True)["status"] == "pending"
    rig.settle()
    assert rig.opgg.lookup("TOP", 86, 58, fetch=True)["status"] == "ok"


def test_unknown_champion_ids_are_an_error_not_an_endless_retry_loop(tmp_path):
    rig = Rig(tmp_path)
    rig.opgg.lookup("TOP", 86, 9999, fetch=True)
    rig.settle()
    assert rig.opgg.lookup("TOP", 86, 9999, fetch=True) == {"status": "error"}


def test_enemy_roles_wait_for_the_tables_then_answer_at_once(tmp_path):
    rig = Rig(tmp_path)
    assert rig.opgg.enemy_roles([238, 86]) is None
    rig.settle()
    roles = rig.opgg.enemy_roles([238, 86, 0, 0, 0])
    assert [r and r["role"] for r in roles] == ["MIDDLE", "TOP", None, None, None]
    assert rig.fake.count("lol_list_champions") == 1 and rig.fake.count("lol_list_lane_meta_champions") == 1
    rig.opgg.enemy_roles([238])
    assert rig.fake.count("lol_list_lane_meta_champions") == 1
