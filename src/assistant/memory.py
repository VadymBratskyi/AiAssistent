from __future__ import annotations

import logging
import time
from pathlib import Path

log = logging.getLogger(__name__)

DEFAULT_MEMORY_PATH = Path("/data/memory.md")

_HEADER = "# Bot Memory\n\nLong-term facts the user has taught the bot.\n\n"


class Memory:
    """Human-readable long-term memory kept as a markdown file in the PVC.

    Every user turn gets this content prepended so the agent has fresh facts.
    Kept plain-text on purpose — the user can `/memory` to inspect it or
    hand-edit the file directly if they want.
    """

    def __init__(self, path: Path = DEFAULT_MEMORY_PATH) -> None:
        self.path = path
        self.path.parent.mkdir(parents=True, exist_ok=True)
        if not self.path.exists():
            self.path.write_text(_HEADER, encoding="utf-8")

    def read(self) -> str:
        try:
            return self.path.read_text(encoding="utf-8")
        except OSError:
            return _HEADER

    def facts(self) -> list[str]:
        """Return the fact lines (bullets), without the markdown header."""
        return [
            line[2:].strip()
            for line in self.read().splitlines()
            if line.startswith("- ")
        ]

    def append(self, fact: str) -> str:
        fact = fact.strip().replace("\n", " ")
        if not fact:
            return ""
        line = f"- {time.strftime('%Y-%m-%d')}: {fact}\n"
        with self.path.open("a", encoding="utf-8") as f:
            f.write(line)
        log.info("memory added: %s", fact[:80])
        return line.rstrip()

    def remove_matching(self, needle: str) -> int:
        needle = needle.strip().lower()
        if not needle:
            return 0
        content = self.read()
        kept: list[str] = []
        removed = 0
        for line in content.splitlines():
            if line.startswith("- ") and needle in line.lower():
                removed += 1
                continue
            kept.append(line)
        # Preserve trailing newline discipline
        self.path.write_text("\n".join(kept).rstrip() + "\n", encoding="utf-8")
        if removed:
            log.info("memory removed %d line(s) matching %r", removed, needle)
        return removed

    def snapshot_for_prompt(self) -> str:
        """Compact rendering to prepend to user turns. Empty if no facts."""
        facts = self.facts()
        if not facts:
            return ""
        return "Пам'ять про мене:\n" + "\n".join(f"- {f}" for f in facts)


__all__ = ["Memory", "DEFAULT_MEMORY_PATH"]
