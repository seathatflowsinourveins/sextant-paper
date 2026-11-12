"""Immutable, offline ranking generations (ARCH R5 ruling 6530772072/6530860159).

No scorer, publisher, I/O or import-time registration lives here. The page and
node must hash the SAME canonical payload, not an rc7 transport envelope.
PROVISIONAL is decision evidence; SETTLED is a separate audit observation, not
vendor finality or permission to revise an earlier decision.

Upstream checked: nautilus_trader@v2.0.0rc7, installed in
.venv/lib/python3.13/site-packages/nautilus_trader/:
  model/__init__.pyi:1673-1702,8397 (CustomData, DataType, registration);
  model/custom.py:94-158,195-218 (JSON and Arrow callback/schema pattern).
Pinned source: docs/concepts/custom_data.md:188-201,274-292,337-363;
crates/model/src/python/data/mod.rs:617-777. Unlike the generic decorator, this
contract needs frozen nested records and lossless Decimal strings. rc7 requires
catalog ts_event/ts_init to be timestamp(ns, UTC); its callback view casts ONLY
those columns from/to the contract's int64 nanoseconds, with no datetime/float.
House composition: data/focus_receipts.py:501-624 (separate event/receipt clocks,
canonical hash identity before publication). File publication is NOT this slice.
"""

from __future__ import annotations

import hashlib
import json
from collections.abc import Mapping, Sequence
from dataclasses import dataclass, fields, is_dataclass
from datetime import date
from decimal import Decimal, InvalidOperation
from enum import StrEnum
from typing import Any, ClassVar

import pyarrow as pa
from nautilus_trader.model import DataType, register_custom_data_class

SCHEMA_VERSION = 2
TYPE_NAME = "RankingGeneration"
_MAX_INT64 = 2**63 - 1


class GenerationStatus(StrEnum):
    PROVISIONAL = "PROVISIONAL"
    SETTLED = "SETTLED"


class CoverageState(StrEnum):
    UNKNOWN = "UNKNOWN"
    BOOTSTRAPPING = "BOOTSTRAPPING"
    READY = "READY"
    DEGRADED = "DEGRADED"
    RECOVERING = "RECOVERING"


def _text(value: Any, name: str, *, optional: bool = False) -> None:
    if value is None and optional:
        return
    if not isinstance(value, str) or not value:
        raise ValueError(f"{name} must be a nonempty string")


def _integer(value: Any, name: str, *, optional: bool = False, minimum: int = 0) -> None:
    if value is None and optional:
        return
    if type(value) is not int or not minimum <= value <= _MAX_INT64:
        raise ValueError(f"{name} must be an int64 >= {minimum}")


def _boolean(value: Any, name: str, *, optional: bool = False) -> None:
    if value is None and optional:
        return
    if type(value) is not bool:
        raise ValueError(f"{name} must be boolean")


def _decimal(value: Any) -> None:
    if value is not None and (not isinstance(value, Decimal) or not value.is_finite()):
        raise ValueError("numeric values must be finite Decimal or null, never float")


def _tuple[T](value: Sequence[T], cls: type[T]) -> tuple[T, ...]:
    if not isinstance(value, (tuple, list)) or any(not isinstance(item, cls) for item in value):
        raise ValueError(f"expected a sequence of {cls.__name__}")
    return tuple(value)


@dataclass(frozen=True, slots=True)
class GenerationId:
    """Sequence lineage is scoped to a producer epoch; null means no predecessor."""

    producer_epoch: str
    sequence: int
    previous_sequence: int | None = None

    def __post_init__(self) -> None:
        _text(self.producer_epoch, "producer_epoch")
        _integer(self.sequence, "sequence")
        _integer(self.previous_sequence, "previous_sequence", optional=True)
        if self.previous_sequence is not None and self.previous_sequence >= self.sequence:
            raise ValueError("previous_sequence must precede sequence within the producer epoch")


