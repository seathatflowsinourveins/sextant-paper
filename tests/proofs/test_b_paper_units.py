"""B-only extraction of Sextant #704 unit proofs; mixed IBKR source stays held.

Source: Sextant@33cdee7b65d53c93ab106b7b3196fc790b14a5a8,
tests/proofs/test_paper_units.py:34-138,207-214,280-330. Function bodies and
decorators are unchanged; only module setup selects the two imported B units.
Roster-ops/lead forward-port exception, 2026-11-11.
"""

from __future__ import annotations

import configparser
import re
import shlex
import shutil
import subprocess
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parents[2]
DEPLOY = ROOT / "deploy/paper"
B = "sextant-paper-b-stage1.service"
B_TEST_ENTRY = "sextant-paper-b-test-entry.service"
UNITS = (B, B_TEST_ENTRY)


def _unit(path: Path) -> configparser.ConfigParser:
    parsed = configparser.ConfigParser(interpolation=None)
    parsed.optionxform = str  # type: ignore[assignment,method-assign]
    assert parsed.read(path), f"missing unit: {path}"
    return parsed


def _install(tmp_path: Path, override: str | None = None):
    repo = tmp_path / "checkout"
    deploy = repo / "deploy/paper"
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
    done = subprocess.run(
        ["bash", str(deploy / "install.sh")], env=env, capture_output=True, text=True
    )
    return done, repo, config / "systemd/user", calls


@pytest.mark.parametrize("override", [None, "/work/checkout&literal|path"])
def test_installer_only_renders_and_reloads_never_starts_or_enables(tmp_path: Path, override):
    done, repo, units, calls = _install(tmp_path, override)
    assert done.returncode == 0, done.stderr
    assert sorted(p.name for p in units.iterdir()) == sorted(UNITS)
    assert calls.read_text().splitlines() == ["--user daemon-reload"]
    for name in UNITS:
        service = _unit(units / name)["Service"]
        assert service["WorkingDirectory"] == (override or str(repo))
        assert service["ExecStart"].startswith(
            f"/usr/bin/bash {override or repo}/scripts/with-keys.sh --strict "
        )
        assert service["EnvironmentFile"].startswith("%h/.config/sextant/paper/")
        (units / name).write_text("stale")
    again, *_ = _install(tmp_path, override)
    assert again.returncode == 0, again.stderr
    assert all("stale" not in (units / name).read_text() for name in UNITS)


@pytest.mark.parametrize(
    "override", ["/work/check out", "/work/100%h", "/work/$HOME", '/w/"q', "/w/'q", "/w/\\q"]
)
def test_installer_refuses_paths_systemd_would_split_or_expand(tmp_path: Path, override: str):
    done, _, units, calls = _install(tmp_path, override)
    assert done.returncode == 2
    assert not units.exists() and not calls.exists()


def _arguments(name: str, values: dict[str, str]) -> list[str]:
    command = shlex.split(_unit(DEPLOY / name)["Service"]["ExecStart"])
    # systemd.service(5): ${NAME} is one argument, including whitespace and literal dollars.
    args = command[command.index("-m") + 2 :]
    return [re.sub(r"\$\{([A-Z_]+)\}", lambda m: values.get(m[1], ""), arg) for arg in args]


def test_paper_installer_is_shellcheck_clean():
    assert (DEPLOY / "install.sh").is_file()
    if not shutil.which("shellcheck"):
        pytest.skip("shellcheck unavailable")
    done = subprocess.run(
        ["shellcheck", str(DEPLOY / "install.sh")], capture_output=True, text=True
    )
    assert done.returncode == 0, done.stdout + done.stderr


def test_b_test_entry_unit_only_adds_the_fixed_instrument_opt_in():
    stage1 = (DEPLOY / B).read_text()
    attended = DEPLOY / B_TEST_ENTRY
    assert attended.is_file(), "missing separate attended test-entry unit"
    assert attended.read_text() == stage1.replace(
        "Description=Sextant Paper B attended stage 1 (protection only)",
        "Description=Sextant Paper B attended test entry",
    ).replace("paper-b-stage1.params", "paper-b-test-entry.params").replace(
        "--lease-source ${B_LEASE_SOURCE}\n",
        "--lease-source ${B_LEASE_SOURCE} --attended-test-entry ${B_ATTENDED_TEST_INSTRUMENT}\n",
    )
    command = shlex.split(_unit(attended)["Service"]["ExecStart"])
    assert command[-2:] == ["--attended-test-entry", "${B_ATTENDED_TEST_INSTRUMENT}"]
    assert command.count("--attended-test-entry") == 1
    assert "-c" not in command
    assert not any("attended-test" in arg for arg in _arguments(B, {}))
    # Every expansion remains exactly one argument; no unbraced expansion/splice.
    for arg in command:
        if "$" in arg:
            assert re.fullmatch(r"\$\{[A-Z_]+\}", arg)


def test_b_test_entry_parameters_are_separate_complete_and_blank():
    example = DEPLOY / "paper-b-test-entry.params.example"
    assert example.is_file(), "missing attended instrument parameter example"

    def assignments(path):
        return dict(
            line.split("=", 1)
            for line in path.read_text().splitlines()
            if line and not line.startswith("#")
        )

    stage1 = assignments(DEPLOY / "paper-b-stage1.params.example")
    assert assignments(example) == stage1 | {"B_ATTENDED_TEST_INSTRUMENT": ""}
    service = _unit(DEPLOY / B_TEST_ENTRY)["Service"]
    assert service["EnvironmentFile"] == ("%h/.config/sextant/paper/paper-b-test-entry.params")
    assert "B_ATTENDED_TEST_INSTRUMENT" not in stage1


def test_b_test_entry_instrument_is_one_literal_argument_even_when_missing():
    # T1 owns node validation; this proof covers systemd's single-token boundary only.
    for instrument in ("", "fixture with spaces $literal --not-an-option"):
        args = _arguments(B_TEST_ENTRY, {"B_ATTENDED_TEST_INSTRUMENT": instrument})
        assert args[-2:] == ["--attended-test-entry", instrument]
        assert len(args) == len(_arguments(B, {})) + 2


def test_paper_installer_has_no_start_or_enable_command():
    installer = (DEPLOY / "install.sh").read_text()
    assert not re.search(r"systemctl[^\n]*(?:\bstart\b|\benable\b)", installer)
