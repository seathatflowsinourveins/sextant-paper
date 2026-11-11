"""Bind the existing parity installer's helpers to one isolated CI profile.

Source: c5ffd943fcfbb7fd861ef514e0c5ee7361b1d14b:
deploy/wsl/lane-runtimes/parity.py:274-329 (_quality_missing, ensure_workspace, _native).
The upstream module is unchanged. Profile data is ~/.dsh-full's no-memory recipe.
pnpm workspace storeDir: https://pnpm.io/settings, read 2026-11-09.
"""

import importlib.util
import json
import os
from pathlib import Path

ROOT = Path(__file__).resolve().parents[2]
module = importlib.util.spec_from_file_location(
    "parity", Path(__file__).parent / "upstream/parity.py"
)
assert module and module.loader
parity = importlib.util.module_from_spec(module)
module.loader.exec_module(parity)
spec = json.loads((Path(__file__).parent / "dsh-profile.json").read_text())
profile = Path(os.environ["DSH_HOME"]) / "profiles/headless"
parity.ensure_workspace(profile, ROOT)
# Fresh hosted profiles keep package-manager state beside their own manifest.
workspace_path = profile / "pnpm-workspace.yaml"
if "storeDir:" not in workspace_path.read_text():
    with workspace_path.open("a") as workspace:
        workspace.write(f"\nstoreDir: {json.dumps(str(profile / 'pnpm-store'))}\n")
manifest_path = profile / "package.json"
manifest = json.loads(manifest_path.read_text()) if manifest_path.exists() else {"private": True}
manifest.setdefault("dsh", {}).setdefault("profile", {}).setdefault(
    "bundles", ["@deepseek-ai/dsh-base", "@deepseek-ai/dsh-headless"]
)
manifest_path.write_text(json.dumps(manifest, indent=2) + "\n")
# The composite loads this dependency through its own overlay, not a second bundle row.
parity._native(
    ["pnpm", "--dir", str(profile), "add", "--save-exact", "@perrylink/dsh-github@0.7.27"]
)
# Native pnpm installs the existing registration shim's own pinned dependency.
parity._native(
    [
        "pnpm",
        "--dir",
        str(ROOT / "config/dsh/review-cmd"),
        "install",
        "--frozen-lockfile",
        "--store-dir",
        str(profile / "pnpm-store"),
    ]
)
quality = parity.SPEC["runtimes"]["dsh"]["quality"]
quality["profiles"] = ["headless"]
quality["plugins"] = [
    row
    for row in spec["bundles"]
    if not row["spec"].startswith(("config/", ".github/"))
    and not row["package"].startswith("@deepseek-ai/")
]
missing = parity._quality_missing(profile, "headless", ROOT)
local = [
    str(ROOT / row["spec"])
    for row in spec["bundles"]
    if row["spec"].startswith(("config/", ".github/")) and row["package"] != "sextant-dsh-quality"
]
parity._native(
    [
        "dsh",
        "plugin",
        "--profile",
        "headless",
        "add",
        "--save-exact",
        *missing,
        *local,
        str(ROOT / ".github/gate/bundles/api-only"),
    ]
)
parity._native(["dsh", "--profile", "headless", "--dump-config"])
