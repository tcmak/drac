#!/usr/bin/env python3
"""
Tests for drac.py (record and compare dry-run output). Uses small throwaway scripts in /tmp.
"""

import getpass
import os
import shutil
import socket
import sys
import tempfile
import textwrap
import unittest
from io import StringIO
from unittest import mock

import drac

HERE = os.path.dirname(os.path.abspath(__file__))
for p in HERE, os.path.abspath(os.path.join(HERE, "..")):
    if p not in sys.path:
        sys.path.insert(0, p)


class DracTestBase(unittest.TestCase):
    def setUp(self):
        self.dir = tempfile.mkdtemp(prefix="test_drac_")

    def tearDown(self):
        shutil.rmtree(self.dir, ignore_errors=True)

    def write(self, name, content, executable=False):
        path = os.path.join(self.dir, name)
        os.makedirs(os.path.dirname(path), exist_ok=True)
        with open(path, "w") as f:
            f.write(textwrap.dedent(content).lstrip("\n"))
        if executable:
            os.chmod(path, 0o755)
        return path

    def script(self, name, body):
        return self.write(name, "#!/bin/bash\n" + textwrap.dedent(body).lstrip("\n"), executable=True)

    def drac(self, *argv):
        out, err = StringIO(), StringIO()
        rc = drac.run_cli(list(argv), stdout=out, stderr=err)
        return rc, out.getvalue(), err.getvalue()

    def read(self, path):
        with open(path) as f:
            return f.read()


class TestRecordAndCompare(DracTestBase):
    def test_record_then_compare_pass_and_fail(self):
        self.script("tool.sh", """
            echo "hello $1"
            echo "warning: dry run" >&2
            exit ${EXIT:-0}
        """)
        cases = self.write("tool.toml", """
            script = "tool.sh"
            live = false

            [[case]]
            name = "hello"
            args = ["--dry-run"]
        """)
        snap = os.path.join(self.dir, "tool", "hello.snap")

        # A missing snapshot is a failure, not a silent pass
        rc, out, err = self.drac(cases)
        self.assertEqual(rc, 1)
        self.assertIn("FAIL: tool::hello", out)
        self.assertIn("record it with -r", out)

        rc, out, err = self.drac("-r", cases)
        self.assertEqual(rc, 0)
        self.assertIn("RECORDED: tool::hello", out)
        self.assertEqual(
            self.read(snap),
            "$ tool.sh --dry-run\n--- exit 0\n--- stdout\nhello --dry-run\n--- stderr\nwarning: dry run\n"
        )

        rc, out, err = self.drac(cases)
        self.assertEqual(rc, 0)
        self.assertIn("PASS: tool::hello", out)
        self.assertIn("1 passed, 0 failed, 0 errors, 0 recorded", out)

        # stdout, stderr and the exit code are all compared
        self.script("tool.sh", 'echo "bye $1"; echo "warning: dry run" >&2')
        rc, out, err = self.drac(cases)
        self.assertEqual(rc, 1)
        self.assertIn("--- FAIL: tool::hello ---", out)
        self.assertIn("-hello --dry-run", out)
        self.assertIn("+bye --dry-run", out)

        self.script("tool.sh", 'echo "hello $1\n"')
        rc, out, err = self.drac(cases)
        self.assertIn("-warning: dry run", out)

        self.script("tool.sh", 'echo "hello $1"; echo "warning: dry run" >&2; exit 3\n')
        rc, out, err = self.drac(cases)
        self.assertIn("--- exit 3", out)

    def test_trailing_newlines_are_kept_exactly(self):
        self.script("tool.sh", 'printf "\\n\\n\\n"; printf "no newline" >&2\n')
        cases = self.write("tool.toml", """
            script = "tool.sh"
            [[case]]
            name = "c"
            args = ["--dry-run"]
        """)
        self.assertEqual(self.drac("-r", cases)[0], 0)
        self.assertEqual(
            self.read(os.path.join(self.dir, "tool", "c.snap")),
            "$ tool.sh --dry-run\n--- exit 0\n--- stdout\n\n\n\n--- stderr\nno newline\n"
        )
        self.assertEqual(self.drac(cases)[0], 0)
        self.script("tool.sh", 'printf "\\n\\n\\n"; printf "no newLine" >&2\n')
        self.assertEqual(self.drac(cases)[0], 1)

    def test_environment_setup_cwd_and_stdin(self):
        os.environ["DRAC_TEST_INHERITED"] = "from caller"
        self.addCleanup(os.environ.pop, "DRAC_TEST_INHERITED", None)
        self.script("tool.sh", """
            echo "DRAC=$DRAC FOO=$FOO BAR=$BAR INHERITED=${DRAC_TEST_INHERITED-unset}"
            echo "dir=$DRAC_DIR"
            echo "pwd=$(pwd)"
            cat "$DRAC_TMP/order"
            echo "arg=$2"
            echo "stdin=$(cat)"
        """)
        cases = self.write("tool.toml", """
            script = "tool.sh"
            setup = "mkdir sub && echo file >> order"
            cwd = "${DRAC_TMP}/sub"
            env = { FOO = "${DRAC_TMP}/foo" }

            [[case]]
            name = "inherit"
            args = ["--dry-run", "${FOO}"]
            env = { BAR = "case" }
            setup = "echo case >> order"
            stdin = "piped in"
        """)
        self.assertEqual(self.drac("-r", cases)[0], 0)
        snap = self.read(os.path.join(self.dir, "tool", "inherit.snap"))
        self.assertIn("DRAC=1 FOO=<TMP>/foo BAR=case INHERITED=from caller", snap)
        self.assertIn(f"dir={self.dir}", snap)
        self.assertIn("pwd=<TMP>/sub", snap)
        self.assertIn("file\ncase\n", snap)
        self.assertIn("arg=<TMP>/foo", snap)
        self.assertIn("stdin=piped in", snap)
        self.assertIn("$ tool.sh --dry-run ${FOO}", snap)

        minimal = self.write("minimal.toml", """
            script = "tool.sh"
            inherit_env = false
            setup = "touch order"
            [[case]]
            name = "minimal"
            args = ["--dry-run"]
        """)
        self.assertEqual(self.drac("-r", minimal)[0], 0)
        self.assertIn("INHERITED=unset", self.read(os.path.join(self.dir, "minimal", "minimal.snap")))

    def test_interpreter_runs_scripts_without_the_executable_bit(self):
        self.write("plain.sh", "echo via bash $1\n")
        cases = self.write("plain.toml", """
            script = "plain.sh"
            interpreter = "bash"
            [[case]]
            name = "c"
            args = ["--dry-run"]
        """)
        self.assertEqual(self.drac("-r", cases)[0], 0)
        self.assertIn("via bash --dry-run", self.read(os.path.join(self.dir, "plain", "c.snap")))

        no_interpreter = self.write("noint.toml", """
            script = "plain.sh"
            [[case]]
            name = "c"
            args = ["--dry-run"]
        """)
        rc, out, err = self.drac(no_interpreter)
        self.assertEqual(rc, 1)
        self.assertIn("ERROR: noint::c", out)
        self.assertIn("cannot run", out)


