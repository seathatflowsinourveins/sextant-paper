"""Deliver the converged shelf, the lane MCP set and the dsh quality set (parity.json).

Each runtime gets its own native format in its own lane home. Nothing goes to the shared
~/.agents/skills root, which blind legs also read. Rendered files carry environment-variable
names only; keys reach a session at launch through scripts/with-keys.sh. A lane file or skill
that differs from the pinned render is kept, reported and counted as a failure. Muse gets
its launch pins from muse-launch.sh; its shelf and MCP set wait on their tracked issue, because
Muse 1.4.6 has one user config root and its -c flag cannot add an MCP server.
"""

import argparse
import hashlib
import json
import os
import re
import shutil
import subprocess
import sys
import tempfile
from pathlib import Path

HERE = Path(__file__).resolve().parent
REPO = HERE.parents[2]
SPEC = json.loads((HERE / "parity.json").read_text())
LOCK = json.loads((REPO / SPEC["lock"]).read_text())
GIT = (
    "git",
    "--no-replace-objects",
    "-c",
    "core.fsmonitor=false",
    "-c",
    "core.hooksPath=/dev/null",
)
NATIVE_TIMEOUT = 600  # seconds per native dsh or agy call; a stall is reported, never waited out
# codex-launch.sh's guard: the lane CODEX_HOME never resolves into ~/.codex or an Orca account home,
# where Codex's ChatGPT logins live (Orca docs/site/content/docs/agents/codex.mdx).
LOGIN_HOMES = (".codex", ".config/orca", ".local/share/orca")


def _native(command: list[str]) -> None:
    """One native install call, stdin closed so no prompt can wait on the terminal."""
    subprocess.run(command, check=True, stdin=subprocess.DEVNULL, timeout=NATIVE_TIMEOUT)


class Refused(Exception):
    """A lane file differs from the pinned render; it stays as it is for review."""


# What a step reports as NOT_CONFIGURED before the next step runs: a refusal, the filesystem,
# or a native call that failed or stalled.
REPORTED = (Refused, OSError, subprocess.CalledProcessError, subprocess.TimeoutExpired)


def _guard_roots(lane: Path, *roots: Path) -> None:
    """Resolve like codex-launch.sh's readlink -m, before mkdir, chmod or native writes.
    Check the lane itself too: resolving the boundary would silently bless a redirected home.
    Native node_modules package links are inputs, not managed directory roots."""
    boundary = Path(os.path.abspath(lane))
    for root in (lane, *roots):
        if root.is_symlink() or not Path(os.path.realpath(root)).is_relative_to(boundary):
            raise Refused(
                f"{root}: managed root is linked or resolves outside {boundary}; refusing"
            )


def _dsh_roots(dsh_home: Path) -> None:
    roots = [dsh_home / "skills", dsh_home / "profiles"]
    for name in ("dsh-tui", *SPEC["runtimes"]["dsh"]["profiles"]):
        profile = dsh_home / "profiles" / name
        roots.extend([profile, profile / "node_modules", profile / "package.json"])
        if profile.is_dir():
            roots.extend(profile.iterdir())  # manifests, locks, patches and native state roots
        roots.extend([profile / ".plugin-manager", profile / ".plugin-manager/logs"])
    _guard_roots(dsh_home, *roots)


def _antigravity_roots(home: Path) -> None:
    """Antigravity 1.4.6's native plugin and MCP state, alongside the generated shelf cache."""
    lane = home / ".gemini"
    plugins = lane / "config/plugins"
    data = lane / "antigravity-cli/plugin_data"
    _guard_roots(
        lane,
        lane / "config",
        plugins,
        lane / "config/mcp_config.json",
        lane / "antigravity-cli",
        data,
    )
    # Native plugin installs also write below each plugin/state directory. Check the parents
    # first, then existing descendants, so a nested link cannot redirect that native write.
    for root in (plugins, data):
        if root.is_dir():
            _guard_roots(lane, *root.rglob("*"))


