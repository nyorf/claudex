#!/usr/bin/env node
// the parsing helpers are adapted from scripts/hooks/gateguard-fact-force.js in affaan-m/ECC at d3b8a3e,
// which descends from zunoworks/gateguard; both are MIT-licensed
//
// Copyright (c) 2026 Affaan Mustafa
// Copyright (c) 2026 Hirokazu Seto / ZUNO WORKS K.K.
//
// Permission is hereby granted, free of charge, to any person obtaining a copy
// of this software and associated documentation files (the "Software"), to deal
// in the Software without restriction, including without limitation the rights
// to use, copy, modify, merge, publish, distribute, sublicense, and/or sell
// copies of the Software, and to permit persons to whom the Software is
// furnished to do so, subject to the following conditions:
//
// The above copyright notice and this permission notice shall be included in all
// copies or substantial portions of the Software.
//
// THE SOFTWARE IS PROVIDED "AS IS", WITHOUT WARRANTY OF ANY KIND, EXPRESS OR
// IMPLIED, INCLUDING BUT NOT LIMITED TO THE WARRANTIES OF MERCHANTABILITY,
// FITNESS FOR A PARTICULAR PURPOSE AND NONINFRINGEMENT. IN NO EVENT SHALL THE
// AUTHORS OR COPYRIGHT HOLDERS BE LIABLE FOR ANY CLAIM, DAMAGES OR OTHER
// LIABILITY, WHETHER IN AN ACTION OF CONTRACT, TORT OR OTHERWISE, ARISING FROM,
// OUT OF OR IN CONNECTION WITH THE SOFTWARE OR THE USE OR OTHER DEALINGS IN THE
// SOFTWARE.

const ALLOWED = ['status', 'log', 'diff', 'show', 'ls-files', 'rev-parse', 'archive', 'blame', 'grep', 'merge-base'];
const ALLOWED_SET = new Set(ALLOWED);

// a wrapper this parser does not know still hides a bare `git` token; these words after it mean a real call
const GIT_ACTIONS = new Set([
  ...ALLOWED, 'add', 'am', 'apply', 'bisect', 'branch', 'checkout', 'cherry-pick', 'clean', 'clone', 'commit',
  'commit-tree', 'config', 'fetch', 'filter-branch', 'gc', 'hash-object', 'init', 'maintenance', 'merge', 'mv',
  'notes', 'prune', 'pull', 'push', 'read-tree', 'rebase', 'reflog', 'remote', 'repack', 'replace', 'reset',
  'restore', 'revert', 'rm', 'sparse-checkout', 'stash', 'submodule', 'switch', 'symbolic-ref', 'tag',
  'update-index', 'update-ref', 'worktree', 'write-tree',
]);

// the environment twins of `-c` and `--exec-path`, each able to make an allowed subcommand run a program;
// HOME and XDG_CONFIG_HOME pick the global config, whose core.pager or core.fsmonitor runs one too
const GIT_CONFIG_ENV =
  /^(GIT_CONFIG\w*|GIT_EXEC_PATH|GIT_EXTERNAL_DIFF|GIT_PAGER|PAGER|GIT_SSH|GIT_SSH_COMMAND|HOME|XDG_CONFIG_HOME)$/;
const ASSIGNMENT = /^([A-Za-z_][A-Za-z0-9_]*)=/;

const SHELLS = new Set(['sh', 'bash', 'zsh', 'dash', 'ksh', 'mksh', 'ash', 'fish', 'csh', 'tcsh']);
// each runs the string after its `-c` through a shell
const STRING_RUNNERS = new Set(['su', 'runuser', 'script']);

