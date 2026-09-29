import asyncio
import hashlib
import json
import os
import re
import shutil
import threading
import uuid
from pathlib import Path

from fastapi import FastAPI, UploadFile, File, Form, HTTPException
from fastapi.responses import StreamingResponse, FileResponse, JSONResponse
from fastapi.staticfiles import StaticFiles

app = FastAPI()

OUTPUT_DIR = Path("output")
OUTPUT_DIR.mkdir(exist_ok=True)

jobs: dict = {}

VIDEO_EXTS = {".mp4", ".mkv", ".mov", ".avi", ".webm", ".m4v"}

# -- DEMO MODE ----------------------------------------------------------------
# DEMO_MODE=1 returns 403 for upload, punctuation, scene and subtitle edits and
# queue render, and keeps the cinema preset off GPT. Browsing, style previews,
# render, trim and banner keep working. The public demo runs in this mode.
DEMO_MODE = os.environ.get("DEMO_MODE", "").lower() in ("1", "true", "yes")
if DEMO_MODE:
    print("[demo] DEMO_MODE=1: upload, punctuation, scene edits and queue render disabled")


def _demo_disabled(message: str) -> JSONResponse:
    return JSONResponse(status_code=403,
                        content={"error": "demo_disabled", "message": message})


def get_api_key() -> str:
    key = os.environ.get("OPENAI_API_KEY")
    if key:
        return key
    config = Path.home() / ".config" / "clip-factory" / "key"
    if config.exists():
        return config.read_text().strip()
    if DEMO_MODE:
        return ""  # demo never calls OpenAI - uploads/punctuate are blocked
    raise RuntimeError("No OpenAI API key found")


def _job_dir(job_id: str) -> Path:
    """Return output/<job_id>/. A job_id is 8 hex characters; anything else is a 404."""
    if not re.fullmatch(r"[0-9a-f]{8}", job_id):
        raise HTTPException(status_code=404, detail="Not found")
    return OUTPUT_DIR / job_id


def _name(name: str) -> str:
    """Accept a bare file name inside a session dir. Paths and dot files are a 404."""
    if not name or name.startswith(".") or "/" in name or "\\" in name:
        raise HTTPException(status_code=404, detail="Not found")
    return name


def _upload_name(filename: str | None) -> str:
    """Strip client-supplied path parts from an upload's file name."""
    name = Path(filename or "").name
    if not name or name.startswith(".") or "\\" in name:
        raise HTTPException(status_code=400, detail="Invalid file name")
    return name


def _output_file(rel: str) -> Path | None:
    """Resolve a path relative to output/. Return None if it leaves output/."""
    if not isinstance(rel, str) or not rel:
        return None
    p = (OUTPUT_DIR / rel).resolve()
    return p if p.is_relative_to(OUTPUT_DIR.resolve()) and p.is_file() else None


def push(job_id: str, event: dict):
    if job_id in jobs:
        jobs[job_id]["events"].append(event)


def _find_source_video(job_dir: Path) -> Path | None:
    """Find the episode source video in job_dir, skipping rendered clips."""
    for p in job_dir.iterdir():
        if p.suffix.lower() in VIDEO_EXTS and not p.name.startswith("clip_") \
                and not p.name.startswith("preview_") \
                and not p.name.startswith("trimmed_") \
                and not p.name.startswith("bannered_"):
            return p
    return None


def _rehydrate(job_id: str, job_dir: Path) -> dict:
    """
    Load session state from the JSON files on disk. Does not run the pipeline.
    Returns the jobs[job_id] dict and registers it in `jobs`.
    """
    video_path = _find_source_video(job_dir)
    api_key = get_api_key()

    state = {
        "status": "loaded",
        "video_path": video_path,
        "job_dir": job_dir,
        "api_key": api_key,
        "events": [],
        "scenes": [],
        "segments": [],
        "words": [],
        "clip_groups": {},
        "pipeline_done": True,
    }

    transcript_file = job_dir / "transcript.json"
    words_file = job_dir / "words.json"
    scenes_file = job_dir / "scenes.json"
    clip_groups_file = job_dir / "clip_groups.json"

    if transcript_file.exists():
        state["segments"] = json.loads(transcript_file.read_text())
    if words_file.exists():
        state["words"] = json.loads(words_file.read_text())
    if scenes_file.exists():
        state["scenes"] = json.loads(scenes_file.read_text())
    if clip_groups_file.exists():
        raw = json.loads(clip_groups_file.read_text())
        # JSON keys are always strings; convert scene_id back to int
        state["clip_groups"] = {int(k): v for k, v in raw.items()}

    jobs[job_id] = state
    return state


