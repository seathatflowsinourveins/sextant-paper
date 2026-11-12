"""A local, script-free page of the newest ranked movers; display only.

Renders one `RankingGeneration` (or a refusal reason) into `ranked.json` and
`ranked.html`, atomic and mode 600, from values the rows already carry: rank, the
five features, `gate_failed` reasons against `engine.GATES`, `focus_eligible` and
reason codes. Numbers are the rows' Decimals quantized, exact string in `title`.
No float, script or external reference; `<meta http-equiv=refresh>` at the cadence
floor (https://html.spec.whatwg.org/multipage/semantics.html#attr-meta-http-equiv-refresh,
read 2026-11-11). STALE past the display bound `max_age_s`. Movers are ranked rows
cut to `top_n` as in `bridge.py`. Imports no TradingView module.
"""

from __future__ import annotations

import html
import os
import tempfile
from collections.abc import Mapping
from datetime import datetime
from decimal import ROUND_HALF_EVEN, Decimal
from pathlib import Path
from typing import Any
from zoneinfo import ZoneInfo

from sextant.execution.state import write_atomic
from sextant.ranking.engine import GATES
from sextant.ranking.generation import RankingGeneration

SCHEMA = "sextant.tv-ranked-view/1"
NS = 1_000_000_000
ET = ZoneInfo("America/New_York")
FEATURES = ("move", "move_1m", "move_5m", "dvol_ratio", "trades_accel")
METADATA = frozenset({"SOURCE", "ASSET", "REFERENCE", "LABELS", "CLOCKS"})
GATE_LABELS = dict(zip(GATES, ("move", "1m up", "accel", "dvol"), strict=True))
CENT = Decimal("0.01")


def _text(value: Decimal | None) -> str | None:
    return None if value is None else format(value, "f")


def view_rows(generation: RankingGeneration) -> list[dict[str, Any]]:
    """Every ranked row in rank order, with its plain ticker and recorded fields."""
    rows = []
    for row in sorted(
        (item for item in generation.rows if item.rank is not None),
        key=lambda item: item.rank,  # type: ignore[arg-type,return-value]
    ):
        values = {feature.name: feature.value for feature in row.features}
        failed = {reason.detail for reason in row.reasons if reason.code == "gate_failed"}
        rows.append(
            {
                "symbol": row.symbol,
                "rank": row.rank,
                "score": _text(row.score),
                **{name: _text(values.get(name)) for name in FEATURES},
                "gates_passed": [gate for gate in GATES if gate not in failed],
                "focus_eligible": row.focus_eligible is True,
                "reasons": [
                    reason.code if reason.detail is None else f"{reason.code}:{reason.detail}"
                    for reason in row.reasons
                    if reason.code not in METADATA
                ],
            }
        )
    return rows


def generation_view(
    generation: RankingGeneration, *, segment: str, top_n: int, universe_source: str
) -> dict[str, Any]:
    coverage = generation.coverage
    rows = view_rows(generation)
    return {
        "generated_at_ns": generation.known_at,
        "session_date": generation.session_date.isoformat(),
        "segment": segment,
        "coverage": {
            "state": coverage.state.value,
            "complete": coverage.complete,
            "expected_symbols": coverage.expected_symbols,
            "observed_symbols": coverage.observed_symbols,
        },
        "rows_total": len(generation.rows),
        "ranked": len(rows),
        "top_n": top_n,
        "universe_source": universe_source,
        "rows": rows,
    }


def view_document(
    generation: Mapping[str, Any] | None,
    *,
    reason: str | None,
    max_age_s: int,
    refresh_s: int | None,
    written_at_ns: int,
) -> dict[str, Any]:
    """Fresh, or (with a reason) unavailable carrying the last generation, never as new."""
    return {
        "schema": SCHEMA,
        "display_only": True,
        "status": "fresh" if reason is None else "unavailable",
        "reason": reason,
        "written_at_ns": written_at_ns,
        "max_age_s": max_age_s,
        "refresh_s": refresh_s,
        "generation": None if generation is None else dict(generation),
    }