@dataclass(frozen=True, slots=True)
class RankingReason:
    code: str
    detail: str | None = None
    children: tuple[RankingReason, ...] = ()

    def __post_init__(self) -> None:
        _text(self.code, "reason code")
        _text(self.detail, "reason detail", optional=True)
        object.__setattr__(self, "children", _tuple(self.children, RankingReason))


@dataclass(frozen=True, slots=True)
class Coverage:
    """Evidence, not inferred readiness: unknown counts remain null, never zero-filled."""

    state: CoverageState
    complete: bool
    expected_symbols: int | None = None
    observed_symbols: int | None = None
    missing_symbols: int | None = None
    late_symbols: int | None = None
    invalid_symbols: int | None = None
    reasons: tuple[RankingReason, ...] = ()

    def __post_init__(self) -> None:
        if not isinstance(self.state, CoverageState):
            raise ValueError("unknown coverage state")
        _boolean(self.complete, "coverage.complete")
        for name in (
            "expected_symbols",
            "observed_symbols",
            "missing_symbols",
            "late_symbols",
            "invalid_symbols",
        ):
            _integer(getattr(self, name), name, optional=True)
        object.__setattr__(self, "reasons", _tuple(self.reasons, RankingReason))


@dataclass(frozen=True, slots=True)
class RankingFeature:
    """All numeric inputs, including prices/money, retain exact Decimal spelling."""

    name: str
    value: Decimal | None = None
    ts_event: int | None = None
    known_at: int | None = None
    partial: bool = False
    completed: bool = True

    def __post_init__(self) -> None:
        _text(self.name, "feature name")
        _decimal(self.value)
        _integer(self.ts_event, "feature.ts_event", optional=True)
        _integer(self.known_at, "feature.known_at", optional=True)
        _boolean(self.partial, "feature.partial")
        _boolean(self.completed, "feature.completed")


@dataclass(frozen=True, slots=True)
class RankingRow:
    symbol: str
    instrument_id: str | None = None
    score: Decimal | None = None
    rank: int | None = None
    features: tuple[RankingFeature, ...] = ()
    partial: bool = False
    completed: bool = True
    focus_eligible: bool | None = None
    reasons: tuple[RankingReason, ...] = ()

    def __post_init__(self) -> None:
        _text(self.symbol, "symbol")
        _text(self.instrument_id, "instrument_id", optional=True)
        _decimal(self.score)
        _integer(self.rank, "rank", minimum=1, optional=True)
        _boolean(self.partial, "row.partial")
        _boolean(self.completed, "row.completed")
        _boolean(self.focus_eligible, "focus_eligible", optional=True)
        features_ = _tuple(self.features, RankingFeature)
        if len({item.name for item in features_}) != len(features_):
            raise ValueError("duplicate feature name")
        object.__setattr__(self, "features", tuple(sorted(features_, key=lambda item: item.name)))
        object.__setattr__(self, "reasons", _tuple(self.reasons, RankingReason))