def _names(key: str, row: dict) -> list[str]:
    if key == "trading":  # the seven hash-locked trading skill folders
        return sorted({path.split("/", 1)[0] for path in LOCK["skills"]["files"] if "/" in path})
    return row["skills"]


def shelf_names() -> list[str]:
    return [name for key, row in SPEC["shelf"].items() for name in _names(key, row)]


def _git(path: Path, *args: str) -> str:
    return subprocess.check_output([*GIT, "-C", str(path), *args], text=True).strip()


def checkout(path: Path, pin: dict[str, str]) -> Path:
    """A shallow clone at the lock's commit; refresh a clean cache when its pin changes."""
    _guard_roots(path.parent, path, path / ".git")
    if not path.exists():
        url = f"https://github.com/{pin['repo']}.git"
        subprocess.run(
            [*GIT, "clone", "--depth", "1", "--branch", pin["ref"], url, str(path)], check=True
        )
    if _git(path, "status", "--porcelain", "--untracked-files=all", "--ignored"):
        raise Refused(f"{path}: differs from its pinned commit")
    if _git(path, "rev-parse", "HEAD") != pin["sha"]:
        # git-fetch(1): a full object name is a refspec source; git-checkout(1): --detach
        # selects that exact commit without moving a local branch. Never reset a dirty cache.
        _git(path, "fetch", "--depth", "1", "origin", pin["sha"])
        _git(path, "checkout", "--detach", pin["sha"])
    if _git(path, "rev-parse", "HEAD") != pin["sha"]:
        raise Refused(f"{path}: not at the lock's {pin['sha']}")
    return path


def shelf(cache: Path) -> dict[str, Path]:
    """The 25 pinned skill trees: three verified checkouts and the hash-locked trading copy."""
    _guard_roots(cache)
    trading = REPO / SPEC["shelf"]["trading"]["root"]
    files = {str(p.relative_to(trading)) for p in trading.rglob("*") if p.is_file()}
    if files != set(LOCK["skills"]["files"]):
        raise Refused("the trading shelf's file list differs from .claude/shelf.lock.json")
    for relative, digest in LOCK["skills"]["files"].items():
        if hashlib.sha256((trading / relative).read_bytes()).hexdigest() != digest:
            raise Refused(f"trading shelf content differs: {relative}")
    roots = {"trading": trading}
    for key, row in SPEC["shelf"].items():
        if key != "trading":
            roots[key] = checkout(cache / row["cache"], LOCK["plugins"][key]) / "skills"
    skills = {}
    for key, row in SPEC["shelf"].items():
        for name in _names(key, row):
            if not (roots[key] / name / "SKILL.md").is_file():
                raise Refused(f"{roots[key] / name}: missing from its pinned source")
            skills[name] = roots[key] / name
    return skills


def _tree(root: Path) -> dict[str, tuple[bytes, bool]]:
    return {
        str(p.relative_to(root)): (p.read_bytes(), bool(p.stat().st_mode & 0o100))
        for p in root.rglob("*")
        if p.is_file()
    }


def copy_skill(source: Path, root: Path) -> str:
    _guard_roots(root.parent, root)
    destination = root / source.name
    for tree in (source, destination):
        if tree.is_symlink() or any(p.is_symlink() for p in tree.rglob("*")):
            raise Refused(f"{tree}: links are never copied or replaced")
    if destination.exists():
        if _tree(destination) != _tree(source):
            raise Refused(f"{destination}: differs from the pinned skill")
        return "unchanged"
    root.mkdir(parents=True, exist_ok=True)
    with tempfile.TemporaryDirectory(prefix=".skill-pending-", dir=root) as staging:
        shutil.copytree(source, Path(staging) / source.name)
        os.rename(Path(staging) / source.name, destination)
    return "installed"


def _publish(path: Path, text: str, mode: int) -> str:
    _guard_roots(path.parent, path)
    path.parent.mkdir(parents=True, exist_ok=True)
    descriptor, pending = tempfile.mkstemp(prefix=path.name + ".pending-", dir=path.parent)
    with os.fdopen(descriptor, "w") as output:
        output.write(text)
    os.chmod(pending, mode)
    os.replace(pending, path)
    return "written"


