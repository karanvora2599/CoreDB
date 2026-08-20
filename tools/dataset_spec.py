"""The Meridian dataset: a deterministic synthetic ingestion log for CoreDB.

This module is *pure* - it imports nothing from `coredb` and touches no
storage. It emits a chronological list of ingestion events (the same three
operations `coredb` exposes as writes: `assert`, `retract`, `sync`) plus a
`ground_truth` block describing what was planted, known **by construction**
rather than by asking the engine. `tools/gen_test_dataset.py` replays the
events into LMDB; `tests/test_large_dataset.py` checks the engine against
the ground truth. Keeping generation independent of the engine is what makes
those tests non-tautological.

Domain: "Meridian", a fictional industrial supply network (firms, the
components they produce, the ports they ship through, the regions they sit
in, and the certifiers that audit them). Deliberately unrelated to the
NVIDIA news graph CoreDB was generalized from - a domain-agnostic engine
should be exercised on a domain it was not shaped by. Every name is
synthetic and every source URL uses the reserved `.example` TLD (RFC 2606),
so nothing here resolves to a real organization or document.

Layers, chosen so each one stresses a different part of the engine:

| Layer            | Cadence     | Stresses                                        |
|------------------|-------------|-------------------------------------------------|
| `LOCATED_IN`     | once        | wide, static entity population; `MATCH`          |
| `PRODUCES`       | once        | bipartite fan-out; reverse (`ops_idx`) lookups   |
| `SHIPS_VIA`      | once        | high-degree shared objects (ports as hubs)       |
| `PARTNERS_WITH`  | semi-annual | slow interval churn; `DIFF` over long windows    |
| `SUPPLIES` (mid) | monthly     | the bulk of version count; `SERIES`/`DIFF`       |
| `SUPPLIES` (hub) | weekly      | deep per-entity history H, for `TRACK`'s sweep   |
| `CERTIFIED_BY`   | episodic    | `Assertion`/`Source` evidence for `WHY_CHANGED`  |
| `PROBE_*`        | planted     | exact, hand-checkable answers (see ground truth) |

The `PROBE_*` entities form their own connected components, disjoint from
the bulk graph. That is deliberate twice over: it makes their expected
answers exact (nothing random can wire a shortcut into them), and it keeps
the BFS-bounded probes (`PATH`, `FIRST_CONNECTED`, `PATH_HISTORY`) cheap to
evaluate even though the surrounding graph has tens of thousands of edges.
"""
from __future__ import annotations

import random
from datetime import date, timedelta

# Bumped whenever this module's output changes - the event log's shape or
# content, or the ground-truth block's keys. A dataset directory records the
# version it was built with, so a stale on-disk copy is detected instead of
# silently under-testing (or failing with a confusing KeyError).
SPEC_VERSION = 1

DEFAULT_SEED = 20260819

CALENDAR_START = "2021-01-04"   # a Monday
CALENDAR_END = "2025-12-29"     # also a Monday

PROBE_PREFIX = "PROBE_"

SCALES: dict[str, dict[str, int]] = {
    # "entries" here means RelationshipVersions - see module docstring for
    # which layer contributes what. `full` lands in the high tens of
    # thousands; `small` is ~1/10th for a fast edit/run loop and for CI
    # machines that shouldn't spend a minute on generation.
    "full": {
        "firms": 3200, "components": 1200, "ports": 140, "regions": 32, "certifiers": 48,
        "partner_firms": 800, "mid_firms": 200, "hub_firms": 6, "cert_firms": 600,
        "sources": 400,
    },
    "small": {
        "firms": 320, "components": 120, "ports": 24, "regions": 8, "certifiers": 8,
        "partner_firms": 80, "mid_firms": 24, "hub_firms": 3, "cert_firms": 60,
        "sources": 60,
    },
}


# ----------------------------------------------------------------------
# Calendar helpers
# ----------------------------------------------------------------------

def _d(s: str) -> date:
    return date.fromisoformat(s)


def weekly_ticks(start: str = CALENDAR_START, end: str = CALENDAR_END) -> list[str]:
    """Every Monday in [start, end] - the finest ingestion cadence."""
    out, cur, last = [], _d(start), _d(end)
    while cur <= last:
        out.append(cur.isoformat())
        cur += timedelta(days=7)
    return out


