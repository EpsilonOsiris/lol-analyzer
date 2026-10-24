"""A main who plays ADC sees exactly what Composiciones showed before the line selector existed."""
import json
import os
from pathlib import Path

import pytest
from composiciones_golden import FIXTURES, ME_RIOT_ID, capture_all, drop_linea

GOLDEN = Path(__file__).parent / "fixtures" / "composiciones_golden.json"


@pytest.fixture
def root(tmp_path, monkeypatch):
    monkeypatch.setenv("LOLAN_HOME", str(tmp_path))
    monkeypatch.setenv("LOLAN_OPGG", "off")
    for var in ("RIOT_API_KEY", "RIOT_ID", "PLATFORM", "REGION", "CLAN_PREFIX", "CLAN_ALIASES"):
        monkeypatch.delenv(var, raising=False)
    (tmp_path / ".env").write_text(f"RIOT_API_KEY=RGAPI-test\nRIOT_ID={ME_RIOT_ID}\n", encoding="utf-8")
    return tmp_path


def golden():
    return json.loads(GOLDEN.read_text(encoding="utf-8"))


@pytest.mark.skipif(not os.environ.get("LOLAN_REGEN_GOLDEN"), reason="only to (re)capture the golden")
def test_regenerate_golden(tmp_path, monkeypatch, capsys):
    out = {}
    for fixture in FIXTURES:
        root = tmp_path / fixture
        (root / "data").mkdir(parents=True)
        monkeypatch.setenv("LOLAN_HOME", str(root))
        monkeypatch.setenv("LOLAN_OPGG", "off")
        for var in ("RIOT_API_KEY", "RIOT_ID", "PLATFORM", "REGION", "CLAN_PREFIX", "CLAN_ALIASES"):
            monkeypatch.delenv(var, raising=False)
        (root / ".env").write_text(f"RIOT_API_KEY=RGAPI-test\nRIOT_ID={ME_RIOT_ID}\n", encoding="utf-8")
        out[fixture] = capture_all(root, capsys, fixture)
    GOLDEN.write_text(json.dumps(out, ensure_ascii=False, indent=1), encoding="utf-8")


@pytest.mark.parametrize("fixture", sorted(FIXTURES))
@pytest.mark.parametrize("how", ["auto"])
def test_adc_main_gets_the_same_page_and_command_output(root, capsys, fixture, how):
    suffix, extra = ("", ()) if how == "auto" else ("&linea=BOTTOM", ("--linea", "adc"))
    got = capture_all(root, capsys, fixture, suffix, extra)
    if how == "explicit":
        got["paginas"] = {k: drop_linea(v) for k, v in got["paginas"].items()}
    assert got == golden()[fixture]