def write_owned(path: Path, text: str, mode: int = 0o644) -> str:
    _guard_roots(path.parent, path)
    if path.exists():
        if path.read_text() != text:
            raise Refused(f"{path}: differs from the pinned render")
        return "unchanged"
    return _publish(path, text, mode)


def _command(server: dict, home: Path) -> list[str]:
    return [str(home / server["command"]), *server.get("args", [])]


def codex_toml(home: Path) -> str:
    """Codex's native mcp_servers tables: env-sourced bearer and headers, opt-in stdio jobs."""
    lines = [
        "# Rendered by deploy/wsl/lane-runtimes/parity.py from parity.json; an edit is kept.",
        "",
    ]
    for name, server in SPEC["mcp"].items():
        lines.append(f"[mcp_servers.{name}]")
        if "url" in server:
            lines.append(f"url = {json.dumps(server['url'])}")
            if server.get("scheme") == "Bearer":
                lines.append(f"bearer_token_env_var = {json.dumps(server['key'])}")
            elif "key" in server:
                header, key = json.dumps(server["header"]), json.dumps(server["key"])
                lines.append(f"env_http_headers = {{ {header} = {key} }}")
        else:
            command, *args = _command(server, home)
            lines.append(f"command = {json.dumps(command)}")
            if args:
                lines.append(f"args = {json.dumps(args)}")
            if server.get("env"):
                pairs = ", ".join(f"{k} = {json.dumps(v)}" for k, v in server["env"].items())
                lines.append(f"env = {{ {pairs} }}")
        if server.get("opt_in"):
            lines.append("enabled = false")
        lines.append("")
    return "\n".join(lines)


def _codex_lane(home: Path) -> Path:
    lane = home / SPEC["runtimes"]["codex"]["home"]
    resolved = Path(os.path.realpath(lane))  # readlink -m, as the launcher resolves it
    for root in LOGIN_HOMES:
        login = Path(os.path.realpath(home / root))
        if resolved == login or login in resolved.parents:
            raise Refused(
                f"the lane CODEX_HOME resolves into a Codex login home ({root}); refusing"
            )
    _guard_roots(lane, lane / "skills", lane / "config.toml")
    return lane


def install_codex(skills: dict[str, Path], home: Path) -> list[str]:
    lane = _codex_lane(home)
    lane.mkdir(parents=True, exist_ok=True)
    lane.chmod(0o700)
    states = [copy_skill(path, lane / "skills") for path in skills.values()]
    return [*states, write_owned(lane / "config.toml", codex_toml(home))]


def _recorded(profile: Path, value: str | None) -> str | None:
    """A recorded link: resolved as dsh-plugin-manager resolves it (installed, lib/index.js:214):
    pnpm records a local path relative to the profile, or with ~ for the home directory."""
    if value is not None and value.startswith("link:"):
        return "link:" + os.path.realpath(profile / Path(value.removeprefix("link:")).expanduser())
    return value


def _bundle_installed(profile: Path, bundle: Path) -> bool:
    """dsh's recorded dependency, installed target and enabled bundle must all agree."""
    manifest = profile / "package.json"
    present = json.loads(manifest.read_text()) if manifest.is_file() else {}
    package = json.loads((bundle / "package.json").read_text())["name"]
    return (
        _recorded(profile, present.get("dependencies", {}).get(package))
        == f"link:{os.path.realpath(bundle)}"
        and os.path.realpath(profile / "node_modules" / package) == os.path.realpath(bundle)
        and package in present.get("dsh", {}).get("profile", {}).get("bundles", [])
    )


def _quality_target(repository: Path, spec: str) -> tuple[str, str]:
    """A plugin row's `dsh plugin add` argument and the dependency value the profile records.
    A repository bundle (a config/ or .github/ path, like the review shim) is added by its
    absolute path and records as a link, like the quality and MCP bundles; a github: spec
    records verbatim under its package's own name, an npm spec as its exact version."""
    if spec.startswith(("config/", ".github/")):
        target = str(repository / spec)
        return target, f"link:{os.path.realpath(target)}"
    return spec, (spec if spec.startswith("github:") else spec.rsplit("@", 1)[1])


