"""Core data types.

Four cooperating concepts, not one flat temporal-edge record:

- Entity: stable identity for a node.
- Relationship: stable identity for a (subject, predicate, object) triple -
  permanent once created, even as the relationship opens/closes/reopens.
- RelationshipVersion: one interval of that relationship, bitemporal on two
  independent axes - valid time (valid_from/valid_to, when true in the
  world) and system time (system_from/system_to, when this database
  recorded or closed it).
- Assertion: one piece of evidence backing a RelationshipVersion. A version's
  confidence/weight is derived from its assertions rather than being a
  single scalar some caller silently overwrites.
"""
from __future__ import annotations

from dataclasses import dataclass, field, asdict


@dataclass
class Entity:
    entity_id: str
    type: str | None = None
    attrs: dict = field(default_factory=dict)
    first_seen: str | None = None
    last_seen: str | None = None

    def to_dict(self) -> dict:
        return asdict(self)

    @classmethod
    def from_dict(cls, d: dict) -> "Entity":
        return cls(**d)


@dataclass
class Relationship:
    relationship_id: int
    subject_id: str
    predicate: str
    object_id: str
    created_at: str  # ISO datetime, when this triple was first asserted

    def to_dict(self) -> dict:
        return asdict(self)

    @classmethod
    def from_dict(cls, d: dict) -> "Relationship":
        return cls(**d)


@dataclass
class RelationshipVersion:
    version_id: int
    relationship_id: int
    subject_id: str
    predicate: str
    object_id: str
    valid_from: str                 # "YYYY-MM-DD"
    valid_to: str | None            # None means still open
    system_from: str                # ISO datetime - when this version was recorded
    system_to: str | None           # ISO datetime - when it was superseded/closed
    last_confirmed: str             # "YYYY-MM-DD", last date this version was reconfirmed true
    confidence: float | None = None
    properties: dict = field(default_factory=dict)
    assertion_ids: list[int] = field(default_factory=list)

    def is_open(self) -> bool:
        return self.valid_to is None

    def to_dict(self) -> dict:
        return asdict(self)

    @classmethod
    def from_dict(cls, d: dict) -> "RelationshipVersion":
        return cls(**d)


@dataclass
class Assertion:
    assertion_id: int
    relationship_id: int
    version_id: int
    source_id: int | None
    event_time: str | None          # when the underlying event happened in the world, if known
    published_at: str | None        # when the source document was published, if known
    ingested_at: str                # ISO datetime - when this system recorded the assertion
    polarity: int = 1               # +1 supports the relationship, -1 contradicts/retracts it
    confidence: float | None = None
    payload: dict = field(default_factory=dict)

    def to_dict(self) -> dict:
        return asdict(self)

    @classmethod
    def from_dict(cls, d: dict) -> "Assertion":
        return cls(**d)


@dataclass
class Source:
    source_id: int
    url: str
    title: str | None = None
    domain: str | None = None

    def to_dict(self) -> dict:
        return asdict(self)

    @classmethod
    def from_dict(cls, d: dict) -> "Source":
        return cls(**d)
