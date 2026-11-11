#!/usr/bin/env python3
"""Check the live dsh profile and the gate recipe against config/dsh/profile.lock.json.

The lock is the source. The live profile is the machine that runs sessions; the gate
recipe is the headless copy CI installs. A missing, extra or changed pin is drift, and
drift must stop the session-start command instead of silently changing the harness.
"""

from __future__ import annotations

import argparse
import json
import shutil
import subprocess
import sys
from pathlib import Path
from typing import Any

ROOT = Path(__file__).resolve().parents[1]
DEFAULT_PROFILE = Path.home() / ".dsh-full" / "profiles" / "dsh-tui" / "package.json"
IGNORED_PARTS = {".git", "node_modules", "__pycache__"}
# The lock is repo metadata, not a file the live bundle needs to mirror.
REPO_ONLY = {Path("profile.lock.json")}


class DriftError(Exception):
    """The lock, the live profile or the gate recipe cannot be used."""


def _read_json(path: Path) -> Any:
    try:
        return json.loads(path.read_text(encoding="utf-8"))
    except OSError as exc:
        raise DriftError(f"cannot read {path}: {exc}") from exc
    except json.JSONDecodeError as exc:
        raise DriftError(f"cannot parse {path}: {exc}") from exc


def _require(condition: object, message: str) -> None:
    if not condition:
        raise DriftError(message)


def load_lock(path: Path) -> dict[str, Any]:
    lock = _read_json(path)
    _require(isinstance(lock, dict), f"{path} is not a JSON object")
    for key in (
        "schema",
        "lock",
        "source",
        "engine",
        "engine_authority",
        "pnpm",
        "interactive",
        "headless",
    ):
        _require(key in lock, f"{path} has no {key}")
    _require(lock["schema"] == 1, f"{path} has an unknown schema {lock['schema']!r}")
    _require(isinstance(lock["lock"], str), "lock path must be a string")
    _require(not Path(lock["lock"]).is_absolute(), "lock path must be repo-relative")
    _require(".." not in Path(lock["lock"]).parts, "lock path escapes the repo")
    for variant in ("interactive", "headless"):
        _require(isinstance(lock[variant], dict), f"{variant} must be an object")
        _require(isinstance(lock[variant].get("bundles"), list), f"{variant} has no bundles")
    _require(
        isinstance(lock["interactive"].get("dependencies"), list),
        "interactive has no dependencies",
    )
    _require(
        isinstance(lock["headless"].get("github_plugin"), str),
        "headless has no github_plugin",
    )
    for variant, field in (("interactive", "dependencies"), ("headless", "bundles")):
        seen: set[str] = set()
        for entry in lock[variant][field]:
            _require(isinstance(entry, dict), f"{variant} entry is not an object")
            name = entry.get("package")
            spec = entry.get("spec")
            _require(isinstance(name, str) and name, f"{variant} entry has no package")
            _require(name not in seen, f"duplicate package {name}")
            seen.add(name)
            _require(isinstance(spec, str) and spec, f"{name} has no spec")
            if spec.startswith("link:"):
                link = entry.get("link")
                _require(isinstance(link, str) and link, f"{name} has no link path")
                _require(link == spec[5:], f"{name} link and spec disagree")
                _require(not Path(link).is_absolute(), f"{name} has an absolute link {link}")
                _require(".." not in Path(link).parts, f"{name} link escapes the repo: {link}")
                _require(isinstance(entry.get("version"), str), f"{name} link has no version")
    return lock


def load_profile(path: Path) -> dict[str, Any]:
    profile = _read_json(path)
    _require(isinstance(profile, dict), f"{path} is not a JSON object")
    _require(isinstance(profile.get("dependencies"), dict), f"{path} has no dependencies")
    return profile


def render_gate(lock: dict[str, Any]) -> dict[str, Any]:
    """Render the committed headless recipe from the lock."""
    bundles = []
    for entry in lock["headless"]["bundles"]:
        spec = entry["spec"]
        bundles.append(
            {"package": entry["package"], "spec": spec[5:] if spec.startswith("link:") else spec}
        )
    return {
        "lock": lock["lock"],
        "source": lock["source"],
        "engine": lock["engine"],
        "engine_authority": lock["engine_authority"],
        "pnpm": lock["pnpm"],
        "bundles": bundles,
        "github_plugin": lock["headless"]["github_plugin"],
    }


