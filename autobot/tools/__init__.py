"""The capability registry.

To give the agent a new capability: write a Capability subclass (see
base.py) and add it to CAPABILITIES. Order is fixed so the tools array -
and with it the prompt cache - stays identical between requests.
"""

from __future__ import annotations

from .base import Capability, ToolContext, ToolError
from .browser import BrowserTools
from .computer import ComputerTools
from .files import FileEditor
from .schedule import ScheduleTool
from .shell import ShellTool
from .user import UserTools

CAPABILITIES = [ComputerTools, BrowserTools, ShellTool, FileEditor, UserTools, ScheduleTool]


class Registry:
    def __init__(self, cfg) -> None:
        self.capabilities: list[Capability] = [cls(cfg) for cls in CAPABILITIES]

    def get(self, cls: type) -> Capability:
        return next(c for c in self.capabilities if isinstance(c, cls))

    def definitions(self) -> list[dict]:
        return [d for cap in self.capabilities for d in cap.definitions()]

    def find(self, name: str, toolset_name: str | None) -> Capability | None:
        return next((c for c in self.capabilities if c.handles(name, toolset_name)), None)

    def close(self) -> None:
        for cap in self.capabilities:
            try:
                cap.close()
            except Exception:
                pass


__all__ = ["Registry", "Capability", "ToolContext", "ToolError", "CAPABILITIES"]
