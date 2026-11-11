// Native node:test pattern already used by workflow-scenarios.mjs; no provider or GitHub calls.
const { test } = require('node:test');
const assert = require('node:assert/strict');
const { execFileSync } = require('node:child_process');
const fs = require('node:fs');
const path = require('node:path');
const gate = require('../../.github/gate/gate.cjs');

test('ordinary PRs never select an API-key reviewer', () => {
  for (const author of ['', 'codex', 'claude', 'muse', 'agy']) {
    assert.equal(gate.route(author ? `Agent-Family ${author}` : '', [{filename: 'docs/example.md'}]).quality, '');
  }
  assert.throws(() => gate.route('Agent-Family codex\nReview-Family claude', []));
  assert.throws(() => gate.route('Agent-Family claude\nReview-Family codex', []));
});

test('dsh authors get Codex by default and Claude only when selected', () => {
  assert.equal(gate.route('Agent-Family dsh', []).quality, 'codex');
  assert.equal(gate.route('Agent-Family dsh\nReview-Family codex', []).quality, 'codex');
  assert.equal(gate.route('Agent-Family dsh\nReview-Family claude', []).quality, 'claude');
  assert.throws(() => gate.route('Agent-Family dsh\nReview-Family dsh', []));
});

test('owner-named risk prefixes select one extra other-family review; Codex selects it', () => {
  for (const prefix of ['src/sextant/execution/', 'src/sextant/exits/', 'src/sextant/paper/',
    'src/sextant/risk/', 'src/sextant/alpaca/', 'lanes/paper/', 'lanes/live/', 'deploy/live/',
    'config/dsh/review-cmd/', '.github/']) {
    for (const field of ['filename', 'previous_filename']) {
      const files = [{filename: 'docs/moved.txt', [field]: `${prefix}example.py`}];
      assert.equal(gate.route('Agent-Family codex', files).quality, '', `${prefix}:${field}`);
      assert.equal(gate.route('Agent-Family claude', files).quality, 'codex', `${prefix}:${field}`);
      assert.equal(gate.route('Agent-Family dsh', files).quality, 'codex', `${prefix}:${field}`);
    }
  }
  assert.equal(gate.route('Agent-Family codex', [{filename: 'src/sextant/riskier/example.py'}]).quality, '');
  assert.equal(gate.route('Agent-Family claude', [{filename: 'dangerfile.js'}]).quality, 'codex');
  assert.equal(gate.route('Agent-Family codex\nReview-Family claude', [{filename: '.github/workflows/ci.yml'}]).quality, 'claude');
});

test('a Codex author on a risk path adds no API reviewer unless it selects one (D-34)', () => {
  const risk = [{filename: '.github/workflows/ci.yml'}];
  assert.equal(gate.route('Agent-Family codex', risk).quality, '');
  assert.equal(gate.route('Agent-Family codex\nReview-Family claude', risk).quality, 'claude');
  for (const author of ['dsh', 'claude', 'muse', 'agy']) {
    assert.equal(gate.route(`Agent-Family ${author}`, risk).quality, 'codex', author);
  }
  assert.throws(() => gate.route('Agent-Family codex\nReview-Family codex', risk));
});

test('ambiguous, malformed and conflicting markers fail closed', () => {
  for (const body of ['Agent-Family <family>', 'Agent-Family: dsh', 'Agent-Family  dsh',
    'Agent-Family codex\nAgent-Family dsh', 'Review-Family codex',
    'Agent-Family codex\nReview-Family codex', 'Agent-Family dsh\nReview-Family opus',
    'Agent-Family dsh\nReview-Family claude\nReview-Family codex',
    'Agent-Family codex2', 'Agent-Family dsh_foo', 'Agent-Family codexX',
    'Agent-Family dsh\nReview-Family codex2']) assert.throws(() => gate.route(body, []), body);
});

