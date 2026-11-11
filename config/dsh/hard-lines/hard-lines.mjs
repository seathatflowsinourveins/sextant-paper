// AGENTS.md's hard lines, refused in every dsh profile by a monotonic tool guard.
// Mechanism: deepseek-harness@dsh-v0.2.2-rc.1 66491b05e3f05ad35bcdd4f6cd7ed26041d9e3db,
//   packages/core/tools/README.md:92 "Enforce policy on calls" (ctx.tools.guard: a returned reason
//   denies the call after the tools/pre-execute waterfall, and no later listener can turn it back) and
//   src/contracts.ts:391 ToolGuard, whose ToolExecution carries the tool name and arguments but no
//   Session directory (:174-223); tool names and arguments from docs/tool-catalog.md: bash and pwsh
//   (`command`), terminal_send (`text`), write and edit (`file_path`), str_replace_editor (`command`,
//   `path`), apply_patch (`input`), working_directory (`cd`) and create_worktree.
// Commands: .claude/settings.json's deny list, verbatim (tests/proofs/test_hard_lines.py keeps them
//   equal), matched first as code.claude.com/docs/en/permissions.md, read 2026-11-08, says Claude Code
//   matches them: compound commands split, subshells and substitutions checked, its fixed wrappers and
//   leading assignments stripped, `*` any text, no `*` exact, and a trailing ` *` also the bare
//   command. Then dsh goes further than Claude Code: it strips sudo, doas and exec by their own option
//   grammars (sudo.ws/docs/man/sudo.man, man.openbsd.org/doas, gnu.org/software/bash/manual/bash.html
//   "exec", all read 2026-11-08) and, like Codex's own bash -lc split (openai/codex@rust-v0.167.0
//   codex-rs/shell-command/src/bash.rs), checks a shell's -c script, or the here-document it reads, as
//   commands of its own. Words are read as bash reads them (bash manual 3.1.3 Comments, 3.6
//   Redirections and 3.6.6 Here Documents): a word beginning with # starts a comment, a redirection is
//   not an argument wherever it stands, and a quoted here-document's lines are only text.
// Writes: AGENTS.md Start, nothing under /mnt/wsl outside /mnt/wsl/sextant, for every write path: the
//   file tools' paths, apply_patch's Add/Update/Delete File and Move to headers
//   (packages/fs/tool-fs/README.md:263), a shell's output redirections and the paths a command other
//   than one of Claude Code's read-only commands names (permissions.md "Read-only commands"). A
//   relative path resolves against a Session directory the guard never sees (tool-fs README.md:28), so
//   one that names mnt/wsl is judged as if it reached there, and neither working_directory nor a
//   shell's cd may move under /mnt, from where a relative path would reach /mnt/wsl unnamed.
// PowerShell: its Env: drive and .NET's Environment.GetEnvironmentVariable(s) print variables
//   (learn.microsoft.com/powershell/module/microsoft.powershell.core/about/about_environment_provider,
//   read 2026-11-08), so pwsh refuses both.
// Directories: bash and pwsh take `workdir`, terminal_open `cwd`, subagent and subagent_fork a
//   child's `cwd`, and working_directory `cd`, read from the pinned bundles' own schemas by
//   deploy/wsl/hard-lines/tools.py, which `just ci` runs. From any of them under /mnt a
//   relative path would reach /mnt/wsl unnamed, so no call may run in one.
// The rules array is generated from deploy/wsl/hard-lines/matrix.json by
//   deploy/wsl/hard-lines/generate.py, which writes .claude/settings.json's Bash rules and copies
//   them here; a spelling is added to the matrix, never to this file. The path forms and option
//   slots live in that table as globs, so this file holds no second copy of them: a mutation that
//   removed its basename and git-option branches failed no proof, and they are gone.
// Shells and interpreters: bash reads -c's script from "the first non-option argument" (bash manual
//   6.1), so -c may sit anywhere in a cluster and an option before it may take a value of its own
//   (`bash -c -o pipefail <script>`), which is not the script; every non-option word after -c is
//   read, so an option value that happens to be a command is read too, over-blocking on the safe
//   side. pwsh takes -Command | -c and -EncodedCommand | -e |
//   -ec, base64 of UTF-16LE (learn.microsoft.com/powershell/module/microsoft.powershell.core/about/
//   about_pwsh); cmd takes /c and /k, where bare `set` prints every variable (learn.microsoft.com/
//   windows-server/administration/windows-commands/cmd). All read 2026-11-08.
// An interpreter's one-liner is NOT read: the harness itself runs `python3 -I -c` everywhere, so a
//   rule over program text would break it (the lead, 2026-11-08). The PR's "Residuals by design"
//   names the guards that hold instead: main's ruleset for a push, the devc sandbox for a write,
//   and scripts/with-keys.sh for a key's value. A Windows path resolves in any case on its drive, so wsl.exe is
//   matched caselessly, which Claude Code's own exact rules cannot do.
import path from 'node:path'

