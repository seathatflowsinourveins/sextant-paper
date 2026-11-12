"""One tracked recipe for C's four per-segment baseline files.

A single run fetches the overnight table from the boats feed and the daytime
table from sip through the reviewed fetchers in :mod:`sextant.data.baseline_bars`,
splits them into one segment per file, and verifies that every file carries the
baseline schema and all requested symbols. Only a clean set replaces the active
files, and the four files land before a ``manifest.json`` that records the
command, ``as_of``, symbol list, window, feeds and each file's sha256. The
manifest is written last: a reader treats it as the commit record and checks the
digests before using the files.

A symbol the fetchers gave no baseline for a segment is named in the manifest's
``coverage``; a segment with no symbol at all is ``empty_segment`` and refuses.

``--universe`` takes the K2 ranked universe artifact written by
``python -m sextant.paper.c_universe --out PATH``; this recipe never re-ranks.
Only :func:`main` reads the environment or builds a client.

Symbols are bare vendor tickers: a venue-qualified instrument ID such as
``AAPL.ALPACA`` is refused before any request. A segment that is zero across
its whole window, or thinner than the schema floor, refuses the whole build
before the active set is touched: the flags go to stderr, the exit is non-zero,
and the previous baseline files stay byte-identical.

Upstream: alpacahq/alpaca-py@v0.44.0 ``alpaca/data/requests.py:StockBarsRequest``
and ``alpaca/data/client.py:get_stock_bars``, called as ``baseline_bars`` does.
"""

import argparse
import hashlib
import json
import os
import sys
import time
from collections.abc import Callable, Iterable, Mapping, Sequence
from datetime import date
from pathlib import Path
from typing import Any, cast

from sextant.data.alpaca_history import environment_credentials, symbol_name
from sextant.data.baseline_bars import (
    BarsClient,
    fetch_baseline,
    fetch_overnight_baseline,
    prior_xnys_sessions,
)
from sextant.ranking.baseline import (
    BaselineSample,
    BaselineTable,
    dump_baseline,
    load_baseline,
    segment_start,
)

RECIPE_SEGMENTS = ("OVERNIGHT", "PRE", "RTH", "POST")
SEGMENT_FILES = {
    "OVERNIGHT": "overnight.json",
    "PRE": "pre.json",
    "RTH": "rth.json",
    "POST": "post.json",
}
FEEDS = {"OVERNIGHT": "boats", "PRE": "sip", "RTH": "sip", "POST": "sip"}
MANIFEST_NAME = "manifest.json"
MIN_SAMPLES_PER_SESSION = 2  # the schema's start and end sample for one session
NO_BASELINE_REASON = (
    "no vendor bars for the segment in the prior sessions of a fully paged answer; "
    "the reader treats the baseline as unknown"
)


class BaselineFlagged(ValueError):
    """A build whose outputs are not fit to replace the active baseline set."""

    def __init__(self, flags: Mapping[str, Mapping[str, Sequence[str]]]) -> None:
        self.flags = {key: dict(value) for key, value in flags.items() if value}
        super().__init__(f"baseline flags {json.dumps(self.flags, sort_keys=True)}")


def _symbols(symbols: Iterable[str]) -> tuple[str, ...]:
    names: list[str] = []
    for symbol in symbols:
        if symbol.endswith(".ALPACA"):
            raise ValueError(
                "baseline symbols are bare vendor tickers, not instrument IDs: "
                f"{symbol} (pass {symbol.removesuffix('.ALPACA')})"
            )
        names.append(symbol_name(symbol))
    if not names:
        raise ValueError("at least one symbol is required")
    return tuple(dict.fromkeys(names))


def segment_table(table: BaselineTable, name: str) -> BaselineTable:
    """One segment's samples, keeping the source, known_at and feed; may be empty."""
    samples = tuple(sample for sample in table.samples if sample.segment == name)
    return BaselineTable(samples, table.provenance, table.known_at, table.feed)


def absent(tables: Mapping[str, BaselineTable], symbols: Sequence[str]) -> dict[str, list[str]]:
    """Per segment, the requested symbols the fetchers gave no baseline, in request order."""
    return {
        name: [symbol for symbol in _symbols(symbols) if not tables[name].covers(symbol, name)]
        for name in RECIPE_SEGMENTS
    }


def coverage_line(manifest: Mapping[str, Any]) -> str:
    """One journal line: per segment, symbols with and without a baseline."""
    parts = [
        f"{name} {entry['with_baseline']} with, {len(entry['without_baseline'])} without"
        for name, entry in manifest["coverage"].items()
    ]
    return "baseline coverage: " + "; ".join(parts)