function fixture() {
  const head = 'a'.repeat(40);
  const base = 'd'.repeat(40);
  const statuses = [], outputs = {}, failures = [], reviews = [];
  const pr = {number: 7, state: 'open', head: {sha: head}, base: {ref: 'gate', sha: base, repo: {full_name: 'owner/project'}}};
  const diff = 'diff --git a/example.txt b/example.txt\n+snapshot\n';
  const github = {rest: {
    pulls: {get: async params => ({data: params?.mediaType?.format === 'diff' ? diff : pr}),
      listReviews: 'reviews', createReview: async args => reviews.push(args)},
    repos: {get: async () => ({data: {full_name: 'owner/project', default_branch: 'gate'}}),
      getCommit: async ({ref}) => ({data: {sha: ref, parents: [{sha: 'e'.repeat(40)}],
        commit: {tree: {sha: 'f'.repeat(40)}}}}),
      createCommitStatus: async status => statuses.push(status)},
  }, paginate: async () => []};
  const context = {repo: {owner: 'owner', repo: 'project'}, serverUrl: 'https://github.com', runId: 12,
    payload: {workflow_run: {conclusion: 'success'}}};
  const core = {setOutput: (name, value) => {outputs[name] = value;}, setFailed: value => failures.push(value)};
  return {github, context, core, head, pr, statuses, outputs, failures, reviews};
}

async function promptText(engine) {
  const f = fixture();
  const temporary = fs.mkdtempSync(path.join(process.cwd(), '.gate-prompt-test-'));
  const previous = process.env.RUNNER_TEMP;
  process.env.RUNNER_TEMP = temporary;
  try {
    await gate.prompt(f, 7, f.head, engine);
    return fs.readFileSync(f.outputs.file, 'utf8');
  } finally {
    if (previous === undefined) delete process.env.RUNNER_TEMP;
    else process.env.RUNNER_TEMP = previous;
    fs.rmSync(temporary, {recursive: true, force: true});
  }
}

test('source status cannot pass a moved head or failed CI', async () => {
  const f = fixture();
  f.pr.head.sha = 'b'.repeat(40);
  await gate.sourceResult(f, 7, f.head, 'success');
  assert.equal(f.statuses[0].state, 'failure');
  assert.equal(f.statuses[0].sha, f.head);
  assert.equal(f.outputs.ready, 'false');
  f.pr.head.sha = f.head;
  f.context.payload.workflow_run.conclusion = 'failure';
  await gate.sourceResult(f, 7, f.head, 'success');
  assert.equal(f.outputs.ready, 'false');
});

test('aggregate review requires dsh and every selected quality job', async () => {
  for (const [quality, results, expected] of [
    ['', {source: 'success', dsh: 'success', codex: 'skipped', claude: 'skipped'}, 'success'],
    ['', {source: 'success', dsh: 'failure'}, 'failure'],
    ['codex', {source: 'success', dsh: 'success', codex: 'skipped'}, 'failure'],
    ['codex', {source: 'success', dsh: 'success', codex: 'success'}, 'success'],
    ['claude', {source: 'success', dsh: 'success', claude: 'failure'}, 'failure'],
    ['', {source: 'failure', dsh: 'success'}, 'failure'],
  ]) {
    const f = fixture();
    await gate.finish(f, 7, f.head, results, quality);
    assert.equal(f.statuses[0].state, expected);
    assert.equal(f.statuses[0].sha, f.head);
    assert.equal(f.statuses[0].context, 'review');
  }
});

test('third-head mapping leaves P1 and lower nonblocking but keeps P0 blocking', () => {
  const report = priority => ({findings: [{title: `[P${priority}] Witness`, body: 'file:1, input, wrong output', priority}],
    overall_correctness: 'patch is incorrect', overall_explanation: 'A concrete witness.'});
  assert.equal(gate.qualityPassed(report(1)), false);
  assert.equal(gate.qualityPassed(report(1), 0), true);
  assert.equal(gate.qualityPassed(report(0), 0), false);
  assert.equal(gate.qualityPassed(report(2)), true);
});

// #205's Codex final message, verbatim: the action succeeded, but the review did not run.
const HOLLOW_205 = '{"findings": [], "overall_correctness": "patch is incorrect", ' +
  '"overall_explanation": "Review of 2f023a0653baba6801a2ea9ffcf559a9bd10c208 could not be completed: ' +
  'GitHub CLI authentication is unavailable and api.github.com does not resolve. Required context and ' +
  'upstream sources could not be verified; this reports a review failure, not a demonstrated patch defect.", ' +
  '"overall_confidence_score": 0.03}';

test('a review that did not run fails closed on its empty findings (#205 fixture)', async () => {
  const f = fixture();
  await gate.qualityResult(f, 7, f.head, HOLLOW_205, 'codex');
  assert.equal(f.outputs.passed, 'false');
  assert.equal(f.reviews.length, 0);
  assert.equal(f.failures.length, 1);
  assert.match(f.failures[0], /did not run/i);
  assert.match(f.failures[0], /reports no findings/i);
  assert.match(f.failures[0], /network or authentication fault/);
});