def run_pipeline(job_id: str, video_path: Path, job_dir: Path, api_key: str):
    try:
        from transcribe import transcribe_video
        from select_scenes import select_scenes
        from sanity_check import sanity_check_transcript, apply_corrections

        push(job_id, {"type": "step", "step": 1, "total": 3,
                       "message": "Transcribing with Whisper..."})

        transcript_file = job_dir / "transcript.json"
        words_file = job_dir / "words.json"

        if transcript_file.exists() and words_file.exists():
            segments = json.loads(transcript_file.read_text())
            words = json.loads(words_file.read_text())
        else:
            result = transcribe_video(video_path, api_key)
            segments = result["segments"]
            words = result["words"]
            transcript_file.write_text(json.dumps(segments, ensure_ascii=False, indent=2))
            words_file.write_text(json.dumps(words, ensure_ascii=False, indent=2))

        jobs[job_id]["segments"] = segments
        jobs[job_id]["words"] = words
        duration = segments[-1]["end"] if segments else 0
        push(job_id, {"type": "transcript_ready",
                       "segments": segments,
                       "count": len(segments),
                       "duration_sec": duration,
                       "word_count": len(words)})

        push(job_id, {"type": "step", "step": 2, "total": 3,
                       "message": "Sanity check on transcript (GPT-4o-mini)..."})

        corrections_file = job_dir / "corrections.json"
        if corrections_file.exists():
            corrections = json.loads(corrections_file.read_text())
        else:
            corrections = sanity_check_transcript(segments, api_key)
            corrections_file.write_text(json.dumps(corrections, ensure_ascii=False, indent=2))

        if corrections:
            push(job_id, {"type": "corrections_found", "corrections": corrections})
            segments, words = apply_corrections(segments, words, corrections)
            transcript_file.write_text(json.dumps(segments, ensure_ascii=False, indent=2))
            words_file.write_text(json.dumps(words, ensure_ascii=False, indent=2))
            jobs[job_id]["segments"] = segments
            jobs[job_id]["words"] = words
        else:
            push(job_id, {"type": "corrections_found", "corrections": []})

        push(job_id, {"type": "step", "step": 3, "total": 3,
                       "message": "Selecting scenes with GPT-4o..."})

        scenes_file = job_dir / "scenes.json"
        if scenes_file.exists():
            scenes = json.loads(scenes_file.read_text())
            push(job_id, {"type": "gpt_complete", "count": len(scenes), "cached": True})
        else:
            def on_prompt(info):
                push(job_id, {"type": "gpt_request",
                              "model": info["model"],
                              "system": info["system"],
                              "prompt": info["prompt"]})

            def on_chunk(text):
                push(job_id, {"type": "gpt_chunk", "text": text})

            scenes_raw = select_scenes(segments, api_key,
                                       on_chunk=on_chunk, on_prompt=on_prompt)
            scenes = [
                {
                    "id": i + 1,
                    "start": round(s["start"], 1),
                    "end": round(s["end"], 1),
                    "duration": round(s["end"] - s["start"], 1),
                    "hook": s.get("hook", ""),
                    "description": s.get("description", ""),
                    "why_shorts": s.get("why_shorts", ""),
                    "reason": s.get("reason", ""),
                    "approved": True,
                }
                for i, s in enumerate(scenes_raw)
            ]
            scenes_file.write_text(json.dumps(scenes, ensure_ascii=False, indent=2))
            push(job_id, {"type": "gpt_complete", "count": len(scenes)})

        jobs[job_id]["scenes"] = scenes
        jobs[job_id]["status"] = "ready"
        push(job_id, {"type": "ready", "scenes": scenes})

    except Exception as e:
        jobs[job_id]["status"] = "error"
        push(job_id, {"type": "error", "message": str(e)})
    finally:
        jobs[job_id]["pipeline_done"] = True


