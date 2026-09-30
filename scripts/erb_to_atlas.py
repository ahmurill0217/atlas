"""EnterpriseRAG-Bench (Onyx) Gmail, Google Drive and HubSpot -> Atlas input JSON.

The benchmark: github.com/onyx-dot-app/EnterpriseRAG-Bench. Its released text files keep only
title and content; the original JSON under generated_data/sources/ keeps the metadata (senders,
owners, stages, dates) and the benchmark id (`dataset_doc_uuid`, the dsid the questions cite).

    git clone --filter=blob:none --no-checkout --depth 1 \\
        https://github.com/onyx-dot-app/EnterpriseRAG-Bench.git runs/erb/repo
    (cd runs/erb/repo && git sparse-checkout set --no-cone /generated_data/sources/gmail/ \\
        /generated_data/sources/hubspot/ /generated_data/sources/google_drive/ /generated_data/employee_directory.yaml \\
        /questions.jsonl /extra_questions.jsonl && git checkout main)

    # a subset: every gold document, the documents most like each question, random fill
    uv run --with scikit-learn python scripts/erb_to_atlas.py select runs/erb/repo runs/erb/subset.json
    uv run python scripts/erb_to_atlas.py convert runs/erb/repo runs/erb/subset.json runs/erb/json
    uv run python scripts/erb_to_atlas.py questions runs/erb/repo runs/erb/questions.jsonl

- Gmail: one email JSON per message, split from the thread's header blocks (From/To/Cc/Date/
  Subject/Attachments). thread_id is the dsid. Internal addresses come in many spellings
  (first_last@redwood.ai, olga@redwood.com, ...); each is rewritten to the person's address in
  the employee directory when the name or handle identifies one person, so one person is one
  entity. A thread whose messages can't be split becomes one document with its participants.
- Drive: an Atlas document; the owner is the author (with their directory address), the path,
  team, status, dates and links head the text.
- HubSpot: an Atlas document (source_type crm_company); every CRM field heads the text. The
  owner, SE and CSM are participants kept as metadata; the ontology has no CRM account yet.
"""

from __future__ import annotations

import json
import random
import re
from datetime import datetime, timedelta, timezone
from email.utils import getaddresses, parsedate_to_datetime
from pathlib import Path

import typer
import yaml

app = typer.Typer(add_completion=False)

SOURCES = ("gmail", "google_drive", "hubspot")
DOMAIN = "redwoodinference.com"
_TZ = {"PT": -7, "PST": -8, "PDT": -7, "MT": -6, "MST": -7, "MDT": -6, "CT": -5, "CST": -6, "CDT": -5,
       "ET": -4, "EST": -5, "EDT": -4, "UTC": 0, "GMT": 0, "Z": 0, "BST": 1, "CET": 1, "CEST": 2,
       "IST": 5.5, "SGT": 8, "JST": 9, "AEST": 10}
_HEADER = re.compile(r"^(From|To|Cc|CC|Bcc|BCC|Date|Sent|Subject|Attachments?):[ \t]*(.*)$")
_BLOCK_START = re.compile(r"^From:[^\n]*\n(?:(?:To|Cc|CC|Bcc|Date|Sent|Subject):[^\n]*\n)+", re.MULTILINE)


# --- reading the benchmark -------------------------------------------------------

def _files(repo: Path, source: str):
    yield from sorted((repo / "generated_data" / "sources" / source).rglob("*.json"))


def _unescape(text: str) -> str:
    if "\\n" in text and text.count("\\n") > text.count("\n"):
        text = text.replace("\\r\\n", "\n").replace("\\n", "\n").replace("\\t", "\t").replace('\\"', '"')
    return text


def _render(value, depth: int = 0) -> str:
    if value is None:
        return ""
    if isinstance(value, str):
        return _unescape(value).strip()
    if isinstance(value, list):
        items = [_render(v, depth + 1) for v in value]
        return "\n".join(f"- {i}" if "\n" not in i else i for i in items if i)
    if isinstance(value, dict):
        return "\n".join(f"{k}: {_render(v, depth + 1)}" for k, v in value.items() if _render(v, depth + 1))
    return str(value)


