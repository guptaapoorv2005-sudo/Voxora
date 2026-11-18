"""Deterministic text-callable tool loop for offline evaluations.

The planner is injectable: production model quality is evaluated separately.
Calls still enter the same typed ClinicVoiceTools used by the voice worker.
"""
from dataclasses import dataclass
from typing import Any, Protocol

from livekit.agents import llm


@dataclass(frozen=True)
class ToolStep:
    name: str
    arguments: dict[str, Any]


class Planner(Protocol):
    def plan(self, utterance: str) -> list[ToolStep]: ...


class ScriptedPlanner:
    def __init__(self, scripts: dict[str, list[ToolStep]]):
        self.scripts = scripts

    def plan(self, utterance: str) -> list[ToolStep]:
        return self.scripts.get(utterance, [])


class TextToolLoop:
    def __init__(self, tools: Any, planner: Planner, context: Any):
        self.tools = {tool.__name__: tool for tool in tools.get_tools()}
        self.planner = planner
        self.context = context

    async def turn(self, utterance: str) -> list[str]:
        responses = []
        for step in self.planner.plan(utterance):
            if step.name not in self.tools:
                raise ValueError("Unknown tool")
            try:
                responses.append(await self.tools[step.name](self.context, **step.arguments))
            except llm.StopResponse:
                responses.append("[readback]")
        return responses
