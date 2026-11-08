#!/usr/bin/env python3
"""
|_||_||_|   DRAC: Dry Run Automatic Checker
| || || |   "Sucking the bugs out of your scripts"
|_||_||_|

drac.py - record and compare the dry-run output of tools and scripts.

Many tools (bash or python) print what they would do under a dry-run flag (e.g. -n or
--dry-run). drac turns that into regression tests: a case file lists inputs for one
script, drac runs the script on each input and compares exit code, stdout and stderr
with what was recorded before.

drac.py [-q] [-v] [-k PATTERN] FILE...  compare with the recorded snapshots
drac.py -r [-q] [-k PATTERN] FILE...  record (or re-record) the snapshots
drac.py -ab [-q] [-k PATTERN] FILE...  run the baseline (e.g. installed) script and the
    script under test on the same input and compare; nothing is stored, so live state
    cannot go stale
add -cov [-cov-include GLOB] [-cov-out DIR] to any of these to also report which lines
    of the scripts under test the cases ran (python exact, bash approximate)
FILE is a .toml case file or a directory of them.

Case file (TOML; paths are relative to the case file):

script = "../bin/backup.py"  # the version under test (required)
baseline = "/usr/local/bin/backup.py"  # for -ab
interpreter = "bash"  # optional; default runs the script directly
live = false  # true if it reads live state; never run from unittest
inherit_env = true  # start from the caller's environment (default) or a minimal one
env = { KEY = "value" }
scrub = [["regex", "replacement"], ...]
builtin_scrub = ["tmp", "sha", "date"]  # also available: "host", "user"
debug_flags = ["-n", "--dry-run"]  # a case must pass one of these unless safe = true
setup = "shell command"  # runs in $DRAC_TMP before every case
cwd = "${DRAC_TMP}"  # default; where the script runs
timeout = 120

[[case]]
name = "full"
args = ["--dry-run", "--source", "${DRAC_TMP}/input"]
# optional per case: env and scrub (added to the file's), setup (runs after the file's),
# cwd, stdin, timeout, safe = true (allow running without a dry-run flag)

Every case runs in a fresh temporary directory, $DRAC_TMP; $DRAC_DIR is the case file's
directory. ${NAME} in args, env, cwd and setup expands from the case's environment.
Output is normalised before comparing: the temporary directory becomes <TMP>, then the
file's scrub patterns apply, then the built-in ones (40-hex commits, dates, timestamps,
pid-named /tmp files; optionally host and user names).
Snapshots live in <case file without .toml>/<case name>.snap.
"""

import argparse
import contextlib
import difflib
import fnmatch
import getpass
import glob
import os
import re
import shutil
import socket
import subprocess
import sys
import tempfile
import types
import unittest
from dataclasses import dataclass, field
from typing import Optional

# drac.py also runs inside the interpreter of the script under test (see _python_cov_main), which
# may be older than 3.11: tomllib is imported where it is used.

_FILE_KEYS = {"script", "baseline", "interpreter", "live", "inherit_env", "env", "scrub", "builtin_scrub",
              "debug_flags", "setup", "cwd", "timeout", "sandbox", "network", "case"}
_CASE_KEYS = {"name", "args", "env", "scrub", "setup", "cwd", "stdin", "timeout", "safe", "sandbox"}
_MINIMAL_ENV = {"PATH", "HOME", "USER", "LOGNAME", "LANG", "TZ"}

_MONTHS = "Jan|Feb|Mar|Apr|May|Jun|Jul|Aug|Sep|Oct|Nov|Dec"
_BUILTIN_SCRUBS = {
    "tmp": [(r"/tmp/[\w.-]*\.\d+\b", "<TMPFILE>")],
    "sha": [(r"\b[0-9a-f]{40}\b", "<SHA>")],
    "date": [
        # date(1): Thu Oct 29 19:08:38 GMT 2026
        (rf"\b(?:Mon|Tue|Wed|Thu|Fri|Sat|Sun) (?:{_MONTHS}) [ \d]\d \d\d:\d\d:\d\d(?: [A-Z]{{2,5}})? \d{{4}}\b", "<DATE>"),
        # date '+%a %d %b %Y %H:%M': Fri 30 Oct 2026 00:44 GMT
        (rf"\b(?:Mon|Tue|Wed|Thu|Fri|Sat|Sun) [ \d]?\d (?:{_MONTHS}) \d{{4}} \d\d:\d\d(?::\d\d)?(?: [A-Z]{{2,5}})?\b", "<DATE>"),
        # ISO 8601: 2026-10-29, 2026-10-29T19:08:38+00:00
        (r"\b\d{4}-\d\d-\d\d(?:[T ]\d\d:\d\d(?::\d\d(?:\.\d+)?)?(?:Z|[+-]\d\d:?\d\d)?)?\b", "<DATE>"),
        # 20261029 19:08:38 GMT
        (r"\b20\d{6} \d\d:\d\d:\d\d(?: [A-Z]{2,5})?\b", "<DATE>"),
        # 20261029-190838-739560 (file name stamps)
        (r"\b20\d{6}[-_]\d{6}(?:[-.]\d{1,6})?\b", "<STAMP>"),
        (r"\b20\d\d(?:0[1-9]|1[0-2])(?:0[1-9]|[12]\d|3[01])\b", "<YYYYMMDD>"),
    ],
}


