"""B-only port of Sextant@9f346445 Stage 2 deployment proofs.

Preserve runnable B proof bodies; exclude IBKR-only units, keys and assertions.
Repeated starts execute the real wrapper/CLI with fixture credential adapters only.
"""

from __future__ import annotations

import configparser
import json
import re
import shlex
import shutil
import subprocess
import sys
from contextlib import ExitStack
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parents[2]
DEPLOY = ROOT / "deploy/paper"
B = "sextant-paper-b-stage1.service"
B_TEST_ENTRY = "sextant-paper-b-test-entry.service"
UNITS = (B, B_TEST_ENTRY)
KEYS = {
    B: ["ALPACA_PAPER_2_API_KEY", "ALPACA_PAPER_2_SECRET_KEY"],
}
KEYS[B_TEST_ENTRY] = KEYS[B]
B_STAGE2 = "sextant-paper-b-stage2.service"
ALL_UNITS = (*UNITS, B_STAGE2)


def _unit(path: Path) -> configparser.ConfigParser:
    parsed = configparser.ConfigParser(interpolation=None)
    parsed.optionxform = str  # type: ignore[assignment,method-assign]
    assert parsed.read(path), f"missing unit: {path}"
    return parsed


@pytest.mark.parametrize("name", UNITS)
def test_attended_units_never_restart_or_enable_and_inject_only_named_keys(name: str) -> None:
    unit = _unit(DEPLOY / name)
    service = unit["Service"]
    command = shlex.split(service["ExecStart"])
    prefix = ["/usr/bin/bash", "%h/sextant/scripts/with-keys.sh", "--strict"]
    for key in KEYS[name]:
        prefix += ["-k", key]
    assert command[: len(prefix) + 1] == [*prefix, "--"]
    assert command.count("-k") == len(KEYS[name])
    assert command[len(prefix) + 1 : len(prefix) + 6] == [
        "%h/.local/bin/uv",
        "run",
        "--frozen",
        "python",
        "-m",
    ]
    assert service["Type"] == "simple" and service["Restart"] == "no"
    assert "Install" not in unit
    assert set(service) == {
        "Type",
        "WorkingDirectory",
        "Environment",
        "EnvironmentFile",
        "ExecStart",
        "Restart",
    }
    assert service["WorkingDirectory"] == "%h/sextant"
    assert service["Environment"] == (
        "PATH=%h/.local/bin:/usr/local/bin:/usr/bin:/bin PYTHONUNBUFFERED=1"
    )
    parameter_file = service["EnvironmentFile"]
    assert parameter_file == f"%h/.config/sextant/paper/{name[8:-8]}.params"
    assert not parameter_file.startswith("-") and ".env" not in parameter_file
    example = DEPLOY / (Path(parameter_file).name + ".example")
    assert example.is_file()
    # No identity/credential values or free-form argument lists in shipped parameters.
    assignments = [
        line.split("=", 1)
        for line in example.read_text().splitlines()
        if line and not line.startswith("#")
    ]
    assert assignments and all(value == "" for _, value in assignments)
    assert not set(KEYS[name]) & {key for key, _ in assignments}
    used = set(re.findall(r"\$\{([A-Z_]+)\}", service["ExecStart"]))
    assert used <= {key for key, _ in assignments}


def _install(tmp_path: Path, override: str | None = None):
    repo = tmp_path / "checkout"
    deploy = repo / "deploy/paper"
    deploy.mkdir(parents=True, exist_ok=True)
    for name in ("install.sh", *ALL_UNITS):
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
    assert sorted(p.name for p in units.iterdir()) == sorted(ALL_UNITS)
    assert calls.read_text().splitlines() == ["--user daemon-reload"]
    for name in ALL_UNITS:
        service = _unit(units / name)["Service"]
        assert service["WorkingDirectory"] == (override or str(repo))
        assert service["ExecStart"].startswith(
            f"/usr/bin/bash {override or repo}/scripts/with-keys.sh --strict "
        )
        assert service["EnvironmentFile"].startswith("%h/.config/sextant/paper/")
        (units / name).write_text("stale")
    again, *_ = _install(tmp_path, override)
    assert again.returncode == 0, again.stderr
    assert all("stale" not in (units / name).read_text() for name in ALL_UNITS)


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


