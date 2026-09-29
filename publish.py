"""
publish.py: post finished clips from clip-factory to channels.

Input layout:
  output/<job_id>/clip_NN_shorts.mp4          - base render
  output/<job_id>/trimmed_clip_NN_shorts.mp4  - trimmed (trim_clip)
  output/<job_id>/bannered_clip_NN_shorts.mp4 - with a banner (apply_banner)
  output/<job_id>/<file>.caption.txt          - caption sidecar (optional)
Each clip number posts its final variant: bannered_ > trimmed_ > base.
Caption: <file>.caption.txt, else the scene hook from scenes.json, else empty.

State: output/<job_id>/posted.json. Idempotent: a clip is never posted twice.

Platforms: Telegram (Bot API) works. YouTube and VK are stubs.

Channel config: ~/.config/clip-factory/publish.json
Set telegram_api_base to a self-hosted Bot API server to post files over 50 MB.
{
  "telegram_api_base": "https://api.telegram.org",
  "channels": {
    "clips_ru": {
      "niche": "short clips",
      "telegram": { "bot_token": "123456:ABC...", "chat_id": "@my_channel" }
    }
  }
}

CLI:
  python publish.py list [--job JOB_ID]
  python publish.py post --job JOB_ID --channel NAME [--platforms telegram] [--clip FILE] [--dry-run]
  python publish.py scan --channel NAME [--platforms telegram] [--dry-run]
"""
import argparse
import json
import re
import sys
import time
from pathlib import Path

import requests

OUTPUT_DIR = Path("output")
CONFIG_PATH = Path.home() / ".config" / "clip-factory" / "publish.json"
TG_PUBLIC_API = "https://api.telegram.org"
TG_BOT_SIZE_LIMIT = 50 * 1024 * 1024  # 50 MB on the public Bot API

# Variant prefixes, highest priority first (the first match is final).
VARIANT_PREFIXES = ["bannered_", "trimmed_", ""]
CLIP_RE = re.compile(r"^(?:bannered_|trimmed_)*(clip_(\d+)_shorts)\.mp4$")


# --------------------------- config ---------------------------
def load_config() -> dict:
    if not CONFIG_PATH.exists():
        return {"telegram_api_base": TG_PUBLIC_API, "channels": {}}
    cfg = json.loads(CONFIG_PATH.read_text(encoding="utf-8"))
    cfg.setdefault("telegram_api_base", TG_PUBLIC_API)
    cfg.setdefault("channels", {})
    return cfg


def get_channel(cfg: dict, name: str) -> dict:
    ch = cfg["channels"].get(name)
    if not ch:
        raise SystemExit(f"Channel '{name}' not found in {CONFIG_PATH}. Available: {list(cfg['channels'])}")
    return ch


# ----------------------- clip choice -------------------------
def _base_key(filename: str):
    """Base key of any variant ('bannered_clip_03_shorts.mp4' -> ('clip_03_shorts', 3)). None if not a clip."""
    m = CLIP_RE.match(filename)
    if not m:
        return None
    return m.group(1), int(m.group(2))


def _variant_rank(filename: str) -> int:
    for i, pref in enumerate(VARIANT_PREFIXES):
        if pref and filename.startswith(pref):
            return i
    return len(VARIANT_PREFIXES) - 1  # the base clip_ has the lowest priority


def postable_clips(job_dir: Path) -> list[dict]:
    """Final variant for each clip number, with its caption."""
    by_base: dict[str, Path] = {}
    for p in sorted(job_dir.glob("*.mp4")):
        key = _base_key(p.name)
        if not key:
            continue
        base, _no = key
        if base not in by_base or _variant_rank(p.name) < _variant_rank(by_base[base].name):
            by_base[base] = p

    scenes = _load_scenes(job_dir)
    out = []
    for base, path in sorted(by_base.items(), key=lambda kv: _base_key(kv[1].name)[1]):
        _b, no = _base_key(path.name)
        out.append({
            "file": path,
            "clip_no": no,
            "caption": _resolve_caption(path, no, scenes),
            "size_mb": round(path.stat().st_size / 1048576, 1),
        })
    return out


