"""Synthetic dataset generators for benchmarks/bench_suite.py. Pure
functions of (db, size params) - no domain content, consistent with
CoreDB's own domain-agnostic engine."""
from __future__ import annotations

from datetime import date, timedelta


def seed_random_graph(db, n_entities: int, n_edges: int, valid_from: str = "2020-01-01") -> None:
    """n_edges relationships (SUBJECT_i, LINK, SUBJECT_j) scattered across
    n_entities entities, each opened once and left open - a plain, static
    graph for AS_OF/HISTORY/DIFF/PATH benchmarks that don't care about
    history depth."""
    for i in range(n_edges):
        s = f"Node{i % n_entities}"
        o = f"Node{(i * 7 + 1) % n_entities}"
        if s == o:
            o = f"Node{(i * 7 + 2) % n_entities}"
        db.assert_fact(s, "LINK", o, valid_from, confidence=0.5 + (i % 5) / 10)


def seed_churn_history(db, subject: str, predicate: str, object_id: str,
                        n_cycles: int, start_date: str = "2020-01-01", cycle_days: int = 7) -> str:
    """Repeatedly opens then closes (subject, predicate, object_id),
    simulating a long ingestion history - this is what produces H, the
    history depth TRACK's per-step cost scales with. Returns the final date
    reached (useful as the query window's end)."""
    d = date.fromisoformat(start_date)
    for _ in range(n_cycles):
        db.assert_fact(subject, predicate, object_id, d.isoformat())
        d += timedelta(days=cycle_days)
        db.retract_fact(subject, predicate, object_id, d.isoformat())
        d += timedelta(days=cycle_days)
    return d.isoformat()


def seed_entity_churn(db, entity_id: str, n_cycles: int, start_date: str = "2020-01-01",
                       cycle_days: int = 7, weighted: bool = True) -> str:
    """Like seed_churn_history, but spreads the churn across n_cycles
    *distinct* relationships all touching entity_id, so degree()/TRACK
    DEGREE has real history depth to sweep through (a single relationship
    churning doesn't change degree()'s count, only edge_weight's value)."""
    d = date.fromisoformat(start_date)
    for i in range(n_cycles):
        confidence = 0.5 + (i % 5) / 10 if weighted else None
        db.assert_fact(entity_id, "TOUCHES", f"Peer{i}", d.isoformat(), confidence=confidence)
        d += timedelta(days=cycle_days)
        db.retract_fact(entity_id, "TOUCHES", f"Peer{i}", d.isoformat())
        d += timedelta(days=cycle_days)
    return d.isoformat()
