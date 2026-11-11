// deepseek-harness@dsh-v0.2.1-rc.5: packages/core/tools/README.md (restrict),
// packages/context/file-reference-local/src/index.ts (agent/created).
export const name = 'sextant-source-tools'
export const inject = ['tools']

export function apply(ctx) {
  ctx.on('agent/created', ({ agent }) => {
    const reads = new Set(['mcp__deepwiki__read_wiki_structure', 'mcp__deepwiki__read_wiki_contents'])
    const deny = ctx.tools.schemas().map(tool => tool.name)
      .filter(name => name.startsWith('mcp__deepwiki__') && !reads.has(name))
    if (deny.length) agent.ctx.tools.restrict({ deny })
  })
}
