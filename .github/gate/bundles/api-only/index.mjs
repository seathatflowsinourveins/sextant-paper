// Same monotonic guard as config/dsh/hard-lines/hard-lines.mjs:2-7 and apply().
// deepseek-harness@66491b05, packages/core/tools/README.md:92 and src/contracts.ts:391.
// Tool names/arguments: PerryLink/dsh-github@2fc0009c, src/ci/tool.ts:42-67 and src/tools.ts.
// Repository-bearing PR reference: src/ci/tool.ts:123-126 and src/state.ts parsePrRef.
// Spill root and read/grep retrieval: deepseek-harness@66491b05,
// packages/spill/spill-local/README.md:32,50 and packages/spill/spill-policy/README.md:47-54.
// The consumed path field is per tool: read uses `file_path`
// (@deepseek-ai/dsh-tool-fs@0.2.2-rc.1 lib/index.js:232-237,254-257); grep uses
// `path` (@deepseek-ai/dsh-tool-fs-search@0.2.2-rc.1 lib/index.js:924-931,1155).
import { resolve, sep } from 'node:path';
export const name = 'sextant-gate-api-only';
export const inject = ['tools'];
const reads = new Set(['gh_file', 'gh_repo', 'gh_review', 'gh_search']);
const spillPathFields = new Map([['read', 'file_path'], ['grep', 'path']]);
function spillRoot(env) {
  const output = env.DSH_GITHUB_CI_OUTPUT_DIR;
  return typeof output === 'string' && output !== '' ? resolve(output, 'spill') : '';
}
function withinSpillRoot(execution, root) {
  if (root === '') return false;
  const field = spillPathFields.get(execution.name);
  if (field === undefined) return false;
  const args = execution.arguments || {};
  const requested = args[field];
  if (typeof requested !== 'string' || requested === '') return false;
  const target = resolve(requested);
  return target === root || target.startsWith(root + sep);
}
export function refusal(execution, env = process.env) {
  if (reads.has(execution.name)) return undefined;
  if (spillPathFields.has(execution.name)) {
    return withinSpillRoot(execution, spillRoot(env)) ? undefined :
      'The PR gate reads only the analysis pass spill file under its configured output root; PR code is never executed.';
  }
  if (execution.name !== 'ci_run') return 'The PR gate permits GitHub API reads and ci_run only; PR code is never executed.';
  const args = execution.arguments || {};
  const target = `${env.DSH_GATE_REPO}#${env.DSH_GATE_PR}`;
  if (args.pr !== target) return `Use the upstream qualified PR reference ${target}; the gate reviews only that PR.`;
  if (!['analyze', 'publish'].includes(args.task)) return 'The model review must analyze and publish; the static review task is unavailable.';
  if (args.task === 'publish' && (typeof args.body !== 'string' || !args.body.includes(`Review of ${env.DSH_GATE_HEAD}`) || !Array.isArray(args.findings))) {
    return 'Publish a model-authored review for the exact head, with an explicit findings array.';
  }
  return undefined;
}
export function apply(ctx) {
  ctx.tools.guard(execution => refusal(execution));
}