class DracError(Exception):
    """A case or case file that cannot be run (as opposed to a case whose output differs)."""


@dataclass
class Case:
    file: str
    name: str
    args: list[str]
    script: str
    baseline: Optional[str] = None
    interpreter: Optional[str] = None
    live: bool = True
    inherit_env: bool = True
    env: dict = field(default_factory=dict)
    scrub: list = field(default_factory=list)
    builtin_scrub: list = field(default_factory=lambda: ["tmp", "sha", "date"])
    debug_flags: list = field(default_factory=lambda: ["-n", "--dry-run"])
    setup: list = field(default_factory=list)
    cwd: Optional[str] = None
    stdin: Optional[str] = None
    timeout: float = 120
    safe: bool = False
    sandbox: Optional[dict] = None
    network: bool = False

    @property
    def id(self) -> str:
        return f"{os.path.splitext(os.path.basename(self.file))[0]}::{self.name}"

    @property
    def snapshot(self) -> str:
        return os.path.join(os.path.splitext(self.file)[0], f"{self.name}.snap")


def _resolve(base_dir: str, path: Optional[str]) -> Optional[str]:
    if path is None:
        return None
    path = os.path.expanduser(path)
    return path if os.path.isabs(path) else os.path.normpath(os.path.join(base_dir, path))


_STRING_LISTS = ("args", "debug_flags", "builtin_scrub")


def _check_type(where: str, key: str, value, kind):
    if not isinstance(value, kind):
        raise DracError(f"{where}: '{key}' has the wrong type")
    if key in _STRING_LISTS and not all(isinstance(v, str) for v in value):
        raise DracError(f"{where}: '{key}' must be a list of strings")


def _load_toml(f) -> dict:
    try:
        import tomllib
        return tomllib.load(f)
    except ModuleNotFoundError:
        try:
            import tomli  # type: ignore[import-not-found]
            return tomli.load(f)
        except ModuleNotFoundError:
            return _fallback_toml_loads(f.read().decode("utf-8"))
    except Exception as e:
        if type(e).__name__ == "TOMLDecodeError":
            raise DracError(f"not valid TOML: {e}") from None
        raise


def _fallback_toml_loads(text: str) -> dict:
    """Minimal TOML parser for Python < 3.11 when neither tomllib nor tomli is available."""
    import ast

    def strip_comment(line: str) -> str:
        quote = None
        i = 0
        while i < len(line):
            c = line[i]
            if quote:
                if quote == '"' and c == "\\":
                    i += 2
                    continue
                if c == quote:
                    quote = None
            elif c in ('"', "'"):
                quote = c
            elif c == "#":
                return line[:i]
            i += 1
        if quote:
            raise DracError("not valid TOML: unterminated string")
        return line

    def split_top(s: str, sep: str) -> list[str]:
        parts, cur, depth, quote = [], [], 0, None
        i = 0
        while i < len(s):
            c = s[i]
            if quote:
                cur.append(c)
                if quote == '"' and c == "\\":
                    i += 1
                    if i < len(s):
                        cur.append(s[i])
                elif c == quote:
                    quote = None
            elif c in ('"', "'"):
                quote = c
                cur.append(c)
            elif c in "[{":
                depth += 1
                cur.append(c)
            elif c in "]}":
                depth -= 1
                cur.append(c)
            elif c == sep and depth == 0:
                parts.append("".join(cur).strip())
                cur = []
            else:
                cur.append(c)
            i += 1
        if quote or depth != 0:
            raise DracError("not valid TOML: unbalanced quotes or brackets")
        tail = "".join(cur).strip()
        if tail:
            parts.append(tail)
        return parts

    def parse_key(k: str) -> str:
        k = k.strip()
        if len(k) >= 2 and k[0] == k[-1] and k[0] in ('"', "'"):
            return ast.literal_eval(k) if k[0] == '"' else k[1:-1]
        if not re.match(r"^[\w-]+$", k):
            raise DracError(f"not valid TOML: bad key {k!r}")
        return k

    def parse_val(v: str):
        v = v.strip()
        if not v:
            raise DracError("not valid TOML: missing value")
        if v == "true":
            return True
        if v == "false":
            return False
        if v[0] == "'" and v.endswith("'") and len(v) >= 2:
            return v[1:-1]
        if v[0] == '"' and v.endswith('"') and len(v) >= 2:
            try:
                return ast.literal_eval(v)
            except SyntaxError as e:
                raise DracError(f"not valid TOML: {e}") from None
        if v[0] == "[" and v.endswith("]"):
            inner = v[1:-1].strip()
            return [parse_val(x) for x in split_top(inner, ",")] if inner else []
        if v[0] == "{" and v.endswith("}"):
            inner = v[1:-1].strip()
            out = {}
            for item in (split_top(inner, ",") if inner else []):
                if "=" not in item:
                    raise DracError("not valid TOML: bad inline table")
                ik, iv = item.split("=", 1)
                out[parse_key(ik)] = parse_val(iv)
            return out
        if re.match(r"^[+-]?\d+$", v):
            return int(v)
        if re.match(r"^[+-]?\d+\.\d+$", v):
            return float(v)
        raise DracError(f"not valid TOML: {v!r}")

    root: dict = {}
    target = root
    for raw in text.splitlines():
        line = strip_comment(raw).strip()
        if not line:
            continue
        if line.startswith("[[") and line.endswith("]]"):
            sec = parse_key(line[2:-2])
            target = {}
            root.setdefault(sec, []).append(target)
        elif line.startswith("[") and line.endswith("]"):
            sec = parse_key(line[1:-1])
            target = root.setdefault(sec, {})
            if not isinstance(target, dict):
                raise DracError(f"not valid TOML: [{sec}] conflicts")
        elif "=" in line:
            k, v = line.split("=", 1)
            target[parse_key(k)] = parse_val(v)
        else:
            raise DracError(f"not valid TOML: {line!r}")
    return root


