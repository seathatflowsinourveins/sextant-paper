"""Public gate invariants; GitHub workflow_run/REST and upstream composite contracts.

Sources: .github/gate/upstreams.json and google/eng-practices@3bb3ec25:
review/reviewer/standard.md:31-34,72,88-90; review/developer/small-cls.md:175.
"""

import fnmatch
import hashlib
import json
import re
import shutil
import subprocess
from pathlib import Path

import yaml

ROOT = Path(__file__).parents[2]
WORKFLOWS = ROOT / ".github/workflows"


def _gate() -> dict:
    return yaml.safe_load((WORKFLOWS / "gate.yml").read_text())


def test_no_workflow_requests_a_local_runner_or_privileged_pr_trigger() -> None:
    for path in (*WORKFLOWS.glob("*.yml"), *WORKFLOWS.glob("*.yaml")):
        workflow = yaml.safe_load(path.read_text())
        events = workflow.get("on", workflow.get(True))
        assert "pull_request_target" not in events, path
        for job in workflow["jobs"].values():
            assert job["runs-on"] == "ubuntu-24.04", path


def test_gate_runs_only_after_ci_from_the_default_branch() -> None:
    gate = _gate()
    assert gate.get("on", gate.get(True)) == {
        "workflow_run": {"workflows": ["ci"], "types": ["completed"]}
    }
    for job in gate["jobs"].values():
        for step in job["steps"]:
            if step.get("uses", "").startswith("actions/checkout@"):
                assert step["with"] == {
                    "ref": "${{ github.sha }}",
                    "persist-credentials": False,
                }


def test_danger_reads_api_data_and_never_checks_out_the_head() -> None:
    source = _gate()["jobs"]["source"]
    danger = next(step for step in source["steps"] if step.get("id") == "danger")
    assert "--dangerfile dangerfile.js --failOnErrors" in danger["run"]
    assert danger["env"]["DANGER_EVENT_PATH"] == "${{ steps.prepare.outputs.danger_event }}"
    assert danger["run"].startswith('GITHUB_EVENT_PATH="$DANGER_EVENT_PATH" ')
    assert not any("run" in step and "checkout" in step["run"] for step in source["steps"])


def test_dangers_native_provider_accepts_the_api_only_event(tmp_path: Path) -> None:
    """Danger 14.1.0 GitHubActions constructor/useEventDSL, without network calls."""
    executable = shutil.which("danger")
    assert executable is not None
    event = tmp_path / "danger-event.json"
    event.write_text(
        json.dumps({"pull_request": {"number": 7, "base": {"repo": {"full_name": "owner/repo"}}}})
    )
    done = subprocess.run(
        [
            "node",
            "-e",
            "const {createRequire}=require('node:module');"
            "const load=createRequire(process.argv[1]);"
            "const {GitHubActions}=load('danger/distribution/ci_source/providers/GitHubActions');"
            "const source=new GitHubActions({GITHUB_EVENT_PATH:process.argv[2],"
            "GITHUB_WORKFLOW:'gate'});"
            "process.stdout.write(JSON.stringify([source.useEventDSL,"
            "source.pullRequestID,source.repoSlug]));",
            str(Path(executable).resolve()),
            str(event),
        ],
        text=True,
        capture_output=True,
        timeout=30,
    )
    assert done.returncode == 0, done.stderr
    assert json.loads(done.stdout) == [False, 7, "owner/repo"]


def test_every_action_in_workflows_and_composites_has_a_full_pin() -> None:
    paths = [*WORKFLOWS.glob("*.yml"), *WORKFLOWS.glob("*.yaml")]
    paths += list((ROOT / ".github/actions").rglob("action.y*ml"))
    for path in paths:
        data = yaml.safe_load(path.read_text())
        groups = [data["runs"]] if "runs" in data else data["jobs"].values()
        for group in groups:
            for step in group.get("steps", []):
                action = step.get("uses", "")
                if action and not action.startswith("./"):
                    assert re.fullmatch(r"[\w./-]+@[0-9a-f]{40}", action), (path, action)


def test_ci_runs_the_complete_check_without_repository_secrets() -> None:
    text = (WORKFLOWS / "ci.yml").read_text()
    ci = yaml.safe_load(text)
    assert set(ci.get("on", ci.get(True))) == {"pull_request"}
    assert ci["permissions"] == {"contents": "read"}
    runs = " ".join(
        str(step.get("run", "")) for job in ci["jobs"].values() for step in job["steps"]
    )
    for part in ("just lint", "just test tests/proofs", "just test tests/repo", "just secrets"):
        assert part in runs, part
    assert "secrets." not in text
    assert "enable-cache: true" in text and "package-manager-cache: false" in text