def content(raw: dict) -> str:
    """The document's text: its content fields in order, labelled when there are several."""
    fields = [f for f in raw.get("content_field_names") or [] if raw.get(f) not in (None, "", [])]
    if len(fields) == 1:
        return _render(raw[fields[0]])
    return "\n\n".join(f"{f.replace('_', ' ').capitalize()}:\n{_render(raw[f])}" for f in fields)


def title(raw: dict) -> str:
    return _render(raw.get(raw.get("title_field_name") or "title")) or ""


# --- people ------------------------------------------------------------------------

def _key(name: str) -> str:
    return re.sub(r"[^a-z]", "", (name or "").lower())


class Directory:
    """Employee directory: resolve the many spellings of an internal address to one person."""

    def __init__(self, path: Path):
        data = yaml.safe_load(path.read_text())
        self.people = [p for dept in data["departments"].values() for p in dept]
        self.by_name = {_key(p["name"]): p for p in self.people}
        firsts: dict[str, list] = {}
        for p in self.people:
            firsts.setdefault(_key(p["name"].split()[0]), []).append(p)
        self.by_first = firsts

    def person(self, name: str | None, handle: str | None = None, within: list[str] | None = None) -> dict | None:
        if name and _key(name) in self.by_name:
            return self.by_name[_key(name)]
        if handle:
            h = handle.lower()
            if _key(h) in self.by_name:                                # first_last, first.last, firstlast
                return self.by_name[_key(h)]
            options = self.by_first.get(_key(h), [])
            if within:                                                # a bare first name: someone on the thread
                on_thread = [p for p in options if _key(p["name"]) in {_key(w) for w in within}]
                options = on_thread or options
            if len(options) == 1:
                return options[0]
        return None

    def email(self, name: str | None) -> str | None:
        """An employee's address: the directory's, else first.last@ (Drive and HubSpot name only
        employees; their APIs would give the address)."""
        p = self.person(name)
        if p:
            return p["email"].lower()
        words = [_key(w) for w in (name or "").split()]
        return f"{words[0]}.{words[-1]}@{DOMAIN}" if len(words) >= 2 and all(words) else None


def _internal(domain: str) -> bool:
    return "redwood" in domain


def canonical(directory: Directory, name: str | None, addr: str | None, within: list[str]) -> str | None:
    """"Name <address>" with internal people at their directory address."""
    name, addr = (name or "").strip().strip('"'), (addr or "").strip().strip(".").lower()
    if "@" not in addr:
        if not addr and not name:
            return None
        p = directory.person(name or addr, None, within)
        return f"{p['name']} <{p['email'].lower()}>" if p else (name or addr)
    local, domain = addr.rsplit("@", 1)
    if _internal(domain):
        p = directory.person(name, local, within)
        if p:
            return f"{p['name']} <{p['email'].lower()}>"
        if re.fullmatch(r"[a-z]+[._][a-z]+", local):                   # not in the directory: one spelling
            local = local.replace("_", ".")
        elif len(name.split()) == 2 and "@" not in name:
            local = _key(name.split()[0]) + "." + _key(name.split()[1])
        addr = f"{local}@{DOMAIN}"
    return f"{name} <{addr}>" if name else addr


def _addresses(directory: Directory, value: str, within: list[str]) -> list[str]:
    out = []
    for name, addr in getaddresses([value.replace(";", ",")]):
        a = canonical(directory, name, addr, within)
        if a and a not in out:
            out.append(a)
    return out


# --- dates -------------------------------------------------------------------------

def parse_date(value: str | None) -> datetime | None:
    if not value:
        return None
    s = str(value).strip()
    try:
        d = datetime.fromisoformat(s.replace("Z", "+00:00"))
    except ValueError:
        try:
            d = parsedate_to_datetime(s)
        except (TypeError, ValueError, IndexError):
            from dateutil import parser
            try:
                d = parser.parse(s.replace(" at ", " "), fuzzy=True,
                                 tzinfos={k: int(v * 3600) for k, v in _TZ.items()})
            except (ValueError, OverflowError):
                return None
    return d if d.tzinfo else d.replace(tzinfo=timezone.utc)


