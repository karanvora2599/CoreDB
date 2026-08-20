"""Deep/at-scale benchmark - NOT part of run_all.py's default suite (too
slow for a normal dev-loop run). Exists to answer one specific question
the M10 "integer-interned entity/predicate ids + covering indexes"
proposal raised: does string-key size/comparison cost in spo_idx/ops_idx/
relationship_lookup actually show up as a bottleneck at realistic scale?

    python -m benchmarks.bench_at_scale

Deliberately uses LONG entity names and a SMALL, heavily-reused predicate
vocabulary - the scenario where interning should help the most, if it
helps at all (large predicate strings repeated across many keys; a B+tree
whose keys are noticeably bigger than a packed-integer equivalent would
be). If this doesn't show a measurable bottleneck, that's a real,
reportable finding - not a gap in coverage. See Documentation/ROADMAP.md's
Milestone 10 for how this result was used.
"""
from __future__ import annotations

from .harness import bench, print_report, temp_db

_PREDICATES = ["SUPPLIED_BY", "CO_OCCURS_WITH", "PARTNERS_WITH", "COMPETES_WITH", "ACQUIRED_BY"]


def _long_entity_names(n: int) -> list[str]:
    return [f"Organization_{i:06d}_LongDescriptiveName" for i in range(n)]


def bench_ingest_at_scale(n_facts: int = 50_000, n_entities: int = 5_000):
    entities = _long_entity_names(n_entities)
    with temp_db(map_size=2**31) as db:
        def run():
            with db.write_batch():
                for i in range(n_facts):
                    s = entities[i % n_entities]
                    o = entities[(i * 7 + 1) % n_entities]
                    p = _PREDICATES[i % len(_PREDICATES)]
                    db.assert_fact(s, p, o, "2020-01-01", confidence=0.5)
        return bench("at_scale.ingest (long names, batched)", n_facts, run)


def bench_hot_path_as_of_at_scale(n_facts: int = 50_000, n_entities: int = 5_000, n_lookups: int = 2000):
    entities = _long_entity_names(n_entities)
    with temp_db(map_size=2**31) as db:
        with db.write_batch():
            for i in range(n_facts):
                s = entities[i % n_entities]
                o = entities[(i * 7 + 1) % n_entities]
                p = _PREDICATES[i % len(_PREDICATES)]
                db.assert_fact(s, p, o, "2020-01-01", confidence=0.5)
        target_i = min(123, n_entities - 1)
        target = entities[target_i]
        target_predicate = _PREDICATES[target_i % len(_PREDICATES)]
        return bench("at_scale.as_of (hot subject+predicate)", n_lookups,
                      lambda: [db.as_of((target, target_predicate, None), "2020-01-01") for _ in range(n_lookups)])


AT_SCALE_BENCHMARKS = [bench_ingest_at_scale, bench_hot_path_as_of_at_scale]


def run_at_scale() -> list:
    return [fn() for fn in AT_SCALE_BENCHMARKS]


if __name__ == "__main__":
    print_report(run_at_scale())