@pytest.mark.parametrize("name", [B, B_STAGE2])
def test_b_command_uses_current_parser_and_keeps_paths_and_source_as_single_arguments(
    tmp_path, name
):
    from sextant.paper import node_order

    account = tmp_path / "fixture account.json"
    account.write_text(
        json.dumps(
            dict(
                lane="alpaca-paper-b",
                account_id="FIXTUREACCOUNT",
                client_id="ALPACA",
                namespace="BTEST",
                sessions=["rth"],
            )
        )
    )
    values = dict(
        B_ACCOUNT_CONFIG=str(account),
        B_CLAIM_LIST=str(tmp_path / "claim list.json"),
        B_RECEIPT_DIR=str(tmp_path / "receipt dir"),
        B_DURATION_S="7",
        B_WRITER_LEASE_ROOT=str(tmp_path / "writer locks"),
        B_STREAM_LEASE_ROOT=str(tmp_path / "producer leases"),
        B_EXISTING_STREAMS="0",
        B_LEASE_SOURCE="fixture only: no streams; $literal --not-an-option",
    )
    parsed = node_order.parse(_arguments(name, values))
    assert parsed.duration_s == 7 and parsed.existing_streams == 0
    assert parsed.universe_artifact == tmp_path / "claim list.json"
    assert parsed.receipt_dir == tmp_path / "receipt dir"
    assert parsed.writer_lease_root == tmp_path / "writer locks"
    assert parsed.stream_lease_root == tmp_path / "producer leases"
    assert parsed.lease_source == values["B_LEASE_SOURCE"]
    assert [parsed.api_key_env, parsed.api_secret_env] == KEYS[B]
    assert parsed.account.lane == "alpaca-paper-b"
    with pytest.raises(SystemExit):
        node_order.parse(_arguments(name, values | {"B_DURATION_S": ""}))


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


# Stage 2 changes supervision only: #661's strict wrapper, fixed flags and guards stay shared.
def test_stage2_reuses_the_attended_start_path_with_bounded_failure_restarts():
    attended = _unit(DEPLOY / B)
    unit = _unit(DEPLOY / B_STAGE2)
    service = unit["Service"]
    assert "Install" not in unit
    assert service["ExecStart"] == attended["Service"]["ExecStart"]
    expected = dict(attended["Service"]) | {
        "EnvironmentFile": "%h/.config/sextant/paper/paper-b-stage2.params",
        "Restart": "on-failure",
        "RestartSec": "100ms",
        "RestartPreventExitStatus": "2",
    }
    # No second command, force-restart status, alternate entry point or argument string.
    assert dict(service) == expected
    assert dict(unit["Unit"]) == dict(attended["Unit"]) | {
        "Description": "Sextant Paper B unattended stage 2 (protection only)",
        "StartLimitIntervalSec": "10s",
        "StartLimitBurst": "5",
    }
    template = DEPLOY / "paper-b-stage2.params.example"
    assignments = {
        k: v
        for line in template.read_text().splitlines()
        if line and not line.startswith("#")
        for k, v in [line.split("=", 1)]
    }
    attended_assignments = {
        k: v
        for line in (DEPLOY / "paper-b-stage1.params.example").read_text().splitlines()
        if line and not line.startswith("#")
        for k, v in [line.split("=", 1)]
    }
    assert assignments == attended_assignments
    assert all(value == "" for value in assignments.values())


def test_stage2_restart_settings_follow_sourced_uncalibrated_registry_rows():
    unit = _unit(DEPLOY / B_STAGE2)
    rows = json.loads((DEPLOY / "parameters.json").read_text())["rows"]
    locations = {
        "Restart": "Service",
        "RestartSec": "Service",
        "RestartPreventExitStatus": "Service",
        "StartLimitIntervalSec": "Unit",
        "StartLimitBurst": "Unit",
    }
    assert set(rows) == set(locations)
    for name, section in locations.items():
        row = rows[name]
        assert unit[section][name] == str(row["value"])
        assert row["status"] == "UNCALIBRATED" and row["unit"] and row["source"]
        assert row["read_date"] == "2026-11-11"