def test_fork_pr_workflows_never_bind_repository_secrets() -> None:
    """GitHub events-that-trigger-workflows#pull_request, fork PR secrets, read 2026-11-09.

    Owner ruling 00:58 replaces the live fork run with this static proof and the
    all_external_contributors API receipt in the handoff scratch/receipts directory.
    """
    found = []
    for path in (*WORKFLOWS.glob("*.yml"), *WORKFLOWS.glob("*.yaml")):
        text = path.read_text()
        workflow = yaml.safe_load(text)
        events = workflow.get("on", workflow.get(True))
        if "pull_request" in events:
            found.append(path.name)
            assert "secrets." not in text, path
    assert "ci.yml" in found


def test_parity_set_has_twenty_four_bundles_and_no_memory_plugins() -> None:
    profile = json.loads((ROOT / ".github/gate/dsh-profile.json").read_text())
    assert len(profile["bundles"]) == 24
    names = {row["package"] for row in profile["bundles"]}
    assert "@deepseek-ai/dsh-headless" in names
    assert not any(re.search(r"memento|engram|openviking", name, re.I) for name in names)
    assert profile["engine_authority"] == "reuses main's dsh pin (lead ruling 2026-11-09)"


def test_dsh_github_scripts_are_unchanged_at_the_qualifying_upstream_pin() -> None:
    expected = {
        "action-patch.mjs": "a897b80da6c1a42f32261e54a92e2bd1163dd075e0574edc4c336b96ceb3e797",
        "action-post.mjs": "f8df0a576aefe1ff68e2c45caae276bf06ecae6eeba5648346a7ff0cf0981875",
    }
    for filename, digest in expected.items():
        script = ROOT / ".github/actions/dsh-github/scripts" / filename
        assert hashlib.sha256(script.read_bytes()).hexdigest() == digest


def test_landing_standard_and_fail_on_error_are_wired_into_the_verdict() -> None:
    rules = (WORKFLOWS / "shared/review-rules.md").read_text().split("## Report", 1)[1]
    for required in (
        "3bb3ec25b3b0199f4940b1aa75f0ac5c5753301c",
        "standard.md:31-34,72,88-90",
        "small-cls.md:175",
        "concrete failing witness at the exact head",
        "input or state",
        "wrong output, crash or failing test",
        "speculative or unwitnessed",
        "From the third reviewed commit, only a P0 blocks",
        "ONE follow-up issue per PR",
        "fail-on: error",
    ):
        assert required in rules
    dsh = _gate()["jobs"]["dsh"]
    review = next(step for step in dsh["steps"] if step.get("id") == "review")
    assert review["with"]["blocking"] == "true"
    assert review["with"]["fail-on"] == "error"
    adapter = (ROOT / ".github/gate/gate.cjs").read_text()
    assert ".github/workflows/shared/review-rules.md" in adapter


def test_every_blocking_finding_carries_an_executed_witness_or_the_static_label() -> None:
    """The program lead's 2026-11-11 gate ruling: eight static findings, four did not reproduce."""
    prompt = (WORKFLOWS / "review.md").read_text()
    rules = (WORKFLOWS / "shared/review-rules.md").read_text()
    for surface in (prompt, rules):
        assert "executed witness" in surface
        assert "static: no witness run" in surface
    report = rules.split("## Report", 1)[1].split("## Two author families", 1)[0]
    assert "states its evidence status" in report
    assert "does not replace the witness" in report
    assert "non-blocking" in report and "follow-up issue" in report


def test_quality_verdict_runs_even_when_the_review_action_fails() -> None:
    """A failed action leaves no verdict; the verdict step still runs to name it."""
    for family in ("codex", "claude"):
        verdict = next(
            step for step in _gate()["jobs"][family]["steps"] if step.get("id") == "verdict"
        )
        assert verdict.get("if") == "always()", family


def test_paper_carveout_keeps_only_the_hard_lines_blocking() -> None:
    rules = (WORKFLOWS / "shared/review-rules.md").read_text()
    owner = (
        "it is only paper and i can reset them anytime,never gate the e2e and sota workflow , "
        "we need full automation on paper lane rather than so afraid of touch the order, "
        "they meant to be automated with sota stratiges, exit, reentry etc"
    )
    assert owner in rules
    carveout = rules.split("paper carve-out", 1)[1]
    for required in (
        "lanes/paper/",
        "src/sextant/paper/",
        "secret",
        "live",
        "one writer per account",
        "Decimal",
        "never cancelled, sold back or resized",
        "ONE follow-up issue",
    ):
        assert required in carveout
    assert "source" in carveout and "ci" in carveout


