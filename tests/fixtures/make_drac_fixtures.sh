#!/usr/bin/env bash
set -euo pipefail

# 1. Fixture scripts under test
mkdir -p bin
cat <<'EOF' > bin/backup.sh
#!/usr/bin/env bash
echo "backup mode=$1 src=${2:-default}"
if [ "${1:-}" = "--full" ]; then
    echo "note: full scan" >&2
fi
EOF
chmod +x bin/backup.sh

cat <<'EOF' > bin/backup_v1.sh
#!/usr/bin/env bash
echo "plan: sync $*"
EOF
chmod +x bin/backup_v1.sh

cat <<'EOF' > bin/backup_v2.sh
#!/usr/bin/env bash
echo "plan: sync $*"
if [ "${2:-}" = "--verify" ]; then
    echo "plan: checksum verify"
fi
EOF
chmod +x bin/backup_v2.sh

cat <<'EOF' > bin/not_exec.sh
#!/usr/bin/env bash
echo "never runs"
EOF
chmod -x bin/not_exec.sh

cat <<'EOF' > bin/cov_tool.py
#!/usr/bin/env python3
import sys

def run(argv):
    if "--dry-run" in argv:
        print(f"would process {len(argv)} arg(s)")
        return 0
    print("processing for real")
    return 1

if __name__ == "__main__":
    sys.exit(run(sys.argv[1:]))
EOF
chmod +x bin/cov_tool.py

cat <<'EOF' > bin/cov_tool.sh
#!/usr/bin/env bash
# Sample bash script for coverage
if [ "${1:-}" = "--dry-run" ]; then
    echo "dry run ok"
else
    echo "live run"
fi
EOF
chmod +x bin/cov_tool.sh

cat <<'EOF' > bin/unmeasured.pl
#!/bin/echo
EOF
chmod +x bin/unmeasured.pl

# 2. Suite for -r (record mode)
mkdir -p record_suite
cat <<'EOF' > record_suite/backup.toml
script = "../bin/backup.sh"
live = false

[[case]]
name = "basic"
args = ["--dry-run", "docs"]

[[case]]
name = "full"
args = ["--full", "--dry-run", "photos"]
EOF

# 3. Suite for passing compare & -v
mkdir -p pass_suite/backup
cp record_suite/backup.toml pass_suite/backup.toml
cat <<'EOF' > pass_suite/backup/basic.snap
$ backup.sh --dry-run docs
--- exit 0
--- stdout
backup mode=--dry-run src=docs
--- stderr
EOF
cat <<'EOF' > pass_suite/backup/full.snap
$ backup.sh --full --dry-run photos
--- exit 0
--- stdout
backup mode=--full src=--dry-run
--- stderr
note: full scan
EOF

# 4. Suite with pass, diff failure, and missing snapshot
mkdir -p diff_suite/deploy
cat <<'EOF' > diff_suite/deploy.toml
script = "../bin/backup.sh"
live = false

[[case]]
name = "unchanged"
args = ["--dry-run", "app"]

[[case]]
name = "drifted"
args = ["--dry-run", "db"]

[[case]]
name = "unrecorded"
args = ["--dry-run", "cache"]
EOF

cat <<'EOF' > diff_suite/deploy/unchanged.snap
$ backup.sh --dry-run app
--- exit 0
--- stdout
backup mode=--dry-run src=app
--- stderr
EOF

cat <<'EOF' > diff_suite/deploy/drifted.snap
$ backup.sh --dry-run db
--- exit 0
--- stdout
backup mode=--dry-run src=old_db
--- stderr
EOF

# 5. Suite for -ab mode
mkdir -p ab_suite
cat <<'EOF' > ab_suite/sync.toml
script = "../bin/backup_v2.sh"
baseline = "../bin/backup_v1.sh"

[[case]]
name = "same"
args = ["--dry-run", "data"]

[[case]]
name = "changed"
args = ["--dry-run", "--verify"]
EOF

cat <<'EOF' > ab_suite/no_baseline.toml
script = "../bin/backup_v2.sh"

[[case]]
name = "missing_base"
args = ["--dry-run"]
EOF

# 6. Case files for safety and runtime errors
mkdir -p errors/errors
cat <<'EOF' > errors/errors.toml
script = "../bin/backup.sh"
live = false

[[case]]
name = "unsafe_call"
args = ["--force", "prod"]

[[case]]
name = "safe_help"
args = ["-h"]
safe = true

[[case]]
name = "bad_setup"
args = ["--dry-run"]
setup = "echo 'fixture builder failed' >&2; exit 2"

[[case]]
name = "bad_cwd"
args = ["--dry-run"]
cwd = "${DRAC_TMP}/no_such_dir"
EOF

cat <<'EOF' > errors/errors/safe_help.snap
$ backup.sh -h
--- exit 0
--- stdout
backup mode=-h src=default
--- stderr
EOF

cat <<'EOF' > errors/missing_script.toml
script = "../bin/does_not_exist.sh"
[[case]]
name = "missing"
args = ["--dry-run"]
EOF

cat <<'EOF' > errors/not_executable.toml
script = "../bin/not_exec.sh"
[[case]]
name = "perm"
args = ["--dry-run"]
EOF

# 7. Invalid config / TOML files
cat <<'EOF' > bad_unknown.toml
script = "bin/backup.sh"
scirpt = "typo.sh"
[[case]]
name = "c"
args = ["--dry-run"]
EOF

cat <<'EOF' > bad_syntax.toml
script = "bin/backup.sh"
[[case]
name = "unclosed"
EOF

cat <<'EOF' > bad_case.toml
script = "bin/backup.sh"
[[case]]
name = "bad name with spaces"
args = ["--dry-run"]
EOF

# 8. Suite for -cov
mkdir -p cov_suite/py cov_suite/sh cov_suite/other
cat <<'EOF' > cov_suite/py.toml
script = "../bin/cov_tool.py"
live = false
[[case]]
name = "dry"
args = ["--dry-run", "item1"]
EOF
cat <<'EOF' > cov_suite/py/dry.snap
$ cov_tool.py --dry-run item1
--- exit 0
--- stdout
would process 2 arg(s)
--- stderr
EOF

cat <<'EOF' > cov_suite/sh.toml
script = "../bin/cov_tool.sh"
live = false
[[case]]
name = "dry"
args = ["--dry-run"]
EOF
cat <<'EOF' > cov_suite/sh/dry.snap
$ cov_tool.sh --dry-run
--- exit 0
--- stdout
dry run ok
--- stderr
EOF

cat <<'EOF' > cov_suite/other.toml
script = "../bin/unmeasured.pl"
live = false
[[case]]
name = "dry"
args = ["--dry-run"]
EOF
cat <<'EOF' > cov_suite/other/dry.snap
$ unmeasured.pl --dry-run
--- exit 0
--- stdout
unmeasured.pl --dry-run
--- stderr
EOF
