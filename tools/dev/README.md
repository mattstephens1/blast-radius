# Dev tools (not used by the entry)

- `gen.py <sqlglot_src> <out_dir> <n> <seed>`: synthetic one-token mutation patches like the sample's.
  Used: `python3 tools/dev/gen.py sqlglot_src $S/synth/commits 400 7` (+ copy sample/tests.json to $S/synth/).
- `costs.py <dir>`: per-category simulated cost of `<dir>/synth_out.jsonl` (stderr notes in `<dir>/synth_err.txt`).
- `labeler/`: ground truth. `docker build -t br-labeler tools/dev/labeler` (needs requirements.lock copied in),
  then `docker run --rm -v <patch dir>:/p:ro br-labeler /p/a.patch /p/b.patch ...` prints `<id> <failing test id>` and `<id> DONE`.
  Parallel run (-n 3) then serial confirm, because xdist workers crash under memory pressure and report false failures.
  Only one labeler container at a time (Docker VM has 3.8 GB).
- `misses.py <labels.txt> <out.jsonl>`: real failures vs selection.
- `tolabel.txt`: synthetic commits narrowed by the v10 rules, in labeling order.

Local runs of the selector need: IMPORT_BASELINE, TPC_BASELINE, TPC_FIXTURES env vars
(build with `app/import_state.py --baseline` and `app/tpc_check.py --baseline`, PYTHONHASHSEED=0;
TPC fixtures can be copied out of the br-labeler image from /sqlglot/tests/fixtures/optimizer/tpc-{ds,h}).