class TestScrubbing(DracTestBase):
    def case(self, **kw):
        return drac.Case(file=os.path.join(self.dir, "x.toml"), name="c", args=[], script="", **kw)

    def test_builtin_scrubs(self):
        tmp = os.path.join(self.dir, "tmp")
        text = "\n".join([
            f"{tmp}/data.json",
            "/tmp/job.12345 and /tmp/run.3.987",
            "commit 0123456789abcdef0123456789abcdef01234567",
            "short 0123456789 stays",
            "Thu Oct 29 19:08:38 GMT 2026 and Mon Jan  5 01:02:03 2026",
            "2026-10-29 and 2026-10-29T19:08:38+00:00 and 2026-10-29 19:08:38.52",
            "20261029 19:08:38 GMT",
            "Lock (Fri 30 Oct 2026 00:44 GMT) and Mon 5 Jan 2026 01:02:03",
            "20261029-190838-739560.item938.json and 20261029_190838",
            "20261029 is a day, 20261399 is not, item938 stays",
        ])
        out = drac.normalise(text, self.case(), tmp)
        self.assertEqual(out.splitlines(), [
            "<TMP>/data.json",
            "<TMPFILE> and <TMPFILE>",
            "commit <SHA>",
            "short 0123456789 stays",
            "<DATE> and <DATE>",
            "<DATE> and <DATE> and <DATE>",
            "<DATE>",
            "Lock (<DATE>) and <DATE>",
            "<STAMP>.item938.json and <STAMP>",
            "<YYYYMMDD> is a day, 20261399 is not, item938 stays",
        ])

    def test_optional_and_custom_scrubs(self):
        host = socket.gethostname()
        user = getpass.getuser()
        text = f"{host} ({host.split('.')[0]}) by {user} on 2026-10-29 ref abc1234567"
        case = self.case(builtin_scrub=["host", "user"], scrub=[[r"\b[0-9a-f]{10}\b", "<COMMIT>"]])
        out = drac.normalise(text, case, "/nonexistent")
        self.assertEqual(out, "<HOST> (<HOST>) by <USER> on 2026-10-29 ref <COMMIT>")

        self.assertEqual(drac.normalise(text, self.case(), "/nonexistent"),
                         f"{host} ({host.split('.')[0]}) by {user} on <DATE> ref abc1234567")

    def test_tmp_path_is_scrubbed_through_symlinks(self):
        real = os.path.join(self.dir, "real")
        os.makedirs(real)
        link = os.path.join(self.dir, "link")
        os.symlink(real, link)
        out = drac.normalise(f"{link}/a {real}/b", self.case(), link)
        self.assertEqual(out, "<TMP>/a <TMP>/b")


