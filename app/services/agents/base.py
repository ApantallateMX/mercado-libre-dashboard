"""
app/services/agents/base.py

Base class infrastructure for all AI agents.
Uses httpx directly against the Anthropic Messages API — no SDK dependency.
"""

from __future__ import annotations

import logging
from abc import ABC, abstractmethod
from dataclasses import dataclass, field
from typing import Any

import httpx

from app.config import ANTHROPIC_API_KEY

logger = logging.getLogger(__name__)

ANTHROPIC_API_URL = "https://api.anthropic.com/v1/messages"
ANTHROPIC_MODEL = "claude-sonnet-4-6"
ANTHROPIC_VERSION = "2023-06-01"
MAX_TOOL_ITERATIONS = 10


# ─────────────────────────────────────────────────────────────────────────────
# Result dataclass
# ─────────────────────────────────────────────────────────────────────────────

@dataclass
class AgentResult:
    """Standardized return value for every agent run."""
    success: bool
    message: str
    data: dict = field(default_factory=dict)
    actions: list = field(default_factory=list)
    agent_name: str = ""


# ─────────────────────────────────────────────────────────────────────────────
# BaseAgent
# ─────────────────────────────────────────────────────────────────────────────

class BaseAgent(ABC):
    """
    Abstract base for all dashboard agents.

    Subclasses MUST define:
      - name        (str class attribute)
      - description (str class attribute)
      - emoji       (str class attribute)
      - _define_tools()       → list of Claude tool dicts
      - _get_system_prompt()  → str
      - _handle_tool_call()   → str (serialized result)

    The `run()` method drives the full tool-use loop automatically.
    """

    # ── Class-level identity — override in every subclass ──────────────────
    name: str = "base_agent"
    description: str = "Base agent"
    emoji: str = "🤖"

    def __init__(self, memory_manager=None):
        """
        Args:
            memory_manager: Optional MemoryManager instance for persistent
                            key-value storage and conversation history.
        """
        self.memory = memory_manager
        self._logger = logging.getLogger(f"agents.{self.name}")

    # ── Abstract interface ──────────────────────────────────────────────────

    @abstractmethod
    def _define_tools(self) -> list:
        """
        Return a list of tool definitions in Anthropic tool format:

        [
          {
            "name": "tool_name",
            "description": "What this tool does",
            "input_schema": {
              "type": "object",
              "properties": { ... },
              "required": [...]
            }
          },
          ...
        ]
        """

    @abstractmethod
    def _get_system_prompt(self) -> str:
        """Return the system prompt string for this agent."""

    @abstractmethod
    async def _handle_tool_call(self, tool_name: str, tool_input: dict) -> str:
        """
        Execute a tool requested by Claude and return a string result.

        Args:
            tool_name:  Name of the tool Claude chose.
            tool_input: Dict of arguments Claude provided.

        Returns:
            String representation of the tool result (will be sent back as
            a tool_result content block).
        """

    # ── Public entry point ──────────────────────────────────────────────────

    async def run(self, task: str, context: dict | None = None) -> AgentResult:
        """
        Execute the agent for a given task using the Claude tool-use loop.

        Algorithm:
          1. Build initial user message (task + optional context).
          2. Call Claude with tools defined by _define_tools().
          3. If stop_reason == "tool_use":
               a. Execute every tool_use block via _handle_tool_call().
               b. Append assistant message + tool results to messages.
               c. Go to 2.
          4. If stop_reason == "end_turn" (text response): return AgentResult.
          5. Abort after MAX_TOOL_ITERATIONS to prevent infinite loops.

        Args:
            task:    Natural-language task description.
            context: Optional dict with extra runtime data for the agent.

        Returns:
            AgentResult with success flag, final message, and any data/actions
            collected during tool execution.
        """
        context = context or {}
        actions: list[str] = []
        collected_data: dict[str, Any] = {}

        # Build the opening user message
        user_content = task
        if context:
            import json
            ctx_str = json.dumps(context, ensure_ascii=False, indent=2)
            user_content = f"{task}\n\nContexto adicional:\n```json\n{ctx_str}\n```"

        messages: list[dict] = [{"role": "user", "content": user_content}]
        tools = self._define_tools()

        for iteration in range(MAX_TOOL_ITERATIONS):
            self._logger.debug("Tool-use loop iteration %d", iteration + 1)

            try:
                response = await self._call_claude(messages, tools=tools)
            except Exception as exc:
                self._logger.error("Claude API error: %s", exc)
                return AgentResult(
                    success=False,
                    message=f"Error al llamar a Claude: {exc}",
                    data=collected_data,
                    actions=actions,
                    agent_name=self.name,
                )

            stop_reason = response.get("stop_reason", "end_turn")
            content_blocks: list[dict] = response.get("content", [])

            # ── Tool-use turn ──────────────────────────────────────────────
            if stop_reason == "tool_use":
                # Append full assistant message (may mix text + tool_use blocks)
                messages.append({"role": "assistant", "content": content_blocks})

                # Build the tool results list
                tool_results: list[dict] = []
                for block in content_blocks:
                    if block.get("type") != "tool_use":
                        continue

                    tool_name = block["name"]
                    tool_input = block.get("input", {})
                    tool_use_id = block["id"]

                    self._logger.debug("Executing tool: %s | input: %s", tool_name, tool_input)
                    actions.append(f"tool:{tool_name}")

                    try:
                        result_str = await self._handle_tool_call(tool_name, tool_input)
                    except Exception as exc:
                        self._logger.warning("Tool %s raised: %s", tool_name, exc)
                        result_str = f"ERROR: {exc}"

                    tool_results.append({
                        "type": "tool_result",
                        "tool_use_id": tool_use_id,
                        "content": result_str,
                    })

                # Append the user turn carrying all tool results
                messages.append({"role": "user", "content": tool_results})
                continue  # next iteration

            # ── Final text turn ────────────────────────────────────────────
            final_text = "\n".join(
                block.get("text", "")
                for block in content_blocks
                if block.get("type") == "text"
            ).strip()

            return AgentResult(
                success=True,
                message=final_text or "(sin respuesta de texto)",
                data=collected_data,
                actions=actions,
                agent_name=self.name,
            )

        # Exceeded iteration limit
        self._logger.warning("Agent %s exceeded MAX_TOOL_ITERATIONS (%d)", self.name, MAX_TOOL_ITERATIONS)
        return AgentResult(
            success=False,
            message=f"El agente superó el límite de {MAX_TOOL_ITERATIONS} iteraciones de herramientas.",
            data=collected_data,
            actions=actions,
            agent_name=self.name,
        )

    # ── Internal helpers ────────────────────────────────────────────────────

    async def _call_claude(
        self,
        messages: list,
        tools: list | None = None,
        max_tokens: int = 2048,
    ) -> dict:
        """
        POST to Anthropic Messages API and return the parsed response dict.

        Args:
            messages:   Conversation history (role/content pairs).
            tools:      Optional list of tool definitions.
            max_tokens: Max tokens in the response.

        Returns:
            Parsed JSON response body as dict.

        Raises:
            RuntimeError: If ANTHROPIC_API_KEY is not configured or API returns error.
        """
        if not ANTHROPIC_API_KEY:
            raise RuntimeError("ANTHROPIC_API_KEY no está configurada")

        payload: dict[str, Any] = {
            "model": ANTHROPIC_MODEL,
            "max_tokens": max_tokens,
            "system": self._get_system_prompt(),
            "messages": messages,
        }
        if tools:
            payload["tools"] = tools

        headers = {
            "x-api-key": ANTHROPIC_API_KEY,
            "anthropic-version": ANTHROPIC_VERSION,
            "content-type": "application/json",
        }

        async with httpx.AsyncClient(timeout=120.0) as client:
            resp = await client.post(ANTHROPIC_API_URL, json=payload, headers=headers)

        if resp.status_code != 200:
            try:
                err = resp.json()
                msg = err.get("error", {}).get("message", resp.text)
            except Exception:
                msg = resp.text
            raise RuntimeError(f"Anthropic API error {resp.status_code}: {msg}")

        return resp.json()