def _clock(ns: int) -> str:
    return datetime.fromtimestamp(ns // NS, ET).strftime("%Y-%m-%d %H:%M:%S %Z")


def _number(text: str | None, *, percent: bool) -> str:
    if text is None:
        return '<td class="n">n/a</td>'
    exact = Decimal(text)
    shown = (exact.scaleb(2) if percent else exact).quantize(CENT, rounding=ROUND_HALF_EVEN)
    label = f"{shown:+f}" if percent else f"{shown:f}"
    return f'<td class="n" title="{html.escape(text)}">{label}</td>'


STYLE = """
body{margin:0;padding:16px;background:#111;color:#e8e8e6;font:14px/1.4 system-ui,sans-serif}
h1{font-size:18px}.state{font-size:48px;font-weight:800}
.fresh{color:#5fd38d}.stale{color:#ffb000}.unavailable{color:#ff5c5c}
dl{display:grid;grid-template-columns:max-content 1fr;gap:2px 12px;margin:8px 0 16px}
dt{color:#9aa0a6}dd{margin:0}.wrap{overflow-x:auto}
table{border-collapse:collapse}th,td{padding:3px 8px;border-bottom:1px solid #333}
th{text-align:left;color:#9aa0a6;position:sticky;top:0;background:#111}
td.n{text-align:right;font-variant-numeric:tabular-nums}.yes{color:#5fd38d}.no{color:#777}
"""


def render_html(document: Mapping[str, Any], *, now_ns: int) -> str:
    """One self-contained page; inline CSS, no script, no external reference."""
    generation = document.get("generation")
    max_age_s = int(document["max_age_s"])
    states: list[tuple[str, str]] = []
    if document["status"] == "unavailable":
        states.append(("unavailable", f"UNAVAILABLE: {document['reason']}"))
    facts: list[tuple[str, str]] = [("Page written", _clock(int(document["written_at_ns"])))]
    body = ""
    if generation is not None:
        generated = int(generation["generated_at_ns"])
        age_s = max(0, now_ns - generated) // NS
        if age_s > max_age_s:
            states.append(("stale", f"STALE: generation {age_s} s old (bound {max_age_s} s)"))
        elif document["status"] == "fresh":
            states.append(("fresh", "FRESH"))
        coverage = generation["coverage"]
        facts += [
            ("Generated", f"{_clock(generated)} (age {age_s} s at render)"),
            ("Stale after", _clock(generated + max_age_s * NS)),
            ("Session segment", f"{generation['session_date']} {generation['segment']}"),
            (
                "Coverage",
                f"{coverage['state']}, complete {coverage['complete']}, "
                f"{coverage['observed_symbols']} of {coverage['expected_symbols']} observed",
            ),
            (
                "Ranked",
                f"{generation['ranked']} of {generation['rows_total']} names "
                f"(table shows the top {generation['top_n']})",
            ),
            ("Universe", generation["universe_source"]),
        ]
        head = (
            "<tr><th>Rank</th><th>Ticker</th><th>Move %</th><th>1m %</th><th>5m %</th>"
            "<th>$Vol ratio</th><th>Trades accel</th>"
            + "".join(f"<th>{html.escape(label)}</th>" for label in GATE_LABELS.values())
            + "<th>Focus</th><th>Reasons</th></tr>"
        )
        cells = []
        for row in generation["rows"][: int(generation["top_n"])]:
            passed = set(row["gates_passed"])
            gates = "".join(
                '<td class="yes">yes</td>' if gate in passed else '<td class="no">no</td>'
                for gate in GATE_LABELS
            )
            focus = "yes" if row["focus_eligible"] else "no"
            reasons = "" if row["focus_eligible"] else ", ".join(row["reasons"])
            cells.append(
                f'<tr><td class="n">{row["rank"]}</td><td>{html.escape(row["symbol"])}</td>'
                + _number(row["move"], percent=True)
                + _number(row["move_1m"], percent=True)
                + _number(row["move_5m"], percent=True)
                + _number(row["dvol_ratio"], percent=False)
                + _number(row["trades_accel"], percent=False)
                + gates
                + f'<td class="{focus}">{focus}</td><td>{html.escape(reasons)}</td></tr>'
            )
        body = f'<div class="wrap"><table>{head}{"".join(cells)}</table></div>'
    refresh_s = document.get("refresh_s")
    if type(refresh_s) is int and refresh_s > 0:
        refresh = f'<meta http-equiv="refresh" content="{refresh_s}">'
        facts.append(("Reloads every", f"{refresh_s} s (J5 cadence floor)"))
    else:
        refresh = ""
        facts.append(("Reloads", "never: outside the display window"))
    facts.append(("Note", "this page cannot age itself; compare Stale after with your clock"))
    banner = "".join(
        f'<div class="state {kind}">{html.escape(text)}</div>' for kind, text in states
    )
    listing = "".join(
        f"<dt>{html.escape(name)}</dt><dd>{html.escape(value)}</dd>" for name, value in facts
    )
    return (
        '<!doctype html><html lang="en"><head><meta charset="utf-8">'
        f"{refresh}<title>Sextant ranked movers</title><style>{STYLE}</style></head>"
        f"<body><h1>Sextant ranked movers (display only)</h1>{banner}<dl>{listing}</dl>"
        f"{body}</body></html>\n"
    )


def _write_text(path: Path, text: str) -> None:
    """`write_atomic` (execution/state.py) for text."""
    path.parent.mkdir(parents=True, exist_ok=True)
    fd, temporary = tempfile.mkstemp(dir=path.parent, prefix=f".{path.name}.")
    try:
        with os.fdopen(fd, "w", encoding="utf-8") as stream:
            stream.write(text)
            stream.flush()
            os.fsync(stream.fileno())
        os.replace(temporary, path)
        directory = os.open(path.parent, os.O_RDONLY)
        try:
            os.fsync(directory)
        finally:
            os.close(directory)
    except BaseException:
        Path(temporary).unlink(missing_ok=True)
        raise


def write_view(directory: Path, document: Mapping[str, Any], *, now_ns: int) -> Path:
    """Write `ranked.json` then `ranked.html`; return the page's path."""
    directory = Path(directory)
    write_atomic(directory / "ranked.json", dict(document))
    page = directory / "ranked.html"
    _write_text(page, render_html(document, now_ns=now_ns))
    return page
