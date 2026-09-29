# CLAUDE.md

Instructions for coding agents working in this repository.

## What this is

Clip Factory (repo `clip-factory`) cuts long Russian-language episodes into 9:16 clips with burned-in subtitles. A FastAPI app runs an LLM pipeline (Whisper, GPT-4o-mini, GPT-4o) and renders with FFmpeg. The browser UI is one static HTML file.

## Project map

```
app.py             FastAPI app: upload, pipeline threads, SSE progress, scenes, render, queue, trim, banner, frames
transcribe.py      ffmpeg audio extract + whisper-1 -> {segments, words} with word timestamps (language='ru')
sanity_check.py    gpt-4o-mini finds ASR typos -> [{wrong, right, context}]; apply_corrections() rewrites segments and words
select_scenes.py   gpt-4o picks 5-7 scenes from a transcript with pause markers; validates 20-150 s; one retry if < 3 valid
punctuate_clip.py  gpt-4o-mini punctuates one scene and splits it into subtitle lines; lines are mapped back to word timestamps
cinema_format.py   gpt-4o splits a scene into 1-2 line subtitle frames for the "cinema" preset
render.py          ASS builders per preset + FFmpeg render (9:16 crop or letterbox, 1080x1920, libx264/aac)
postprocess.py     trim_clip, apply_banner, extract_frame (rendered clips; extract_frame also reads the source for scene thumbnails)
publish.py         CLI: post finished clips to a Telegram channel via Bot API; YouTube and VK are stubs
static/index.html  The whole UI: vanilla JS and CSS, no build step
start-demo.sh      Runs uvicorn plus a Cloudflare Quick Tunnel
output/<job_id>/   Per-episode state on disk (gitignored)
```

## Commands

```bash
python3 -m venv .venv && . .venv/bin/activate
pip install -r requirements.txt

# FFmpeg must include libass: `ffmpeg -filters | grep ass` must list the `ass` filter.
export OPENAI_API_KEY=sk-...        # or put the key in ~/.config/clip-factory/key

python -m uvicorn app:app --port 8765          # http://127.0.0.1:8765
DEMO_MODE=1 python -m uvicorn app:app --port 8765   # demo mode: upload, punctuation and queue render return 403, no key needed

python publish.py list
python publish.py post --job <job_id> --channel <name> --dry-run
```

Run uvicorn from the repo root. `output/` and `static/` are resolved relative to the working directory. A fresh clone has no sessions, because `output/` is gitignored. The first upload of a 23 min episode costs about $0.27. `DEMO_MODE=1` only browses sessions already in `output/`.

There is no test suite. Verify changes by running the app and walking through a cached session (Reopen recent session on Step 01). Pipeline stages that call OpenAI cost money: do not trigger uploads or punctuation in automated checks unless asked.

## Pipeline contract

`job_id = sha1(video bytes)[:8]`. Uploading the same file again reuses `output/<job_id>/` and every cache in it. Each stage skips work if its file exists. To re-run a stage, delete its file.

| File | Written by | Shape |
|---|---|---|
| `<source>.mp4` (any of mp4, mkv, mov, avi, webm, m4v) | upload | original video |
| `transcript.json` | transcribe | `[{start, end, text}]`, seconds |
| `words.json` | transcribe | `[{start, end, word}]` |
| `corrections.json` | sanity_check | `[{wrong, right, context}]` |
| `scenes.json` | select_scenes, UI edits | `[{id, start, end, duration, hook, description, why_shorts, reason, approved}]` |
| `clip_groups.json` | punctuate_clip, UI edits | `{"<scene_id>": [{items: [{i, t, start, end}]}]}` |
| `cinema_cache/<sha1[:12]>.json` | render (cinema) | GPT subtitle frames, keyed by hash of the scene's clip_groups |
| `clip_NN_shorts.mp4` | render | final 1080x1920 clip |
| `preview_<id>_<style>.mp4` | preview_style | 5 s, 480x854, no audio |
| `trimmed_*`, `bannered_*` | postprocess | derived clips; publish.py picks bannered_ > trimmed_ > base |
| `<clip>.caption.txt` | operator, optional | post caption for publish.py; falls back to the scene hook |
| `frames/*.png`, `assets/*`, `posted.json` | UI and publish.py | thumbnails, banner uploads, post log |

Rules the code relies on:

- Word timestamps are the only source of timing. LLM output supplies text and line breaks; timings are re-derived from `words.json` (see `_map_lines_to_words` and step 2 of `_make_ass_cinema`). Keep it that way.
- LLM output is never trusted: JSON response format, schema checks, range clamps and post-processing (`_enforce_sentence_breaks`, `_strip_trailing_punct`, overlap clamp). Punctuation and cinema layout fall back to rules on any exception (`_fallback`, `_cinema_blocks_heuristic`). The typo check returns no fixes on any exception. Scene picking has no fallback. A bad response ends the pipeline with an `error` event. New LLM steps must never block a render.
- Render cuts exactly at scene `start`/`end`. Punctuation reads words from 4 s before the scene start (`PRE_BUFFER` in app.py) for context.
- SSE: `GET /api/progress/{job_id}` streams events from `jobs[job_id]["events"]` and closes on `render_done` or `error`. Event types: `step`, `transcript_ready`, `corrections_found`, `gpt_request`, `gpt_chunk`, `gpt_complete`, `ready`, `punctuating`, `clip_punctuated`, `punctuate_done`, `render_progress`, `clip_done`, `render_done`, `error`.
- `jobs` is in-memory only. After a restart, sessions are rebuilt from disk by `_rehydrate()`.
- `DEMO_MODE=1` blocks upload, punctuation, scene and subtitle edits and single-scene render from the queue (HTTP 403 `demo_disabled`). The cinema preset never calls GPT in demo mode or without a key; it uses a cached layout or the local heuristic.

## Conventions

- Python 3.10+, standard library plus the packages in `requirements.txt`. No database: state is JSON next to the video.
- FFmpeg is called through `subprocess.run([...])` with argument lists, never a shell string. Raise `RuntimeError` with the stderr tail on failure.
- Prompts are in Russian because the target content is Russian. User-facing UI copy is English.
- No emoji or decorative symbols in code, UI, logs or docs. Status is a word: ok, skip, error, done.
- No em or en dashes in UI strings or docs; use a hyphen or a new sentence. LLM prompt text is exempt because it teaches Russian punctuation.
- Keep the UI a single file with no build step. Do not add frameworks.
- Commit one change per commit with a message that says what changed and why.

## How to add a subtitle preset

1. `render.py`: add an entry to `STYLES` with font, size, colors (ASS `&HAABBGGRR`), outline, shadow, `gap_threshold`, `hold_after` and a `render_mode`.
2. If no existing builder fits, write `_make_ass_<mode>(clip_groups, clip_start, clip_end, pos_y, style) -> str` that returns a full ASS document (use `_ass_header(style)` and `\an5\pos(540,pos_y)`), and register it in `_ASS_BUILDERS`.
3. `app.py`: add the style name to the allowlist tuple in `start_render`, `preview_style` and `render_scene`.
4. `static/index.html`: add a radio input and label to `#style-picker` (`id="style-<name>"`, `value="<name>"`) and an entry to `CFQ_STYLES` for the queue page.
5. Check it: open a cached session, pick the preset on Step 03 (this renders a 5 s preview), then render one scene and look at the output frame by frame around a dialogue change.
