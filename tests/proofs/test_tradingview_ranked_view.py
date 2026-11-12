"""Local ranked-movers page witnesses."""

from __future__ import annotations

import json
import subprocess
import sys
from datetime import datetime
from decimal import ROUND_HALF_EVEN, Decimal
from pathlib import Path
from typing import Any

import test_tradingview_prestage as base  # pyright: ignore[reportMissingImports]

from sextant.ranking.generation import (
    Coverage,
    CoverageState,
    GenerationId,
    GenerationStatus,
    RankingFeature,
    RankingGeneration,
    RankingReason,
    RankingRow,
)
from sextant.tradingview.prestage import run
from sextant.tradingview.ranked_view import generation_view, render_html, view_document

D = Decimal
NS = base.NS
DAY = base.DAY
MAPPED = base.SYMBOLS
ALL = (*MAPPED, "DDD")
SRC = Path(__file__).resolve().parents[2] / "src"
SESSION_MODULES = {
    "sextant.tradingview.__main__",
    "sextant.tradingview.auth",
    "sextant.tradingview.stored_login",
    "sextant.tradingview.diagnostics",
    "sextant.tradingview.sync",
    "sextant.tradingview.loop",
}


def _directory(tmp_path: Path) -> tuple[Path, Path, Path]:
    tmp_path.mkdir(parents=True, exist_ok=True)
    nasdaq = tmp_path / "nasdaqlisted.txt"
    nasdaq.write_text(
        "Symbol|Security Name|Test Issue|ETF\n"
        + "".join(f"{name}|{name} Corporation|N|N\n" for name in ALL)
        + "File Creation Time|2026-11-09 00:00:00.000\n",
        encoding="utf-8",
    )
    other = tmp_path / "otherlisted.txt"
    other.write_text("ACT Symbol|Security Name|Test Issue|ETF\n", encoding="utf-8")
    symbol_map = tmp_path / "map.json"
    symbol_map.write_text(
        json.dumps({**{name: f"NASDAQ:{name}" for name in MAPPED}, "DDD": None}),
        encoding="utf-8",
    )
    return nasdaq, other, symbol_map


def _bars(*, current: bool = True) -> tuple[Any, ...]:
    prior = tuple(
        base.bar("RTH", day, minute, name, "10.00", 1_000, 5)
        for day in base.PRIOR
        for minute in range(390)
        for name in ALL
    )
    if not current:
        return prior
    step = {"AAA": D("0.10"), "BBB": D("0.10"), "CCC": D("0.10"), "DDD": D("0.30")}
    return prior + tuple(
        base.bar(
            "RTH",
            DAY,
            minute,
            name,
            f"{D('10.10') + step[name] * minute:f}",
            5_000,
            5 if minute < 5 else 40,
        )
        for minute in range(7)
        for name in ALL
    )


def _run(
    tmp_path: Path, *, view: bool, current: bool = True, clock: tuple[int, int] = (9, 38)
) -> int:
    nasdaq, other, symbol_map = _directory(tmp_path)
    source = base.RecordedSource(_bars(current=current), dict.fromkeys(ALL, D("10.00")))
    return run(
        research_dir=tmp_path / "unit",
        nasdaq_listed=nasdaq,
        other_listed=other,
        symbol_map_path=symbol_map,
        bounds=base.bounds(),
        state_root=tmp_path / "state",
        once=True,
        clock=lambda: base.at(DAY, *clock),
        sleep=lambda seconds: None,
        source_factory=lambda lease: source,
        page=lambda line: None,
        ranked_view_dir=tmp_path / "view" if view else None,
    )


def _features(document: dict[str, Any]) -> dict[str, dict[str, str]]:
    return {
        row["symbol"]: {item["name"]: item["value"] for item in row["features"]}
        for row in document["ranking_generation"]["rows"]
    }


