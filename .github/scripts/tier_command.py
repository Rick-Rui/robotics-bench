#!/usr/bin/env python3
"""Which slash commands work on a PR of each tier (docs/COMMANDS.md), and the reply for one that does not.

    COMMENT_BODY=... tier_command.py --tier gpu     prints the reply (Markdown) when the comment holds a
                                                    command that does not work on this tier; nothing otherwise

Fenced code, blockquotes and inline code are not commands. Used by tier-commands.yml; the workflows that
run the commands skip PRs of other tiers themselves.
"""
import argparse
import os
import re

ALLOWED = {
    "cpu": ("check", "review", "validate", "oracle", "run", "cheat", "test"),
    "gpu": ("check", "review", "validate", "scene", "oracle", "run", "cheat", "test"),
    "real": ("check", "review", "real", "stop", "logs"),
}
NAMES = {"cpu": "a CPU-tier PR", "gpu": "a GPU-tier PR", "real": "a real-robot-tier PR"}
COMMANDS = sorted({c for cs in ALLOWED.values() for c in cs})
HINTS = {
    "real": "Real-robot runs are available only on a real-robot-tier PR (`tier:real`).",
    "stop": "`/stop` stops a real-robot run and works only on a real-robot-tier PR.",
    "logs": "On this tier the logs are in the Actions run linked at the end of every result comment.",
    "validate": "The real-robot tier has no automatic validation: the simulation tier is merged first, then an operator runs `/real` on site.",
    "scene": "`/scene` is the scene check on the GPU pool and works only on a GPU-tier PR (`tier:gpu`).",
}


def strip(body):
    out, fenced = [], False
    for line in (body or "").splitlines():
        if re.match(r"\s*```", line):
            fenced = not fenced
            continue
        if fenced or re.match(r"\s*>", line):
            continue
        out.append(re.sub(r"`[^`]*`", "", line))
    return "\n".join(out)


def commands(body):
    return re.findall(r"(?:^|\s)/(" + "|".join(COMMANDS) + r")(?=\s|$)", strip(body))


def reply(body, tier):
    if tier not in ALLOWED:
        return ""
    wrong = [c for c in dict.fromkeys(commands(body)) if c not in ALLOWED[tier]]
    if not wrong:
        return ""
    lines = [f"This is {NAMES[tier]} (`tier:{tier}`); " + ", ".join(f"`/{c}`" for c in wrong)
             + " is not available on this tier and was not run."]
    lines += [HINTS[c] for c in wrong if c in HINTS and not (c == "validate" and tier != "real")]
    lines.append("Commands available on this tier: " + " ".join(f"`/{c}`" for c in ALLOWED[tier]) + ".")
    return "\n\n".join(lines)


if __name__ == "__main__":
    p = argparse.ArgumentParser()
    p.add_argument("--tier", required=True)
    a = p.parse_args()
    out = reply(os.environ.get("COMMENT_BODY", ""), a.tier)
    if out:
        print(out)