def load_cases(path: str) -> list[Case]:
    path = os.path.abspath(path)
    where = os.path.basename(path)
    try:
        with open(path, "rb") as f:
            data = _load_toml(f)
    except OSError as e:
        raise DracError(f"{where}: {e.strerror}")
    except DracError as e:
        raise DracError(f"{where}: {e}")

    unknown = set(data) - _FILE_KEYS
    if unknown:
        raise DracError(f"{where}: unknown key(s) {', '.join(sorted(unknown))}")
    if "script" not in data:
        raise DracError(f"{where}: 'script' is required")
    types_ = {"script": str, "baseline": str, "interpreter": str, "live": bool, "inherit_env": bool, "env": dict,
              "scrub": list, "builtin_scrub": list, "debug_flags": list, "setup": str, "cwd": str,
              "timeout": (int, float), "sandbox": dict, "network": bool, "case": list}
    for key, kind in types_.items():
        if key in data:
            _check_type(where, key, data[key], kind)
    unknown_builtins = set(data.get("builtin_scrub", [])) - set(_BUILTIN_SCRUBS) - {"host", "user"}
    if unknown_builtins:
        raise DracError(f"{where}: unknown builtin_scrub {', '.join(sorted(unknown_builtins))}")

    base = os.path.dirname(path)
    cases: list[Case] = []
    seen: set[str] = set()
    for i, c in enumerate(data.get("case", [])):
        cwhere = f"{where} case {i + 1}"
        if not isinstance(c, dict):
            raise DracError(f"{cwhere}: must be a table ([[case]])")
        unknown = set(c) - _CASE_KEYS
        if unknown:
            raise DracError(f"{cwhere}: unknown key(s) {', '.join(sorted(unknown))}")
        for key in ("name", "args"):
            if key not in c:
                raise DracError(f"{cwhere}: '{key}' is required")
        ctypes = {"name": str, "args": list, "env": dict, "scrub": list, "setup": str, "cwd": str, "stdin": str,
                  "timeout": (int, float), "safe": bool, "sandbox": dict}
        for key, kind in ctypes.items():
            if key in c:
                _check_type(cwhere, key, c[key], kind)
        if not re.match(r"^[\w.-]+$", c["name"]):
            raise DracError(f"{cwhere}: name '{c['name']}' may only use letters, digits, '_', '.', '-'")
        if c["name"] in seen:
            raise DracError(f"{where}: duplicate case name '{c['name']}'")
        seen.add(c["name"])
        scrub = data.get("scrub", []) + c.get("scrub", [])
        for pair in scrub:
            if not (isinstance(pair, list) and len(pair) == 2 and all(isinstance(p, str) for p in pair)):
                raise DracError(f"{cwhere}: scrub entries must be [\"regex\", \"replacement\"]")
            try:
                re.compile(pair[0])
            except re.error as e:
                raise DracError(f"{cwhere}: bad scrub regex {pair[0]!r}: {e}")
        cases.append(Case(
            file=path,
            name=c["name"],
            args=c["args"],
            script=_resolve(base, data["script"]),
            baseline=_resolve(base, data.get("baseline")),
            interpreter=data.get("interpreter"),
            live=data.get("live", True),
            inherit_env=data.get("inherit_env", True),
            env={**data.get("env", {}), **c.get("env", {})},
            scrub=scrub,
            builtin_scrub=data.get("builtin_scrub", ["tmp", "sha", "date"]),
            debug_flags=data.get("debug_flags", ["-n", "--dry-run"]),
            setup=[s for s in (data.get("setup"), c.get("setup")) if s],
            cwd=c.get("cwd", data.get("cwd")),
            stdin=c.get("stdin"),
            timeout=c.get("timeout", data.get("timeout", 120)),
            safe=c.get("safe", False),
            sandbox=_sandbox(cwhere, data, c),
            network=data.get("network", "sandbox" not in data and "sandbox" not in c),
        ))
    if not cases:
        raise DracError(f"{where}: no [[case]] entries")
    return cases


def _sandbox(where: str, data: dict, case: dict) -> Optional[dict]:
    """The case's {"/absolute/mount point": "source directory"} (the file's entries, then the case's), or None."""
    if "sandbox" not in data and "sandbox" not in case:
        return None
    mounts = {**data.get("sandbox", {}), **case.get("sandbox", {})}
    for target, source in mounts.items():
        if not os.path.isabs(target) or not isinstance(source, str):
            raise DracError(f'{where}: sandbox entries must be "/absolute/mount/point" = "source directory"')
    return mounts


def find_case_files(paths: list[str]) -> list[str]:
    files: list[str] = []
    for p in paths:
        if os.path.isdir(p):
            files.extend(sorted(glob.glob(os.path.join(p, "*.toml"))))
        else:
            files.append(p)
    return files