@dataclass(frozen=True, slots=True, kw_only=True)
class RankingGeneration:
    generation_id: GenerationId
    session_date: date
    input_cutoff: int
    ts_event: int
    ts_init: int
    known_at: int
    method_version: str
    profile_version: str
    ranker_version: str
    coverage: Coverage
    status: GenerationStatus
    rows: tuple[RankingRow, ...] = ()

    schema_version: ClassVar[int] = SCHEMA_VERSION

    def __post_init__(self) -> None:
        if not isinstance(self.generation_id, GenerationId) or not isinstance(
            self.coverage, Coverage
        ):
            raise ValueError("generation id and coverage must be immutable records")
        if type(self.session_date) is not date:
            raise ValueError("session_date must be a date")
        for name in ("input_cutoff", "ts_event", "ts_init", "known_at"):
            _integer(getattr(self, name), name)
        _text(self.method_version, "method_version")
        _text(self.profile_version, "profile_version")
        _text(self.ranker_version, "ranker_version")
        if not isinstance(self.status, GenerationStatus):
            raise ValueError("unknown generation status")
        rows = _tuple(self.rows, RankingRow)
        if len({item.symbol for item in rows}) != len(rows):
            raise ValueError("duplicate symbol identity")
        object.__setattr__(self, "rows", tuple(sorted(rows, key=_row_key)))

    def to_dict(self) -> dict[str, Any]:
        return {"type": TYPE_NAME, "schema_version": SCHEMA_VERSION, **_wire(self)}

    def to_json(self) -> str:
        return canonical_bytes(self).decode("utf-8")

    @classmethod
    def from_json(cls, data: bytes | str | Mapping[str, Any]) -> RankingGeneration:
        values = _load(data) if isinstance(data, (str, bytes)) else data
        return _generation(values)

    @classmethod
    def type_name_static(cls) -> str:
        return TYPE_NAME

    @classmethod
    def arrow_schema_py(cls) -> pa.Schema:
        return _RC7_ARROW_SCHEMA

    def encode_record_batch_py(self, items: Sequence[RankingGeneration]) -> pa.RecordBatch:
        return encode_arrow(items).cast(_RC7_ARROW_SCHEMA)

    @classmethod
    def decode_record_batch_py(
        cls, metadata: Mapping[str, str], batch: pa.RecordBatch
    ) -> list[RankingGeneration]:
        # rc7 adds a data_type column and catalog metadata around our payload.
        if "type_name" in metadata and metadata["type_name"] != TYPE_NAME:
            raise ValueError("unknown custom data type")
        names = batch.schema.names
        expected = _RC7_ARROW_SCHEMA.names
        if names not in (expected, [*expected, "data_type"]):
            raise ValueError("unknown rc7 Arrow schema")
        payload = batch.select(expected)
        # rc7's DataFusion query returns top-level Utf8 columns as Utf8View.
        # Accept this one lossless physical representation, NOT arbitrary casts
        # (for example float timestamps or numeric Decimal columns).
        normalized = pa.schema(
            [
                pa.field(field.name, pa.string(), nullable=field.nullable, metadata=field.metadata)
                if pa.types.is_string_view(field.type)
                else field
                for field in payload.schema
            ]
        )
        if not normalized.equals(_RC7_ARROW_SCHEMA, check_metadata=False):
            raise ValueError("unknown rc7 Arrow schema")
        payload = payload.cast(_RC7_ARROW_SCHEMA)
        return decode_arrow(payload.cast(ARROW_SCHEMA))


def _row_key(row: RankingRow) -> tuple[bool, int, str, str]:
    return row.rank is None, row.rank or 0, row.symbol, row.instrument_id or ""


def _wire(value: Any) -> Any:
    if isinstance(value, Decimal):
        _decimal(value)
        return str(value)
    if isinstance(value, StrEnum):
        return value.value
    if type(value) is date:
        return value.isoformat()
    if is_dataclass(value) and not isinstance(value, type):
        return {field.name: _wire(getattr(value, field.name)) for field in fields(value)}
    if isinstance(value, tuple):
        return [_wire(item) for item in value]
    if value is None or type(value) in (str, int, bool):
        return value
    raise ValueError(f"unsupported wire value: {type(value).__name__}; floats are forbidden")


def _dump(value: Any) -> bytes:
    return json.dumps(
        value, sort_keys=True, separators=(",", ":"), ensure_ascii=True, allow_nan=False
    ).encode()


def canonical_bytes(record: RankingGeneration) -> bytes:
    """The sole canonical payload encoding: sorted keys, ranked rows, explicit nulls.

    Feature names are sorted; reasons (and their children) retain meaningful
    producer order. Decimal scale/exponents/signed zero are evidence and are NOT
    normalized. Hashes never contain a digest, a mutable clock or transport data.
    """
    if not isinstance(record, RankingGeneration):
        raise TypeError("expected RankingGeneration")
    return _dump(record.to_dict())


def generation_digest(record: RankingGeneration) -> str:
    return hashlib.sha256(canonical_bytes(record)).hexdigest()


def _reject_number(token: str) -> Any:
    raise ValueError(f"floating point/nonfinite JSON number forbidden: {token}")


def _unique_object(pairs: list[tuple[str, Any]]) -> dict[str, Any]:
    result: dict[str, Any] = {}
    for key, value in pairs:
        if key in result:
            raise ValueError(f"duplicate JSON key: {key}")
        result[key] = value
    return result


