"""
Per-scene punctuation and line grouping.
GPT returns text line by line. Lines are matched back to words by a normalized form.
"""
import json
import re
from openai import OpenAI

MODEL = 'gpt-4o-mini'

SYSTEM = ("Ты пунктуатор русских субтитров. Получаешь поток слов из автотранскрипции, "
          "расставляешь пунктуацию по правилам русского языка, разбиваешь на короткие строки-субтитры.")

PROMPT = """Расставь пунктуацию и сгруппируй слова в субтитр-строки.

━━━ ОСНОВНЫЕ ПРАВИЛА ━━━
1. Можно добавлять знаки и заглавные буквы. Слова не переставляй.
2. Каждая строка = одна группа субтитра, 2-4 слова, до ~22 символов.
3. Точка/?/! ВСЕГДА заканчивает строку. Не объединяй разные предложения.
4. Заглавная буква — начало предложения, имена собственные, "Я".

━━━ ВЫБРАСЫВАЙ из вывода ━━━
НЕ включай в строки токены, которые НЕ являются репликами/речью персонажей:
• Описания звуков: МУЗЫКА, ЗАСТАВКА, СМЕХ, АПЛОДИСМЕНТЫ, ВЗДОХ, ШУМ, КРИК, ПЛАЧ, ВЫСТРЕЛ
• Описания действий: [идёт], [уходит], [плачет], (за кадром)
• Авторские/режиссёрские пометки: написанные капсом 2+ слова подряд, обычно описывающие
  не диалог, а саундтрек или сценическое действие («ДИНАМИЧНАЯ МУЗЫКА», «МУЗЫКАЛЬНАЯ ЗАСТАВКА»)
• Закадровые надписи и титры

ВКЛЮЧАЙ — обычные слова диалога даже если они в верхнем регистре по контексту:
аббревиатуры (США, СССР, ИИ), эмфатический крик внутри реплики («ДА!»), местоимение «Я».

━━━ ТИРЕ И ДЕФИС (правила русского) ━━━
• Тире — (em-dash): между подлежащим и сказуемым-существительным («Москва — столица»),
  при прямой речи, в неполных предложениях («— Привет», «Я — в магазин»).
• Дефис - (короткий): только внутри сложных слов («кто-то», «по-русски», «когда-нибудь»),
  никогда не отдельно.
• Различай: тире окружено пробелами с обеих сторон, дефис без пробелов.

━━━ КОНЕЦ СТРОКИ ━━━
В самом КОНЦЕ строки разрешены только `!` и `?`. Точку, запятую, двоеточие, тире —
НЕ ставь как последний символ.

━━━ СЛОВА ━━━
{words_flat}

Верни JSON: {{"lines": ["Всем привет!", "Я Миша,", "а это Оля"]}}.
Только JSON."""


def _clean(s: str) -> str:
    return re.sub(r'[^\w]+', '', s, flags=re.UNICODE).lower()


def _strip_trailing_punct(text: str) -> str:
    """Strip trailing punctuation except ! and ?: periods, commas, dashes, colons."""
    return re.sub(r'[.,;:\u2014\u2013\-]+\s*$', '', text)


def _enforce_sentence_breaks(lines: list[str]) -> list[str]:
    """
    Split lines after . ? and ! in case GPT ignored the rule.
    Two sentences never end up on one line.
    """
    out = []
    for line in lines:
        # Split after .!? (lookbehind keeps the mark)
        parts = re.split(r'(?<=[.!?])\s+', line.strip())
        for p in parts:
            p = p.strip()
            if p:
                out.append(p)
    return out


LOOKAHEAD = 15   # words to look ahead for a match, to skip sound tags GPT dropped


def _map_lines_to_words(lines: list[str], clip_words: list[dict]) -> list[dict]:
    """
    GPT may drop words on purpose (sound tags such as MUSIC, stage notes).
    Match by normalized form with a wide lookahead and skip the dropped words.
    """
    groups = []
    word_idx = 0
    n = len(clip_words)

    for raw_line in lines:
        line = _strip_trailing_punct(raw_line.strip())
        if not line:
            continue
        tokens = line.split()
        items = []
        for tok in tokens:
            if word_idx >= n:
                break
            clean_tok = _clean(tok)
            if not clean_tok:
                continue
            # Wide lookahead: GPT may have dropped tags between lines of dialogue
            found = -1
            for lookahead in range(min(LOOKAHEAD, n - word_idx)):
                if _clean(clip_words[word_idx + lookahead]['word']) == clean_tok:
                    found = word_idx + lookahead
                    break
            if found < 0:
                # No match: keep the original word and advance by one
                tok = clip_words[word_idx]['word']
                found = word_idx
            items.append({
                'i': found,
                't': tok,
                'start': clip_words[found]['start'],
                'end': clip_words[found]['end'],
            })
            word_idx = found + 1
        if items:
            groups.append({'items': items})

    # Unmatched trailing words were dropped by GPT on purpose (sound tags).
    # Do not append them, or tags like "MUSIC" show up at the end of the clip.

    return groups


def _fallback(clip_words: list[dict]) -> list[dict]:
    """Rule-based: 3 words per group, no punctuation."""
    groups, cur = [], []
    for i, w in enumerate(clip_words):
        cur.append({'i': i, 't': w['word'],
                    'start': w['start'], 'end': w['end']})
        if len(cur) >= 3:
            groups.append({'items': cur})
            cur = []
    if cur:
        groups.append({'items': cur})
    return groups


def punctuate_clip(clip_words: list[dict], api_key: str,
                   on_chunk=None, on_prompt=None) -> list[dict]:
    if not clip_words:
        return []

    client = OpenAI(api_key=api_key)
    words_flat = ' '.join(w['word'] for w in clip_words)
    prompt = PROMPT.format(words_flat=words_flat)

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
            temperature=0.2,
            stream=True,
        )

        full = ''
        for chunk in stream:
            text = chunk.choices[0].delta.content or ''
            full += text
            if on_chunk and text:
                on_chunk(text)

        data = json.loads(full)
        lines = data.get('lines') or list(data.values())[0]
        if not isinstance(lines, list) or not lines:
            raise ValueError("Empty or invalid lines")

        # Hard break at sentence ends: GPT sometimes ignores the rule
        lines = _enforce_sentence_breaks(lines)

        return _map_lines_to_words(lines, clip_words)

    except Exception as e:
        print(f"[punctuate_clip] Error: {e}, using fallback")
        return _fallback(clip_words)
