from conftest import ME_PUUID, make_match, make_timeline

from lolan.metrics import (extract_game_metrics, is_enemy_half, phase_key)


def test_snapshots_and_diffs_at_7_and_14():
    row = extract_game_metrics(make_match(), make_timeline(), ME_PUUID)
    assert row["cs_7"] == 56 and row["cs_14"] == 112
    assert row["gold_7"] == 500 + 400 * 7
    assert row["gold_diff_7"] == 100 * 7
    assert row["gold_diff_14"] == 100 * 14
    assert row["cs_diff_14"] == 2 * 14
    assert row["xp_diff_14"] == 30 * 14
    # The game (16 min frames) never reached minute 21 / 28.
    assert row["gold_21"] is None and row["gold_diff_21"] is None and row["cs_28"] is None


def test_phase_buckets():
    row = extract_game_metrics(make_match(), make_timeline(), ME_PUUID)
    assert (row["kills_0_7"], row["deaths_0_7"], row["assists_0_7"]) == (1, 1, 0)
    assert (row["kills_7_14"], row["deaths_7_14"], row["assists_7_14"]) == (0, 1, 1)
    assert row["deaths_14_21"] == 0 and row["deaths_28p"] == 0
    assert row["early_deaths"] == 2
    assert phase_key(0) == "0_7" and phase_key(7 * 60000) == "7_14" and phase_key(40 * 60000) == "28p"


def test_solo_death_and_enemy_half():
    row = extract_game_metrics(make_match(), make_timeline(), ME_PUUID)
    assert row["solo_deaths"] == 1  # the 9:00 death had no assists; the 2:00 one had
    assert row["deaths_enemy_half"] == 1  # the 2:00 death at (10000, 8000) for blue


def test_enemy_half_is_reversed_for_red():
    assert is_enemy_half(100, 10000, 8000) is True
    assert is_enemy_half(100, 3000, 4000) is False
    assert is_enemy_half(200, 3000, 4000) is True
    assert is_enemy_half(200, 10000, 8000) is False
    assert is_enemy_half(100, None, 5) is False


def test_first_blood_victim_not_involvement():
    row = extract_game_metrics(make_match(), make_timeline(), ME_PUUID)
    assert row["first_blood"] == 0 and row["first_blood_victim"] == 1


def test_objectives_buildings_and_wards():
    row = extract_game_metrics(make_match(), make_timeline(), ME_PUUID)
    assert row["dragons_own"] == 1 and row["dragons_enemy"] == 0
    assert row["grubs_enemy"] == 1 and row["grubs_own"] == 0
    assert row["obj_own_7_14"] == 1 and row["obj_enemy_14_21"] == 1
    assert row["buildings_taken_7_14"] == 1  # enemy tower destroyed at 13:00
    assert row["buildings_lost_14_21"] == 1  # our tower destroyed at 15:00
    assert row["wards_0_7"] == 2 and row["wards_7_14"] == 1  # opponent's ward is ignored


def test_flat_rates():
    row = extract_game_metrics(make_match(), make_timeline(), ME_PUUID)
    assert row["champion"] == "Ahri" and row["role"] == "MIDDLE" and row["win"] == 1
    assert row["opp_champion"] == "Zed"
    assert row["patch"] == "14.5"
    dur_min = 1000 / 60
    assert abs(row["cs_per_min"] - 120 / dur_min) < 1e-9
    assert abs(row["deaths_per_10"] - 2 * 10 / dur_min) < 1e-9
    assert row["kda"] == (1 + 1) / 2
    assert row["solo_kills"] == 1 and row["cs_first10"] == 70
    assert row["cs_diff_end"] == 20 and row["gold_diff_end"] == 1000
    assert row["kp"] == 2 / (1 + 2 * 4)  # my k+a over team kills (me 1 + four teammates with 2)
    assert row["analyzable"] is True and row["remake"] is False


def test_remake_excluded_by_duration_and_flag():
    short = extract_game_metrics(make_match(duration=200), None, ME_PUUID)
    assert short["remake"] is True and short["analyzable"] is False
    m = make_match()
    m["info"]["participants"][2]["gameEndedInEarlySurrender"] = True
    assert extract_game_metrics(m, None, ME_PUUID)["remake"] is True


def test_non_summoners_rift_queue_not_analyzable():
    row = extract_game_metrics(make_match(queue=450), None, ME_PUUID)  # ARAM
    assert row["remake"] is False and row["analyzable"] is False


def test_missing_lane_opponent():
    row = extract_game_metrics(make_match(drop_opp_position=True), make_timeline(), ME_PUUID)
    assert row["opp_champion"] is None
    assert row["gold_diff_14"] is None and row["cs_diff_14"] is None and row["gold_diff_end"] is None
    assert row["gold_14"] == 500 + 400 * 14  # own snapshots still there
    assert row["deaths_0_7"] == 1


def test_missing_timeline():
    row = extract_game_metrics(make_match(), None, ME_PUUID)
    assert row["has_timeline"] == 0
    assert row["gold_14"] is None and row["deaths_0_7"] is None and row["early_deaths"] is None
    assert row["solo_deaths"] is None
    assert row["cs_per_min"] is not None and row["gold_diff_end"] == 1000


def test_missing_optional_fields_do_not_crash():
    m = make_match(challenges=False)
    for p in m["info"]["participants"]:
        p.pop("visionScore"); p.pop("wardsPlaced"); p.pop("totalDamageTaken")
    row = extract_game_metrics(m, {"info": {}}, ME_PUUID)
    assert row["solo_kills"] is None and row["skillshots_hit"] is None
    assert row["vision_score"] == 0


def test_player_not_in_match():
    assert extract_game_metrics(make_match(), None, "someone-else") is None