def _expand(text: str, env: dict) -> str:
    return re.sub(r"\$\{(\w+)\}", lambda m: env.get(m.group(1), ""), text)


def normalise(text: str, case: Case, tmp: str) -> str:
    for t in sorted({tmp, os.path.realpath(tmp)}, key=len, reverse=True):
        text = text.replace(t, "<TMP>")
    for pattern, replacement in case.scrub:
        text = re.sub(pattern, replacement, text)
    for name in case.builtin_scrub:
        if name == "host":
            for h in sorted({socket.gethostname(), socket.gethostname().split(".")[0]}, key=len, reverse=True):
                text = re.sub(rf"\b{re.escape(h)}\b", "<HOST>", text)
        elif name == "user":
            text = re.sub(rf"\b{re.escape(getpass.getuser())}\b", "<USER>", text)
        else:
            for pattern, replacement in _BUILTIN_SCRUBS[name]:
                text = re.sub(pattern, replacement, text)
    return text

# --- Coverage (-cov) ---
# Which lines of the scripts under test the cases run. Python is exact: the script runs under
# sys.monitoring in its own interpreter. Bash is approximate: xtrace goes to a separate file
# descriptor (the compared output is untouched) and a heuristic decides which lines are code.

_PY_COV_FLAG = "--drac-cov-python"
_MARK = "\x01"
_PS4 = f"+{_MARK}${{BASH_SOURCE}}{_MARK}${{LINENO}}{_MARK} "
_XTRACE_LINE = re.compile(f"{_MARK}([^{_MARK}]*){_MARK}(\\d+){_MARK}")


def _python_cov_main(argv: list[str]) -> None:
    """Runs `script args` like `python script args` and appends the lines it ran to `out` as JSON."""
    import json
    import runpy

    out, script, *args = argv
    me = os.path.abspath(__file__)
    target = os.path.abspath(script)
    cov_code = _python_cov_main.__code__
    hits: set = set()
    mon = getattr(sys, "monitoring", None)
    if mon:
        mon.use_tool_id(mon.COVERAGE_ID, "drac")

        def on_line(code, line):
            if code is not cov_code:
                hits.add((code.co_filename, line))
            return mon.DISABLE

        mon.register_callback(mon.COVERAGE_ID, mon.events.LINE, on_line)
        mon.set_events(mon.COVERAGE_ID, mon.events.LINE)
    else:  # before python3.12

        def tracer(frame, event, arg):
            if event == "line" and frame.f_code is not cov_code:
                hits.add((frame.f_code.co_filename, frame.f_lineno))
            return tracer

        sys.settrace(tracer)

    sys.argv = [script, *args]
    sys.path[0] = os.path.dirname(target)
    try:
        runpy.run_path(script, run_name="__main__")
    finally:
        if mon:
            mon.set_events(mon.COVERAGE_ID, 0)
        else:
            sys.settrace(None)
        skip = (sys.prefix + os.sep, sys.base_prefix + os.sep)
        with open(out, "a") as f:
            for filename, line in hits:
                path = os.path.abspath(filename)
                if (path != me or target == me) and not path.startswith(skip) and os.path.isfile(path):
                    f.write(json.dumps([path, line]) + "\n")


def _language(case: Case, script: str) -> tuple[Optional[str], list[str]]:
    """("python", interpreter command), ("bash", ["bash"]) or (None, []) if coverage cannot be measured."""
    if case.interpreter:
        words = [case.interpreter]
    else:
        try:
            with open(script, errors="replace") as f:
                first = f.readline()
        except OSError:
            return None, []
        words = first[2:].split() if first.startswith("#!") else []
    if words and os.path.basename(words[0]) == "env":
        words = words[1:]
    if not words:
        return None, []
    if "python" in os.path.basename(words[0]):
        return "python", words
    if os.path.basename(words[0]) in ("bash", "sh"):
        return "bash", ["bash"]
    return None, []


def python_units(path: str) -> dict[int, list[int]]:
    """Lines of a python file that hold code, as {line: [line]}."""
    try:
        with open(path) as f:
            code = compile(f.read(), path, "exec", dont_inherit=True)
    except (OSError, SyntaxError, ValueError):
        return {}
    lines: set[int] = set()
    stack = [code]
    while stack:
        co = stack.pop()
        if hasattr(co, "co_lines"):
            lines.update(line for _, _, line in co.co_lines() if line)
        else:
            import dis
            lines.update(line for _, line in dis.findlinestarts(co) if line)
        stack.extend(c for c in co.co_consts if isinstance(c, types.CodeType))
    return {line: [line] for line in sorted(lines)}


# Lines bash xtrace never reports (checked against bash 5.1): structure, function headers, case patterns
_BASH_STRUCTURE = re.compile(r"(then|else|fi|do|done|esac|;;|\{|\}|\(|\)|in)\s*(;;)?\s*(#.*)?")
_BASH_FUNCTION = re.compile(r"(function\s+[\w.:-]+(\s*\(\s*\))?|[\w.:-]+\s*\(\s*\))\s*\{?\s*(#.*)?")
_BASH_CASE_PATTERN = re.compile(r"[^()\s#][^()]*\)\s*(#.*)?")
_BASH_CLOSER = re.compile(r"(done|fi|esac|\})\b")
_BASH_HEREDOC = re.compile(r"(?<!<)<<(-?)\s*(['\"]?)([A-Za-z_][\w.-]*)\2")


