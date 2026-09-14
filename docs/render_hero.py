"""Render docs/hero.svg to MP4 (LinkedIn) and GIF by freezing the animation at
successive timestamps in headless Chrome and encoding the frames.

    python docs/render_hero.py            # -> docs/out/hero.mp4, docs/out/hero.gif

Requires Google Chrome/Edge, pillow, imageio, imageio-ffmpeg.
"""

from __future__ import annotations

import os
import subprocess
import sys
from pathlib import Path

HERE = Path(__file__).resolve().parent
OUT = HERE / "out"
FRAMES = OUT / "frames"
DURATION, FPS, SCALE = 12.0, 10, 2
W, H = 960, 420
CHROME = next(p for p in [
    r"C:\Program Files\Google\Chrome\Application\chrome.exe",
    r"C:\Program Files (x86)\Microsoft\Edge\Application\msedge.exe",
    r"C:\Program Files\Microsoft\Edge\Application\msedge.exe",
] if os.path.exists(p))

PAGE = """<!doctype html><meta charset="utf-8">
<style>html,body{margin:0;background:#0b1220;width:%dpx;height:%dpx;overflow:hidden}svg{display:block}</style>
%s
<script>
  const t = parseFloat(new URLSearchParams(location.search).get('t') || '0');
  const svg = document.querySelector('svg');
  svg.pauseAnimations(); svg.setCurrentTime(t);                 // SMIL (animateMotion)
  document.getAnimations().forEach(a => { a.pause(); a.currentTime = t * 1000; }); // CSS animations
</script>"""


def main() -> None:
    FRAMES.mkdir(parents=True, exist_ok=True)
    svg = (HERE / "hero.svg").read_text(encoding="utf-8")
    page = HERE / "out" / "frame.html"
    page.write_text(PAGE % (W, H, svg), encoding="utf-8")
    n = int(DURATION * FPS)
    for i in range(n):
        t = i / FPS
        png = FRAMES / f"f{i:04d}.png"
        if png.exists():
            continue
        subprocess.run([CHROME, "--headless=new", "--disable-gpu", "--hide-scrollbars", "--no-first-run",
                        f"--window-size={W},{H}", f"--force-device-scale-factor={SCALE}", "--virtual-time-budget=1500",
                        f"--screenshot={png}", f"file:///{page.as_posix()}?t={t:.3f}"],
                       capture_output=True, timeout=60)
        print(f"\rframe {i + 1}/{n}", end="", flush=True)
    print()

    import imageio.v3 as iio
    from PIL import Image
    files = sorted(FRAMES.glob("f*.png"))
    frames = [iio.imread(f) for f in files]
    h, w = frames[0].shape[:2]
    frames = [f[: h - h % 2, : w - w % 2, :3] for f in frames]
    mp4 = OUT / "hero.mp4"
    import imageio
    writer = imageio.get_writer(mp4, fps=FPS, codec="libx264", quality=8, pixelformat="yuv420p", macro_block_size=1)
    for fr in frames:
        writer.append_data(fr)
    writer.close()
    gif = OUT / "hero.gif"
    small = [Image.fromarray(fr).resize((W, H), Image.LANCZOS) for fr in frames]
    quant = [im.convert("P", palette=Image.ADAPTIVE, colors=128) for im in small]
    quant[0].save(gif, save_all=True, append_images=quant[1:], duration=int(1000 / FPS), loop=0, optimize=True)
    print(f"mp4: {mp4} ({mp4.stat().st_size / 1e6:.1f} MB)  gif: {gif} ({gif.stat().st_size / 1e6:.1f} MB)  frames={len(frames)} {w}x{h}")


if __name__ == "__main__":
    sys.exit(main())