def test_paper_and_e2e_carveout_is_finding_level() -> None:
    """One fixture per class of the finding-level scope widening #99."""
    rules = (WORKFLOWS / "shared/review-rules.md").read_text()
    carveout = rules.split("paper carve-out", 1)[1].split("\n\n", 1)[0]
    # Finding-level, not PR-level: #99's all-or-nothing diff rule is gone.
    assert "A diff is paper-only" not in rules
    assert "On a paper-only diff" not in rules
    for required in ("per finding", "located in", "mixed PR", "same PR", "blocks `review` ONLY"):
        assert required in carveout
    paper_e2e = (
        ("lanes/paper/README.md", "lanes/paper/**"),
        ("src/sextant/paper/b_protector.py", "src/sextant/paper/**"),
        ("src/sextant/backtest/replay.py", "src/sextant/backtest/**"),
        ("tests/proofs/test_paper_node.py", "test_paper*"),
        ("tests/proofs/test_b_protector.py", "test_b_*"),
        ("tests/proofs/test_ac_node.py", "test_ac_*"),
        ("tests/proofs/test_outside_rth_trail.py", "test_outside_rth*"),
        ("tests/proofs/entry/test_replay.py", "test_replay*"),
    )
    for example, pattern in paper_e2e:
        assert pattern in carveout, pattern
        assert fnmatch.fnmatch(example, pattern) or fnmatch.fnmatch(Path(example).name, pattern), (
            example
        )
    # The manifest-hunk class: judged by its hunk, not by the whole file.
    assert "docs/manifest.md` hunks whose row source names" in carveout
    stay_normal = carveout.split("stay normal", 1)[1]
    for example, pattern in (
        ("docs/adr/0007-custody-and-order-writing.md", "docs/adr/**"),
        ("src/sextant/execution/custody.py", "src/sextant/execution/**"),
        (".github/workflows/gate.yml", ".github/**"),
    ):
        assert pattern in stay_normal, pattern
        assert fnmatch.fnmatch(example, pattern), example
        assert not any(
            fnmatch.fnmatch(example, other) or fnmatch.fnmatch(Path(example).name, other)
            for _, other in paper_e2e
        ), example
    for required in ("both a paper or e2e path", "no paper or e2e location"):
        assert required in carveout


def test_upstream_first_rule_names_the_sdk_call_and_its_severity() -> None:
    rules = (WORKFLOWS / "shared/review-rules.md").read_text()
    owner = (
        "with clean upstrean integrated line should not be talking too much lines "
        "in the first place ,we need sota automation"
    )
    assert owner in rules
    upstream_first = rules.split("## Upstream-first", 1)[1].split("## Report", 1)[0]
    # What a pinned upstream already provides must not be re-implemented in new src code.
    for required in (
        "re-implements what a pinned upstream already provides",
        "HTTP transports",
        "retry or pagination loops",
        "rate pacers",
        "calculators",
        "data clients",
        "OAuth flows",
        # The finding names the SDK call to use instead.
        "TradingClient",
        "StockHistoricalDataClient",
        "NautilusTrader's own components",
        "the MCP SDK",
        "edgartools",
        # Severity: P1 on normal paths, a follow-up under #128 on paper paths.
        "[P1]",
        "#128",
        "never blocking",
        # The example witness: #74's hand-built HTTP transport.
        "#74",
        "src/sextant/execution/alpaca_transport.py",
        # Using the upstream, or carrying it verbatim, is not re-implementing it.
        "not re-implementing it",
        "not an Always P0",
    ):
        assert required in upstream_first, required
    # The Report verdict admits the witnessed P1 on normal paths, and only then.
    report = rules.split("## Report", 1)[1]
    assert "upstream-first duplication on a normal path" in report
    assert (
        "Only a finding at the blocking threshold" in (ROOT / ".github/gate/gate.cjs").read_text()
    )


def test_key_policy_and_failure_paths_execute_against_the_actual_adapter() -> None:
    done = subprocess.run(
        ["node", "--test", "tests/proofs/public-gate.test.cjs"],
        cwd=ROOT,
        text=True,
        capture_output=True,
        timeout=30,
    )
    assert done.returncode == 0, done.stdout + done.stderr


def test_the_review_spill_root_is_the_gates_known_output_dir() -> None:
    overlay = (ROOT / ".github/gate/headless-services.cordis.yml").read_text()
    assert "- id: spill-local" in overlay
    assert "root: !!js process.env.DSH_GITHUB_CI_OUTPUT_DIR + '/spill'" in overlay
    action = (ROOT / ".github/actions/dsh-github/action.yml").read_text()
    assert "DSH_GITHUB_CI_OUTPUT_DIR: ${{ inputs.output-dir }}" in action
    bundle = (ROOT / ".github/gate/bundles/api-only/index.mjs").read_text()
    assert "DSH_GITHUB_CI_OUTPUT_DIR" in bundle
    assert "const spillPathFields = new Map([['read', 'file_path'], ['grep', 'path']]);" in bundle