def build(
    client: BarsClient,
    symbols: Sequence[str],
    *,
    as_of: date,
    known_at: int,
    prior_sessions: int = 20,
) -> dict[str, BaselineTable]:
    """Fetch both feeds once and split the results into one table per segment."""
    names = _symbols(symbols)
    overnight = fetch_overnight_baseline(
        client, names, as_of=as_of, known_at=known_at, prior_sessions=prior_sessions
    )
    daytime = fetch_baseline(
        client, names, as_of=as_of, known_at=known_at, feed="sip", prior_sessions=prior_sessions
    )
    tables = {"OVERNIGHT": segment_table(overnight, "OVERNIGHT")}
    for name in ("PRE", "RTH", "POST"):
        tables[name] = segment_table(daytime, name)
    return tables


def verify(
    tables: Mapping[str, BaselineTable],
    symbols: Sequence[str],
    without: Mapping[str, Sequence[str]] | None = None,
) -> None:
    """Refuse a file set that is not one segment over exactly the requested symbols.

    ``without`` names, per segment, the requested symbols declared to have none.
    """
    requested = frozenset(_symbols(symbols))
    for name in RECIPE_SEGMENTS:
        table = tables.get(name)
        if table is None:
            raise ValueError(f"the {name} baseline is missing")
        segments = {sample.segment for sample in table.samples}
        if not segments <= {name}:
            raise ValueError(f"the {name} baseline must hold one segment, found {sorted(segments)}")
        expected = requested - frozenset((without or {}).get(name, ()))
        covered = {sample.symbol for sample in table.samples}
        if covered != expected:
            raise ValueError(f"the {name} baseline does not cover {sorted(expected - covered)}")
        feed = FEEDS[name]
        if table.feed != feed:
            raise ValueError(f"the {name} baseline must use the {feed} feed, found {table.feed}")


def render(tables: Mapping[str, BaselineTable]) -> dict[str, str]:
    """Each segment as baseline JSON, proven to load back to the same identity."""
    texts: dict[str, str] = {}
    for name in RECIPE_SEGMENTS:
        table = tables.get(name)
        if table is None:
            raise ValueError(f"the {name} baseline is missing")
        text = dump_baseline(table) + "\n"
        if load_baseline(text).identity != table.identity:
            raise ValueError(f"the {name} baseline does not round-trip through its schema")
        texts[SEGMENT_FILES[name]] = text
    return texts


def survey(
    tables: Mapping[str, BaselineTable],
    symbols: Sequence[str],
    sessions: Sequence[date],
) -> dict[str, dict[str, list[str]]]:
    """Name each segment file that is zero-filled or thinner than the schema floor.

    A table whose samples are all zero is indistinguishable from a missing
    baseline and must never arm a slot silently. A segment with no samples is
    ``empty_segment``: no symbol traded, which is a failed read or a wrong window.
    """
    expected = [session.isoformat() for session in sessions]
    flags: dict[str, dict[str, list[str]]] = {"zero_filled": {}, "sparse": {}, "empty_segment": {}}
    for name in RECIPE_SEGMENTS:
        table = tables[name]
        if not table.samples:
            flags["empty_segment"][name] = list(symbols)
            continue
        by_symbol: dict[str, list[BaselineSample]] = {}
        for sample in table.samples:
            by_symbol.setdefault(sample.symbol, []).append(sample)
        zero: list[str] = []
        sparse: list[str] = []
        for symbol in symbols:
            if symbol not in by_symbol:
                continue  # ``verify`` matched it to a declared absence
            samples = by_symbol[symbol]
            present = {sample.session for sample in samples}
            if len(samples) < MIN_SAMPLES_PER_SESSION * len(expected):
                sparse.append(symbol)
            elif set(expected) - present:
                sparse.append(symbol)
            if samples and all(
                sample.dollar_volume == 0 and sample.trade_count == 0 for sample in samples
            ):
                zero.append(symbol)
        if zero:
            flags["zero_filled"][name] = zero
        if sparse:
            flags["sparse"][name] = sparse
    return flags