PRE_BUFFER = 4


def run_punctuate(job_id: str, job_dir: Path, api_key: str):
    try:
        from punctuate_clip import punctuate_clip

        all_words = jobs[job_id].get("words", [])
        scenes = [s for s in jobs[job_id]["scenes"] if s.get("approved", True)]
        clip_groups_cache = jobs[job_id].setdefault("clip_groups", {})

        for i, scene in enumerate(scenes):
            sid = scene["id"]
            if sid in clip_groups_cache:
                push(job_id, {"type": "clip_punctuated", "scene_id": sid,
                              "groups": clip_groups_cache[sid], "cached": True})
                continue

            push(job_id, {"type": "punctuating", "scene_id": sid,
                          "current": i + 1, "total": len(scenes),
                          "message": f"Punctuating clip {i+1}/{len(scenes)} (#{sid})..."})

            start = max(0.0, scene["start"] - PRE_BUFFER)
            end = scene["end"]
            clip_words = [w for w in all_words
                          if w["end"] > start and w["start"] < end]

            groups = punctuate_clip(clip_words, api_key)
            clip_groups_cache[sid] = groups

            (job_dir / "clip_groups.json").write_text(
                json.dumps(clip_groups_cache, ensure_ascii=False, indent=2))

            push(job_id, {"type": "clip_punctuated", "scene_id": sid,
                          "groups": groups, "cached": False})

        push(job_id, {"type": "punctuate_done"})

    except Exception as e:
        push(job_id, {"type": "error", "message": str(e)})


def run_render(job_id: str, video_path: Path, job_dir: Path, style: str = "cinema"):
    try:
        from render import render_clip

        segments = jobs[job_id]["segments"]
        scenes = [s for s in jobs[job_id]["scenes"] if s.get("approved", True)]
        clip_groups_cache = jobs[job_id].get("clip_groups", {})

        for i, scene in enumerate(scenes):
            push(job_id, {
                "type": "render_progress",
                "current": i + 1, "total": len(scenes),
                "message": f"Rendering clip {i+1} of {len(scenes)} (style: {style})..."
            })
            out = job_dir / f"clip_{scene['id']:02d}_shorts.mp4"
            clip_groups = clip_groups_cache.get(scene["id"], [])
            render_clip(video_path, scene, segments, out,
                        clip_groups=clip_groups, style=style)
            push(job_id, {
                "type": "clip_done",
                "clip_id": scene["id"],
                "filename": out.name,
                "hook": scene["hook"],
                "url": f"/api/clips/{job_id}/{out.name}"
            })

        jobs[job_id]["status"] = "done"
        push(job_id, {"type": "render_done"})

    except Exception as e:
        jobs[job_id]["status"] = "error"
        push(job_id, {"type": "error", "message": str(e)})


# -- API ----------------------------------------------------------------------

