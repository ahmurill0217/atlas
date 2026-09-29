"""Load and consistency-check an ontology version directory.

Loading is deterministic: the same files always produce the same Ontology
(including its checksum), and any inconsistency fails loudly at load time
instead of surfacing as odd graph writes later.
"""

from __future__ import annotations

import hashlib
import re
from functools import lru_cache
from pathlib import Path

import yaml

from atlas.ontology.models import (
    WILDCARD,
    EntityTypeDef,
    Ontology,
    Policies,
    RelationDef,
    RelationMappingRule,
    RoleDef,
    TypeAlias,
)

MODULE_FILES = ("core.yaml", "business.yaml")  # load order; later versions may add modules
SUPPORT_FILES = ("aliases.yaml", "mappings.yaml", "validation.yaml")


class OntologyError(ValueError):
    def __init__(self, problems: list[str]):
        self.problems = problems
        super().__init__("invalid ontology:\n  - " + "\n  - ".join(problems))


def normalize_label(text: str) -> str:
    """'Customer_Company' / 'customer-company ' -> 'customer company'."""
    spaced = re.sub(r"([a-z0-9])([A-Z])", r"\1 \2", text.strip())
    return " ".join(re.split(r"[\s_\-]+", spaced.lower())).strip()


def to_relation_key(text: str) -> str:
    """'owns project' / 'ownsProject' / 'OWNS_PROJECT' -> 'OWNS_PROJECT'."""
    return normalize_label(text).upper().replace(" ", "_")


def _read(path: Path) -> dict:
    with path.open(encoding="utf-8") as fh:
        return yaml.safe_load(fh) or {}


def checksum_of(directory: Path) -> str:
    digest = hashlib.sha256()
    for name in sorted(p.name for p in directory.glob("*.yaml")):
        digest.update(name.encode())
        digest.update((directory / name).read_bytes())
    return digest.hexdigest()


@lru_cache(maxsize=8)
def load_ontology(directory: str | Path) -> Ontology:
    directory = Path(directory)
    problems: list[str] = []
    docs = {name: _read(directory / name) for name in MODULE_FILES + SUPPORT_FILES if (directory / name).exists()}
    missing = [n for n in MODULE_FILES + SUPPORT_FILES if n not in docs]
    if missing:
        raise OntologyError([f"missing file {n}" for n in missing])

    versions = {str(d.get("version")) for d in docs.values()}
    if len(versions) != 1:
        problems.append(f"files disagree on version: {sorted(versions)}")
    version = versions.pop() if len(versions) == 1 else "invalid"

    entity_types: dict[str, EntityTypeDef] = {}
    roles: dict[str, RoleDef] = {}
    relations: dict[str, RelationDef] = {}
    for name in MODULE_FILES:
        doc, module = docs[name], docs[name].get("module", name.removesuffix(".yaml"))
        for tname, spec in (doc.get("entities") or {}).items():
            if tname in entity_types:
                problems.append(f"entity type {tname} defined twice")
            entity_types[tname] = EntityTypeDef(name=tname, module=module, **_tuples(spec))
        for rname, spec in (doc.get("roles") or {}).items():
            if rname in roles:
                problems.append(f"role {rname} defined twice")
            roles[rname] = RoleDef(name=rname, module=module, **_tuples(spec))
        for rname, spec in (doc.get("relations") or {}).items():
            if rname in relations:
                problems.append(f"relation {rname} defined twice")
            if rname != to_relation_key(rname):
                problems.append(f"relation {rname} must be UPPER_SNAKE_CASE")
            relations[rname] = RelationDef(name=rname, module=module, **_tuples(spec))

    known_types = set(entity_types)

    def check_types(where: str, types: tuple[str, ...]) -> None:
        for t in types:
            if t != WILDCARD and t not in known_types:
                problems.append(f"{where}: unknown entity type {t!r}")

    for t in entity_types.values():
        if t.parent and t.parent not in known_types:
            problems.append(f"entity type {t.name}: unknown parent {t.parent!r}")
        seen, cur = set(), t.name
        while cur and cur in entity_types:
            if cur in seen:
                problems.append(f"entity type {t.name}: parent cycle")
                break
            seen.add(cur)
            cur = entity_types[cur].parent
    for r in roles.values():
        check_types(f"role {r.name}", r.applies_to)
    for rel in relations.values():
        check_types(f"relation {rel.name} source", rel.source)
        check_types(f"relation {rel.name} target", rel.target)
        for side, role in (("source", rel.implies_source_role), ("target", rel.implies_target_role)):
            if role and role not in roles:
                problems.append(f"relation {rel.name}: implies unknown role {role!r}")

    # Aliases: language variation mapped onto existing schema only.
    entity_type_aliases: dict[str, TypeAlias] = {}
    for phrase, spec in (docs["aliases.yaml"].get("entity_types") or {}).items():
        alias = TypeAlias(**spec)
        if alias.type not in known_types:
            problems.append(f"type alias {phrase!r}: unknown type {alias.type!r}")
        if alias.role and (alias.role not in roles or alias.type not in roles[alias.role].applies_to):
            problems.append(f"type alias {phrase!r}: role {alias.role!r} does not apply to {alias.type}")
        entity_type_aliases[normalize_label(phrase)] = alias

    relation_aliases: dict[str, str] = {}
    for canonical, phrases in (docs["aliases.yaml"].get("relations") or {}).items():
        if canonical not in relations:
            problems.append(f"relation aliases: unknown relation {canonical!r}")
        for phrase in phrases:
            key = normalize_label(phrase)
            if key in relation_aliases and relation_aliases[key] != canonical:
                problems.append(f"relation alias {phrase!r} maps to both {relation_aliases[key]} and {canonical}")
            relation_aliases[key] = canonical

    mappings: list[RelationMappingRule] = []
    for spec in docs["mappings.yaml"].get("relation_mappings") or []:
        rule = RelationMappingRule(**_tuples(spec))
        rule = rule.model_copy(update={"suggested": to_relation_key(rule.suggested)})
        check_types(f"mapping {rule.suggested}", rule.source + rule.target)
        rel = relations.get(rule.canonical)
        if rel is None:
            problems.append(f"mapping {rule.suggested}: unknown canonical relation {rule.canonical!r}")
        mappings.append(rule)

    policies = Policies(**{k: v for k, v in docs["validation.yaml"].items() if k != "version"})
    for cls in policies.acceptance:
        if cls not in policies.provenance_classes:
            problems.append(f"acceptance: unknown provenance class {cls!r}")

    if problems:
        raise OntologyError(problems)

    ontology = Ontology(
        version=version,
        checksum=checksum_of(directory),
        entity_types=entity_types,
        roles=roles,
        relations=relations,
        entity_type_aliases=entity_type_aliases,
        relation_aliases=relation_aliases,
        relation_mappings=tuple(mappings),
        policies=policies,
    )
    # Mappings must only ever produce edges the canonical relation allows.
    for rule in mappings:
        for s in rule.source:
            for t in rule.target:
                if not ontology.relation_allows(rule.canonical, s, t):
                    problems.append(f"mapping {rule.suggested} {s}->{t} produces an invalid {rule.canonical} edge")
    if problems:
        raise OntologyError(problems)
    return ontology


def _tuples(spec: dict) -> dict:
    return {k: tuple(v) if isinstance(v, list) else v for k, v in (spec or {}).items()}
