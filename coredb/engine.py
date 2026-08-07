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
from .series import GraphDelta, GraphSeries
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
                    t.put("closed_time_idx", K.time_key(version.last_confirmed, vid), K.encode_id(vid))
                    version.valid_to = version.last_confirmed
                    version.system_to = now_iso
                    self._store_version(t, version)
                    closed.append(vid)

        return {"opened": opened, "closed": closed, "confirmed": confirmed}

    def _open_objects_for(self, t, subject: str, predicate: str) -> dict:
        """object_id -> (relationship_id, version_id) for every currently-open
        version under (subject, predicate) - scans spo_idx's open intervals
        via the relationships they belong to. Used by sync_snapshot to find
        objects that need closing."""
        result = {}
        prefix = K.spo_prefix(subject, predicate)
        seen_relationships = set()
        for _, val in t.range_iter("spo_idx", prefix, K.prefix_upper_bound(prefix)):
            version = self._load_version(t, K.decode_id(val))
            if version.relationship_id in seen_relationships:
                continue
            seen_relationships.add(version.relationship_id)
            open_raw = t.get("open_idx", K.encode_id(version.relationship_id))
            if open_raw:
                open_vid = K.decode_id(open_raw)
                open_version = self._load_version(t, open_vid) if open_vid != version.version_id else version
                result[open_version.object_id] = (version.relationship_id, open_vid)
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
        (inclusive), aggregated by object_id. Intended for patterns with a
        bound subject (aggregate over objects); an entity can have several
        disjoint intervals in the window, whose day counts are summed."""
        subject, predicate, obj = pattern
        with self._store.txn() as t:
            candidates = [v for v in self._scan_candidates(t, subject, predicate, obj)
                          if v.valid_from <= end and (v.valid_to is None or v.valid_to >= start)]
        start_dt, end_dt = _parse_date(start), _parse_date(end)
        day_counts: dict[str, int] = {}
        for v in candidates:
            overlap_from = max(start_dt, _parse_date(v.valid_from))
            overlap_to = min(end_dt, _parse_date(v.valid_to) if v.valid_to else end_dt)
            days = max(0, (overlap_to - overlap_from).days + 1)
            day_counts[v.object_id] = day_counts.get(v.object_id, 0) + days
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
