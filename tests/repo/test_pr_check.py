"""`just pr` refuses locally on the Danger body rules and the 45,000-character review window.

Source: danger/danger-js@14.1.0 `72540533c4f99bad772ba303be0eb4af6eddbdc4`:
`source/commands/danger-local.ts` and `source/platforms/LocalGit.ts`;
`docs/tutorials/fast-feedback.html.md`, read 2026-11-10.
"""

import json
import re
import shutil
import subprocess
import tempfile
from pathlib import Path

ROOT = Path(__file__).parents[2]
MANIFEST = (
    "| Practice or pick | Source, pinned | How it is applied | Proof |\n"
    "|---|---|---|---|\n"
    "| Real row | source | applied | proof |\n"
)
SHA = "c" * 40
# Runs dangerfile.js with only the modules its local branch reads. `body: null` makes the body
# file unreadable, as a bad path or a mistyped variable would.
HARNESS = """
const fs = require("fs");
const vm = require("vm");
const input = JSON.parse(fs.readFileSync(0, "utf8"));
const fails = [];
const pending = [];
const reads = [];
const commands = [];
const danger = { git: { created_files: input.created, modified_files: [], deleted_files: [] } };
if (input.github) {
  danger.github = {
    pr: { body: input.body, user: { login: input.login } },
    utils: { fileContents: async () => input.manifest },
  };
}
const fakeFs = {
  readFileSync: (path) => {
    reads.push(path);
    if (input.body === null) throw new Error("EACCES: permission denied, open '" + path + "'");
    return input.body;
  },
};
const fakeChildProcess = {
  execSync: (command) => {
    commands.push(command);
    return input.manifest;
  },
};
const api = {
  danger,
  fail: (message) => fails.push(message),
  schedule: (task) => pending.push(task()),
};
const modules = { danger: api, fs: fakeFs, child_process: fakeChildProcess };
const sandbox = {
  require: (id) => {
    if (!(id in modules)) throw new Error("unexpected require: " + id);
    return modules[id];
  },
  process: { env: input.env },
};
vm.runInNewContext(fs.readFileSync(process.argv[1], "utf8"), sandbox);
Promise.all(pending).then(() =>
  console.log(JSON.stringify({ fails: fails, reads: reads, commands: commands })),
);
"""
GIT_ENV = {
    "GIT_CONFIG_GLOBAL": "/dev/null",
    "GIT_CONFIG_NOSYSTEM": "1",
    "PATH": "/usr/bin:/bin",
    "HOME": tempfile.gettempdir(),
}


def _body(*, sources: str = "a/b@v1 " + SHA, row: str = '"Real row"') -> str:
    return (
        "## What changed\nx\n"
        f"## Manifest row\n{row}\n"
        f"## Upstream references\n{sources}\n"
        "## How verified\ny\n"
        "## How to revert\nz\n"
    )


def _local(
    *,
    body: str | None = None,
    env: dict[str, str] | None = None,
    created: tuple[str, ...] = (),
    github: bool = False,
    login: str = "someone",
) -> dict:
    payload = {
        "body": body,
        "env": env or {},
        "created": list(created),
        "manifest": MANIFEST,
        "github": github,
        "login": login,
    }
    done = subprocess.run(
        ["node", "-e", HARNESS, str(ROOT / "dangerfile.js")],
        input=json.dumps(payload),
        capture_output=True,
        text=True,
        check=True,
    )
    return json.loads(done.stdout)


def test_a_local_body_file_passes_the_body_rules() -> None:
    assert _local(body=_body(), env={"LOCAL_PR_BODY_FILE": "body.md"})["fails"] == []


def test_the_local_body_is_read_from_its_file() -> None:
    result = _local(body=_body(), env={"LOCAL_PR_BODY_FILE": "body.md"})
    assert result["reads"] == ["body.md"]


def test_the_local_manifest_is_read_from_head() -> None:
    result = _local(body=_body(), env={"LOCAL_PR_BODY_FILE": "body.md"})
    assert result["commands"] == ["git show HEAD:docs/manifest.md"]


def test_a_local_run_without_a_body_variable_fails() -> None:
    fails = _local(body=_body(), env={})["fails"]
    assert any("LOCAL_PR_BODY_FILE" in failure for failure in fails)
    assert any("What changed" in failure for failure in fails)


def test_a_local_run_with_an_empty_body_path_fails() -> None:
    fails = _local(body=_body(), env={"LOCAL_PR_BODY_FILE": ""})["fails"]
    assert any("LOCAL_PR_BODY_FILE" in failure for failure in fails)