def test_ranked_view_page_shows_the_facts_and_an_unmapped_name(tmp_path: Path) -> None:
    assert _run(tmp_path, view=True) == 0
    view = tmp_path / "view"
    document = json.loads((view / "ranked.json").read_text(encoding="utf-8"))
    page = (view / "ranked.html").read_text(encoding="utf-8")
    generation = document["generation"]
    assert document["status"] == "fresh" and document["max_age_s"] == 600
    assert generation["segment"] == "RTH" and generation["session_date"] == "2026-11-09"
    assert generation["coverage"]["state"] == "READY"
    assert generation["ranked"] == 4 and generation["rows_total"] == 4
    assert generation["top_n"] == 100
    rows = {row["symbol"]: row for row in generation["rows"]}
    # The unit's own J5 rows carry the same Decimal strings for the mapped names.
    j5 = json.loads((tmp_path / "state" / "j5-unit.json").read_text(encoding="utf-8"))
    for name, values in _features(j5).items():
        for field in ("move", "move_1m", "move_5m", "dvol_ratio", "trades_accel"):
            assert rows[name][field] == values[field]
    first = generation["rows"][0]
    # DDD, the strongest mover, has no TradingView symbol: the unit omits it, the page not.
    assert first["rank"] == 1 and first["symbol"] == "DDD" and "<td>DDD</td>" in page
    assert "DDD" not in _features(j5) and "NASDAQ:DDD" not in json.dumps(j5)
    assert "asset_or_halt_unknown" in first["reasons"] and first["focus_eligible"] is False
    shown = D(first["move"]).scaleb(2).quantize(D("0.01"), rounding=ROUND_HALF_EVEN)
    assert f'title="{first["move"]}">{shown:+f}</td>' in page
    generated = datetime.fromtimestamp(generation["generated_at_ns"] // NS, base.NY)
    assert generated.strftime("%Y-%m-%d %H:%M:%S") in page
    assert "FRESH" in page and "STALE" not in page and "UNAVAILABLE" not in page
    assert "2026-11-09 RTH" in page and "READY, complete True" in page
    assert "4 of 4 names" in page and "asset_or_halt_unknown" in page
    assert '<meta http-equiv="refresh" content="150">' in page  # 09:38 is an onset window
    assert "<script" not in page and "://" not in page
    for name in ("ranked.json", "ranked.html"):
        assert (view / name).stat().st_mode & 0o777 == 0o600


def test_ranked_view_leaves_the_unit_files_unchanged(tmp_path: Path) -> None:
    without, with_view = tmp_path / "without", tmp_path / "with"
    assert _run(without, view=False) == 0
    assert _run(with_view, view=True) == 0
    assert not (without / "view").exists() and (with_view / "view" / "ranked.html").is_file()
    assert (without / "unit").readlink() == (with_view / "unit").readlink()
    for name in ("top100.json", "focus.json", "display-lists.json"):
        assert (without / "unit" / name).read_bytes() == (with_view / "unit" / name).read_bytes()
    assert (without / "state" / "j5-unit.json").read_bytes() == (
        with_view / "state" / "j5-unit.json"
    ).read_bytes()


def test_ranked_view_refused_tick_renders_unavailable(tmp_path: Path) -> None:
    assert _run(tmp_path, view=True, current=False) == 1
    document = json.loads((tmp_path / "view" / "ranked.json").read_text(encoding="utf-8"))
    page = (tmp_path / "view" / "ranked.html").read_text(encoding="utf-8")
    assert document["status"] == "unavailable" and document["generation"] is None
    assert document["reason"] == (
        "the producer refused the tick: no completed minute bars for this segment"
    )
    assert "UNAVAILABLE: the producer refused the tick: no completed minute bars" in page
    assert "FRESH" not in page and "<table>" not in page
    assert _run(tmp_path, view=True, clock=(2, 0)) == 0
    page = (tmp_path / "view" / "ranked.html").read_text(encoding="utf-8")
    assert "UNAVAILABLE: the producer is idle" in page


def _generation(known_at: int) -> RankingGeneration:
    def row(name: str, rank: int | None, move: str, focus: bool, *reasons: RankingReason):
        features = tuple(
            RankingFeature(key, D(value), known_at, known_at)
            for key, value in (
                ("move", move),
                ("move_1m", "0.004"),
                ("move_5m", "-0.0125"),
                ("dvol_ratio", "3.456"),
                ("trades_accel", "8"),
            )
        )
        return RankingRow(
            name,
            score=D(move) * D("3.456"),
            rank=rank,
            features=features,
            focus_eligible=focus,
            reasons=(RankingReason("SOURCE", "{}"), *reasons),
        )

    return RankingGeneration(
        generation_id=GenerationId("fixture", 0),
        session_date=DAY,
        input_cutoff=known_at,
        ts_event=known_at,
        ts_init=known_at,
        known_at=known_at,
        method_version="fixture",
        profile_version="fixture",
        ranker_version="fixture",
        coverage=Coverage(CoverageState.READY, True, 3, 3, 0, 0, 0),
        status=GenerationStatus.PROVISIONAL,
        rows=(
            row("ZED", 2, "0.031", False, RankingReason("gate_failed", "mover_min_dvol_ratio")),
            row("ABC", 1, "0.123456789", True),
            row("QQQ", None, "0.01", False, RankingReason("asset_or_halt_unknown")),
        ),
    )


def _fresh(known_at: int, reason: str | None = None, written: int = 0) -> dict[str, Any]:
    generation = generation_view(
        _generation(known_at), segment="RTH", top_n=100, universe_source="fixture directory"
    )
    return view_document(
        generation, reason=reason, max_age_s=600, refresh_s=300, written_at_ns=written or known_at
    )


def test_ranked_view_old_generation_renders_stale() -> None:
    known_at = base.at(DAY, 10, 0)
    document = _fresh(known_at)
    assert [row["symbol"] for row in document["generation"]["rows"]] == ["ABC", "ZED"]
    at_bound = render_html(document, now_ns=known_at + 600 * NS)
    assert "FRESH" in at_bound and "STALE" not in at_bound
    past = render_html(document, now_ns=known_at + 601 * NS)
    assert "STALE: generation 601 s old (bound 600 s)" in past and "FRESH" not in past
    assert "Stale after</dt><dd>2026-11-09 10:10:00 EST" in past
    carried = _fresh(known_at, "fixture refusal", known_at + 900 * NS)
    page = render_html(carried, now_ns=known_at + 900 * NS)
    assert "UNAVAILABLE: fixture refusal" in page and "STALE: generation 900 s old" in page
    assert "<td>ABC</td>" in page
    assert "http-equiv" not in render_html({**document, "refresh_s": None}, now_ns=known_at)


def test_ranked_view_renders_exact_decimals_and_gates() -> None:
    known_at = base.at(DAY, 10, 0)
    page = render_html(_fresh(known_at), now_ns=known_at)
    assert 'title="0.123456789">+12.35</td>' in page
    assert 'title="0.004">+0.40</td>' in page and 'title="-0.0125">-1.25</td>' in page
    assert 'title="3.456">3.46</td>' in page and 'title="8">8.00</td>' in page
    zed = page[page.index("<td>ZED</td>") :].split("</tr>")[0]
    assert zed.count('<td class="yes">yes</td>') == 3  # three gates, not focus
    assert "gate_failed:mover_min_dvol_ratio" in zed and '<td class="no">no</td>' in zed
    abc = page[page.index("<td>ABC</td>") :].split("</tr>")[0]
    assert abc.count('<td class="yes">yes</td>') == 5  # four gates and focus
    assert "QQQ" not in page  # an unranked row is counted, never listed
    assert "2 of 3 names" in page


def _loaded(module: str) -> set[str]:
    code = f"import sys, {module}; print(*sorted(m for m in sys.modules if m[:8] == 'sextant.'))"
    env = {"PYTHONPATH": str(SRC), "PATH": "/usr/bin:/bin"}
    out = subprocess.run([sys.executable, "-c", code], capture_output=True, text=True, env=env)
    return set(out.stdout.split())


def test_ranked_view_imports_no_tradingview_session_module() -> None:
    """The renderer's import graph holds no TradingView module; the producer's no session."""
    renderer = _loaded("sextant.tradingview.ranked_view")
    assert {name for name in renderer if name.startswith("sextant.tradingview.")} == {
        "sextant.tradingview.ranked_view"
    }
    producer = _loaded("sextant.tradingview.prestage")
    assert "sextant.tradingview.ranked_view" in producer
    assert not SESSION_MODULES.intersection(producer)
