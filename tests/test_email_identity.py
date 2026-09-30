"""Email identity and employment rules: bulk senders, mailboxes, names, address patterns."""

import uuid

import pytest

from atlas.extraction.structured import StructuredExtractor
from atlas.ingestion.adapters.email_json import EmailJsonAdapter
from tests.conftest import ontology_variant


def _email(sender: str, to: list[str]):
    return EmailJsonAdapter().normalize(None, {"source_system": "test", "message_id": uuid.uuid4().hex,
                                               "from": sender, "to": to, "body": "hi"})


@pytest.mark.parametrize("address", [
    "noreply@acme.com", "info@iwon.com", "newsletter@ftenergy.com", "owner-energy@lists.thebiz.net",
    "ferc-request@acme.com", "alerts@info.iwon.com", "list@mailman.enron.com", "anyone@lists.acme.com"])
def test_bulk_senders_get_no_employment(ontology, address):
    cs = StructuredExtractor(ontology, ["enron.com"]).extract(_email(address, ["jeff.skilling@enron.com"]),
                                                                   uuid.uuid4())
    works_at = {(e.source_local_id, e.target_local_id) for e in cs.edges if e.suggested_relation == "WORKS_AT"}
    assert works_at == {("person:email:jeff.skilling@enron.com", "org:domain:enron.com")}
    assert f"person:email:{address}" in {e.local_id for e in cs.entities}         # sender still resolved
    assert any(e.suggested_relation == "AUTHORED_BY" for e in cs.edges)             # authorship kept


def test_consumer_provider_subdomains_are_generic(ontology):
    cs = StructuredExtractor(ontology).extract(_email("brian@email.msn.com", ["pat@mail.yahoo.com"]),
                                                   uuid.uuid4())
    assert not [e for e in cs.edges if e.suggested_relation == "WORKS_AT"]


@pytest.mark.parametrize("address", ["sarah.chen@acme.com", "information.desk@acme.com", "news.anchor@cnn.com",
                                     "mike@mail.northwind.io", "dana@eng.acme.com"])
def test_people_still_get_employment(ontology, address):
    cs = StructuredExtractor(ontology).extract(_email(address, []), uuid.uuid4())
    assert [e.suggested_relation for e in cs.edges if e.suggested_relation == "WORKS_AT"] == ["WORKS_AT"]


def test_policy_lives_in_the_ontology(ontology, tmp_path):
    def without_bulk_rules(docs):
        del docs["validation.yaml"]["email_domain_employment"]["bulk_senders"]

    plain = ontology_variant(tmp_path, "0.9", without_bulk_rules)
    cs = StructuredExtractor(plain).extract(_email("noreply@acme.com", []), uuid.uuid4())
    assert any(e.suggested_relation == "WORKS_AT" for e in cs.edges)    # the rule comes from the policy, not code
    cs = StructuredExtractor(ontology).extract(_email("noreply@acme.com", []), uuid.uuid4())
    assert not any(e.suggested_relation == "WORKS_AT" for e in cs.edges)


# --- email identity: names, mailboxes, organizations -------------------------------------

def _person(cs, email):
    return next(e for e in cs.entities if e.local_id == f"person:email:{email}")


def test_names_from_address_and_display(ontology):
    x = StructuredExtractor(ontology)
    cs = x.extract(_email("phillip.allen@enron.com", ['"Allen, Phillip K." <pk@acme.com>', "pallen@enron.com",
                                                      "k..allen@enron.com"]), uuid.uuid4())
    assert (_person(cs, "phillip.allen@enron.com").name, _person(cs, "phillip.allen@enron.com").properties) == \
        ("Phillip Allen", {"first_name": "Phillip", "last_name": "Allen"})
    assert _person(cs, "pk@acme.com").name == "Phillip Allen"                 # display name wins
    for handle in ("pallen@enron.com", "k..allen@enron.com"):                  # handles are not names
        assert _person(cs, handle).name == handle.split("@")[0] and _person(cs, handle).properties == {}


