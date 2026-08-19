"""The bitemporal graph engine.

Generalizes Knowledge_Graph/backend/db.py's proven pattern (interval-valued
facts with valid time + transaction time, snapshots/diffs/history all
derived from the fact log rather than stored) to arbitrary
(subject, predicate, object) triples instead of one hardcoded hub, on top
of a four-concept model: Entity, Relationship (stable triple identity),
RelationshipVersion (one interval), Assertion (one piece of evidence
backing a version).

A "pattern" is a 3-tuple (subject, predicate, object) where each element is
either a bound string or None (wildcard). Query methods return matching
RelationshipVersion objects; projecting which fields were wildcards is the
DSL executor's job, not the engine's.
"""
from __future__ import annotations

import json
from datetime import date, datetime, timezone

from .errors import SchemaVersionError, ValidationError
from .model import Assertion, Entity, RelationshipVersion
from .series import GraphDelta, GraphSeries, date_range
from .signal import GraphSignal
from .storage import keys as K
from .storage.kvstore import KVStore

_VERSIONS_LOW = b"\x00" * 8
_VERSIONS_HIGH = b"\xff" * 8 + b"\x00"

SCHEMA_VERSION = 1
_SCHEMA_VERSION_KEY = b"__schema_version__"


def _utcnow_iso() -> str:
    return datetime.now(timezone.utc).isoformat()


def _parse_date(d: str) -> date:
    return datetime.strptime(d, "%Y-%m-%d").date()


def _validate_identifier(name: str, value) -> None:
    if not isinstance(value, str) or not value:
        raise ValidationError(f"{name} must be a non-empty string, got {value!r}")
    if "\x00" in value:
        raise ValidationError(f"{name} must not contain a NUL byte, got {value!r}")


def _validate_date(name: str, value) -> None:
    if not isinstance(value, str):
        raise ValidationError(f"{name} must be a 'YYYY-MM-DD' string, got {value!r}")
    try:
        _parse_date(value)
    except ValueError as e:
        raise ValidationError(f"{name} is not a valid 'YYYY-MM-DD' date: {value!r}") from e


_MAX_DEPTH_CEILING = 10


def _validate_max_depth(max_depth) -> None:
    if not isinstance(max_depth, int) or isinstance(max_depth, bool) or not (1 <= max_depth <= _MAX_DEPTH_CEILING):
        raise ValidationError(
            f"max_depth must be an integer in [1, {_MAX_DEPTH_CEILING}], got {max_depth!r}"
        )