@app.post("/api/upload")
async def upload(file: UploadFile = File(...)):
    """
    Stream the file to a temp path and hash it with SHA-1 on the way.
    job_id = sha1(content)[:8]. Uploading the same video again lands in the
    existing folder and reuses every cache.
    """
    if DEMO_MODE:
        return JSONResponse(
            status_code=403,
            content={"error": "demo_disabled", "message": "Upload is disabled in demo mode. Start the server without DEMO_MODE=1 to upload."}
        )
    filename = _upload_name(file.filename)
    tmp_path = OUTPUT_DIR / f".tmp_{uuid.uuid4().hex[:8]}_{filename}"
    sha = hashlib.sha1()
    with open(tmp_path, "wb") as f:
        while True:
            chunk = await file.read(1024 * 1024)
            if not chunk:
                break
            sha.update(chunk)
            f.write(chunk)

    job_id = sha.hexdigest()[:8]
    job_dir = OUTPUT_DIR / job_id
    job_dir.mkdir(parents=True, exist_ok=True)
    final_path = job_dir / filename

    # The video is already there: cache hit. Drop the temp file.
    if final_path.exists():
        tmp_path.unlink(missing_ok=True)
    else:
        tmp_path.rename(final_path)

    api_key = get_api_key()
    # No state in memory yet: load it from disk
    if job_id not in jobs:
        _rehydrate(job_id, job_dir)
    jobs[job_id]["video_path"] = final_path
    jobs[job_id]["api_key"] = api_key
    jobs[job_id]["status"] = "processing"
    jobs[job_id]["events"] = []
    jobs[job_id]["pipeline_done"] = False

    threading.Thread(
        target=run_pipeline,
        args=(job_id, final_path, job_dir, api_key),
        daemon=True
    ).start()

    return {"job_id": job_id, "filename": filename}


@app.get("/api/sessions")
async def list_sessions():
    """List existing session dirs with a short summary of each."""
    out = []
    for p in sorted(OUTPUT_DIR.iterdir(), key=lambda x: x.stat().st_mtime, reverse=True):
        if not p.is_dir() or p.name.startswith(".") or len(p.name) != 8:
            continue
        video = _find_source_video(p)
        if not video:
            continue
        scenes_file = p / "scenes.json"
        clips_count = sum(1 for f in p.iterdir() if f.name.startswith("clip_") and f.suffix == ".mp4")
        out.append({
            "job_id": p.name,
            "video": video.name,
            "size_mb": round(video.stat().st_size / 1e6, 1),
            "scenes_count": len(json.loads(scenes_file.read_text())) if scenes_file.exists() else 0,
            "clips_count": clips_count,
            "mtime": p.stat().st_mtime,
        })
    return {"sessions": out}


@app.post("/api/load_session/{job_id}")
async def load_session(job_id: str):
    """
    Load an existing session from output/<job_id>/.

    Returns the cached transcript, scenes and subtitles, so the UI shows the
    scenes and the subtitle editor at once. Rendered clips are left out on
    purpose: "Render approved" runs the render again, so the user sees the
    real pipeline step and not stale files from disk.
    """
    job_dir = _job_dir(job_id)
    if not job_dir.exists() or not job_dir.is_dir():
        return JSONResponse({"error": "Session not found"}, status_code=404)
    state = _rehydrate(job_id, job_dir)
    if not state["video_path"]:
        return JSONResponse({"error": "Source video missing in session dir"}, status_code=400)

    return {
        "job_id": job_id,
        "video": state["video_path"].name,
        "segments": state["segments"],
        "scenes": state["scenes"],
        "clip_groups": state["clip_groups"],
        "has_words": bool(state["words"]),
        # clips intentionally omitted - Step 4 stays empty until user runs Render
    }


@app.get("/api/progress/{job_id}")
async def progress(job_id: str):
    if job_id not in jobs:
        return JSONResponse({"error": "Not found"}, status_code=404)

    async def generate():
        last = 0
        while True:
            evs = jobs[job_id]["events"]
            while last < len(evs):
                e = evs[last]
                yield f"data: {json.dumps(e)}\n\n"
                last += 1
                if e.get("type") in ("render_done", "error"):
                    return
            await asyncio.sleep(0.3)

    return StreamingResponse(generate(), media_type="text/event-stream",
                             headers={"Cache-Control": "no-cache"})


