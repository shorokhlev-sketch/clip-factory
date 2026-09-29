import hashlib
import json
import os
import subprocess
import tempfile
from pathlib import Path

SUB_Y = 1380         # about 72% down a 1920 px frame: below center, above the Shorts UI

# -- Style presets ------------------------------------------------------------
#
# render_mode dispatches into _ASS_BUILDERS:
#   'cinema'  = GPT-4o frames (cinema_format.py), cached per scene. On failure
#               a local heuristic merges up to 3 groups, max 46 non-space
#               chars, wrapped at 23.
#   'chunks'  = one group = one Dialogue line (classic clean style).
#   'karaoke' = word-by-word, active word highlighted in yellow.
#   'one_word'= one word at a time, large centered.
#
# Colors are AABBGGRR (AA=00 is fully opaque).

WHITE = '&H00FFFFFF'
YELLOW = '&H0000FFFF'
BLACK = '&H00000000'
SHADOW_HALF = '&H80000000'

STYLES = {
    'cinema': {
        'font': 'Helvetica', 'size': 60, 'bold': 1, 'italic': 0,
        'outline': 2.0, 'shadow': 1.0, 'render_mode': 'cinema',
        'primary': WHITE, 'outline_color': BLACK, 'shadow_color': SHADOW_HALF,
        # cinema tuning - invariant: hard cap 46 non-space chars per instance ->
        # always fits into 2 lines of <= 23 chars (never 3+).
        'max_groups': 3,             # try up to N adjacent groups, but...
        'instance_max_chars': 46,    # ...drop the next group if total would exceed this
        'line_chars': 23,            # wrap at this many non-whitespace chars per line
        'dialogue_gap': 0.4,         # gap (s) that starts a new speaker turn on a new line
        'gap_threshold': 2.0,        # gap (s) that breaks instance and hides subtitle
        'hold_after': 1.5,           # how long subtitle lingers after last word in long-gap case
    },
    'chunks': {
        'font': 'Helvetica', 'size': 72, 'bold': 1, 'italic': 0,
        'outline': 2.0, 'shadow': 1.0, 'render_mode': 'chunks',
        'primary': WHITE, 'outline_color': BLACK, 'shadow_color': SHADOW_HALF,
        'gap_threshold': 1.5, 'hold_after': 0.8,
    },
    'karaoke': {
        'font': 'Arial Black', 'size': 90, 'bold': 1, 'italic': 0,
        'outline': 4.0, 'shadow': 0.0, 'render_mode': 'karaoke',
        'primary': WHITE, 'outline_color': BLACK, 'shadow_color': BLACK,
        'highlight': YELLOW,
        'gap_threshold': 1.5, 'hold_after': 0.8,
    },
    'one_word': {
        'font': 'Arial Black', 'size': 130, 'bold': 1, 'italic': 0,
        'outline': 5.0, 'shadow': 0.0, 'render_mode': 'one_word',
        'primary': WHITE, 'outline_color': BLACK, 'shadow_color': BLACK,
        'gap_threshold': 1.5, 'hold_after': 0.8,
    },
}


def probe_display_size(video_path: Path) -> tuple[int, int]:
    r = subprocess.run(
        ['ffprobe', '-v', 'quiet', '-select_streams', 'v:0',
         '-show_entries', 'stream=width,height,sample_aspect_ratio',
         '-of', 'json', str(video_path)],
        capture_output=True, text=True
    )
    s = json.loads(r.stdout)['streams'][0]
    w, h = s['width'], s['height']
    sar = s.get('sample_aspect_ratio', '1:1')
    if sar and ':' in str(sar) and sar not in ('0:1', 'N/A', '1:1'):
        n, d = map(int, sar.split(':'))
        w = int(w * n / d)
    return w, h


def _tc(s: float) -> str:
    h, rem = divmod(s, 3600)
    m, sec = divmod(rem, 60)
    cs = int((sec % 1) * 100)
    return f"{int(h)}:{int(m):02d}:{int(sec):02d}.{cs:02d}"


