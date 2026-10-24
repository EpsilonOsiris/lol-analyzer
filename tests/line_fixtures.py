"""Synthetic support and jungle games (participant fields and challenges the per-line metrics read)."""
from conftest import BASE_TIME_MS, ME_PUUID, ROLES, ev_kill, make_match, make_timeline

HOUR = 3600_000

SUPPORT_CHALLENGES = {
    "wardTakedowns": 8, "controlWardsPlaced": 5, "effectiveHealAndShielding": 6000.0,
    "enemyChampionImmobilizations": 40, "saveAllyFromDeath": 2, "completeSupportQuestInTime": 1,
}
JUNGLE_CHALLENGES = {
    "jungleCsBefore10Minutes": 60, "enemyJungleMonsterKills": 14, "scuttleCrabKills": 3,
    "dragonTakedowns": 2, "riftHeraldTakedowns": 1, "baronTakedowns": 1, "epicMonsterSteals": 1,
    "voidMonsterKill": 3, "takedownsFirstXMinutes": 6,
}


def role_pid(role):
    return ROLES.index(role) + 1


def make_role_game(role, match_id="LA1_1", challenges=None, fields=None, opp_challenges=None,
                   opp_fields=None, **kw):
    """A game where ME_PUUID plays `role` (blue side) against the enemy `role` (red side).

    `challenges` / `fields` update the player's `challenges` dict / participant fields, the `opp_`
    arguments the lane opponent's. `challenges=None` leaves the player without `challenges`.
    """
    pid = role_pid(role)
    match = make_match(match_id, my_pid=pid, **kw)
    parts = match["info"]["participants"]
    me, opp = parts[pid - 1], parts[pid + 4]
    me.pop("challenges", None)
    if challenges is not None:
        me["challenges"] = dict(challenges)
    me.update(fields or {})
    if opp_challenges is not None:
        opp["challenges"] = dict(opp_challenges)
    opp.update(opp_fields or {})
    opp["championName"] = f"Rival{role.title()}"
    return match


def role_timeline(role, events=None, **kw):
    pid = role_pid(role)
    return make_timeline(my_pid=pid, opp_pid=pid + 5, events=events if events is not None else [], **kw)


def add_role_games(store, role, count, prefix, champion="Soraka", puuid=ME_PUUID, start=0, timeline=True,
                   queue=420, challenges=None, **overrides):
    """Save `count` games of `puuid` in `role`; `overrides(i)` may return per-game kwargs for make_role_game."""
    for i in range(count):
        mid = f"{prefix}_{i}"
        kw = {"champion": champion, "win": bool((i + start) % 2), "queue": queue,
              "created": BASE_TIME_MS + (start + i) * 3 * HOUR}
        kw.update(overrides.get("kw", lambda i: {})(i) if callable(overrides.get("kw")) else {})
        match = make_role_game(role, mid, challenges=challenges, **kw)
        if puuid != ME_PUUID:
            for p in match["info"]["participants"]:
                if p["puuid"] == ME_PUUID:
                    p["puuid"] = puuid
            match["metadata"]["participants"] = [p["puuid"] for p in match["info"]["participants"]]
        store.save_match(mid, match, role_timeline(role, [ev_kill(200_000, role_pid(role) + 5, role_pid(role))])
                         if timeline else None)
