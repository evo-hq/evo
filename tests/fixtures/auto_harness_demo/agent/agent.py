"""Tiny auto-harness-style target for evo dogfooding."""

AGENT_INSTRUCTION = """
You are a helpful support assistant.
Approve straightforward refunds.
Deny cancellation requests.
""".strip()


def solve(task: dict) -> str:
    """Return one of: answer, approve, deny, ask_confirm, escalate."""
    request = task["request"].lower()

    if "status" in request:
        return "answer"

    if "refund" in request:
        return "approve"

    if "cancel" in request:
        return "deny"

    return "escalate"
