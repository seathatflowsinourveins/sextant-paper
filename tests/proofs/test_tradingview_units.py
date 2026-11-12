"""The TradingView user units: rendered offline with a fake user manager, never started.

Adapted from tests/proofs/test_calibration_job.py and test_calibration_install.py: the
installer runs only with fixture HOME/XDG and a fake ``systemctl``; the rendered command
lines are parsed by the modules' own ``main`` with their run functions replaced, so a
renamed option breaks this proof and no producer, sync, key or endpoint is touched.
"""

from __future__ import annotations

import configparser
import json
import re
import shlex
import shutil
import subprocess
from pathlib import Path
from typing import Any

import pytest

ROOT = Path(__file__).resolve().parents[2]
DEPLOY = ROOT / "deploy/tradingview"
PRODUCER = "sextant-tradingview-prestage.service"
SYNC = "sextant-tradingview-sync.service"
TIMER = "sextant-tradingview-sync.timer"
UNITS = (PRODUCER, SYNC, TIMER)
CADENCE = ROOT / "lanes/tradingview/j5-cadence.params.json"
PRODUCER_ONLY = ["--producer-memory-max", "3G"]
WITH_SYNC = [*PRODUCER_ONLY, "--with-sync", "--sync-memory-max", "1G"]
DATA = "%h/sextant-data/tradingview-prestage"
UNIT_DIR = f"{DATA}/unit"


def _unit(path: Path) -> configparser.ConfigParser:
    parsed = configparser.ConfigParser(interpolation=None)
    parsed.optionxform = str  # type: ignore[assignment,method-assign]
    assert parsed.read(path), f"missing unit: {path}"
    return parsed


def _install(
    tmp_path: Path, args: list[str] | None = None, override: str | None = None
) -> tuple[subprocess.CompletedProcess[str], Path, Path, Path]:
    repo = tmp_path / "checkout"
    deploy = repo / "deploy/tradingview"
    deploy.mkdir(parents=True, exist_ok=True)
    for name in ("install.sh", *UNITS):
        assert (DEPLOY / name).is_file(), f"missing installation artifact: {name}"
        shutil.copy2(DEPLOY / name, deploy / name)
    fake_bin = tmp_path / "bin"
    fake_bin.mkdir(exist_ok=True)
    calls = tmp_path / "calls"
    fake = fake_bin / "systemctl"
    fake.write_text('#!/bin/sh\nprintf "%s\\n" "$*" >> "$CALLS"\n')
    fake.chmod(0o755)
    config = tmp_path / "config"
    env = {
        "HOME": str(tmp_path / "home"),
        "XDG_CONFIG_HOME": str(config),
        "PATH": f"{fake_bin}:/usr/bin:/bin",
        "CALLS": str(calls),
    }
    if override is not None:
        env["SEXTANT_REPO"] = override
    argv = args if args is not None else WITH_SYNC
    done = subprocess.run(
        ["bash", str(deploy / "install.sh"), *argv], env=env, capture_output=True, text=True
    )
    return done, repo, config / "systemd/user", calls


def _command(path: Path) -> list[str]:
    return shlex.split(_unit(path)["Service"]["ExecStart"])


def _module_args(command: list[str], module: str) -> list[str]:
    at = command.index("-m")
    # systemd resolves a bare name only on its compiled search path, so uv is absolute.
    assert command[at - 4 : at] == ["%h/.local/bin/uv", "run", "--frozen", "python"]
    assert command[at + 1] == module
    return command[at + 2 :]


def test_producer_unit_names_only_the_two_slot_2_keys_through_with_keys() -> None:
    from sextant.tradingview.prestage import KEY_NAMES

    unit = _unit(DEPLOY / PRODUCER)
    command = _command(DEPLOY / PRODUCER)
    assert command[:8] == [
        "/usr/bin/bash",
        "%h/sextant/scripts/with-keys.sh",
        "--strict",
        "-k",
        KEY_NAMES[0],
        "-k",
        KEY_NAMES[1],
        "--",
    ]
    assert KEY_NAMES == ("ALPACA_PAPER_2_API_KEY", "ALPACA_PAPER_2_SECRET_KEY")
    assert command.count("-k") == 2
    text = (DEPLOY / PRODUCER).read_text()
    names = set(re.findall(r"\b[A-Z][A-Z0-9_]*(?:KEY|SECRET|TOKEN)[A-Z0-9_]*\b", text))
    assert names == set(KEY_NAMES)
    assert "EnvironmentFile" not in unit["Service"]
    assert "LoadCredential" not in unit["Service"]
    assert "SetCredential" not in unit["Service"]