def test_a_local_run_with_an_unreadable_body_file_fails() -> None:
    fails = _local(body=None, env={"LOCAL_PR_BODY_FILE": "missing.md"})["fails"]
    assert any("missing.md" in failure for failure in fails)
    assert any("What changed" in failure for failure in fails)


def test_an_empty_body_file_still_fails_every_section() -> None:
    fails = _local(body="", env={"LOCAL_PR_BODY_FILE": "body.md"})["fails"]
    names = ("What changed", "Manifest row", "Upstream references", "How verified", "How to revert")
    for name in names:
        assert any(name in failure for failure in fails), name


def test_a_lanes_change_without_a_lanes_authority_is_refused() -> None:
    fails = _local(
        body=_body(sources="example/fork@v1 " + SHA),
        env={"LOCAL_PR_BODY_FILE": "body.md"},
        created=("lanes/paper/README.md",),
    )["fails"]
    assert any("lanes" in failure and "authority" in failure.lower() for failure in fails)


def test_a_lanes_change_with_its_authority_passes() -> None:
    body = _body(sources="nautechsystems/nautilus_trader@v1 " + SHA)
    result = _local(
        body=body,
        env={"LOCAL_PR_BODY_FILE": "body.md"},
        created=("lanes/paper/README.md",),
    )
    assert result["fails"] == []


def test_an_empty_section_is_refused_locally() -> None:
    body = _body().replace("\ny\n", "\n\n")
    fails = _local(body=body, env={"LOCAL_PR_BODY_FILE": "body.md"})["fails"]
    assert any("How verified" in failure for failure in fails)


def test_the_local_branch_is_inert_when_github_is_present() -> None:
    plain = _local(body=_body(), github=True)
    with_bad_variable = _local(
        body=_body(), github=True, env={"LOCAL_PR_BODY_FILE": "definitely-missing.md"}
    )
    assert plain["fails"] == []
    assert with_bad_variable["fails"] == []
    assert with_bad_variable["reads"] == []


def test_the_local_variable_never_changes_a_github_run() -> None:
    plain = _local(body="", github=True)["fails"]
    with_variable = _local(body="", github=True, env={"LOCAL_PR_BODY_FILE": "missing.md"})["fails"]
    assert plain == with_variable
    assert any("What changed" in failure for failure in plain)


def _git(repo: Path, *args: str) -> str:
    done = subprocess.run(
        ["git", "-C", str(repo), *args], capture_output=True, text=True, env=GIT_ENV
    )
    assert done.returncode == 0, done.stderr
    return done.stdout.strip()


def _node_dir() -> str:
    node = shutil.which("node")
    assert node is not None, "node is danger's runtime"
    return str(Path(node).parent)


def _repo(
    tmp_path: Path,
    *,
    commits: bool = True,
    window_bytes: int = 10,
    danger: bool = True,
    version: str = "14.1.0",
) -> Path:
    repo = tmp_path / "repo"
    (repo / "scripts").mkdir(parents=True)
    (repo / ".devcontainer" / "agents").mkdir(parents=True)
    (repo / "docs").mkdir()
    shutil.copy(ROOT / "scripts" / "pr-check.sh", repo / "scripts" / "pr-check.sh")
    shutil.copy(ROOT / "dangerfile.js", repo / "dangerfile.js")
    shutil.copy(
        ROOT / ".devcontainer" / "agents" / "package.json",
        repo / ".devcontainer" / "agents" / "package.json",
    )
    (repo / "docs" / "manifest.md").write_text(MANIFEST)
    (repo / "body.md").write_text(_body())
    (repo / "bin").mkdir()
    if danger:
        stub = repo / "bin" / "danger"
        stub.write_text(
            "#!/usr/bin/env bash\n"
            "set -eu\n"
            f'if [[ "${{1:-}}" == "--version" ]]; then echo "{version}"; exit 0; fi\n'
            'if [[ -z "${LOCAL_PR_BODY_FILE:-}" || ! -r "${LOCAL_PR_BODY_FILE}" ]]; then\n'
            '  echo "stub danger: no readable LOCAL_PR_BODY_FILE" >&2\n'
            "  exit 9\n"
            "fi\n"
            f'printf "%s\\n" "$@" > "{repo / "danger-args"}"\n'
            "exit 0\n"
        )
        stub.chmod(0o755)
    _git(repo, "init", "-q", "-b", "main")
    _git(repo, "config", "user.email", "t@example.invalid")
    _git(repo, "config", "user.name", "t")
    _git(repo, "add", ".")
    _git(repo, "commit", "-q", "-m", "base")
    base = _git(repo, "rev-parse", "HEAD")
    if commits:
        _git(repo, "switch", "-q", "-c", "lane")
        if window_bytes:
            (repo / "change.txt").write_text("x" * window_bytes + "\n")
            _git(repo, "add", "change.txt")
            _git(repo, "commit", "-q", "-m", "change")
    _git(repo, "update-ref", "refs/remotes/origin/main", base)
    return repo


