"""Webhook endpoints for receiving push notifications from agents."""

import logging
from fastapi import APIRouter, Request

from .state import sessions
from .agent_client import AgentResult

logger = logging.getLogger(__name__)

router = APIRouter()


@router.post("/webhook")
async def receive_webhook(request: Request):
    """
    Receive push notification from agent when task completes.

    Expected payload formats:

    MCP-style result:
    {
        "jsonrpc": "2.0",
        "id": "task_xxx",
        "result": {
            "content": [{"type": "text", "text": "..."}],
            "isError": false
        }
    }

    Or error:
    {
        "jsonrpc": "2.0",
        "id": "task_xxx",
        "error": {"code": -1, "message": "..."}
    }

    Custom notification format:
    {
        "task_id": "task_xxx",
        "agent": "AgentName",
        "tool": "tool_name",
        "success": true,
        "content": "result content"
    }
    """
    try:
        body = await request.json()
    except Exception as e:
        logger.warning(f"Failed to parse webhook body: {e}")
        return {"status": "error", "message": "Invalid JSON"}

    logger.debug(f"Received webhook: {body}")

    # Parse different formats
    result = _parse_notification(body)

    if not result:
        logger.warning(f"Could not parse notification: {body}")
        return {"status": "error", "message": "Unknown format"}

    # Deliver to session
    delivered = await sessions.deliver_result(result.task_id, result)

    if delivered:
        logger.info(f"Delivered result for task {result.task_id}")
        return {"status": "ok", "task_id": result.task_id}
    else:
        logger.warning(f"No session found for task {result.task_id}")
        return {"status": "error", "message": "Session not found"}


def _parse_notification(body: dict) -> AgentResult | None:
    """Parse notification into AgentResult."""

    # Try MCP JSON-RPC format
    if "jsonrpc" in body:
        task_id = body.get("id", "")

        if "error" in body:
            error = body["error"]
            return AgentResult(
                task_id=task_id,
                agent_name=body.get("_agent", "Unknown"),
                tool_name=body.get("_tool", "unknown"),
                content=error.get("message", str(error)),
                success=False,
            )

        if "result" in body:
            result = body["result"]

            # Extract text from MCP content array
            content_parts = []
            for item in result.get("content", []):
                if item.get("type") == "text":
                    content_parts.append(item.get("text", ""))

            return AgentResult(
                task_id=task_id,
                agent_name=body.get("_agent", "Unknown"),
                tool_name=body.get("_tool", "unknown"),
                content="\n".join(content_parts),
                success=not result.get("isError", False),
            )

    # Try custom format
    if "task_id" in body:
        return AgentResult(
            task_id=body["task_id"],
            agent_name=body.get("agent", "Unknown"),
            tool_name=body.get("tool", "unknown"),
            content=body.get("content", ""),
            success=body.get("success", True),
        )

    return None
