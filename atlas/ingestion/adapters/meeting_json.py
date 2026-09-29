"""Meeting JSON (Fathom `new-meeting-content-ready` shape) -> NormalizedDocument.

Uses: title/meeting_title, recording_id, url/share_url, scheduled_*/recording_*
times, calendar_invitees[{name, email, email_domain, is_external,
matched_speaker_display_name}], calendar_invitees_domains_type,
recorded_by{name, email, team}, transcript[{speaker{display_name,
matched_calendar_invitee_email}, text, timestamp}], default_summary
{markdown_formatted}, action_items[{description, assignee{name, email},
completed, recording_timestamp, recording_playback_url}].
"""

from __future__ import annotations

from datetime import datetime
from pathlib import Path

from atlas.ingestion.adapters.base import UnsupportedSource, build_text
from atlas.ingestion.normalized import NormalizedDocument, Participant, document_id_for


def _dt(value) -> datetime | None:
    return datetime.fromisoformat(str(value).replace("Z", "+00:00")) if value else None


def _email(value) -> str | None:
    return (value or "").strip().lower() or None


class MeetingJsonAdapter:
    name = "meeting_json"

    def can_handle(self, path: Path, payload: dict | None) -> bool:
        return bool(payload) and "recording_id" in payload and "calendar_invitees" in payload

    def normalize(self, path: Path, payload: dict | None) -> NormalizedDocument:
        if not payload:
            raise UnsupportedSource(f"{path}: empty meeting payload")
        system = payload.get("source_system", "fathom")
        external_id = str(payload["recording_id"])

        participants: list[Participant] = []
        for i, inv in enumerate(payload.get("calendar_invitees") or []):
            participants.append(Participant(name=inv.get("name"), email=_email(inv.get("email")), role="invitee",
                                            is_external=inv.get("is_external"),
                                            source_field=f"calendar_invitees[{i}]"))
        rec = payload.get("recorded_by") or {}
        recorder = None
        if rec:
            recorder = Participant(name=rec.get("name"), email=_email(rec.get("email")), role="recorder",
                                   team=rec.get("team"), is_external=False, source_field="recorded_by")
            participants.append(recorder)

        # Fathom links invitees to speaker labels; use it when a turn lacks an email.
        email_by_speaker_label = {inv["matched_speaker_display_name"]: _email(inv.get("email"))
                                  for inv in payload.get("calendar_invitees") or []
                                  if inv.get("matched_speaker_display_name") and inv.get("email")}
        speakers: dict[tuple, Participant] = {}
        parts: list[tuple[str, str, dict]] = []
        summary = ((payload.get("default_summary") or {}).get("markdown_formatted") or "")
        parts.append(("summary", summary, {"source_field": "default_summary.markdown_formatted"}))
        for i, turn in enumerate(payload.get("transcript") or []):
            speaker = turn.get("speaker") or {}
            name = speaker.get("display_name")
            email = _email(speaker.get("matched_calendar_invitee_email")) or email_by_speaker_label.get(name)
            key = (email, name)
            if key not in speakers:
                speakers[key] = Participant(name=name, email=email, role="speaker",
                                            source_field=f"transcript[{i}].speaker")
            parts.append(("transcript_turn", f"{name}: {turn.get('text', '')}",
                          {"source_field": f"transcript[{i}]", "speaker_name": name, "speaker_email": email,
                           "timestamp": turn.get("timestamp")}))
        participants += list(speakers.values())
        for i, item in enumerate(payload.get("action_items") or []):
            assignee = item.get("assignee") or {}
            parts.append(("action_item", item.get("description", ""),
                          {"source_field": f"action_items[{i}]", "assignee_name": assignee.get("name"),
                           "assignee_email": _email(assignee.get("email")), "completed": item.get("completed"),
                           "recording_timestamp": item.get("recording_timestamp"),
                           "playback_url": item.get("recording_playback_url")}))
        raw_text, sections = build_text(parts)

        start = _dt(payload.get("recording_start_time")) or _dt(payload.get("scheduled_start_time"))
        return NormalizedDocument(
            document_id=document_id_for(system, external_id),
            source_system=system,
            source_type="meeting",
            source_external_id=external_id,
            title=payload.get("meeting_title") or payload.get("title"),
            uri=payload.get("share_url") or payload.get("url"),
            raw_text=raw_text,
            author=recorder,
            participants=participants,
            created_at=start,
            metadata={
                "recording_id": payload["recording_id"],
                "scheduled_start_time": payload.get("scheduled_start_time"),
                "scheduled_end_time": payload.get("scheduled_end_time"),
                "recording_start_time": payload.get("recording_start_time"),
                "recording_end_time": payload.get("recording_end_time"),
                "calendar_invitees_domains_type": payload.get("calendar_invitees_domains_type"),
                "calendar_event_id": payload.get("calendar_event_id"),
            },
            sections=sections,
            permissions=payload.get("permissions") or {},
        )
