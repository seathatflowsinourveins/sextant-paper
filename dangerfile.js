// The repo's first rule, enforced on every PR: never self-invent. Runs from the base branch.
// Framework: danger/danger-js@14.1.0. Rules below only read the PR's own body and file list.
const { danger, fail, schedule } = require("danger");

// `danger local` runs this file without PR metadata; `just pr` passes the body through
// LOCAL_PR_BODY_FILE and the check reads the manifest from the commit it would push.
const local = !danger.github;
const pr = local ? { user: { login: "local" } } : danger.github.pr;
const body = local ? localBody() : (pr.body ?? "");
function localBody() {
  const path = process.env.LOCAL_PR_BODY_FILE;
  if (!path) {
    fail("PR body: LOCAL_PR_BODY_FILE is not set; run `bash scripts/pr-check.sh <body.md>` or `just pr`.");
    return "";
  }
  try {
    return require("fs").readFileSync(path, "utf8");
  } catch (error) {
    fail(`PR body: LOCAL_PR_BODY_FILE ${path} cannot be read: ${error.message}`);
    return "";
  }
}
function localManifest() {
  try {
    return require("child_process").execSync("git show HEAD:docs/manifest.md", { encoding: "utf8" });
  } catch (error) {
    fail(`Manifest row: git show HEAD:docs/manifest.md failed: ${error.message}`);
    return "";
  }
}
const section = (name) => {
  const match = body.match(new RegExp(`^## ${name}\\s*$([\\s\\S]*?)(?=^## |(?![\\s\\S]))`, "m"));
  return (match?.[1] ?? "").replace(/<!--[\s\S]*?-->/g, "").trim();
};

const sources = section("Upstream references");
// `owner/repo@tag`, or the manifest's own `[owner/repo](url)@tag`, then the full commit SHA.
const repoAtCommit = /[\w.-]+\/[\w.-]+(?:\]\([^)\s]*\))?@\S*[\s\S]*?\b[0-9a-f]{40}\b/;
const docsWithDate = /https?:\/\/\S+[\s\S]*?read 20\d\d-\d\d-\d\d/;
const paper = /(doi\.org\/10\.|\bdoi:10\.|arxiv\.org\/abs\/|\barXiv:\d)/i;

// Dependabot's generated body cites the upstream's own release notes, and it changes pins only.
if (pr.user.login !== "dependabot[bot]") {
  for (const name of ["What changed", "Manifest row", "Upstream references", "How verified", "How to revert"]) {
    if (!section(name)) fail(`PR body: "${name}" is empty.`);
  }

  if (!repoAtCommit.test(sources) && !docsWithDate.test(sources) && !paper.test(sources)) {
    fail(
      "Never self-invent: Upstream references must name a source repo@tag with its full commit SHA, " +
        "a docs page with `read YYYY-MM-DD`, or a paper (DOI or arXiv).",
    );
  }

  const named = [...section("Manifest row").matchAll(/"([^"]+)"/g)].map((match) => match[1]);
  schedule(async () => {
    const manifest = local
      ? localManifest()
      : await danger.github.utils.fileContents("docs/manifest.md");
    const rows = manifest.split("\n").filter((row) => !row.startsWith("| Practice or pick |"));
    if (!named.some((name) => rows.some((row) => row.startsWith(`| ${name} |`)))) {
      fail('Manifest row: name a row of docs/manifest.md in "double quotes". This PR may add it.');
    }

    // A third-party plugin row carries the disclosure its admission depends on.
    const register = manifest.split(/^## /m).find((part) => part.startsWith("Third-party plugins\n"));
    if (!register) return;
    const pluginRows = register
      .split("\n")
      .filter((row) => row.startsWith("| ") && !row.startsWith("| Practice or pick |") && !row.startsWith("|---"));
    for (const name of named) {
      const row = pluginRows.find((line) => line.startsWith(`| ${name} |`));
      if (row && !/capabilit|red line|red-line/i.test(row)) {
        fail(`Third-party plugin row "${name}" must record its capability and red-line disclosure (docs/handbook/plugins.md).`);
      }
    }
  });
}

const changed = [...danger.git.created_files, ...danger.git.modified_files, ...(danger.git.deleted_files ?? [])];
const touchesStrategy = changed.some((path) => path.startsWith("src/sextant/strategies/"));
if (touchesStrategy && !repoAtCommit.test(sources) && !paper.test(sources)) {
  fail("Never self-invent: a strategy starts from an upstream template at a pinned commit or a cited paper.");
}

// One upstream owns each surface (docs/handbook/sources.md). A governed path must name it,
// so a fork or a second SDK fails here instead of landing as a quiet second owner.
const AUTHORITY = {
  engine: {
    paths: ["src/sextant/exits/", "src/sextant/execution/", "src/sextant/risk/", "src/sextant/backtest/"],
    owners: [
      "nautechsystems/nautilus_trader",
      "alpacahq/alpaca-py",
      "docs.alpaca.markets",
    ],
  },
  broker: {
    paths: ["src/sextant/paper/", "src/sextant/alpaca/", "lanes/paper/"],
    owners: ["alpacahq/alpaca-py", "docs.alpaca.markets", "nautechsystems/nautilus_trader"],
  },
  data: {
    paths: ["src/sextant/data/"],
    owners: [
      "alpacahq/alpaca-py",
      "sharadar.com",
      "apache/arrow",
      "psf/requests",
    ],
  },
  tradingview: {
    paths: ["src/sextant/tradingview/", "lanes/tradingview/"],
    owners: ["ferroxlabs/tvcontrol", "tradingview.com"],
  },
  harness: {
    paths: ["deploy/wsl/"],
    owners: [
      "deepseek-ai/deepseek-harness",
      "trailofbits/claude-code-devcontainer",
      "everyinc/compound-engineering-plugin",
      "obra/superpowers",
      "typesafe-ai/skills",
      "ccch1mneyyy/dsh-tui",
      "anthropics/claude-code",
      "stablyai/orca",
      "orchestratorinc/agent-orchestrator",
      "danger/danger-js",
      "volcengine/openviking",
      "ggml-org/llama.cpp",
    ],
  },
  lanes: {
    paths: ["lanes/"],
    owners: [
      "nautechsystems/nautilus_trader",
      "alpacahq/alpaca-py",
      "sharadar.com",
      "tradingview.com",
    ],
  },
};
if (pr.user.login !== "dependabot[bot]") {
  const cited = sources.toLowerCase();
  const escape = (text) => text.replace(/[.*+?^${}()|[\]\\]/g, "\\$&");
  const names = (owner) => {
    // A repo is cited as owner/repo, in a link's text, or in a URL path; a domain stands alone.
    return new RegExp(`(^|[^\\w.-])${escape(owner)}(?![\\w.-])`);
  };
  for (const [surface, rule] of Object.entries(AUTHORITY)) {
    const path = changed.find((file) => rule.paths.some((prefix) => file.startsWith(prefix)));
    if (!path) continue;
    if (!rule.owners.some((owner) => names(owner).test(cited))) {
      fail(
        `Never self-invent: ${path} is governed by the ${surface} surface, but Upstream references ` +
          `name no authority for it. Cite one of ${rule.owners.join(", ")} (docs/handbook/sources.md).`,
      );
    }
  }
}