def _iso(value) -> str | None:
    d = parse_date(value)
    return d.isoformat() if d else None


# --- Gmail -------------------------------------------------------------------------

def _message_texts(raw: dict) -> list[str]:
    """The thread as one text per message."""
    messages = raw.get("messages")
    if isinstance(messages, list) and messages and all(isinstance(m, str) for m in messages):
        texts = [_unescape(m) for m in messages]
    else:
        texts = [content(raw)]
    out = []
    for t in texts:                                                      # a text holding several header blocks
        starts = [m.start() for m in _BLOCK_START.finditer(t)]
        if len(starts) > 1 and not isinstance(messages, list):
            bounds = starts + [len(t)]
            out += [t[a:b] for a, b in zip(bounds, bounds[1:])]
        else:
            out.append(t)
    return [t for t in out if t.strip()]


def parse_message(text: str) -> tuple[dict, str]:
    lines = text.strip("\n").split("\n")
    headers, i = {}, 0
    while i < len(lines) and (m := _HEADER.match(lines[i].strip())):
        field = m.group(1).lower().replace("sent", "date").rstrip("s")
        field = "attachments" if field.startswith("attachment") else field
        headers[field] = m.group(2).strip()
        i += 1
    return headers, "\n".join(lines[i:]).strip()


def gmail_documents(raw: dict, directory: Directory) -> list[dict]:
    dsid = raw["dataset_doc_uuid"]
    within = raw.get("participants_internal") or []
    thread_attachments = [a for a in raw.get("attachments") or [] if isinstance(a, str)]
    subject = _render(raw.get("subject"))
    parsed = [parse_message(t) for t in _message_texts(raw)]
    parsed = [(h, b) for h, b in parsed if h.get("from")]
    if not parsed:                                                       # no header blocks: one thread document
        owner = directory.person(raw.get("mailbox_owner", "").replace("_", " "))
        people = [{"name": n, "email": directory.email(n), "role": "participant"} for n in within]
        people += [{"name": str(n), "role": "participant"} for n in raw.get("participants_external") or []]
        return [{"atlas_document": "1.0", "source_system": "gmail", "id": dsid, "source_type": "email_thread",
                 "title": subject, "created_at": _iso(raw.get("first_email_at")),
                 "updated_at": _iso(raw.get("last_email_at")), "participants": people,
                 "text": (f"Attachments: {', '.join(thread_attachments)}\n\n" if thread_attachments else "")
                         + content(raw),
                 "metadata": {"erb_dsid": dsid, "thread_id": dsid,
                              "mailbox_owner": owner["name"] if owner else raw.get("mailbox_owner")}}]

    first, last = parse_date(raw.get("first_email_at")), parse_date(raw.get("last_email_at"))
    listed = {a for h, _ in parsed for a in re.split(r",\s*", h.get("attachments", "")) if a}
    out = []
    for i, (h, body) in enumerate(parsed):
        date = parse_date(h.get("date"))
        if date is None and first:                                      # spread undated messages over the thread
            span = (last - first) if last else timedelta(0)
            date = first + span * (i / max(len(parsed) - 1, 1))
        sender = _addresses(directory, h["from"], within)
        attachments = [a for a in re.split(r",\s*", h.get("attachments", "")) if a]
        if i == 0 and not listed:
            attachments = thread_attachments
        body = (f"Attachments: {', '.join(attachments)}\n\n" if attachments else "") + body
        out.append({"source_system": "gmail", "message_id": f"{dsid}#{i + 1}", "thread_id": dsid,
                    "subject": h.get("subject") or subject,
                    "from": sender[0] if sender else h["from"],
                    "to": _addresses(directory, h.get("to", ""), within),
                    "cc": _addresses(directory, h.get("cc", ""), within),
                    "date": date.isoformat() if date else None, "body": body,
                    "attachments": [{"filename": a} for a in attachments]})
    return out


# --- Drive and HubSpot ---------------------------------------------------------------

def _lines(pairs) -> str:
    out = []
    for label, value in pairs:
        v = ", ".join(_render(x) for x in value) if isinstance(value, list) else _render(value)
        if v:
            out.append(f"{label}: {v}")
    return "\n".join(out)


