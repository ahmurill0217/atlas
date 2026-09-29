from sqlalchemy import select

from brain.db.models import Entity, ResolutionLog
from brain.extraction.extractor import ExtractionResult
from brain.graph.repository import GraphRepository
from brain.pipeline.build_brain import apply_extraction
from brain.resolution.entity_resolver import Decision, EntityResolver, acceptable_alias
from brain.extraction.models import ExtractedEntity
from tests.conftest import ent, graph, make_chunk


def _apply(session, settings, text, entities, relationships=(), uri="doc.md", index=0):
    chunk = make_chunk(session, text, uri, index)
    return apply_extraction(session, chunk, ExtractionResult(graph(entities, relationships)), None, settings)


def _entity(name, entity_type, aliases=()):
    return ExtractedEntity(**ent("x", name, entity_type, aliases))


def test_exact_normalized_name_match(session, settings):
    repo = GraphRepository(session)
    kras_g12c = repo.create_entity("KRAS G12C", "Mutation", None)
    resolver = EntityResolver(repo, settings)
    d = resolver.resolve(_entity("kras-g12c", "Mutation"), "Mutation")
    assert d.decision is Decision.MATCH_EXISTING and d.entity_id == kras_g12c and d.method == "exact_name"
    # Type disagreement alone does not split a uniquely named entity.
    assert resolver.resolve(_entity("KRAS G12C", "Variant"), "Variant").entity_id == kras_g12c


def test_gene_and_mutation_stay_distinct(session, settings):
    repo = GraphRepository(session)
    repo.create_entity("KRAS G12C", "Mutation", None)
    d = EntityResolver(repo, settings).resolve(_entity("KRAS", "Gene"), "Gene")
    assert d.decision is Decision.CREATE_NEW


def test_alias_resolution_both_directions(session, settings):
    repo = GraphRepository(session)
    soto = repo.create_entity("Sotorasib", "Drug", None)
    repo.add_alias(soto, "AMG 510", "extracted_alias")
    resolver = EntityResolver(repo, settings)
    # existing alias <- extracted name
    d = resolver.resolve(_entity("AMG 510", "Drug"), "Drug")
    assert (d.decision, d.entity_id, d.method) == (Decision.MATCH_EXISTING, soto, "alias")  # name vs existing alias
    # existing canonical name <- extracted alias
    d = resolver.resolve(_entity("Lumakras", "Drug", ["sotorasib"]), "Drug")
    assert d.entity_id == soto
    # spacing-only variant via the fuzzy step's compact key
    d = resolver.resolve(_entity("AMG510", "Compound"), "Compound")
    assert (d.decision, d.entity_id, d.method) == (Decision.MATCH_EXISTING, soto, "fuzzy")


def test_numeric_designations_never_fuzzy_merge(session, settings):
    repo = GraphRepository(session)
    repo.create_entity("CodeBreaK 100", "ClinicalTrial", None)
    d = EntityResolver(repo, settings).resolve(_entity("CodeBreaK 200", "ClinicalTrial"), "ClinicalTrial")
    assert d.decision is Decision.CREATE_NEW


def test_ambiguous_alias_is_not_merged(session, settings):
    repo = GraphRepository(session)
    a = repo.create_entity("Mercury", "Planet", None)
    b = repo.create_entity("Mercury", "ChemicalElement", None)
    d = EntityResolver(repo, settings).resolve(_entity("mercury", "Deity"), "Deity")
    assert d.decision is Decision.AMBIGUOUS and {c.entity_id for c in d.candidates} == {a, b}
    # The type breaks the tie when it matches exactly one candidate.
    assert EntityResolver(repo, settings).resolve(_entity("Mercury", "Planet"), "Planet").entity_id == a


def test_pipeline_flags_ambiguous_entities_and_logs_decisions(session, settings):
    repo = GraphRepository(session)
    repo.create_entity("Mercury", "Planet", None)
    repo.create_entity("Mercury", "ChemicalElement", None)
    _apply(session, settings, "Mercury was a Roman god.", [ent("e1", "Mercury", "Deity")])
    flagged = session.scalars(select(Entity).where(Entity.entity_type == "Deity")).one()
    assert flagged.properties["resolution_status"] == "ambiguous"
    assert len(flagged.properties["ambiguous_with"]) == 2
    logged = session.scalars(select(ResolutionLog).where(ResolutionLog.kind == "entity")).one()
    assert logged.decision == "AMBIGUOUS" and len(logged.details["candidates"]) == 2


def test_aliases_across_chunks_converge_on_one_entity(session, settings):
    _apply(session, settings, "Sotorasib, previously known as AMG 510, is a drug.",
           [ent("e1", "Sotorasib", "Drug", ["AMG 510"])], uri="a.md")
    _apply(session, settings, "The trial evaluated AMG 510.", [ent("e1", "AMG 510", "Compound")], uri="b.md")
    _apply(session, settings, "Amgen markets Lumakras (sotorasib).",
           [ent("e1", "Lumakras", "Drug", ["sotorasib"])], uri="c.md")
    entities = session.scalars(select(Entity)).all()
    assert [e.canonical_name for e in entities] == ["Sotorasib"]
    assert entities[0].entity_type == "Drug"  # majority of type votes
    assert entities[0].properties["type_votes"] == {"Drug": 2, "Compound": 1}


def test_unsafe_aliases_are_rejected():
    assert not acceptable_alias("KRAS", "KRAS G12C")          # token subset
    assert not acceptable_alias("CodeBreaK 200", "CodeBreaK 100")  # different number
    assert acceptable_alias("AMG 510", "Sotorasib")
    assert acceptable_alias("NSCLC", "non-small cell lung cancer")


def test_name_hit_beats_conflicting_alias(session, settings):
    """'Like Lumakras, Krazati binds...' -> LLM wrongly lists Lumakras as an alias of Krazati."""
    repo = GraphRepository(session)
    soto = repo.create_entity("Sotorasib", "Drug", None)
    repo.add_alias(soto, "Lumakras", "extracted_alias")
    ada = repo.create_entity("adagrasib", "Drug", None)
    repo.add_alias(ada, "Krazati", "extracted_alias")
    d = EntityResolver(repo, settings).resolve(_entity("Krazati", "Drug", ["Lumakras"]), "Drug")
    assert (d.decision, d.entity_id) == (Decision.MATCH_EXISTING, ada)
    # With no name hit, two conflicting alias hits stay ambiguous.
    d = EntityResolver(repo, settings).resolve(_entity("Drug X", "Drug", ["Lumakras", "Krazati"]), "Drug")
    assert d.decision is Decision.AMBIGUOUS


def test_alias_guard_rejects_sizes_citations_and_plural_supersets():
    assert not acceptable_alias("137B", "LaMDA")
    assert not acceptable_alias("540 B", "LaMDA")
    assert not acceptable_alias("Rajpurkar et al., 2016", "SQuAD")
    assert not acceptable_alias("Universal Transformers", "Transformer")
    assert acceptable_alias("Transformers", "Transformer") is True  # same name, plural
    assert acceptable_alias("T5", "text-to-text transfer transformer")
    assert acceptable_alias("NQ", "Natural Questions")
