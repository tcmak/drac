  ### How to contribute

  • Compare against golden snapshots (and measure drac.py's own coverage):
    ./drac.py tests
    ./drac.py -cov tests

  • Run everything (unit tests + golden snapshot tests) via unittest:
    python3 -m unittest discover -s . -p 'test_*.py'

  • Re-record snapshots after an intentional change:
    ./drac.py -r tests/drac.toml