@pytest.mark.parametrize("holders", [("writer", "stream"), ("stream", "writer")])
def test_stage2_repeated_wrapper_starts_recheck_both_incumbent_leases(tmp_path, holders):
    """A restart bypassing either admission gate would connect instead of refusing here.

    Run the real strict wrapper and real node CLI twice. Replace only encrypted credential
    injection and uv with local adapters: no real credential store, client or service runs.
    The two attempts face DIFFERENT incumbents, so a saved admission cannot satisfy both.
    """
    from sextant.execution.custody import AccountScope, WriterLease
    from sextant.tradingview.producer import reserve_stream

    command = shlex.split(_unit(DEPLOY / B_STAGE2)["Service"]["ExecStart"])
    account = tmp_path / "fixture account.json"
    account.write_text(
        json.dumps(
            dict(
                lane="alpaca-paper-b",
                account_id="FIXTUREACCOUNT",
                client_id="ALPACA",
                namespace="BTEST",
                sessions=["rth"],
            )
        )
    )
    scripts = tmp_path / "scripts"
    scripts.mkdir()
    shutil.copy2(ROOT / "scripts/with-keys.sh", scripts / "with-keys.sh")
    (scripts / "key-systemd.sh").write_text(
        "ns_systemd_init() { return 0; }\n"
        "ns_systemd_inject() {\n"
        '  [[ "$strict" == 1 && "${#keys[@]}" == 2 && '
        '"${keys[0]}" == ALPACA_PAPER_2_API_KEY && '
        '"${keys[1]}" == ALPACA_PAPER_2_SECRET_KEY ]] || return 97\n'
        '  "$@"\n}\n'
    )
    uv = tmp_path / "uv"
    uv.write_text("#!/bin/sh\nshift 3\nexec " + shlex.quote(sys.executable) + ' "$@"\n')
    uv.chmod(0o755)
    values = dict(
        B_ACCOUNT_CONFIG=str(account),
        B_CLAIM_LIST=str(tmp_path / "unused universe.json"),
        B_RECEIPT_DIR=str(tmp_path / "receipt dir"),
        B_DURATION_S="7",
        B_WRITER_LEASE_ROOT=str(tmp_path / "incumbent locks"),
        B_STREAM_LEASE_ROOT=str(tmp_path / "producer leases"),
        B_EXISTING_STREAMS="0",
        B_LEASE_SOURCE="fixture only; no stream exists",
    )
    command = [
        str(scripts / "with-keys.sh")
        if word == "%h/sextant/scripts/with-keys.sh"
        else str(uv)
        if word == "%h/.local/bin/uv"
        else re.sub(r"\$\{([A-Z_]+)\}", lambda m: values[m[1]], word)
        for word in command
    ]
    # Explicit fixture environment: never inherit credentials or the operator's Bash hooks.
    env = {
        "HOME": str(tmp_path),
        "PATH": "/usr/bin:/bin",
        "PYTHONPATH": str(ROOT / "src"),
        "B_EXPECTED_ACCOUNT_SUFFIX": "ACCOUNT",
    }
    receipt = Path(values["B_RECEIPT_DIR"]) / "order-mode-receipt.json"
    for holder in holders:
        with ExitStack() as held:
            if holder == "writer":
                lease = WriterLease(
                    AccountScope("ALPACA", "FIXTUREACCOUNT"),
                    Path(values["B_WRITER_LEASE_ROOT"]),
                    "incumbent",
                )
                lease.acquire()
                held.callback(lease.close)
            else:
                held.enter_context(
                    reserve_stream(
                        Path(values["B_STREAM_LEASE_ROOT"]),
                        paper_slot=2,
                        existing_streams=0,
                        source="fixture producer; no stream",
                    )
                )
            done = subprocess.run(
                command, cwd=ROOT, env=env, capture_output=True, text=True, timeout=30
            )
        assert done.returncode == 2, done.stdout + done.stderr
        body = json.loads(receipt.read_text())
        reason = "writer_lease" if holder == "writer" else "market_data_lease"
        assert (body["phase"], body["reason"]) == ("refused", reason)
        assert body["lease"]["writer_root"] == values["B_WRITER_LEASE_ROOT"]
        assert body["lease"]["stream_root"] == values["B_STREAM_LEASE_ROOT"]