@app.patch("/api/scenes/{job_id}/{scene_id}")
async def update_scene(job_id: str, scene_id: int, data: dict):
    if DEMO_MODE:
        return _demo_disabled("Scene edits are disabled in the demo.")
    if job_id not in jobs:
        return JSONResponse({"error": "Not found"}, status_code=404)
    for scene in jobs[job_id]["scenes"]:
        if scene["id"] == scene_id:
            for k in ("start", "end", "approved"):
                if k in data:
                    scene[k] = data[k]
            scene["duration"] = round(scene["end"] - scene["start"], 1)
            (jobs[job_id]["job_dir"] / "scenes.json").write_text(
                json.dumps(jobs[job_id]["scenes"], ensure_ascii=False, indent=2))
            return {"ok": True, "scene": scene}
    return JSONResponse({"error": "Scene not found"}, status_code=404)


@app.post("/api/scenes/{job_id}")
async def create_scene(job_id: str, data: dict):
    if DEMO_MODE:
        return _demo_disabled("Adding scenes is disabled in the demo.")
    if job_id not in jobs:
        return JSONResponse({"error": "Not found"}, status_code=404)

    start = float(data.get("start", 0))
    end = float(data.get("end", 0))
    if end <= start:
        return JSONResponse({"error": "end must be > start"}, status_code=400)

    scenes = jobs[job_id].setdefault("scenes", [])
    new_id = max((s["id"] for s in scenes), default=0) + 1
    new_scene = {
        "id": new_id,
        "start": round(start, 1),
        "end": round(end, 1),
        "duration": round(end - start, 1),
        "hook": data.get("hook", "(manual scene)"),
        "description": data.get("description", ""),
        "why_shorts": "",
        "reason": "manual",
        "approved": True,
    }
    scenes.append(new_scene)
    (jobs[job_id]["job_dir"] / "scenes.json").write_text(
        json.dumps(scenes, ensure_ascii=False, indent=2))
    return {"ok": True, "scene": new_scene}


@app.post("/api/punctuate/{job_id}")
async def start_punctuate(job_id: str, data: dict | None = None):
    """
    Run per-clip punctuation for all approved scenes.
    Optional body: {"force_scene_ids": [int, ...]} - invalidate cached
    subtitles for those scenes so they get regenerated (used after the
    user changes a scene's start/end).
    """
    if DEMO_MODE:
        return JSONResponse(
            status_code=403,
            content={"error": "demo_disabled", "message": "Punctuation calls GPT and is disabled in demo mode."}
        )
    if job_id not in jobs:
        return JSONResponse({"error": "Not found"}, status_code=404)
    force = (data or {}).get("force_scene_ids") or []
    if force:
        cache = jobs[job_id].setdefault("clip_groups", {})
        for sid in force:
            cache.pop(int(sid), None)
        (jobs[job_id]["job_dir"] / "clip_groups.json").write_text(
            json.dumps(cache, ensure_ascii=False, indent=2))
    threading.Thread(
        target=run_punctuate,
        args=(job_id, jobs[job_id]["job_dir"], jobs[job_id]["api_key"]),
        daemon=True
    ).start()
    return {"ok": True}


@app.patch("/api/clip_groups/{job_id}/{scene_id}")
async def update_clip_groups(job_id: str, scene_id: int, data: dict):
    if DEMO_MODE:
        return _demo_disabled("Subtitle edits are disabled in the demo.")
    if job_id not in jobs:
        return JSONResponse({"error": "Not found"}, status_code=404)
    cache = jobs[job_id].setdefault("clip_groups", {})
    new_groups = data.get("groups", [])
    cache[scene_id] = new_groups
    (jobs[job_id]["job_dir"] / "clip_groups.json").write_text(
        json.dumps(cache, ensure_ascii=False, indent=2))
    return {"ok": True}


@app.post("/api/render/{job_id}")
async def start_render(job_id: str, data: dict | None = None):
    if job_id not in jobs:
        return JSONResponse({"error": "Not found"}, status_code=404)
    style = (data or {}).get("style", "cinema")
    if style not in ("cinema", "chunks", "karaoke", "one_word"):
        return JSONResponse({"error": f"Unknown style: {style}"}, status_code=400)
    jobs[job_id]["status"] = "rendering"
    threading.Thread(
        target=run_render,
        args=(job_id, jobs[job_id]["video_path"], jobs[job_id]["job_dir"], style),
        daemon=True
    ).start()
    return {"ok": True, "style": style}