def drive_document(raw: dict, directory: Directory) -> dict:
    dsid, owner = raw["dataset_doc_uuid"], _render(raw.get("owner"))
    head = _lines([("Owner", owner), ("Drive", raw.get("drive_area")), ("Path", raw.get("path")),
                   ("Type", raw.get("doc_type")), ("Team", raw.get("team")), ("Status", raw.get("status")),
                   ("Created", raw.get("created_at")), ("Last modified", raw.get("last_modified")),
                   ("Collaborators", raw.get("collaborators") or []), ("Tags", raw.get("tags") or []),
                   ("Linked", raw.get("linked_artifacts") or [])])
    collaborators = [c for c in raw.get("collaborators") or [] if isinstance(c, str)]
    return {"atlas_document": "1.0", "source_system": "gdrive", "id": dsid, "source_type": raw.get("doc_type") or "doc",
            "title": title(raw), "uri": f"drive://{raw.get('path', '')}",
            "created_at": _iso(raw.get("created_at")), "updated_at": _iso(raw.get("last_modified")),
            "author": {"name": owner, "email": directory.email(owner)} if owner else None,
            "participants": [{"name": c, "email": directory.email(c), "role": "editor"} for c in collaborators],
            "text": f"{head}\n\n{content(raw)}",
            "metadata": {"erb_dsid": dsid, "drive_path": raw.get("path"), "team": raw.get("team"),
                         "status": raw.get("status")}}


HUBSPOT_FIELDS = [("Company", "company_name"), ("Domain", "company_domain"), ("HubSpot id", "company_id"),
                  ("Stage", "stage"), ("Owner", "owner"), ("Solutions engineer", "se_assigned"),
                  ("CSM", "csm_assigned"), ("Tier", "account_tier"), ("Industry", "industry"),
                  ("Employees", "employee_count_range"), ("HQ region", "hq_region"),
                  ("Forecast close month", "forecast_close_month"), ("Estimated ARR", "estimated_arr_range"),
                  ("Created", "created_at"), ("Updated", "updated_at"), ("Last activity", "last_activity_at"),
                  ("Interested products", "interested_products"), ("Use cases", "use_cases"),
                  ("Deployment", "deployment_requirements"), ("Security", "security_requirements"),
                  ("Competitors", "competitors"), ("Next step", "next_step"), ("Blockers", "blockers"),
                  ("Fireflies calls", "linked_fireflies"), ("Gmail threads", "linked_gmail_threads"),
                  ("Drive docs", "linked_drive_docs"), ("Support tickets", "linked_support_tickets")]


def hubspot_document(raw: dict, directory: Directory) -> dict:
    dsid = raw["dataset_doc_uuid"]
    head = _lines([(label, raw.get(key)) for label, key in HUBSPOT_FIELDS])
    shown = {k for _, k in HUBSPOT_FIELDS}
    rest = content({**raw, "content_field_names": [f for f in raw.get("content_field_names") or [] if f not in shown]})
    people = [{"name": _render(raw.get(k)), "email": directory.email(_render(raw.get(k))), "role": role}
              for k, role in (("owner", "owner"), ("se_assigned", "solutions_engineer"), ("csm_assigned", "csm"))
              if _render(raw.get(k))]
    return {"atlas_document": "1.0", "source_system": "hubspot", "id": dsid, "source_type": "crm_company",
            "title": _render(raw.get("company_name")) or title(raw),
            "uri": f"hubspot://company/{raw.get('company_id', dsid)}",
            "created_at": _iso(raw.get("created_at")), "updated_at": _iso(raw.get("updated_at")),
            "participants": people, "text": f"{head}\n\n{rest}".strip(),
            "metadata": {"erb_dsid": dsid, "stage": raw.get("stage"), "company_domain": raw.get("company_domain")}}


# --- commands ------------------------------------------------------------------------

