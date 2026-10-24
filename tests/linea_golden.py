"""Pages and command output for a laner main, to pin them across the per-line metric work.

tests/fixtures/linea_golden.json was captured with the code before the line-aware analysis
existed (every game was judged like a laner's). `capture_all` must keep returning the same text
for a main whose line resolves to BOTTOM, automatically or chosen. The CSV only has to keep the
columns it had then (new metrics may add columns).
"""
import csv
import io
import re

from conftest import BASE_TIME_MS, ME_PUUID, ev_kill, make_match, make_timeline

from lolan import web
from lolan.__main__ import main
from lolan.store import Store

HOUR = 3600_000
ME_RIOT_ID = "Yo#LAN"
FRIEND = "puuid-3"          # blue teammate in every game (mid)
CHAMPIONS = ("Jinx", "Kaisa", "Ashe")
PAGES = {
    "resumen": "/",
    "resumen-todas-colas": "/?cola=todas",
    "resumen-antes14": "/?fase=antes14&res=derrotas",
    "partidas": "/partidas",
    "partidas-flex": "/partidas?cola=flex",
    "campeon-jinx": "/campeon/Jinx",
    "campeon-kaisa": "/campeon/kaisa?cola=todas",
    "partida-timeline": "/partida/LA1_2",
    "partida-sin-timeline": "/partida/LA1_3",
    "partida-flex": "/partida/LA1_FLEX",
    "clan": "/clan",
    "clan-todas": "/clan?cola=todas",
}
# Pages whose content depends on one player's line: an explicit ?linea=BOTTOM only applies to them.
NOT_EXPLICIT = ("clan", "clan-todas")


def _events(i):
    """Deaths of participant 4 (the main), kills and an objective; varies with the game number."""
    ev = [ev_kill(150_000 + i * 20_000, 9, 4, [10] if i % 2 else [], x=9000 + i * 100, y=7000),
          ev_kill(400_000, 4, 8, [3], x=4000, y=4000),
          ev_kill(700_000 + i * 10_000, 10, 4, [], x=3000 + i * 50, y=3500),
          {"type": "ELITE_MONSTER_KILL", "timestamp": 600_000, "killerId": 2, "killerTeamId": 100,
           "monsterType": "DRAGON"},
          {"type": "WARD_PLACED", "timestamp": 90_000, "creatorId": 4}]
    if i % 3 == 0:
        ev.append(ev_kill(500_000, 9, 4, [10, 8], x=11000, y=9000))
    return ev


def add_lane_games(root):
    """Yo#LAN (ADC, main) and a mid friend in 12 solo games and 1 flex game, some without timeline."""
    store = Store(root / "data" / "lol.db")
    store.add_player(ME_PUUID, ME_RIOT_ID, is_main=True)
    store.add_player(FRIEND, "Amigo Fiel#LAN")
    for i in range(12):
        mid = f"LA1_{i}"
        m = make_match(mid, my_pid=4, queue=420, champion=CHAMPIONS[i % 3], opp_champion="Ezreal",
                       win=bool(i % 3), kills=2 + i % 5, deaths=1 + (i * 7) % 6, assists=3 + i % 4,
                       duration=1500 + 90 * i, created=BASE_TIME_MS + i * 3 * HOUR)
        me = m["info"]["participants"][3]
        me.update({"totalMinionsKilled": 150 + 9 * i, "goldEarned": 9000 + 250 * i,
                   "visionScore": 15 + i, "totalDamageDealtToChampions": 14000 + 900 * i})
        tl = None
        if i % 4 != 3:
            def tweak(pfs, minute, i=i):
                pfs["4"]["totalGold"] += (i - 5) * 40 * minute
                pfs["4"]["minionsKilled"] += (i % 4) * minute
            tl = make_timeline(minutes=14 + i % 4, events=_events(i), my_pid=4, opp_pid=9, tweak=tweak)
        store.save_match(mid, m, tl)
    flex = make_match("LA1_FLEX", my_pid=4, queue=440, champion="Jinx", created=BASE_TIME_MS + 60 * HOUR)
    store.save_match("LA1_FLEX", flex, make_timeline(my_pid=4, opp_pid=9, events=_events(1)))
    store.close()


def page_body(html: str) -> str:
    """The <main> block: the header changes with the selector, the page itself must not."""
    return html[html.index("<main"):html.index("</main>")]


def drop_linea(text: str, value="BOTTOM") -> str:
    """Remove the line choice from links/forms so a chosen line can be compared with the automatic one."""
    text = re.sub(rf'[?&;]amp;linea={value}|[?&]linea={value}', "", text)
    return re.sub(rf'\s*<input type="hidden" name="linea" value="{value}">', "", text)


def capture_pages(client, suffix=""):
    out = {}
    for name, url in PAGES.items():
        if suffix and name in NOT_EXPLICIT:
            continue
        sep = "&" if "?" in url else "?"
        resp = client.get(url + (sep + suffix.lstrip("&?") if suffix else ""))
        assert resp.status_code == 200, (name, url, resp.status_code)
        out[name] = page_body(resp.get_data(as_text=True))
    return out


def _reports(root, sub):
    base = root / "reports" / sub
    files = [base / "resumen.md", *sorted((base / "campeones").glob("*.md")),
             *sorted((base / "partidas").glob("*.md"))]
    return {p.relative_to(base).as_posix(): p.read_text(encoding="utf-8") for p in files}


def _csv_rows(root, sub):
    path = root / "data" / (f"games-{sub}.csv" if sub else "games.csv")
    return list(csv.DictReader(io.StringIO(path.read_text(encoding="utf-8"))))


def capture_commands(root, capsys, extra=()):
    out = {}
    runs = {"analyze": (["analyze"], ""), "analyze-solo": (["analyze", "--cola", "solo"], "solo"),
            "game-ultima": (["game", "ultima"], None), "game-id": (["game", "LA1_2"], None),
            "campeon": (["campeon", "Jinx"], None)}
    for name, (args, sub) in runs.items():
        extra_args = list(extra) if args[0] in ("analyze", "campeon") else []
        assert main([*args, *extra_args]) == 0
        printed = capsys.readouterr().out
        entry = {"stdout": printed.replace(str(root), "<raiz>")}
        if sub is not None:
            entry["informes"] = _reports(root, sub)
            entry["csv"] = _csv_rows(root, sub)
        out[name] = entry
    return out


def capture_all(root, capsys, suffix="", extra=()):
    add_lane_games(root)
    client = web.create_app(root).test_client()
    pages = capture_pages(client, suffix)
    return {"paginas": pages, "comandos": capture_commands(root, capsys, extra)}