def monthly_ticks(start: str = CALENDAR_START, end: str = CALENDAR_END) -> list[str]:
    """The 1st of every month fully inside [start, end]. Clamped to `start`
    so no layer can emit an event before the calendar's own first date."""
    out, y, m, first, last = [], _d(start).year, _d(start).month, _d(start), _d(end)
    while date(y, m, 1) <= last:
        if date(y, m, 1) >= first:
            out.append(date(y, m, 1).isoformat())
        y, m = (y + 1, 1) if m == 12 else (y, m + 1)
    return out


def semiannual_ticks(start: str = CALENDAR_START, end: str = CALENDAR_END) -> list[str]:
    return [t for t in monthly_ticks(start, end) if t[5:7] in ("01", "07")]


# ----------------------------------------------------------------------
# Event constructors - the only three shapes the loader understands.
# ----------------------------------------------------------------------

def _assert(s: str, p: str, o: str, d: str, c: float | None = None, sources=None) -> dict:
    e: dict = {"op": "assert", "s": s, "p": p, "o": o, "d": d}
    if c is not None:
        e["c"] = round(c, 4)
    if sources:
        e["sources"] = sources
    return e


def _retract(s: str, p: str, o: str, d: str) -> dict:
    return {"op": "retract", "s": s, "p": p, "o": o, "d": d}


def _sync(s: str, p: str, d: str, objects: dict[str, float],
           sources: dict | None = None) -> dict:
    e: dict = {"op": "sync", "s": s, "p": p, "d": d,
         "objects": {k: round(v, 4) for k, v in sorted(objects.items())}}
    if sources:
        e["sources"] = {k: sources[k] for k in sorted(sources)}
    return e


# ----------------------------------------------------------------------
# Source pool - a bounded set of reusable "documents".
# ----------------------------------------------------------------------

_PUBLISHERS = [
    ("meridian-registry.example", "Meridian Trade Registry"),
    ("portauthority.example", "Consolidated Port Authority Bulletin"),
    ("tradewire.example", "Tradewire Industrial Daily"),
    ("standards-board.example", "Meridian Standards Board"),
]


def _build_source_pool(rng: random.Random, n: int, ticks: list[str]) -> list[dict]:
    """`n` distinct source documents, reused across assertions.

    Deliberately bounded rather than one-URL-per-assertion: `_load_source`
    in the engine resolves a `source_id` by scanning the whole `sources`
    table (it is keyed by URL, for `_find_or_create_source`'s dedup), so
    source cardinality - not assertion count - is what sets `WHY_CHANGED`'s
    per-assertion lookup cost. A realistic registry publishes many filings
    from a few publishers, and keeping the pool bounded means the
    provenance tests measure the provenance path rather than that scan.
    """
    pool = []
    for i in range(n):
        domain, publisher = _PUBLISHERS[i % len(_PUBLISHERS)]
        pool.append({
            "url": f"https://{domain}/filings/{i:05d}",
            "title": f"{publisher} filing {i:05d}",
            "domain": domain,
            "published_at": rng.choice(ticks),
        })
    return pool


# ----------------------------------------------------------------------
# Bulk layers
# ----------------------------------------------------------------------

def _static_topology(rng: random.Random, firms, components, ports, regions) -> list[dict]:
    """Opened once on the calendar's first tick and left open: where each
    firm and port sits, what each firm makes, and which ports it ships
    through. This is the part of a real graph that never churns - it exists
    so pattern scans and traversal have a realistic static backdrop rather
    than a graph made entirely of hot edges.
    """
    events: list[dict] = []
    t0 = CALENDAR_START
    for f in firms:
        events.append(_assert(f, "LOCATED_IN", rng.choice(regions), t0, c=1.0))
    for p in ports:
        events.append(_assert(p, "LOCATED_IN", rng.choice(regions), t0, c=1.0))

    for f in firms:
        made = rng.sample(components, rng.randint(2, 6))
        events.append(_sync(f, "PRODUCES", t0, {c: rng.uniform(0.6, 1.0) for c in made}))
        used = rng.sample(ports, rng.randint(1, 3))
        events.append(_sync(f, "SHIPS_VIA", t0, {p: rng.uniform(0.4, 0.95) for p in used}))
    return events