class Database:
    def __init__(self, store: KVStore):
        self._store = store
        self._check_schema_version()

    def close(self) -> None:
        self._store.close()

    def _check_schema_version(self) -> None:
        with self._store.txn(write=True) as t:
            raw = t.get("counters", _SCHEMA_VERSION_KEY)
            if raw is None:
                t.put("counters", _SCHEMA_VERSION_KEY, str(SCHEMA_VERSION).encode())
            else:
                on_disk = int(raw.decode())
                if on_disk != SCHEMA_VERSION:
                    raise SchemaVersionError(
                        f"database schema_version={on_disk} does not match this code's "
                        f"SCHEMA_VERSION={SCHEMA_VERSION}. Use Database.dump() with the "
                        "matching old version and coredb.restore() with this one to migrate."
                    )

    # ------------------------------------------------------------------
    # Internal helpers
    # ------------------------------------------------------------------

    def _load_version(self, t, vid: int) -> RelationshipVersion:
        raw = t.get("versions", K.encode_id(vid))
        return RelationshipVersion.from_dict(json.loads(raw))

    def _store_version(self, t, version: RelationshipVersion) -> None:
        t.put("versions", K.encode_id(version.version_id), json.dumps(version.to_dict()).encode())

    def _touch_entity(self, t, entity_id: str, seen_date: str) -> None:
        key = entity_id.encode("utf-8")
        raw = t.get("entities", key)
        if raw:
            ent = Entity.from_dict(json.loads(raw))
            ent.first_seen = min(ent.first_seen, seen_date) if ent.first_seen else seen_date
            ent.last_seen = max(ent.last_seen, seen_date) if ent.last_seen else seen_date
        else:
            ent = Entity(entity_id=entity_id, first_seen=seen_date, last_seen=seen_date)
        t.put("entities", key, json.dumps(ent.to_dict()).encode())

    def _find_or_create_relationship(self, t, subject: str, predicate: str, object_id: str, now_iso: str) -> int:
        key = K.triple_key(subject, predicate, object_id)
        raw = t.get("relationship_lookup", key)
        if raw:
            return K.decode_id(raw)
        rel_id = t.next_id("relationship")
        t.put("relationship_lookup", key, K.encode_id(rel_id))
        t.put("relationships", K.encode_id(rel_id), json.dumps({
            "relationship_id": rel_id, "subject_id": subject, "predicate": predicate,
            "object_id": object_id, "created_at": now_iso,
        }).encode())
        return rel_id

    def _find_or_create_source(self, t, url: str, title: str, domain: str) -> int:
        key = url.encode("utf-8")
        raw = t.get("sources", key)
        if raw:
            return json.loads(raw)["source_id"]
        sid = t.next_id("source")
        t.put("sources", key, json.dumps(
            {"source_id": sid, "url": url, "title": title, "domain": domain}
        ).encode())
        return sid

    def _create_assertions(self, t, relationship_id: int, version_id: int, source_list,
                            event_time: str, ingested_at: str, confidence: float | None) -> list[int]:
        if not source_list:
            return []
        ids = []
        for src in source_list:
            if isinstance(src, str):
                url, title, domain, published_at = src, "", "", None
            else:
                url = src["url"]
                title, domain = src.get("title", ""), src.get("domain", "")
                published_at = src.get("published_at")
            source_id = self._find_or_create_source(t, url, title, domain)
            aid = t.next_id("assertion")
            assertion = Assertion(assertion_id=aid, relationship_id=relationship_id, version_id=version_id,
                                   source_id=source_id, event_time=event_time, published_at=published_at,
                                   ingested_at=ingested_at, polarity=1, confidence=confidence)
            t.put("assertions", K.encode_id(aid), json.dumps(assertion.to_dict()).encode())
            t.put("assertions_by_version", K.assertion_by_version_key(version_id, aid), b"1")
            ids.append(aid)
        return ids

    def _open_version(self, t, subject: str, predicate: str, obj: str, valid_from: str,
                       confidence: float | None, now_iso: str) -> RelationshipVersion:
        rel_id = self._find_or_create_relationship(t, subject, predicate, obj, now_iso)
        vid = t.next_id("version")
        version = RelationshipVersion(
            version_id=vid, relationship_id=rel_id, subject_id=subject, predicate=predicate,
            object_id=obj, valid_from=valid_from, valid_to=None, system_from=now_iso, system_to=None,
            last_confirmed=valid_from, confidence=confidence,
        )
        self._store_version(t, version)
        t.put("open_idx", K.encode_id(rel_id), K.encode_id(vid))
        t.put("open_by_sp_idx", K.open_by_sp_key(subject, predicate, rel_id), K.encode_id(vid))
        t.put("spo_idx", K.spo_key(subject, predicate, valid_from, vid), K.encode_id(vid))
        t.put("ops_idx", K.ops_key(obj, predicate, subject, valid_from, vid), K.encode_id(vid))
        t.put("opened_time_idx", K.time_key(valid_from, vid), K.encode_id(vid))
        self._touch_entity(t, subject, valid_from)
        self._touch_entity(t, obj, valid_from)
        return version

    def _scan_candidates(self, t, subject: str | None, predicate: str | None, obj: str | None):
        """Best-available-index scan for a pattern, yielding RelationshipVersion
        objects. Bound subject or object uses spo_idx/ops_idx; a pattern with
        neither bound (only a predicate, or nothing at all) falls back to a
        full table scan - fine at this scale, a known limitation for large
        graphs (flagged for a future index).
        """
        if subject is not None:
            prefix = K.spo_prefix(subject, predicate)
            for _, val in t.range_iter("spo_idx", prefix, K.prefix_upper_bound(prefix)):
                version = self._load_version(t, K.decode_id(val))
                if obj is not None and version.object_id != obj:
                    continue
                yield version
        elif obj is not None:
            prefix = K.ops_prefix(obj, predicate)
            for _, val in t.range_iter("ops_idx", prefix, K.prefix_upper_bound(prefix)):
                yield self._load_version(t, K.decode_id(val))
        else:
            for _, val in t.range_iter("versions", _VERSIONS_LOW, _VERSIONS_HIGH):
                version = RelationshipVersion.from_dict(json.loads(val))
                if predicate is not None and version.predicate != predicate:
                    continue
                yield version

    def _scan_time_index(self, t, table: str, date_from: str, date_to: str,
                          exclusive_start: bool = False, exclusive_end: bool = False) -> list[RelationshipVersion]:
        start = date_from.encode()
        end = date_to.encode() + b"\xff"
        date_field = "valid_from" if table == "opened_time_idx" else "valid_to"
        results = []
        for _, val in t.range_iter(table, start, end):
            version = self._load_version(t, K.decode_id(val))
            d = getattr(version, date_field)
            if exclusive_start and d == date_from:
                continue
            if exclusive_end and d == date_to:
                continue
            results.append(version)
        return results

    # ------------------------------------------------------------------
    # Mutation API
    # ------------------------------------------------------------------

    def assert_fact(self, subject: str, predicate: str, object_id: str, valid_from: str,
                     confidence: float | None = None, sources=None) -> int:
        """Open a new interval for (subject, predicate, object_id), or - if
        one is already open - confirm it as of valid_from instead of
        creating a duplicate. Returns the version_id."""
        _validate_identifier("subject", subject)
        _validate_identifier("predicate", predicate)
        _validate_identifier("object_id", object_id)
        _validate_date("valid_from", valid_from)
        now_iso = _utcnow_iso()
        with self._store.txn(write=True) as t:
            rel_id = self._find_or_create_relationship(t, subject, predicate, object_id, now_iso)
            existing = t.get("open_idx", K.encode_id(rel_id))
            if existing:
                vid = K.decode_id(existing)
                version = self._load_version(t, vid)
                version.last_confirmed = max(version.last_confirmed, valid_from)
                if confidence is not None:
                    version.confidence = confidence
                new_ids = self._create_assertions(t, rel_id, vid, sources, valid_from, now_iso, confidence)
                version.assertion_ids.extend(new_ids)
                self._store_version(t, version)
                self._touch_entity(t, subject, valid_from)
                self._touch_entity(t, object_id, valid_from)
            else:
                version = self._open_version(t, subject, predicate, object_id, valid_from, confidence, now_iso)
                vid = version.version_id
                new_ids = self._create_assertions(t, rel_id, vid, sources, valid_from, now_iso, confidence)
                if new_ids:
                    version.assertion_ids.extend(new_ids)
                    self._store_version(t, version)
            return vid

    def retract_fact(self, subject: str, predicate: str, object_id: str, valid_to: str) -> int | None:
        """Explicitly close the open interval for (subject, predicate, object_id)
        at valid_to. Returns the version_id closed, or None if nothing was open."""
        _validate_identifier("subject", subject)
        _validate_identifier("predicate", predicate)
        _validate_identifier("object_id", object_id)
        _validate_date("valid_to", valid_to)
        now_iso = _utcnow_iso()
        with self._store.txn(write=True) as t:
            key = K.triple_key(subject, predicate, object_id)
            rel_raw = t.get("relationship_lookup", key)
            if not rel_raw:
                return None
            rel_id = K.decode_id(rel_raw)
            existing = t.get("open_idx", K.encode_id(rel_id))
            if not existing:
                return None
            vid = K.decode_id(existing)
            version = self._load_version(t, vid)
            if valid_to < version.valid_from:
                raise ValidationError(
                    f"valid_to ({valid_to!r}) precedes valid_from ({version.valid_from!r}) "
                    f"for ({subject!r}, {predicate!r}, {object_id!r})"
                )
            version.valid_to = valid_to
            version.system_to = now_iso
            self._store_version(t, version)
            t.delete("open_idx", K.encode_id(rel_id))
            t.delete("open_by_sp_idx", K.open_by_sp_key(subject, predicate, rel_id))
            t.put("closed_time_idx", K.time_key(valid_to, vid), K.encode_id(vid))
            return vid

    def sync_snapshot(self, subject: str, predicate: str, objects_now_true: dict,
                       as_of_date: str, sources: dict | None = None) -> dict:
        """Generalizes db.py's ingest_daily_snapshot to any (subject, predicate)
        pair: diffs `objects_now_true` (object_id -> confidence) against the
        currently-open versions for this pair, opening new ones, confirming
        existing ones, and closing ones no longer present (closed at their
        last_confirmed date, so ingestion gaps don't fabricate false
        closures). `sources`, if given, maps object_id -> list of source
        dicts/urls, each producing an Assertion for this date."""
        _validate_identifier("subject", subject)
        _validate_identifier("predicate", predicate)
        _validate_date("as_of_date", as_of_date)
        for obj in objects_now_true:
            _validate_identifier("object_id", obj)
        sources = sources or {}
        now_iso = _utcnow_iso()
        opened, closed, confirmed = [], [], []
        with self._store.txn(write=True) as t:
            currently_open = self._open_objects_for(t, subject, predicate)  # object_id -> (relationship_id, version_id)

            for obj, confidence in objects_now_true.items():
                if obj in currently_open:
                    rel_id, vid = currently_open[obj]
                    version = self._load_version(t, vid)
                    # max(), not a direct assignment: an out-of-chronological-order
                    # as_of_date must never move last_confirmed backwards below
                    # valid_from, or a later close would produce an inverted interval.
                    version.last_confirmed = max(version.last_confirmed, as_of_date)
                    if confidence is not None:
                        version.confidence = confidence
                    new_ids = self._create_assertions(t, rel_id, vid, sources.get(obj), as_of_date, now_iso, confidence)
                    version.assertion_ids.extend(new_ids)
                    self._store_version(t, version)
                    self._touch_entity(t, subject, as_of_date)
                    self._touch_entity(t, obj, as_of_date)
                    confirmed.append(vid)
                else:
                    version = self._open_version(t, subject, predicate, obj, as_of_date, confidence, now_iso)
                    vid = version.version_id
                    new_ids = self._create_assertions(t, version.relationship_id, vid, sources.get(obj),
                                                       as_of_date, now_iso, confidence)
                    if new_ids:
                        version.assertion_ids.extend(new_ids)
                        self._store_version(t, version)
                    opened.append(vid)

            for obj, (rel_id, vid) in currently_open.items():
                if obj not in objects_now_true:
                    version = self._load_version(t, vid)
                    t.delete("open_idx", K.encode_id(rel_id))
                    t.delete("open_by_sp_idx", K.open_by_sp_key(subject, predicate, rel_id))
                    t.put("closed_time_idx", K.time_key(version.last_confirmed, vid), K.encode_id(vid))
                    version.valid_to = version.last_confirmed
                    version.system_to = now_iso
                    self._store_version(t, version)
                    closed.append(vid)

        return {"opened": opened, "closed": closed, "confirmed": confirmed}

    def _open_objects_for(self, t, subject: str, predicate: str) -> dict:
        """object_id -> (relationship_id, version_id) for every currently-open
        version under (subject, predicate), via open_by_sp_idx - O(number
        currently open), not O(all history ever seen for this pair). Used by
        sync_snapshot to find objects that need closing."""
        result = {}
        prefix = K.open_by_sp_prefix(subject, predicate)
        for _, val in t.range_iter("open_by_sp_idx", prefix, K.prefix_upper_bound(prefix)):
            vid = K.decode_id(val)
            version = self._load_version(t, vid)
            result[version.object_id] = (version.relationship_id, vid)
        return result

    # ------------------------------------------------------------------
    # Query API - pure reads, everything derived from the version log
    # ------------------------------------------------------------------

    def as_of(self, pattern: tuple, on_date: str) -> list[RelationshipVersion]:
        """Versions matching `pattern` whose interval covers `on_date`. Works
        for any date, including ones never directly asserted, as long as it
        falls inside a version's interval."""
        subject, predicate, obj = pattern
        with self._store.txn() as t:
            return [v for v in self._scan_candidates(t, subject, predicate, obj)
                    if v.valid_from <= on_date and (v.valid_to is None or v.valid_to >= on_date)]

    def history(self, pattern: tuple, start: str | None = None, end: str | None = None) -> list[RelationshipVersion]:
        """All intervals matching `pattern` overlapping [start, end] (open
        bounds mean unbounded), sorted chronologically."""
        subject, predicate, obj = pattern
        with self._store.txn() as t:
            versions = list(self._scan_candidates(t, subject, predicate, obj))
        if start is not None:
            versions = [v for v in versions if v.valid_to is None or v.valid_to >= start]
        if end is not None:
            versions = [v for v in versions if v.valid_from <= end]
        versions.sort(key=lambda v: v.valid_from)
        return versions

    def diff(self, date_from: str, date_to: str, pattern: tuple | None = None) -> dict:
        """Versions opened, closed, or persisted between two dates, computed
        directly from the version log (catches versions that both opened and
        closed entirely inside the window, unlike comparing two as_of
        snapshots). `pattern`, if given, scopes the diff; otherwise it scans
        the whole graph."""
        with self._store.txn() as t:
            if pattern is not None:
                subject, predicate, obj = pattern
                candidates = list(self._scan_candidates(t, subject, predicate, obj))
                opened = [v for v in candidates if v.valid_from > date_from and v.valid_from <= date_to]
                closed = [v for v in candidates
                          if v.valid_to is not None and v.valid_to >= date_from and v.valid_to < date_to]
                persisted = [v for v in candidates
                             if v.valid_from <= date_from and (v.valid_to is None or v.valid_to >= date_to)]
            else:
                opened = self._scan_time_index(t, "opened_time_idx", date_from, date_to, exclusive_start=True)
                closed = self._scan_time_index(t, "closed_time_idx", date_from, date_to, exclusive_end=True)
                # No interval index yet for "spans both dates" - full scan.
                all_versions = [RelationshipVersion.from_dict(json.loads(v))
                                for _, v in t.range_iter("versions", _VERSIONS_LOW, _VERSIONS_HIGH)]
                persisted = [v for v in all_versions
                             if v.valid_from <= date_from and (v.valid_to is None or v.valid_to >= date_to)]
        return {"opened": opened, "closed": closed, "persisted": persisted}

    def diff_delta(self, pattern: tuple | None, date_from: str, date_to: str) -> GraphDelta:
        """Like diff(), reshaped into a structured GraphDelta: node sets are
        netted out against churn, so an object whose relationship both
        opened and closed inside the window (or was already present before
        and after) doesn't spuriously show up as both added and removed."""
        result = self.diff(date_from, date_to, pattern)
        opened, closed, persisted = result["opened"], result["closed"], result["persisted"]
        opened_objs = {v.object_id for v in opened}
        closed_objs = {v.object_id for v in closed}
        persisted_objs = {v.object_id for v in persisted}
        nodes_added = sorted(opened_objs - closed_objs - persisted_objs)
        nodes_removed = sorted(closed_objs - opened_objs - persisted_objs)
        return GraphDelta(date_from=date_from, date_to=date_to, nodes_added=nodes_added,
                           nodes_removed=nodes_removed, edges_opened=opened, edges_closed=closed,
                           edges_persisted=persisted)

    def range_agg(self, pattern: tuple, start: str, end: str) -> dict[str, int]:
        """Day-count of each matching version's overlap with [start, end]
        (inclusive), aggregated by whichever side of the pattern is a
        wildcard - object_id when the subject is bound (or neither side is),
        subject_id for a reverse pattern (object bound, subject wildcard).
        Aggregating by object_id unconditionally would be meaningless for a
        reverse pattern, since every candidate shares the same object_id.
        An entity can have several disjoint intervals in the window, whose
        day counts are summed."""
        subject, predicate, obj = pattern
        with self._store.txn() as t:
            candidates = [v for v in self._scan_candidates(t, subject, predicate, obj)
                          if v.valid_from <= end and (v.valid_to is None or v.valid_to >= start)]
        start_dt, end_dt = _parse_date(start), _parse_date(end)
        key_field = "subject_id" if subject is None and obj is not None else "object_id"
        day_counts: dict[str, int] = {}
        for v in candidates:
            overlap_from = max(start_dt, _parse_date(v.valid_from))
            overlap_to = min(end_dt, _parse_date(v.valid_to) if v.valid_to else end_dt)
            days = max(0, (overlap_to - overlap_from).days + 1)
            key = getattr(v, key_field)
            day_counts[key] = day_counts.get(key, 0) + days
        return day_counts

    def as_known(self, pattern: tuple, on_date: str, knowledge_cutoff: str) -> list[RelationshipVersion]:
        """as_of, further constrained to versions this database had actually
        recorded by `knowledge_cutoff` (ISO datetime) - reconstructs belief
        state as of a past point in wall-clock time, avoiding look-ahead
        bias."""
        subject, predicate, obj = pattern
        with self._store.txn() as t:
            candidates = list(self._scan_candidates(t, subject, predicate, obj))
        return [v for v in candidates
                if v.valid_from <= on_date and (v.valid_to is None or v.valid_to >= on_date)
                and v.system_from <= knowledge_cutoff
                and (v.system_to is None or v.system_to > knowledge_cutoff)]

    def series(self, pattern: tuple, start: str, end: str, resolution_days: int = 1) -> GraphSeries:
        """A lazy view over `pattern`'s history across [start, end] - nothing
        is precomputed here; snapshots/diffs are resolved on demand."""
        return GraphSeries(self, pattern, start, end, resolution_days)

    # ------------------------------------------------------------------
    # Graph metrics - what's honestly computable on the current single-hop
    # model. True centrality (betweenness/closeness/PageRank) needs
    # multi-hop traversal, which doesn't exist yet - deferred, not attempted
    # here.
    # ------------------------------------------------------------------

    def _neighbor_versions(self, t, entity_id: str, on_date: str) -> list[RelationshipVersion]:
        """Every relationship touching entity_id (as subject or object)
        active on on_date, deduplicated by relationship_id so a self-loop
        isn't returned twice. Takes an already-open transaction so callers
        doing many of these (BFS) don't pay a fresh-transaction cost per
        call."""
        subject_side = [v for v in self._scan_candidates(t, entity_id, None, None)
                         if v.valid_from <= on_date and (v.valid_to is None or v.valid_to >= on_date)]
        object_side = [v for v in self._scan_candidates(t, None, None, entity_id)
                        if v.valid_from <= on_date and (v.valid_to is None or v.valid_to >= on_date)]
        by_relationship = {v.relationship_id: v for v in subject_side + object_side}
        return list(by_relationship.values())

    def degree(self, entity_id: str, on_date: str, weighted: bool = False) -> float:
        """How many relationships touch `entity_id` (as subject or object)
        on `on_date`. `weighted=True` sums confidence (None treated as 0.0)
        instead of counting. Deduplicated by relationship_id so a self-loop
        (entity_id as both subject and object of the same relationship)
        isn't counted twice."""
        with self._store.txn() as t:
            neighbors = self._neighbor_versions(t, entity_id, on_date)
        if weighted:
            return sum(v.confidence or 0.0 for v in neighbors)
        return float(len(neighbors))

    def edge_weight(self, subject: str, predicate: str, object_id: str, on_date: str) -> float | None:
        """The confidence of one specific relationship on `on_date`, or None
        if it isn't open (or has no confidence set) then."""
        matches = self.as_of((subject, predicate, object_id), on_date)
        return matches[0].confidence if matches else None

    def track(self, metric: str, target, start: str, end: str, resolution_days: int = 1) -> GraphSignal:
        """Evaluate a graph metric at each `resolution_days` step across
        [start, end], returning a GraphSignal - a plain time series, eager
        (not lazy like GraphSeries) since a signal is a small point list
        meant to be joined/plotted immediately."""
        if metric == "degree":
            fn = lambda d: self.degree(target, d)
        elif metric == "weighted_degree":
            fn = lambda d: self.degree(target, d, weighted=True)
        elif metric == "edge_weight":
            subject, predicate, object_id = target
            fn = lambda d: self.edge_weight(subject, predicate, object_id, d)
        else:
            raise ValidationError(
                f"unknown metric {metric!r} - expected 'degree', 'weighted_degree', or 'edge_weight'"
            )
        points = [(d, fn(d)) for d in date_range(start, end, resolution_days)]
        return GraphSignal(metric=metric, target=target, points=points)

    # ------------------------------------------------------------------
    # Multi-hop traversal - point-to-point path queries between two named
    # entities, via BFS over _neighbor_versions. Not general multi-hop
    # pattern matching inside MATCH/HISTORY (still deferred) - see
    # TGQL_SPEC.md.
    # ------------------------------------------------------------------

    def path_exists(self, subject: str, object_id: str, on_date: str,
                     max_depth: int = 4) -> list[RelationshipVersion] | None:
        """Shortest path from `subject` to `object_id` over edges active on
        `on_date`, via breadth-first search bounded by `max_depth` hops.
        Returns the path as an ordered list of RelationshipVersion (empty
        list if subject == object_id), or None if unreachable within
        max_depth."""
        _validate_identifier("subject", subject)
        _validate_identifier("object_id", object_id)
        _validate_date("on_date", on_date)
        _validate_max_depth(max_depth)
        if subject == object_id:
            return []
        with self._store.txn() as t:
            visited = {subject}
            frontier = [(subject, [])]
            for _ in range(max_depth):
                next_frontier = []
                for node, path in frontier:
                    for v in self._neighbor_versions(t, node, on_date):
                        other = v.object_id if v.subject_id == node else v.subject_id
                        if other == object_id:
                            return path + [v]
                        if other not in visited:
                            visited.add(other)
                            next_frontier.append((other, path + [v]))
                frontier = next_frontier
                if not frontier:
                    break
        return None

    def first_connected(self, subject: str, object_id: str, start: str | None = None,
                         end: str | None = None, max_depth: int = 4) -> str | None:
        """The earliest date within [start, end] (or all history if either
        bound is omitted) at which `subject` and `object_id` become
        connected within `max_depth` hops. Candidate dates are drawn from
        opened_time_idx (every date some relationship opened) -
        connectivity isn't monotonic (edges can also close), so this is a
        chronological scan calling path_exists at each candidate, not a
        binary search."""
        _validate_identifier("subject", subject)
        _validate_identifier("object_id", object_id)
        if start is not None:
            _validate_date("start", start)
        if end is not None:
            _validate_date("end", end)
        _validate_max_depth(max_depth)
        if subject == object_id:
            # Trivially connected throughout the queried window - "first"
            # is the window's own start, or undetermined without one.
            return start

        with self._store.txn() as t:
            start_bytes = (start or "").encode()
            end_bytes = end.encode() + b"\xff" if end is not None else b"\xff"
            candidate_dates = sorted({
                self._load_version(t, K.decode_id(val)).valid_from
                for _, val in t.range_iter("opened_time_idx", start_bytes, end_bytes)
            })

        for d in candidate_dates:
            if self.path_exists(subject, object_id, d, max_depth=max_depth) is not None:
                return d
        return None

    def path_history(self, subject: str, object_id: str, start: str, end: str,
                      resolution_days: int = 1, max_depth: int = 4) -> list[dict]:
        """Steps through [start, end] at resolution_days, resolving
        path_exists at each date - shows a path emerging, changing, or
        disappearing over time. Returned as plain dicts (a path is
        structured data, not a float, so GraphSignal's points shape doesn't
        fit here)."""
        points = []
        for d in date_range(start, end, resolution_days):
            path = self.path_exists(subject, object_id, d, max_depth=max_depth)
            points.append({
                "date": d,
                "path": [v.to_dict() for v in path] if path is not None else None,
            })
        return points

    def get_entity(self, entity_id: str) -> Entity | None:
        with self._store.txn() as t:
            raw = t.get("entities", entity_id.encode("utf-8"))
        return Entity.from_dict(json.loads(raw)) if raw else None

    # ------------------------------------------------------------------
    # Storage management
    # ------------------------------------------------------------------

    def stats(self) -> dict:
        """Cheap entry counts per logical table - uses LMDB's native stat(),
        not manual iteration."""
        with self._store.txn() as t:
            return {
                "relationships": t.count("relationships"),
                "versions": t.count("versions"),
                "assertions": t.count("assertions"),
                "entities": t.count("entities"),
                "sources": t.count("sources"),
            }

    def backup(self, path: str) -> None:
        """A compacted, self-contained copy of the current database at `path`."""
        self._store.backup(path)

    def dump(self, path: str) -> None:
        """Schema-independent JSON-lines export of the logical facts
        (subject/predicate/object/valid_from/valid_to/confidence, sorted by
        valid_from) - not internal ids or system-time, which a raw storage
        copy would preserve but a schema change would invalidate. Pair with
        coredb.restore() to migrate across a schema change."""
        with self._store.txn() as t:
            versions = [RelationshipVersion.from_dict(json.loads(v))
                        for _, v in t.range_iter("versions", _VERSIONS_LOW, _VERSIONS_HIGH)]
        versions.sort(key=lambda v: v.valid_from)
        with open(path, "w", encoding="utf-8") as f:
            for v in versions:
                record = {
                    "subject_id": v.subject_id, "predicate": v.predicate, "object_id": v.object_id,
                    "valid_from": v.valid_from, "valid_to": v.valid_to, "confidence": v.confidence,
                }
                f.write(json.dumps(record) + "\n")

    # ------------------------------------------------------------------
    # DSL entrypoint - lazy import to keep engine.py decoupled from the
    # query package (executor.py never imports engine.py, so this isn't
    # circular, just deferred).
    # ------------------------------------------------------------------

    def execute(self, dsl: str) -> list[dict] | dict:
        from .query.executor import execute as _execute
        from .query.parser import parse
        return _execute(self, parse(dsl))