def test_mailbox_and_organization_use_registrable_domain(ontology):
    cs = StructuredExtractor(ontology, ["enron.com"]).extract(
        _email("pallen@ect.enron.com", ["ann@world.bbc.co.uk", "joe@houston.rr.com"]), uuid.uuid4())
    assert _person(cs, "pallen@ect.enron.com").identifiers == {"email": "pallen@ect.enron.com",
                                                               "mailbox": "pallen@enron.com"}
    assert "mailbox" not in _person(cs, "joe@houston.rr.com").identifiers       # consumer ISP
    orgs = {e.local_id: e.properties for e in cs.entities if e.suggested_type == "Organization"}
    assert orgs == {"org:domain:enron.com": {"domain": "enron.com", "is_internal": True},
                    "org:domain:bbc.co.uk": {"domain": "bbc.co.uk", "is_internal": False}}
    works = next(e for e in cs.edges if e.local_id.startswith("person:email:pallen") and e.suggested_relation == "WORKS_AT")
    assert works.properties == {"email_domain": "ect.enron.com"}


def _ingest(kg, settings, ontology, tmp_path, messages):
    import json

    from atlas.pipeline import KnowledgeIngestionPipeline
    for i, (sender, to) in enumerate(messages):
        (tmp_path / f"{i:02d}.json").write_text(json.dumps(
            {"source_system": "test", "message_id": f"m{i}-{uuid.uuid4().hex}", "from": sender, "to": to,
             "subject": "s", "body": "b"}))
    report = KnowledgeIngestionPipeline(kg, settings, ontology).ingest(tmp_path)
    assert "documents_failed" not in report.stats
    return report


def _people(kg):
    from sqlalchemy import text
    with kg.connect() as c:
        rows = c.execute(text("""SELECT e.canonical_name, array_agg(x.value ORDER BY x.value) AS emails
            FROM kg.entities e JOIN kg.entity_external_ids x ON x.entity_id = e.id AND x.identifier_type = 'email'
            WHERE e.entity_type = 'Person' GROUP BY e.id, e.canonical_name"""))
        return {r.canonical_name: r.emails for r in rows}


@pytest.mark.parametrize("order", [1, -1])
def test_one_person_across_addresses(kg, atlas_settings, ontology, tmp_path, order):
    """flast handle and sub-domain variants fold into the first.last person, in either order."""
    msgs = [("phillip.allen@enron.com", ["tim.belden@enron.com"]), ("pallen@ect.enron.com", ["tim.belden@enron.com"]),
            ("pallen@enron.com", ["jadams@acme.com", "john.adams@acme.com"])][::order]
    _ingest(kg, atlas_settings, ontology, tmp_path, msgs)
    people = _people(kg)
    assert people["Phillip Allen"] == ["pallen@ect.enron.com", "pallen@enron.com", "phillip.allen@enron.com"]
    assert people["John Adams"] == ["jadams@acme.com", "john.adams@acme.com"]
    assert set(people) == {"Phillip Allen", "Tim Belden", "John Adams"}
    from sqlalchemy import text
    with kg.connect() as c:
        assert c.execute(text("SELECT count(*) FROM kg.audit_log WHERE action = 'identity_linked'")).scalar() == 2
        assert c.execute(text("SELECT count(*) FROM kg.review_items")).scalar() == 0


def test_ambiguous_handle_is_never_linked(kg, atlas_settings, ontology, tmp_path):
    _ingest(kg, atlas_settings, ontology, tmp_path, [
        ("phillip.allen@enron.com", ["paul.allen@enron.com"]), ("pallen@enron.com", ["tim.belden@enron.com"])])
    people = _people(kg)
    assert people["pallen"] == ["pallen@enron.com"]                             # two people fit: kept apart
    from sqlalchemy import text
    with kg.connect() as c:
        assert c.execute(text("SELECT count(*) FROM kg.review_items WHERE review_type = 'POSSIBLE_DUPLICATE'")).scalar() == 2


def test_display_name_contradiction_blocks_link(kg, atlas_settings, ontology, tmp_path):
    _ingest(kg, atlas_settings, ontology, tmp_path, [
        ("phillip.allen@enron.com", ["tim.belden@enron.com"]), ("Paul Allen <pallen@enron.com>", ["tim.belden@enron.com"])])
    people = _people(kg)
    assert people["Paul Allen"] == ["pallen@enron.com"] and people["Phillip Allen"] == ["phillip.allen@enron.com"]