def _load(data: bytes | str) -> Any:
    return json.loads(
        data,
        parse_float=_reject_number,
        parse_constant=_reject_number,
        object_pairs_hook=_unique_object,
    )


def _object(value: Any, required: set[str], optional: set[str] | None = None) -> dict[str, Any]:
    if (
        not isinstance(value, Mapping)
        or not required <= value.keys()
        or value.keys() - required - (optional or set())
    ):
        raise ValueError("missing or unknown record fields")
    return dict(value)


def _list(value: Any) -> list[Any]:
    if not isinstance(value, list):
        raise ValueError("expected JSON array")
    return value


def _read_decimal(value: Any) -> Decimal | None:
    if value is None:
        return None
    if not isinstance(value, str) or not value:
        raise ValueError("Decimal wire values must be strings or null")
    try:
        result = Decimal(value)
    except InvalidOperation as error:
        raise ValueError("invalid Decimal string") from error
    _decimal(result)
    return result


def _reason(value: Any) -> RankingReason:
    row = _object(value, {"code", "children"}, {"detail"})
    return RankingReason(
        row["code"], row.get("detail"), tuple(_reason(item) for item in _list(row["children"]))
    )


def _feature(value: Any) -> RankingFeature:
    row = _object(value, {"name", "partial", "completed"}, {"value", "ts_event", "known_at"})
    return RankingFeature(
        row["name"],
        _read_decimal(row.get("value")),
        row.get("ts_event"),
        row.get("known_at"),
        row["partial"],
        row["completed"],
    )


def _row(value: Any) -> RankingRow:
    row = _object(
        value,
        {"symbol", "features", "partial", "completed", "reasons"},
        {"instrument_id", "score", "rank", "focus_eligible"},
    )
    return RankingRow(
        row["symbol"],
        row.get("instrument_id"),
        _read_decimal(row.get("score")),
        row.get("rank"),
        tuple(_feature(item) for item in _list(row["features"])),
        row["partial"],
        row["completed"],
        row.get("focus_eligible"),
        tuple(_reason(item) for item in _list(row["reasons"])),
    )


def _generation(value: Any) -> RankingGeneration:
    row = _object(
        value, {"type", "schema_version", *(field.name for field in fields(RankingGeneration))}
    )
    if (
        row["type"] != TYPE_NAME
        or type(row["schema_version"]) is not int
        or row["schema_version"] != SCHEMA_VERSION
    ):
        raise ValueError("unknown ranking schema version or type")
    identity = _object(row["generation_id"], {"producer_epoch", "sequence"}, {"previous_sequence"})
    coverage = _object(
        row["coverage"],
        {"state", "complete", "reasons"},
        {
            "expected_symbols",
            "observed_symbols",
            "missing_symbols",
            "late_symbols",
            "invalid_symbols",
        },
    )
    _text(row["session_date"], "session_date")
    session = date.fromisoformat(row["session_date"])
    if session.isoformat() != row["session_date"]:
        raise ValueError("session_date must be YYYY-MM-DD")
    return RankingGeneration(
        generation_id=GenerationId(**identity),
        session_date=session,
        input_cutoff=row["input_cutoff"],
        ts_event=row["ts_event"],
        ts_init=row["ts_init"],
        known_at=row["known_at"],
        method_version=row["method_version"],
        profile_version=row["profile_version"],
        ranker_version=row["ranker_version"],
        coverage=Coverage(
            **{
                **coverage,
                "state": CoverageState(coverage["state"]),
                "reasons": tuple(_reason(item) for item in _list(coverage["reasons"])),
            }
        ),
        status=GenerationStatus(row["status"]),
        rows=tuple(_row(item) for item in _list(row["rows"])),
    )


