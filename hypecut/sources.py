"""Video sources: local file, VOD (download only the spike windows), live rolling buffer."""
from __future__ import annotations

import csv
import json
import shutil
import subprocess
import sys
import time
from pathlib import Path

from .config import Settings
from .spikes import Spike


def probe_duration(path: str) -> float:
    r = subprocess.run(["ffprobe", "-v", "error", "-show_entries", "format=duration", "-of", "json", path],
                       capture_output=True, text=True, check=True)
    return float(json.loads(r.stdout)["format"]["duration"])


def _ytdlp() -> list[str]:
    return ["yt-dlp"] if shutil.which("yt-dlp") else [sys.executable, "-m", "yt_dlp"]


def _ytdlp_run(args: list[str], tries: int = 3) -> None:
    """Run yt-dlp with retries and print its stderr on failure.

    YouTube's section downloads (--download-sections) intermittently fail with
    throttling / network errors even when the video is fine, so we retry a few
    times. The stderr is logged because the desktop app runs via pythonw.exe,
    which has no console and would otherwise swallow the real error message.
    """
    for attempt in range(1, tries + 1):
        try:
            subprocess.run(_ytdlp() + args, capture_output=True, text=True,
                           encoding="utf-8", errors="replace", check=True)
            return
        except subprocess.CalledProcessError as e:
            err = (e.stderr or "").strip()
            print(f"[vod] yt-dlp failed (attempt {attempt}/{tries}): {err[-500:]}", flush=True)
            if attempt < tries:
                time.sleep(2 * attempt)
    raise


def _dl_flags() -> list[str]:
    """Resilience flags for YouTube downloads (throttling / flaky networks)."""
    return ["--retries", "10", "--fragment-retries", "10", "--extractor-retries", "3",
            "--socket-timeout", "30"]


# ---------------------------------------------------------------- VOD
def vod_resolver(url: str, work: Path):
    """Download just the window around each spike (yt-dlp --download-sections)."""
    def resolve(sp: Spike) -> tuple[str, float]:
        a, b = max(sp.start - 2, 0), sp.end + 2
        out = work / f"vod_{int(a)}_{int(b)}.mp4"
        if not out.exists():
            print(f"[vod] downloading {a:.0f}s-{b:.0f}s ...")
            _ytdlp_run(["-q", "--no-warnings", "-f", "bv*[height<=720]+ba/b[height<=720]/b",
                        "--download-sections", f"*{a:.0f}-{b:.0f}", "--force-keyframes-at-cuts",
                        "--merge-output-format", "mp4", * _dl_flags(), "-o", str(out), url])
        return str(out), a
    return resolve


def download_full(url: str, work: Path) -> str:
    """Small (max 360p) copy of the whole video for no-chat analysis; reused if already downloaded.
    The clips are still cut from HD: only their sections are downloaded in full quality."""
    found = list(work.glob("full_video.*"))
    if found:
        return str(found[0])
    print("[vod] no chat: downloading a small copy of the video to analyse it ...", flush=True)
    _ytdlp_run(["-q", "--no-warnings", "-f", "bv*[height<=360]+ba/b[height<=360]/wv*+ba/w",
                "--merge-output-format", "mp4", *_dl_flags(), "-o", str(work / "full_video.%(ext)s"), url])
    return str(next(work.glob("full_video.*")))


# ---------------------------------------------------------------- live
class LiveBuffer:
    """Records the live stream into short .ts segments (rolling) and can cut any recent time range.

    Stream time 0 = the moment recording started (shared with LiveChat's clock).
    """

    def __init__(self, url: str, work: Path, s: Settings):
        self.url, self.s = url, s
        self.dir = work / "buffer"
        self.dir.mkdir(parents=True, exist_ok=True)
        self.list_file = self.dir / "segments.csv"
        self.procs: list[subprocess.Popen] = []
        self.t0 = time.time()

    def start(self):
        if shutil.which("streamlink"):
            src = ["streamlink", "--stdout", "--twitch-disable-ads", self.url, "720p,720p60,best"] if "twitch.tv" in self.url \
                else ["streamlink", "--stdout", self.url, "720p,720p60,best"]
        else:
            src = _ytdlp() + ["-q", "-f", "b[height<=720]/best", "-o", "-", self.url]
        p1 = subprocess.Popen(src, stdout=subprocess.PIPE, stderr=subprocess.DEVNULL)
        ff = ["ffmpeg", "-loglevel", "error", "-i", "pipe:0", "-c", "copy", "-map", "0:v:0?", "-map", "0:a:0?",
              "-f", "segment", "-segment_time", str(self.s.segment_seconds), "-reset_timestamps", "1",
              "-segment_list", str(self.list_file), "-segment_list_type", "csv", "-segment_list_flags", "+live",
              str(self.dir / "seg_%06d.ts")]
        p2 = subprocess.Popen(ff, stdin=p1.stdout)
        p1.stdout.close()
        self.procs = [p1, p2]
        self.t0 = time.time()
        print(f"[live] recording rolling buffer to {self.dir}")
        return self

    def segments(self) -> list[tuple[Path, float, float]]:
        if not self.list_file.exists():
            return []
        rows = []
        with open(self.list_file, newline="") as f:
            for r in csv.reader(f):
                if len(r) >= 3:
                    rows.append((self.dir / r[0], float(r[1]), float(r[2])))
        return rows

    def recorded_until(self) -> float:
        seg = self.segments()
        return seg[-1][2] if seg else 0.0

    def cut(self, a: float, b: float, out: Path) -> tuple[str, float]:
        segs = [x for x in self.segments() if x[2] > a and x[1] < b and x[0].exists()]
        if not segs:
            raise RuntimeError("no buffered video for that range yet")
        lst = out.with_suffix(".txt")
        lst.write_text("".join(f"file '{p.resolve().as_posix()}'\n" for p, _, _ in segs), encoding="utf-8")
        subprocess.run(["ffmpeg", "-y", "-loglevel", "error", "-f", "concat", "-safe", "0", "-i", str(lst),
                        "-c", "copy", str(out)], check=True)
        return str(out), segs[0][1]

    def prune(self):
        if self.s.keep_buffer:
            return
        horizon = self.recorded_until() - self.s.buffer_minutes * 60
        for p, _, end in self.segments():
            if end < horizon and p.exists():
                p.unlink()  # only the tool's own temporary buffer segments

    def stop(self):
        for p in self.procs:
            p.terminate()