def test_list_addresses_get_no_name_and_no_mailbox(ontology):
    cs = StructuredExtractor(ontology).extract(_email("owner-newsletter@lists.houstonpress.com", []), uuid.uuid4())
    p = _person(cs, "owner-newsletter@lists.houstonpress.com")
    assert p.name == "owner-newsletter" and p.properties == {} and "mailbox" not in p.identifiers


def test_handle_that_is_a_first_name_never_links(kg, atlas_settings, ontology, tmp_path):
    settings = atlas_settings.model_copy(update={"internal_domains": ["enron.com"]})
    _ingest(kg, settings, ontology, tmp_path, [
        ("home.john@enron.com", ["john.arnold@enron.com"]), ("john@enron.com", ["tim.belden@enron.com"])])
    assert _people(kg)["john"] == ["john@enron.com"]


def test_lastname_handles_link_only_inside_the_company(kg, atlas_settings, ontology, tmp_path):
    settings = atlas_settings.model_copy(update={"internal_domains": ["enron.com"]})
    _ingest(kg, settings, ontology, tmp_path, [
        ("john.lavorato@enron.com", ["steve.lafontaine@bankofamerica.com"]),
        ("lavorato@enron.com", ["lafontaine@bankofamerica.com"])])
    people = _people(kg)
    assert people["John Lavorato"] == ["john.lavorato@enron.com", "lavorato@enron.com"]     # internal: linked
    assert people["lafontaine"] == ["lafontaine@bankofamerica.com"]                         # external: kept apart


# --- provider mailboxes and role addresses (policy 1.1) -------------------------------------

@pytest.mark.parametrize("address, mailbox", [
    ("kassondra.cisneroz@gmail.com", "kassondracisneroz@gmail.com"),
    ("Kassondra.Cisneroz+bank@googlemail.com", "kassondracisneroz@gmail.com"),
    ("kassondracisneroz@gmail.com", "kassondracisneroz@gmail.com")])
def test_gmail_spellings_share_one_mailbox(ontology, address, mailbox):
    cs = StructuredExtractor(ontology).extract(_email(address, []), uuid.uuid4())
    assert _person(cs, address.lower()).identifiers["mailbox"] == mailbox


def test_other_consumer_providers_keep_dots(ontology):
    cs = StructuredExtractor(ontology).extract(_email("sam.lee@yahoo.com", []), uuid.uuid4())
    assert "mailbox" not in _person(cs, "sam.lee@yahoo.com").identifiers


def test_gmail_spellings_resolve_to_one_person(kg, atlas_settings, tmp_path):
    import json

    from sqlalchemy import text
    from sqlalchemy.orm import Session

    from atlas.ontology import load_ontology
    from atlas.pipeline import KnowledgeIngestionPipeline
    from tests.conftest import ROOT

    for i, sender in enumerate(["Kassy C <kassondra.cisneroz@gmail.com>", "kassondracisneroz@gmail.com"]):
        (tmp_path / f"m{i}.json").write_text(json.dumps({"source_system": "test", "message_id": f"g{i}",
                                                         "from": sender, "to": ["me@example.org"],
                                                         "date": f"2026-01-0{i + 1}T10:00:00Z", "body": "hi"}))
    KnowledgeIngestionPipeline(kg, atlas_settings, load_ontology(str(ROOT / "ontology"))).ingest(tmp_path)
    with Session(kg) as s:
        owners = s.execute(text("""SELECT DISTINCT entity_id FROM kg.entity_external_ids
                                   WHERE value IN ('kassondra.cisneroz@gmail.com', 'kassondracisneroz@gmail.com')""")).all()
    assert len(owners) == 1


def test_role_addresses_are_named_by_address(ontology):
    cs = StructuredExtractor(ontology).extract(
        _email("Kassondra Cisneroz <receptionist@williamacohan.com>", ["Charlie Murillo <info@garagedoor.com>"]),
        uuid.uuid4())
    front = _person(cs, "receptionist@williamacohan.com")
    assert front.name == "receptionist@williamacohan.com" and "Kassondra Cisneroz" in front.aliases
    assert not front.properties.get("first_name")                  # no person's name on a shared address
    assert _person(cs, "info@garagedoor.com").name == "info@garagedoor.com"