def _bash_is_code(line: str) -> bool:
    s = line.strip()
    if not s or s.startswith("#"):
        return False
    if _BASH_STRUCTURE.fullmatch(s) or _BASH_FUNCTION.fullmatch(s) or _BASH_CASE_PATTERN.fullmatch(s):
        return False
    if _BASH_CLOSER.match(s):
        # `done < <(cmd)` or `done | sort` run a command on this line; `done < file` does not
        return bool(re.search(r"\$\(|<\(|\|", s))
    return True


def _bash_open_quote(text: str) -> bool:
    """True if text ends inside a quoted string (the statement continues on the next line)."""
    quote = None
    i = 0
    while i < len(text):
        c = text[i]
        if quote == "'":
            if c == "'":
                quote = None
        elif quote == '"':
            if c == "\\":
                i += 1
            elif c == '"':
                quote = None
        elif c == "\\":
            i += 1
        elif c in "'\"":
            quote = c
        elif c == "#" and (i == 0 or text[i - 1] in " \t\n;"):
            end = text.find("\n", i)
            if end < 0:
                break
            i = end
        i += 1
    return quote is not None


def bash_units(path: str) -> dict[int, list[int]]:
    """
    Statements of a bash script that hold code, as {reported line: [all its lines]} (approximate).
    A statement continued with a trailing backslash or an open quote spans several lines; xtrace
    reports it on one of them, so the statement counts as run if any of its lines was traced.
    """
    try:
        with open(path, errors="replace") as f:
            lines = f.read().splitlines()
    except OSError:
        return {}
    units: dict[int, list[int]] = {}
    heredocs: list[tuple[str, bool]] = []
    i = 0
    while i < len(lines):
        if heredocs:
            word, strip_tabs = heredocs[0]
            if (lines[i].lstrip("\t") if strip_tabs else lines[i]) == word:
                heredocs.pop(0)
            i += 1
            continue
        group = [i + 1]
        text = lines[i]
        while i + 1 < len(lines) and (text.endswith("\\") or _bash_open_quote(text)):
            i += 1
            group.append(i + 1)
            text += "\n" + lines[i]
        if _bash_is_code(lines[group[0] - 1]):
            units[group[0]] = group
        heredocs.extend((m.group(3), m.group(1) == "-") for m in _BASH_HEREDOC.finditer(text))
        i += 1
    return units


@dataclass
class Coverage:
    """Lines run, collected over all cases. Only files under `include` (or, by default, under the
    directories of the scripts under test) are reported, which leaves out e.g. ~/.bashrc via BASH_ENV."""
    include: list = field(default_factory=list)
    hits: dict = field(default_factory=dict)
    kinds: dict = field(default_factory=dict)
    roots: set = field(default_factory=set)
    unmeasured: set = field(default_factory=set)
    aliases: dict = field(default_factory=dict)  # copy that ran -> the file it is a copy of

    def wanted(self, path: str) -> bool:
        if self.include:
            return any(fnmatch.fnmatch(path, p) or fnmatch.fnmatch(os.path.basename(path), p) for p in self.include)
        return any(path.startswith(root + os.sep) for root in self.roots)

    def start(self, script: str, kind: str):
        script = os.path.abspath(script)
        self.roots.add(os.path.dirname(script))
        self.kinds.setdefault(script, kind)
        self.hits.setdefault(script, set())

    def add(self, path: str, line: int, kind: str):
        path = os.path.abspath(path)
        path = self.aliases.get(path, path)
        if self.wanted(path) and os.path.isfile(path):
            self.kinds.setdefault(path, kind)
            self.hits.setdefault(path, set()).add(line)

    def collect_python(self, cov_file: str):
        import json

        if os.path.exists(cov_file):
            with open(cov_file) as f:
                for entry in f:
                    path, line = json.loads(entry)
                    self.add(path, line, "python")

    def collect_bash(self, trace_file: str, cwd: str):
        with open(trace_file, errors="replace") as f:
            for entry in f:
                m = _XTRACE_LINE.search(entry)
                if m and m.group(1):
                    self.add(os.path.join(cwd, m.group(1)), int(m.group(2)), "bash")

    def results(self) -> list[tuple[str, str, dict, set]]:
        """[(path, kind, units, lines run)] for every reported file."""
        out = []
        for path in sorted(self.kinds):
            if not self.wanted(path):
                continue
            kind = self.kinds[path]
            units = python_units(path) if kind == "python" else bash_units(path)
            out.append((path, kind, units, self.hits.get(path, set())))
        return out

    def report(self) -> str:
        rows = []
        for path, kind, units, hit in self.results():
            run = {u for u, lines in units.items() if any(line in hit for line in lines)}
            pct = 100.0 * len(run) / len(units) if units else 100.0
            rows.append((_display(path), kind, f"{len(run)}/{len(units)}", f"{pct:.1f}%",
                         _ranges([u for u in units if u not in run], list(units))))
        lines = ["", "Coverage (python: exact; bash: approximate)"]
        if rows:
            w = [max(len(r[i]) for r in rows) for i in range(4)]
            for name, kind, count, pct, missing in rows:
                line = f"{name:<{w[0]}} {kind:<{w[1]}} {count:<{w[2]}} {pct:<{w[3]}}"
                lines.append(line + (f" not run: {missing}" if missing else ""))
        else:
            lines.append(" nothing measured")
        for script in sorted(self.unmeasured):
            lines.append(f" not measured (only python and bash are supported): {_display(script)}")
        return "\n".join(lines)

    def annotate(self, out_dir: str) -> list[str]:
        """Writes <out_dir>/<file>.cov listings: '+' run, '-' not run, ' ' not code. Returns the paths."""
        os.makedirs(out_dir, exist_ok=True)
        written = []
        for path, kind, units, hit in self.results():
            run_lines = {line for lines in units.values() if any(x in hit for x in lines) for line in lines}
            code_lines = {line for lines in units.values() for line in lines}
            name = os.path.basename(path)
            target = os.path.join(out_dir, f"{name}.cov")
            n = 2
            while target in written:
                target = os.path.join(out_dir, f"{name}.{n}.cov")
                n += 1
            with open(path, errors="replace") as src, open(target, "w") as f:
                f.write(f"#{path}({kind}) '+' run, '-' not run\n")
                for number, text in enumerate(src.read().splitlines(), 1):
                    mark = "+" if number in run_lines else "-" if number in code_lines else " "
                    f.write(f"{mark} {number:5}    {text}\n")
            written.append(target)
        return written


