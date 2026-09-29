"""
Post-process operations on rendered clips.
They run outside the pipeline and need only an mp4 and parameters.
"""
import json
import subprocess
from pathlib import Path


def probe_video_size(src: Path) -> tuple[int, int]:
    """Returns (width, height) in pixels for the video stream."""
    r = subprocess.run(
        ['ffprobe', '-v', 'quiet', '-print_format', 'json',
         '-show_streams', '-select_streams', 'v:0', str(src)],
        capture_output=True, text=True
    )
    streams = json.loads(r.stdout).get('streams', [{}])
    s = streams[0] if streams else {}
    return int(s.get('width', 1080)), int(s.get('height', 1920))


def extract_frame(src: Path, out_png: Path, t: float = 0.0) -> None:
    """Extract a single PNG frame at time t (seconds)."""
    cmd = ['ffmpeg', '-y', '-ss', str(max(0.0, t)), '-i', str(src),
           '-frames:v', '1', '-q:v', '2', str(out_png)]
    r = subprocess.run(cmd, capture_output=True)
    if r.returncode != 0:
        raise RuntimeError(r.stderr.decode()[-800:])


def trim_clip(src: Path, out: Path, start: float, end: float):
    """Trim src to [start, end] in seconds. Re-encodes for frame accuracy."""
    duration = end - start
    if duration <= 0:
        raise ValueError("end must be > start")
    cmd = [
        'ffmpeg', '-y',
        '-ss', str(start), '-i', str(src), '-t', str(duration),
        '-c:v', 'libx264', '-preset', 'fast', '-crf', '23',
        '-c:a', 'aac', '-b:a', '128k',
        str(out),
    ]
    result = subprocess.run(cmd, capture_output=True)
    if result.returncode != 0:
        raise RuntimeError(result.stderr.decode()[-800:])


# position -> FFmpeg overlay X:Y expression.
# W/H is the main video size, w/h is the banner size.
POSITION_MAP = {
    'top':    '(W-w)/2:80',
    'bottom': '(W-w)/2:H-h-80',
    'center': '(W-w)/2:(H-h)/2',
    'tl':     '40:40',
    'tr':     'W-w-40:40',
    'bl':     '40:H-h-40',
    'br':     'W-w-40:H-h-40',
}


def _is_animated(asset: Path) -> bool:
    """GIF, MP4 and WebM are animated. PNG and JPG are static."""
    ext = asset.suffix.lower()
    return ext in ('.gif', '.mp4', '.webm', '.mov', '.m4v')


def _is_image(asset: Path) -> bool:
    """A static image needs -loop 1 so the overlay lasts the whole clip."""
    ext = asset.suffix.lower()
    return ext in ('.png', '.jpg', '.jpeg', '.webp', '.bmp')


def apply_banner(src: Path, out: Path, asset: Path,
                 position: str | None = 'top',
                 t_start: float = 0.0, t_end: float = 3.0,
                 freeze_intro: float = 0.0,
                 banner_width: int = 480,
                 x_pct: float | None = None,
                 y_pct: float | None = None,
                 w_pct: float | None = None):
    """
    Overlay a banner on src during [t_start, t_end].

    Position:
      - x_pct/y_pct (0..100): top-left corner as a percent of video W/H.
      - w_pct (0..100): banner width as a percent of W. Overrides banner_width.
      - Without x_pct/y_pct, POSITION_MAP[position] and banner_width in px apply.

    freeze_intro > 0: the first N seconds of the output are a frozen first frame.
    """
    if t_end <= t_start:
        raise ValueError("t_end must be > t_start")

    custom_xy = (x_pct is not None and y_pct is not None)
    if custom_xy:
        x_frac = max(0.0, min(100.0, float(x_pct))) / 100.0
        y_frac = max(0.0, min(100.0, float(y_pct))) / 100.0
        xy = f"main_w*{x_frac:.4f}:main_h*{y_frac:.4f}"
    else:
        if position not in POSITION_MAP:
            raise ValueError(f"Unknown position: {position}")
        xy = POSITION_MAP[position]

    # Banner scale: prefer w_pct over banner_width.
    if w_pct is not None and float(w_pct) > 0:
        vw, _ = probe_video_size(src)
        scale_w_px = max(40, int(vw * float(w_pct) / 100.0))
    else:
        scale_w_px = int(banner_width)

    fi_ms = int(freeze_intro * 1000)
    animated = _is_animated(asset)
    static_image = _is_image(asset)

    filter_parts = []

    if freeze_intro > 0:
        filter_parts.append(f"[0:v]tpad=start_duration={freeze_intro}:start_mode=clone[v0]")
        filter_parts.append(f"[0:a]adelay={fi_ms}|{fi_ms}[a0]")
        v_in = "[v0]"
        a_out = "[a0]"
    else:
        v_in = "[0:v]"
        a_out = "0:a"

    if animated:
        filter_parts.append(f"[1:v]scale={scale_w_px}:-2,setpts=PTS-STARTPTS[b]")
    else:
        filter_parts.append(f"[1:v]scale={scale_w_px}:-2[b]")

    overlay_args = f"{xy}:enable='between(t,{t_start},{t_end})'"
    filter_parts.append(f"{v_in}[b]overlay={overlay_args}[outv]")

    filter_complex = ";".join(filter_parts)

    cmd = ['ffmpeg', '-y', '-i', str(src)]
    # Asset input flags: static image -> -loop 1, animated video or GIF -> -stream_loop -1
    if static_image:
        cmd += ['-loop', '1']
    elif animated:
        cmd += ['-stream_loop', '-1']
    cmd += ['-i', str(asset)]
    cmd += [
        '-filter_complex', filter_complex,
        '-map', '[outv]',
        '-map', a_out,
        '-c:v', 'libx264', '-preset', 'fast', '-crf', '23',
        '-c:a', 'aac', '-b:a', '128k',
        '-shortest',  # the main video sets the length; a looped overlay must not extend it
        str(out),
    ]

    result = subprocess.run(cmd, capture_output=True)
    if result.returncode != 0:
        raise RuntimeError(result.stderr.decode()[-1200:])
