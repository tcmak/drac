# DRAC

```
|_||_||_|   DRAC: Dry Run Automatic Checker
| || || |   "Sucking the bugs out of your scripts"
|_||_||_|
```

Many tools and scripts, bash or python, take a dry-run flag (`-n` or `--dry-run`) and print what they would do instead of doing it. That output is often the closest thing a script has to a specification, and checking it by eye after every change is slow and easy to get wrong. `drac.py` records the dry-run output for a set of inputs and tells you when it changes.

## Quick start

```
./drac.py tests                          # compare every case with its snapshot
./drac.py -r tests/backup.toml           # record (or re-record) snapshots
./drac.py -q -k full tests               # quiet; only cases matching "full"
./drac.py -ab tests/live/report.toml     # baseline copy vs working copy
./drac.py -cov -cov-out /tmp/cov tests   # ...and which lines the cases ran
```

The offline cases also run with the rest of the test suite:

```
python3 -m unittest discover -s . -p 'test_*.py'
```

## Modes

| Command | What it does | Stores anything? |
|---|---|---|
| `drac.py FILE...` | Runs each case and compares with its snapshot. A missing snapshot is a **failure** ("record it with `-r`"), never a silent pass. | No |
| `drac.py -r FILE...` | Runs each case and writes its snapshot, overwriting the old one. | Yes |
| `drac.py -ab FILE...` | Runs the `baseline` script and the `script` under test on the same input and compares the two outputs. | No |

`FILE` is a `.toml` case file or a directory (its `*.toml` files; subdirectories are not searched).