@app.post("/api/preview_style/{job_id}/{scene_id}")
async def preview_style(job_id: str, scene_id: int, data: dict):
    """
    Mini render to check a subtitle style: 5 s, 480p, no audio.
    Synchronous: the client waits for the response. Takes 2-4 s.
    """
    if job_id not in jobs:
        return JSONResponse({"error": "Not found"}, status_code=404)
    style = data.get("style", "cinema")
    if style not in ("cinema", "chunks", "karaoke", "one_word"):
        return JSONResponse({"error": f"Unknown style: {style}"}, status_code=400)

    scene = next((s for s in jobs[job_id]["scenes"] if s["id"] == scene_id), None)
    if not scene:
        return JSONResponse({"error": "Scene not found"}, status_code=404)

    from render import render_clip
    segments = jobs[job_id]["segments"]
    clip_groups = jobs[job_id].get("clip_groups", {}).get(scene_id, [])
    job_dir = jobs[job_id]["job_dir"]
    out = job_dir / f"preview_{scene_id}_{style}.mp4"

    render_clip(
        jobs[job_id]["video_path"], scene, segments, out,
        clip_groups=clip_groups, style=style, preview=True,
    )
    return {"ok": True, "url": f"/api/clips/{job_id}/{out.name}"}


# -- Clip queue (cross-session) -----------------------------------------------

@app.get("/api/queue")
async def queue():
    """
    Approved scenes from all sessions that have no rendered clip yet. This is
    the render queue behind the Queue page.
    """
    items = []
    for p in sorted(OUTPUT_DIR.iterdir(), key=lambda x: x.stat().st_mtime, reverse=True):
        if not p.is_dir() or p.name.startswith(".") or len(p.name) != 8:
            continue
        scenes_file = p / "scenes.json"
        video = _find_source_video(p)
        if not scenes_file.exists() or not video:
            continue
        try:
            scenes = json.loads(scenes_file.read_text())
        except Exception:
            continue
        punctuated = set()
        cg = p / "clip_groups.json"
        if cg.exists():
            try:
                punctuated = {int(k) for k in json.loads(cg.read_text())}
            except Exception:
                pass
        for s in scenes:
            if not s.get("approved", True):
                continue
            sid = s["id"]
            if (p / f"clip_{sid:02d}_shorts.mp4").exists():
                continue  # already rendered
            items.append({
                "job_id": p.name,
                "video": video.name,
                "scene_id": sid,
                "hook": s.get("hook", ""),
                "description": s.get("description", ""),
                "start": s.get("start"),
                "end": s.get("end"),
                "duration": s.get("duration"),
                "punctuated": sid in punctuated,
            })
    return {"queue": items, "count": len(items)}