def _ass_header(style: dict, play_h: int = 1920) -> str:
    # Alignment=5 is middle-center (\pos sets the exact position)
    # WrapStyle=0 - smart wrap
    return (
        "[Script Info]\n"
        "ScriptType: v4.00+\n"
        "PlayResX: 1080\n"
        f"PlayResY: {play_h}\n"
        "WrapStyle: 0\n\n"
        "[V4+ Styles]\n"
        "Format: Name, Fontname, Fontsize, PrimaryColour, SecondaryColour, "
        "OutlineColour, BackColour, Bold, Italic, Underline, StrikeOut, "
        "ScaleX, ScaleY, Spacing, Angle, BorderStyle, Outline, Shadow, "
        "Alignment, MarginL, MarginR, MarginV, Encoding\n"
        f"Style: Default,{style['font']},{style['size']},{style['primary']},&H000000FF,"
        f"{style['outline_color']},{style['shadow_color']},"
        f"{style['bold']},{style['italic']},0,0,100,100,0,0,1,"
        f"{style['outline']},{style['shadow']},5,80,80,50,1\n\n"
        "[Events]\n"
        "Format: Layer, Start, End, Style, Name, MarginL, MarginR, MarginV, Effect, Text\n"
    )


def _make_ass_chunks(clip_groups: list[dict], clip_start: float, clip_end: float,
                     pos_y: int, style: dict) -> str:
    """chunks: one group (a punctuator line) = one Dialogue line."""
    header = _ass_header(style)
    lines = []
    pos = r'{\an5\pos(540,' + str(pos_y) + r')}'
    gap_threshold = style.get('gap_threshold', 1.5)
    hold_after    = style.get('hold_after', 0.8)

    for gi, group in enumerate(clip_groups):
        items = group.get('items', [])
        if not items:
            continue
        t0 = max(0.0, items[0]['start'] - clip_start)
        last_word_end_abs = items[-1]['end']
        next_start_abs = None
        if gi + 1 < len(clip_groups):
            next_items = clip_groups[gi + 1].get('items', [])
            if next_items:
                next_start_abs = next_items[0]['start']

        if next_start_abs is None:
            t1_abs = min(last_word_end_abs + hold_after, clip_end)
        else:
            gap = next_start_abs - last_word_end_abs
            t1_abs = next_start_abs if gap < gap_threshold else last_word_end_abs + hold_after
        t1 = min(clip_end - clip_start, t1_abs - clip_start)
        if t1 <= t0:
            continue
        text = ' '.join(item['t'] for item in items)
        lines.append(f"Dialogue: 0,{_tc(t0)},{_tc(t1)},Default,,0,0,0,,{pos}{text}")
    return header + '\n'.join(lines)


def _wrap_line(text: str, max_no_space: int) -> list[str]:
    """Greedy word wrap counting non-whitespace characters only."""
    words = text.split()
    out, cur, cur_count = [], [], 0
    for w in words:
        wl = len(w)  # word has no spaces inside
        if cur and cur_count + wl > max_no_space:
            out.append(' '.join(cur))
            cur, cur_count = [w], wl
        else:
            cur.append(w)
            cur_count += wl
    if cur:
        out.append(' '.join(cur))
    return out


def _group_text(group: dict) -> str:
    return ' '.join(it['t'] for it in group.get('items', []))


def _read_api_key() -> str:
    if os.environ.get('OPENAI_API_KEY'):
        return os.environ['OPENAI_API_KEY']
    cfg = Path.home() / '.config' / 'clip-factory' / 'key'
    if cfg.exists():
        return cfg.read_text().strip()
    raise RuntimeError("OpenAI API key not found (env OPENAI_API_KEY or ~/.config/clip-factory/key)")


def _cinema_blocks_cached(clip_groups: list[dict], job_dir: Path | None) -> list[dict]:
    """
    Ask GPT-4o (via cinema_format.cinema_format) to split the scene into
    cinema-instances. Cache by SHA1 of normalized clip_groups in
    <job_dir>/cinema_cache/<hash>.json.
    """
    key_data = json.dumps(clip_groups, ensure_ascii=False, sort_keys=True).encode('utf-8')
    cache_key = hashlib.sha1(key_data).hexdigest()[:12]

    cache_file: Path | None = None
    if job_dir is not None:
        cache_dir = Path(job_dir) / 'cinema_cache'
        cache_dir.mkdir(parents=True, exist_ok=True)
        cache_file = cache_dir / f'{cache_key}.json'
        if cache_file.exists():
            try:
                return json.loads(cache_file.read_text())
            except Exception:
                pass  # corrupt cache -> regenerate

    if os.environ.get('DEMO_MODE', '').lower() in ('1', 'true', 'yes'):
        raise RuntimeError("demo: no GPT")  # caller falls back to the heuristic

    from cinema_format import cinema_format
    api_key = _read_api_key()
    blocks = cinema_format(clip_groups, api_key)

    if cache_file is not None:
        cache_file.write_text(json.dumps(blocks, ensure_ascii=False, indent=2))
    return blocks


