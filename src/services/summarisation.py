"""
User-level summarisation ("intent profiling") powered by Mistral.

Key design:
- Keyed by user_name (not conversation_id) so we can learn preferences across chats.
- First run: summarise all messages for user_name.
- Subsequent runs: only summarise delta messages since last_summarised_at and update summary.

Storage:
- conversation_messages: already stores each message (with user_name, created_at).
- user_summaries: one row per user_name with summary_text and last_summarised_at.
"""

from __future__ import annotations

import logging
from datetime import datetime, timezone
from typing import Any, Dict, List, Optional, Tuple

from src.config import MODEL_NAME
from src.services.flight_services import call_mistral_with_backoff
from src.services.supabase_persistence import _get_client

logger = logging.getLogger(__name__)

MESSAGES_TABLE = "conversation_messages"
SUMMARY_TABLE = "user_summaries"


SUMMARISER_SYSTEM_PROMPT = """You are a summarisation agent for a flight-finder assistant.

Goal: Build a compact "user profile" describing the user's recurring intents and preferences so future flight searches can be tailored.

Include (when present):
- Typical routes / cities / airports they ask about
- Preferences: flexible dates, cheapest, non-stop, time of day, cabin class, passengers
- Output format preferences: likes tables, CSV downloads, brief answers, etc.
- Any constraints or recurring issues (e.g. missing dates, ambiguity around airports)
- In case if they have any preferences for credit card which they have asked for in the past.

Rules:
- Keep it short (max ~8 bullet points).
- Be factual; don't invent details.
- Prefer stable preferences over one-off requests.
"""


def _utcnow_iso() -> str:
    return datetime.now(timezone.utc).isoformat()


def _safe_user_name(user_name: Optional[str]) -> Optional[str]:
    u = (user_name or "").strip()
    return u or None


def get_user_summary(user_name: str) -> Optional[Dict[str, Any]]:
    """Return summary row for user_name (or None)."""
    client = _get_client()
    u = _safe_user_name(user_name)
    if client is None or not u:
        return None
    try:
        resp = (
            client.table(SUMMARY_TABLE)
            .select("user_name, summary_text, last_summarised_at, updated_at")
            .eq("user_name", u)
            .limit(1)
            .execute()
        )
        rows = getattr(resp, "data", None) or []
        return rows[0] if rows else None
    except Exception as e:
        logger.warning("get_user_summary failed user_name=%s err=%s", u, e)
        return None


def _fetch_user_messages(
    user_name: str,
    since_iso: Optional[str] = None,
    limit: int = 500,
) -> List[Dict[str, Any]]:
    """Fetch messages for user_name, optionally after since_iso timestamp."""
    client = _get_client()
    if client is None:
        return []
    q = (
        client.table(MESSAGES_TABLE)
        .select("role, content, created_at, conversation_id, turn_index")
        .eq("user_name", user_name)
        .order("created_at", desc=False)
        .limit(limit)
    )
    if since_iso:
        q = q.gt("created_at", since_iso)
    resp = q.execute()
    return getattr(resp, "data", None) or []


def _max_created_at(rows: List[Dict[str, Any]]) -> Optional[str]:
    if not rows:
        return None
    # created_at is timestamptz in Supabase; returned as ISO-ish string
    vals = [r.get("created_at") for r in rows if r.get("created_at")]
    return max(vals) if vals else None


def _call_summariser(existing_summary: str, new_messages: List[Dict[str, Any]]) -> str:
    """Call Mistral to produce (or update) a user profile summary."""
    # Keep messages compact; summariser doesn't need exact formatting.
    lines = []
    for m in new_messages:
        role = (m.get("role") or "").strip() or "unknown"
        content = (m.get("content") or "").strip()
        if not content:
            continue
        # Bound each line to avoid huge payloads
        content = content.replace("\n", " ")
        if len(content) > 500:
            content = content[:500] + "…"
        lines.append(f"- {role}: {content}")

    messages_block = "\n".join(lines) if lines else "(no new messages)"
    user_content = f"""Existing user profile summary (may be empty):
{existing_summary.strip() if existing_summary else "(empty)"}

New conversation messages (chronological):
{messages_block}

Task:
Update the user profile summary given the new messages. Return ONLY the updated summary text."""

    payload = {
        "model": MODEL_NAME,
        "messages": [
            {"role": "system", "content": SUMMARISER_SYSTEM_PROMPT},
            {"role": "user", "content": user_content},
        ],
        "temperature": 0.2,
        "max_tokens": 500,
    }
    resp = call_mistral_with_backoff(payload)
    return (resp.choices[0].message.content or "").strip()


def ensure_user_summary_updated(user_name: str) -> Optional[str]:
    """
    Ensure a user_name has an up-to-date summary in Supabase.
    Returns summary_text (or None if not available).
    """
    u = _safe_user_name(user_name)
    if not u:
        return None
    client = _get_client()
    if client is None:
        return None

    existing = get_user_summary(u) or {}
    existing_summary = (existing.get("summary_text") or "").strip()
    last_ts = (existing.get("last_summarised_at") or "").strip() or None

    # Full or delta fetch
    messages = _fetch_user_messages(u, since_iso=last_ts if last_ts else None, limit=800)
    if not messages:
        return existing_summary or None

    new_last_ts = _max_created_at(messages) or last_ts
    try:
        updated_summary = _call_summariser(existing_summary, messages)
        updated_summary = (updated_summary or "").strip()
        if not updated_summary:
            # Don't wipe existing summary on an empty model response
            return existing_summary or None

        row = {
            "user_name": u,
            "summary_text": updated_summary,
            "last_summarised_at": new_last_ts,
            "updated_at": _utcnow_iso(),
        }
        client.table(SUMMARY_TABLE).upsert(row).execute()
        logger.info("ensure_user_summary_updated: updated user_name=%s", u)
        return updated_summary
    except Exception as e:
        logger.warning("ensure_user_summary_updated failed user_name=%s err=%s", u, e)
        return existing_summary or None