test('a hollow verdict with other wording also fails closed on its empty findings', async () => {
  const f = fixture();
  const text = JSON.stringify({findings: [], overall_correctness: 'patch is incorrect',
    overall_explanation: 'I was unable to review the diff; required context was unavailable.'});
  await gate.qualityResult(f, 7, f.head, text, 'codex');
  assert.equal(f.outputs.passed, 'false');
  assert.equal(f.reviews.length, 0);
  assert.equal(f.failures.length, 1);
  assert.match(f.failures[0], /did not run/i);
  assert.match(f.failures[0], /reports no findings/i);
});

test('a real verdict that describes an incomplete patch evaluates normally', async () => {
  const report = {
    findings: [{title: '[P1] Migration omits the new table', body: 'file:1, input, wrong output', priority: 1}],
    overall_correctness: 'patch is incorrect',
    overall_explanation: 'The migration could not be completed because the new table is missing; the review found one P1.',
  };
  assert.equal(gate.qualityPassed(report, 0), true);
  const f = fixture();
  await gate.qualityResult(f, 7, f.head, JSON.stringify(report), 'codex');
  assert.equal(f.reviews.length, 1);
  assert.equal(f.outputs.passed, 'false');
  assert.deepEqual(f.failures, ['The other-family quality review requires changes']);
});

test('missing and unreadable verdicts fail closed with named reasons', async () => {
  for (const [text, reason] of [
    ['', /verdict is missing/i],
    ['not json', /verdict is unreadable/i],
    ['{"findings": [], "overall_correctness": "unknown", "overall_explanation": "x"}', /verdict is unreadable/i],
  ]) {
    const f = fixture();
    await gate.qualityResult(f, 7, f.head, text, 'codex');
    assert.equal(f.outputs.passed, 'false');
    assert.equal(f.reviews.length, 0);
    assert.equal(f.failures.length, 1);
    assert.match(f.failures[0], reason);
  }
});

test('a valid structured verdict still posts one review and passes', async () => {
  const f = fixture();
  const text = JSON.stringify({findings: [], overall_correctness: 'patch is correct',
    overall_explanation: 'No blocking findings.'});
  await gate.qualityResult(f, 7, f.head, text, 'codex');
  assert.equal(f.failures.length, 0);
  assert.equal(f.outputs.passed, 'true');
  assert.equal(f.reviews.length, 1);
  assert.match(f.reviews[0].body, new RegExp(`^Review of ${f.head}`));
});

test('the API-only guard refuses execution and publishing for another head or PR', async () => {
  const {refusal} = await import('../../.github/gate/bundles/api-only/index.mjs');
  const env = {DSH_GATE_REPO: 'owner/project', DSH_GATE_PR: '7', DSH_GATE_HEAD: 'a'.repeat(40)};
  for (const name of ['bash', 'pwsh', 'write', 'edit', 'apply_patch', 'terminal_send']) assert.ok(refusal({name}, env));
  assert.equal(refusal({name: 'gh_file'}, env), undefined);
  const args = {task: 'publish', pr: 'owner/project#7', body: `Review of ${env.DSH_GATE_HEAD}`, findings: []};
  assert.equal(refusal({name: 'ci_run', arguments: args}, env), undefined);
  assert.ok(refusal({name: 'ci_run', arguments: {...args, pr: '8'}}, env));
  assert.ok(refusal({name: 'ci_run', arguments: {...args, pr: '7', ownerRepo: 'owner/project'}}, env));
  assert.ok(refusal({name: 'ci_run', arguments: {...args, pr: 'another/repo#7'}}, env));
  assert.ok(refusal({name: 'ci_run', arguments: {...args, task: 'review'}}, env));
  assert.ok(refusal({name: 'ci_run', arguments: {...args, body: 'Review of another head'}}, env));
});