def _load_scenes(job_dir: Path) -> dict:
    f = job_dir / "scenes.json"
    if not f.exists():
        return {}
    try:
        return {s.get("id"): s for s in json.loads(f.read_text(encoding="utf-8"))}
    except Exception:
        return {}


def _resolve_caption(path: Path, clip_no: int, scenes: dict) -> str:
    sidecar = Path(str(path) + ".caption.txt")
    if sidecar.exists():
        txt = sidecar.read_text(encoding="utf-8").strip()
        if txt:
            return txt
    scene = scenes.get(clip_no)
    if scene and scene.get("hook"):
        return scene["hook"].strip()
    return ""


# ----------------------- state --------------------------------
def _posted_path(job_dir: Path) -> Path:
    return job_dir / "posted.json"


def load_posted(job_dir: Path) -> dict:
    f = _posted_path(job_dir)
    return json.loads(f.read_text(encoding="utf-8")) if f.exists() else {}


def is_posted(posted: dict, filename: str, channel: str, platform: str) -> bool:
    rec = posted.get(filename, {}).get(channel, {}).get(platform)
    return bool(rec and rec.get("status") == "ok")


def mark_posted(job_dir: Path, filename: str, channel: str, platform: str, info: dict):
    posted = load_posted(job_dir)
    posted.setdefault(filename, {}).setdefault(channel, {})[platform] = {
        "status": info.get("status", "ok"),
        "id": info.get("id"),
        "url": info.get("url"),
        "ts": int(time.time()),
    }
    _posted_path(job_dir).write_text(json.dumps(posted, ensure_ascii=False, indent=2), encoding="utf-8")


# ----------------------- Telegram -----------------------------
def publish_telegram(video: Path, caption: str, bot_token: str, chat_id: str,
                     api_base: str = TG_PUBLIC_API) -> dict:
    """sendVideo to a channel. The bot must be an admin. Returns {status, id, url}."""
    size = video.stat().st_size
    if api_base == TG_PUBLIC_API and size > TG_BOT_SIZE_LIMIT:
        raise RuntimeError(
            f"{video.name} is {size/1048576:.1f} MB, over the 50 MB public Bot API limit. "
            f"Run a self-hosted telegram-bot-api server and set telegram_api_base in the config."
        )
    url = f"{api_base}/bot{bot_token}/sendVideo"
    with open(video, "rb") as f:
        resp = requests.post(
            url,
            data={"chat_id": chat_id, "caption": caption[:1024], "supports_streaming": "true"},
            files={"video": (video.name, f, "video/mp4")},
            timeout=300,
        )
    data = resp.json()
    if not data.get("ok"):
        raise RuntimeError(f"Telegram API error: {data.get('description', resp.text[:300])}")
    msg = data["result"]
    mid = msg.get("message_id")
    link = None
    if isinstance(chat_id, str) and chat_id.startswith("@"):
        link = f"https://t.me/{chat_id[1:]}/{mid}"
    return {"status": "ok", "id": mid, "url": link}


def publish_youtube(*_a, **_k):
    raise NotImplementedError("YouTube publisher is not implemented")


def publish_vk(*_a, **_k):
    raise NotImplementedError("VK publisher is not implemented")


PLATFORMS = {"telegram": publish_telegram, "youtube": publish_youtube, "vk": publish_vk}