def _churn_layer(rng: random.Random, subjects, pool, predicate: str, ticks: list[str],
                  size: int, turnover: float, source_pool, source_rate: float) -> list[dict]:
    """A `sync_snapshot`-per-tick layer: each subject holds ~`size` objects,
    replacing ~`turnover` of them each tick. This is the shape CoreDB's
    `sync_snapshot` exists for - "here is everything true right now" - and
    it is what actually produces history depth, since a dropped object gets
    closed at its own `last_confirmed` rather than at the tick date.
    """
    events: list[dict] = []
    for subj in subjects:
        # Never point a subject at itself: a self-loop is legal in the
        # engine but would make planted degree arithmetic ambiguous.
        candidates = [o for o in pool if o != subj]
        current = set(rng.sample(candidates, size))
        n_swap = max(0, int(round(size * turnover)))
        for i, tick in enumerate(ticks):
            # No swap on the first tick: the initial sample *is* that tick's
            # snapshot, so every subject's history starts from a state that
            # was actually ingested rather than one immediately overwritten.
            if i and n_swap and len(current) >= n_swap:
                for gone in rng.sample(sorted(current), n_swap):
                    current.discard(gone)
                while len(current) < size:
                    current.add(rng.choice(candidates))
            # sorted(), not plain set iteration: a set of strings iterates
            # in PYTHONHASHSEED-dependent order, so drawing from `rng` while
            # walking it unseeded would make the log reproducible only
            # within a single process. Every rng draw below is keyed to a
            # sorted order for exactly that reason.
            objects = {o: rng.uniform(0.35, 0.99) for o in sorted(current)}
            sources = {}
            for o in sorted(current):
                if rng.random() < source_rate:
                    sources[o] = [rng.choice(source_pool)]
            events.append(_sync(subj, predicate, tick, objects, sources or None))
    return events


def _certification_layer(rng: random.Random, firms, certifiers, ticks, source_pool) -> list[dict]:
    """Episodic, evidence-heavy: a certification opens with a citation,
    runs for a while, lapses, and is later renewed. Every event carries a
    source, so this layer is what gives `WHY_CHANGED` a real evidence trail
    to walk (the other layers only sample sources).
    """
    events: list[dict] = []
    for f in firms:
        certifier = rng.choice(certifiers)
        i = rng.randrange(0, 12)
        while i < len(ticks) - 8:
            opened = ticks[i]
            events.append(_assert(f, "CERTIFIED_BY", certifier, opened,
                                   c=rng.uniform(0.7, 1.0), sources=[rng.choice(source_pool)]))
            i += rng.randint(20, 60)
            if i >= len(ticks):
                break
            events.append(_retract(f, "CERTIFIED_BY", certifier, ticks[i]))
            i += rng.randint(4, 20)
    return events


# ----------------------------------------------------------------------
# Planted probes - exact answers, known by construction.
# ----------------------------------------------------------------------

CHAIN_NODES = [f"{PROBE_PREFIX}CHAIN_{c}" for c in "ABCDE"]
CHAIN_EDGE_DATES = ["2021-06-07", "2021-09-06", "2022-01-03", "2022-03-07"]
CHAIN_BREAK = ("2022-06-06", "2022-09-05")   # C->D closed, then reopened
CHAIN_SHORTCUT = ("2023-01-02", "2023-06-30")  # A->E direct, 1 hop, then closed

SHIFT_ENTITY = f"{PROBE_PREFIX}SHIFT"
SHIFT_REGIMES = [("2022-01-03", 4), ("2023-07-03", 20), ("2024-10-07", 9)]
SHIFT_WINDOW = ("2022-01-03", "2025-12-29")

CHURN_TRIPLE = (f"{PROBE_PREFIX}CHURN", "SUPPLIES", f"{PROBE_PREFIX}CHURN_PEER")
CHURN_CYCLES = 12

HUB_ENTITY = f"{PROBE_PREFIX}HUB"
HUB_DEGREE = 300
HUB_DATE = "2023-01-02"

DORMANT_ENTITY = f"{PROBE_PREFIX}DORMANT"
DORMANT_INTERVAL = ("2021-01-04", "2021-01-11")
DORMANT_QUIET_DATE = "2024-01-01"