test('the API-only guard reads the whole analysis pass only from the named spill root', async () => {
  const {refusal} = await import('../../.github/gate/bundles/api-only/index.mjs');
  const env = {DSH_GATE_REPO: 'owner/project', DSH_GATE_PR: '7', DSH_GATE_HEAD: 'a'.repeat(40),
    DSH_GITHUB_CI_OUTPUT_DIR: '/tmp/gate-output'};
  const spill = '/tmp/gate-output/spill/session-0123456789ab/0123456789ab-ci_run.txt';
  const read = (file_path) => refusal({name: 'read', arguments: {file_path}}, env);
  // The documented spill retrieval path is allowed, and nothing beyond the root is.
  assert.equal(read(spill), undefined);
  assert.ok(read('/tmp/gate-output/spill/../dsh-github-ci.cordis.yml'));
  assert.ok(read('/tmp/gate-output/spill-other/session-0123456789ab/file.txt'));
  assert.ok(read('/tmp/gate-output/notes.txt'));
  assert.ok(read('/tmp/other/spill/session-0123456789ab/file.txt'));
  assert.ok(read('/etc/passwd'));
  assert.ok(refusal({name: 'read'}, env));
  const grep = (args) => refusal({name: 'grep', arguments: args}, env);
  assert.equal(grep({pattern: 'diff', path: spill}), undefined);
  assert.ok(grep({pattern: 'diff', path: '/tmp/gate-output'}));
  assert.ok(grep({pattern: 'diff'}));
  // The pinned grep consumes `path` (dsh-tool-fs-search lib/index.js:924-931,
  // :1155); a conflicting `file_path` or a missing `path` must not be admitted.
  assert.ok(grep({pattern: 'diff', file_path: spill, path: '/outside-spill/proof.txt'}));
  assert.ok(grep({pattern: 'diff', file_path: spill}));
  // The pinned read consumes `file_path` (dsh-tool-fs lib/index.js:232-237);
  // an extra `path` inside the root must not authorize it.
  assert.ok(refusal({name: 'read', arguments: {path: spill}}, env));
  // Without the configured root, no local read is admitted.
  assert.ok(refusal({name: 'read', arguments: {file_path: spill}},
    {...env, DSH_GITHUB_CI_OUTPUT_DIR: ''}));
});

test('the actual dsh prompt uses the upstream qualified PR reference accepted by its guard', async () => {
  const f = fixture();
  const temporary = fs.mkdtempSync(path.join(process.cwd(), '.gate-prompt-test-'));
  const previous = process.env.RUNNER_TEMP;
  process.env.RUNNER_TEMP = temporary;
  try {
    await gate.prompt(f, 7, f.head, 'dsh');
    const match = f.outputs.text.match(/first call ci_run with (\{[^\n]*?\})/);
    assert.ok(match);
    const args = JSON.parse(match[1]);
    assert.deepEqual(args, {task: 'analyze', pr: 'owner/project#7'});
    const {refusal} = await import('../../.github/gate/bundles/api-only/index.mjs');
    assert.equal(refusal({name: 'ci_run', arguments: args},
      {DSH_GATE_REPO: 'owner/project', DSH_GATE_PR: '7', DSH_GATE_HEAD: f.head}), undefined);
    // The prompt names the upstream spill recovery, and keeps the fail-closed rule.
    assert.match(f.outputs.text, /spill file/i);
    assert.match(f.outputs.text, /offset/);
    assert.match(f.outputs.text, /limit/);
    assert.match(f.outputs.text, /error finding/i);
    assert.match(f.outputs.text, /do not approve unseen changes/i);
  } finally {
    if (previous === undefined) delete process.env.RUNNER_TEMP;
    else process.env.RUNNER_TEMP = previous;
    fs.rmSync(temporary, {recursive: true, force: true});
  }
});

test('the Codex reviewer is told the job is snapshot-only and offline', async () => {
  const text = await promptText('codex');
  assert.match(text, /snapshot-only/i);
  assert.match(text, /no GitHub token/i);
  assert.match(text, /denies network access/i);
  assert.match(text, /not a review failure/i);
  assert.match(text, /do not call gh, curl, the GitHub API or any network client/i);
  assert.match(text, /do not run repository setup commands such as `just session-start`/i);
  assert.match(text, /Never check out, import or execute PR code/);
  assert.ok(!text.includes('Read PR content only through the GitHub API'));
  assert.ok(text.includes('"diff"'));
  assert.ok(text.includes('a'.repeat(40)));
});

test('the dsh reviewer keeps the GitHub-API-only instruction for its ci_run pipeline', async () => {
  const text = await promptText('dsh');
  assert.match(text, /Read PR content only through the GitHub API/);
  assert.ok(!/snapshot-only/i.test(text));
});

test('the Codex reviewer prompt carries no unqualified GitHub-API read instruction', async () => {
  const text = await promptText('codex');
  const normalized = text.replace(/\s+/g, ' ');
  assert.ok(!normalized.includes('Read its diff through the GitHub API'),
    'review.md API-read sentence leaks into the offline prompt');
  assert.ok(!normalized.includes('Read PR content only through the GitHub API'),
    'shared API-read sentence leaks into the offline prompt');
  assert.ok(!text.includes('Trusted default-branch guidelines: .github/workflows/review.md'),
    'review.md is not part of the offline reviewer guidelines');
});