def _cinema_blocks_heuristic(clip_groups: list[dict], style: dict) -> list[dict]:
    """Local fallback if GPT path fails. Same algorithm as the previous version."""
    max_groups          = style.get('max_groups', 3)
    instance_max_chars  = style.get('instance_max_chars', 46)
    line_chars          = style.get('line_chars', 23)
    dialogue_gap        = style.get('dialogue_gap', 0.4)
    hold_after          = style.get('hold_after', 1.5)
    gap_threshold       = style.get('gap_threshold', 2.0)

    def _bucket_chars(bucket):
        return sum(len(it['t']) for g in bucket for it in g.get('items', []))

    instances = []
    i = 0
    while i < len(clip_groups):
        if not clip_groups[i].get('items'):
            i += 1; continue
        bucket = [clip_groups[i]]
        j = i + 1
        while j < len(clip_groups) and len(bucket) < max_groups:
            if not clip_groups[j].get('items'):
                j += 1; continue
            prev_end = bucket[-1]['items'][-1]['end']
            cur_start = clip_groups[j]['items'][0]['start']
            if cur_start - prev_end >= gap_threshold:
                break
            candidate = bucket + [clip_groups[j]]
            if _bucket_chars(candidate) > instance_max_chars:
                break
            bucket.append(clip_groups[j]); j += 1
        instances.append(bucket); i = j

    out = []
    for bucket in instances:
        first_word_start = bucket[0]['items'][0]['start']
        last_word_end    = bucket[-1]['items'][-1]['end']

        speaker_turns = [[bucket[0]]]
        for k in range(1, len(bucket)):
            prev_end = bucket[k - 1]['items'][-1]['end']
            cur_start = bucket[k]['items'][0]['start']
            (speaker_turns.append([bucket[k]]) if cur_start - prev_end >= dialogue_gap
             else speaker_turns[-1].append(bucket[k]))

        rendered_lines = []
        for turn in speaker_turns:
            turn_text = ' '.join(_group_text(g) for g in turn).strip()
            for ln in _wrap_line(turn_text, line_chars):
                rendered_lines.append(ln)

        out.append({
            "start": first_word_start,
            "end": last_word_end + hold_after,
            "lines": rendered_lines,
        })
    return out


def _make_ass_cinema(clip_groups: list[dict], clip_start: float, clip_end: float,
                     pos_y: int, style: dict, *, job_dir: Path | None = None) -> str:
    """
    cinema (v3): blocks come from GPT-4o (cinema_format.py) with per-scene cache.
    On any failure we fall back to the local heuristic so render never breaks.
    """
    header = _ass_header(style)
    lines = []
    pos = r'{\an5\pos(540,' + str(pos_y) + r')}'

    blocks: list[dict] = []
    try:
        blocks = _cinema_blocks_cached(clip_groups, job_dir)
    except Exception as e:
        print(f"[cinema] GPT path failed, falling back to heuristic: {e}")
    if not blocks:
        blocks = _cinema_blocks_heuristic(clip_groups, style)

    # 1. Strip a leading speaker dash if GPT adds one. The prompt forbids it.
    for b in blocks:
        b['lines'] = [str(l).lstrip('\u2014').lstrip().rstrip() for l in (b.get('lines') or []) if str(l).strip()]

    # 2. Re-derive timing from actual word timestamps.
    #    GPT segments the text well but is sloppy with start/end; we trust the
    #    Whisper word timeline instead. Words are consumed sequentially across
    #    blocks (GPT must not reorder or invent words).
    flat_words = [it for g in clip_groups for it in g.get('items', [])]
    idx = 0
    realigned = []
    for b in blocks:
        joined = ' '.join(b['lines']).strip()
        word_count = len([w for w in joined.split() if w])
        if word_count == 0:
            continue
        available = len(flat_words) - idx
        if available <= 0:
            break
        take = min(word_count, available)
        first = flat_words[idx]
        last  = flat_words[idx + take - 1]
        b['start'] = float(first['start'])
        b['end']   = float(last['end']) + 0.25  # tiny natural tail
        idx += take
        realigned.append(b)
    blocks = realigned

    # 3. Final pass: clamp end[i] <= start[i+1] so frames never overlap.
    for i in range(len(blocks) - 1):
        try:
            if blocks[i]['end'] > blocks[i + 1]['start']:
                blocks[i]['end'] = blocks[i + 1]['start']
        except (KeyError, ValueError, TypeError):
            continue

    for block in blocks:
        try:
            start_abs = float(block['start'])
            end_abs   = float(block['end'])
            block_lines = [str(x) for x in (block.get('lines') or []) if str(x).strip()]
        except (KeyError, ValueError, TypeError):
            continue
        if not block_lines:
            continue
        t0 = max(0.0, start_abs - clip_start)
        t1 = min(clip_end - clip_start, end_abs - clip_start)
        if t1 <= t0:
            continue
        text = r'\N'.join(block_lines)
        lines.append(f"Dialogue: 0,{_tc(t0)},{_tc(t1)},Default,,0,0,0,,{pos}{text}")

    return header + '\n'.join(lines)


