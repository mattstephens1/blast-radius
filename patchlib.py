"""Parse and apply unified diffs (no external tools needed)."""
import re

HUNK = re.compile(r"^@@ -(\d+)(?:,(\d+))? \+(\d+)(?:,(\d+))? @@")


def parse(text):
    """Return list of files: {old, new, hunks:[(old_start, lines)]}."""
    files, cur, hunk = [], None, None
    lines = text.split("\n")
    i = 0
    while i < len(lines):
        ln = lines[i]
        if ln.startswith("--- ") and i + 1 < len(lines) and lines[i + 1].startswith("+++ "):
            old = ln[4:].split("\t")[0]
            new = lines[i + 1][4:].split("\t")[0]
            cur = {"old": old[2:] if old.startswith("a/") else old,
                   "new": new[2:] if new.startswith("b/") else new, "hunks": []}
            files.append(cur)
            i += 2
            continue
        m = HUNK.match(ln)
        if m and cur is not None:
            hunk = (int(m.group(1)), [])
            cur["hunks"].append(hunk)
        elif hunk is not None and ln[:1] in (" ", "+", "-"):
            hunk[1].append(ln)
        elif hunk is not None and ln.startswith("\\"):
            pass
        i += 1
    return files


def apply(old_text, hunks):
    """Apply hunks to old_text, returning the new text."""
    old = old_text.split("\n") if old_text else []
    out, pos = [], 0
    for start, body in hunks:
        idx = max(start - 1, 0)
        out.extend(old[pos:idx])
        pos = idx
        for ln in body:
            tag, content = ln[0], ln[1:]
            if tag == " ":
                out.append(old[pos]); pos += 1
            elif tag == "-":
                pos += 1
            else:
                out.append(content)
    out.extend(old[pos:])
    return "\n".join(out)


def changed_old_lines(hunks):
    """Old-file line numbers that were removed, plus the old lines either side of insertions."""
    lines = set()
    for start, body in hunks:
        pos = start
        for ln in body:
            if ln[0] == " ":
                pos += 1
            elif ln[0] == "-":
                lines.add(pos); pos += 1
            else:
                lines.add(pos - 1); lines.add(pos)
    return lines



def changed_new_lines(hunks):
    """New-file line numbers that were added, plus the new lines either side of deletions."""
    lines, shift = set(), 0
    for start, body in hunks:
        pos = start + shift
        for ln in body:
            if ln[0] == " ":
                pos += 1
            elif ln[0] == "+":
                lines.add(pos); pos += 1; shift += 1
            else:
                lines.add(pos - 1); lines.add(pos); shift -= 1
    return lines
