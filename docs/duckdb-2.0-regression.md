# DuckDB 2.0-alpha regression — 2026-09-10

**Verdict:** the main-branch nightly that self-identifies as **v2.0.0-alpha** runs this
solution's whole unit suite, the four-path parity probe and the DuckLake catalog ATTACH
after two small, version-neutral code changes (the `aws` extension loaded explicitly; the
fts score filtered in a subquery). Both are in main now, so 1.5.5 and the alpha run the
same code. The runtime stays pinned to **1.5.x** (`duckdb>=1.5.5,<1.6` in
`requirements.txt` / `pyproject.toml`); upgrading to 2.0 waits for GA plus a clean re-run of
this list.

## What was tested, on what

| | Runtime line | Pre-release line |
|---|---|---|
| PyPI version | `1.5.5` | `1.6.0.dev379` (`pip install --pre --upgrade duckdb`) |
| `PRAGMA version` | `v1.5.5` (Variegata) | **`v2.0.0-alpha39998`** (Cyanoptera) — the nightly is the 2.0 line under a 1.6 PyPI label |
| Where | an EC2 worker (Ubuntu ARM, `.venv` / `.venv-alpha`), same env as the runtime | same |
| Storage compatibility setting | default | `storage_compatibility_version = latest` |

PyPI has **no `2.0.0aN` wheel** as of 2026-09-10 (release list checked via the PyPI JSON
API); the DuckDB release calendar CSV (`duckdb.org/data/duckdb-releases.csv`) lists every
1.5.x release with `lts=false`, 1.4.x with `lts=true` and 1.4.0's end of life as
2026-09-16, and 1.5.0's as 2026-09-01; it carries no planned 2.0.0 row, so the "late
October GA" date stays as recorded at the time, not re-verified here.

## Results

| Check | 1.5.5 | v2.0.0-alpha39998 | Difference |
|---|---|---|---|
| Unit suite (`tests/unit`, 507 tests, no network) | 507 passed | 507 passed¹ | none |
| Four-path parity, `count(*)` of the 2025-02-21 slice (`value > 0`) via raw / `s3t` / `glue` / `dl` | 1,226,888 on all four (raw 5.0 s) | 1,226,888 on all four (raw 2.4 s, legs 0.2–0.6 s) | none; rows identical |
| `ATTACH 'ducklake:s3://…/blockchain.ducklake' (READ_ONLY)` — the catalog file is a DuckDB database written by 1.5.x | ok, 3 snapshots, ducklake ext `eb7b95df82` | **ok, 3 snapshots**, ducklake ext `eb7b95df82` | none — a 1.5-written catalog file opens read-only under 2.0-alpha |
| Engine init on the worker (view binding + 3 legs) | 15.0 s | 6.3 s (warm S3 listing cache from the run just before; not a like-for-like number) | — |
| sqlglot `duckdb` dialect output (governance rewrite, cost expansion, semantic compiler) accepted by the parser | yes | yes — every pipeline statement in the suite parses and runs | none observed |
| `DESCRIBE SELECT * FROM … LIMIT 0` shape used by the schema prober | 2-tuple rows | same | none |
| `PRAGMA create_fts_index` / `match_bm25` (data map L1) | ok | index + macro ok; **`WHERE score IS NOT NULL` on the SELECT-list alias of `match_bm25` fails**: *Binder Error: Alias "score" referenced - but the expression has side effects. This is not yet supported.* (8 data-map / reconcile tests) | **second behavioural difference** → the search query now computes the score in a subquery and filters outside it; runs on both versions |
| `credential_chain` S3 secret | works with `httpfs` alone | **needs the `aws` extension explicitly** (`autoinstall_known_extensions=false` surfaced it: *"required extension 'aws' … Install it first"*) | **first behavioural difference** → `aws` is now baked and LOADed on every version |
| `read_parquet(..., hive_partitioning=1)`, `union_by_name`, `read_csv` all_varchar | ok | ok | none |

¹ First alpha run showed one failure (`test_search_ranks_by_name_and_column_tokens_with_fts`):
the test connection did not point at the engine's extension directory, so `LOAD fts` found
nothing and the data map fell back to token overlap. An environment artefact, not a 2.0
change — fixed by giving test connections the engine's `home_directory` (they must mirror
the LOAD-only engine now that nothing INSTALLs at runtime).

## Extension pre-baking

- `scripts/install_extensions.py` is the one place extensions are fetched: `httpfs`, `aws`,
  `iceberg`, `ducklake`, `fts` into `DUCKDB_EXTENSION_DIR` (`/opt/duckdb-extensions` in the
  image, built at `docker build` time, read-only for the runtime user).
- `compute/engine.py` sets `extension_directory`, `autoinstall_known_extensions=false`, and
  only ever `LOAD`s; `storage/legs.py` and `context/datamap.py` LOAD too. A missing
  extension is a build error with the fix in the message, never a download.
- Unit tests assert that a cold start issues no `INSTALL` and LOADs the five (recorder
  connection), and that a missing extension raises with the fix.
- **Cloud (reference deployment)**: image 230.9 MB → **283.9 MB** (+53.0 MB
  for the five extensions); the engine's cold-start line in CloudWatch:
  `{"engine": "cold start", "duckdb": "1.5.5", "init_ms": 29004, "extensions_loaded":
  ["avro", "aws", "core_functions", "ducklake", "httpfs", "iceberg", "icu", "json",
  "parquet"], "install_at_runtime": false, "view_errors": []}`; no INSTALL / extension
  repository / download line in the log group. Init time is dominated by binding the raw
  views (S3 LIST of the public dataset's partitions) — the download step is gone, but the previous build did
  not log its init time, so there is no like-for-like "before" number; the worker measured
  15.0 s for the same init with extensions already local.
- Threat model T010 (cold-start extension download): **Accepted → Mitigated**.

## Upgrade gate (unchanged)

Move the runtime to 2.0 when all of: 2.0 GA published; sqlglot release notes confirm the
2.0 grammar; this table re-run clean on GA (unit suite, parity, DuckLake ATTACH written by
the loader's pinned version — the loader and the runtime must pin the SAME DuckDB, since a
2.0-written catalog file is only best-effort readable by 1.x).