| Option | Meaning |
|---|---|
| `-q` | One character per case (`.` pass, `F` fail, `E` error, `R` recorded); details only for failures. |
| `-v` | Also print each case's command line. |
| `-k PATTERN` | Only cases whose `file::name` matches: a glob if it contains `*?[`, otherwise a substring. |
| `-cov` | Also report which lines of the scripts under test the cases ran (see [Coverage](#coverage)). |
| `-cov-include GLOB` | Report these files (path or file-name glob, repeatable). Implies `-cov`. |
| `-cov-out DIR` | Also write annotated listings to `DIR`. Implies `-cov`. |

Exit status: `0` all passed or recorded, `1` any failure or error, `2` bad command line or case file.

### Snapshot or A/B?

Many dry runs read **live state** (filesystems, databases, repositories, config files, today's date). A snapshot of that goes stale as soon as external state changes, without any code changing. So there are two kinds of case file:

- **Offline** (`live = false`): the case builds its own input in a temporary directory (a fixture) and the output is compared with a stored snapshot. Stable, so these run in `unittest`. Use for scripts that accept path options or environment overrides.
- **Live** (`live = true`, the default): use only with `-ab`. The baseline script and your modified copy run back to back on the same real state; nothing is stored, so nothing goes stale. A pass means *your change does not alter what the script prints for that input today*. Keep these under `tests/live/` so the unittest hook never sees them.

## Case files

One TOML file per script. Paths are relative to the case file.

```toml
# tests/backup.toml
script      = "../bin/backup.py"
interpreter = "python3"
live        = false
setup       = 'bash "${DRAC_DIR}/fixtures/make_sample_data.sh"'
scrub       = [["\\b[0-9a-f]{10}\\b", "<ID>"]]

[env]
DATA_DIR = "${DRAC_TMP}/data"

[[case]]
name = "full"
args = ["--dry-run", "--source", "${DRAC_TMP}/input"]

[[case]]
name = "usage"
args = ["-h"]
safe = true
```

### File keys

| Key | Default | Meaning |
|---|---|---|
| `script` | (required) | The version under test. |
| `baseline` | none | The version to compare against with `-ab`, e.g. the installed copy (`/usr/local/bin/...`). |
| `interpreter` | none | Run as `interpreter script args` (e.g. `"bash"`, `"python3"`). Without it the script must be executable. |
| `live` | `true` | `true` if the script reads live state. Live files are never run by `unittest`. |
| `inherit_env` | `true` | Start from your environment. `false` keeps only `PATH`, `HOME`, `USER`, `LOGNAME`, `LANG`, `TZ`. |
| `env` | `{}` | Extra environment variables (an `[env]` table works too). |
| `setup` | none | Shell command (bash) run in `$DRAC_TMP` before every case, e.g. to build a fixture. |
| `cwd` | `$DRAC_TMP` | Where the script runs. Scripts that resolve paths from the current directory need this. |
| `scrub` | `[]` | `[["regex", "replacement"], ...]` applied to the output before comparing. |
| `builtin_scrub` | `["tmp", "sha", "date"]` | Built-in normalisations to apply; `"host"` and `"user"` are also available. |
| `debug_flags` | `["-n", "--dry-run"]` | A case must pass one of these (see [Safety](#safety)). |
| `timeout` | `120` | Seconds, for setup and for the script. |
| `sandbox` | none | `{ "/absolute/path" = "fixture directory" }`: run in a sandbox with these paths replaced (see [Sandbox](#sandbox)). |
| `network` | `false` with a sandbox | `true` keeps network access inside the sandbox. |

### Case keys (`[[case]]`)

| Key | Meaning |
|---|---|
| `name` | Required. Letters, digits, `_`, `.`, `-`; also the snapshot file name. |
| `args` | Required. List of arguments. |
| `env` | Added to the file's `env`. |
| `setup` | Runs **after** the file's `setup`. |
| `scrub` | Added to the file's `scrub`. |
| `cwd`, `timeout` | Override the file's. |
| `stdin` | Text fed to the script's standard input (default: empty). |
| `safe` | `true` allows running without a dry-run flag. |
| `sandbox` | Added to the file's `sandbox` (same mount point: the case's wins). |

Unknown keys are rejected, so a typo such as `scirpt` is an error rather than silently ignored.

### Variables

Each case runs in a fresh temporary directory. These are set for setup and for the script, and `${NAME}` expands from the case's environment in `args`, `env`, `cwd` and `setup`:

| Variable | Value |
|---|---|
| `DRAC_TMP` | The case's temporary directory (deleted afterwards). |
| `DRAC_DIR` | The directory of the case file (for fixtures next to it). |
| `DRAC` | `1`, in case a script wants to know it is being checked. |

## Snapshots

Stored next to the case file: `backup.toml` → `backup/<case>.snap`. A snapshot holds the command (as written in the case file), exit code, stdout and stderr:

```
$ backup.py --dry-run --source ${DRAC_TMP}/input
--- exit 2
--- stdout
scanning <TMP>/input
archive target: <TMP>/data/archive-<ID>.tar.gz

Cannot proceed:
    destination directory is not empty
--- stderr
```

(`tests/backup/refuses_nonempty_dest.snap`)

Output is normalised before comparing, in this order:

1. The case's temporary directory becomes `<TMP>` (also through symlinks).
2. The case file's `scrub` patterns.
3. The built-ins:

| Name | Replaces | With |
|---|---|---|
| `tmp` | pid-named temp files such as `/tmp/job.12345`, `/tmp/run.3.987` | `<TMPFILE>` |
| `sha` | 40-character hex digests / commit ids | `<SHA>` |
| `date` | `date` output (`Thu Oct 29 19:08:38 GMT 2026`), ISO dates and times, `20261029 19:08:38 GMT` | `<DATE>` |
| | file-name stamps `20261029-190838-739560` | `<STAMP>` |
| | plain `YYYYMMDD` dates | `<YYYYMMDD>` |
| `host` (opt-in) | this host's name, long and short | `<HOST>` |
| `user` (opt-in) | the current user name | `<USER>` |

Short ids or hashes are not scrubbed by default (too easily confused with other numbers); add a `scrub` pattern for the format the script prints, as `backup.toml` does for 10-character hex ids.

Snapshots are plain text and belong in version control next to the case files. Review a snapshot diff the way you would review code: it *is* the change in behaviour.

## Coverage

`-cov` works with every mode and reports, per file, how many lines of code the cases ran and which did not run. It measures the script under test only (with `-ab`, never the baseline), and the compared output is not affected, so snapshots pass or fail exactly as without it.

```
...
$ ./drac.py -q -cov tests
...
16 passed, 0 failed, 0 errors, 0 recorded

Coverage (python: exact; bash: approximate)
utils.py    python  147/397  37.0%  not run: 31-35, 49, 54, ...
backup.py   python  332/554  59.9%  not run: 40, 48, ..., 534-604, ...
cleanup.sh  bash    179/294  60.9%  not run: 56, ..., 279-324, ...
...
```

| Language | How | Accuracy |
|--|--|--|
| Python | The script runs under `sys.monitoring` (python 3.12+) or `sys.settrace` (older) in its own interpreter; code lines come from the compiler. Imported modules (e.g. `utils.py`) are measured too. | Exact. Unreachable code is not counted. |
| Bash | `xtrace` with a marked `PS4`, written to a separate file descriptor (`BASH_XTRACEFD`), and `SHELLOPTS=xtrace` so child bash scripts are traced too. Which lines count as code is a heuristic, checked against bash 5.1: comments, `then`/`do`/`done`/`fi`/`esac`/`;;`, braces, function headers, case patterns and heredoc bodies are not code; a statement continued by `\` or an open quote counts once. | Approximate. Good for finding untouched code, not for exact percentages. |

Other languages are listed as "not measured". The language comes from `interpreter`, or from the script's `#!` line (`#!/usr/bin/env python3` included).

**Which files are reported:** by default, files under the directories of the scripts under test. That leaves out what the environment drags in, e.g. `~/.bashrc` through `BASH_ENV`, or `/bin/egrep` (a shell script). Use `-cov-include` to choose explicitly, e.g. `-cov-include 'my-*'` for bash scripts called through `PATH`.

**Annotated listings** (`-cov-out DIR`) are written as `DIR/<file>.cov`: every source line with `+` (ran), `-` (did not run) or blank (not code):

```
+    42    print(f"Would remove {len(files)} file(s) from {target_dir}")
+    43    return 0
     44
     45    # Delete the matched files
-    46    for path in files:
```

**Reading the numbers:** a dry run follows the `--dry-run` branch, so the real-execution path shows as not run even with perfect cases (above: `backup.py` 534-604, where files are actually written). That is the useful part: it lists exactly what the dry-run suite cannot check, which needs real-execution tests against a fixture or a `--dry-run` that follows the real path more closely. Lines not run that *are* on the dry-run path are missing cases: add an input that reaches them.

## Sandbox

Some scripts hard-code absolute paths (such as `/var/lib/myapp/...`), so they cannot be pointed at a fixture via flags or environment variables. With `sandbox`, the script runs **unmodified** in a private namespace where those paths are replaced by the fixture:

```toml
setup = 'bash "${DRAC_DIR}/fixtures/make_sample_data.sh"'  # builds ${DRAC_TMP}/myapp-data

[sandbox]
"/var/lib/myapp" = "${DRAC_TMP}/myapp-data"
```

- Each fixture directory is bind-mounted over its path, in a private user + mount namespace (`unshare`, no root needed). Nothing outside changes; writes to those paths land in the fixture.
- A mount point inside another one (`/var/lib/myapp/cache` inside `/var/lib/myapp`) must exist in the outer **fixture**, not on the real disk.
- The network is cut (only loopback) unless `network = true`, so a dry run that leaks an `ssh`, `curl` or `rsync` fails instead of reaching a host. Use this for any script that talks to the network.
- Inside, the script runs as uid 0 of its own namespace (`$USER` is unchanged); files it creates in a fixture belong to you outside.
- File descriptor 9 is kept open to the run's own stderr (`exec 9>&2`), so stand-in executables (e.g. fake `ssh`, `curl` or `rsync` placed first on `PATH`) can report leaked commands to `>&9` even when the script redirects their stderr with `2>/dev/null`.
- Coverage works inside the sandbox.
- If namespaces are not available, sandboxed cases are errors ("sandbox not available: ...").

## Safety

- A case is refused unless its `args` include one of `debug_flags`. A case that cannot change anything without one (`-h`, an argument error) must say so with `safe = true`.
- Every case runs in its own temporary directory, which is the default working directory.
- For scripts that touch shared paths or the network, use a `sandbox`: fixtures instead of the real paths, and no network.
- Live cases read real state. They are only as safe as the script's dry-run mode: check that `--dry-run` / `-n` really changes nothing before adding a live case, and keep them to `-ab`.

## Workflow

**Before changing a script that has an installed copy:** write a live case file with `baseline` set to the installed copy and the inputs you would normally try by hand. Run `drac.py -ab` before and after your change; every difference it shows is a behaviour change.

**To make a script part of the regression suite:**

1. Make it runnable against a fixture: path options, environment overrides (e.g. `DATA_DIR`, `CONFIG_FILE`), or a `[sandbox]` mount table for scripts that hard-code paths such as `/var/lib/myapp/...`.
2. Write a fixture builder in `tests/fixtures/`. If the fixture creates git commits, fix git identities and dates (`GIT_AUTHOR_DATE`, `GIT_COMMITTER_DATE`, names, emails) and set `GIT_CONFIG_GLOBAL=/dev/null`, `GIT_CONFIG_NOSYSTEM=1` so commit ids are identical on every run and your own git config cannot leak in.
3. Add `tests/<script>.toml` with `live = false`, record with `-r`, read every snapshot, then run without `-r` twice to make sure the output is stable.

**When a case fails:** read the diff. If the change is intended, re-record that case (`drac.py -r -k 'backup::full' tests/backup.toml`) and commit the new snapshot with the code change. If not, you have found a regression.

## Files

| Path | What |
|---|---|
| `drac.py` | The checker. `make_test_class()` turns case files into `unittest` tests. |
| `test_drac.py` | Tests for `drac.py` itself (throwaway scripts). |
| `test_drac_snapshots.py` | Runs the offline case files (`tests/*.toml`, or `$DRAC_CASES_DIR`) as part of the unittest suite. |
| `tests/` | Case files, snapshots, fixtures; `live/` for `-ab`-only files. |

## Limitations

- Output only: files a script would write are not compared, so the dry-run text has to say what would be written (most dry-run modes already do).
- Cases run one at a time.
- A live A/B pass says nothing about inputs you did not list; add a case for each kind of input you care about.
- Coverage counts lines, not branches: a line with an `if` that was only ever true counts as run.
- Bash coverage is approximate (see Coverage); a script that turns tracing off itself (`set +x`) stops being measured from that point.