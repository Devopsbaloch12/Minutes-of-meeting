import re
from collections.abc import Callable

from app.providers.llm.client import OpenAICompatibleLLM
from app.utils.text import split_text

# Fixed labels make the notes of every part line up, so the final call can
# merge them by label. "Speakers" carries an introduction made in part 1 to the
# minutes, which otherwise only see "Speaker 1" in the later parts.
PART_PROMPT = (
    "You are taking notes on one part of a longer meeting transcript; the other parts are "
    "handled separately. Write bullet points under these labels, leaving out a label with "
    "nothing under it:\n"
    "Speakers: each speaker label with the name and role the transcript gives for it, for "
    'example "Speaker 1 = Laura, project manager". Only names that are actually stated.\n'
    "Agenda: the topics taken up in this part.\n"
    "Discussion: the main points, with who raised them.\n"
    "Decisions: what was agreed.\n"
    'Action items: task, owner (or "Unassigned"), due date as said (or "Not stated").\n'
    "Figures: numbers, prices, dates and names mentioned.\n"
    "Write the notes in English, translating whatever was said in another language, with "
    "names in English letters. Use only what this part says. Do not invent anything and do "
    "not write an introduction or a conclusion."
)

# Asked only to "keep everything", models returned condensed notes as long as
# their input, so the loop stalled above the chunk size. The word budget (half a
# chunk, at ~6 characters a word) is what makes each round actually shrink.
CONDENSE_PROMPT = (
    "Below are notes taken from consecutive parts of a single meeting, in order. Merge them into "
    "one set of notes under the same labels, in at most {words} words. Keep every speaker name, "
    "decision, and action item with its owner and due date; shorten the discussion points first."
)

MERGE_HEADER = (
    "Below are notes taken from consecutive parts of a single meeting, in order. A speaker label "
    "means the same person in every part; the Speakers notes say who they are. "
    "Write the final Minutes of Meeting covering the whole meeting.\n\n"
)

_HEADING = re.compile(r"^#{1,3}\s+(.+?)\s*#*\s*$", re.MULTILINE)
_THINKING = re.compile(r"^\s*<think>.*?</think>\s*", re.DOTALL)
_FENCE = re.compile(r"^```[a-zA-Z]*\n(.*)\n```$", re.DOTALL)


def get_llm_client(config: dict) -> OpenAICompatibleLLM:
    return OpenAICompatibleLLM(
        base_url=(config.get("llm_base_url") or "").strip(),
        api_key=(config.get("llm_api_key") or "").strip(),
        model=(config.get("llm_model") or "").strip(),
        temperature=_number(config.get("llm_temperature"), 0.2),
        max_tokens=int(_number(config.get("llm_max_output_tokens"), 4000)),
        timeout=int(_number(config.get("llm_timeout_seconds"), 300)),
    )


def generate_minutes(
    client: OpenAICompatibleLLM,
    transcript: str,
    prompt: str,
    chunk_chars: int,
    on_progress: Callable[[int, int], None] | None = None,
) -> str:
    """Summarise a transcript of any length.

    Transcripts that exceed the chunk size are summarised part by part and then
    merged, so meeting length is not capped by the model's context window.
    """
    chunks = split_text(transcript, chunk_chars)
    if len(chunks) == 1:
        return _write_minutes(client, prompt, f"Transcript:\n\n{transcript}")

    notes = []
    for index, chunk in enumerate(chunks, start=1):
        notes.append(f"### Part {index} of {len(chunks)}\n{client.complete(PART_PROMPT, chunk)}")
        if on_progress:
            on_progress(index, len(chunks))

    # The notes of a long meeting can outgrow the chunk size themselves; sent in
    # one request they would overflow the same context window the split avoids.
    # Condense them in groups until they fit, unless a round stops shrinking them.
    combined = "\n\n".join(notes)
    condense = CONDENSE_PROMPT.format(words=max(100, chunk_chars // 12))
    while len(combined) > chunk_chars:
        groups = split_text(combined, chunk_chars)
        if len(groups) >= len(notes):
            break
        notes = [
            f"### Notes {index} of {len(groups)}\n{client.complete(condense, group)}"
            for index, group in enumerate(groups, start=1)
        ]
        combined = "\n\n".join(notes)
    return _write_minutes(client, prompt, MERGE_HEADER + combined)


def _write_minutes(client: OpenAICompatibleLLM, prompt: str, content: str) -> str:
    """The final call, asked once more if it skipped a section the prompt requires.

    Smaller models drop a section now and then, most often Action Items when
    there are few. The required sections are the prompt's own "## " headings,
    so a prompt rewritten in Settings is checked against its own structure, and
    one without headings is not checked at all.
    """
    minutes = clean_answer(client.complete(prompt, content))
    missing = missing_sections(prompt, minutes)
    if missing:
        reminder = (f"\n\nYour previous answer left out these sections: {', '.join(missing)}. "
                    "Write the complete minutes again, with every section.")
        minutes = clean_answer(client.complete(prompt, content + reminder))
    return minutes


def missing_sections(prompt: str, minutes: str) -> list[str]:
    required = _HEADING.findall(prompt)
    present = {h.lower() for h in _HEADING.findall(minutes)}
    return [h for h in required if h.lower() not in present]


def clean_answer(text: str) -> str:
    """Drop an inline <think> block and a fence around the whole answer.

    Some local models still put their reasoning in the answer rather than in a
    separate field, and smaller ones like to wrap Markdown in ```markdown.
    """
    text = _THINKING.sub("", text.strip())
    fenced = _FENCE.match(text)
    return (fenced.group(1) if fenced else text).strip()


def _number(value, fallback: float) -> float:
    try:
        return float(value)
    except (TypeError, ValueError):
        return fallback
