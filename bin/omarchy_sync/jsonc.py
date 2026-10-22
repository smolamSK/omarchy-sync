"""Just enough JSONC for Omarchy's menu file: comments, trailing commas, fenced blocks."""

import json
import re

CODE, STRING, COMMENT = "c", "s", "x"


def _kinds(text: str):
    """For each character: CODE, STRING (inside quotes) or COMMENT."""
    kinds = [CODE] * len(text)
    i, n = 0, len(text)

    def mark(a, b, kind):
        for k in range(a, min(b, n)):
            kinds[k] = kind

    while i < n:
        if text[i] == '"':
            j = i + 1
            while j < n and text[j] != '"':
                j += 2 if text[j] == "\\" else 1
            mark(i, j + 1, STRING)
            i = j + 1
        elif text.startswith("//", i):
            j = text.find("\n", i)
            j = n if j < 0 else j
            mark(i, j, COMMENT)
            i = j
        elif text.startswith("/*", i):
            j = text.find("*/", i + 2)
            j = n if j < 0 else j + 2
            mark(i, j, COMMENT)
            i = j
        else:
            i += 1
    return kinds


def strip(text: str) -> str:
    """Plain JSON: comments blanked, trailing commas before } or ] removed."""
    kinds = _kinds(text)
    chars = [c if k != COMMENT or c == "\n" else " " for c, k in zip(text, kinds)]
    pending = None  # index of a comma that may turn out to be trailing
    for i, (c, k) in enumerate(zip(chars, kinds)):
        if k == COMMENT or c.isspace():
            continue
        if k == CODE and c in "}]" and pending is not None:
            chars[pending] = " "
        pending = i if k == CODE and c == "," else None
    return "".join(chars)


def loads(text: str):
    return json.loads(strip(text)) if text.strip() else {}


def fence(name: str):
    return f"// >>> {name} >>>", f"// <<< {name} <<<"


def remove_block(text: str, name: str) -> str:
    start, end = fence(name)
    pattern = re.compile(r"\n*[ \t]*" + re.escape(start) + r".*?" + re.escape(end) + r"[ \t]*", re.S)
    return pattern.sub("", text)


def block_entries(text: str, name: str) -> dict:
    """The entries inside a fenced block, or {}."""
    start, end = fence(name)
    m = re.search(re.escape(start) + r"(.*?)" + re.escape(end), text, re.S)
    return loads("{" + m.group(1) + "}") if m else {}


def put_block(text: str, name: str, entries: dict, comment: str = "") -> str:
    """Replace (or add) a fenced block of entries just before the object's closing brace."""
    text = remove_block(text, name)
    if not entries:
        return text
    if not text.strip():
        text = "{\n}\n"
    kinds = _kinds(text)
    close = max(i for i, c in enumerate(text) if c == "}" and kinds[i] == CODE)
    last = max((i for i in range(close) if kinds[i] != COMMENT and not text[i].isspace()), default=-1)
    needs_comma = last >= 0 and text[last] not in "{,"
    start, end = fence(name)
    lines = [f"  {start}"]
    if comment:
        lines.append(f"  // {comment}")
    for key, value in entries.items():
        lines.append(f"  {json.dumps(key, ensure_ascii=False)}: "
                     f"{json.dumps(value, ensure_ascii=False, separators=(',', ':'))},")
    lines.append(f"  {end}")
    head = text[: last + 1] + ("," if needs_comma else "")
    return head + text[last + 1: close].rstrip() + "\n\n" + "\n".join(lines) + "\n" + text[close:]
