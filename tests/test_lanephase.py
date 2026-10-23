import pytest
from lane_fixtures import (BLUE_MID, MIN, RED_MID, bought, level_up, make_lane_match,
                           make_lane_timeline, mid_edge, undo)

from lolan import lanephase as lp


def mid_pair(match, timeline, legendary=None):
    pairs = lp.lane_pairs(match, timeline, legendary)
    return next(p for p in pairs if p["role"] == "MIDDLE")


def test_diff14_and_both_perspectives_mirror():
    gold, xp = mid_edge(extra_gold=100, extra_xp=30)
    pair = mid_pair(make_lane_match(), make_lane_timeline(gold=gold, xp=xp))
    blue, red = pair["sides"]
    assert (blue["gold14"], blue["xp14"], blue["diff14"]) == (1400, 420, 1820)
    assert blue["result"] == "win"
    assert (red["gold14"], red["xp14"], red["diff14"], red["result"]) == (-1400, -420, -1820, "loss")
    assert (blue["champ"], blue["opp"]) == (103, 108) and (red["champ"], red["opp"]) == (108, 103)
    assert (blue["side"], red["side"]) == (100, 200)
    assert pair["patch"] == "16.21" and pair["queue"] == 420 and pair["role"] == "MIDDLE"
    # the minute-by-minute view mirrors too
    for b, r in zip(blue["series"], red["series"]):
        assert b[3] == -r[3] and b[1] == r[2] and b[2] == r[1]


@pytest.mark.parametrize("extra,expected", [(300, "even"), (301, "win"), (-300, "even"),
                                            (-301, "loss"), (0, "even")])
def test_result_thresholds(extra, expected):
    # 14 minutes of gold: the lead at minute 14 is 14 * edge; use an exact total through the frame
    gold = {BLUE_MID: lambda m: 500 + 300 * m + (extra if m >= 14 else 0)}
    pair = mid_pair(make_lane_match(), make_lane_timeline(gold=gold))
    assert pair["sides"][0]["diff14"] == extra
    assert pair["sides"][0]["result"] == expected


def test_brackets_follow_each_sides_own_level():
    levels = [1, 1, 2, 3, 3, 4, 5, 6, 6, 7, 8, 9, 9, 10, 10]
    pair = mid_pair(make_lane_match(), make_lane_timeline(
        gold={BLUE_MID: lambda m: 500 + 400 * m}, xp={BLUE_MID: lambda m: 250 * m},
        level={BLUE_MID: lambda m: levels[m] if m < len(levels) else 10, RED_MID: lambda m: 1}))
    blue, red = pair["sides"]
    # minute m is filed under the level at the start of the minute (frame m - 1)
    assert {k: v["minutes"] for k, v in blue["brackets"].items()} == {"1-2": 3, "3-5": 4, "6-8": 4, "9+": 3}
    assert all(v["gain"] == v["minutes"] * 100 for v in blue["brackets"].values())
    # the opponent stays level 1 the whole lane: every minute in "1-2", losing 100 a minute
    assert red["brackets"] == {"1-2": {"gain": -1400, "minutes": 14}}
    assert sum(v["gain"] for v in blue["brackets"].values()) == blue["diff14"]


def test_first_to_levels_2_and_6():
    events = [level_up(BLUE_MID, 2, 90_000), level_up(RED_MID, 2, 100_000),
              level_up(BLUE_MID, 6, 400_000), level_up(RED_MID, 6, 400_000)]
    blue, red = mid_pair(make_lane_match(), make_lane_timeline(events=events))["sides"]
    assert (blue["first2"], red["first2"]) == (1, 0)
    assert (blue["first6"], red["first6"]) == (None, None)            # tied
    events = [level_up(BLUE_MID, 6, 500_000)]                         # the rival never got there
    blue, red = mid_pair(make_lane_match(), make_lane_timeline(events=events))["sides"]
    assert (blue["first6"], red["first6"]) == (1, 0)
    assert (blue["first2"], red["first2"]) == (None, None)            # unknown for both


def test_series_runs_to_minute_25_at_most():
    short = mid_pair(make_lane_match(duration=1200), make_lane_timeline(minutes=20))["sides"][0]
    assert [e[0] for e in short["series"]] == list(range(21))
    gold, _ = mid_edge(100)
    long = mid_pair(make_lane_match(duration=2400), make_lane_timeline(minutes=40, gold=gold))["sides"][0]
    assert [e[0] for e in long["series"]] == list(range(26))
    assert long["series"][10] == [10, 6, 6, 1000]                      # [minute, my lvl, opp lvl, diff]
    assert short["minutes"] == 20.0 and short["win"] == 1


def test_win_follows_the_side():
    blue, red = mid_pair(make_lane_match(blue_win=False), make_lane_timeline())["sides"]
    assert (blue["win"], red["win"]) == (0, 1)


