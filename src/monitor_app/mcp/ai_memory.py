"""
AI Memory MCP tools for cross-session dialogue persistence.

Provides tools for recording and retrieving AI dialogue history,
enabling context continuity across Claude Code sessions.

Opt-in via SWF_DIALOGUE_TURNS environment variable.
"""

import logging
from django.utils import timezone
from asgiref.sync import sync_to_async

from mcp_server import mcp_server as mcp

from ..models import AIMemory

logger = logging.getLogger(__name__)


@mcp.tool()
async def swf_record_ai_memory(
    username: str,
    session_id: str,
    role: str,
    content: str,
    namespace: str = None,
    project_path: str = None,
) -> dict:
    """
    Record a dialogue exchange for AI memory persistence.

    Called by Claude Code hooks to store user prompts and assistant responses.
    Each exchange is stored as a separate record for retrieval across sessions.

    Args:
        username: Developer username (required)
        session_id: Claude Code session ID (required)
        role: Either 'user' or 'assistant' (required)
        content: The message content (required)
        namespace: Testbed namespace if applicable
        project_path: Project directory path

    Returns:
        Success/failure status with record ID
    """
    if role not in ('user', 'assistant'):
        return {"success": False, "error": f"Invalid role '{role}'. Must be 'user' or 'assistant'."}

    if not username or not session_id or not content:
        return {"success": False, "error": "username, session_id, and content are required"}

    @sync_to_async
    def do_record():
        try:
            record = AIMemory.objects.create(
                username=username,
                session_id=session_id,
                role=role,
                content=content,
                namespace=namespace,
                project_path=project_path,
            )
            logger.debug(
                f"AI memory recorded: user={username} session={session_id[:8]}... "
                f"role={role} len={len(content)}"
            )
            return {
                "success": True,
                "id": record.id,
                "username": username,
                "role": role,
                "content_length": len(content),
            }
        except Exception as e:
            logger.error(f"Failed to record AI memory: {e}")
            return {"success": False, "error": str(e)}

    return await do_record()


@mcp.tool()
async def swf_get_ai_memory(
    username: str,
    turns: int = 20,
    namespace: str = None,
) -> list:
    """
    Get recent dialogue history for session context.

    Called at session start to load recent exchanges into the AI's context.
    Returns chronologically ordered messages (oldest first) for natural
    conversation flow.

    Args:
        username: Developer username (required)
        turns: Number of conversation turns to retrieve (default: 20).
               Each turn = 1 user + 1 assistant message, so 20 turns = up to 40 messages.
        namespace: Filter to messages from this namespace (optional)

    Returns:
        List of dialogue entries with: role, content, created_at, session_id
    """
    if not username:
        return {"error": "username is required"}

    max_messages = turns * 2  # Each turn is user + assistant

    @sync_to_async
    def fetch():
        qs = AIMemory.objects.filter(username=username)

        if namespace:
            qs = qs.filter(namespace=namespace)

        # Get most recent messages, then reverse for chronological order
        recent = qs.order_by('-created_at')[:max_messages]
        messages = list(reversed([
            {
                "role": m.role,
                "content": m.content,
                "created_at": m.created_at.isoformat() if m.created_at else None,
                "session_id": m.session_id,
            }
            for m in recent
        ]))

        return {
            "items": messages,
            "count": len(messages),
            "username": username,
            "turns_requested": turns,
            "namespace": namespace,
        }

    return await fetch()