test('all nonblocking findings share one follow-up issue across heads and families', async () => {
  const f = fixture(), issues = [];
  let creates = 0, updates = 0;
  f.github.rest.issues = {
    listForRepo: 'issues',
    create: async args => {creates++; issues.push({...args, number: 9, user: {login: 'github-actions[bot]'}});},
    update: async args => {updates++; Object.assign(issues[0], args);},
  };
  f.github.paginate = async () => issues;
  const notes = {dsh: JSON.stringify(['[P2] docs follow-up']), codex: JSON.stringify(['[P3] style follow-up'])};
  await gate.followupIssue(f.github, f.context, 7, f.head, notes);
  await gate.followupIssue(f.github, f.context, 7, f.head, notes);
  await gate.followupIssue(f.github, f.context, 7, 'b'.repeat(40), notes);
  assert.equal(creates, 1);
  assert.equal(updates, 1);
  assert.match(issues[0].body, /docs follow-up/);
  assert.match(issues[0].body, /style follow-up/);
  assert.match(issues[0].body, new RegExp('b'.repeat(40)));
});

test('review attempt repeats do not count as another reviewed commit', async () => {
  const f = fixture();
  const review = sha => ({user: {login: 'github-actions[bot]'}, body: `Review of ${sha}`});
  f.github.paginate = async () => [review('b'.repeat(40)), review('b'.repeat(40)), review(f.head),
    {user: {login: 'someone'}, body: `Review of ${'c'.repeat(40)}`}];
  assert.equal(await gate.blockingPriority(f.github, f.context, 7, f.head), 1);
  f.github.paginate = async () => [review('b'.repeat(40)), review('c'.repeat(40))];
  assert.equal(await gate.blockingPriority(f.github, f.context, 7, f.head), 0);
});

// #208 (ffb2f9a -> a896245 -> ca98e85) and #201 (39a8349 -> 74c9e59 -> 8457400)
// were updated by GitHub's "Update a pull request branch" endpoint, which merges
// HEAD from the base branch into the pull request branch (read 2026-11-09).
// The merge's parents and tree identify it: two parents, the previous head then
// the base tip at merge time, and a tree different from the previous head's. The
// base advances afterwards, so that tip is the current base tip or an ancestor
// of it: 74c9e59 merged f7314de, which eb5236b later contains; 8457400 merged
// eb5236b itself.
function witness() {
  const sha = {
    ffb2f9a: 'ffb2f9afd6ccc39f69b24a7c11b9bf0acaf68cbd',
    a896245: 'a8962450e32d67232fa6412094a690c95182f4d8',
    ca98e85: 'ca98e85040be737bd04611cae28f4d4fcd3371bf',
    eb5236b: 'eb5236b853297839034c2854130c932019a7c473',
    d364e47b: 'd364e47bd209776f8e89c86dbda8204f8d70b3e7',
    c39a8349: '39a8349bfb1fa06dc49db2b7f29b221b46d3086a',
    p39a8349: '9abd6daa15758b47d7f285a06d96bf6d315048af',
    f7314de: 'f7314de7e2cd904db65974a0de940a804a5fb2e0',
    c74c9e59: '74c9e59c06a681751d89f262e9a037248f04e3e4',
    d8457400: '845740082da78ddb9d7e9489a6fa074441ff2921',
    later: '9'.repeat(40),
  };
  const trees = {
    ffb2f9a: '6e9af1765e1f09ea9ab480170668f53bf35509cd',
    a896245: '6d0f62e4489eb708b65304c1ed47a91a7673b91f',
    ca98e85: '1aebd089ad23ab816fd3c974cdbea52d7a3efa3c',
    c39a8349: '8dba23d1c3162de80d0f9d5c66509dd9e7b2c479',
    c74c9e59: '21c862cbceca4a9f298fc7db0fbeb834840815d6',
    d8457400: '2be8d441f9a0339fbe1ae6bf26b2998fa20cb8a0',
    later: '7'.repeat(40),
  };
  const commits = {};
  const add = (name, parents) => { commits[sha[name]] = {data: {sha: sha[name],
    parents: parents.map(parent => ({sha: sha[parent]})), commit: {tree: {sha: trees[name]}}}}; };
  add('ffb2f9a', ['d364e47b']);
  add('a896245', ['ffb2f9a']);
  add('ca98e85', ['a896245', 'eb5236b']);
  add('c39a8349', ['p39a8349']);
  add('c74c9e59', ['c39a8349', 'f7314de']);
  add('d8457400', ['c74c9e59', 'eb5236b']);
  add('later', ['d8457400']);
  const reviews = names => names.map(name => ({user: {login: 'github-actions[bot]'},
    body: `Review of ${sha[name]}`}));
  const install = (f, head, base = sha.eb5236b) => {
    f.pr.head.sha = sha[head];
    f.pr.base.sha = base;
    f.github.rest.repos.getCommit = async ({ref}) => {
      assert.ok(commits[ref], `unexpected commit read: ${ref}`);
      return commits[ref];
    };
    const ancestry = {[`${sha.f7314de}...${sha.eb5236b}`]: 'ahead'};
    f.github.rest.repos.compareCommitsWithBasehead = async ({basehead}) => {
      const [base, tip] = basehead.split('...');
      const status = base === tip ? 'identical' : ancestry[basehead];
      assert.ok(status, `unexpected base comparison: ${basehead}`);
      return {data: {status}};
    };
  };
  return {sha, install, reviews};
}