def _quality_missing(profile: Path, name: str, repository: Path) -> list[str]:
    """The quality set's pinned plugin specs and bundle this profile has not recorded. A
    github: spec records verbatim under its package's own name, an npm spec as its exact
    version (dsh-plugin-manager), a repository bundle as a link like the quality one."""
    quality = SPEC["runtimes"]["dsh"]["quality"]
    if name not in quality["profiles"]:
        return []
    manifest = profile / "package.json"
    present = json.loads(manifest.read_text()).get("dependencies", {}) if manifest.is_file() else {}
    wanted = {}
    for entry in quality["plugins"]:
        if name in entry.get("profiles", quality["profiles"]):
            target, recorded = _quality_target(repository, entry["spec"])
            wanted[target] = (entry["package"], recorded)
    bundle = repository / SPEC["runtimes"]["dsh"]["quality_bundle"]
    package = json.loads((bundle / "package.json").read_text())["name"]
    wanted[str(bundle)] = (package, f"link:{os.path.realpath(bundle)}")
    return [
        spec
        for spec, (package, version) in wanted.items()
        if _recorded(profile, present.get(package)) != version
    ]


def dsh_missing(profile: Path, repository: Path) -> list[str]:
    """A shipped profile that never ran has no manifest; dsh plugin initializes its template
    (dsh-official-bundles PROFILE_TEMPLATES; dsh-app-boot). The quality set (parity.json
    quality) joins per profile; dsh-tui composes the MCP set through its
    sextant-dsh-research bundle, so it only takes the quality set here."""
    name = profile.name
    bundle = repository / SPEC["runtimes"]["dsh"]["mcp_bundle"]
    mcp = [] if name == "dsh-tui" or _bundle_installed(profile, bundle) else [str(bundle)]
    return [*mcp, *_quality_missing(profile, name, repository)]


def ensure_workspace(profile: Path, repository: Path) -> str:
    """The profile's pnpm workspace must acknowledge the quality tree's build scripts
    (onnxruntime-node via dsh-engram, protobufjs) before the first add, or pnpm fails
    the whole add after recording it (ERR_PNPM_IGNORED_BUILDS) and no bundle is ever
    selected. An uninitialized profile keeps a file already present (initProfile
    never overwrites), so seed Sextant's own; an initialized one is the operator's
    one-time edit, reported here rather than rewritten."""
    path = profile / "pnpm-workspace.yaml"
    if not path.exists():
        profile.mkdir(parents=True, exist_ok=True)
        return _publish(
            path, (repository / "deploy/wsl/dsh/pnpm-workspace.yaml").read_text(), 0o644
        )
    text = path.read_text()
    if "onnxruntime-node:" in text and "protobufjs:" in text:
        return "unchanged"
    raise Refused(
        f"{path}: add `onnxruntime-node: false` and `protobufjs: false` under "
        "allowBuilds (deploy/wsl/dsh/pnpm-workspace.yaml), then rerun; pnpm "
        "otherwise refuses the quality set's build scripts mid-add"
    )


_ENTRY_ID = re.compile(r"^-\s+id:\s*([\w.-]+)\s*$", re.MULTILINE)


def _patch_lines(text: str) -> list[str]:
    """A profile patch's array lines, with comment and blank lines removed."""
    return [
        line for line in text.splitlines() if line.strip() and not line.lstrip().startswith("#")
    ]


def _entry_blocks(text: str) -> list[tuple[str, str]]:
    """A pin source's top-level rows: each id and the block of lines that states it. Entries
    start only at column zero, so an `insert:` row's nested ids are never matched."""
    starts = list(_ENTRY_ID.finditer(text))
    blocks = []
    for index, match in enumerate(starts):
        end = starts[index + 1].start() if index + 1 < len(starts) else len(text)
        blocks.append((match.group(1), text[match.start() : end].rstrip() + "\n"))
    ids = [entry_id for entry_id, _ in blocks]
    if not blocks or len(ids) != len(set(ids)):
        raise Refused(f"not a patch array of uniquely identified entries ({len(blocks)} rows)")
    return blocks