def _check_engine(
    profile: dict[str, Any], expected: str, errors: list[str], warnings: list[str]
) -> None:
    engines = profile.get("engines")
    declared = engines.get("dsh") if isinstance(engines, dict) else None
    if isinstance(declared, str) and declared:
        if declared != expected:
            errors.append(f"engine drift: profile declares {declared}, lock expects {expected}")
        return
    dsh = shutil.which("dsh")
    if dsh is None:
        warnings.append("engine is not declared and dsh is not on PATH; runtime check skipped")
        return
    result = subprocess.run([dsh, "--version"], capture_output=True, text=True, check=False)
    if result.returncode != 0 or expected not in result.stdout:
        errors.append(
            f"engine drift: dsh --version returned {result.stdout.strip()!r}, expected {expected}"
        )


def _package_manifest(target: Path) -> dict[str, Any]:
    path = target / "package.json"
    _require(path.is_file(), f"link target {target} has no package.json")
    data = _read_json(path)
    _require(isinstance(data, dict), f"{path} is not an object")
    return data


def _visible_files(target: Path) -> dict[Path, Path]:
    files: dict[Path, Path] = {}
    for path in target.rglob("*"):
        if not path.is_file():
            continue
        rel = path.relative_to(target)
        if any(part in IGNORED_PARTS for part in rel.parts):
            continue
        files[rel] = path
    return files


def _nested_link_paths(link: str, all_links: list[str]) -> set[Path]:
    prefix = link.rstrip("/") + "/"
    return {Path(other[len(prefix) :]) for other in all_links if other.startswith(prefix)}


def _check_link(
    name: str,
    entry: dict[str, Any],
    live_target: Path,
    root: Path,
    all_links: list[str],
    errors: list[str],
) -> None:
    link = entry["link"]
    version = entry["version"]
    repo_target = (root / link).resolve()
    _require(repo_target.is_dir(), f"{name}: repo link target {link} is missing")
    repo_manifest = _package_manifest(repo_target)
    if repo_manifest.get("name") != name:
        errors.append(f"{name}: repo target names {repo_manifest.get('name')!r}")
    if repo_manifest.get("version") != version:
        errors.append(
            f"{name}: repo target version {repo_manifest.get('version')!r}, lock expects {version}"
        )

    live_target = live_target.expanduser().resolve()
    if not live_target.is_dir():
        errors.append(f"{name}: live link target {live_target} is missing")
        return
    live_manifest = _package_manifest(live_target)
    if live_manifest.get("name") != name:
        errors.append(f"{name}: live target names {live_manifest.get('name')!r}")
    if live_manifest.get("version") != version:
        errors.append(
            f"{name}: live target version {live_manifest.get('version')!r}, lock expects {version}"
        )
    nested = _nested_link_paths(link, all_links)
    allowed = {Path(path) for path in entry.get("extra_live", [])}

    def is_nested(rel: Path) -> bool:
        return any(rel == item or item in rel.parents for item in nested)

    repo_files = _visible_files(repo_target)
    live_files = _visible_files(live_target)
    for rel, repo_file in sorted(repo_files.items()):
        if rel in REPO_ONLY or is_nested(rel):
            continue
        live_file = live_files.get(rel)
        if live_file is None:
            errors.append(f"{name}: live link is missing {rel}")
        elif live_file.read_bytes() != repo_file.read_bytes():
            errors.append(f"{name}: live link differs at {rel}")
    extras = sorted(rel for rel in set(live_files) - set(repo_files) if not is_nested(rel))

    def is_allowed(rel: Path) -> bool:
        return any(rel == item or item in rel.parents for item in allowed)

    undeclared = [rel for rel in extras if not is_allowed(rel)]
    if undeclared:
        named = ", ".join(str(path) for path in undeclared[:5])
        errors.append(f"{name}: {len(undeclared)} extra live file(s): {named}")


def _check_dependencies(
    lock: dict[str, Any],
    profile: dict[str, Any],
    profile_path: Path,
    root: Path,
    errors: list[str],
    warnings: list[str],
) -> None:
    expected = {entry["package"]: entry for entry in lock["interactive"]["dependencies"]}
    all_links = [entry["link"] for entry in expected.values() if entry.get("link")]
    live = profile["dependencies"]
    for name, entry in expected.items():
        if name not in live:
            errors.append(f"missing dependency {name}")
            continue
        spec = entry["spec"]
        live_spec = live[name]
        if spec.startswith("link:"):
            if not isinstance(live_spec, str) or not live_spec.startswith("link:"):
                errors.append(f"{name}: expected a link, got {live_spec!r}")
                continue
            live_target = Path(live_spec[5:]).expanduser()
            if not live_target.is_absolute():
                live_target = profile_path.parent / live_target
            _check_link(name, entry, live_target, root, all_links, errors)
        elif live_spec != spec:
            errors.append(f"{name}: expected {spec}, got {live_spec!r}")
    for name in sorted(set(live) - set(expected)):
        errors.append(f"unexpected dependency {name}")


