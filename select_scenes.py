import json
from openai import OpenAI

SYSTEM = "Ты редактор YouTube Shorts. Находишь лучшие моменты в транскриптах российских сериалов."

PROMPT = """Найди 5-7 лучших моментов для YouTube Shorts.

Видео: {duration_sec:.0f} секунд ({duration_min:.1f} минут).
start и end — СЕКУНДЫ (float), строго от 0 до {duration_sec:.0f}.

━━━ ОПРЕДЕЛЕНИЕ ГРАНИЦ СЦЕН ━━━
В транскрипте отмечены паузы: [··· ПАУЗА 3.2с ···]
Паузы > 1.5с = смена сцены, музыкальная отбивка, монтажный переход.
ПРАВИЛА:
• Начинай клип ПОСЛЕ паузы — не с середины сцены
• Заканчивай клип ПЕРЕД следующей паузой — не обрывай на полуслове
• Включай ВЕСЬ контекст: завязку + развитие + пуант + реакцию
• Если шутка — нужен и сетап, и пуант, и смех/реакция — всё вместе
• Лучше взять чуть больше контекста, чем потерять смысл

━━━ ТРЕБОВАНИЯ К СЦЕНЕ ━━━
• Длительность: 45-90 секунд
• Цепляет с первых 3 секунд
• Законченная история — понятное начало и конец
• Эмоционально насыщенная: смешно, драматично, узнаваемо

Транскрипт:
{transcript}

Верни JSON:
{{"scenes": [
  {{
    "start": 125.3,
    "end": 198.0,
    "hook": "первая фраза/действие с которого начнётся видео",
    "description": "что происходит в сцене целиком — завязка, развитие, финал (3-5 предложений)",
    "why_shorts": "почему сработает в Shorts (1 предложение)"
  }}
]}}

Только JSON, без пояснений."""

PAUSE_THRESHOLD = 1.5  # seconds of silence that add a pause marker


def build_transcript(segments: list[dict]) -> str:
    """Format the transcript with pause markers between segments."""
    lines = []
    prev_end = 0.0
    for seg in segments:
        gap = seg['start'] - prev_end
        if gap > PAUSE_THRESHOLD and prev_end > 0:
            lines.append(f"  [··· ПАУЗА {gap:.1f}с ···]")
        m, s = divmod(int(seg['start']), 60)
        lines.append(f"[{m}:{s:02d} / {seg['start']:.1f}s] {seg['text']}")
        prev_end = seg['end']
    return '\n'.join(lines)


def select_scenes(segments: list[dict], api_key: str,
                  on_chunk=None, on_prompt=None) -> list[dict]:
    client = OpenAI(api_key=api_key)

    duration = segments[-1]['end'] if segments else 0
    transcript = build_transcript(segments)
    prompt = PROMPT.format(
        transcript=transcript,
        duration_sec=duration,
        duration_min=duration / 60
    )

    if on_prompt:
        on_prompt({'model': 'gpt-4o', 'system': SYSTEM, 'prompt': prompt})

    def _ask(messages, temp):
        stream = client.chat.completions.create(
            model='gpt-4o',
            messages=messages,
            response_format={'type': 'json_object'},
            temperature=temp,
            stream=True
        )
        full = ''
        for chunk in stream:
            text = chunk.choices[0].delta.content or ''
            full += text
            if on_chunk and text:
                on_chunk(text)
        return full

    def _validate(scenes):
        out = []
        for s in scenes:
            s['start'] = round(max(0.0, float(s['start'])), 1)
            s['end'] = round(min(float(duration), float(s['end'])), 1)
            d = s['end'] - s['start']
            if 20 <= d <= 150:
                out.append(s)
        return out

    messages = [
        {'role': 'system', 'content': SYSTEM},
        {'role': 'user', 'content': prompt}
    ]

    full = _ask(messages, temp=0.3)
    data = json.loads(full)
    scenes = data.get('scenes') or list(data.values())[0]
    valid = _validate(scenes)

    # Retry once with a direct instruction if fewer than 3 scenes are valid
    if len(valid) < 3:
        if on_chunk:
            on_chunk(f"\n\n[Retry: only {len(valid)} valid scenes, asking again]\n\n")
        retry_msg = (
            f"Ты вернул {len(valid)} валидных сцен. Это мало — нужно МИНИМУМ 5.\n"
            f"Видео длится {duration:.0f} секунд — там точно есть как минимум 5 разных "
            f"законченных моментов длительностью 45-90 секунд каждый.\n"
            f"Не давай одну огромную сцену на полэпизода — это плохой Shorts.\n"
            f"Верни заново JSON с 5-7 РАЗНЫМИ короткими сценами."
        )
        messages.append({'role': 'assistant', 'content': full})
        messages.append({'role': 'user', 'content': retry_msg})
        full2 = _ask(messages, temp=0.6)
        data2 = json.loads(full2)
        scenes2 = data2.get('scenes') or list(data2.values())[0]
        valid2 = _validate(scenes2)
        # Keep the better result
        if len(valid2) > len(valid):
            valid = valid2

    return valid
