"""Entry point Claude Code launches for the auto-bot MCP server (see README)."""

import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))

from autobot.mcp_server import main  # noqa: E402

main()