# Recursive reasons remain one canonical JSON string column at each explicit
# boundary. Arrow structs cannot be recursive; no depth cap or lossy flattening.
_FEATURE = pa.struct(
    [
        pa.field("name", pa.string(), nullable=False),
        pa.field("value", pa.string()),
        pa.field("ts_event", pa.int64()),
        pa.field("known_at", pa.int64()),
        pa.field("partial", pa.bool_(), nullable=False),
        pa.field("completed", pa.bool_(), nullable=False),
    ]
)
_ROW = pa.struct(
    [
        pa.field("symbol", pa.string(), nullable=False),
        pa.field("instrument_id", pa.string()),
        pa.field("score", pa.string()),
        pa.field("rank", pa.int64()),
        pa.field("features", pa.list_(_FEATURE), nullable=False),
        pa.field("partial", pa.bool_(), nullable=False),
        pa.field("completed", pa.bool_(), nullable=False),
        pa.field("focus_eligible", pa.bool_()),
        pa.field("reasons", pa.string(), nullable=False),
    ]
)
ARROW_SCHEMA = pa.schema(
    [
        pa.field("type", pa.string(), nullable=False),
        pa.field("schema_version", pa.int64(), nullable=False),
        pa.field(
            "generation_id",
            pa.struct(
                [
                    pa.field("producer_epoch", pa.string(), nullable=False),
                    pa.field("sequence", pa.int64(), nullable=False),
                    pa.field("previous_sequence", pa.int64()),
                ]
            ),
            nullable=False,
        ),
        pa.field("session_date", pa.string(), nullable=False),
        pa.field("input_cutoff", pa.int64(), nullable=False),
        pa.field("ts_event", pa.int64(), nullable=False),
        pa.field("ts_init", pa.int64(), nullable=False),
        pa.field("known_at", pa.int64(), nullable=False),
        pa.field("method_version", pa.string(), nullable=False),
        pa.field("profile_version", pa.string(), nullable=False),
        pa.field("ranker_version", pa.string(), nullable=False),
        pa.field(
            "coverage",
            pa.struct(
                [
                    pa.field("state", pa.string(), nullable=False),
                    pa.field("complete", pa.bool_(), nullable=False),
                    *(
                        pa.field(name, pa.int64())
                        for name in (
                            "expected_symbols",
                            "observed_symbols",
                            "missing_symbols",
                            "late_symbols",
                            "invalid_symbols",
                        )
                    ),
                    pa.field("reasons", pa.string(), nullable=False),
                ]
            ),
            nullable=False,
        ),
        pa.field("status", pa.string(), nullable=False),
        pa.field("rows", pa.list_(_ROW), nullable=False),
    ],
    metadata={b"type": TYPE_NAME.encode(), b"schema_version": str(SCHEMA_VERSION).encode()},
)
_RC7_ARROW_SCHEMA = pa.schema(
    [
        pa.field(field.name, pa.timestamp("ns", tz="UTC"), nullable=field.nullable)
        if field.name in ("ts_event", "ts_init")
        else field
        for field in ARROW_SCHEMA
    ],
    metadata=ARROW_SCHEMA.metadata,
)


def encode_arrow(records: Sequence[RankingGeneration]) -> pa.RecordBatch:
    rows = []
    for record in records:
        row = record.to_dict()
        row["coverage"]["reasons"] = _dump(row["coverage"]["reasons"]).decode()
        for item in row["rows"]:
            item["reasons"] = _dump(item["reasons"]).decode()
        rows.append(row)
    return pa.RecordBatch.from_pylist(rows, schema=ARROW_SCHEMA)


def decode_arrow(batch: pa.RecordBatch) -> list[RankingGeneration]:
    if not batch.schema.equals(ARROW_SCHEMA, check_metadata=True):
        raise ValueError("unknown ranking Arrow schema")
    records = []
    for row in batch.to_pylist():
        row["coverage"]["reasons"] = _load(row["coverage"]["reasons"])
        for item in row["rows"]:
            item["reasons"] = _load(item["reasons"])
        records.append(RankingGeneration.from_json(row))
    return records


def register_ranking_generation() -> DataType:
    """Explicit rc7 setup only; imports and construction never register anything."""
    register_custom_data_class(RankingGeneration)
    return DataType(TYPE_NAME)
