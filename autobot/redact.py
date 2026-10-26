"""Mask things that look like secrets before they reach logs or the console."""

from __future__ import annotations

import re

_SECRET = re.compile(
    r"(sk-[A-Za-z0-9_\-]{16,}"            # Anthropic / OpenAI style
    r"|fc-[A-Za-z0-9]{16,}"               # Firecrawl
    r"|gh[pousr]_[A-Za-z0-9]{20,}"        # GitHub
    r"|github_pat_[A-Za-z0-9_]{20,}"
    r"|AKIA[0-9A-Z]{16}"                  # AWS access key id
    r"|xox[abprs]-[A-Za-z0-9-]{10,}"      # Slack
    r"|AIza[0-9A-Za-z_\-]{30,}"           # Google API key
    r"|eyJ[A-Za-z0-9_\-]{15,}\.[A-Za-z0-9_\-]{10,}\.[A-Za-z0-9_\-]{10,}"  # JWT
    r"|\b[0-9]{8,10}:[A-Za-z0-9_\-]{35}\b"  # Telegram bot token
    r")"
)


def redact(text: str) -> str:
    if not text:
        return text
    return _SECRET.sub(lambda m: m.group(0)[:6] + "…[redacted]", text)