class TestSafetyAndErrors(DracTestBase):
    def test_debug_flag_is_required_unless_safe(self):
        self.script("tool.sh", 'echo "ran $*"')
        cases = self.write("tool.toml", """
            script = "tool.sh"

            [[case]]
            name = "unsafe"
            args = ["--push"]

            [[case]]
            name = "declared_safe"
            args = ["-h"]
            safe = true
        """)
        rc, out, err = self.drac("-r", cases)
        self.assertEqual(rc, 1)
        self.assertIn("ERROR: tool::unsafe", out)
        self.assertIn("refusing to run without -n or --dry-run", out)
        self.assertIn("RECORDED: tool::declared_safe", out)
        self.assertFalse(os.path.exists(os.path.join(self.dir, "tool", "unsafe.snap")))

        custom = self.write("custom.toml", """
            script = "tool.sh"
            debug_flags = ["--preview"]
            [[case]]
            name = "dry"
            args = ["--preview", "x"]
        """)
        self.assertEqual(self.drac("-r", custom)[0], 0)

    def test_setup_failures_timeouts_and_missing_scripts(self):
        self.script("slow.sh", "sleep 5\n")
        cases = self.write("err.toml", """
            script = "slow.sh"
            [[case]]
            name = "setup_fails"
            args = ["--dry-run"]
            setup = "echo broken >&2; exit 3"
            [[case]]
            name = "setup_times_out"
            args = ["--dry-run"]
            setup = "sleep 5"
            timeout = 0.5
            [[case]]
            name = "times_out"
            args = ["--dry-run"]
            timeout = 0.5
        """)
        rc, out, err = self.drac(cases)
        self.assertEqual(rc, 1)
        self.assertIn("setup failed (exit 3): broken", out)
        self.assertIn("setup timed out after 0.5s", out)
        self.assertIn("ERROR: err::times_out", out)
        self.assertIn("timed out after 0.5s", out)

        bad_cwd = self.write("badcwd.toml", """
            script = "slow.sh"
            cwd = "${DRAC_TMP}/absent"
            [[case]]
            name = "c"
            args = ["--dry-run"]
        """)
        rc, out, err = self.drac(bad_cwd)
        self.assertIn("cwd is not a directory:", out)
        self.assertIn("/absent", out)

        missing = self.write("missing.toml", """
            script = "nope.sh"
            [[case]]
            name = "c"
            args = ["--dry-run"]
        """)
        rc, out, err = self.drac(missing)
        self.assertIn("script not found:", out)


class TestABMode(DracTestBase):
    def test_baseline_against_candidate(self):
        self.script("old.sh", 'echo "plan $1 \n"')
        self.script("new.sh", 'echo "plan $1 \n"')
        cases = self.write("ab.toml", """
            script = "new.sh"
            baseline = "old.sh"
            [[case]]
            name = "same"
            args = ["--dry-run"]
        """)
        rc, out, err = self.drac("-ab", cases)
        self.assertEqual(rc, 0)
        self.assertIn("PASS: ab::same", out)
        self.assertFalse(os.path.exists(os.path.join(self.dir, "ab")))  # nothing stored

        self.script("new.sh", 'echo "plan $1"; echo "extra step" \n')
        rc, out, err = self.drac("-ab", cases)
        self.assertEqual(rc, 1)
        self.assertIn("baseline", out)
        self.assertIn("candidate", out)
        self.assertIn("+extra step", out)

        no_baseline = self.write("nobase.toml", """
            script = "new.sh"
            [[case]]
            name = "c"
            args = ["--dry-run"]
        """)
        rc, out, err = self.drac("-ab", no_baseline)
        self.assertEqual(rc, 1)
        self.assertIn("no baseline configured for -ab", out)

        rc, out, err = self.drac("-ab", "-r", cases)
        self.assertEqual(rc, 2)


class TestCommandLine(DracTestBase):
    def setUp(self):
        super().setUp()
        self.script("tool.sh", 'echo "1"')
        self.write("suite/alpha.toml", """
            script = "../tool.sh"
            [[case]]
            name = "one"
            args = ["--dry-run", "1"]
            [[case]]
            name = "two"
            args = ["--dry-run", "2"]
        """)
        self.write("suite/beta.toml", """
            script = "../tool.sh"
            [[case]]
            name = "one"
            args = ["--dry-run", "b1"]
        """)
        self.write("suite/notes.txt", "not a case file\n")
        self.suite = os.path.join(self.dir, "suite")

    def test_directory_filter_quiet_and_verbose(self):
        rc, out, err = self.drac("-r", self.suite)
        self.assertEqual(rc, 0)
        self.assertEqual(out.splitlines()[-1], "0 passed, 0 failed, 0 errors, 3 recorded")

        rc, out, err = self.drac("-q", self.suite)
        self.assertEqual(rc, 0)
        self.assertEqual(out.splitlines(), ["...", "3 passed, 0 failed, 0 errors, 0 recorded"])

        rc, out, err = self.drac("-k", "one", self.suite)
        self.assertIn("PASS: alpha::one", out)
        self.assertIn("PASS: beta::one", out)
        self.assertNotIn("alpha::two", out)

        rc, out, err = self.drac("-k", "alpha::*", self.suite)
        self.assertIn("2 passed", out)

        rc, out, err = self.drac("-v", "-k", "beta", self.suite)
        self.assertIn("tool.sh --dry-run b1", out)

        self.write("suite/alpha/two.snap", "something else\n")
        rc, out, err = self.drac(self.suite)
        self.assertEqual(rc, 1)
        self.assertEqual(out.splitlines()[-1], "2 passed, 1 failed, 0 errors, 0 recorded")
        self.assertIn("FAIL: alpha::two", out)

    def test_usage(self):
        rc, out, err = self.drac()
        self.assertEqual(rc, 2)
        self.assertIn("the following arguments are required: files", err)

        rc, out, err = self.drac("-h")
        self.assertEqual(rc, 0)
        self.assertIn("-cov-include GLOB", out)

        rc, out, err = self.drac("-r", "-ab", self.suite)
        self.assertEqual(rc, 2)
        self.assertIn("not allowed with argument", err)