def _owner_pins(repository: Path) -> dict[str, tuple[str, list[tuple[str, str]]]]:
    """The owner-ruled pins for each managed profile, from the repository's source files
    (config/dsh/pins, parity.json's runtimes.dsh.pins), read before any change to a home."""
    pins = {}
    for name, relative in SPEC["runtimes"]["dsh"]["pins"].items():
        path = repository / relative
        try:
            text = path.read_text()
        except OSError as error:
            raise Refused(
                f"{path}: the owner-ruled dsh pins source is missing ({error}); no dsh changes made"
            ) from error
        lines = _patch_lines(text)
        if not lines or not lines[0].startswith("- "):
            raise Refused(f"{path}: not a top-level patch array; no dsh changes made")
        try:
            pins[name] = (text, _entry_blocks(text))
        except Refused as error:
            raise Refused(f"{path}: {error}; no dsh changes made") from error
    return pins


def _write_pins(path: Path, source: str, entries: list[tuple[str, str]]) -> str:
    """Write one profile's owner pins. The template (or a comments-only file, which dsh
    refuses to boot) is replaced by the source itself, so the ruling's comment header
    arrives with the rows. A populated file only gains the rows it lacks: every existing
    entry and its order stay untouched, Orca's managed block included. A rerun with every
    id present writes nothing."""
    if not path.parent.is_dir():
        raise Refused(f"{path.parent}: the dsh profile is missing, so its pins cannot be written")
    text = path.read_text() if path.is_file() else ""
    lines = _patch_lines(text)
    if not lines or lines == ["[]"]:
        return _publish(path, source, 0o644)
    if not lines[0].startswith("- "):
        raise Refused(f"{path}: not a top-level patch array; refusing to edit its pins")
    present = set(_ENTRY_ID.findall(text))
    missing = [(entry_id, block) for entry_id, block in entries if entry_id not in present]
    if not missing:
        return "unchanged"
    addition = "".join(block for _, block in missing)
    return _publish(path, text + ("" if text.endswith("\n") else "\n") + addition, 0o644)


def _install_bundle_dependencies(targets: list[str]) -> list[str]:
    """A repository bundle's own pinned dependency must live beside it before dsh loads it:
    the review shim imports @apcamargo/dsh-review from its own node_modules. The gate adapter
    installs it the same way (.github/gate/parity-entry.py:39-50); a bundle without a lockfile
    has nothing to install."""
    states = []
    for target in targets:
        path = Path(target)
        if path.is_dir() and (path / "pnpm-lock.yaml").is_file():
            _native(["pnpm", "--dir", target, "install", "--frozen-lockfile"])
            states.append(f"{path.name}: installed its pinned dependency")
    return states


