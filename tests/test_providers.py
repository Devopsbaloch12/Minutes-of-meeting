from types import SimpleNamespace

import deepgram
import pytest

from app.providers.llm.client import OpenAICompatibleLLM
from app.providers.speech import deepgram_provider


def _llm_replying(content, finish_reason):
    llm = OpenAICompatibleLLM(base_url="http://llm.test/v1", api_key="", model="reasoner", max_tokens=40)
    reply = SimpleNamespace(choices=[SimpleNamespace(
        message=SimpleNamespace(content=content), finish_reason=finish_reason)])
    llm._client = SimpleNamespace(chat=SimpleNamespace(completions=SimpleNamespace(create=lambda **_: reply)))
    return llm


def test_llm_returns_a_finished_answer():
    assert _llm_replying("  ## Summary  ", "stop").complete("system", "user") == "## Summary"


@pytest.mark.parametrize("content, wording", [
    ("", "before it wrote an answer"),              # the whole budget went on reasoning
    ("## Summary\n\n| Task |", "answer was cut off"),  # minutes truncated mid-table
])
def test_llm_refuses_an_answer_cut_off_by_the_token_limit(content, wording):
    with pytest.raises(RuntimeError) as error:
        _llm_replying(content, "length").complete("system", "user")
    assert wording in str(error.value) and "Max output tokens limit (40)" in str(error.value)


class FakeDeepgram:
    created = []

    def __init__(self, **kwargs):
        FakeDeepgram.created.append(kwargs)
        self.listen = SimpleNamespace(v1=SimpleNamespace(media=SimpleNamespace(transcribe_file=self._transcribe)))

    def _transcribe(self, request, **options):
        FakeDeepgram.uploads = [b"".join(request), b"".join(request)]  # as a retry would send it
        return {"results": {"utterances": [
            {"speaker": 0, "transcript": "Shall we start?"},
            {"speaker": 1, "transcript": "Yes."},
        ]}}


def test_deepgram_streams_the_recording_and_times_out_per_piece(monkeypatch, tmp_path):
    monkeypatch.setattr(deepgram, "DeepgramClient", FakeDeepgram)
    monkeypatch.setattr(deepgram_provider, "CHUNK_BYTES", 4)
    audio = tmp_path / "audio.wav"
    audio.write_bytes(b"RIFF....data0123456789")

    provider = deepgram_provider.DeepgramProvider({"deepgram_api_key": "dg-key", "diarization_enabled": "true"})
    transcript = provider.transcribe(str(audio))

    timeout = FakeDeepgram.created[-1]["timeout"]
    assert timeout.write <= 120, "a stalled upload must fail within minutes"
    assert timeout.read >= 600, "Deepgram may take minutes to process a long meeting"
    # Re-iterable: a retry after a dropped connection resends the whole file.
    assert FakeDeepgram.uploads == [audio.read_bytes(), audio.read_bytes()]
    assert transcript == "[Speaker 0]: Shall we start?\n\n[Speaker 1]: Yes."


def test_audio_chunks_are_bounded_and_complete(tmp_path):
    audio = tmp_path / "audio.wav"
    audio.write_bytes(bytes(range(256)) * 10)
    chunks = list(deepgram_provider.AudioChunks(str(audio), chunk_bytes=1000))
    assert [len(c) for c in chunks] == [1000, 1000, 560]
    assert b"".join(chunks) == audio.read_bytes()


def test_deepgram_is_told_the_language_when_one_is_set(monkeypatch, tmp_path):
    sent = {}

    class Recording(FakeDeepgram):
        def _transcribe(self, request, **options):
            sent.update(options)
            return super()._transcribe(request, **options)

    monkeypatch.setattr(deepgram, "DeepgramClient", Recording)
    audio = tmp_path / "audio.wav"
    audio.write_bytes(b"RIFF")
    deepgram_provider.DeepgramProvider({"deepgram_api_key": "k", "speech_language": "ur"}).transcribe(str(audio))
    assert sent["language"] == "ur" and sent["model"] == "nova-3"


def test_deepgram_uses_multilingual_mode_when_auto_detecting(monkeypatch, tmp_path):
    sent = {}

    class Recording(FakeDeepgram):
        def _transcribe(self, request, **options):
            sent.update(options)
            return super()._transcribe(request, **options)

    monkeypatch.setattr(deepgram, "DeepgramClient", Recording)
    audio = tmp_path / "audio.wav"
    audio.write_bytes(b"RIFF")
    deepgram_provider.DeepgramProvider(
        {"deepgram_api_key": "k", "speech_language": "auto"}
    ).transcribe(str(audio))
    assert sent["language"] == "multi" and sent["model"] == "nova-3"


def test_auto_retries_as_urdu_when_multilingual_returns_hindi(monkeypatch, tmp_path):
    calls = []

    class HindiThenUrdu(FakeDeepgram):
        def _transcribe(self, request, **options):
            calls.append(options["language"])
            text = "नमस्ते, कैसे हैं?" if options["language"] == "multi" else "ہیلو، کیسے ہیں؟"
            return {"results": {"utterances": [{"speaker": 0, "transcript": text}]}}

    monkeypatch.setattr(deepgram, "DeepgramClient", HindiThenUrdu)
    audio = tmp_path / "audio.wav"
    audio.write_bytes(b"RIFF")
    transcript = deepgram_provider.DeepgramProvider(
        {"deepgram_api_key": "k", "speech_language": "auto"}
    ).transcribe(str(audio))
    assert calls == ["multi", "ur"]
    assert transcript == "ہیلو، کیسے ہیں؟"


def test_auto_keeps_english_without_a_second_request(monkeypatch, tmp_path):
    calls = []

    class English(FakeDeepgram):
        def _transcribe(self, request, **options):
            calls.append(options["language"])
            return {"results": {"utterances": [{"speaker": 0, "transcript": "Hello there."}]}}

    monkeypatch.setattr(deepgram, "DeepgramClient", English)
    audio = tmp_path / "audio.wav"
    audio.write_bytes(b"RIFF")
    transcript = deepgram_provider.DeepgramProvider(
        {"deepgram_api_key": "k", "speech_language": "auto"}
    ).transcribe(str(audio))
    assert calls == ["multi"] and transcript == "Hello there."
