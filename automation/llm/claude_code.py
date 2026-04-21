import json
import sys
from pathlib import Path

from .base import Extraction, EXTRACTION_PROMPT


SENTINEL_START = "<<<LLM_EXTRACT_REQUEST>>>"
SENTINEL_END = "<<<END_LLM_EXTRACT_REQUEST>>>"
RESPONSE_START = "<<<LLM_EXTRACT_RESPONSE>>>"
RESPONSE_END = "<<<END_LLM_EXTRACT_RESPONSE>>>"


class ClaudeCodeExtractor:
    """Emits a request on stdout that Claude Code (the outer driver) fulfills.

    The user runs this script from a Claude Code chat. When the script prints a
    request block, Claude Code reads the referenced screenshots and writes a
    JSON response block to stdin. Free within the user's subscription.
    """

    name = "claude_code"

    async def extract(self, dom_hint: dict, screenshots: dict[str, Path]) -> Extraction:
        req = {
            "prompt": EXTRACTION_PROMPT,
            "dom_hint": dom_hint,
            "screenshots": {k: str(v) for k, v in screenshots.items()},
        }
        sys.stdout.write(
            f"\n{SENTINEL_START}\n{json.dumps(req, indent=2)}\n{SENTINEL_END}\n"
        )
        sys.stdout.write(
            "Claude Code: read the screenshots above, then paste a line starting with\n"
            f"{RESPONSE_START} and ending with {RESPONSE_END} containing the JSON.\n"
        )
        sys.stdout.flush()

        buf: list[str] = []
        capturing = False
        for line in sys.stdin:
            stripped = line.strip()
            if stripped == RESPONSE_START:
                capturing = True
                continue
            if stripped == RESPONSE_END:
                break
            if capturing:
                buf.append(line)

        payload = json.loads("".join(buf))
        return Extraction(source=self.name, **payload)
