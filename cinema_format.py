"""
Cinema-style subtitle formatter via GPT-4o.

Takes a punctuated word stream from one scene and returns a list of subtitle
"instances" - each instance is one on-screen frame (1 or 2 lines). The prompt
forbids speaker dashes; render.py also strips any leading dash it gets back.

Caching is done by the caller (render.py) using a hash of the input.
"""
import json
from openai import OpenAI

MODEL = "gpt-4o"

SYSTEM = (
    "You are a cinematic subtitle formatter for short vertical videos "
    "(YouTube Shorts / TikTok / Reels). Output strict JSON only."
)

PROMPT = """Сформируй кинематографичные субтитры из транскрипта сцены.

Правила:
1. Каждый "instance" (один кадр субтитра) = 1 или 2 строки. Никогда не 3 и больше.
2. Каждая строка ≤ 23 непробельных символа.
3. Общая длина instance ≤ 46 непробельных символов.
4. Не сшивай в одну строку слова из разных предложений без правильной пунктуации.
5. Хронологию соблюдай ЧЁТКО:
   - start = время первого слова инстанса
   - end = время последнего слова инстанса + 0.2-0.5 с естественного "выдыха"
   - КРИТИЧЕСКИ ВАЖНО: instances НЕ должны пересекаться. end[i] ≤ start[i+1].
6. Короткие реплики (2-3 слова) могут быть отдельным instance.
7. Не выкидывай слова. Не добавляй своих слов. Можешь чуть подправить пунктуацию (точка/запятая/?/!).
8. Не используй em-dash "—" и никакие другие префиксы реплик. Просто текст.

Пример монолога (один говорящий, длинная фраза):
{{
  "start": 30.20,
  "end": 32.40,
  "lines": [
    "Это два разных",
    "мира."
  ]
}}

Транскрипт слов из сцены (с timestamp):
{words_json}

Верни строго JSON-объект формы:
{{ "instances": [ ... ] }}

Только JSON. Без объяснений, без code fence.
"""


def cinema_format(clip_groups: list, api_key: str) -> list[dict]:
    """Return list of subtitle instances for the scene. May be empty."""
    words = []
    for g in clip_groups or []:
        for it in g.get('items', []):
            words.append({
                "t": it['t'],
                "start": round(float(it['start']), 2),
                "end": round(float(it['end']), 2),
            })

    if not words:
        return []

    client = OpenAI(api_key=api_key)
    resp = client.chat.completions.create(
        model=MODEL,
        messages=[
            {"role": "system", "content": SYSTEM},
            {"role": "user", "content": PROMPT.format(
                words_json=json.dumps(words, ensure_ascii=False)
            )},
        ],
        response_format={"type": "json_object"},
        temperature=0.2,
    )
    raw = resp.choices[0].message.content
    parsed = json.loads(raw)

    if isinstance(parsed, dict):
        for k in ("instances", "blocks", "subtitles", "result"):
            if k in parsed and isinstance(parsed[k], list):
                return parsed[k]
        return []
    if isinstance(parsed, list):
        return parsed
    return []