const INTERPRETERS = {
  python: {
    text: /^-[bBdEhiIOPqsSuvVx]*c/,
    value: new Set(['-W', '-X']),
    none: new Set(['-V', '--version', '-h', '--help']),
  },
  node: {
    text: /^(-[ep]{1,2}|--eval|--print)(=|$)/,
    value: new Set([
      '-r', '--require', '--import', '--loader', '--experimental-loader', '-C', '--conditions', '--input-type',
    ]),
    none: new Set(['-v', '--version', '-h', '--help', '--test', '--run', '-c', '--check']),
  },
  perl: { text: /^-(?![MImd])[A-Za-z]*[eE]/, value: new Set(), none: new Set(['-v', '-V', '-h']) },
  ruby: {
    text: /^-(?![rIC])[A-Za-z]*e/,
    value: new Set(['-r', '-I', '-C']),
    none: new Set(['--version', '-h', '--help']),
  },
};
const REDIRECT = /^(\d*|&)(>>|>&|<&|<>|>\||&>>|&>|<<-?|[<>])([\s\S]*)$/;
const KEYWORDS = new Set(['!', '{', '}', 'if', 'then', 'else', 'elif', 'do', 'while', 'until', 'exec', 'nohup']);
const MAX_DEPTH = 4;

const SUDO_VALUE_FLAGS = new Set([
  '-u', '--user', '-g', '--group', '-U', '--other-user', '-p', '--prompt', '-C', '--close-from', '-D', '--chdir',
  '-h', '--host', '-r', '--role', '-t', '--type', '-T', '--command-timeout',
]);
const XARGS_VALUE_FLAGS = new Set([
  '-I', '-n', '-P', '-L', '-s', '-d', '-E', '-a', '--arg-file', '--delimiter', '--max-args', '--max-procs',
  '--max-lines', '--max-chars', '--eof', '--replace',
]);
const UV_RUN_VALUE_FLAGS = new Set([
  '--with', '--with-editable', '--with-requirements', '--python', '-p', '--project', '--directory', '--package',
  '--extra', '--group', '--only-group', '--no-group', '--env-file', '--index', '--default-index', '--index-url',
  '--extra-index-url', '-w',
]);

class Unparsable extends Error {}

// `.git/` and `git-readonly.mjs` mention git without calling it
function namesGit(text) {
  return /(^|[^\w.-])git(?![\w-])/.test(text);
}

function commandBasename(token) {
  if (!token) return '';
  return token
    .replace(/^.*[\\/]/, '')
    .replace(/\.exe$/i, '')
    .toLowerCase();
}

// parentheses split too, so a `( … )` group or the inside of `$( … )` becomes a segment of its own
function quoteAwareSegments(input) {
  const segments = [];
  let words = [];
  let current = '';
  let hasWord = false;
  let quote = null;
  let escaped = false;
  let comment = false;
  let dollar = false;

  const flushWord = () => {
    if (hasWord) words.push(current);
    current = '';
    hasWord = false;
  };
  const flushSegment = () => {
    flushWord();
    if (words.length) segments.push(words);
    words = [];
  };

  for (const ch of input) {
    const afterDollar = dollar;
    dollar = false;
    if (comment) {
      if (ch !== '\n') continue;
      comment = false;
    }
    if (escaped) {
      current += ch;
      hasWord = true;
      escaped = false;
      continue;
    }
    if (ch === '\\' && quote !== "'") {
      escaped = true;
      hasWord = true;
      continue;
    }
    if (quote) {
      if (ch === quote) quote = null;
      else current += ch;
      hasWord = true;
      continue;
    }
    if (ch === '"' || ch === "'") {
      // bash's $'…' and $"…" drop the $ and decode escapes, so the word the shell passes is not the one read here
      if (afterDollar) throw new Unparsable();
      quote = ch;
      hasWord = true;
      continue;
    }
    if (';|&\n\r()'.includes(ch)) {
      flushSegment();
      continue;
    }
    if (/\s/.test(ch)) {
      flushWord();
      continue;
    }
    if (ch === '#' && !hasWord) {
      comment = true;
      continue;
    }
    current += ch;
    hasWord = true;
    dollar = ch === '$';
  }
  if (quote || escaped) throw new Unparsable();
  flushSegment();
  return segments;
}

