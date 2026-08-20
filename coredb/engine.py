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
from collections import deque
from datetime import date, datetime, timedelta, timezone

from .errors import SchemaVersionError, ValidationError
from .model import Assertion, Entity, RelationshipVersion, Source
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


def _next_day(d: str) -> str:
    return (_parse_date(d) + timedelta(days=1)).strftime("%Y-%m-%d")


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

    def _load_source(self, t, source_id: int) -> Source | None:
        """Resolve a source_id back to its Source record. The sources table
        is keyed by url (for _find_or_create_source's dedup), not by
        source_id, so this is a full scan - a one-off provenance lookup,
        not a hot path, same tradeoff as _active_entity_ids/diff()'s global
        branch."""
        for _, val in t.range_iter("sources", b"", b"\xff"):
            s = Source.from_dict(json.loads(val))
            if s.source_id == source_id:
                return s
        return None

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

    def series_snapshots(self, pattern: tuple, start: str, end: str,
                          resolution_days: int = 1) -> list[tuple[str, list[RelationshipVersion]]]:
        """The same (date, matching_versions) pairs as calling
        as_of(pattern, d) for every d in date_range(start, end,
        resolution_days), computed in one O(H + D) sweep instead of D
        separate O(H) as_of() scans - H being the total number of matching
        intervals across the pattern's whole history. Unlike
        _degree_track_points there's no relationship-level dedup ambiguity
        to resolve: as_of() never deduped by relationship_id, so this is a
        plain active-version-set sweep, keyed by version_id. Used by
        GraphSeries.__iter__; GraphSeries.at() (a single date) still calls
        as_of() directly since a lone query has no D to amortize."""
        subject, predicate, obj = pattern
        with self._store.txn() as t:
            versions = list(self._scan_candidates(t, subject, predicate, obj))
        events = []  # (date, is_open, version)
        for v in versions:
            events.append((v.valid_from, True, v))
            if v.valid_to is not None:
                events.append((_next_day(v.valid_to), False, v))
        events.sort(key=lambda e: e[0])

        active: dict[int, RelationshipVersion] = {}
        points = []
        ei, n_events = 0, len(events)
        for d in date_range(start, end, resolution_days):
            while ei < n_events and events[ei][0] <= d:
                _, is_open, v = events[ei]
                if is_open:
                    active[v.version_id] = v
                else:
                    active.pop(v.version_id, None)
                ei += 1
            points.append((d, list(active.values())))
        return points

    # ------------------------------------------------------------------
    # Graph metrics. degree/weighted_degree/edge_weight need only a single
    # neighbor lookup; closeness/betweenness/pagerank (below, in the
    # Centrality section) build on the BFS traversal primitives to answer
    # what single-hop metrics can't.
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

    def _neighbor_node_ids(self, t, entity_id: str, on_date: str) -> set[str]:
        """Distinct node ids adjacent to entity_id as of on_date - like
        _neighbor_versions but deduplicated by the *other* node rather than
        by relationship_id, so two entities connected by more than one
        relationship (e.g. different predicates) count as a single graph
        edge. This is what simple-graph algorithms (closeness/betweenness)
        need; path_exists etc. use _neighbor_versions directly since they
        need the actual RelationshipVersion for path reconstruction, and
        their visited-set logic already handles multi-edges correctly
        without this dedup."""
        others = {v.object_id if v.subject_id == entity_id else v.subject_id
                  for v in self._neighbor_versions(t, entity_id, on_date)}
        others.discard(entity_id)
        return others

    def _all_versions_touching(self, t, entity_id: str) -> list[RelationshipVersion]:
        """Every RelationshipVersion (every interval across the entity's
        whole history, not date-filtered) where entity_id is subject or
        object - the raw material for a degree/weighted_degree interval
        sweep. Deduplicated by version_id (not relationship_id, unlike
        _neighbor_versions) since every disjoint interval matters here, not
        just whichever is active "now"; the dedup only matters for a
        self-loop, which would otherwise appear in both the subject-side
        and object-side scan."""
        subject_side = list(self._scan_candidates(t, entity_id, None, None))
        object_side = list(self._scan_candidates(t, None, None, entity_id))
        by_version = {v.version_id: v for v in subject_side + object_side}
        return list(by_version.values())

    def _degree_track_points(self, entity_id: str, query_dates: list[str],
                              weighted: bool) -> list[tuple[str, float]]:
        """degree()/weighted_degree, swept across query_dates in one O(H + D)
        pass instead of D separate O(H) degree() calls (H = entity_id's
        total history depth, D = len(query_dates)). Fetches every version
        touching entity_id once, groups by relationship_id (a relationship
        contributes at most once, matching degree()'s own dedup), builds
        +delta/-delta events at each interval's valid_from/day-after-valid_to,
        and sweeps a running total forward.

        Boundary case: a same-day retract_fact(valid_to=d) immediately
        followed by assert_fact(valid_from=d) produces two versions of one
        relationship whose valid-time intervals both cover d (valid_to is
        inclusive) even though they were never simultaneously open in
        system time. degree()'s per-date dedup resolves this via
        list/iteration order (not a meaningful guarantee); this sweep
        instead deterministically has the most-recently-opened version win
        - a documented, deliberate choice, not an accidental divergence.
        """
        with self._store.txn() as t:
            versions = self._all_versions_touching(t, entity_id)
        by_rel: dict[int, list[RelationshipVersion]] = {}
        for v in versions:
            by_rel.setdefault(v.relationship_id, []).append(v)

        events = []  # (date, is_open, relationship_id, version_id, delta)
        for rel_id, vs in by_rel.items():
            for v in vs:
                delta = (v.confidence or 0.0) if weighted else 1.0
                events.append((v.valid_from, True, rel_id, v.version_id, delta))
                if v.valid_to is not None:
                    events.append((_next_day(v.valid_to), False, rel_id, v.version_id, delta))
        events.sort(key=lambda e: e[0])

        open_versions: dict[int, set] = {}
        contribution: dict[int, float] = {}
        running = 0.0
        points = []
        ei, n_events = 0, len(events)
        for d in query_dates:
            while ei < n_events and events[ei][0] <= d:
                _, is_open, rel_id, vid, delta = events[ei]
                open_set = open_versions.setdefault(rel_id, set())
                if is_open:
                    open_set.add(vid)
                    running -= contribution.get(rel_id, 0.0)
                    contribution[rel_id] = delta
                    running += delta
                else:
                    open_set.discard(vid)
                    if not open_set:
                        running -= contribution.get(rel_id, 0.0)
                        contribution[rel_id] = 0.0
                ei += 1
            points.append((d, running))
        return points

    def _edge_weight_track_points(self, subject: str, predicate: str, object_id: str,
                                   query_dates: list[str]) -> list[tuple[str, float | None]]:
        """edge_weight(), swept across query_dates in one O(H + D) pass
        instead of D separate O(H) edge_weight() calls. Fetches the
        triple's full version history once (already valid_from-sorted via
        history()) and steps a "currently active interval" pointer forward,
        reporting its confidence (or None where no interval covers a date)
        - a step function, not an additive counter, since edge_weight has
        at most one relationship to track. Same same-day-boundary caveat as
        _degree_track_points: the most-recently-opened covering interval
        wins."""
        versions = self.history((subject, predicate, object_id))
        points = []
        vi, n_versions = 0, len(versions)
        current = None
        for d in query_dates:
            while vi < n_versions and versions[vi].valid_from <= d:
                current = versions[vi]
                vi += 1
            if current is not None and (current.valid_to is None or current.valid_to >= d):
                points.append((d, current.confidence))
            else:
                points.append((d, None))
        return points

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

    def track(self, metric: str, target, start: str, end: str, resolution_days: int = 1,
              max_depth: int = 4) -> GraphSignal:
        """Evaluate a graph metric at each `resolution_days` step across
        [start, end], returning a GraphSignal - a plain time series, eager
        (not lazy like GraphSeries) since a signal is a small point list
        meant to be joined/plotted immediately. `max_depth` only applies to
        the BFS-bounded metrics (closeness/betweenness); harmless to pass
        for the others.

        degree/weighted_degree/edge_weight are computed via a single O(H + D)
        interval sweep (see _degree_track_points/_edge_weight_track_points)
        rather than D separate O(H) per-date reconstructions.
        closeness/betweenness/pagerank remain one call per date - they're
        global per-date graph computations with no simple event-sweep
        equivalent (see Documentation/ARCHITECTURE.md's Performance
        section); benchmarks/ tracks this cost so it stays visible."""
        query_dates = list(date_range(start, end, resolution_days))
        if metric == "degree":
            points = self._degree_track_points(target, query_dates, weighted=False)
        elif metric == "weighted_degree":
            points = self._degree_track_points(target, query_dates, weighted=True)
        elif metric == "edge_weight":
            subject, predicate, object_id = target
            points = self._edge_weight_track_points(subject, predicate, object_id, query_dates)
        elif metric == "closeness":
            points = [(d, self.closeness(target, d, max_depth=max_depth)) for d in query_dates]
        elif metric == "betweenness":
            points = [(d, self.betweenness(target, d, max_depth=max_depth)) for d in query_dates]
        elif metric == "pagerank":
            points = [(d, self.pagerank(target, d)) for d in query_dates]
        else:
            raise ValidationError(
                f"unknown metric {metric!r} - expected one of 'degree', 'weighted_degree', "
                "'edge_weight', 'closeness', 'betweenness', 'pagerank'"
            )
        return GraphSignal(metric=metric, target=target, points=points)

    def changepoints(self, metric: str, target, start: str, end: str, resolution_days: int = 1,
                      max_depth: int = 4, min_size: int = 2, penalty: float | None = None) -> list[str]:
        """track()s `metric` across [start, end] and returns the dates
        where its value undergoes a significant mean shift (binary
        segmentation - see coredb/signal.py's detect_changepoints)."""
        signal = self.track(metric, target, start, end, resolution_days, max_depth=max_depth)
        return signal.changepoints(min_size=min_size, penalty=penalty)

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

    # ------------------------------------------------------------------
    # Centrality - built on the BFS primitives above. Closeness and
    # betweenness are honest variants for a graph that may be disconnected
    # and is always traversed with a bounded max_depth (see closeness()'s
    # and betweenness_all()'s docstrings for exactly why). Betweenness and
    # PageRank are global computations - they process every active entity
    # in one pass regardless of which entity you actually care about.
    # ------------------------------------------------------------------

    def _active_entity_ids(self, t, on_date: str) -> set[str]:
        """Every entity touching at least one relationship active on
        on_date - the node set for the global algorithms below. No index
        exists for "distinct entities as of a date", so this is a full
        versions-table scan - the same documented limitation diff()'s
        global branch already has, not a new one."""
        ids = set()
        for _, val in t.range_iter("versions", _VERSIONS_LOW, _VERSIONS_HIGH):
            v = RelationshipVersion.from_dict(json.loads(val))
            if v.valid_from <= on_date and (v.valid_to is None or v.valid_to >= on_date):
                ids.add(v.subject_id)
                ids.add(v.object_id)
        return ids

    def _bfs_distances(self, t, entity_id: str, on_date: str, max_depth: int) -> dict[str, int]:
        """Every node reachable from entity_id within max_depth hops, as of
        on_date, mapped to its shortest-path distance (entity_id itself is
        not included - distance 0 to yourself isn't meaningful here)."""
        distances = {}
        seen = {entity_id}
        frontier = {entity_id}
        depth = 0
        while frontier and depth < max_depth:
            depth += 1
            next_frontier = set()
            for node in frontier:
                for other in self._neighbor_node_ids(t, node, on_date):
                    if other not in seen:
                        seen.add(other)
                        distances[other] = depth
                        next_frontier.add(other)
            frontier = next_frontier
        return distances

    def closeness(self, entity_id: str, on_date: str, max_depth: int = 4) -> float:
        """Harmonic closeness centrality: sum(1/distance) over every node
        reachable from entity_id within max_depth hops, as of on_date. Uses
        the harmonic form rather than classical closeness
        ((n-1)/sum(distances)) because classical closeness is undefined -
        or misleadingly small - when the graph may be disconnected or
        traversal is bounded by max_depth, both always true here. This is
        the standard Marchiori & Latora variant for exactly this situation,
        not an ad hoc substitute."""
        _validate_identifier("entity_id", entity_id)
        _validate_date("on_date", on_date)
        _validate_max_depth(max_depth)
        with self._store.txn() as t:
            distances = self._bfs_distances(t, entity_id, on_date, max_depth)
        return sum(1.0 / d for d in distances.values())

    def betweenness_all(self, on_date: str, max_depth: int = 4) -> dict[str, float]:
        """Brandes' betweenness centrality (unweighted, undirected -
        consistent with how BFS treats relationships elsewhere in the
        engine) over every entity active on on_date, with each source's BFS
        bounded by max_depth. A global computation: this processes the
        whole graph in one pass regardless of which entity you actually
        care about - call this directly rather than betweenness() in a loop
        if you need more than one entity's score, since betweenness() would
        recompute the whole graph on every call."""
        _validate_date("on_date", on_date)
        _validate_max_depth(max_depth)
        with self._store.txn() as t:
            nodes = self._active_entity_ids(t, on_date)
            betweenness = {v: 0.0 for v in nodes}
            for s in nodes:
                stack = []
                predecessors = {v: [] for v in nodes}
                sigma = {v: 0 for v in nodes}
                sigma[s] = 1
                dist = {v: -1 for v in nodes}
                dist[s] = 0
                queue = deque([s])
                while queue:
                    v = queue.popleft()
                    stack.append(v)
                    if dist[v] >= max_depth:
                        continue
                    for w in self._neighbor_node_ids(t, v, on_date):
                        if dist[w] < 0:
                            dist[w] = dist[v] + 1
                            queue.append(w)
                        if dist[w] == dist[v] + 1:
                            sigma[w] += sigma[v]
                            predecessors[w].append(v)
                delta = {v: 0.0 for v in nodes}
                while stack:
                    w = stack.pop()
                    for v in predecessors[w]:
                        delta[v] += (sigma[v] / sigma[w]) * (1 + delta[w])
                    if w != s:
                        betweenness[w] += delta[w]
        # Each unordered pair's shortest path is counted from both ends -
        # halve to correct the undirected double-count.
        return {k: v / 2.0 for k, v in betweenness.items()}

    def betweenness(self, entity_id: str, on_date: str, max_depth: int = 4) -> float:
        """One entity's betweenness centrality. See betweenness_all()'s
        docstring: this still computes the whole graph internally, so
        prefer betweenness_all() directly if you need more than one
        entity's score."""
        _validate_identifier("entity_id", entity_id)
        return self.betweenness_all(on_date, max_depth=max_depth).get(entity_id, 0.0)

    def _out_neighbors(self, t, entity_id: str, on_date: str) -> list[str]:
        """Distinct object_ids of relationships where entity_id is the
        subject, active on on_date - the directed out-edges PageRank
        follows (unlike closeness/betweenness, PageRank's link-following
        semantics are inherently directional, not undirected)."""
        objects = {v.object_id for v in self._scan_candidates(t, entity_id, None, None)
                   if v.valid_from <= on_date and (v.valid_to is None or v.valid_to >= on_date)}
        objects.discard(entity_id)
        return list(objects)

    def pagerank_all(self, on_date: str, damping: float = 0.85, max_iterations: int = 100,
                      tol: float = 1e-6) -> dict[str, float]:
        """Standard power-iteration PageRank over every entity active on
        on_date, following directed out-edges (subject -> object). Dangling
        nodes (no out-edges) redistribute their rank uniformly, standard
        PageRank handling. A global computation like betweenness_all - call
        this directly rather than pagerank() in a loop if you need more
        than one entity's score."""
        _validate_date("on_date", on_date)
        with self._store.txn() as t:
            nodes = list(self._active_entity_ids(t, on_date))
            out_edges = {v: self._out_neighbors(t, v, on_date) for v in nodes}
        n = len(nodes)
        if n == 0:
            return {}
        rank = {v: 1.0 / n for v in nodes}
        for _ in range(max_iterations):
            new_rank = {v: (1.0 - damping) / n for v in nodes}
            for v in nodes:
                out = out_edges[v]
                if not out:
                    share = damping * rank[v] / n
                    for u in nodes:
                        new_rank[u] += share
                else:
                    share = damping * rank[v] / len(out)
                    for u in out:
                        new_rank[u] += share
            diff = sum(abs(new_rank[v] - rank[v]) for v in nodes)
            rank = new_rank
            if diff < tol:
                break
        return rank

    def pagerank(self, entity_id: str, on_date: str, damping: float = 0.85,
                 max_iterations: int = 100) -> float:
        """One entity's PageRank. See pagerank_all()'s docstring: this
        still computes the whole graph internally, so prefer pagerank_all()
        directly if you need more than one entity's score."""
        _validate_identifier("entity_id", entity_id)
        return self.pagerank_all(on_date, damping=damping, max_iterations=max_iterations).get(entity_id, 0.0)

    # ------------------------------------------------------------------
    # Provenance - the data model has captured this since M2
    # (RelationshipVersion.assertion_ids, Assertion.source_id/event_time/
    # published_at/ingested_at) but never had a query surface to walk it
    # until now.
    # ------------------------------------------------------------------

    def assertions_for_version(self, version_id: int) -> list[Assertion]:
        """Every Assertion backing one specific RelationshipVersion, in
        ingested_at order."""
        with self._store.txn() as t:
            version = self._load_version(t, version_id)
            assertions = []
            for aid in version.assertion_ids:
                raw = t.get("assertions", K.encode_id(aid))
                if raw:
                    assertions.append(Assertion.from_dict(json.loads(raw)))
        assertions.sort(key=lambda a: a.ingested_at)
        return assertions

    def why_changed(self, subject: str, predicate: str, object_id: str,
                     date_from: str, date_to: str) -> dict:
        """Traces what changed about one relationship between two dates and
        which assertions (evidence) are responsible: the interval-level
        status (from diff(), scoped to this exact triple) plus every
        assertion attached to any of this triple's versions whose
        ingested_at falls in [date_from, date_to], each resolved to its
        source - the evidence trail behind the status."""
        _validate_identifier("subject", subject)
        _validate_identifier("predicate", predicate)
        _validate_identifier("object_id", object_id)
        _validate_date("date_from", date_from)
        _validate_date("date_to", date_to)

        pattern = (subject, predicate, object_id)
        result = self.diff(date_from, date_to, pattern)
        opened, closed, persisted = result["opened"], result["closed"], result["persisted"]
        if opened and closed:
            status = "churned"
        elif opened:
            status = "opened"
        elif closed:
            status = "closed"
        elif persisted:
            status = "persisted"
        else:
            status = "no_relationship"

        assertions = []
        for v in self.history(pattern):
            assertions.extend(self.assertions_for_version(v.version_id))
        # Filter by event_time (valid time - the date each assertion's claim
        # pertains to, e.g. its valid_from/as_of_date at creation), not
        # ingested_at (system time - when it was recorded): status above is
        # a valid-time classification via diff(), so the evidence trail
        # needs to align with that, not with when the system happened to
        # learn about it. Assertions without an event_time (defensive -
        # every current call site always sets it) are excluded, since
        # there's nothing to judge "in window" against.
        assertions = [a for a in assertions if a.event_time is not None and date_from <= a.event_time <= date_to]
        assertions.sort(key=lambda a: a.ingested_at)

        with self._store.txn() as t:
            provenance = []
            for a in assertions:
                source = self._load_source(t, a.source_id) if a.source_id is not None else None
                provenance.append({"assertion": a.to_dict(), "source": source.to_dict() if source else None})

        return {
            "subject_id": subject, "predicate": predicate, "object_id": object_id,
            "date_from": date_from, "date_to": date_to, "status": status,
            "versions": {
                "opened": [v.to_dict() for v in opened],
                "closed": [v.to_dict() for v in closed],
                "persisted": [v.to_dict() for v in persisted],
            },
            "assertions": provenance,
        }

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
