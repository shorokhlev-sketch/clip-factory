"""
Typo check on the Whisper transcript.
Finds non-existent words and clear recognition errors.
Leaves correct words alone and fixes only obvious mistakes.
"""
import json
import re
from openai import OpenAI

MODEL = 'gpt-4o-mini'

SYSTEM = ("Ты редактор транскрипции. Получаешь автоматически распознанный текст. "
          "Находишь несуществующие или явно неверно распознанные слова и предлагаешь правильные.")

PROMPT = """Просканируй транскрипцию и найди явные ошибки автораспознавания.

ЧТО ИСКАТЬ:
• Несуществующие слова (например "бесcoботно" вместо "беззаботно")
• Слова, не подходящие по контексту (омофоны: "пора" / "пара")
• Явные опечатки

ЧТО НЕ ТРОГАТЬ:
• Корректные слова с любой грамматикой
• Имена собственные
• Разговорные/сленговые слова
• Слова правильные, но звучащие странно — оставь как есть, если сомневаешься

Транскрипция:
{transcript}

Верни JSON со списком исправлений. Если ошибок нет — пустой список.

{{"corrections": [
  {{"wrong": "бесcoботно", "right": "беззаботно", "context": "учились в универе, время летело быстро и бесcoботно"}},
  {{"wrong": "пара", "right": "пора", "context": "Уже пара спать"}}
]}}

Только JSON."""


def sanity_check_transcript(segments: list[dict], api_key: str,
                            on_chunk=None, on_prompt=None) -> list[dict]:
    """
    Returns [{wrong, right, context}].
    Returns an empty list if nothing is found or the call fails.
    """
    if not segments:
        return []

    client = OpenAI(api_key=api_key)
    transcript = '\n'.join(seg['text'].strip() for seg in segments)
    prompt = PROMPT.format(transcript=transcript)

    if on_prompt:
        on_prompt({'model': MODEL, 'system': SYSTEM, 'prompt': prompt})

    try:
        stream = client.chat.completions.create(
            model=MODEL,
            messages=[
                {'role': 'system', 'content': SYSTEM},
                {'role': 'user', 'content': prompt},
            ],
            response_format={'type': 'json_object'},
            temperature=0.1,
            stream=True,
        )

        full = ''
        for chunk in stream:
            text = chunk.choices[0].delta.content or ''
            full += text
            if on_chunk and text:
                on_chunk(text)

        data = json.loads(full)
        corrections = data.get('corrections', [])
        return corrections if isinstance(corrections, list) else []

    except Exception as e:
        print(f"[sanity_check] Error: {e}")
        return []


def apply_corrections(segments: list[dict], words: list[dict],
                      corrections: list[dict]) -> tuple[list[dict], list[dict]]:
    """Replace wrong -> right in segment and word texts. Timestamps stay the same."""
    if not corrections:
        return segments, words

    def replace_word_only(text: str, wrong: str, right: str) -> str:
        # Replace whole words only and keep the case of the first letter
        pattern = r'\b' + re.escape(wrong) + r'\b'

        def _repl(m):
            matched = m.group(0)
            if matched and matched[0].isupper():
                return right[0].upper() + right[1:]
            return right
        return re.sub(pattern, _repl, text, flags=re.IGNORECASE | re.UNICODE)

    new_segments = []
    for seg in segments:
        text = seg['text']
        for c in corrections:
            text = replace_word_only(text, c['wrong'], c['right'])
        new_segments.append({**seg, 'text': text})

    new_words = []
    for w in words:
        word = w['word']
        for c in corrections:
            word = replace_word_only(word, c['wrong'], c['right'])
        new_words.append({**w, 'word': word})

    return new_segments, new_words
