"""The plug-in contract every capability implements.

A Capability owns one or more tools Claude can call: either an
Anthropic-defined toolset (computer, browser), an Anthropic-defined tool
(the text editor), or plain custom tools with a JSON schema. To add a new
capability, subclass Capability, then list it in tools/__init__.py.
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import TYPE_CHECKING, Any

if TYPE_CHECKING:
    from ..config import Config
    from ..control import Control
    from ..state import Run
    from ..ui import UI

# What execute() may return: plain text, or a list of content blocks
# (text / image / browser_state dicts) for the tool_result.
Output = str | list[dict]


class ToolError(Exception):
    """An expected failure. The message goes back to Claude as an error result."""


@dataclass
class ToolContext:
    cfg: "Config"
    ui: "UI"
    control: "Control"
    run: "Run"

    @property
    def unattended(self) -> bool:
        return self.run.unattended or not self.ui.can_ask


class Capability:
    # Anthropic toolsets: tool_use blocks carry toolset_name == this value.
    toolset: str | None = None

    def __init__(self, cfg: "Config") -> None:
        self.cfg = cfg

    def definitions(self) -> list[dict]:
        """Entries for the request's `tools` array."""
        raise NotImplementedError

    def tool_names(self) -> set[str]:
        """Plain tool names this capability answers (not used for toolsets)."""
        return {d["name"] for d in self.definitions() if "name" in d}

    def handles(self, name: str, toolset_name: str | None) -> bool:
        if self.toolset:
            return toolset_name == self.toolset
        return toolset_name is None and name in self.tool_names()

    def execute(self, name: str, args: dict[str, Any], ctx: ToolContext) -> Output:
        raise NotImplementedError

    def describe(self, name: str, args: dict[str, Any]) -> str:
        """One line for the console and log, e.g. 'click (640, 380)'."""
        return f"{name} {_short(args)}"

    def close(self) -> None:
        """Release resources (browser, handles). Called on shutdown."""


def custom_tool(name: str, description: str, properties: dict, required: list[str]) -> dict:
    """A JSON-schema tool definition, with eager input streaming on."""
    return {
        "name": name,
        "description": description,
        "input_schema": {"type": "object", "properties": properties, "required": required},
        "eager_input_streaming": True,
    }


def validate(definition: dict, args: Any) -> None:
    """Minimal schema check. With eager input streaming the API does not
    validate inputs, so a truncated or malformed call must be caught here."""
    if not isinstance(args, dict):
        raise ToolError("Invalid input: expected a JSON object.")
    schema = definition.get("input_schema", {})
    for key in schema.get("required", []):
        if key not in args:
            raise ToolError(f"Invalid input: missing required field '{key}'.")
    types = {"string": str, "integer": int, "number": (int, float), "boolean": bool,
             "array": list, "object": dict}
    for key, spec in schema.get("properties", {}).items():
        if key in args and args[key] is not None and spec.get("type") in types:
            if not isinstance(args[key], types[spec["type"]]):
                raise ToolError(f"Invalid input: '{key}' should be a {spec['type']}.")


def _short(args: Any, limit: int = 90) -> str:
    text = ", ".join(f"{k}={v!r}" for k, v in args.items()) if isinstance(args, dict) else repr(args)
    text = text.replace("\n", "\\n")
    return text if len(text) <= limit else text[: limit - 1] + "…"