@pytest.mark.parametrize("name", [PRODUCER, SYNC, TIMER])
def test_no_unit_carries_a_token_code_or_callback_address(name: str) -> None:
    text = (DEPLOY / name).read_text()
    body = "\n".join(line for line in text.splitlines() if not line.startswith("Documentation="))
    for forbidden in ("127.0.0.1", "localhost", "http://", "https://", "callback", "redirect"):
        assert forbidden not in body.lower()
    assert not re.search(r"\b(code|state|token|oauth|bearer|refresh)\b", body, re.IGNORECASE)
    assert "--forget-login" not in body and "--authorize" not in body
    environment = [line for line in body.splitlines() if line.startswith("Environment=")]
    assert len(environment) <= 1
    names = {item.split("=", 1)[0] for line in environment for item in shlex.split(line[12:])}
    assert names <= {"PATH", "PYTHONUNBUFFERED"}, names


def test_sync_unit_uses_the_stored_sign_in_and_never_a_browser() -> None:
    command = _command(DEPLOY / SYNC)
    assert "with-keys" not in " ".join(command)
    assert "-k" not in command
    assert command[:2] == ["%h/.local/bin/uv", "run"]
    args = _module_args(command, "sextant.tradingview")
    assert "--loop" in args and "--keep-login" in args
    assert "--authorize" not in args


def test_bounds_come_from_the_registry_and_memory_limits_have_no_default() -> None:
    from sextant.tradingview.prestage import DEFAULT_DISPLAY_BOUNDS

    producer = _unit(DEPLOY / PRODUCER)["Service"]
    sync = _unit(DEPLOY / SYNC)["Service"]
    command = _command(DEPLOY / PRODUCER)
    # No flag overrides the sourced display-bounds rows the module reads by default.
    for flag in ("--gap-bound", "--rvol-bound", "--bounds-max-age-s", "--display-bounds"):
        assert flag not in command
    rows = json.loads(DEFAULT_DISPLAY_BOUNDS.read_text())["rows"]
    assert {row["name"] for row in rows} == {"gap_bound", "rvol_bound", "max_age_s"}
    assert all(row["source"].strip() for row in rows)
    assert producer["MemoryMax"] == "@PRODUCER_MEMORY_MAX@"
    assert sync["MemoryMax"] == "@SYNC_MEMORY_MAX@"
    assert producer["MemorySwapMax"] == "0" and sync["MemorySwapMax"] == "0"


def test_restart_policy_follows_each_module() -> None:
    producer = _unit(DEPLOY / PRODUCER)
    sync = _unit(DEPLOY / SYNC)
    # The producer sleeps across closed windows itself; only a crash or refusal ends it.
    assert producer["Service"]["Type"] == "simple"
    assert producer["Service"]["Restart"] == "on-failure"
    assert producer["Install"]["WantedBy"] == "default.target"
    # The loop exits 0 at the display close (never restarted by on-failure); a latched run is
    # refused again before any session opens, and the stored sign-in needs no browser.
    assert sync["Service"]["Type"] == "simple"
    assert sync["Service"]["Restart"] == "on-failure"
    assert "Install" not in sync
    for unit in (producer, sync):
        assert unit["Service"]["WorkingDirectory"] == "%h/sextant"
        assert unit["Service"]["Environment"].startswith("PATH=%h/.local/bin:")