@app.post("/api/render_scene/{job_id}/{scene_id}")
async def render_scene(job_id: str, scene_id: int, data: dict | None = None):
    """
    Render one scene with the given style (Queue page).
    Loads the session from disk and punctuates the scene first if needed.
    """
    if DEMO_MODE:
        return JSONResponse(status_code=403, content={"error": "demo_disabled"})
    style = (data or {}).get("style", "cinema")
    if style not in ("cinema", "chunks", "karaoke", "one_word"):
        return JSONResponse({"error": f"Unknown style: {style}"}, status_code=400)
    job_dir = _job_dir(job_id)
    if not job_dir.is_dir():
        return JSONResponse({"error": "Session not found"}, status_code=404)
    state = jobs.get(job_id) or _rehydrate(job_id, job_dir)
    if not state.get("video_path"):
        return JSONResponse({"error": "Source video missing"}, status_code=400)
    scene = next((s for s in state["scenes"] if s["id"] == scene_id), None)
    if not scene:
        return JSONResponse({"error": "Scene not found"}, status_code=404)

    cache = state.setdefault("clip_groups", {})

    # New edges from the queue trim editor: save them to the scene and
    # scenes.json, and drop the cached punctuation so subtitles are rebuilt
    # for the new window. Timecodes are saved first, then the clip is cut.
    ns, ne = (data or {}).get("start"), (data or {}).get("end")
    if ns is not None and ne is not None and float(ne) > float(ns):
        scene["start"] = float(ns)
        scene["end"] = float(ne)
        scene["duration"] = round(scene["end"] - scene["start"], 1)
        (job_dir / "scenes.json").write_text(
            json.dumps(state["scenes"], ensure_ascii=False, indent=2))
        cache.pop(scene_id, None)

    # Punctuate now if the scene has no cached subtitles
    if scene_id not in cache:
        try:
            from punctuate_clip import punctuate_clip
            start = max(0.0, scene["start"] - PRE_BUFFER)
            end = scene["end"]
            clip_words = [w for w in state.get("words", [])
                          if w["end"] > start and w["start"] < end]
            cache[scene_id] = punctuate_clip(clip_words, state["api_key"])
            (job_dir / "clip_groups.json").write_text(
                json.dumps(cache, ensure_ascii=False, indent=2))
        except Exception as e:
            return JSONResponse({"error": f"punctuate failed: {e}"}, status_code=500)

    try:
        from render import render_clip
        out = job_dir / f"clip_{scene_id:02d}_shorts.mp4"
        render_clip(state["video_path"], scene, state["segments"], out,
                    clip_groups=cache.get(scene_id, []), style=style)
    except Exception as e:
        return JSONResponse({"error": f"render failed: {e}"}, status_code=500)
    return {"ok": True, "url": f"/api/clips/{job_id}/{out.name}",
            "filename": out.name, "style": style}


# -- Post-process: trim, banner -----------------------------------------------

@app.post("/api/clips/{job_id}/{filename}/trim")
async def trim_clip_endpoint(job_id: str, filename: str, data: dict):
    """Trim a rendered clip to [start, end], in seconds from the clip start."""
    job_dir = _job_dir(job_id)
    src = job_dir / _name(filename)
    if not src.exists():
        return JSONResponse({"error": "Clip not found"}, status_code=404)
    start = float(data.get("start", 0))
    end = float(data.get("end", 0))
    if end <= start:
        return JSONResponse({"error": "end must be > start"}, status_code=400)
    out = job_dir / f"trimmed_{filename}"
    from postprocess import trim_clip
    trim_clip(src, out, start, end)
    return {"ok": True, "url": f"/api/clips/{job_id}/{out.name}", "filename": out.name}


@app.post("/api/clips/{job_id}/upload_asset")
async def upload_asset(job_id: str, file: UploadFile = File(...)):
    """Upload an image, GIF or video to use as a banner."""
    job_dir = _job_dir(job_id)
    if not job_dir.exists():
        return JSONResponse({"error": "Session not found"}, status_code=404)
    filename = _upload_name(file.filename)
    assets_dir = job_dir / "assets"
    assets_dir.mkdir(exist_ok=True)
    dst = assets_dir / filename
    with open(dst, "wb") as f:
        shutil.copyfileobj(file.file, f)
    return {"ok": True, "path": str(dst.relative_to(OUTPUT_DIR)), "filename": filename}