def _make_ass_karaoke(clip_groups: list[dict], clip_start: float, clip_end: float,
                      pos_y: int, style: dict) -> str:
    """karaoke: word by word inside a group, the active word in yellow."""
    header = _ass_header(style)
    lines = []
    pos = r'{\an5\pos(540,' + str(pos_y) + r')}'
    primary = style['primary']
    highlight = style.get('highlight', YELLOW)
    gap_threshold = style.get('gap_threshold', 1.5)
    hold_after    = style.get('hold_after', 0.8)

    for gi, group in enumerate(clip_groups):
        items = group.get('items', [])
        if not items:
            continue
        last_word_end_abs = items[-1]['end']
        next_start_abs = None
        if gi + 1 < len(clip_groups):
            next_items = clip_groups[gi + 1].get('items', [])
            if next_items:
                next_start_abs = next_items[0]['start']

        if next_start_abs is None:
            group_end_abs = min(last_word_end_abs + hold_after, clip_end)
        else:
            gap = next_start_abs - last_word_end_abs
            group_end_abs = next_start_abs if gap < gap_threshold else last_word_end_abs + hold_after
        group_end_t = group_end_abs - clip_start

        for wi, active in enumerate(items):
            t0 = max(0.0, active['start'] - clip_start)
            if wi + 1 < len(items):
                t1 = max(t0 + 0.05, items[wi + 1]['start'] - clip_start)
            else:
                t1 = group_end_t
            t1 = min(clip_end - clip_start, t1)
            if t1 <= t0:
                continue

            parts = []
            for i, item in enumerate(items):
                text = item['t']
                if i == wi:
                    parts.append(r'{\c' + highlight + r'}' + text + r'{\c' + primary + r'}')
                else:
                    parts.append(text)
            display = ' '.join(parts)
            lines.append(f"Dialogue: 0,{_tc(t0)},{_tc(t1)},Default,,0,0,0,,{pos}{display}")
    return header + '\n'.join(lines)


def _make_ass_one_word(clip_groups: list[dict], clip_start: float, clip_end: float,
                       pos_y: int, style: dict) -> str:
    """one_word: one word at a time, large, centered."""
    header = _ass_header(style)
    lines = []
    pos = r'{\an5\pos(540,' + str(pos_y) + r')}'
    gap_threshold = style.get('gap_threshold', 1.5)
    hold_after    = style.get('hold_after', 0.8)

    flat = []
    for g in clip_groups:
        for it in g.get('items', []):
            flat.append(it)

    for wi, item in enumerate(flat):
        t0 = max(0.0, item['start'] - clip_start)
        next_start_abs = flat[wi + 1]['start'] if wi + 1 < len(flat) else None
        last_word_end_abs = item['end']

        if next_start_abs is None:
            t1_abs = min(last_word_end_abs + hold_after, clip_end)
        else:
            gap = next_start_abs - last_word_end_abs
            t1_abs = next_start_abs if gap < gap_threshold else last_word_end_abs + hold_after
        t1 = min(clip_end - clip_start, t1_abs - clip_start)
        if t1 <= t0:
            continue
        text = item['t']
        lines.append(f"Dialogue: 0,{_tc(t0)},{_tc(t1)},Default,,0,0,0,,{pos}{text}")
    return header + '\n'.join(lines)




def _make_ass_segments_fallback(segments: list[dict], clip_start: float, clip_end: float,
                                pos_y: int, style: dict) -> str:
    """Fallback without clip_groups: segment-level subtitles."""
    header = _ass_header(style)
    in_range = [s for s in segments if s['end'] > clip_start and s['start'] < clip_end]
    lines = []
    pos = r'{\an5\pos(540,' + str(pos_y) + r')}'
    for seg in in_range:
        t0 = max(0.0, seg['start'] - clip_start)
        t1 = min(clip_end - clip_start, seg['end'] - clip_start)
        lines.append(f"Dialogue: 0,{_tc(t0)},{_tc(t1)},Default,,0,0,0,,{pos}{seg['text'].strip()}")
    return header + '\n'.join(lines)


