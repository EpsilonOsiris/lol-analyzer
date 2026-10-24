"""Renders the Composiciones page and command for a main who plays ADC, to pin their output.

tests/fixtures/composiciones_golden.json was captured with the code before the line selector
existed (the page was always seen from the ADC's seat). `capture_all` must keep returning the
same text for a main whose line resolves to BOTTOM, automatically or chosen.
"""
import re

from comps_fixtures import SCRIPT, script_matches
from test_web_comps import add_comp_games

from lolan import web
from lolan.__main__ import main
from lolan.store import Store

ME_RIOT_ID = "Epsilon Me#LAN"
PAGES = {
    "base": "/composiciones?cola=flex",
    "todas": "/composiciones?cola=todas",
    "armar-defecto": "/composiciones?cola=flex&armar=1",
    "armar-clan": "/composiciones?cola=flex&armar=1&p_UTILITY=Epsilon+Alfa&p_JUNGLE=Epsilon+Beta&c_TOP=ornn",
    "armar-completo": ("/composiciones?cola=flex&armar=1&p_UTILITY=Epsilon+Alfa&c_UTILITY=taric"
                       "&c_JUNGLE=Sejuani&enemigos=Zed,+Leona&todos=1"),
}
COMMANDS = {"cli": [], "cli-flex": ["--cola", "flex"], "cli-todos": ["--todos-los-adc"]}


def add_script_games(root, script=SCRIPT):
    """Store the games of a comps_fixtures script (flex queue) with Epsilon Me as the main player."""
    store = Store(root / "data" / "lol.db")
    store.add_player("puuid-me", ME_RIOT_ID, is_main=True)
    for match_id, match, timeline in script_matches(script):
        store.save_match(match_id, match, timeline)
    store.close()


FIXTURES = {"solo-adc": add_comp_games, "mezcla": add_script_games}


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
        resp = client.get(url + suffix)
        assert resp.status_code == 200, (name, resp.status_code)
        out[name] = page_body(resp.get_data(as_text=True))
    return out


def capture_commands(root, capsys, extra=()):
    out = {}
    for name, args in COMMANDS.items():
        report = root / "reports" / ("flex" if "--cola" in args else "") / "composiciones.md"
        assert main(["composiciones", *args, *extra]) == 0
        printed = capsys.readouterr().out.replace(str(report), "<informe>")
        out[name] = {"stdout": printed, "informe": report.read_text(encoding="utf-8")}
    return out


def capture_all(root, capsys, fixture, suffix="", extra=()):
    FIXTURES[fixture](root)
    client = web.create_app(root).test_client()
    return {"paginas": capture_pages(client, suffix), "comandos": capture_commands(root, capsys, extra)}
