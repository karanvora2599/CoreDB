# Query language

CoreDB's query language is called **TGQL** (Temporal Graph Query Language). The full, versioned grammar reference — every statement, its syntax, and its result shape — lives in [`TGQL_SPEC.md`](TGQL_SPEC.md).

Quick orientation: run TGQL with `Database.execute(source: str)`. There's also a lower-level Python API (`db.as_of`, `db.history`, `db.diff`/`diff_delta`, `db.range_agg`, `db.as_known`, `db.series`, `db.assert_fact`, `db.retract_fact`, `db.sync_snapshot`) that TGQL compiles down to — use it directly if you don't need TGQL's string syntax.
