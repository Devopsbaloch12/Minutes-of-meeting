import httpx

from app.providers.speech.base import BaseSpeechProvider, merge_turns

# The recording is streamed in 1 MB pieces, so the write timeout bounds each piece,
# not the whole upload. Sent as one body (the SDK's bytes path), a two-hour meeting
# (~230 MB of WAV) outlasted the 60-second default; raising that to 30 minutes then
# let a stalled connection hang a 25-second meeting for the full 30. Per piece, a
# stall fails within two minutes and a long upload still completes. The read
# timeout covers Deepgram's own processing, which it caps at about ten minutes.
REQUEST_TIMEOUT = httpx.Timeout(connect=30, write=120, read=900, pool=30)
CHUNK_BYTES = 1 << 20


class AudioChunks:
    """The recording as a re-iterable stream.

    Each pass reopens the file, so when the SDK retries after a dropped connection
    it resends the whole recording, not the empty remainder of a used-up iterator.
    """

    def __init__(self, path: str, chunk_bytes: int = CHUNK_BYTES):
        self.path = path
        self.chunk_bytes = chunk_bytes

    def __iter__(self):
        with open(self.path, "rb") as handle:
            while chunk := handle.read(self.chunk_bytes):
                yield chunk


class DeepgramProvider(BaseSpeechProvider):
    name = "deepgram"
    label = "Deepgram"

    def transcribe(self, audio_path: str) -> str:
        from deepgram import DeepgramClient

        client = DeepgramClient(api_key=self._require_key("deepgram_api_key", "Deepgram"), timeout=REQUEST_TIMEOUT)
        audio = AudioChunks(audio_path)

        options = {
            "model": self._setting("deepgram_model", "nova-3"),
            "smart_format": True,
            "punctuate": True,
            "paragraphs": True,
            "diarize": self.diarize,
            "utterances": self.diarize,
        }
        if self.language:
            options["language"] = self.language
        elif self.auto_detect:
            # nova-3 code-switching: speakers may switch language mid-sentence.
            # Deepgram lists 55 languages for "multi"; Urdu is not one of them,
            # so it writes Urdu speech as Hindi (Devanagari). That is not what the
            # uploader asked for: if Hindi appears, transcribe again as Urdu.
            options["language"] = "multi"

        text = _extract(client.listen.v1.media.transcribe_file(request=audio, **options))
        if self.auto_detect and not self.language and _devanagari_count(text) >= 10:
            options["language"] = "ur"
            urdu = _extract(
                client.listen.v1.media.transcribe_file(request=AudioChunks(audio_path), **options)
            )
            if urdu:
                return urdu
        return text


def _devanagari_count(text: str) -> int:
    return sum("\u0900" <= ch <= "\u097f" for ch in text)


def _extract(response) -> str:
    results = _get(response, "results")
    if results is None:
        raise RuntimeError("Deepgram returned a response without results.")

    utterances = _get(results, "utterances")
    if utterances:
        text = merge_turns((_get(u, "speaker"), _get(u, "transcript")) for u in utterances)
        if text:
            return text

    alternative = _first_alternative(results)
    if alternative is not None:
        paragraphs = _get(alternative, "paragraphs")
        formatted = _get(paragraphs, "transcript") if paragraphs is not None else None
        if formatted:
            return formatted.strip()
        raw = _get(alternative, "transcript")
        if raw:
            return raw.strip()
    return ""


def _first_alternative(results):
    channels = _get(results, "channels") or []
    alternatives = _get(channels[0], "alternatives") if channels else None
    return alternatives[0] if alternatives else None


def _get(obj, key):
    """Deepgram models expose attributes; raw dicts appear in some responses."""
    if obj is None:
        return None
    if isinstance(obj, dict):
        return obj.get(key)
    return getattr(obj, key, None)