def _run_pr_check(repo: Path) -> subprocess.CompletedProcess[str]:
    # A clean PATH first, so the host's own danger install cannot satisfy a missing-pin test.
    env = {
        "PATH": f"{repo / 'bin'}:{_node_dir()}:/usr/bin:/bin",
        "HOME": str(repo.parent),
        "GIT_CONFIG_GLOBAL": "/dev/null",
        "GIT_CONFIG_NOSYSTEM": "1",
    }
    return subprocess.run(
        ["bash", str(repo / "scripts" / "pr-check.sh"), "body.md"],
        cwd=repo,
        capture_output=True,
        text=True,
        env=env,
    )


def test_the_script_refuses_when_the_pinned_danger_is_missing(tmp_path: Path) -> None:
    repo = _repo(tmp_path, danger=False)
    done = _run_pr_check(repo)
    assert done.returncode != 0
    assert "deploy/wsl/setup.sh" in done.stderr


def test_the_script_refuses_a_danger_off_the_repository_pin(tmp_path: Path) -> None:
    repo = _repo(tmp_path, version="13.9.0")
    done = _run_pr_check(repo)
    assert done.returncode != 0
    assert "13.9.0" in done.stderr
    assert "14.1.0" in done.stderr
    assert "deploy/wsl/setup.sh" in done.stderr


def test_the_script_runs_the_pinned_local_mode_with_the_body_file(tmp_path: Path) -> None:
    repo = _repo(tmp_path)
    done = _run_pr_check(repo)
    assert done.returncode == 0, done.stderr
    args = (repo / "danger-args").read_text().splitlines()
    assert args[0] == "local"
    assert "--dangerfile" in args
    assert args[args.index("--dangerfile") + 1] == str(repo / "dangerfile.js")
    assert args[args.index("--base") + 1] == "origin/main"
    assert args[-1] == "--failOnErrors"
    assert "pr-check:" in done.stdout


def test_the_script_refuses_a_review_window_over_the_limit(tmp_path: Path) -> None:
    repo = _repo(tmp_path, window_bytes=50_000)
    done = _run_pr_check(repo)
    assert done.returncode != 0
    assert "45000" in done.stderr
    assert "#305" in done.stderr


def test_the_script_refuses_a_window_with_no_commits(tmp_path: Path) -> None:
    repo = _repo(tmp_path, commits=False)
    done = _run_pr_check(repo)
    assert done.returncode != 0
    assert "nothing to land" in done.stderr
    assert not (repo / "danger-args").exists()


def test_the_script_never_fetches_at_run_time() -> None:
    text = (ROOT / "scripts" / "pr-check.sh").read_text()
    for marker in ("npx", "npm install", "npm ci", "curl ", "wget ", "git clone"):
        assert marker not in text, marker


def test_nothing_under_github_sets_the_body_file_variable() -> None:
    hits = [
        path.relative_to(ROOT).as_posix()
        for path in (ROOT / ".github").rglob("*")
        if path.is_file() and "LOCAL_PR_BODY_FILE" in path.read_text(errors="ignore")
    ]
    assert hits == []


def test_the_raw_diff_limit_is_one_named_constant_with_its_measurement() -> None:
    lines = (ROOT / "scripts" / "pr-check.sh").read_text().splitlines()
    assignments = [line for line in lines if line.strip().startswith("readonly RAW_DIFF_LIMIT=")]
    assert len(assignments) == 1
    match = re.search(r"RAW_DIFF_LIMIT=(\d+)", assignments[0])
    assert match is not None
    assert match.group(1) == "45000"
    assert "#305" in assignments[0]
    assert "git diff origin/main...HEAD" in assignments[0]


def test_the_justfile_chains_the_check_before_the_push_and_keeps_104_lines() -> None:
    lines = (ROOT / "justfile").read_text().splitlines()
    assert len(lines) == 104
    assert lines[101] == '    bash scripts/pr-check.sh "{{body_file}}" && git push -u origin HEAD'


def test_the_manifest_row_names_the_local_check_and_its_proofs() -> None:
    key = "| just pr refuses locally on the Danger body rules and the review window |"
    rows = [
        line
        for line in (ROOT / "docs" / "manifest.md").read_text().splitlines()
        if line.startswith(key)
    ]
    assert len(rows) == 1
    assert "danger-local.ts" in rows[0]
    assert "tests/repo/test_pr_check.py" in rows[0]
