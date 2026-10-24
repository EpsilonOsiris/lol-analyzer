"""A laner main sees exactly what the pages and commands showed before the per-line metrics existed."""
import json
import os
import re
from pathlib import Path

import pytest
from linea_golden import ME_RIOT_ID, capture_all, drop_linea

GOLDEN = Path(__file__).parent / "fixtures" / "linea_golden.json"


def setup_env(root, monkeypatch):
    monkeypatch.setenv("LOLAN_HOME", str(root))
    monkeypatch.setenv("LOLAN_OPGG", "off")
    for var in ("RIOT_API_KEY", "RIOT_ID", "PLATFORM", "REGION", "CLAN_PREFIX", "CLAN_ALIASES"):
        monkeypatch.delenv(var, raising=False)
    (root / ".env").write_text(f"RIOT_API_KEY=RGAPI-test\nRIOT_ID={ME_RIOT_ID}\n", encoding="utf-8")


@pytest.fixture
def root(tmp_path, monkeypatch):
    setup_env(tmp_path, monkeypatch)
    return tmp_path


def golden():
    return json.loads(GOLDEN.read_text(encoding="utf-8"))


@pytest.mark.skipif(not os.environ.get("LOLAN_REGEN_GOLDEN"), reason="only to (re)capture the golden")
def test_regenerate_golden(root, capsys):
    GOLDEN.write_text(json.dumps(capture_all(root, capsys), ensure_ascii=False, indent=1), encoding="utf-8")


def squash(pages):
    """Pages without insignificant whitespace (templates may be laid out differently)."""
    return {k: re.sub(r">\s+<", "><", re.sub(r"\s+", " ", v)).strip() for k, v in pages.items()}


def restrict_csv(got, want):
    """New metrics may add CSV columns; the ones from the golden must keep their values."""
    for name, entry in want["comandos"].items():
        if "csv" in entry:
            cols = list(entry["csv"][0])
            got["comandos"][name]["csv"] = [{c: row[c] for c in cols} for row in got["comandos"][name]["csv"]]
    return got


@pytest.mark.parametrize("how", ["auto", "explicit"])
def test_laner_main_gets_the_same_pages_and_reports(root, capsys, how):
    suffix, extra = ("", ()) if how == "auto" else ("linea=BOTTOM", ("--linea", "adc"))
    want = golden()
    got = capture_all(root, capsys, suffix, extra)
    if how == "explicit":
        got["paginas"] = {k: drop_linea(v) for k, v in got["paginas"].items()}
        want["paginas"] = {k: v for k, v in want["paginas"].items() if k in got["paginas"]}
    got = restrict_csv(got, want)
    assert squash(got["paginas"]) == squash(want["paginas"])
    assert got["comandos"] == want["comandos"]