test('the reviewed-head count takes only heads the author changed (#208, #201)', async () => {
  const {sha, install, reviews} = witness();
  const at208 = fixture();
  install(at208, 'ca98e85');
  at208.github.paginate = async () => reviews(['ffb2f9a', 'a896245', 'ca98e85']);
  assert.equal(await gate.reviewedHeadCount(at208.github, at208.context, 7, sha.ca98e85), 2);

  const at201 = fixture();
  install(at201, 'd8457400');
  at201.github.paginate = async () => reviews(['c39a8349', 'c74c9e59', 'd8457400']);
  assert.equal(await gate.reviewedHeadCount(at201.github, at201.context, 7, sha.d8457400), 1);
});

test('a base-branch update merge keeps the P0/P1 bar for the next author head (#208, #201)', async () => {
  const {sha, install, reviews} = witness();
  const at208 = fixture();
  install(at208, 'ca98e85');
  at208.github.paginate = async () => reviews(['ffb2f9a', 'a896245', 'ca98e85']);
  assert.equal(await gate.blockingPriority(at208.github, at208.context, 7, sha.ca98e85), 1);

  const at201 = fixture();
  install(at201, 'd8457400');
  at201.github.paginate = async () => reviews(['c39a8349', 'c74c9e59', 'd8457400']);
  assert.equal(await gate.blockingPriority(at201.github, at201.context, 7, sha.d8457400), 1);

  const later = fixture();
  install(later, 'later');
  later.github.paginate = async () => reviews(['c39a8349', 'c74c9e59', 'd8457400']);
  assert.equal(await gate.reviewedHeadCount(later.github, later.context, 7, sha.later), 2);
  assert.equal(await gate.blockingPriority(later.github, later.context, 7, sha.later), 1);
});

test('two base updates in a row never count, even after the base moves', async () => {
  const {sha, install, reviews} = witness();
  const afterFirst = reviews(['c39a8349', 'c74c9e59']);
  const afterSecond = reviews(['c39a8349', 'c74c9e59', 'd8457400']);

  // At tip A the first update merge is an update.
  const atA = fixture();
  install(atA, 'c74c9e59', sha.f7314de);
  atA.github.paginate = async () => afterFirst;
  assert.equal(await gate.reviewedHeadCount(atA.github, atA.context, 7, sha.c74c9e59), 1);

  // Once main advances to B the earlier update stays an update, not an author head.
  const movedBase = fixture();
  install(movedBase, 'c74c9e59');
  movedBase.github.paginate = async () => afterFirst;
  assert.equal(await gate.reviewedHeadCount(movedBase.github, movedBase.context, 7, sha.c74c9e59), 1);

  // A second update merge at B is also an update; the count stays 1.
  const atB = fixture();
  install(atB, 'd8457400');
  atB.github.paginate = async () => afterSecond;
  assert.equal(await gate.reviewedHeadCount(atB.github, atB.context, 7, sha.d8457400), 1);

  // The next author head counts once, so the P0/P1 bar still applies.
  const later = fixture();
  install(later, 'later');
  later.github.paginate = async () => afterSecond;
  assert.equal(await gate.reviewedHeadCount(later.github, later.context, 7, sha.later), 2);
  assert.equal(await gate.blockingPriority(later.github, later.context, 7, sha.later), 1);
});