@app.post("/api/clips/{job_id}/{filename}/banner")
async def banner_clip_endpoint(job_id: str, filename: str, data: dict):
    """
    Overlay a banner (image, GIF or video) on a clip.
    data: {asset: "relative_path", position: "top|bottom|center|tl|tr|bl|br",
           t_start: 0.0, t_end: 3.0, freeze_intro: 0.0}
    freeze_intro > 0 -> the clip starts with the first frame frozen for N
                       seconds under the banner. Audio comes from the clip.
    """
    job_dir = _job_dir(job_id)
    src = job_dir / _name(filename)
    if not src.exists():
        return JSONResponse({"error": "Clip not found"}, status_code=404)
    asset_rel = data.get("asset")
    if not asset_rel:
        return JSONResponse({"error": "asset required"}, status_code=400)
    asset = _output_file(asset_rel)
    if asset is None:
        return JSONResponse({"error": "Asset file not found"}, status_code=404)

    out = job_dir / f"bannered_{filename}"
    from postprocess import apply_banner
    apply_banner(
        src, out, asset,
        position=data.get("position", "top"),
        t_start=float(data.get("t_start", 0)),
        t_end=float(data.get("t_end", 3)),
        freeze_intro=float(data.get("freeze_intro", 0)),
        x_pct=float(data["x_pct"]) if data.get("x_pct") is not None else None,
        y_pct=float(data["y_pct"]) if data.get("y_pct") is not None else None,
        w_pct=float(data["w_pct"]) if data.get("w_pct") is not None else None,
    )
    return {"ok": True, "url": f"/api/clips/{job_id}/{out.name}", "filename": out.name}


_SCENE_FRAME_RE = re.compile(r"^(?P<stem>.+)_t(?P<cs>\d{7})\.png$")


@app.get("/api/clips/{job_id}/frames/{name}")
async def scene_frame_endpoint(job_id: str, name: str):
    """
    Scene card thumbnail: frames/<source_stem>_t<centiseconds>.png.
    In production nginx serves these files straight from disk. Locally the
    frame is extracted from the source video on first request and cached.
    """
    m = _SCENE_FRAME_RE.match(_name(name))
    job_dir = _job_dir(job_id)
    if not m or not job_dir.is_dir():
        return JSONResponse({"error": "Not found"}, status_code=404)
    out_png = job_dir / "frames" / name
    if not out_png.exists():
        source = _find_source_video(job_dir)
        if not source or source.stem != m.group("stem"):
            return JSONResponse({"error": "Not found"}, status_code=404)
        out_png.parent.mkdir(parents=True, exist_ok=True)
        from postprocess import extract_frame
        try:
            extract_frame(source, out_png, t=int(m.group("cs")) / 100)
        except Exception as e:
            return JSONResponse({"error": "ffmpeg failed", "detail": str(e)[-400:]}, status_code=500)
    return FileResponse(out_png, media_type="image/png")


@app.get("/api/clips/{job_id}/{filename}/frame")
async def clip_frame_endpoint(job_id: str, filename: str, t: float = 0.0):
    """Return a PNG frame of the clip at `t` seconds. Cached in job_dir/frames/."""
    job_dir = _job_dir(job_id)
    src = job_dir / _name(filename)
    if not src.exists():
        return JSONResponse({"error": "Clip not found"}, status_code=404)
    frames_dir = job_dir / "frames"
    frames_dir.mkdir(parents=True, exist_ok=True)
    safe_stem = filename.rsplit(".", 1)[0]
    out_png = frames_dir / f"{safe_stem}_t{int(round(t * 100)):07d}.png"
    if not out_png.exists():
        from postprocess import extract_frame
        try:
            extract_frame(src, out_png, t=float(t))
        except Exception as e:
            return JSONResponse({"error": "ffmpeg failed", "detail": str(e)[-400:]}, status_code=500)
    return FileResponse(out_png, media_type="image/png")


# -- Static file serving -----------------------------------------------------

@app.get("/api/video/{job_id}")
async def serve_video(job_id: str):
    job_dir = _job_dir(job_id)
    if job_id in jobs and jobs[job_id].get("video_path"):
        return FileResponse(str(jobs[job_id]["video_path"]), media_type="video/mp4")
    video = _find_source_video(job_dir) if job_dir.exists() else None
    if video:
        return FileResponse(str(video), media_type="video/mp4")
    return JSONResponse({"error": "Not found"}, status_code=404)


@app.get("/api/clips/{job_id}/{filename}")
async def serve_clip(job_id: str, filename: str):
    p = _job_dir(job_id) / _name(filename)
    return FileResponse(str(p), media_type="video/mp4") if p.exists() \
        else JSONResponse({"error": "File not found"}, status_code=404)


app.mount("/", StaticFiles(directory="static", html=True), name="static")
