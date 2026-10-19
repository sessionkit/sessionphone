`recording-switch-cases.json` is a byte-for-byte copy of sessionvoice's
recording-switch contract fixtures ("EARS-387 recording switch contract, rules
1-11"), copied from sessionvoice b1f1985 (tests/fixtures/recording-switch-cases.json),
sha256 0b63e136c4584ff2585cc961c09e4ec6711a50f922fb904785faa10fda5d19ce. It holds 59
cases: the first 35 are the `record` / `record_days` switch's, and 24 more
cover rules 10-11 (`record_notice`, `record_spoken_notice`). Both sides run
every case. Never edit it here: a change to the contract lands on the
sessionvoice side first, then is copied again. `tests/test_recording_switch.py`
runs it and checks the hash.