# ----------------------- posting ------------------------------
def post_job(job_id: str, channel_name: str, platforms: list[str],
             only_clip: str | None = None, dry_run: bool = False) -> None:
    cfg = load_config()
    channel = get_channel(cfg, channel_name)
    job_dir = OUTPUT_DIR / job_id
    if not job_dir.is_dir():
        raise SystemExit(f"Session not found: {job_dir}")

    clips = postable_clips(job_dir)
    if only_clip:
        clips = [c for c in clips if c["file"].name == only_clip]
        if not clips:
            raise SystemExit(f"Clip {only_clip} not found in {job_dir}")

    posted = load_posted(job_dir)
    for c in clips:
        fname = c["file"].name
        for platform in platforms:
            if platform not in channel:
                print(f"  skip  {fname} [{platform}]: no platform config for channel '{channel_name}'")
                continue
            if is_posted(posted, fname, channel_name, platform):
                print(f"  skip  {fname} [{platform}]: already posted")
                continue
            if dry_run:
                cap = c["caption"].replace("\n", " / ")[:80]
                print(f"  dry   {fname} [{platform}] {c['size_mb']}MB  caption: \"{cap}\"")
                continue
            try:
                if platform == "telegram":
                    info = publish_telegram(
                        c["file"], c["caption"],
                        channel["telegram"]["bot_token"], channel["telegram"]["chat_id"],
                        api_base=cfg["telegram_api_base"],
                    )
                else:
                    info = PLATFORMS[platform](c["file"], c["caption"], **channel[platform])
                mark_posted(job_dir, fname, channel_name, platform, info)
                print(f"  ok    {fname} [{platform}] {info.get('url') or info.get('id')}")
            except Exception as e:
                print(f"  error {fname} [{platform}]: {e}")
            time.sleep(3)  # stay under Telegram flood limits


def _session_dirs() -> list[Path]:
    if not OUTPUT_DIR.is_dir():
        return []
    return sorted(p for p in OUTPUT_DIR.iterdir()
                  if p.is_dir() and len(p.name) == 8 and not p.name.startswith("."))


def cmd_list(job_id: str | None):
    dirs = [OUTPUT_DIR / job_id] if job_id else _session_dirs()
    found = 0
    for d in dirs:
        clips = postable_clips(d) if d.is_dir() else []
        if not clips:
            continue
        found += len(clips)
        posted = load_posted(d)
        print(f"\njob {d.name}")
        for c in clips:
            fname = c["file"].name
            plats = posted.get(fname, {})
            done = ",".join(sorted({p for ch in plats.values() for p in ch})) or "none"
            cap = c["caption"].replace("\n", " / ")[:70] or "(no caption)"
            print(f"  {fname}  {c['size_mb']}MB  posted:[{done}]  \"{cap}\"")
    if not found:
        print(f"No rendered clips in {OUTPUT_DIR.resolve()}")


def main():
    ap = argparse.ArgumentParser(description="Post clip-factory clips to channels")
    sub = ap.add_subparsers(dest="cmd", required=True)

    p_list = sub.add_parser("list", help="list clips ready to post")
    p_list.add_argument("--job", help="session job_id (default: all)")

    p_post = sub.add_parser("post", help="post the clips of one session")
    p_post.add_argument("--job", required=True)
    p_post.add_argument("--channel", required=True)
    p_post.add_argument("--platforms", default="telegram", help="comma-separated: telegram,youtube,vk")
    p_post.add_argument("--clip", help="post only this file")
    p_post.add_argument("--dry-run", action="store_true")

    p_scan = sub.add_parser("scan", help="post everything not yet posted, across all sessions")
    p_scan.add_argument("--channel", required=True)
    p_scan.add_argument("--platforms", default="telegram")
    p_scan.add_argument("--dry-run", action="store_true")

    a = ap.parse_args()
    if a.cmd == "list":
        cmd_list(a.job)
    elif a.cmd == "post":
        post_job(a.job, a.channel, [x.strip() for x in a.platforms.split(",") if x.strip()],
                 only_clip=a.clip, dry_run=a.dry_run)
    elif a.cmd == "scan":
        plats = [x.strip() for x in a.platforms.split(",") if x.strip()]
        for d in _session_dirs():
            if postable_clips(d):
                print(f"\njob {d.name}")
                post_job(d.name, a.channel, plats, dry_run=a.dry_run)


if __name__ == "__main__":
    main()
