"""CI invariants retained from the template's test_public_gate.py.

Sources: template cc156d0; GitHub rulesets documentation, read 2026-11-11.
"""

import json
import re
from pathlib import Path

import yaml

ROOT = Path(__file__).parents[2]
WORKFLOWS = ROOT / ".github/workflows"


def test_no_workflow_requests_a_local_runner_or_privileged_pr_trigger() -> None:
    for path in (*WORKFLOWS.glob("*.yml"), *WORKFLOWS.glob("*.yaml")):
        workflow = yaml.safe_load(path.read_text())
        events = workflow.get("on", workflow.get(True))
        assert "pull_request_target" not in events, path
        for job in workflow["jobs"].values():
            assert job["runs-on"] == "ubuntu-24.04", path


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


def test_only_ci_is_required_and_squash_is_the_merge_method() -> None:
    ruleset = json.loads((ROOT / ".github/rulesets/main.json").read_text())
    rules = {rule["type"]: rule.get("parameters", {}) for rule in ruleset["rules"]}
    assert rules["required_status_checks"]["required_status_checks"] == [
        {"context": "ci", "integration_id": 15368}
    ]
    assert rules["pull_request"]["allowed_merge_methods"] == ["squash"]
    assert rules["pull_request"]["required_approving_review_count"] == 0
    workflow = yaml.safe_load((WORKFLOWS / "ci.yml").read_text())
    assert workflow["jobs"]["ci"]["name"] == "ci"
    assert set(workflow["jobs"]["ci"]["needs"]) == {"lint", "tests", "budgets", "secrets"}