_ASS_BUILDERS = {
    'cinema': _make_ass_cinema,
    'chunks': _make_ass_chunks,
    'karaoke': _make_ass_karaoke,
    'one_word': _make_ass_one_word,
}


def render_clip(video_path: Path, scene: dict, segments: list[dict],
                output_path: Path,
                clip_groups: list[dict] = None,
                style: str = 'cinema',
                preview: bool = False):
    """
    style: cinema | chunks | karaoke | one_word
    preview: True -> fast render (5 s, 480p, no audio) to check a style
    """
    if style not in STYLES:
        raise ValueError(f"Unknown style: {style}")
    style_cfg = STYLES[style]

    # Cut exactly at the scene edges, with no buffer at either end.
    # (app.PRE_BUFFER only widens the word window for punctuation.)
    start = max(0.0, scene['start'])
    end = scene['end']
    if preview:
        # 5-second window centred on the middle of the scene - gives a more
        # representative slice for evaluating subtitle style than the first 5
        # seconds (which often catches the lead-in silence / setup line).
        scene_start = scene['start']
        scene_end = scene['end']
        scene_duration = scene_end - scene_start
        if scene_duration <= 5.0:
            # Scene is shorter than preview window - render the whole thing.
            start = scene_start
            end = scene_end
        else:
            mid = (scene_start + scene_end) / 2.0
            start = max(0.0, mid - 2.5)
            end = min(scene_end, start + 5.0)
    duration = end - start

    if preview:
        TARGET_W, TARGET_H = 480, 854
    else:
        TARGET_W, TARGET_H = 1080, 1920

    dw, dh = probe_display_size(video_path)
    src_ratio = dw / dh

    if src_ratio >= TARGET_W / TARGET_H:
        vf_crop = (
            "scale=trunc(iw*sar/2)*2:ih,setsar=1,"
            f"crop=trunc(ih*{TARGET_W}/{TARGET_H}/2)*2:ih:(iw-trunc(ih*{TARGET_W}/{TARGET_H}/2)*2)/2:0,"
            f"scale={TARGET_W}:{TARGET_H}"
        )
        pos_y = int(SUB_Y * TARGET_H / 1920)
    else:
        content_h = int(TARGET_W / src_ratio / 2) * 2
        pad_y = (TARGET_H - content_h) // 2
        vf_crop = (
            "scale=trunc(iw*sar/2)*2:ih,setsar=1,"
            f"scale={TARGET_W}:{content_h},"
            f"pad={TARGET_W}:{TARGET_H}:0:{pad_y}:black"
        )
        pos_y = pad_y + content_h - int(120 * TARGET_H / 1920)

    # ASS uses PlayResY=1920 coordinates and libass scales them to the output.
    # So pos_y is relative to 1920 (PlayResY), not TARGET_H.
    ass_pos_y = SUB_Y
    if not (src_ratio >= TARGET_W / TARGET_H):
        # Letterbox: recompute pos_y for PlayResY=1920
        content_h_1920 = int(1080 / src_ratio / 2) * 2
        pad_y_1920 = (1920 - content_h_1920) // 2
        ass_pos_y = pad_y_1920 + content_h_1920 - 120

    # Pick the ASS builder
    render_mode = style_cfg['render_mode']
    if clip_groups:
        builder = _ASS_BUILDERS.get(render_mode)
        builder_kwargs = {}
        if render_mode == 'cinema':
            builder_kwargs['job_dir'] = output_path.parent
        ass_content = builder(clip_groups, start, end, ass_pos_y, style_cfg, **builder_kwargs)
    else:
        ass_content = _make_ass_segments_fallback(segments, start, end, ass_pos_y, style_cfg)

    with tempfile.NamedTemporaryFile(
        mode='w', suffix='.ass', delete=False, encoding='utf-8'
    ) as f:
        f.write(ass_content)
        ass_path = f.name

    vf = f"setpts=PTS-STARTPTS,{vf_crop},ass={ass_path}"

    ff_preset = 'ultrafast' if preview else 'fast'
    audio_args = ['-an'] if preview else ['-c:a', 'aac', '-b:a', '128k']

    result = subprocess.run(
        ['ffmpeg', '-y',
         '-ss', str(start), '-i', str(video_path), '-t', str(duration),
         '-vf', vf,
         '-c:v', 'libx264', '-preset', ff_preset, '-crf', '23',
         *audio_args,
         str(output_path)],
        capture_output=True
    )

    Path(ass_path).unlink(missing_ok=True)

    if result.returncode != 0:
        raise RuntimeError(result.stderr.decode()[-800:])