def install_dsh(
    skills: dict[str, Path], dsh_home: Path, dsh: list[str], repository: Path
) -> list[str]:
    _dsh_roots(dsh_home)
    # setup.sh installs the research bundle through dsh plugin add. Verify both the recorded
    # dependency and the installed, selected link before copying a skill or changing a profile.
    profile = dsh_home / "profiles/dsh-tui"
    research = repository / "config/dsh"
    if not _bundle_installed(profile, research):
        raise Refused(
            "dsh-tui: sextant-dsh-research is missing, stale or inactive; run setup.sh's "
            "dsh plugin --profile dsh-tui add step for this repository before parity; "
            "no dsh changes made"
        )
    # acp and headless; dsh-tui composes the same MCP set through its sextant-dsh-research
    # bundle but still takes the quality set, so its quality profiles join the loop.
    quality_profiles = SPEC["runtimes"]["dsh"]["quality"]["profiles"]
    profiles = [*SPEC["runtimes"]["dsh"]["profiles"]]
    profiles += [name for name in quality_profiles if name not in profiles]
    # Read every owner pin source before the first skill copy or native call, and require
    # exactly the managed profiles, so a missing or misnamed source changes nothing.
    pins = _owner_pins(repository)
    if set(pins) != set(profiles):
        raise Refused(
            f"parity.json: dsh pins cover {', '.join(sorted(pins))}; profiles are "
            f"{', '.join(sorted(profiles))}; no dsh changes made"
        )
    states = [copy_skill(path, dsh_home / "skills") for path in skills.values()]
    for name in profiles:
        missing = dsh_missing(dsh_home / "profiles" / name, repository)
        if missing and shutil.which("pnpm") is None:  # dsh-plugin-manager spawns pnpm from PATH
            raise Refused(
                "pnpm is not on PATH; dsh plugin add needs the host-tools lock "
                "(deploy/wsl/dsh/package-lock.json, pnpm 11.21.0). Run parity.sh, which puts "
                "~/.local/share/sextant-host-tools/node_modules/.bin first, or install it with "
                "setup.sh"
            )
        if missing:
            if name in SPEC["runtimes"]["dsh"]["quality"]["profiles"]:
                states.append(ensure_workspace(dsh_home / "profiles" / name, repository))
            states.extend(_install_bundle_dependencies(missing))
            _native([*dsh, "plugin", "--profile", name, "add", "--save-exact", *missing])
            states.append(f"{name}: added {' '.join(missing)}")
    # The profile exists only after the native add initializes it. Write the owner pins
    # then, keeping whatever the profile already carries.
    for name in profiles:
        source, entries = pins[name]
        states.append(
            f"{name}: pins "
            + _write_pins(dsh_home / "profiles" / name / "cordis.patch.yml", source, entries)
        )
    return states


def agy_mcp_commands(home: Path, bridge: Path) -> list[list[str]]:
    """agy mcp add/disable arguments. Antigravity 1.4.6 sends header placeholders literally, so
    keyed servers run the pinned mcp-remote bridge, which expands ${VAR} from its own env."""
    commands, disabled = [], []
    for name, server in SPEC["mcp"].items():
        if "key" in server:
            value = f"${{{server['key']}}}"
            if server.get("scheme"):
                value = f"{server['scheme']} {value}"
            header = f"{server['header']}:{value}"
            commands.append(["add", name, "--", str(bridge), server["url"], "--header", header,
                             "--silent", "--transport", "http-only"])  # fmt: skip
        elif "url" in server:
            commands.append(["add", name, server["url"]])
        else:
            env = [arg for k, v in server.get("env", {}).items() for arg in ("--env", f"{k}={v}")]
            commands.append(["add", *env, name, "--", *_command(server, home)])
        if server.get("opt_in"):
            disabled.append(["disable", name])
    return commands + disabled


def install_antigravity(
    skills: dict[str, Path], cache: Path, home: Path, agy: list[str], bridge: Path
) -> list[str]:
    _antigravity_roots(home)
    # Google's documented plugin.json + skills/ bundle; Superpowers installs as its own plugin.
    plugin = cache / "antigravity"
    _guard_roots(
        cache, plugin, plugin / "skills", plugin / "plugin.json", plugin / "LICENSE.tradermonty"
    )
    manifest = {
        "name": SPEC["runtimes"]["antigravity"]["plugin"],
        "version": "1.0.0",
        "description": "Sextant's pinned trading, compounding and TypeSafe skills",
    }
    states = [write_owned(plugin / "plugin.json", json.dumps(manifest, indent=2) + "\n")]
    superpowers = set(SPEC["shelf"]["superpowers@superpowers-dev"]["skills"])
    for name, path in skills.items():
        if name not in superpowers:
            states.append(copy_skill(path, plugin / "skills"))
    licence = (REPO / SPEC["shelf"]["trading"]["root"] / "LICENSE.tradermonty").read_text()
    states.append(write_owned(plugin / "LICENSE.tradermonty", licence))
    for directory in (cache / "superpowers", plugin):
        _native([*agy, "plugin", "install", str(directory)])
    for command in agy_mcp_commands(home, bridge):
        _native([*agy, "mcp", *command])
    return states