def _probe_events(source_pool) -> tuple[list[dict], dict]:
    """Every planted structure the tests assert against, plus the
    `ground_truth` block describing it. Nothing here is random."""
    events: list[dict] = []

    # --- Chain: A-B-C-D-E, 4 hops, with a break and a later 1-hop shortcut.
    for (a, b), d in zip(zip(CHAIN_NODES, CHAIN_NODES[1:]), CHAIN_EDGE_DATES):
        events.append(_assert(a, "SUPPLIES", b, d, c=0.8))
    events.append(_retract(CHAIN_NODES[2], "SUPPLIES", CHAIN_NODES[3], CHAIN_BREAK[0]))
    events.append(_assert(CHAIN_NODES[2], "SUPPLIES", CHAIN_NODES[3], CHAIN_BREAK[1], c=0.8))
    events.append(_assert(CHAIN_NODES[0], "PARTNERS_WITH", CHAIN_NODES[4], CHAIN_SHORTCUT[0], c=0.6))
    events.append(_retract(CHAIN_NODES[0], "PARTNERS_WITH", CHAIN_NODES[4], CHAIN_SHORTCUT[1]))

    # --- Shift: a degree signal that is exactly flat inside each regime, so
    # the expected change points are the regime start dates and nothing
    # else. Peers are opened/closed to hit each regime's target degree.
    open_peers: list[str] = []
    minted = 0   # monotonic, so a reopened slot never reuses a retired peer's name
    for regime_start, target in SHIFT_REGIMES:
        while len(open_peers) > target:
            gone = open_peers.pop()
            # valid_to is inclusive, so close the day *before* the regime
            # starts for the new degree to hold on the regime's first date.
            events.append(_retract(SHIFT_ENTITY, "SUPPLIES", gone,
                                    (_d(regime_start) - timedelta(days=1)).isoformat()))
        while len(open_peers) < target:
            peer = f"{PROBE_PREFIX}SHIFT_PEER_{minted:03d}"
            minted += 1
            open_peers.append(peer)
            events.append(_assert(SHIFT_ENTITY, "SUPPLIES", peer, regime_start, c=0.75))

    # --- Churn: one triple opened and closed CHURN_CYCLES times, each open
    # citing a distinct source, so HISTORY/WHY_CHANGED have exact counts.
    s, p, o = CHURN_TRIPLE
    churn_intervals, churn_sources = [], []
    cur = _d("2021-02-01")
    for i in range(CHURN_CYCLES):
        src = source_pool[i % len(source_pool)]
        opened = cur.isoformat()
        closed = (cur + timedelta(days=20)).isoformat()
        events.append(_assert(s, p, o, opened, c=0.5 + (i % 5) / 10, sources=[src]))
        events.append(_retract(s, p, o, closed))
        churn_intervals.append([opened, closed])
        churn_sources.append(src["url"])
        cur += timedelta(days=90)

    # --- Hub: one very high-degree node, opened all at once.
    events.append(_sync(HUB_ENTITY, "SUPPLIES", HUB_DATE,
                         {f"{PROBE_PREFIX}HUB_PEER_{i:03d}": 0.5 for i in range(HUB_DEGREE)}))

    # --- Dormant: connected only briefly in 2021, so it is a live entity
    # with degree 0 (and closeness 0.0) on any later date.
    events.append(_assert(DORMANT_ENTITY, "SUPPLIES", f"{PROBE_PREFIX}DORMANT_PEER",
                           DORMANT_INTERVAL[0], c=0.5))
    events.append(_retract(DORMANT_ENTITY, "SUPPLIES", f"{PROBE_PREFIX}DORMANT_PEER",
                            DORMANT_INTERVAL[1]))

    ground_truth = {
        "chain": {
            "nodes": CHAIN_NODES,
            "edge_dates": CHAIN_EDGE_DATES,
            "expected_hops": len(CHAIN_NODES) - 1,
            "first_connected": CHAIN_EDGE_DATES[-1],
            "first_connected_window": ["2022-01-01", "2022-06-01"],
            "break": list(CHAIN_BREAK),
            "shortcut": list(CHAIN_SHORTCUT),
            # The inclusive-`valid_to` boundary, spelled out: retracting the
            # middle edge at date X leaves it open *through* X, so the chain
            # is still whole that day and only breaks on X+1. Reopening at Y
            # reconnects on Y itself. (date, connected) pairs.
            "break_boundary": [
                [CHAIN_BREAK[0], True],
                [(_d(CHAIN_BREAK[0]) + timedelta(days=1)).isoformat(), False],
                [(_d(CHAIN_BREAK[1]) - timedelta(days=1)).isoformat(), False],
                [CHAIN_BREAK[1], True],
            ],
            # (date, expected hop count or None) - checked against path_exists.
            "path_at": [
                ["2022-01-15", None],           # D-E not open yet
                ["2022-04-01", 4],              # full chain
                ["2022-08-01", None],           # C-D closed
                ["2022-10-01", 4],              # C-D reopened
                ["2023-03-01", 1],              # direct shortcut wins
                ["2023-08-01", 4],              # shortcut closed again
            ],
            # Harmonic closeness (sum of 1/distance, un-normalized) from
            # node A. Exact because the chain is its own component: with
            # the plain chain, B/C/D/E sit at 1/2/3/4 hops; with the
            # shortcut open, E moves to 1 hop and D to 2 (via E).
            "closeness_at": [
                ["2022-04-01", sum(1.0 / d for d in (1, 2, 3, 4))],
                ["2023-03-01", sum(1.0 / d for d in (1, 1, 2, 2))],
            ],
        },
        "shift": {
            "entity": SHIFT_ENTITY,
            "window": list(SHIFT_WINDOW),
            "resolution_days": 7,
            "regimes": [[d, n] for d, n in SHIFT_REGIMES],
            # Regime starts after the first: exactly what CHANGEPOINTS should find.
            "expected_changepoints": [d for d, _ in SHIFT_REGIMES[1:]],
        },
        "churn": {
            "triple": list(CHURN_TRIPLE),
            "n_versions": CHURN_CYCLES,
            "intervals": churn_intervals,
            # One source cited per open, in interval order - so WHY_CHANGED
            # over the whole window must surface exactly these, and no
            # assertion is attached to a retract (a closure carries no
            # citation in this dataset).
            "source_urls": churn_sources,
            "n_assertions_in_window": CHURN_CYCLES,
            "expected_status": "churned",
        },
        "hub": {"entity": HUB_ENTITY, "on_date": HUB_DATE, "degree": HUB_DEGREE},
        "dormant": {
            "entity": DORMANT_ENTITY,
            "interval": list(DORMANT_INTERVAL),
            "quiet_date": DORMANT_QUIET_DATE,
        },
    }
    return events, ground_truth