// `$( … )` and backticks run even inside double quotes, which the segment split keeps whole
function substitutions(input) {
  const found = [];
  let quote = null;
  for (let i = 0; i < input.length; i += 1) {
    const ch = input[i];
    if (ch === '\\' && quote !== "'") {
      i += 1;
      continue;
    }
    if (quote === "'") {
      if (ch === "'") quote = null;
      continue;
    }
    if (ch === "'" && !quote) {
      quote = ch;
      continue;
    }
    if (ch === '"') {
      quote = quote ? null : '"';
      continue;
    }
    if (ch === '`') {
      const end = input.indexOf('`', i + 1);
      if (end === -1) throw new Unparsable();
      found.push(input.slice(i + 1, end));
      i = end;
      continue;
    }
    if (ch === '$' && input[i + 1] === '(') {
      let depth = 0;
      let j = i + 1;
      for (; j < input.length; j += 1) {
        if (input[j] === '\\') j += 1;
        else if (input[j] === '(') depth += 1;
        else if (input[j] === ')' && --depth === 0) break;
      }
      if (j >= input.length) throw new Unparsable();
      found.push(input.slice(i + 2, j));
      i = j;
    }
  }
  return found;
}

// a heredoc body is data unless a shell or an interpreter reads it
function splitHeredocs(input) {
  const lines = input.split('\n');
  const script = [];
  const bodies = [];
  for (let i = 0; i < lines.length; i += 1) {
    const line = lines[i];
    script.push(line);
    const delimiters = [...line.matchAll(/<<(-?)\s*(['"]?)([A-Za-z_][\w-]*)\2/g)].filter(
      (m) => line[m.index - 1] !== '<' && line[m.index + 2] !== '<',
    );
    for (const m of delimiters) {
      const body = [];
      i += 1;
      while (i < lines.length && (m[1] ? lines[i].replace(/^\t+/, '') : lines[i]) !== m[3]) {
        body.push(lines[i]);
        i += 1;
      }
      if (i >= lines.length) throw new Unparsable();
      bodies.push({ line, body: body.join('\n') });
    }
  }
  return { script: script.join('\n'), bodies };
}

function unwrapLeadWrappers(input) {
  let tokens = input;
  let index = 0;
  const assigned = [];
  for (let guard = 0; guard < 16 && index < tokens.length; guard += 1) {
    const token = tokens[index];
    const base = commandBasename(token);
    const assignment = token.match(ASSIGNMENT);
    if (assignment) {
      assigned.push(assignment[1]);
      index += 1;
      continue;
    }
    if (KEYWORDS.has(base)) {
      index += 1;
      continue;
    }
    if (base === 'time') {
      index += 1;
      if (tokens[index] === '-p') index += 1;
      continue;
    }
    if (base === 'sudo' || base === 'doas') {
      index += 1;
      while (index < tokens.length) {
        const flag = tokens[index];
        if (flag === '--') {
          index += 1;
          break;
        }
        if (flag === '-' || !flag.startsWith('-')) break;
        index += SUDO_VALUE_FLAGS.has(flag) ? 2 : 1;
      }
      continue;
    }
    if (base === 'env') {
      index += 1;
      while (index < tokens.length) {
        const arg = tokens[index];
        if (arg === '-S' || arg === '--split-string' || arg.startsWith('--split-string=')) {
          const packed = arg.startsWith('--split-string=') ? arg.slice(15) : tokens[index + 1] ?? '';
          const rest = tokens.slice(index + (arg.startsWith('--split-string=') ? 1 : 2));
          tokens = [...tokens.slice(0, index), ...quoteAwareSegments(packed).flat(), ...rest];
          continue;
        }
        if (['-u', '--unset', '-C', '--chdir'].includes(arg)) {
          index += 2;
          continue;
        }
        const envAssignment = arg.match(ASSIGNMENT);
        if (envAssignment) assigned.push(envAssignment[1]);
        if (arg.startsWith('-') || envAssignment) {
          index += 1;
          continue;
        }
        break;
      }
      continue;
    }
    if (base === 'busybox') {
      index += 1;
      continue;
    }
    if (base === 'nice') {
      index += 1;
      if (tokens[index] === '-n' || tokens[index] === '--adjustment') index += 2;
      else if (/^(-n?-?\d+|--adjustment=.*)$/.test(tokens[index] ?? '')) index += 1;
      continue;
    }
    if (base === 'command') {
      index += 1;
      while (tokens[index]?.startsWith('-')) {
        // `command -v git` looks git up and runs nothing
        if (/^-[^-]*[vV]/.test(tokens[index])) return { tokens, index: tokens.length, assigned };
        index += 1;
      }
      continue;
    }
    if (base === 'timeout') {
      index += 1;
      while (tokens[index]?.startsWith('-')) {
        index += ['-s', '--signal', '-k', '--kill-after'].includes(tokens[index]) ? 2 : 1;
      }
      index += 1;
      continue;
    }
    if (base === 'xargs') {
      index += 1;
      while (tokens[index]?.startsWith('-')) {
        if (tokens[index] === '--') {
          index += 1;
          break;
        }
        index += XARGS_VALUE_FLAGS.has(tokens[index]) ? 2 : 1;
      }
      continue;
    }
    if (base === 'uv' && tokens[index + 1] === 'run') {
      index += 2;
      while (tokens[index]?.startsWith('-')) {
        if (tokens[index] === '--') {
          index += 1;
          break;
        }
        index += UV_RUN_VALUE_FLAGS.has(tokens[index]) ? 2 : 1;
      }
      continue;
    }
    if (base === 'mise' && (tokens[index + 1] === 'exec' || tokens[index + 1] === 'x')) {
      const separator = tokens.indexOf('--', index + 2);
      index = separator === -1 ? tokens.length : separator + 1;
      continue;
    }
    break;
  }
  return { tokens, index, assigned };
}

function gitRefusal(tokens, assigned) {
  const configured = assigned.find((name) => GIT_CONFIG_ENV.test(name));
  if (configured) return `${configured}=`;
  let i = 1;
  while (i < tokens.length) {
    const t = tokens[i];
    if (t === '-C' || t === '--git-dir' || t === '--work-tree') {
      i += 2;
      continue;
    }
    if (/^--(git-dir|work-tree)=/.test(t) || ['--no-pager', '-p', '--paginate', '--no-optional-locks'].includes(t)) {
      i += 1;
      continue;
    }
    if (['--version', '-v', '--help', '-h', '--html-path', '--man-path', '--info-path'].includes(t)) return null;
    if (t.startsWith('-')) return t.split('=')[0];
    if (!ALLOWED_SET.has(t)) return t;
    const rest = tokens.slice(i + 1);
    // both options make an allowed subcommand run a program of the caller's choosing
    if (t === 'grep' && rest.some((a) => /^--op/.test(a) || /^-[^-]*O/.test(a))) return 'grep --open-files-in-pager';
    if (t === 'archive' && rest.some((a) => /^--(r|e)/.test(a))) return 'archive --remote/--exec';
    // these write a file wherever the caller names it, and a baseline goes through `git archive HEAD | tar -x`
    if (t === 'archive' && rest.some((a) => /^--o/.test(a) || /^-[^-]*o/.test(a))) return 'archive --output';
    if (['diff', 'log', 'show'].includes(t) && rest.some((a) => /^--output(=|$)/.test(a))) return `${t} --output`;
    return null;
  }
  return null;
}

// a here-string is stdin, and the program itself when a shell or an interpreter has no other
function splitHereStrings(args) {
  const strings = [];
  const rest = [];
  let heredoc = false;
  for (let i = 0; i < args.length; i += 1) {
    const here = args[i].match(/^\d*<<<([\s\S]*)$/);
    if (here) {
      if (here[1]) strings.push(here[1]);
      else if (i + 1 < args.length) strings.push(args[(i += 1)]);
      continue;
    }
    if (REDIRECT.exec(args[i])?.[2].startsWith('<<')) heredoc = true;
    rest.push(args[i]);
  }
  return { strings, rest, heredoc };
}

function interpreterFamily(base) {
  const family = base.replace(/[\d.]+$/, '');
  return Object.hasOwn(INTERPRETERS, family) ? family : null;
}

// 'text' for an inline program, 'stdin' when the program arrives on stdin, 'file' for a script or a module
function interpreterProgram(family, args) {
  const { text, value, none } = INTERPRETERS[family];
  for (let i = 0; i < args.length; i += 1) {
    const a = args[i];
    if (a === '-') return 'stdin';
    if (a === '--') return i + 1 < args.length ? 'file' : 'stdin';
    if (text.test(a)) return 'text';
    if (none.has(a) || (family === 'python' && /^-m/.test(a))) return 'file';
    const redirect = a.match(REDIRECT);
    if (redirect) {
      if (!redirect[3]) i += 1;
      continue;
    }
    if (!a.startsWith('-')) return 'file';
    if (value.has(a)) i += 1;
  }
  return 'stdin';
}

// every argument after the string flag is read as a command, so `sh -c "$0" 'git push'` is caught too
function shellRefusal(base, words, raw, depth) {
  const { strings, rest: args, heredoc } = splitHereStrings(words);
  for (const text of strings) {
    const refused = commandRefusal(text, depth + 1);
    if (refused) return refused;
  }
  let texts = null;
  let operand = null;
  let readsStdin = false;
  for (let i = 0; i < args.length; i += 1) {
    const a = args[i];
    const glued = a.match(/^--(?:init-)?command=([\s\S]*)$/);
    if (glued) {
      texts = [glued[1], ...args.slice(i + 1)];
      break;
    }
    const cluster = /^-[A-Za-z]+$/.test(a);
    if (
      a === '--command' ||
      a === '--init-command' ||
      (cluster && (a.includes('c') || (base === 'fish' && a.includes('C'))))
    ) {
      texts = args.slice(i + 1);
      break;
    }
    if (a === '--') {
      operand = args[i + 1] ?? null;
      break;
    }
    if (/^[-+][oO]$/.test(a)) {
      i += 1;
      continue;
    }
    if (cluster && a.includes('s')) readsStdin = true;
    const redirect = a.match(REDIRECT);
    if (redirect) {
      if (!redirect[3]) i += 1;
      continue;
    }
    if (a.startsWith('-') || a.startsWith('+')) continue;
    operand = a;
    break;
  }
  if (texts) {
    for (const text of texts) {
      const refused = commandRefusal(text, depth + 1);
      if (refused) return refused;
    }
  } else if ((operand === null || readsStdin) && !strings.length && !heredoc && namesGit(raw)) {
    // a shell with no script reads its program from stdin, where the piped text may be a git call
    throw new Unparsable();
  }
  return null;
}

function runnerRefusal(args, depth) {
  for (let i = 0; i < args.length; i += 1) {
    const a = args[i];
    const glued = a.match(/^--(?:session-)?command=([\s\S]*)$/) ?? a.match(/^-c([\s\S]+)$/);
    const texts = glued
      ? [glued[1], ...args.slice(i + 1)]
      : /^-[A-Za-z]*c$|^--(session-)?command$/.test(a)
        ? args.slice(i + 1)
        : null;
    if (!texts) continue;
    for (const text of texts) {
      const refused = commandRefusal(text, depth + 1);
      if (refused) return refused;
    }
    return null;
  }
  return null;
}

function segmentRefusal(segment, raw, depth) {
  // `bash<<<"…"` arrives as one word
  const split = segment.flatMap((w) => {
    const at = w.indexOf('<<<');
    return at > 0 && !/^\d*$/.test(w.slice(0, at)) ? [w.slice(0, at), w.slice(at)] : [w];
  });
  const { tokens, index, assigned } = unwrapLeadWrappers(split);
  if (index < tokens.length) {
    const word = tokens[index];
    const base = commandBasename(word);
    const args = tokens.slice(index + 1);
    if (/[$`]/.test(word)) {
      if (namesGit(raw)) throw new Unparsable();
    } else if (base === 'git') {
      const refused = gitRefusal(tokens.slice(index), assigned);
      if (refused) return refused;
    } else if (/^git-[a-z-]+$/.test(base)) {
      return base;
    } else if (base === 'export' || base === 'declare' || base === 'typeset') {
      const configured = args.map((a) => a.match(ASSIGNMENT)?.[1]).find((name) => name && GIT_CONFIG_ENV.test(name));
      if (configured && namesGit(raw)) return `${configured}=`;
    } else if (base === 'eval') {
      const refused = commandRefusal(args.join(' '), depth + 1);
      if (refused) return refused;
    } else if (SHELLS.has(base)) {
      const refused = shellRefusal(base, args, raw, depth);
      if (refused) return refused;
    } else if (STRING_RUNNERS.has(base)) {
      const refused = runnerRefusal(args, depth);
      if (refused) return refused;
    } else if (interpreterFamily(base)) {
      // a program text that names git is refused even when it only prints the word: a string and a call look alike
      const { strings, rest, heredoc } = splitHereStrings(args);
      const program = interpreterProgram(interpreterFamily(base), rest);
      const stdin = strings.length ? strings.some(namesGit) : !heredoc && namesGit(raw);
      if (program === 'text' ? rest.some(namesGit) : program === 'stdin' && stdin) throw new Unparsable();
    }
  }
  for (let i = 0; i < tokens.length - 1; i += 1) {
    if (i === index) continue;
    // an unknown wrapper such as `watch` or BSD `script` can still hide a shell running a string
    if (SHELLS.has(commandBasename(tokens[i])) && /^-[A-Za-z]*c[A-Za-z]*$/.test(tokens[i + 1])) {
      const refused = shellRefusal(commandBasename(tokens[i]), tokens.slice(i + 1), raw, depth);
      if (refused) return refused;
    }
    if (commandBasename(tokens[i]) !== 'git') continue;
    // a git word right after a gitleaks command word is gitleaks' own scan mode, not a git call; gitleaks itself
    // runs only read-only git. only the command word counts: after an argument such as `flock gitleaks` or
    // `ssh gitleaks` the git word is a real call, and no other program gets this, since `pass git push` runs git
    if (i - 1 === index && commandBasename(tokens[i - 1]) === 'gitleaks') continue;
    const next = tokens[i + 1];
    if (next.startsWith('-') || GIT_ACTIONS.has(next)) {
      const refused = gitRefusal(tokens.slice(i), assigned);
      if (refused) return refused;
    }
  }
  return null;
}

function commandRefusal(raw, depth = 0) {
  if (depth > MAX_DEPTH) {
    if (namesGit(raw)) throw new Unparsable();
    return null;
  }
  const { script, bodies } = splitHeredocs(raw);
  for (const { line, body } of bodies) {
    const reader = quoteAwareSegments(line).find((words) => words.some((w) => w.includes('<<')));
    const unwrapped = reader ? unwrapLeadWrappers(reader) : null;
    const readerBase = unwrapped ? commandBasename(unwrapped.tokens[unwrapped.index]?.split('<<')[0]) : '';
    const family = interpreterFamily(readerBase);
    if (SHELLS.has(readerBase) || readerBase === 'eval') {
      const refused = commandRefusal(body, depth + 1);
      if (refused) return refused;
    } else if (family && namesGit(body)) {
      const args = splitHereStrings(unwrapped.tokens.slice(unwrapped.index + 1)).rest;
      if (interpreterProgram(family, args) === 'stdin') throw new Unparsable();
    }
  }
  for (const inner of substitutions(script)) {
    const refused = commandRefusal(inner, depth + 1);
    if (refused) return refused;
  }
  for (const segment of quoteAwareSegments(script)) {
    const refused = segmentRefusal(segment, raw, depth);
    if (refused) return refused;
  }
  return null;
}

// the allowlist cannot vouch for a call it cannot read, so an unreadable command that names git is refused
function verdict(command) {
  try {
    return commandRefusal(command);
  } catch (error) {
    if (!(error instanceof Unparsable)) throw error;
    // a throw ends the parse early, so a git word split by quotes or a backslash elsewhere is looked for too
    return namesGit(command) || namesGit(command.replace(/[\\'"]/g, '')) ? 'call that does not parse safely' : null;
  }
}

async function main() {
  const chunks = [];
  for await (const c of process.stdin) chunks.push(c);
  const input = JSON.parse(Buffer.concat(chunks).toString('utf8'));
  const command = input?.tool_input?.command;
  if (typeof command !== 'string') return;

  const refused = verdict(command);
  if (!refused) return;
  const what = refused.startsWith('git-') ? refused : `git ${refused}`;
  process.stderr.write(
    `${what} refused: agents run only read-only git (${ALLOWED.join(', ')}; rules/13 §13). ` +
      'Hand the owner the command instead.\n',
  );
  process.exit(2);
}

// a broken hook must never block work, so a crash lets the call through and says so
main().catch((error) => {
  process.stderr.write(`git-readonly hook failed open: ${error?.message ?? error}\n`);
  process.exit(0);
});
