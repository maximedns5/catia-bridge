# Testing on a live CATIA

`scripts/live_suite.py` runs the whole live suite on a running CATIA in
one command and packs every report into one zip to send back.

```powershell
python scripts\live_suite.py --out live_run.zip
```

| Step | What runs | Needs |
|---|---|---|
| Offline | The clash analysis of every assembly fixture | No CATIA |
| Trees | Every fixture in `tests/fixtures/trees`, built with the tree command (`--only m10,s` keeps the names starting with those prefixes) | CATIA open |
| Probes | `probe_properties.py --create`, `probe_drawing.py` | CATIA open |
| Product | `probe_constraints.py`, `probe_clashes.py`, then the adopt build with drawings and clashes | `--product STEP` imported in CATIA, its window active (or named with `--window`) |

The zip holds one report per step, `summary.json` (per step: passed or
not, measured and expected volume, the first line of an error) and
`summary.txt` (the same, short, in French).

The run keeps going after a failed step. It stops when a CATIA dialog
blocks the session, since every later call would be refused the same way:
close the dialog (read it first), then run the remaining steps, for
example with `--only` or `--skip-trees`. `--keep-going` runs past it.

The script attaches to the running CATIA and never starts one, and saves
nothing. Each built part stays open in its own window: close the windows,
without saving, between two runs (live, a CATIA session held about 180 of
them after a day of tests).