export const name = 'sextant-hard-lines'
export const inject = ['tools']

export const rules = [
  'Bash(wsl.exe *)',
  'Bash(*/wsl.exe)',
  'Bash(*/wsl.exe *)',
  'Bash(git push * main)',
  'Bash(git push * main *)',
  'Bash(git push *:main)',
  'Bash(git push *:main *)',
  'Bash(git push *+main)',
  'Bash(git push *+main *)',
  'Bash(git push *refs/heads/main)',
  'Bash(git push *refs/heads/main *)',
  'Bash(*/git push * main)',
  'Bash(*/git push * main *)',
  'Bash(*/git push *:main)',
  'Bash(*/git push *:main *)',
  'Bash(*/git push *+main)',
  'Bash(*/git push *+main *)',
  'Bash(*/git push *refs/heads/main)',
  'Bash(*/git push *refs/heads/main *)',
  'Bash(git push *--all*)',
  'Bash(git push *--branches*)',
  'Bash(git push *--mirror*)',
  'Bash(*/git push *--all*)',
  'Bash(*/git push *--branches*)',
  'Bash(*/git push *--mirror*)',
  'Bash(git worktree add *)',
  'Bash(*/git worktree add)',
  'Bash(*/git worktree add *)',
  'Bash(printenv *)',
  'Bash(*/printenv)',
  'Bash(*/printenv *)',
  'Bash(env *)',
  'Bash(*/env)',
  'Bash(*/env *)',
  'Bash(export *-p)',
  'Bash(export *-p *)',
  'Bash(sudo *wsl.exe*)',
  'Bash(sudo *env*)',
  'Bash(sudo *git push*)',
  'Bash(sudo *git worktree add*)',
  'Bash(*/sudo *wsl.exe*)',
  'Bash(*/sudo *env*)',
  'Bash(*/sudo *git push*)',
  'Bash(*/sudo *git worktree add*)',
  'Bash(doas *wsl.exe*)',
  'Bash(doas *env*)',
  'Bash(doas *git push*)',
  'Bash(doas *git worktree add*)',
  'Bash(*/doas *wsl.exe*)',
  'Bash(*/doas *env*)',
  'Bash(*/doas *git push*)',
  'Bash(*/doas *git worktree add*)',
  'Bash(exec *wsl.exe*)',
  'Bash(exec *env*)',
  'Bash(exec *git push*)',
  'Bash(exec *git worktree add*)',
  'Bash(*/exec *wsl.exe*)',
  'Bash(*/exec *env*)',
  'Bash(*/exec *git push*)',
  'Bash(*/exec *git worktree add*)',
  'Bash(env *wsl.exe*)',
  'Bash(env *env*)',
  'Bash(env *git push*)',
  'Bash(env *git worktree add*)',
  'Bash(*/env *wsl.exe*)',
  'Bash(*/env *env*)',
  'Bash(*/env *git push*)',
  'Bash(*/env *git worktree add*)',
  'Bash(nice *wsl.exe*)',
  'Bash(nice *env*)',
  'Bash(nice *git push*)',
  'Bash(nice *git worktree add*)',
  'Bash(*/nice *wsl.exe*)',
  'Bash(*/nice *env*)',
  'Bash(*/nice *git push*)',
  'Bash(*/nice *git worktree add*)',
  'Bash(timeout *wsl.exe*)',
  'Bash(timeout *env*)',
  'Bash(timeout *git push*)',
  'Bash(timeout *git worktree add*)',
  'Bash(*/timeout *wsl.exe*)',
  'Bash(*/timeout *env*)',
  'Bash(*/timeout *git push*)',
  'Bash(*/timeout *git worktree add*)',
  'Bash(stdbuf *wsl.exe*)',
  'Bash(stdbuf *env*)',
  'Bash(stdbuf *git push*)',
  'Bash(stdbuf *git worktree add*)',
  'Bash(*/stdbuf *wsl.exe*)',
  'Bash(*/stdbuf *env*)',
  'Bash(*/stdbuf *git push*)',
  'Bash(*/stdbuf *git worktree add*)',
  'Bash(xargs *wsl.exe*)',
  'Bash(xargs *env*)',
  'Bash(xargs *git push*)',
  'Bash(xargs *git worktree add*)',
  'Bash(*/xargs *wsl.exe*)',
  'Bash(*/xargs *env*)',
  'Bash(*/xargs *git push*)',
  'Bash(*/xargs *git worktree add*)',
  'Bash(nohup *wsl.exe*)',
  'Bash(nohup *env*)',
  'Bash(nohup *git push*)',
  'Bash(nohup *git worktree add*)',
  'Bash(*/nohup *wsl.exe*)',
  'Bash(*/nohup *env*)',
  'Bash(*/nohup *git push*)',
  'Bash(*/nohup *git worktree add*)',
  'Bash(time *wsl.exe*)',
  'Bash(time *env*)',
  'Bash(time *git push*)',
  'Bash(time *git worktree add*)',
  'Bash(*/time *wsl.exe*)',
  'Bash(*/time *env*)',
  'Bash(*/time *git push*)',
  'Bash(*/time *git worktree add*)',
  'Bash(builtin *wsl.exe*)',
  'Bash(builtin *env*)',
  'Bash(builtin *git push*)',
  'Bash(builtin *git worktree add*)',
  'Bash(*/builtin *wsl.exe*)',
  'Bash(*/builtin *env*)',
  'Bash(*/builtin *git push*)',
  'Bash(*/builtin *git worktree add*)',
  'Bash(noglob *wsl.exe*)',
  'Bash(noglob *env*)',
  'Bash(noglob *git push*)',
  'Bash(noglob *git worktree add*)',
  'Bash(*/noglob *wsl.exe*)',
  'Bash(*/noglob *env*)',
  'Bash(*/noglob *git push*)',
  'Bash(*/noglob *git worktree add*)',
  'Bash(setsid *wsl.exe*)',
  'Bash(setsid *env*)',
  'Bash(setsid *git push*)',
  'Bash(setsid *git worktree add*)',
  'Bash(*/setsid *wsl.exe*)',
  'Bash(*/setsid *env*)',
  'Bash(*/setsid *git push*)',
  'Bash(*/setsid *git worktree add*)',
  'Bash(git -* push * main)',
  'Bash(git --* push * main)',
  'Bash(git -* push *:main)',
  'Bash(git --* push *:main)',
  'Bash(git -* push *+main)',
  'Bash(git --* push *+main)',
  'Bash(git -* push *refs/heads/main)',
  'Bash(git --* push *refs/heads/main)',
  'Bash(git -* push *--all*)',
  'Bash(git --* push *--all*)',
  'Bash(git -* push *--branches*)',
  'Bash(git --* push *--branches*)',
  'Bash(git -* push *--mirror*)',
  'Bash(git --* push *--mirror*)',
  'Bash(git -* worktree add *)',
  'Bash(git --* worktree add *)',
  'Bash(*/git -* push * main)',
  'Bash(*/git --* push * main)',
  'Bash(*/git -* push *:main)',
  'Bash(*/git --* push *:main)',
  'Bash(*/git -* push *+main)',
  'Bash(*/git --* push *+main)',
  'Bash(*/git -* push *refs/heads/main)',
  'Bash(*/git --* push *refs/heads/main)',
  'Bash(*/git -* push *--all*)',
  'Bash(*/git --* push *--all*)',
  'Bash(*/git -* push *--branches*)',
  'Bash(*/git --* push *--branches*)',
  'Bash(*/git -* push *--mirror*)',
  'Bash(*/git --* push *--mirror*)',
  'Bash(*/git -* worktree add *)',
  'Bash(*/git --* worktree add *)',
  'Bash(*sh -*c* *wsl.exe*)',
  'Bash(*sh -*c* *printenv*)',
  'Bash(*sh -*c* *env*)',
  'Bash(*sh -*c* *git push*)',
  'Bash(*sh -*c* *git worktree add*)',
  'Bash(*cmd.exe *wsl*)',
  'Bash(*powershell* *wsl*)',
  'Bash(*pwsh *wsl*)',
  'Bash(*cmd.exe *Env*)',
  'Bash(*powershell* *Env*)',
  'Bash(*pwsh *Env*)',
  'Bash(*cmd.exe *env*)',
  'Bash(*powershell* *env*)',
  'Bash(*pwsh *env*)',
  'Bash(*cmd.exe *set*)',
  'Bash(*powershell* *set*)',
  'Bash(*pwsh *set*)',
]