# ----------------------------------------------------------------------
# Entry point
# ----------------------------------------------------------------------

def build_events(scale: str = "full", seed: int = DEFAULT_SEED) -> tuple[list[dict], dict]:
    """The full chronological event log plus its ground truth.

    Deterministic in (`scale`, `seed`): the same arguments always produce a
    byte-identical log, which is what lets `gen_test_dataset.py` checksum it
    and lets a test detect a dataset built from a different spec.
    """
    if scale not in SCALES:
        raise ValueError(f"unknown scale {scale!r} - expected one of {sorted(SCALES)}")
    cfg = SCALES[scale]
    rng = random.Random(seed)

    weeks = weekly_ticks()
    months = monthly_ticks()
    halves = semiannual_ticks()

    firms = [f"FIRM_{i:05d}" for i in range(cfg["firms"])]
    components = [f"COMP_{i:04d}" for i in range(cfg["components"])]
    ports = [f"PORT_{i:03d}" for i in range(cfg["ports"])]
    regions = [f"REGION_{i:02d}" for i in range(cfg["regions"])]
    certifiers = [f"CERT_BODY_{i:02d}" for i in range(cfg["certifiers"])]
    source_pool = _build_source_pool(rng, cfg["sources"], weeks)

    deep_history_firms = firms[-cfg["hub_firms"]:]

    events: list[dict] = []
    events += _static_topology(rng, firms, components, ports, regions)
    events += _churn_layer(rng, firms[:cfg["partner_firms"]], firms, "PARTNERS_WITH",
                            halves, size=3, turnover=0.34, source_pool=source_pool, source_rate=0.05)
    events += _churn_layer(rng, firms[:cfg["mid_firms"]], firms, "SUPPLIES",
                            months, size=5, turnover=0.30, source_pool=source_pool, source_rate=0.12)
    events += _churn_layer(rng, deep_history_firms, firms, "SUPPLIES",
                            weeks, size=12, turnover=0.25, source_pool=source_pool, source_rate=0.15)
    events += _certification_layer(rng, firms[:cfg["cert_firms"]], certifiers, weeks, source_pool)

    probe_events, ground_truth = _probe_events(source_pool)
    events += probe_events

    # Chronological replay. A stable sort keeps same-date events in layer
    # order, and the layers never touch each other's (subject, predicate)
    # space, so no two events on one date can race for the same interval.
    events.sort(key=lambda e: e["d"])

    # Not planted values - just pointers at the entities with the deepest
    # history (weekly churn for the whole calendar, on top of the static
    # layers), so the O(H + D) sweep tests know where to find a large H
    # without hardcoding a firm id.
    ground_truth["deep_history"] = {
        "entities": deep_history_firms,
        "predicate": "SUPPLIES",
        "cadence_days": 7,
    }
    ground_truth["calendar"] = {
        "start": CALENDAR_START, "end": CALENDAR_END,
        "weekly_ticks": len(weeks), "monthly_ticks": len(months), "semiannual_ticks": len(halves),
    }
    ground_truth["entity_pools"] = {
        "firms": len(firms), "components": len(components), "ports": len(ports),
        "regions": len(regions), "certifiers": len(certifiers), "sources": len(source_pool),
    }
    return events, ground_truth