def _display(path: str) -> str:
    rel = os.path.relpath(path)
    return path if rel.startswith("..") else rel


def _ranges(missing: list[int], order: list[int]) -> str:
    """'3-5, 9': runs of not-run units, consecutive in `order` (so blank lines do not split a run)."""
    if not missing:
        return ""
    position = {u: i for i, u in enumerate(order)}
    runs: list[list[int]] = []
    for u in sorted(missing, key=position.get):
        if runs and position[u] == position[runs[-1][-1]] + 1:
            runs[-1].append(u)
        else:
            runs.append([u])
    return ", ".join(str(r[0]) if len(r) == 1 else f"{r[0]}-{r[-1]}" for r in runs)

# --- Sandbox ---
# Scripts that hard-code absolute paths (e.g. /var/lib/myapp/...) run unmodified against fixtures:
# inside a private user + mount namespace each `sandbox` source is bind-mounted over its mount
# point, and (unless network = true) inside a private network namespace with only loopback, so a
# dry run that leaks an ssh or rsync fails instead of reaching a host. Writes land in the fixture
# directories. File descriptor 9 is the run's own stderr, for stand-in commands (fixtures/fakebin)
# to report calls even when the script redirects their stderr elsewhere.

_SANDBOX_FAILED = 125
_SANDBOX_SH = ('while [ "$1" != -- ]; do mount --bind "$1" "$2" || '
               ' { echo "drac-sandbox: cannot mount $1 on $2" >&2; exit 125; }; shift 2; done; shift; '
               'exec 9>&2; exec "$@"')
_sandbox_problem: Optional[str] = None


def _sandbox_available() -> Optional[str]:
    """None if unprivileged namespaces work here, otherwise why not (checked once)."""
    global _sandbox_problem
    if _sandbox_problem is None:
        if not shutil.which("unshare"):
            _sandbox_problem = "unshare (util-linux) is not installed"
        else:
            res = subprocess.run(["unshare", "--user", "--map-root-user", "--mount", "--net", "true"],
                                 capture_output=True, text=True)
            _sandbox_problem = "" if res.returncode == 0 else (res.stderr.strip() or f"exit {res.returncode}")
    return _sandbox_problem or None


def _sandboxed(case: Case, cmd: list[str], env: dict) -> list[str]:
    problem = _sandbox_available()
    if problem:
        raise DracError(f"sandbox not available: {problem}")
    mounts: list[str] = []
    placed: list[tuple[str, str]] = []  # (mount point, source), to find nested mount points
    for target, source in sorted(case.sandbox.items()):
        target = os.path.normpath(_expand(target, env))
        source = _resolve(os.path.dirname(case.file), _expand(source, env))
        if not os.path.isdir(source):
            raise DracError(f"sandbox source is not a directory: {source}")
        # A mount point inside an earlier mount is looked up in that mount's source
        seen_as = target
        for outer, outer_source in placed:
            if target.startswith(outer + os.sep):
                seen_as = outer_source + target[len(outer):]
        if not os.path.isdir(seen_as):
            raise DracError(f"sandbox mount point does not exist: {target}")
        placed.append((target, source))
        mounts += [source, target]
    return (["unshare", "--user", "--map-root-user", "--mount"] + ([] if case.network else ["--net"])
            + ["--", "bash", "-c", _SANDBOX_SH, "drac-sandbox", *mounts, "--", *cmd])