// A base update is only a clean one when its merge tree equals the clean merge of
// its parents (git merge-tree --write-tree, read 2026-11-09). #208's ca98e85 and
// #201's 8457400 are such merges. A merge that also carries author changes has a
// different tree and is an author head: its own review decides, and the earlier
// head's failed review is not carried into it.
function mergeRepo() {
  const dir = fs.mkdtempSync(path.join(process.cwd(), '.gate-merge-test-'));
  const run = (...args) => execFileSync('git', args, {cwd: dir, encoding: 'utf8'}).trim();
  const write = (file, text) => fs.writeFileSync(path.join(dir, file), text);
  const commit = (file, message) => {
    run('add', file);
    run('commit', '-q', '-m', message);
    return run('rev-parse', 'HEAD');
  };
  const merge = (ref, message) => {
    run('merge', '-q', '--no-ff', '-m', message, ref);
    return run('rev-parse', 'HEAD');
  };
  const data = ref => ({
    sha: run('rev-parse', ref),
    parents: run('rev-list', '--parents', '-n', '1', ref).split(' ').slice(1).map(sha => ({sha})),
    commit: {tree: {sha: run('rev-parse', `${ref}^{tree}`)}},
  });
  run('init', '-q', '-b', 'main');
  run('config', 'user.name', 'Gate Test');
  run('config', 'user.email', 'gate@example.com');
  write('root.txt', 'root\n');
  const root = commit('root.txt', 'root');
  run('checkout', '-q', '-b', 'author');
  write('author.txt', 'author\n');
  const author = commit('author.txt', 'author');
  run('checkout', '-q', '-b', 'base', root);
  write('base.txt', 'base\n');
  const base = commit('base.txt', 'base');
  run('checkout', '-q', 'author');
  const merge1 = merge('base', 'Merge base into author');
  run('checkout', '-q', 'base');
  write('base-two.txt', 'base two\n');
  const base2 = commit('base-two.txt', 'base two');
  run('checkout', '-q', 'author');
  const merge2 = merge(base2, 'Merge base two into author');
  run('checkout', '-q', '-b', 'dirty', merge1);
  run('merge', '-q', '--no-commit', base2);
  write('review-fix.txt', 'authored fix\n');
  run('add', 'review-fix.txt');
  run('commit', '-q', '-m', 'Merge base two with an authored fix');
  const dirty = run('rev-parse', 'HEAD');
  const install = f => {
    f.pr.base.sha = base2;
    f.github.rest.repos.getCommit = async ({ref}) => ({data: data(ref)});
    f.github.rest.repos.compareCommitsWithBasehead = async ({basehead}) => {
      const [from, to] = basehead.split('...');
      let status = 'diverged';
      try {
        execFileSync('git', ['merge-base', '--is-ancestor', from, to], {cwd: dir});
        status = 'ahead';
      } catch { /* not an ancestor */ }
      assert.equal(status, 'ahead', `unexpected base comparison: ${basehead}`);
      return {data: {status}};
    };
  };
  const statuses = (f, byRef) => {
    f.github.paginate = async (method, params) => byRef[params.ref] || [];
  };
  return {dir, sha: {root, author, base, base2, merge1, merge2, dirty}, data, install, statuses};
}

test("a clean base-merge head cannot clear the last author head's completed blocking review (#208, #201)", async () => {
  const cwd = process.cwd();
  const f = fixture();
  const repo = mergeRepo();
  try {
    repo.install(f);
    f.pr.head.sha = repo.sha.merge2;
    repo.statuses(f, {[repo.sha.author]: [{context: 'review-blocking-dsh', state: 'failure',
      description: 'The dsh review completed with a blocking finding'}]});
    process.chdir(repo.dir);
    await gate.finish(f, 7, repo.sha.merge2, {source: 'success', dsh: 'success'}, '');
    assert.equal(f.statuses.at(-1).state, 'failure');
    assert.match(f.statuses.at(-1).description, /last author head/);
    assert.equal(f.failures.length, 1);
    assert.match(f.failures[0], /last author head/);

    // A newer passing review at the author head clears it, as a fresh review should.
    const cleared = fixture();
    repo.install(cleared);
    cleared.pr.head.sha = repo.sha.merge2;
    repo.statuses(cleared, {[repo.sha.author]: [
      {context: 'review-blocking-dsh', state: 'success'},
      {context: 'review-blocking-dsh', state: 'failure'},
    ]});
    await gate.finish(cleared, 7, repo.sha.merge2, {source: 'success', dsh: 'success'}, '');
    assert.equal(cleared.statuses.at(-1).state, 'success');
    assert.equal(cleared.failures.length, 0);
  } finally {
    process.chdir(cwd);
    fs.rmSync(repo.dir, {recursive: true, force: true});
  }
});

