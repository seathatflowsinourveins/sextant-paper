"""Paper-local B inputs, BUILD ON Sextant@453cae04 sim/launcher.py:259-272.

The caller supplies its exit, fee, state and segment choices; no deployment defaults.
S1 stays in the sim repository; B carries this sourced composition without a sim import.
Native Strategy configuration: nautilus_trader@d53849b2 trading/__init__.pyi:483-552.
"""

import json
from decimal import Decimal
from pathlib import Path
from typing import TypedDict

from sextant.entry.parameters import EntryParameters
from sextant.exits.policy import ExitParams
from sextant.ranking.baseline import BaselineTable


class EntryInputs(TypedDict):
    entry_params: EntryParameters | None
    exit_params: ExitParams | None
    commission_rate: Decimal | None
    basis_path: Path | None
    baseline: BaselineTable | None


def load_entry_inputs(
    entry: Path | EntryParameters | None,
    *,
    exit_params: ExitParams | None,
    commission_rate: Decimal | None,
    basis_path: Path | None,
    baseline: BaselineTable | None,
) -> EntryInputs:
    """Read S1's sizing rows once, or reuse B's already validated registry object."""
    if isinstance(entry, Path):
        entry = EntryParameters.from_rows(json.loads(entry.read_text()), sizing_only=True)
    return {
        "entry_params": entry,
        "exit_params": exit_params,
        "commission_rate": commission_rate,
        "basis_path": basis_path,
        "baseline": baseline,
    }
