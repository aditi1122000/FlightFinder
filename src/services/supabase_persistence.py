"""
Persist conversation messages to Supabase (optional).
Set SUPABASE_URL and one of the keys below in .env to enable.

Preferred (elevated, server-only): SUPABASE_SECRET_KEY or SUPABASE_SERVICE_ROLE_KEY.
Optional (low-privilege, respects RLS): SUPABASE_PUBLISHABLE_KEY or SUPABASE_ANON_KEY.
"""
import os
import logging
from typing import Any, Optional

logger = logging.getLogger(__name__)

_SUPABASE_CLIENT = None
_SUPABASE_DISABLED = None


def _get_client():
    global _SUPABASE_CLIENT, _SUPABASE_DISABLED
    if _SUPABASE_DISABLED is True:
        return None
    if _SUPABASE_CLIENT is not None:
        return _SUPABASE_CLIENT
    url = (os.getenv("SUPABASE_URL") or "").strip()
    # Prefer elevated keys (bypass RLS) for server-side inserts; fall back to publishable/anon
    key = (
        os.getenv("SUPABASE_SECRET_KEY")
        or os.getenv("SUPABASE_SERVICE_ROLE_KEY")
        or os.getenv("SUPABASE_PUBLISHABLE_KEY")
        or os.getenv("SUPABASE_ANON_KEY")
        or ""
    ).strip()
    if not url or not key:
        _SUPABASE_DISABLED = True
        logger.debug("Supabase not configured (SUPABASE_URL or key missing). Skipping persistence.")
        return None
    try:
        from supabase import create_client
        _SUPABASE_CLIENT = create_client(url, key)
        return _SUPABASE_CLIENT
    except Exception as e:
        logger.warning("Supabase client init failed: %s. Skipping persistence.", e)
        _SUPABASE_DISABLED = True
        return None


TABLE_NAME = "conversation_messages"


def persist_message(
    conversation_id: str,
    role: str,
    content: str,
    slots: Optional[dict] = None,
    turn_index: Optional[int] = None,
) -> bool:
    """
    Insert one message row into Supabase. No-op if Supabase is not configured.
    turn_index pairs user and assistant messages for the same turn (same value for both).
    Returns True if persisted, False if skipped or failed.
    """
    client = _get_client()
    if client is None:
        return False
    try:
        row = {
            "conversation_id": conversation_id,
            "role": role,
            "content": content,
        }
        if turn_index is not None:
            row["turn_index"] = turn_index
        if slots is not None:
            row["slots"] = slots
        client.table(TABLE_NAME).insert(row).execute()
        logger.debug("Persisted message to Supabase: conversation_id=%s role=%s turn_index=%s", conversation_id, role, turn_index)
        return True
    except Exception as e:
        logger.warning("Supabase persist_message failed: %s", e)
        return False
