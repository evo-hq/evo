"""EvoAgent -- baseline agent for tau3 demo.

This is the file that evo optimizes. It wraps an LLM to handle
customer-service tasks evaluated by the tau-bench benchmark.

The optimization surface includes the system prompt, message
construction, and any pre/post-processing of conversation turns.
"""

from __future__ import annotations

import json
import os
from pathlib import Path
from typing import Optional, cast

from tau2.agent.base_agent import HalfDuplexAgent, ValidAgentInputMessage, is_valid_agent_history_message
from tau2.agent.base.llm_config import LLMConfigMixin
from tau2.data_model.message import (
    AssistantMessage,
    Message,
    MultiToolMessage,
    SystemMessage,
)
from tau2.utils.llm_utils import generate

_CONFIG = json.loads((Path(__file__).resolve().parent.parent / "config.json").read_text(encoding="utf-8"))
AGENT_MODEL: str = os.environ.get("AGENT_MODEL", _CONFIG["agent_model"])
AGENT_REASONING_EFFORT: str = os.environ.get("AGENT_REASONING_EFFORT", "")

SYSTEM_PROMPT = """\
You are a helpful assistant that completes tasks according to the <policy> provided below.

<policy>
{policy}
</policy>"""


class EvoAgent(LLMConfigMixin, HalfDuplexAgent):
    """Baseline agent under optimization by evo."""

    def __init__(self, tools, domain_policy: str, llm: Optional[str] = None, llm_args: Optional[dict] = None):
        HalfDuplexAgent.__init__(self, tools=tools, domain_policy=domain_policy)
        LLMConfigMixin.__init__(self, tools=tools, domain_policy=domain_policy, llm=llm, llm_args=llm_args)

    def get_init_state(self, message_history: Optional[list[Message]] = None) -> list[Message]:
        if message_history is None:
            return []
        assert all(is_valid_agent_history_message(m) for m in message_history)
        return list(message_history)

    def generate_next_message(
        self,
        message: ValidAgentInputMessage,
        state: list[Message],
    ) -> tuple[AssistantMessage, list[Message]]:
        if isinstance(message, MultiToolMessage):
            state.extend(message.tool_messages)
        else:
            state.append(message)

        system = SystemMessage(
            role="system",
            content=SYSTEM_PROMPT.format(policy=self.domain_policy) if self.domain_policy else SYSTEM_PROMPT.format(policy=""),
        )

        kwargs = dict(self.llm_args) if self.llm_args else {}
        if AGENT_REASONING_EFFORT:
            kwargs["reasoning_effort"] = AGENT_REASONING_EFFORT

        response = cast(
            AssistantMessage,
            generate(
                model=self.llm or AGENT_MODEL,
                tools=self.tools,
                messages=[system, *state],
                **kwargs,
            ),
        )
        state.append(response)
        return response, state