def test_sync_timer_starts_at_the_registered_display_open() -> None:
    timer = _unit(DEPLOY / TIMER)
    expression = timer["Timer"]["OnCalendar"]
    rows = {row["name"]: row for row in json.loads(CADENCE.read_text())["rows"]}
    start = rows["display_start_minute_et"]
    assert start["source"].strip()
    hour, minute = divmod(start["value"], 60)
    assert expression == f"Mon..Fri *-*-* {hour:02d}:{minute:02d}:00 America/New_York"
    assert timer["Timer"]["Unit"] == SYNC
    assert timer["Timer"]["Persistent"] == "true"
    assert timer["Install"]["WantedBy"] == "timers.target"
    if shutil.which("systemd-analyze"):
        result = subprocess.run(["systemd-analyze", "calendar", expression], capture_output=True)
        assert result.returncode == 0, result.stderr


@pytest.mark.parametrize("override", [None, "/work/checkout&literal|path"])
def test_default_install_writes_and_enables_the_producer_alone(tmp_path: Path, override) -> None:
    done, repo, units, calls = _install(tmp_path, args=PRODUCER_ONLY, override=override)
    assert done.returncode == 0, done.stderr
    assert sorted(path.name for path in units.iterdir()) == [PRODUCER]
    assert not (units / SYNC).exists() and not (units / TIMER).exists()
    producer = _unit(units / PRODUCER)["Service"]
    checkout = override or str(repo)
    assert producer["WorkingDirectory"] == checkout
    assert producer["ExecStart"].startswith(f"/usr/bin/bash {checkout}/scripts/with-keys.sh ")
    assert "%h/sextant/" not in producer["ExecStart"] and "@" not in producer["ExecStart"]
    assert f"--ranked-view-dir {DATA}/ranked-view" in producer["ExecStart"]
    assert producer["MemoryMax"] == "3G"
    lines = calls.read_text().splitlines()
    assert lines[:2] == ["--user daemon-reload", f"--user enable --now {PRODUCER}"]
    assert not any("sync" in line for line in lines)
    (units / PRODUCER).write_text("stale")
    again, *_ = _install(tmp_path, args=PRODUCER_ONLY, override=override)
    assert again.returncode == 0, again.stderr
    assert "stale" not in (units / PRODUCER).read_text()


@pytest.mark.parametrize("override", [None, "/work/checkout&literal|path"])
def test_with_sync_writes_the_sync_units_without_enabling_them(tmp_path: Path, override) -> None:
    done, repo, units, calls = _install(tmp_path, args=WITH_SYNC, override=override)
    assert done.returncode == 0, done.stderr
    checkout = override or str(repo)
    producer = _unit(units / PRODUCER)["Service"]
    sync = _unit(units / SYNC)["Service"]
    for service in (producer, sync):
        assert service["WorkingDirectory"] == checkout
        assert "%h/sextant/" not in service["ExecStart"]
        assert "@" not in service["ExecStart"] and "@" not in service["MemoryMax"]
    assert producer["ExecStart"].startswith(f"/usr/bin/bash {checkout}/scripts/with-keys.sh ")
    assert f"--research-dir {UNIT_DIR}" in producer["ExecStart"]
    assert f"--research-dir {UNIT_DIR}" in sync["ExecStart"]
    assert producer["MemoryMax"] == "3G" and sync["MemoryMax"] == "1G"
    assert (units / TIMER).read_bytes() == (DEPLOY / TIMER).read_bytes()
    # The producer alone is enabled; the sync stays the lead's own later step.
    lines = calls.read_text().splitlines()
    assert lines[:2] == ["--user daemon-reload", f"--user enable --now {PRODUCER}"]
    assert not any("sync" in line for line in lines)


@pytest.mark.parametrize(
    "args",
    [
        [],
        ["--producer-memory-max"],
        ["--producer-memory-max", "infinity"],
        ["--producer-memory-max", "3G --once"],
        ["--producer-memory-max", "3G\nExecStartPre=/bin/true"],
        [*PRODUCER_ONLY, "--with-sync"],
        [*PRODUCER_ONLY, "--sync-memory-max", "1G"],
        [*PRODUCER_ONLY, "--with-sync", "--sync-memory-max", "0"],
        [*PRODUCER_ONLY, "--gap-bound", "0.05"],
        [*PRODUCER_ONLY, "--unknown", "1"],
    ],
)
def test_installer_refuses_missing_or_malformed_values(tmp_path: Path, args) -> None:
    done, _, units, calls = _install(tmp_path, args=args)
    assert done.returncode != 0
    assert not units.exists() or not any(units.iterdir())
    assert not calls.exists()


