"""Parse /oracle, /run, /cheat or /test from a PR comment (env COMMENT_BODY) for gpu-trials.yml.

Prints {"command", "lanes": [{"harness", "gpu": "auto"}], "trials"} or {"command", "error"}; exit 1 when the
comment holds none of these commands. Fenced code, blockquotes and inline code are not commands.
"""
import json
import os
import re
import sys

COMMANDS = ('oracle', 'run', 'cheat', 'test')
AGENTS = ('codex', 'claude-code')
GPU = 'auto'           # the host job server picks a free enabled card
MAX_TRIALS = 5


def strip(body):
    out, fenced = [], False
    for line in (body or '').splitlines():
        if re.match(r'\s*```', line):
            fenced = not fenced
            continue
        if fenced or re.match(r'\s*>', line):
            continue
        out.append(re.sub(r'`[^`]*`', '', line))
    return '\n'.join(out)


def parse(body):
    text = strip(body)
    m = re.search(r'(?:^|\s)/(' + '|'.join(COMMANDS) + r')(?=\s|$)([^\n]*)', text)
    if not m:
        return None
    command, rest = m.group(1), m.group(2)
    options = dict(re.findall(r'(\w+)=([\w.,-]+)', rest))
    unknown = sorted(set(options) - {'agents', 'trials'})
    if unknown:
        return dict(command=command, error='the GPU tier does not accept ' + ', '.join(unknown) + '; available: `agents=codex,claude-code` `trials=1..5`.')
    if command == 'oracle':
        if options:
            return dict(command=command, error='`/oracle` takes no options.')
        return dict(command=command, lanes=[dict(harness='oracle', gpu=GPU)], trials=1)
    agents = options.get('agents', 'claude-code' if command == 'test' else 'codex,claude-code').split(',')
    if not agents or any(a not in AGENTS for a in agents) or len(set(agents)) != len(agents):
        return dict(command=command, error='on the GPU tier `agents=` accepts codex and claude-code (Kimi k3.1 pro).')
    try:
        trials = int(options.get('trials', '1'))
    except ValueError:
        trials = 0
    if not 1 <= trials <= MAX_TRIALS or (command == 'test' and (trials != 1 or len(agents) != 1)):
        return dict(command=command, error='`trials=` must be 1 to 5; `/test` always runs one agent once.')
    return dict(command=command, lanes=[dict(harness=a, gpu=GPU) for a in agents], trials=trials)


if __name__ == '__main__':
    result = parse(os.environ.get('COMMENT_BODY', ''))
    if result is None:
        sys.exit(1)
    print(json.dumps(result, ensure_ascii=False))