def execute(case: Case, script: str, cov: Optional[Coverage] = None) -> str:
    """Runs one case with the given script and returns its normalised output."""
    if not case.safe and not any(f in case.args for f in case.debug_flags):
        raise DracError(f"refusing to run without {' or '.join(case.debug_flags)}; "
                       "set safe = true if this case cannot change anything")
    if not os.path.exists(script):
        raise DracError(f"script not found: {script}")

    with tempfile.TemporaryDirectory(prefix="drac_") as tmp:
        if case.inherit_env:
            env = dict(os.environ)
        else:
            env = {k: os.environ[k] for k in _MINIMAL_ENV if k in os.environ}
        env.update(DRAC="1", DRAC_TMP=tmp, DRAC_DIR=os.path.dirname(case.file))
        for k, v in case.env.items():
            env[k] = _expand(str(v), env)

        for command in case.setup:
            try:
                res = subprocess.run(["bash", "-c", _expand(command, env)], cwd=tmp, env=env,
                                     capture_output=True, text=True, timeout=case.timeout)
            except subprocess.TimeoutExpired:
                raise DracError(f"setup timed out after {case.timeout}s")
            if res.returncode != 0:
                raise DracError(f"setup failed (exit {res.returncode}): {(res.stderr or res.stdout).strip()}")

        cwd = _resolve(os.path.dirname(case.file), _expand(case.cwd, env)) if case.cwd else tmp
        if not os.path.isdir(cwd):
            raise DracError(f"cwd is not a directory: {cwd}")
        args = [_expand(a, env) for a in case.args]
        # A script under a sandboxed path (e.g. an installed /usr/local/bin/tool as -ab baseline)
        # would be hidden by its own fixture: run a copy from outside instead
        run_script = os.path.abspath(script)
        if case.sandbox is not None and any(
                run_script.startswith(os.path.normpath(_expand(t, env)) + os.sep) for t in case.sandbox):
            run_script = os.path.join(tempfile.mkdtemp(prefix="drac_script_"), os.path.basename(script))
            shutil.copy2(script, run_script)
            if cov is not None:
                cov.aliases[run_script] = os.path.abspath(script)
        cmd = ([case.interpreter] if case.interpreter else []) + [run_script] + args

        # Coverage files live outside $DRAC_TMP so the script cannot see them in its own output
        run_env, pass_fds, cov_dir, trace = env, (), None, None
        if cov is not None:
            kind, interpreter = _language(case, script)
            if kind is None:
                cov.unmeasured.add(os.path.abspath(script))
            else:
                cov.start(script, kind)
                cov_dir = tempfile.mkdtemp(prefix="drac_cov_")
                if kind == "python":
                    cmd = interpreter + [os.path.abspath(__file__), _PY_COV_FLAG,
                                          os.path.join(cov_dir, "python.json"), run_script] + args
                else:
                    trace = open(os.path.join(cov_dir, "xtrace"), "w")
                    # bash ignores PS4 from the environment when running as root (as in the sandbox), so
                    # every bash also sets it from BASH_ENV, then sources the caller's own BASH_ENV
                    bash_env = os.path.join(cov_dir, "bash_env")
                    with open(bash_env, "w") as f:
                        f.write("PS4=$'" + _PS4.replace(_MARK, "\\x01") + "'\n"
                                "[ -n \"$DRAC_BASH_ENV\" ] && . \"$DRAC_BASH_ENV\"\n")
                    run_env = {**env, "PS4": _PS4, "SHELLOPTS": "xtrace", "BASH_XTRACEFD": str(trace.fileno()),
                               "BASH_ENV": bash_env, "DRAC_BASH_ENV": env.get("BASH_ENV", "")}
                    pass_fds = (trace.fileno(),)
                    cmd = ["bash", run_script] + args

        try:
            if case.sandbox is not None:
                cmd = _sandboxed(case, cmd, env)
            res = subprocess.run(cmd, cwd=cwd, env=run_env, input=case.stdin or "", capture_output=True, text=True,
                                 timeout=case.timeout, pass_fds=pass_fds)
        except subprocess.TimeoutExpired:
            raise DracError(f"timed out after {case.timeout}s")
        except OSError as e:
            raise DracError(f"cannot run {script}: {e.strerror}")
        finally:
            if run_script != os.path.abspath(script):
                shutil.rmtree(os.path.dirname(run_script), ignore_errors=True)
            if cov_dir:
                if trace:
                    trace.close()
                    cov.collect_bash(trace.name, cwd)
                else:
                    cov.collect_python(os.path.join(cov_dir, "python.json"))
                shutil.rmtree(cov_dir, ignore_errors=True)

    if case.sandbox is not None and res.returncode == _SANDBOX_FAILED and "drac-sandbox:" in res.stderr:
        raise DracError(res.stderr.strip().splitlines()[-1])

    text = (
        f"$ {os.path.basename(case.script)} {' '.join(case.args)}\n"
        f"--- exit {res.returncode}\n"
        f"--- stdout\n{_ended(res.stdout)}"
        f"--- stderr\n{_ended(res.stderr)}"
    )
    # Scripts print their own path ($0, usage); the script and its baseline must compare equal
    for path in sorted({run_script, os.path.abspath(script)}, key=len, reverse=True):
        text = text.replace(path, os.path.basename(case.script))
    return normalise(text, case, tmp)


def _ended(output: str) -> str:
    """Output ending in a newline, so the next section header starts on its own line."""
    return output if not output or output.endswith("\n") else output + "\n"


def _diff(expected: str, actual: str, from_name: str, to_name: str) -> str:
    return "".join(difflib.unified_diff(expected.splitlines(keepends=True),
                                        actual.splitlines(keepends=True), from_name, to_name))