def _check_bundles(lock: dict[str, Any], profile: dict[str, Any], errors: list[str]) -> None:
    dsh = profile.get("dsh")
    declared = dsh.get("profile", {}).get("bundles") if isinstance(dsh, dict) else None
    if declared != lock["interactive"]["bundles"]:
        errors.append(
            "profile dsh.profile.bundles differ from the lock: "
            f"expected {len(lock['interactive']['bundles'])}, got "
            f"{len(declared) if isinstance(declared, list) else 'none'}"
        )


def _check_gate(lock: dict[str, Any], gate_path: Path, errors: list[str]) -> None:
    committed = _read_json(gate_path)
    if committed != render_gate(lock):
        errors.append(
            f"gate profile {gate_path} differs from the lock; "
            f"run: {Path(__file__).as_posix()} --render-gate > {gate_path}"
        )


def _check_link_targets(lock: dict[str, Any], root: Path, errors: list[str]) -> None:
    for variant, field in (("interactive", "dependencies"), ("headless", "bundles")):
        for entry in lock[variant][field]:
            if not entry["spec"].startswith("link:"):
                continue
            target = (root / entry["link"]).resolve()
            if not target.is_dir():
                errors.append(f"{entry['package']}: repo link target {entry['link']} is missing")
                continue
            manifest = _package_manifest(target)
            if manifest.get("name") != entry["package"]:
                errors.append(f"{entry['package']}: repo target names {manifest.get('name')!r}")
            if manifest.get("version") != entry["version"]:
                errors.append(
                    f"{entry['package']}: repo target version "
                    f"{manifest.get('version')!r}, lock expects {entry['version']}"
                )


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--root", type=Path, default=ROOT)
    parser.add_argument("--lock", type=Path)
    parser.add_argument("--profile", type=Path, default=DEFAULT_PROFILE)
    parser.add_argument("--gate", type=Path)
    parser.add_argument("--render-gate", action="store_true")
    parser.add_argument("--json", action="store_true")
    args = parser.parse_args(argv)

    root = args.root.resolve()
    lock_path = args.lock or root / "config" / "dsh" / "profile.lock.json"
    gate_path = args.gate or root / ".github" / "gate" / "dsh-profile.json"

    try:
        lock = load_lock(lock_path)
        if args.render_gate:
            print(json.dumps(render_gate(lock), indent=2))
            return 0

        errors: list[str] = []
        warnings: list[str] = []
        _check_gate(lock, gate_path, errors)
        _check_link_targets(lock, root, errors)
        if args.profile.is_file():
            profile = load_profile(args.profile)
            _check_engine(profile, lock["engine"], errors, warnings)
            _check_dependencies(lock, profile, args.profile, root, errors, warnings)
            _check_bundles(lock, profile, errors)
        else:
            errors.append(f"live profile is missing: {args.profile}")

        result = {
            "ok": not errors,
            "engine": lock["engine"],
            "interactive_dependencies": len(lock["interactive"]["dependencies"]),
            "interactive_bundles": len(lock["interactive"]["bundles"]),
            "headless_bundles": len(lock["headless"]["bundles"]),
            "errors": errors,
            "warnings": warnings,
        }
        if args.json:
            print(json.dumps(result, indent=2))
        else:
            for warning in warnings:
                print(f"warning: {warning}", file=sys.stderr)
            for error in errors:
                print(f"error: {error}", file=sys.stderr)
            if errors:
                print("dsh profile: DRIFT", file=sys.stderr)
            else:
                print(
                    "dsh profile: OK "
                    f"({result['interactive_dependencies']} dependencies, "
                    f"{result['interactive_bundles']} interactive bundles, "
                    f"{result['headless_bundles']} headless bundles, "
                    f"engine {result['engine']})"
                )
        return 0 if not errors else 2
    except DriftError as exc:
        print(f"error: {exc}", file=sys.stderr)
        return 2


if __name__ == "__main__":
    raise SystemExit(main())
