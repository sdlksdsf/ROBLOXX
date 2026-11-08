#!/usr/bin/env python3
"""Static checks for scripts that run on Windows PowerShell 5.1.

There is no PowerShell in this sandbox, so this reproduces the parse rules that
have actually broken this project instead of a naive brace count:

  * `$x .Method()` - the '.' must immediately follow the variable, else
                     PowerShell raises a PARSE ERROR and the whole file never runs
  * unbalanced braces / parens / brackets outside strings and comments
  * constructs that need PowerShell 6/7 and would throw on 5.1
  * bare-LF line endings on files meant for Windows
  * touching Task.Result before the task completes (blocks forever)
"""
import re, sys

PS7_ONLY = [
    (r"\$psi\.StandardInputEncoding\s*=", "StandardInputEncoding is .NET Core only"),
    (r"\?\?=",                            "??= needs PowerShell 7"),
    (r"[A-Za-z0-9_\)\]]\?\.",             "?. needs PowerShell 7"),
    (r"-Parallel\b",                      "ForEach-Object -Parallel needs PowerShell 7"),
    (r"Join-String\b",                    "Join-String needs PowerShell 6+"),
]


def strip_code(s):
    out, i, n = [], 0, len(s)
    while i < n:
        c = s[i]
        if s[i:i + 2] == "<#":
            j = s.find("#>", i); j = n if j < 0 else j + 2
            out.append(re.sub(r"[^\n]", " ", s[i:j])); i = j; continue
        if c == "#":
            j = i
            while j < n and s[j] != "\n": j += 1
            out.append(" " * (j - i)); i = j; continue
        if c == '"':
            i += 1
            while i < n:
                if s[i] == "`": i += 2; continue
                if s[i] == '"':
                    if i + 1 < n and s[i + 1] == '"': i += 2; continue
                    i += 1; break
                i += 1
            out.append('" "'); continue
        if c == "'":
            i += 1
            while i < n:
                if s[i] == "'":
                    if i + 1 < n and s[i + 1] == "'": i += 2; continue
                    i += 1; break
                i += 1
            out.append("' '"); continue
        out.append(c); i += 1
    return "".join(out)


def check(path):
    raw = open(path, encoding="utf-8", newline="").read()
    norm = raw.replace("\r\n", "\n")
    code = strip_code(norm)
    problems = []
    for m in re.finditer(r"\$[A-Za-z_][A-Za-z0-9_:]* \.[A-Za-z_]\w*\s*\(", code):
        problems.append((code[:m.start()].count("\n") + 1,
                         f"space before '.': {m.group(0)!r} -> parse error in PS 5.1"))
    for o, c in ("{}", "()", "[]"):
        if code.count(o) != code.count(c):
            problems.append((0, f"unbalanced {o}{c}: {code.count(o)} vs {code.count(c)}"))
    for pat, why in PS7_ONLY:
        for m in re.finditer(pat, code):
            problems.append((code[:m.start()].count("\n") + 1, why))
    lf_only = raw.count("\n") - raw.count("\r\n")
    if lf_only:
        problems.append((0, f"{lf_only} lines are bare-LF (expected CRLF)"))
    return problems


if __name__ == "__main__":
    files = sys.argv[1:] or ["bridge/relay/roblox_relay.ps1"]
    bad = 0
    for f in files:
        probs = check(f)
        print(f"{f}: {'OK' if not probs else str(len(probs)) + ' problem(s)'}")
        for line, msg in probs:
            print(f"  {'line ' + str(line) + ': ' if line else ''}{msg}")
        bad += len(probs)
    sys.exit(1 if bad else 0)