def _questions(repo: Path) -> list[dict]:
    """The questions whose sources are all Gmail, Drive or HubSpot. The extra (metadata) questions
    reuse the main questions' ids (qst_0001...), so theirs get an x prefix."""
    out = []
    for name, prefix in (("questions.jsonl", ""), ("extra_questions.jsonl", "x")):
        for line in (repo / name).read_text().splitlines():
            q = json.loads(line)
            if q["source_types"] and set(q["source_types"]) <= set(SOURCES):
                out.append({**q, "question_id": prefix + q["question_id"]})
    return out


@app.command()
def questions(repo: Path, out: Path):
    """Write the questions for these three sources, in the benchmark's JSONL format."""
    qs = _questions(repo)
    out.write_text("".join(json.dumps(q) + "\n" for q in qs))
    typer.echo(f"{len(qs)} questions")


@app.command()
def select(repo: Path, out: Path, gmail: int = 10000, google_drive: int = 5000, hubspot: int = 5000,
           negatives: int = 20, seed: int = 7):
    """Pick a subset: every gold document, the `negatives` documents most like each question
    (TF-IDF over the whole of each source), then random documents up to the per-source sizes."""
    from sklearn.feature_extraction.text import TfidfVectorizer

    questions = _questions(repo)
    ids, sources, texts = [], [], []
    for source in SOURCES:
        for f in _files(repo, source):
            raw = json.loads(f.read_text())
            ids.append(raw["dataset_doc_uuid"])
            sources.append(source)
            texts.append(f"{title(raw)}\n{content(raw)[:4000]}")
    typer.echo(f"{len(ids)} documents, {len(questions)} questions")
    vectorizer = TfidfVectorizer(sublinear_tf=True, min_df=2, max_df=0.5, stop_words="english")
    matrix = vectorizer.fit_transform(texts)
    scores = matrix @ vectorizer.transform([q["question"] for q in questions]).T     # docs x questions
    gold = {d for q in questions for d in q["expected_doc_ids"]}
    chosen = set(gold)
    scores = scores.tocsc()
    for j in range(len(questions)):
        column = scores.getcol(j).toarray().ravel()
        chosen |= {ids[i] for i in column.argsort()[::-1][:negatives]}
    near = len(chosen)
    rng, target = random.Random(seed), {"gmail": gmail, "google_drive": google_drive, "hubspot": hubspot}
    for source in SOURCES:
        pool = [i for i, s in zip(ids, sources) if s == source and i not in chosen]
        have = sum(1 for i, s in zip(ids, sources) if s == source and i in chosen)
        chosen |= set(rng.sample(pool, max(0, target[source] - have)))
    by_source = {s: sum(1 for i, x in zip(ids, sources) if x == s and i in chosen) for s in SOURCES}
    out.parent.mkdir(parents=True, exist_ok=True)
    out.write_text(json.dumps({"gold": sorted(gold), "near": near, "by_source": by_source,
                               "documents": sorted(chosen)}, indent=1))
    typer.echo(f"gold {len(gold)}, with neighbours {near}, total {len(chosen)} {by_source}")


@app.command()
def convert(repo: Path, subset: Path, out: Path):
    """Write Atlas input JSON for the subset's documents (all documents with --subset all)."""
    keep = None if str(subset) == "all" else set(json.loads(subset.read_text())["documents"])
    directory = Directory(repo / "generated_data" / "employee_directory.yaml")
    counts = {s: [0, 0] for s in SOURCES}
    for source in SOURCES:
        target = out / source
        target.mkdir(parents=True, exist_ok=True)
        for f in _files(repo, source):
            raw = json.loads(f.read_text())
            if keep is not None and raw["dataset_doc_uuid"] not in keep:
                continue
            if source == "gmail":
                docs = gmail_documents(raw, directory)
            else:
                docs = [(drive_document if source == "google_drive" else hubspot_document)(raw, directory)]
            for i, doc in enumerate(docs):
                name = f"{raw['dataset_doc_uuid']}_{i + 1}.json" if len(docs) > 1 else f"{raw['dataset_doc_uuid']}.json"
                (target / name).write_text(json.dumps(doc, ensure_ascii=False))
            counts[source][0] += 1
            counts[source][1] += len(docs)
    for s, (n, files) in counts.items():
        typer.echo(f"{s}: {n} documents -> {files} files")


if __name__ == "__main__":
    app()
