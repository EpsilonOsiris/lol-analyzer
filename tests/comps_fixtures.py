"""A realistic, deterministic set of composition records (no network): the main plays mostly ADC,
sometimes mid and top, with two clan teammates and random players around."""
from conftest import BASE_TIME_MS, ME_PUUID, make_comp_match

from lolan import comps

HOUR = 3600_000
ALFA = ("p-alfa", "Epsilon Alfa")
BETA = ("p-beta", "Epsilon Beta")
GAMMA = ("p-gamma", "Epsilon Gamma")

# (main_role, main champion, wins, games, teammates {role: (who|None, champion)})
SCRIPT = (
    ("BOTTOM", "Jinx", 6, 9, {"UTILITY": (ALFA, "Taric"), "JUNGLE": (BETA, "Viego"), "MIDDLE": (GAMMA, "Ahri"), "TOP": (None, "Ornn")}),
    ("BOTTOM", "Vayne", 4, 5, {"UTILITY": (ALFA, "Lulu"), "JUNGLE": (BETA, "Zac"), "MIDDLE": (GAMMA, "Orianna"), "TOP": (None, "Garen")}),
    ("BOTTOM", "Kaisa", 2, 5, {"UTILITY": (ALFA, "Leona"), "JUNGLE": (BETA, "LeeSin"), "MIDDLE": (None, "Zed"), "TOP": (None, "Malphite")}),
    ("BOTTOM", "Ezreal", 3, 4, {"UTILITY": (None, "Janna"), "JUNGLE": (BETA, "Viego"), "MIDDLE": (GAMMA, "Ahri"), "TOP": (None, "Fiora")}),
    ("MIDDLE", "Zed", 5, 8, {"UTILITY": (ALFA, "Thresh"), "JUNGLE": (BETA, "LeeSin"), "BOTTOM": (GAMMA, "Jhin"), "TOP": (None, "Sett")}),
    ("MIDDLE", "Ahri", 3, 4, {"UTILITY": (ALFA, "Lulu"), "JUNGLE": (BETA, "Viego"), "BOTTOM": (GAMMA, "Jinx"), "TOP": (None, "Garen")}),
    ("MIDDLE", "Orianna", 1, 3, {"UTILITY": (None, "Leona"), "JUNGLE": (BETA, "Zac"), "BOTTOM": (GAMMA, "Kaisa"), "TOP": (None, "Ornn")}),
    ("TOP", "Garen", 2, 3, {"UTILITY": (ALFA, "Taric"), "JUNGLE": (BETA, "Zac"), "MIDDLE": (GAMMA, "Ahri"), "BOTTOM": (None, "Jinx")}),
)


def build_records():
    items = []
    for role, champion, wins, games, mates in SCRIPT:
        for i in range(games):
            n = len(items)
            blue = {r: (who[0], who[1], c) if who else (f"p-anon-{r}-{n}", f"Anon{r}{n}", c)
                    for r, (who, c) in mates.items()}
            blue[role] = (ME_PUUID, "Epsilon Me", champion)
            red = dict(zip(("TOP", "JUNGLE", "MIDDLE", "BOTTOM", "UTILITY"),
                           ("Darius", "Khazix", "Syndra", "Draven", "Nautilus")))
            match = make_comp_match(f"LA1_{n}", blue=blue, red=red, win=i < wins, main_role=role,
                                    created=BASE_TIME_MS + n * HOUR)
            items.append((f"LA1_{n}", match, None))
    recs = comps.build_records(items, ME_PUUID)
    return comps.resolve_people(recs, comps.ClanConfig.build("Epsilon", "", None))


# lineups the default (main = ADC) output is pinned against
LINEUPS = {
    "vacio": {},
    "soporte_clan": {"UTILITY": ["Epsilon Alfa", None]},
    "soporte_campeon": {"UTILITY": ["Epsilon Alfa", "Leona"]},
    "equipo_clan": {"UTILITY": ["Epsilon Alfa", None], "JUNGLE": ["Epsilon Beta", None], "MIDDLE": ["Epsilon Gamma", None]},
    "fijos": {"TOP": [None, "Malphite"], "JUNGLE": [None, "Sejuani"], "MIDDLE": [None, "Vi"], "UTILITY": [None, "Taric"]},
    "mixto": {"TOP": [None, "Ornn"], "JUNGLE": ["Epsilon Beta", None], "MIDDLE": [None, "Zed"], "UTILITY": [None, "Lulu"]},
}
ENEMY = ["Darius", "Leona", "Malphite", "Sejuani", "Nautilus"]
