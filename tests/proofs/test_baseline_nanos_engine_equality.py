"""The baseline nanosecond helper uses the engine's ``dt_to_unix_nanos``.

Source: ``nautechsystems/nautilus_trader@v2.0.0rc8``
``d53849b216e20f2c122ca01b4dd0da377638b186``,
``python/nautilus_trader/core/datetime.py:81-104`` (``dt_to_unix_nanos``).
This is the ranking half of LEAD-1105 section 2.  Both call sites build an
America/New_York-aware value, so no aware-only guard is needed here.  These
tests are the RED witness: on the old arithmetic the delegation test fails,
and only the engine import can make it pass.
"""

import inspect
from datetime import datetime
from pathlib import Path
from zoneinfo import ZoneInfo

import pytest
from nautilus_trader.core.datetime import dt_to_unix_nanos

from sextant.ranking import baseline

ET = ZoneInfo("America/New_York")

# Each side of both 2026 America/New_York DST changes (the fall change's
# repeated 01:00 hour is pinned with fold=1), a microsecond-bearing instant,
# and an instant before the 1970 epoch.  Literals are computed independently
# of the helper.
_CASES: tuple[tuple[str, datetime, int], ...] = (
    (
        "spring-before-0200",
        datetime(2026, 3, 8, 1, 59, 59, 999999, tzinfo=ET),
        1772953199999999000,
    ),
    (
        "spring-after-0300",
        datetime(2026, 3, 8, 3, 0, 0, tzinfo=ET),
        1772953200000000000,
    ),
    (
        "fall-before-0200",
        datetime(2026, 11, 1, 1, 59, 59, 999999, tzinfo=ET),
        1793512799999999000,
    ),
    (
        "fall-after-0100-fold1",
        datetime(2026, 11, 1, 1, 0, 0, tzinfo=ET, fold=1),
        1793512800000000000,
    ),
    (
        "fall-after-0200",
        datetime(2026, 11, 1, 2, 0, 0, tzinfo=ET),
        1793516400000000000,
    ),
    (
        "microsecond-bearing",
        datetime(2026, 6, 15, 12, 34, 56, 789012, tzinfo=ET),
        1781541296789012000,
    ),
    (
        "before-1970",
        datetime(1969, 7, 20, 20, 17, 40, 123456, tzinfo=ET),
        -14168539876544000,
    ),
)


def test_the_function_is_the_installed_engines_module_member() -> None:
    assert dt_to_unix_nanos.__module__ == "nautilus_trader.core.datetime"
    provenance = inspect.getsourcefile(dt_to_unix_nanos)
    assert provenance is not None
    assert Path(provenance).parts[-4:] == (
        "site-packages",
        "nautilus_trader",
        "core",
        "datetime.py",
    )


@pytest.mark.parametrize(
    ("value", "expected_ns"),
    [(value, expected_ns) for _, value, expected_ns in _CASES],
    ids=[case_id for case_id, _, _ in _CASES],
)
def test_the_helper_equals_the_engine(value: datetime, expected_ns: int) -> None:
    assert dt_to_unix_nanos(value) == expected_ns
    assert baseline._nanos(value) == expected_ns


def test_the_helper_gets_its_value_from_the_engine(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    assert baseline.dt_to_unix_nanos is dt_to_unix_nanos
    sentinel = 7_012_345_678_901_234_567
    monkeypatch.setattr(baseline, "dt_to_unix_nanos", lambda _value: sentinel)
    value = _CASES[0][1]
    assert baseline._nanos(value) == sentinel
