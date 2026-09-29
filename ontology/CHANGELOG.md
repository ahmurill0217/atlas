# Ontology changelog

The ontology is versioned. Its files are checksummed when a graph first uses a
version, and the pipeline refuses to run if they change afterwards: bump the
`version:` in every file instead. A new version closes the review items it now
covers (`AUTO_RESOLVED`, audited), and graph objects keep the version they were
created under.

## 1.0 (2026-09-29)

The local-development versions 1.0–1.3 were squashed into this one (see git
history before commit "Squash ontology versions"). Contents:

- **Core:** entity types Person, Organization, Document, Project, Event, Location; relations
  WORKS_AT, MEMBER_OF, MANAGES, REPORTS_TO, ATTENDED, HAS_PARTICIPANT, ORGANIZED_BY,
  AUTHORED_BY, SENT_TO (`recipient_type`), MENTIONS, RELATED_TO, ASSOCIATED_WITH.
- **Business:** entity types Team, Meeting, Product, Contract, Initiative, Opportunity, ActionItem;
  relations WORKS_ON, OWNS_PROJECT, ABOUT, CUSTOMER_OF, VENDOR_OF, PART_OF, BETWEEN,
  USES_PRODUCT, ASSIGNED_TO, ORIGINATED_IN. Customer / Vendor are roles on Organization.
- **Policy (validation.yaml):**
  - provenance classes and acceptance thresholds;
  - structured-source trust per field;
  - employment by email domain, with generic domains and bulk / mailing-list
    senders excluded;
  - email identity: the `mailbox` identifier, organizations by registrable
    domain, names from addresses, address-pattern linking.
- **Governance decision:** Drive owner / editor roles are document metadata,
  not edges.