class TestCaseFileErrors(DracTestBase):
    def assertConfigError(self, content, message):
        path = self.write("bad.toml", content)
        rc, out, err = self.drac(path)
        self.assertEqual(rc, 2, content)
        self.assertIn(message, err, content)

    def test_errors(self):
        self.assertConfigError('[[case]]\nname = "c"\nargs = ["--dry-run"]\n', "'script' is required")
        self.assertConfigError("script = false\n", "has the wrong type")
        self.assertConfigError('script = "t"\n', "no [[case]] entries")
        self.assertConfigError('script = "t"\nscirpt = "c"\n', "unknown key(s) scirpt")
        self.assertConfigError('script = "t"\nlive = "no"\n', "'live' has the wrong type")
        self.assertConfigError('script = "t"\nbuiltin_scrub = ["dates"]\n', "unknown builtin_scrub")
        self.assertConfigError('script = "t"\ncase = ["x"]\n', "must be a table")
        self.assertConfigError('script = "t"\n[[case]]\nargs = []\n', "'name' is required")
        self.assertConfigError('script = "t"\n[[case]]\nname = "c"\n', "'args' is required")
        self.assertConfigError('script = "t"\n[[case]]\nname = "c"\nargs = "--dry-run"\n', "'args' has the wrong type")
        self.assertConfigError('script = "t"\n[[case]]\nname = "c"\nargs = [1]\n', "'args' must be a list of strings")
        self.assertConfigError('script = "t"\n[[case]]\nname = "a b"\nargs = []\n', "may only use letters")
        self.assertConfigError('script = "t"\n[[case]]\nname = "c"\nargs = []\nsafe = "no"\n', "'safe' has the wrong type")
        self.assertConfigError('script = "t"\n[[case]]\nname = "c"\nargs = []\nunknown = 1\n', "unknown key(s)")
        self.assertConfigError('script = "t"\n[[case]]\nname = "c"\nargs = []\n[[case]]\nname = "c"\nargs = []\n', "duplicate case name 'c'")
        self.assertConfigError('script = "t"\n[[case]]\nname = "c"\nargs = []\nscrub = [["x"]]\n', "scrub entries must be")
        self.assertConfigError('script = "t"\n[[case]]\nname = "c"\nargs = []\nscrub = [["[", "x"]]\n', "bad scrub regex")

        rc, out, err = self.drac(os.path.join(self.dir, "absent.toml"))
        self.assertEqual(rc, 2)
        self.assertIn("absent.toml: No such file or directory", err)


class TestUnittestIntegration(DracTestBase):
    def test_make_test_class(self):
        self.script("tool.sh", 'echo "ok $*"')
        offline = self.write("cases/offline.toml", """
            script = "../tool.sh"
            live = false
            [[case]]
            name = "recorded"
            args = ["--dry-run"]
            [[case]]
            name = "unrecorded"
            args = ["--dry-run"]
        """)
        self.write("cases/live.toml", """
            script = "../tool.sh"
            [[case]]
            name = "live_case"
            args = ["--dry-run"]
        """)
        self.assertEqual(self.drac("-r", "-k", "recorded", offline)[0], 0)
        os.remove(os.path.join(self.dir, "cases", "offline", "unrecorded.snap"))

        cls = drac.make_test_class([os.path.join(self.dir, "cases")])
        tests = sorted(n for n in dir(cls) if n.startswith("test_"))
        self.assertEqual(tests, ["test_offline_recorded", "test_offline_unrecorded"])

        def run():
            result = unittest.TestResult()
            unittest.defaultTestLoader.loadTestsFromTestCase(cls).run(result)
            return result

        result = run()
        self.assertEqual((result.testsRun, len(result.failures), len(result.skipped)), (2, 0, 1))
        self.assertIn("drac.py -r -k 'offline::unrecorded'", result.skipped[0][1])

        self.script("tool.sh", 'echo "changed $*"\n')
        result = run()
        self.assertEqual(len(result.failures), 1)
        self.assertIn("changed --dry-run", result.failures[0][1])


