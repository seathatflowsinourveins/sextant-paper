"""Immutable local artifact receipts, not a Desktop install or compiler result."""

from __future__ import annotations

import hashlib
import json
import re
from pathlib import Path

STEMS = {"ema_cross", "hurst_vpin", "quote_imbalance", "exit_levels"}


def preset_receipt(root: Path) -> list[dict[str, str]]:
    manifest = json.loads((root / "manifest.json").read_text())
    if not isinstance(manifest, list) or len(manifest) != 4:
        raise ValueError("Need the four reviewed Pine artifacts")
    result = []
    for item in manifest:
        filename = item["file"]
        if filename not in {stem + ".pine" for stem in STEMS}:
            raise ValueError("Unknown Pine preset")
        source = (root / filename).read_bytes()
        digest = hashlib.sha256(source).hexdigest()
        script_id = item["script_id"]
        text = source.decode()
        if (
            digest != item["sha256"]
            or not re.fullmatch(r"NS \d{4}-\d{2}-\d{2} · " + filename[:-5], script_id)
            or f'indicator("{script_id}"' not in text
            or hashlib.sha256(
                source.replace(script_id.encode(), item["upstream_title"].encode(), 1)
            ).hexdigest()
            != item["upstream_sha256"]
        ):
            raise ValueError("Pine hash or immutable script ID differs from reviewed artifact")
        result.append({"file": filename, "script_id": script_id, "sha256": digest})
    if {row["file"] for row in result} != {stem + ".pine" for stem in STEMS}:
        raise ValueError("Duplicate or missing Pine preset")
    return result