@pytest.mark.parametrize("kwargs,timeline_kwargs,reason", [
    (dict(duration=800), dict(), "corta"),
    (dict(remake=True), dict(), "remake"),
    (dict(duration=1000), dict(minutes=13), "sin_frame_14"),
    (dict(queue=1700), dict(), "cola"),
    (dict(drop=range(1, 11)), dict(), "sin_lineas"),
])
def test_match_skips(kwargs, timeline_kwargs, reason):
    pairs, why = lp.analyze_match(make_lane_match(**kwargs), make_lane_timeline(**timeline_kwargs))
    assert pairs == [] and why == reason


def test_no_timeline_is_a_skip():
    assert lp.analyze_match(make_lane_match(), None) == ([], "sin_timeline")


def test_duplicate_or_missing_positions_skip_only_that_role():
    roles = lambda m: {p["role"] for p in lp.lane_pairs(m, make_lane_timeline())}  # noqa: E731
    assert roles(make_lane_match(duplicate=[2])) == {"MIDDLE", "BOTTOM", "UTILITY"}   # two blue TOPs
    assert roles(make_lane_match(drop=[9])) == {"TOP", "JUNGLE", "MIDDLE", "UTILITY"}  # red ADC without role


def test_legendary_items_rule():
    data = {"data": {
        "3031": {"gold": {"purchasable": True, "total": 3500}, "tags": ["Damage"]},          # legendary
        "3158": {"gold": {"purchasable": True, "total": 900}, "tags": ["Boots"], "into": ["3171"]},
        "3171": {"gold": {"purchasable": True, "total": 2000}, "tags": ["Boots"]},           # boots
        "2003": {"gold": {"purchasable": True, "total": 2500}, "tags": ["Consumable"]},
        "3340": {"gold": {"purchasable": True, "total": 2500}, "tags": ["Trinket"]},
        "1038": {"gold": {"purchasable": True, "total": 1300}, "into": ["3031"]},            # component
        "3003": {"gold": {"purchasable": True, "total": 2900}, "into": ["3040"]},            # still builds up
        "3042": {"gold": {"purchasable": False, "total": 2900}},                             # transformation
        "3865": {"gold": {"purchasable": True, "total": 400}, "tags": ["Lane"]},             # support quest
        "9999": {"gold": {"purchasable": True, "total": 2600}, "maps": {"11": False}},       # not on the Rift
        "3115": {"gold": {"purchasable": True, "total": 2999}, "maps": {"11": True}},
        "x": {"gold": {"purchasable": True, "total": 3000}},                                 # not an id
    }}
    assert lp.legendary_items(data) == {3031, 3115}
    assert lp.legendary_items(data["data"]) == {3031, 3115}      # the bare dict works too
    assert lp.legendary_items(None) == set()


def test_items_completion_minutes_with_undo():
    events = [
        bought(BLUE_MID, 3158, 120_000),                 # boots: not legendary
        bought(BLUE_MID, 1038, 300_000),                 # component
        bought(BLUE_MID, 3031, 600_000),                 # 1st legendary at 10.0
        bought(BLUE_MID, 3003, 800_000),                 # bought ...
        undo(BLUE_MID, 3003, 801_000),                   # ... and taken back
        bought(BLUE_MID, 3003, 930_000),                 # 2nd legendary at 15.5
        bought(RED_MID, 3115, 1_000_000),
        bought(0, 3031, 5_000),                          # participant 0 is the shop, ignored
    ]
    timeline = make_lane_timeline(events=events)
    blue, red = mid_pair(make_lane_match(), timeline, legendary={3031, 3003, 3115})["sides"]
    assert blue["items"] == [10.0, 15.5, None]
    assert red["items"] == [16.7, None, None]


def test_items_none_without_item_data():
    blue, _ = mid_pair(make_lane_match(), make_lane_timeline(events=[bought(BLUE_MID, 3031, 600_000)]))["sides"]
    assert blue["items"] is None


def test_sold_item_still_counts_and_repeat_is_once():
    events = [bought(BLUE_MID, 3031, 600_000), {"type": "ITEM_SOLD", "participantId": BLUE_MID,
                                                "itemId": 3031, "timestamp": 700_000},
              bought(BLUE_MID, 3031, 800_000)]
    blue, _ = mid_pair(make_lane_match(), make_lane_timeline(events=events), legendary={3031})["sides"]
    assert blue["items"] == [10.0, None, None]


def test_patch_and_bracket_helpers():
    assert lp.patch_of("16.21.824.8524") == "16.21" and lp.patch_of("") == "" and lp.patch_of("x.y") == ""
    assert [lp.bracket_of(n) for n in (1, 2, 3, 5, 6, 8, 9, 18)] == ["1-2", "1-2", "3-5", "3-5", "6-8", "6-8", "9+", "9+"]
    assert MIN == 60_000
