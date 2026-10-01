"""TTS per the P0 verdict: macOS `say -v Anna`, markdown stripped first.

agent.llm.sanitize_reply already strips reasoning blocks and markdown from
every tutor reply; speech_text applies the same pass again (defense in depth
for direct callers) and removes emoji — `say` would read some of them aloud.
Voice pinned to Anna (de_DE) per AGENTS.md.
"""

from __future__ import annotations

import re
import shutil
import subprocess

DEFAULT_VOICE = "Anna"

# Pictographs/emoji, misc symbols, dingbats, arrows-as-emoji, variation
# selector, ZWJ, keycap — none of these belong in spoken German.
_EMOJI_RE = re.compile(
    "[\U0001F000-\U0001FAFF\u2600-\u27BF\u2B00-\u2BFF\uFE0F\u200D\u20E3]+"
)


def speech_text(text: str) -> str:
    """Markdown- and emoji-free string safe to hand to `say`."""
    from agent.llm import sanitize_reply  # deferred: pulls in the openai SDK

    text = sanitize_reply(text)
    text = _EMOJI_RE.sub("", text)
    text = re.sub(r"[ \t]{2,}", " ", text)
    return text.strip()


class Speaker:
    """Blocking `say` wrapper — speaking counts toward the printed ⏱ latency,
    so the loop awaits it (and Ctrl-C stops speech mid-sentence)."""

    def __init__(self, voice: str = DEFAULT_VOICE, runner=None) -> None:
        self.voice = voice
        self._runner = runner or (lambda argv: subprocess.run(argv, check=False))

    def say(self, text: str) -> None:
        spoken = speech_text(text)
        if not spoken:
            return
        self._runner(["say", "-v", self.voice, spoken])

    @staticmethod
    def available() -> bool:
        return shutil.which("say") is not None
