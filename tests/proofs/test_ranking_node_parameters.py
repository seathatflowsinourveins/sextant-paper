"""Review F7 and the lead's corrected ruling (04:2x): the node's windows come from registry rows
only. A sourced row is UNCALIBRATED; a window with no source is UNAVAILABLE (null) and the node
runs without the feature (the snapshot recorder). No flag overrides a row; the capacity buckets
are no longer a tunable (every second of the run is kept).
"""

from __future__ import annotations

import json
from pathlib import Path

import pytest

from sextant.ranking import node_parameters

ROOT = Path(__file__).parents[2]


def test_the_rows_are_sourced_or_unavailable_never_invented() -> None:
    rows = node_parameters.load()
    assert set(rows) == {"page_window_s", "receipt_interval_s", "status_snapshot_window_s"}
    assert (rows["page_window_s"].value, rows["page_window_s"].status) == (14400, "UNCALIBRATED")
    assert (rows["receipt_interval_s"].value, rows["receipt_interval_s"].status) == (
        60,
        "UNCALIBRATED",
    )
    snapshot = rows["status_snapshot_window_s"]
    assert (snapshot.value, snapshot.status) == (None, "UNAVAILABLE")
    assert all(row.source.strip() and row.unit and row.last_change for row in rows.values())


def test_a_row_resolves_to_its_value_and_no_value_overrides_it() -> None:
    rows = node_parameters.load()
    assert node_parameters.resolve(rows, "page_window_s") == (14400, "registry")
    assert node_parameters.resolve(rows, "status_snapshot_window_s") == (None, "unavailable")
    with pytest.raises(ValueError, match="page_window_s"):
        node_parameters.resolve(rows, "page_window_s", 60)
    with pytest.raises(ValueError, match="bucket_window_s"):  # no row: not a tunable any more
        node_parameters.resolve(rows, "bucket_window_s")


def test_a_malformed_registry_is_refused(tmp_path) -> None:
    bad = json.loads((ROOT / "deploy/ranking-node/parameters.json").read_text())
    bad["rows"]["page_window_s"]["value"] = "14400"  # a string is not a window
    path = tmp_path / "parameters.json"
    path.write_text(json.dumps(bad))
    with pytest.raises(ValueError, match="page_window_s"):
        node_parameters.load(path)
    bad["rows"]["page_window_s"]["value"] = 14400
    bad["rows"]["status_snapshot_window_s"]["status"] = "UNCALIBRATED"  # null, sourced status
    path.write_text(json.dumps(bad))
    with pytest.raises(ValueError, match="status_snapshot_window_s"):
        node_parameters.load(path)


def test_the_resolution_is_written_as_a_receipt(tmp_path) -> None:
    rows = node_parameters.load()
    resolved = {
        "page_window_s": node_parameters.resolve(rows, "page_window_s"),
        "status_snapshot_window_s": node_parameters.resolve(rows, "status_snapshot_window_s"),
    }
    path = node_parameters.write_receipt(tmp_path, resolved)
    body = json.loads(path.read_text())
    assert body["page_window_s"] == {"value": 14400, "origin": "registry"}
    assert body["status_snapshot_window_s"] == {"value": None, "origin": "unavailable"}