class TestCoverage(DracTestBase):
    """-cov: python measured exactly, bash approximately; the compared output never changes."""

    PY_TOOL = """
        #!{python}

        import sys
        import helper
        
        def main(argv):
            if "--dry-run" in argv:
                print("plan", helper.double(2))
            else:
                print("real")
            return 0
            
        if __name__ == "__main__":
            sys.exit(main(sys.argv[1:]))
    """
    PY_HELPER = """
        def double(n):
            return n * 2
            
        def unused():
            return 0
    """
    BASH_TOOL = """
        #!/bin/bash
        # comment
        plan () {
            echo "plan $1"
        }
        case "$1" in
            --dry-run)
                plan dry
                ;;
            *)
                echo real
                ;;
        esac
        if [ "$1" ]; then
            "$(dirname "$0")/child.sh" "$1"
        else
            echo none
        fi
        cat <<EOF
        heredoc
        EOF
        echo a \\
             b
    """
    BASH_CHILD = """
        #!/bin/bash
        if [ "$1" = "--dry-run" ]; then
            echo "child dry"
            exit 0
        fi
        echo "child real"
    """

    def python_suite(self, python=sys.executable, interpreter=True):
        self.write("scripts/tool.py", self.PY_TOOL.format(python=python), executable=True)
        self.write("scripts/helper.py", self.PY_HELPER)
        interp = f'interpreter = "{python}"' if interpreter else ""
        return self.write("py.toml", f"""
            script = "scripts/tool.py"
            {interp}
            [[case]]
            name = "dry"
            args = ["--dry-run"]
        """)

    def bash_suite(self):
        self.write("scripts/tool.sh", self.BASH_TOOL, executable=True)
        self.write("scripts/child.sh", self.BASH_CHILD, executable=True)
        # Like BASH_ENV=~/.bashrc in real runs: traced, but not a script under test
        self.write("outside/env.sh", 'noise=1\n')
        return self.write("sh.toml", """
            script = "scripts/tool.sh"
            env = { BASH_ENV = "${DRAC_DIR}/outside/env.sh" }
            [[case]]
            name = "dry"
            args = ["--dry-run"]
        """)

    def row(self, out, name):
        line = next(line for line in out.splitlines() if line.strip().startswith(os.path.join(self.dir, "scripts", name)))
        return line.split(None, 4)

    def test_python_coverage_is_exact_and_output_unchanged(self):
        cases = self.python_suite()
        self.assertEqual(self.drac("-r", cases)[0], 0)  # recorded without coverage

        rc, out, err = self.drac("-cov", cases)
        self.assertEqual(rc, 0, out)  # same output with coverage on
        self.assertIn("Coverage (python: exact; bash: approximate)", out)
        self.assertEqual(self.row(out, "tool.py")[1:4], ["python", "8/9", "88.9%"])
        self.assertEqual(self.row(out, "helper.py")[1:4], ["python", "3/4", "75.0%"])

    def test_python_interpreter_from_shebang(self):
        cases = self.python_suite(interpreter=False)
        rc, out, err = self.drac("-r", "-cov", cases)
        self.assertEqual(rc, 0, out)
        self.assertIn("8/9", self.row(out, "tool.py"))

    def test_python_before_3_12_uses_settrace(self):
        wrapper = self.write("python_noman", f"""
            #!{sys.executable}
            import runpy, sys
            sys.monitoring = None
            sys.argv = sys.argv[1:]
            runpy.run_path(sys.argv[0], run_name="__main__")
        """, executable=True)
        cases = self.python_suite(python=wrapper)
        rc, out, err = self.drac("-r", "-cov", cases)
        self.assertEqual(rc, 0, out)
        self.assertIn("8/9", self.row(out, "tool.py"))
        self.assertIn("not run: 10", self.row(out, "tool.py"))

    def test_bash_coverage_follows_child_scripts_and_skips_bash_env(self):
        cases = self.bash_suite()
        self.assertEqual(self.drac("-r", cases)[0], 0)

        rc, out, err = self.drac("-cov", cases)
        self.assertEqual(rc, 0, out)
        self.assertEqual(self.row(out, "tool.sh")[1:4], ["bash", "7/9", "77.8%"])
        self.assertEqual(self.row(out, "child.sh")[1:4], ["bash", "3/4", "75.0%"])
        self.assertNotIn("env.sh", out)

    def test_include_annotate_unmeasured_and_ab(self):
        cases = self.bash_suite()
        rc, out, err = self.drac("-r", "-cov-include", "child.sh", cases)
        self.assertIn("child.sh", out)
        self.assertNotIn("tool.sh", out)

        listings = os.path.join(self.dir, "listings")
        rc, out, err = self.drac("-cov-out", listings, cases)
        self.assertIn(f"Annotated listings (2) in {listings}", out)
        tool = self.read(os.path.join(listings, "tool.sh.cov")).splitlines()
        self.assertEqual(tool[0], f"#{os.path.join(self.dir, 'scripts', 'tool.sh')}(bash) '+' run, '-' not run")
        self.assertEqual(tool[1:5], ["      1    #!/bin/bash", "      2    # comment", "      3    plan () {", "+     4        echo \"plan $1\""])
        self.assertEqual(tool[11], "-    11            echo real")
        self.assertEqual(tool[22:24], ["+    22    echo a \\", "+    23         b"])

        self.write("scripts/notes.txt", "#!/bin/cat\n", executable=True)
        other = self.write("other.toml", """
            script = "scripts/notes.txt"
            [[case]]
            name = "c"
            args = ["--dry-run"]
        """)
        rc, out, err = self.drac("-r", "-cov", other)
        self.assertIn("nothing measured", out)
        self.assertIn("not measured (only python and bash are supported):", out)

        # -ab measures the script under test, not the baseline
        self.write("baseline/tool.sh", self.BASH_TOOL, executable=True)
        self.write("baseline/child.sh", self.BASH_CHILD, executable=True)
        ab = self.write("ab.toml", """
            script = "scripts/tool.sh"
            baseline = "baseline/tool.sh"
            [[case]]
            name = "dry"
            args = ["--dry-run"]
        """)
        rc, out, err = self.drac("-ab", "-cov", ab)
        self.assertEqual(rc, 0, out)
        self.assertIn("7/9", self.row(out, "tool.sh"))
        self.assertNotIn(os.path.join(self.dir, "baseline"), out)

    def test_python_units_match_what_python_reports(self):
        path = self.write("sample.py", """
            '''Doc'''
            import os
            
            CONST = {
                'a': 1,
            }
            # comment
            
            def f(x):
                '''Doc'''
                try:
                    x += 1
                except ValueError:
                    return 2
                else:
                    pass
        """)
        self.assertEqual(sorted(drac.python_units(path)), [1, 2, 4, 5, 9, 11, 12, 13, 14, 16])
        self.assertEqual(drac.python_units(self.write("broken.py", "def [ \n")), {})
        self.assertEqual(drac.python_units(os.path.join(self.dir, "absent.py")), {})

    def test_bash_units_match_what_xtrace_reports(self):
        # Constructs checked against bash 5.1 xtrace output; see bash_is_code
        probe1 = self.write("probe1.sh", """
            #!/bin/bash
            # comment
            x=1
            f() {
                echo "in f $1"
            }
            case $x in
                1)
                    echo one
                    ;;
                *)
                    echo other
                    ;;
            esac
            for i in a b
            do
                echo $i
            done
            while test $x -lt 3
            do
                x=$(expr $x + 1)
            done
            if [ $x -eq 3 ]; then
                echo three
            else
                echo never
            fi
            cat <<EOF
            heredoc $x
            EOF
            echo a \\
                 b \\
                 c
            y="multi
               line"
            [ -n "$y" ] && f arg
            { echo grouped; }
            ( echo sub; )
            ! false
            exit 0
        """)
        units = drac.bash_units(probe1)
        self.assertEqual(sorted(units), [3, 5, 7, 9, 12, 15, 17, 19, 21, 23, 24, 26, 28, 31, 34, 36, 37, 38, 39, 40])
        self.assertEqual(units[31], [31, 32, 33])
        self.assertEqual(units[34], [34, 35])

        probe2 = self.write("probe2.sh", """
            #!/bin/bash
            first_part ()
            {
                local n=1
                echo "$(f $1)"
                return 0
            }
            function shutdown
            {
                echo bye
            }
            if [ $x -eq 1 ]; then
                echo one
            elif [ $x -eq 2 ]; then
                echo two
            fi
            case $x in
                1) echo uno ;;
                2) echo dos
                   ;;
            esac
            while read line; do
                echo "r $line"
            done <<EOF
            text $x
            EOF
            echo "it's # not a comment # but this is"
            # comment
            first_part foo bar
        """)
        self.assertEqual(sorted(drac.bash_units(probe2)), [4, 5, 6, 10, 12, 13, 14, 15, 17, 18, 19, 22, 23, 27, 29])
        self.assertEqual(drac.bash_units(os.path.join(self.dir, "absent.sh")), {})

    def test_language_detection(self):
        case = drac.Case(file=os.path.join(self.dir, "x.toml"), name="c", args=[], script="")
        env_py = self.write("env.py", "#!/usr/bin/env python3\n")
        self.assertEqual(drac._language(case, env_py), ("python", ["python3"]))
        self.assertEqual(drac._language(case, self.write("sh.sh", "#!/bin/sh\n")), ("bash", ["bash"]))
        self.assertEqual(drac._language(case, self.write("none", "echo hi\n")), (None, []))
        self.assertEqual(drac._language(case, os.path.join(self.dir, "absent")), (None, []))
        case.interpreter = "/opt/perl/bin/perl"
        self.assertEqual(drac._language(case, env_py), (None, []))

    def test_same_file_names_get_separate_listings(self):
        for d in "a", "b":
            self.write(f"{d}/tool.sh", "#!/bin/bash\necho $*\n", executable=True)
            self.write(f"{d}.toml", f'script = "{d}/tool.sh"\n[[case]]\nname = "c"\nargs = ["--dry-run"]\n')
        listings = os.path.join(self.dir, "listings")
        rc, out, err = self.drac("-r", "-cov", "-cov-out", listings, os.path.join(self.dir, "a.toml"), os.path.join(self.dir, "b.toml"))
        self.assertEqual(sorted(os.listdir(listings)), ["tool.sh.2.cov", "tool.sh.cov"])

    def test_open_quote_and_ranges(self):
        self.assertFalse(drac._bash_open_quote('echo "a \\" b"'))
        self.assertTrue(drac._bash_open_quote('echo # it\'s a comment \n "open'))
        self.assertFalse(drac._bash_open_quote("echo 'b\\'"))
        self.assertTrue(drac._bash_open_quote("multi ''\\\n' line"))
        self.assertTrue(drac._bash_open_quote('echo "'))
        self.assertFalse(drac._bash_open_quote('echo hi # it\'s a comment'))
        self.assertFalse(drac._bash_open_quote('echo \\"'))
        self.assertEqual(drac._ranges([1], [1, 2]), "1")
        self.assertEqual(drac._ranges([26, 27, 28, 39], [24, 25, 26, 27, 28, 30, 38, 39]), "26-28, 39")
        self.assertEqual(drac._ranges([3, 5], [3, 4, 5]), "3, 5")