test('a stale run failure at the last author head is not carried to a clean base merge', async () => {
  const cwd = process.cwd();
  const f = fixture();
  const repo = mergeRepo();
  try {
    repo.install(f);
    f.pr.head.sha = repo.sha.merge2;
    // M landed while A was still reviewed: A's run then failed the stale-head check and
    // published a generic failure. No reviewer completed a blocking verdict at A.
    repo.statuses(f, {[repo.sha.author]: [{context: 'review', state: 'failure',
      description: 'A required review failed, was skipped or did not complete'}]});
    process.chdir(repo.dir);
    await gate.finish(f, 7, repo.sha.merge2, {source: 'success', dsh: 'success'}, '');
    assert.equal(f.statuses.at(-1).state, 'success');
    assert.equal(f.failures.length, 0);
  } finally {
    process.chdir(cwd);
    fs.rmSync(repo.dir, {recursive: true, force: true});
  }
});

test('only a completed review verdict records the carry status', async () => {
  const blocking = fixture();
  await gate.qualityResult(blocking, 7, blocking.head, JSON.stringify({
    findings: [{title: '[P1] The gate copies a stale run', body: 'file:1, wrong output', priority: 1}],
    overall_correctness: 'patch is incorrect',
    overall_explanation: 'One blocking finding.',
  }), 'codex');
  assert.equal(blocking.statuses.find(status => status.context === 'review-blocking-codex')?.state, 'failure');

  const hollow = fixture();
  await gate.qualityResult(hollow, 7, hollow.head, HOLLOW_205, 'codex');
  assert.equal(hollow.statuses.length, 0);

  const passing = fixture();
  await gate.qualityResult(passing, 7, passing.head, JSON.stringify({
    findings: [], overall_correctness: 'patch is correct', overall_explanation: 'No blocking findings.',
  }), 'claude');
  assert.equal(passing.statuses.find(status => status.context === 'review-blocking-claude')?.state, 'success');

  const dir = fs.mkdtempSync(path.join(process.cwd(), '.gate-dsh-verdict-'));
  try {
    const file = path.join(dir, 'report.json');
    const write = report => fs.writeFileSync(file, JSON.stringify(report));
    const dsh = fixture();
    write({headSha: dsh.head, verdict: 'needs-changes', truncated: false, engine: 'model',
      findings: [{severity: 'error', file: 'a.cjs', line: 1, rule: 'r', message: 'm'}]});
    await gate.dshResult(dsh, 7, dsh.head, file);
    assert.equal(dsh.statuses.find(status => status.context === 'review-blocking-dsh')?.state, 'failure');

    const truncated = fixture();
    write({headSha: truncated.head, verdict: 'needs-changes', truncated: true, engine: 'model', findings: []});
    await gate.dshResult(truncated, 7, truncated.head, file);
    assert.equal(truncated.statuses.length, 0);
  } finally {
    fs.rmSync(dir, {recursive: true, force: true});
  }
});

test('a merge that carries an authored fix is a fresh author head, not a base update (#208, #201)', async () => {
  const cwd = process.cwd();
  const f = fixture();
  const repo = mergeRepo();
  try {
    repo.install(f);
    assert.equal(await gate.isCleanBaseMerge(f.github, f.context, f.pr, repo.data(repo.sha.author), repo.dir), false);
    assert.equal(await gate.isCleanBaseMerge(f.github, f.context, f.pr, repo.data(repo.sha.merge1), repo.dir), true);
    assert.equal(await gate.isCleanBaseMerge(f.github, f.context, f.pr, repo.data(repo.sha.dirty), repo.dir), false);
    assert.equal(await gate.lastAuthorHead(f.github, f.context, f.pr, repo.sha.merge2, repo.dir), repo.sha.author);
    assert.equal(await gate.lastAuthorHead(f.github, f.context, f.pr, repo.sha.dirty, repo.dir), null);

    repo.statuses(f, {[repo.sha.author]: [{context: 'review', state: 'failure'}]});
    f.pr.head.sha = repo.sha.dirty;
    process.chdir(repo.dir);
    await gate.finish(f, 7, repo.sha.dirty, {source: 'success', dsh: 'success'}, '');
    assert.equal(f.statuses.at(-1).state, 'success');
    assert.equal(f.failures.length, 0);
  } finally {
    process.chdir(cwd);
    fs.rmSync(repo.dir, {recursive: true, force: true});
  }
});