def publish(
    out: Path,
    tables: Mapping[str, BaselineTable],
    *,
    command: Sequence[str],
    as_of: date,
    symbols: Sequence[str],
    sessions: Sequence[date],
    known_at: int,
    prior_sessions: int,
    source: str,
    universe: Mapping[str, str] | None,
    without: Mapping[str, Sequence[str]] | None = None,
) -> dict[str, Any]:
    """Publish the four files and the manifest only when every check passes.

    A flagged or failed build writes nothing: it raises before ``out`` is
    created or changed, so the active set stays byte-identical. A clean build
    stages the files as a fresh time-suffixed sibling of ``out``, then writes
    each file under a temporary name inside ``out`` and renames it into place,
    with the manifest last. A crash between those renames leaves a mix that the
    old manifest does not cover, so a reader that checks the manifest refuses
    the set instead of using a partial one.
    """
    names = _symbols(symbols)
    if not sessions:
        raise ValueError("at least one prior session is required")
    verify(tables, names, without)
    texts = render(tables)
    flags = survey(tables, names, sessions)
    declared = {name: list((without or {}).get(name, ())) for name in RECIPE_SEGMENTS}
    manifest: dict[str, Any] = {
        "command": list(command),
        "as_of": as_of.isoformat(),
        "known_at": known_at,
        "symbols": list(names),
        "window": {
            "prior_sessions": prior_sessions,
            "first_session": sessions[0].isoformat(),
            "last_session": sessions[-1].isoformat(),
        },
        "source": source,
        "universe": dict(universe) if universe is not None else None,
        "zero_filled": flags["zero_filled"],
        "sparse": flags["sparse"],
        "empty_segment": flags["empty_segment"],
        "coverage": {
            name: {
                "with_baseline": len(names) - len(declared[name]),
                "without_baseline": declared[name],
                "reason": NO_BASELINE_REASON,
            }
            for name in RECIPE_SEGMENTS
        },
        "files": {},
    }
    if any(flags.values()):
        raise BaselineFlagged(flags)
    out.parent.mkdir(parents=True, exist_ok=True)
    token = str(time.time_ns())
    staging = out.parent / f".{out.name}.staging-{token}"
    staging.mkdir()
    target_tmps: list[Path] = []
    try:
        for name in RECIPE_SEGMENTS:
            filename = SEGMENT_FILES[name]
            staged = staging / filename
            staged.write_text(texts[filename], encoding="utf-8")
            manifest["files"][filename] = {
                "segment": name,
                "feed": tables[name].feed,
                "sha256": hashlib.sha256(staged.read_bytes()).hexdigest(),
                "samples": len(tables[name].samples),
                "symbols": sorted({sample.symbol for sample in tables[name].samples}),
            }
        manifest_text = json.dumps(manifest, indent=2, sort_keys=True) + "\n"
        out.mkdir(parents=True, exist_ok=True)
        for name in RECIPE_SEGMENTS:
            filename = SEGMENT_FILES[name]
            temporary = out / f".{filename}.{token}.tmp"
            target_tmps.append(temporary)
            temporary.write_bytes((staging / filename).read_bytes())
            os.replace(temporary, out / filename)
        temporary = out / f".{MANIFEST_NAME}.{token}.tmp"
        target_tmps.append(temporary)
        temporary.write_text(manifest_text, encoding="utf-8")
        os.replace(temporary, out / MANIFEST_NAME)
    finally:
        for temporary in target_tmps:
            if temporary.is_file():
                temporary.unlink()
        _discard(staging)
    return manifest


def _discard(staging: Path) -> None:
    """Remove only the files this run wrote into its own staging directory."""
    for filename in (*SEGMENT_FILES.values(), MANIFEST_NAME):
        path = staging / filename
        if path.parent == staging and path.is_file():
            path.unlink()
    if staging.is_dir():
        staging.rmdir()


def run(
    client: BarsClient,
    symbols: Sequence[str],
    out: Path,
    *,
    command: Sequence[str],
    as_of: date,
    known_at: int,
    prior_sessions: int = 20,
    source: str = "symbols",
    universe: Mapping[str, str] | None = None,
) -> dict[str, Any]:
    """Build and publish one complete baseline set; any refusal leaves no output."""
    names = _symbols(symbols)
    sessions = prior_xnys_sessions(as_of, prior_sessions)
    tables = build(client, names, as_of=as_of, known_at=known_at, prior_sessions=prior_sessions)
    without = absent(tables, names)
    return publish(
        out,
        tables,
        command=command,
        as_of=as_of,
        symbols=names,
        sessions=sessions,
        known_at=known_at,
        prior_sessions=prior_sessions,
        source=source,
        universe=universe,
        without=without,
    )