def check_case(case: Case, mode: str, cov: Optional[Coverage] = None) -> tuple[str, str]:
    """
    Runs one case. mode is "compare", "record" or "ab"; with cov, measures the script under test
    (never the baseline). Returns (status, detail): status is pass, fail, recorded or error;
    detail is a diff or message.
    """
    try:
        if mode == "ab":
            if not case.baseline:
                raise DracError("no baseline configured for -ab")
            expected = execute(case, case.baseline)
            actual = execute(case, case.script, cov)
            return ("pass", "") if expected == actual else ("fail", _diff(expected, actual, "baseline", "candidate"))
        actual = execute(case, case.script, cov)
        if mode == "record":
            os.makedirs(os.path.dirname(case.snapshot), exist_ok=True)
            with open(case.snapshot, "w") as f:
                f.write(actual)
            return "recorded", ""
        if not os.path.exists(case.snapshot):
            return "fail", f"no snapshot ({case.snapshot}); record it with -r"
        with open(case.snapshot) as f:
            expected = f.read()
        return ("pass", "") if expected == actual else ("fail", _diff(expected, actual, "snapshot", "actual"))
    except DracError as e:
        return "error", str(e)


_MARKS = {"pass": (".", "PASS"), "fail": ("F", "FAIL"), "recorded": ("R", "RECORDED"), "error": ("E", "ERROR")}


def run_cli(argv: list[str], stdout=sys.stdout, stderr=sys.stderr) -> int:
    parser = argparse.ArgumentParser(prog="drac.py", description="Record and compare dry-run output.")
    mode = parser.add_mutually_exclusive_group()
    mode.add_argument("-r", action="store_true", help="record (overwrite) snapshots")
    mode.add_argument("-ab", action="store_true", help="compare the baseline script with the script under test")
    parser.add_argument("-q", action="store_true", help="one character per case; details only for failures")
    parser.add_argument("-v", action="store_true", help="also show the command line of each case")
    parser.add_argument("-k", metavar="PATTERN", help="only cases whose file::name matches (fnmatch, substring)")
    parser.add_argument("-cov", action="store_true", help="also report which lines of the scripts under test ran")
    parser.add_argument("-cov-include", metavar="GLOB", action="append", default=[],
                        help="Files to report (path or file-name glob, repeatable; implies -cov); "
                             "default: files under the directories of the scripts under test")
    parser.add_argument("-cov-out", metavar="DIR", help="also write annotated listings to DIR (implies -cov)")
    parser.add_argument("files", nargs="+", help=".toml case files or directories of them")
    try:
        with contextlib.redirect_stdout(stdout), contextlib.redirect_stderr(stderr):
            args = parser.parse_args(argv)
    except SystemExit as e:
        return 2 if e.code else 0
    run_mode = "record" if args.r else "ab" if args.ab else "compare"
    cov = Coverage(include=args.cov_include) if args.cov or args.cov_include or args.cov_out else None

    try:
        cases = [c for f in find_case_files(args.files) for c in load_cases(f)]
    except DracError as e:
        print(f"drac: {e}", file=stderr)
        return 2
    if args.k:
        pattern = args.k if any(ch in args.k for ch in "*?[") else f"*{args.k}*"
        cases = [c for c in cases if fnmatch.fnmatch(c.id, pattern)]

    counts = {status: 0 for status in _MARKS}
    details: list[str] = []
    for case in cases:
        if args.v and not args.q:
            print(f"  {os.path.basename(case.script)} {' '.join(case.args)}", file=stdout)
        status, detail = check_case(case, run_mode, cov)
        counts[status] += 1
        mark, word = _MARKS[status]
        if args.q:
            print(mark, end="", file=stdout, flush=True)
        else:
            print(f"{word}: {case.id}", file=stdout, flush=True)
        if detail:
            details.append(f"--- {word}: {case.id} ---\n{detail.rstrip()}\n")

    if args.q:
        print(file=stdout)
    if details:
        print("\n" + "\n".join(details), file=stdout)
    print(f"{counts['pass']} passed, {counts['fail']} failed, {counts['error']} errors, "
          f"{counts['recorded']} recorded", file=stdout)
    if cov is not None:
        print(cov.report(), file=stdout)
        if args.cov_out:
            written = cov.annotate(args.cov_out)
            print(f"\nAnnotated listings ({len(written)}) in {args.cov_out}", file=stdout)
    return 1 if counts["fail"] or counts["error"] else 0


def make_test_class(paths: list[str], name: str = "TestDracSnapshots") -> type:
    """
    A unittest.TestCase class with one test per case, comparing with recorded snapshots. Files with
    live = true are left out (they depend on external state); cases without a snapshot are skipped.
    """
    attrs: dict = {}
    for path in find_case_files(paths):
        for case in load_cases(path):
            if case.live:
                continue

            def test(self, case=case):
                if not os.path.exists(case.snapshot):
                    self.skipTest(f"no snapshot; record with: drac.py -r -k '{case.id}' {case.file}")
                if case.sandbox is not None and _sandbox_available():
                    self.skipTest(f"sandbox not available: {_sandbox_available()}")
                status, detail = check_case(case, "compare")
                if status != "pass":
                    self.fail(f"{case.id}: {status}\n{detail}")
            attrs["test_" + re.sub(r"\W+", "_", case.id)] = test
    return type(name, (unittest.TestCase,), attrs)


if __name__ == "__main__":
    if sys.argv[1:2] == [_PY_COV_FLAG]:
        _python_cov_main(sys.argv[2:])
    else:
        sys.exit(run_cli(sys.argv[1:]))