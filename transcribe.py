import subprocess
import tempfile
from pathlib import Path
from openai import OpenAI


def transcribe_video(video_path: Path, api_key: str) -> dict:
    """
    Returns {'segments': [{start, end, text}], 'words': [{start, end, word}]}
    Segments used for transcript display, words for word-by-word subtitles.
    """
    client = OpenAI(api_key=api_key)

    with tempfile.NamedTemporaryFile(suffix='.mp3', delete=False) as tmp:
        audio_path = tmp.name

    r = subprocess.run(
        ['ffmpeg', '-y', '-i', str(video_path),
         '-vn', '-ar', '16000', '-ac', '1', '-b:a', '32k', audio_path],
        capture_output=True
    )
    if r.returncode != 0:
        Path(audio_path).unlink(missing_ok=True)
        raise RuntimeError(r.stderr.decode()[-800:])

    with open(audio_path, 'rb') as f:
        result = client.audio.transcriptions.create(
            model='whisper-1',
            file=f,
            response_format='verbose_json',
            timestamp_granularities=['word', 'segment'],
            language='ru'
        )

    Path(audio_path).unlink(missing_ok=True)

    segments = [
        {'start': s.start, 'end': s.end, 'text': s.text.strip()}
        for s in (result.segments or [])
    ]
    words = [
        {'start': w.start, 'end': w.end, 'word': w.word.strip()}
        for w in (result.words or [])
        if w.word.strip()
    ]

    # punctuate_clip drops sound tags like "MUSIC" through a prompt rule, so
    # acronyms in real dialogue are not filtered out by mistake.

    return {'segments': segments, 'words': words}