def preflight(home: Path) -> None:
    """Read-only guard for both shell installers, before their first runtime-directory write."""
    prefix = home / ".local/share/sextant-lane-runtimes"
    cache = home / ".local/share/sextant-runtime-skills"
    muse = home / ".local/share/muse-code"
    pins = json.loads((HERE / "pins.json").read_text())
    _guard_roots(home, prefix, cache, muse, home / ".local/bin")
    names = (
        "bin", "bin/antigravity", "mcp-bridge", "mcp-bridge/node_modules",
        "router", "router/node_modules", "router/node_modules/omniroute/.env", "router-data",
        "pins.json", "model-policy.json", "repository-path", "codex-launch.sh",
        "antigravity-launch.sh", "dsh-launch.sh", "muse-launch.sh", "router-start.sh",
        "routing.json", "mcp-bridge/package.json", "mcp-bridge/package-lock.json",
        "router/package.json", "router/package-lock.json",
    )  # fmt: skip
    _guard_roots(prefix, *(prefix / name for name in names))
    _guard_roots(muse, muse / f"muse-{pins['muse']['version']}")
    roots = [cache / row["cache"] for key, row in SPEC["shelf"].items() if key != "trading"]
    _guard_roots(
        cache,
        *roots,
        *(root / ".git" for root in roots),
        cache / "antigravity",
        cache / "antigravity/skills",
        cache / "antigravity/plugin.json",
        cache / "antigravity/LICENSE.tradermonty",
    )
    _codex_lane(home)
    _dsh_roots(Path(os.environ.get("DSH_HOME") or home / ".dsh"))
    _antigravity_roots(home)


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--runtime", action="append", choices=("codex", "dsh", "antigravity"))
    parser.add_argument("--check-roots", action="store_true", help="read-only installer preflight")
    args = parser.parse_args()
    home = Path.home()
    try:
        preflight(home)
    except REPORTED as error:
        print(f"parity: NOT_CONFIGURED: {error}", file=sys.stderr)
        return 1
    if args.check_roots:
        return 0
    prefix = home / ".local/share/sextant-lane-runtimes"
    cache = home / ".local/share/sextant-runtime-skills"
    repository = Path((prefix / "repository-path").read_text().strip())
    for name in ("CLAUDE_CODE_OAUTH_TOKEN", "ANTHROPIC_AUTH_TOKEN"):  # the launchers' rule
        os.environ.pop(name, None)
    os.environ["AGY_CLI_DISABLE_AUTO_UPDATE"] = "true"
    steps = {
        "codex": lambda skills: install_codex(skills, home),
        "dsh": lambda skills: install_dsh(
            skills,
            Path(os.environ.get("DSH_HOME") or home / ".dsh"),
            [str(home / ".local/share/sextant-agents/node_modules/.bin/dsh")],
            repository,
        ),
        "antigravity": lambda skills: install_antigravity(
            skills,
            cache,
            home,
            [str(prefix / "bin/antigravity/antigravity")],
            prefix / "mcp-bridge/node_modules/.bin/mcp-remote",
        ),
    }
    try:
        skills = shelf(cache)
    except REPORTED as error:
        print(f"shelf: NOT_CONFIGURED: {error}", file=sys.stderr)
        return 1
    failed = 0
    for runtime in args.runtime or list(steps):
        try:
            states = steps[runtime](skills)
        except REPORTED as error:
            print(f"{runtime}: NOT_CONFIGURED: {error}", file=sys.stderr)
            failed += 1
            continue
        counts = {state: states.count(state) for state in sorted(set(states))}
        print(f"{runtime}: {json.dumps(counts)}")
    print("Relaunch lane sessions at a safe checkpoint; running sessions keep their loaded shelf.")
    return 1 if failed else 0


if __name__ == "__main__":
    sys.exit(main())
