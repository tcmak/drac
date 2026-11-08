#!/usr/bin/env python3
"""
Runs the offline case files (tests/*.toml, or $DRAC_CASES_DIR) as part of the unittest suite.
"""

import os
import unittest

import drac

HERE = os.path.dirname(os.path.abspath(__file__))
CASES_DIR = os.environ.get("DRAC_CASES_DIR", os.path.join(HERE, "tests"))

if os.path.exists(CASES_DIR):
    TestDracSnapshots = drac.make_test_class([CASES_DIR])

if __name__ == "__main__":
    unittest.main()