def load_universe(path: Path) -> tuple[tuple[str, ...], dict[str, str]]:
    """The K2 symbols and their digest, both from one read of the artifact."""
    raw = path.read_bytes()
    payload = json.loads(raw)
    rows = payload.get("rows") if isinstance(payload, Mapping) else None
    if not isinstance(rows, list) or not rows:
        raise ValueError(f"the K2 universe artifact at {path} carries no rows")
    symbols: list[str] = []
    for row in rows:
        if not isinstance(row, Mapping) or not isinstance(row.get("symbol"), str):
            raise ValueError("the K2 universe artifact rows must name a symbol")
        symbols.append(row["symbol"])
    return _symbols(symbols), {"path": str(path), "sha256": hashlib.sha256(raw).hexdigest()}


def resolve_universe(path: Path) -> tuple[str, ...]:
    """The symbols the K2 scanner ranked, in the artifact's order; never re-ranked."""
    return load_universe(path)[0]


def verified_baseline_text(path: Path) -> str:
    """The file's text, after proving the manifest commits to exactly these bytes.

    The runner's loader calls this instead of ``path.read_text`` so a file that
    a crash left outside the manifest's commit record is refused, not used.
    """
    raw = path.read_bytes()
    try:
        manifest = json.loads((path.parent / MANIFEST_NAME).read_text(encoding="utf-8"))
        expected = manifest["files"][path.name]["sha256"]
    except (OSError, ValueError, KeyError, TypeError) as exc:
        raise ValueError(f"{path.name} has no manifest commit in {path.parent}: {exc}") from exc
    digest = hashlib.sha256(raw).hexdigest()
    if digest != expected:
        raise ValueError(f"{path.name} does not match its manifest commit: {digest} != {expected}")
    return raw.decode("utf-8")


def _parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        prog="python -m sextant.data.baseline_recipe",
        description="Build C's four per-segment baseline files and their manifest in one run.",
    )
    parser.add_argument(
        "--out",
        type=Path,
        required=True,
        metavar="DIR",
        help="directory receiving the four files and manifest.json",
    )
    source = parser.add_mutually_exclusive_group(required=True)
    source.add_argument("--symbols", nargs="+", metavar="SYMBOL", help="explicit vendor symbols")
    source.add_argument(
        "--universe",
        type=Path,
        metavar="PATH",
        help="K2 ranked universe artifact from python -m sextant.paper.c_universe",
    )
    parser.add_argument(
        "--as-of",
        type=date.fromisoformat,
        default=None,
        metavar="YYYY-MM-DD",
        help="session day whose prior sessions are baselined; default the current segment's date",
    )
    parser.add_argument(
        "--prior-sessions",
        type=int,
        default=20,
        metavar="N",
        help="prior XNYS sessions to baseline (default 20)",
    )
    return parser


def _default_client() -> BarsClient:
    from alpaca.data.historical import StockHistoricalDataClient

    api_key, secret_key = environment_credentials()
    # raw_data=True returns the flat {symbol: [bar, ...]} mapping the fetchers read.
    # alpaca-py names the parameter request_params; the fetchers call positionally.
    return cast(
        BarsClient,
        StockHistoricalDataClient(api_key=api_key, secret_key=secret_key, raw_data=True),
    )


def main(
    argv: Sequence[str] | None = None,
    *,
    client_factory: Callable[[], BarsClient] | None = None,
    now_ns: Callable[[], int] = time.time_ns,
) -> int:
    arguments = list(sys.argv[1:] if argv is None else argv)
    parser = _parser()
    args = parser.parse_args(arguments)
    command = ["python", "-m", "sextant.data.baseline_recipe", *arguments]
    moment = now_ns()
    symbols: tuple[str, ...]
    universe: dict[str, str] | None
    if args.symbols is not None:
        symbols = _symbols(part for value in args.symbols for part in value.split(","))
        source, universe = "symbols", None
    elif args.universe is not None:
        symbols, universe = load_universe(args.universe)
        source = "k2_universe"
    else:
        parser.error("one of --symbols or --universe is required")
    as_of = args.as_of if args.as_of is not None else segment_start(moment)[1]
    factory = client_factory if client_factory is not None else _default_client
    try:
        manifest = run(
            factory(),
            symbols,
            args.out,
            command=command,
            as_of=as_of,
            known_at=moment,
            prior_sessions=args.prior_sessions,
            source=source,
            universe=universe,
        )
    except BaselineFlagged as flagged:
        print(
            f"warning: baseline flags {json.dumps(flagged.flags, sort_keys=True)}",
            file=sys.stderr,
            flush=True,
        )
        return 1
    print(f"wrote {len(manifest['files'])} baselines and {args.out / MANIFEST_NAME}", flush=True)
    print(coverage_line(manifest), flush=True)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