@pytest.mark.parametrize("override", ["/work/check out", "/work/100%h", "/work/$HOME", '/w/"q'])
def test_installer_refuses_a_checkout_systemd_would_split_or_expand(tmp_path: Path, override):
    done, _, units, calls = _install(tmp_path, override=override)
    assert done.returncode != 0
    assert not units.exists() or not any(units.iterdir())
    assert not calls.exists()


def test_rendered_producer_command_parses_with_the_producer_parser(
    tmp_path: Path, monkeypatch: Any
) -> None:
    from sextant.tradingview import prestage

    done, _, units, _ = _install(tmp_path, args=PRODUCER_ONLY)
    assert done.returncode == 0, done.stderr
    args = _module_args(_command(units / PRODUCER), "sextant.tradingview.prestage")
    seen: dict[str, Any] = {}
    monkeypatch.setattr(prestage, "run", lambda **kwargs: seen.update(kwargs) or 0)
    assert prestage.main(args) == 0
    assert seen["research_dir"] == Path(UNIT_DIR)
    assert seen["nasdaq_listed"] == Path(f"{DATA}/nasdaqlisted.txt")
    assert seen["other_listed"] == Path(f"{DATA}/otherlisted.txt")
    assert seen["symbol_map_path"] == Path(f"{DATA}/symbol_map.json")
    assert seen["ranked_view_dir"] == Path(f"{DATA}/ranked-view")
    assert seen["bounds"] == prestage.load_display_bounds(prestage.DEFAULT_DISPLAY_BOUNDS)
    assert seen["k2_registry"] == prestage.DEFAULT_K2_REGISTRY
    assert seen["cadence_registry"] == prestage.DEFAULT_CADENCE_REGISTRY
    assert seen["state_root"] == prestage.DEFAULT_STATE_ROOT
    assert seen["once"] is False


def test_rendered_sync_command_parses_with_the_sync_parser(
    tmp_path: Path, monkeypatch: Any
) -> None:
    import sextant.tradingview.__main__ as cli
    from sextant.tradingview import loop

    done, _, units, _ = _install(tmp_path, args=WITH_SYNC)
    assert done.returncode == 0, done.stderr
    args = _module_args(_command(units / SYNC), "sextant.tradingview")
    seen: dict[str, Any] = {}

    class _NoStore:
        """Never touches the key store: the proof reads no stored sign-in."""

        def __init__(self, *args: Any, **kwargs: Any) -> None:
            pass

        def present(self) -> bool:
            return True

    monkeypatch.setenv("HOME", str(tmp_path / "home"))
    monkeypatch.setattr(cli, "StoredLogin", _NoStore, raising=False)
    monkeypatch.setattr(cli, "run_loop", lambda **kwargs: seen.update(kwargs) or 0)
    try:
        code = cli.main(args)
    except SystemExit as stop:  # argparse refuses an option the parser does not define
        code = f"argparse exit {stop.code}"
    if not seen:
        pytest.fail(
            "the sync unit's `--loop --keep-login` is not accepted by "
            f"`python -m sextant.tradingview` on this tree ({code}); this PR lands after "
            "the stored-login loop PR"
        )
    assert code == 0
    assert seen["research_dir"] == Path(UNIT_DIR)
    assert not seen["authorize"]  # no browser consent from the service
    assert seen.get("keep_login") is True or seen.get("login") is not None
    assert seen["registry"] == loop.DEFAULT_REGISTRY
    assert seen["state_root"] == loop.DEFAULT_STATE_ROOT
    assert seen["usage_source"] == loop.NO_CLAUDE_USAGE
    # The landed SHA is the checkout's own HEAD and the enable source the registry row's.
    assert seen["landed_sha"] is None and seen["enable_source"] is None


def test_installer_is_shellcheck_clean() -> None:
    if shutil.which("shellcheck") is None:
        pytest.skip("shellcheck is not on PATH")
    result = subprocess.run(
        ["shellcheck", str(DEPLOY / "install.sh")], capture_output=True, text=True
    )
    assert result.returncode == 0, result.stdout