const lines = [
  [/wsl\.exe/, 'Never run `wsl.exe`'],
  [/^Bash\((\*\/)?git push/, 'Never push to `main`'],
  [/worktree add/, "Don't `git worktree add` by hand"],
  [/env|export|set/, "Never print an environment variable's value"],
]
const shellTools = { bash: 'command', pwsh: 'command', terminal_send: 'text' }
const keywords = new Set(['if', 'then', 'else', 'elif', 'fi', 'do', 'done', 'while', 'until', '{', '}', '!'])
const shells = new Set(['bash', 'sh', 'zsh', 'dash'])
const readOnly = new Set(['ls', 'cat', 'echo', 'pwd', 'head', 'tail', 'grep', 'find', 'wc', 'which', 'diff', 'stat', 'du'])
const findWrites = /^-(exec|execdir|ok|okdir|delete|fprint|fprint0|fprintf|fls)$/
const wslExe = /^(.*\/)?wsl\.exe$/i
const lookups = new Set(['which', 'type', 'hash', 'whereis', 'whatis', 'test', '['])
const winShells = /^(.*\/)?(cmd|powershell|pwsh)(\.exe)?$/i
const winFlags = /^(\/[cCkK]|-[cC]|-[cC]ommand|-[eE]|-[eE]c|-[eE]ncodedCommand)$/
// The directory parameters of every tool that takes one (docs/tool-catalog.md).
// The tools that take a directory, by their registered names: dsh-base cordis.patch.yml:385-395
// and dsh-web-app presets/*.patch.yml mount the second dsh-tool-subagent as `subagent_fork`, and
// the fork tool is `subagent_fork` (Google's #269, read from the installed packages).
const directories = { bash: 'workdir', pwsh: 'workdir', terminal_open: 'cwd', subagent: 'cwd', subagent_fork: 'cwd', working_directory: 'cd' }

function matcher(rule) {
  const pattern = rule.slice('Bash('.length, -1)
  const exact = (text) => new RegExp('^' + text.split('*').map((part) => part.replace(/[.+?^${}()|[\]\\]/g, '\\$&')).join('.*') + '$', 's')
  const full = exact(pattern)
  const bare = pattern.endsWith(' *') && pattern.indexOf('*') === pattern.length - 1 ? exact(pattern.slice(0, -2)) : null
  return (words) => full.test(words) || (bare !== null && bare.test(words))
}
const compiled = rules.map((rule) => [rule, matcher(rule)])

// The index closing a $( ... ) or <( ... ) whose contents start at text[start], past nested
// parentheses and quotes.
function closing(text, start) {
  let depth = 1
  for (let i = start; i < text.length; i++) {
    const c = text[i]
    if (c === '\\') i++
    else if (c === "'") { const j = text.indexOf("'", i + 1); i = j < 0 ? text.length : j }
    else if (c === '"') { let j = i + 1; while (j < text.length && text[j] !== '"') j += text[j] === '\\' ? 2 : 1; i = j }
    else if (c === '(') depth++
    else if (c === ')' && --depth === 0) return i
  }
  return text.length
}
const tick = (text, start) => { const j = text.indexOf('`', start); return j < 0 ? text.length : j }

// Every $( ) and backtick substitution in expanded text, as commands of their own.
function substitutions(text) {
  const out = []
  for (let i = 0; i < text.length; i++) {
    if (text[i] === '\\') i++
    else if (text[i] === '$' && text[i + 1] === '(') { const k = closing(text, i + 2); out.push(...parse(text.slice(i + 2, k))); i = k }
    else if (text[i] === '`') { const k = tick(text, i + 1); out.push(...parse(text.slice(i + 1, k))); i = k }
  }
  return out
}

// Redirection operators, longest first (bash manual 3.6). >& and <& duplicate a descriptor, or for
// >& with a file name, write it.
const operators = ['&>>', '&>', '<<<', '<<-', '<<', '<>', '<&', '>>', '>|', '>&', '<', '>']
const opens = { '&>>': 'write', '&>': 'write', '<>': 'write', '>>': 'write', '>|': 'write', '>': 'write', '>&': 'dup' }

// Splits shell text into simple commands as bash reads them, each { words, writes, docs }: quotes
// removed, comments dropped, a redirection's target kept out of the words (writes holds the targets
// it writes), here-document bodies in docs, and every substitution checked as commands of its own,
// leaving a placeholder word behind.
function parse(text) {
  const out = []
  const pending = []
  let command = { words: [], writes: [], docs: [] }
  let word = null
  let target = null
  const end = () => {
    if (word === null) return
    if (target === 'write' || (target === 'dup' && !/^(\d+-?|-)$/.test(word))) command.writes.push(word)
    else if (target === null) command.words.push(word)
    target = null
    word = null
  }
  const cut = () => {
    end()
    if (command.words.length || command.writes.length || command.docs.length) out.push(command)
    command = { words: [], writes: [], docs: [] }
  }
  const bodies = (i) => {
    for (const doc of pending.splice(0)) {
      for (let next = text.indexOf('\n', i + 1); ; next = text.indexOf('\n', i + 1)) {
        const line = text.slice(i + 1, next < 0 ? text.length : next)
        i = next < 0 ? text.length : next
        const last = (doc.strip ? line.replace(/^\t+/, '') : line) === doc.delimiter
        if (!last) doc.body += line + '\n'
        if (last || next < 0) break
      }
      if (!doc.quoted) out.push(...substitutions(doc.body))
    }
    return i
  }
  for (let i = 0; i < text.length; i++) {
    const c = text[i]
    if (c === "'") { const j = text.indexOf("'", i + 1) < 0 ? text.length : text.indexOf("'", i + 1); word = (word ?? '') + text.slice(i + 1, j); i = j }
    else if (c === '$' && text[i + 1] === "'") { const j = text.indexOf("'", i + 2) < 0 ? text.length : text.indexOf("'", i + 2); word = (word ?? '') + text.slice(i + 2, j).replace(/\\(.)/g, '$1'); i = j }
    else if (c === '"') {
      let j = i + 1
      let s = ''
      for (; j < text.length && text[j] !== '"'; j++) {
        if (text[j] === '\\' && j + 1 < text.length) s += text[++j]
        else if (text[j] === '$' && text[j + 1] === '(') { const k = closing(text, j + 2); out.push(...parse(text.slice(j + 2, k))); s += '$()'; j = k }
        else if (text[j] === '`') { const k = tick(text, j + 1); out.push(...parse(text.slice(j + 1, k))); s += '``'; j = k }
        else s += text[j]
      }
      word = (word ?? '') + s
      i = j
    } else if (c === '\\' && i + 1 < text.length) { if (text[i + 1] !== '\n') word = (word ?? '') + text[i + 1]; i++ }
    else if (c === '#' && word === null) { while (i + 1 < text.length && text[i + 1] !== '\n') i++ }
    else if ((c === '$' || c === '<' || c === '>') && text[i + 1] === '(') { const k = closing(text, i + 2); out.push(...parse(text.slice(i + 2, k))); word = (word ?? '') + c + '()'; i = k }
    else if (c === '`') { const k = tick(text, i + 1); out.push(...parse(text.slice(i + 1, k))); word = (word ?? '') + '``'; i = k }
    else if (c === '<' || c === '>' || (c === '&' && text[i + 1] === '>')) {
      if (word !== null && /^\d+$/.test(word)) word = null
      end()
      const op = operators.find((candidate) => text.startsWith(candidate, i))
      i += op.length - 1
      if (op === '<<' || op === '<<-') {
        let j = i + 1
        while (text[j] === ' ' || text[j] === '\t') j++
        let k = j
        while (k < text.length && !' \t\n;&|<>()'.includes(text[k])) k++
        const raw = text.slice(j, k)
        const doc = { delimiter: raw.replace(/['"\\]/g, ''), quoted: /['"\\]/.test(raw), strip: op === '<<-', body: '' }
        command.docs.push(doc)
        pending.push(doc)
        i = k - 1
      } else target = opens[op] ?? 'read'
    } else if (';&|()\n'.includes(c)) {
      cut()
      if (c === '\n') i = bodies(i)
    } else if (c === ' ' || c === '\t') end()
    else word = (word ?? '') + c
  }
  cut()
  bodies(text.length)
  return out
}

// Claude Code's own stripping: control-flow words, leading assignments and its fixed wrappers.
// A program's own name, whatever path reached it: /usr/bin/which is which.
const base = (word) => path.posix.basename(word ?? '')

function claudeStep(words) {
  const w = words
  if (keywords.has(w[0])) return w.slice(1)
  if (/^[A-Za-z_][A-Za-z0-9_]*\+?=/.test(w[0])) return w.slice(1)
  if (['time', 'nohup', 'builtin', 'noglob'].includes(w[0])) return w.slice(w[1] === '-p' && w[0] === 'time' ? 2 : 1)
  if (w[0] === 'command' && w[1] !== '-v' && w[1] !== '-V') return w.slice(['-p', '--'].includes(w[1]) ? 2 : 1)
  if (w[0] === 'timeout') { let r = w.slice(1); while (r[0]?.startsWith('-')) r = r.slice(['-s', '-k'].includes(r[0]) ? 2 : 1); return r.slice(1) }
  if (w[0] === 'nice') { const r = w.slice(1); return r[0] === '-n' ? r.slice(2) : /^-\d+$|^--adjustment=/.test(r[0] ?? '') ? r.slice(1) : r }
  if (w[0] === 'stdbuf') { let r = w.slice(1); while (r[0]?.startsWith('-')) r = r.slice(/^-[ioe]$/.test(r[0]) ? 2 : 1); return r }
  if (w[0] === 'xargs' && w[1] !== undefined && !w[1].startsWith('-')) return w.slice(1)
  return null
}

// Each wrapper past its own options and their values, `--`, and, for sudo, VAR=value: sudo(8),
// doas(1), bash's exec builtin, xargs(1), setsid(1) and GNU coreutils' nice, timeout and stdbuf.
const optionsWithValue = {
  sudo: /^-[aCcDghpRrtTuU]$|^--(type|close-from|login-class|chdir|group|host|prompt|chroot|role|command-timeout|other-user|user)$/,
  doas: /^-[aCu]$/,
  exec: /^-a$/,
  xargs: /^-[aEILnPs]$|^--(arg-file|delimiter|eof|replace|max-lines|max-args|max-procs|max-chars|process-slot-var)$/,
  nice: /^-n$|^--adjustment$/,
  timeout: /^-[ks]$|^--(kill-after|signal)$/,
  stdbuf: /^-[ioe]$|^--(input|output|error)$/,
  setsid: /^$/,
  env: /^-[uCS]$|^--(unset|chdir|split-string|block-signal|default-signal|ignore-signal)$/,
}
// timeout and setsid take a positional argument of their own before the command: the duration.
const positional = { timeout: 1 }
function wrapperStep(words) {
  const w = words
  const valued = optionsWithValue[w[0]]
  if (!valued) return null
  const program = w[0]
  let r = w.slice(1)
  while (r.length && r[0].startsWith('-') && r[0] !== '-') {
    if (r[0] === '--') { r = r.slice(1); break }
    const [, value] = /^(--[^=]+)=/.exec(r[0]) ?? []
    r = r.slice(value !== undefined || !valued.test(r[0]) ? 1 : 2)
  }
  while (program === 'env' && r.length && /^[A-Za-z_][A-Za-z0-9_]*=/.test(r[0])) r = r.slice(1)
  while (program === 'sudo' && r.length && /^[A-Za-z_][A-Za-z0-9_]*=/.test(r[0])) r = r.slice(1)
  return r.slice(positional[program] ?? 0)
}

function unwrap(words, steps) {
  let w = [...words]
  for (let next; w.length && (next = steps.map((step) => step(w)).find((r) => r !== null)) !== undefined;) w = next
  return w
}
const claudeView = (words) => unwrap(words, [claudeStep])
const dshView = (words) => unwrap(words, [wrapperStep, claudeStep])

// A refusal for a word that carries a hard line itself: a caseless wsl.exe, or an environment
// printer, which no glob can reach inside an interpreter's program text.
function wordLine(words) {
  const base = path.posix.basename(words[0] ?? '')
  if (lookups.has(base) || readOnly.has(base)) return undefined
  if (base === 'command' && (words[1] === '-v' || words[1] === '-V')) return undefined
  const named = words.find((word) => wslExe.test(word))
  return named === undefined ? undefined : [named, 'Never run `wsl.exe`']
}

function hardLine(rule) {
  const line = lines.find(([test]) => test.test(rule))?.[1] ?? 'AGENTS.md'
  return `AGENTS.md hard line: ${line}. Refused by ${rule}, the deny rule .claude/settings.json and this dsh guard share.`
}

function sharedPath(raw) {
  if (typeof raw !== 'string' || raw === '') return false
  const target = path.posix.normalize(raw)
  if (target.startsWith('/')) {
    const shared = target === '/mnt/wsl' || target.startsWith('/mnt/wsl/')
    return shared && !(target === '/mnt/wsl/sextant' || target.startsWith('/mnt/wsl/sextant/'))
  }
  const parts = target.split('/')
  return parts.some((part, i) => part === 'mnt' && parts[i + 1] === 'wsl' && parts[i + 2] !== 'sextant')
}

// A directory from where a relative path reaches /mnt/wsl without naming it: /mnt and everything
// under it, since ../wsl/<distro> leads out of any of them.
function underMnt(raw) {
  const target = path.posix.normalize(raw)
  if (target.startsWith('/')) return target === '/mnt' || target.startsWith('/mnt/')
  return target.split('/').includes('mnt')
}

const writeRefusal = (target) => `AGENTS.md hard line: never write under /mnt/wsl outside /mnt/wsl/sextant. Refused: ${target}.`
const moveRefusal = (target) => `AGENTS.md hard line: never write under /mnt/wsl outside /mnt/wsl/sextant. Refused: moving to ${target}, from where a relative path reaches /mnt/wsl unseen.`

function shellWrites(command) {
  const under = sharedPath
  const shared = command.writes.find(under)
  if (shared) return writeRefusal(shared)
  const [program, ...args] = dshView(command.words)
  if (program === undefined) return undefined
  const base = path.posix.basename(program)
  if (base === 'cd' || base === 'pushd') {
    const to = args.find((arg) => !arg.startsWith('-'))
    return to && underMnt(to) ? moveRefusal(to) : undefined
  }
  if (readOnly.has(base) && !(base === 'find' && args.some((arg) => findWrites.test(arg)))) return undefined
  const named = args.flatMap((arg) => (arg.includes('=') ? [arg, arg.slice(arg.indexOf('=') + 1)] : [arg])).find(under)
  return named ? writeRefusal(named) : undefined
}

function powershellEnvironment(command) {
  const printer = command.words.find((word) => /^env:/i.test(word) || /getenvironmentvariable/i.test(word))
  return printer ? `AGENTS.md hard line: Never print an environment variable's value. Refused: ${printer}, PowerShell's environment variables.` : undefined
}

// A shell's script argument: bash reads -c's script from the first non-option argument, so -c may
// sit anywhere in a cluster; a Windows shell takes it after /c, /k, -Command or -EncodedCommand.
function scripts(words) {
  const program = path.posix.basename(words[0] ?? '')
  if (shells.has(program)) {
    const flag = words.findIndex((word, i) => i > 0 && /^-[A-Za-z]*c[A-Za-z]*$/.test(word))
    if (flag < 0) return []
    // Every non-option word after -c, since an option before the script may take a value of its
    // own (`bash -c -o pipefail <script>`): the script is one of them, so all are read.
    const out = []
    const rest = words.slice(flag + 1)
    for (let i = 0; i < rest.length; i++) {
      if (rest[i].startsWith('-') || rest[i].startsWith('+')) continue
      out.push(rest[i])
    }
    return out
  }
  if (winShells.test(words[0] ?? '')) {
    const flag = words.findIndex((word, i) => i > 0 && winFlags.test(word))
    if (flag < 0) return []
    // cmd and pwsh take the rest of the line as the command, not one word (their own docs).
    const argument = words.slice(flag + 1).join(' ')
    if (/^-[eE]/.test(words[flag])) {
      try {
        return [Buffer.from(argument, 'base64').toString('utf16le')]
      } catch {
        return []
      }
    }
    return [argument]
  }
  return []
}

// A Windows shell's own environment printer, and `wsl`, which is wsl.exe there.
function windowsRefusal(words, script) {
  const inner = script.split(/[\s;&|]+/).filter(Boolean)
  if (inner.some((word) => /^(wsl|wsl\.exe)$/i.test(word))) return `AGENTS.md hard line: Never run \`wsl.exe\`. Refused: ${script}, inside ${words[0]}.`
  // cmd's `set` prints every variable with no argument, and the ones a NAME prefixes. A glob
  // cannot require "no argument", so the generated rule refuses an assignment too and this
  // follows it: the PR names that over-block
  // (learn.microsoft.com/windows-server/administration/windows-commands/set, read 2026-11-08).
  const printer = /^set$/i.test(inner[0] ?? '')
  if (printer || inner.some((word) => /env:/i.test(word))) {
    return `AGENTS.md hard line: Never print an environment variable's value. Refused: ${script}, inside ${words[0]}.`
  }
  return undefined
}

function shellRefusal(text, tool, depth = 0) {
  for (const command of parse(text)) {
    const claude = claudeView(command.words)
    const dsh = dshView(command.words)
    for (const words of [claude, dsh].filter((w) => w.length)) {
      const hit = compiled.find(([, test]) => test(words.join(' ')))
      if (hit) return hardLine(hit[0])
    }
    const word = wordLine(dsh) ?? wordLine(command.words)
    if (word) return `AGENTS.md hard line: ${word[1]}. Refused: ${word[0]}.`
    if (depth < 4) {
      for (const script of scripts(dsh)) {
        const windows = winShells.test(dsh[0] ?? '') ? windowsRefusal(dsh, script) : undefined
        const inner = windows ?? shellRefusal(script, tool, depth + 1)
        if (inner) return inner
      }
      if (shells.has(path.posix.basename(dsh[0] ?? ''))) {
        for (const doc of command.docs) {
          const inner = shellRefusal(doc.body, tool, depth + 1)
          if (inner) return inner
        }
      }
    }
    const refused = (tool === 'pwsh' ? powershellEnvironment(command) : undefined) ?? shellWrites(command)
    if (refused) return refused
  }
  return undefined
}

const headers = /^\*\*\* (?:Add File|Update File|Delete File|Move to): (.+)$/gm

function patchRefusal(input) {
  for (const [, raw] of input.matchAll(headers)) {
    if (sharedPath(raw.trim())) return writeRefusal(path.posix.normalize(raw.trim()))
  }
  return undefined
}

function toolRefusal(name, args) {
  // Every directory a call may run in, whatever else it does: none may be at or under /mnt.
  const field = directories[name]
  const directory = field ? args[field] : undefined
  if (typeof directory === 'string' && directory !== '' && underMnt(directory)) return moveRefusal(directory)
  if (name === 'write' || name === 'edit') return sharedPath(args.file_path) ? writeRefusal(args.file_path) : undefined
  if (name === 'str_replace_editor') return args.command !== 'view' && sharedPath(args.path) ? writeRefusal(args.path) : undefined
  if (name === 'apply_patch') return typeof args.input === 'string' ? patchRefusal(args.input) : undefined
  if (name === 'create_worktree') return "AGENTS.md hard line: Don't `git worktree add` by hand. Refused: create_worktree makes a worktree Orca's lifecycle never sees."
  const text = shellTools[name] ? args[shellTools[name]] : undefined
  return typeof text === 'string' ? shellRefusal(text, name) : undefined
}

export function apply(ctx) {
  ctx.tools.guard((execution) => {
    const args = execution.arguments
    return args !== null && typeof args === 'object' ? toolRefusal(execution.name, args) : undefined
  })
}
