"""Publisher-side heartbeat emitter for the ADR-0003 entry veto.

Emits exactly what ``GenerationReader.accept_heartbeat`` accepts
(``src/sextant/ranking/reader.py:52-64``): keyword-only ``at``/``received_ns``
ints with ``0 <= at <= received_ns``, ``received_ns - at`` within
``heartbeat_max_age_s``, and ``at`` never regressing. ``at`` is the published
generation's ``known_at`` (``src/sextant/ranking/publisher.py:279`` sets
``known_at=now`` at publish), so a heartbeat exists only for a published,
complete generation and ages out with it (``reader.py:70-73`` checks both
stored clocks). Injected clock and sink only: no stream, socket or thread
starts here, and an invalid heartbeat is never sent (``reader.py:60`` would
clear the reader's stored heartbeat). A sink exception is counted in
``sink_failures`` and treated as not sent, so a committed publication still
returns its generation.
"""

from collections.abc import Callable
from typing import Protocol

from sextant.ranking.generation import CoverageState, RankingGeneration
from sextant.ranking.registry import Registry


def heartbeat_signal(feed: str) -> str:
    """The node bus signal name the RankingActor beats on (DESIGN-N2B1 section 1)."""
    return f"ranking.heartbeat.{feed}"


class HeartbeatSink(Protocol):
    def __call__(self, *, at: int, received_ns: int) -> bool: ...


class HeartbeatEmitter:
    """One heartbeat per newly published eligible generation; never a manufactured one."""

    def __init__(
        self, *, registry: Registry, clock: Callable[[], int], sink: HeartbeatSink
    ) -> None:
        self._registry, self._clock, self._sink = registry, clock, sink
        self._last_at: int | None = None
        self.sink_failures = 0  # sink exceptions counted as not sent, never raised

    def emit(self, generation: RankingGeneration | None) -> bool:
        if not isinstance(generation, RankingGeneration) or not _eligible(generation):
            return False
        at, received_ns = generation.known_at, self._clock()
        if (
            type(at) is not int
            or type(received_ns) is not int
            or not 0 <= at <= received_ns
            or received_ns - at > self._registry.seconds("heartbeat_max_age_s")
            or (self._last_at is not None and at <= self._last_at)
        ):
            return False
        try:
            sent = self._sink(at=at, received_ns=received_ns)
        except Exception:  # containment: a broken optional sink never fails a publication
            self.sink_failures += 1
            return False
        if not sent:
            return False
        self._last_at = at
        return True


def _eligible(generation: RankingGeneration) -> bool:
    cov = generation.coverage
    return bool(
        cov.state is CoverageState.READY
        and cov.complete
        and cov.expected_symbols == cov.observed_symbols == len(generation.rows)
        and cov.missing_symbols == cov.invalid_symbols == cov.late_symbols == 0
        and generation.ts_event
        <= generation.input_cutoff
        <= generation.known_at
        <= generation.ts_init
    )
