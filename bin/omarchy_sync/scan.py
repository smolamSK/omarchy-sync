"""Spot secrets in ordinary config files before they travel in the plain part of an export.

Cautious on purpose: a false alarm only moves a file into the encrypted part (or
leaves it out), while a miss puts a token in plain text on a USB key. Findings name
the line and the kind of secret, never the value.
"""

import re

MAX_SCAN = 1 << 20  # bigger files are not configs

KNOWN = [
    ("a private key", re.compile(r"-----BEGIN [A-Z0-9 ]*PRIVATE KEY-----")),
    ("a GitHub token", re.compile(r"\b(gh[pousr]_[A-Za-z0-9]{36,}|github_pat_[A-Za-z0-9_]{22,})")),
    ("a GitLab token", re.compile(r"\bglpat-[A-Za-z0-9_-]{20,}")),
    ("an API key (sk-…)", re.compile(r"\bsk-[A-Za-z0-9_-]{20,}")),
    ("a Slack token", re.compile(r"\bxox[abprs]-[A-Za-z0-9-]{10,}")),
    ("an AWS access key", re.compile(r"\b(AKIA|ASIA)[0-9A-Z]{16}\b")),
    ("a Google API key", re.compile(r"\bAIza[0-9A-Za-z_-]{35}\b")),
    ("a URL with a password", re.compile(r"\b[a-z][a-z0-9+.-]*://[^/\s:@]+:[^/\s@]{4,}@")),
]

# NAME=value / name: value / name = "value" where the name says what it is.
ASSIGNMENT = re.compile(
    r"""(?ix)
    (?<![\w.-])
    (?P<name>[\w.-]*?(token|secret|passw(or)?d|passphrase|api[_-]?key|access[_-]?key|
                     private[_-]?key|client[_-]?secret|credential|auth)[\w.-]*)
    ["']?\s*[:=]\s*
    (?:"(?P<dq>[^"]+)"|'(?P<sq>[^']+)'|(?P<bare>[^\s"'\#;,]+))"""
)

# Values that only point at a secret kept elsewhere, or are obviously placeholders.
INDIRECT = re.compile(r"""^(\$|%|\{|<|\(|`|~|/|\.\.?/|op://|pass:|keyring:|env:|file:|cmd:)""")
PLACEHOLDER = re.compile(r"^(x+|\*+|changeme|password|secret|token|your[_-].*|example.*|none|null|true|false|\d+)$", re.I)


def findings(data: bytes) -> list:
    """[(line number, what)] for every line that looks like it holds a secret."""
    if len(data) > MAX_SCAN or b"\0" in data:
        return []
    try:
        text = data.decode("utf-8")
    except UnicodeDecodeError:
        return []
    found = []
    for n, line in enumerate(text.splitlines(), 1):
        what = next((label for label, pattern in KNOWN if pattern.search(line)), None)
        if not what:
            for m in ASSIGNMENT.finditer(line):
                value = m.group("dq") or m.group("sq") or m.group("bare") or ""
                if len(value) >= 8 and not INDIRECT.match(value) and not PLACEHOLDER.match(value):
                    what = m.group("name")
                    break
        if what:
            found.append((n, what))
    return found


def describe(hits: list) -> str:
    shown = ", ".join(f"line {n} ({what})" for n, what in hits[:3])
    return shown + (f" and {len(hits) - 3} more" if len(hits) > 3 else "")