@unittest.skipIf(drac._sandbox_available(), f"sandbox not available here: {drac._sandbox_available()}")
class TestSandbox(DracTestBase):
    """sandbox: fixtures bind-mounted over hard-coded paths, no network. Only paths under self.dir are used."""

    PROBE = """
        #!/bin/bash
        echo "target:" $(ls $TARGET)
        echo "inner: $(cat $TARGET/inner/marker)" 2>/dev/null
        touch $TARGET/written && echo "write: ok"
        echo "interfaces: $(ip -o link | awk -F ': ' '{print $2}' | tr '\\n' ' ')"
    """

    def setUp(self):
        super().setUp()
        # The "real" directory the script hard-codes; the sandbox must hide and protect it
        self.target = os.path.join(self.dir, "real")
        self.write("real/real.txt", "real content\n")
        os.makedirs(os.path.join(self.target, "inner"))
        self.script("probe.sh", self.PROBE)

    def case_file(self, sandbox, extra=""):
        return self.write("probe.toml", f"""
            script = "probe.sh"
            env = {{ TARGET = "{self.target}" }}
            setup = "mkdir -p fix/inner inner_fix && echo fixture > fix/fixture.txt && echo nested > inner_fix/marker"
            {extra}
            [sandbox]
            {sandbox}
            [[case]]
            name = "c"
            args = ["--dry-run"]
        """)

    def test_fixture_replaces_paths_writes_stay_in_it_and_no_network(self):
        cases = self.case_file(f'"{self.target}" = "${{DRAC_TMP}}/fix"\n'
                               f'"{self.target}/inner" = "${{DRAC_TMP}}/inner_fix"')
        rc, out, err = self.drac("-r", "-cov", cases)
        self.assertEqual(rc, 0, out)
        snap = self.read(os.path.join(self.dir, "probe", "c.snap"))
        self.assertIn("target: fixture.txt inner", snap)  # the fixture, not real.txt
        self.assertIn("inner: nested", snap)  # nested mount point inside the fixture
        self.assertIn("write: ok", snap)
        self.assertIn("interfaces: lo \n", snap)  # loopback only
        # The real directory is untouched
        self.assertEqual(sorted(os.listdir(self.target)), ["inner", "real.txt"])
        # Coverage still works through the sandbox
        self.assertIn("4/4", next(line for line in out.splitlines() if "probe.sh" in line and "bash" in line))
        # And the output is stable
        self.assertEqual(self.drac(cases)[0], 0)

    def test_script_under_a_sandboxed_path_runs_from_a_copy(self):
        # Like an installed /usr/local/bin/tool as -ab baseline while /usr/local is a fixture
        tool = '#!/bin/bash\necho "I am $0"\necho sees $(ls $TARGET)\n'
        self.write("real/tool.sh", tool, executable=True)
        self.write("work/tool.sh", tool, executable=True)
        cases = self.write("copy.toml", f"""
            script = "real/tool.sh"
            baseline = "work/tool.sh"
            env = {{ TARGET = "{self.target}" }}
            setup = "mkdir fix && echo fix > fix/fixture.txt"
            [sandbox]
            "{self.target}" = "${{DRAC_TMP}}/fix"
            [[case]]
            name = "c"
            args = ["--dry-run"]
        """)
        rc, out, err = self.drac("-r", "-cov", cases)
        self.assertEqual(rc, 0, out)
        snap = self.read(os.path.join(self.dir, "copy", "c.snap"))
        self.assertIn("I am tool.sh", snap)
        self.assertIn("sees fixture.txt", snap)  # its path is replaced by its name
        self.assertIn("2/2", next(line for line in out.splitlines() if "real/tool.sh" in line))
        self.assertNotIn("drac_script_", out)
        # The same script in two places compares equal
        rc, out, err = self.drac("-ab", cases)
        self.assertEqual(rc, 0, out)

    def test_network_true_keeps_the_network_namespace(self):
        case = drac.Case(file=os.path.join(self.dir, "tool"), name="", args=[], script="",
                         sandbox={self.target: self.dir}, network=True)
        cmd = drac._sandboxed(case, ["tool"], {})
        self.assertNotIn("--net", cmd)
        self.assertEqual(cmd[-3:], [self.target, "--", "tool"])
        case.network = False
        self.assertIn("--net", drac._sandboxed(case, ["tool"], {}))

    def test_network_defaults(self):
        base = "script = 'probe.sh'\n"
        case = "[[case]]\nname = 'c'\nargs = ['--dry-run']\n"
        self.assertTrue(drac.load_cases(self.write("a.toml", base + case))[0].network)
        self.assertIsNone(drac.load_cases(self.write("a.toml", base + case))[0].sandbox)
        sandboxed = drac.load_cases(self.write("b.toml", base + f'[sandbox]\n"{self.target}" = "."\n' + case))[0]
        self.assertFalse(sandboxed.network)
        per_case = drac.load_cases(self.write("c.toml", base + case + f'sandbox = {{ "{self.target}" = "." }}\n'))[0]
        self.assertFalse(per_case.network)
        allowed = drac.load_cases(self.write("d.toml", base + f'network = true\n[sandbox]\n"{self.target}" = "."\n' + case))[0]
        self.assertTrue(allowed.network)

    def test_errors(self):
        missing_source = self.case_file(f'"{self.target}" = "${{DRAC_TMP}}/absent"')
        rc, out, err = self.drac("-r", missing_source)
        self.assertIn("sandbox source is not a directory:", out)

        missing_point = self.case_file(f'"{self.dir}/nowhere" = "${{DRAC_TMP}}/fix"')
        rc, out, err = self.drac("-r", missing_point)
        self.assertIn("sandbox mount point does not exist: ", out)

        # A nested mount point must exist inside the outer fixture, not on the real disk
        nested = self.case_file(f'"{self.target}" = "${{DRAC_TMP}}/fix"\n'
                                f'"{self.target}/only_real" = "${{DRAC_TMP}}/inner_fix"')
        os.makedirs(os.path.join(self.target, "only_real"))
        rc, out, err = self.drac("-r", nested)
        self.assertIn("sandbox mount point does not exist: ", out)

        rc, out, err = self.drac(self.write("rel.toml", 'script = "probe.sh"\nsandbox = { relative = "." }\n' + "[[case]]\nname=\"c\"\nargs=['--dry-run']\n"))
        self.assertEqual(rc, 2)
        self.assertIn("sandbox entries must be", err)

        rc, out, err = self.drac(self.write("num.toml", f'script = "probe.sh"\nsandbox = {{ "{self.target}" = 1 }}\n' + "[[case]]\nname=\"c\"\nargs=['--dry-run']\n"))
        self.assertEqual(rc, 2)

    def test_mount_failure_inside_the_sandbox_is_an_error(self):
        cases = self.case_file(f'"{self.target}" = "${{DRAC_TMP}}/fix"')
        failing = ["bash", "-c", "echo 'drac-sandbox: cannot mount a on b' >&2; exit 125"]
        with mock.patch.object(drac, "_sandboxed", return_value=failing):
            rc, out, err = self.drac("-r", cases)
        self.assertIn("ERROR: probe::c", out)
        self.assertIn("drac-sandbox: cannot mount a on b", out)

    def test_unavailable_sandbox_reported(self):
        saved = drac._sandbox_problem
        self.addCleanup(setattr, drac, "_sandbox_problem", saved)
        drac._sandbox_problem = None
        with mock.patch.object(drac.shutil, "which", return_value=None):
            self.assertEqual(drac._sandbox_available(), "unshare (util-linux) is not installed")
        cases = self.case_file(f'"{self.target}" = "${{DRAC_TMP}}/"')
        rc, out, err = self.drac("-r", cases)
        self.assertIn("sandbox not available: unshare (util-linux) is not installed", out)

        drac._sandbox_problem = None
        failed = mock.Mock(returncode=1, stderr="unshare: Operation not permitted\n")
        with mock.patch.object(drac.subprocess, "run", return_value=failed):
            self.assertEqual(drac._sandbox_available(), "unshare: Operation not permitted")
        # unittest skips recorded sandboxed cases rather than failing them (probe.toml is live, so left out)
        self.write("offline.toml", f"""
            script = "probe.sh"
            live = false
            [sandbox]
            "{self.target}" = "${{DRAC_TMP}}"
            [[case]]
            name = "c"
            args = ["--dry-run"]
        """)
        self.write("offline/c.snap", "recorded elsewhere\n")
        result = unittest.TestResult()
        unittest.defaultTestLoader.loadTestsFromTestCase(drac.make_test_class([self.dir])).run(result)
        self.assertEqual((result.testsRun, len(result.skipped), len(result.failures)), (1, 1, 0))
        self.assertIn("sandbox not available: unshare: Operation not permitted", result.skipped[0][1])


if __name__ == "__main__":
    unittest.main()
