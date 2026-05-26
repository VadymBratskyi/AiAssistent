from __future__ import annotations

import asyncio
import logging
from pathlib import Path

from faster_whisper import WhisperModel

log = logging.getLogger(__name__)


class Transcriber:
    """Thin async wrapper around faster-whisper.

    The model is loaded eagerly at construction so the first voice message
    isn't blocked by a multi-second cold start. Loaded once, reused forever.
    """

    def __init__(
        self,
        model_size: str = "small",
        device: str = "cpu",
        compute_type: str = "int8",
        language: str | None = None,
        download_root: Path | None = None,
    ) -> None:
        log.info(
            "loading whisper model size=%s device=%s compute_type=%s download_root=%s",
            model_size,
            device,
            compute_type,
            download_root,
        )
        self.model = WhisperModel(
            model_size,
            device=device,
            compute_type=compute_type,
            download_root=str(download_root) if download_root else None,
        )
        self.language = language
        log.info("whisper model loaded")

    async def transcribe(self, audio_path: Path) -> str:
        loop = asyncio.get_running_loop()
        return await loop.run_in_executor(None, self._transcribe_sync, audio_path)

    def _transcribe_sync(self, audio_path: Path) -> str:
        segments, info = self.model.transcribe(
            str(audio_path),
            language=self.language,
            beam_size=5,
            vad_filter=True,
        )
        text = " ".join(seg.text.strip() for seg in segments).strip()
        log.info(
            "transcribed lang=%s prob=%.2f duration=%.1fs chars=%d",
            info.language,
            info.language_probability,
            info.duration,
            len(text),
        )
        return text


__all__ = ["Transcriber"]
