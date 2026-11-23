"""Command line: hypecut <url-or-video> [--mode live|vod|file] [--chat chat.jsonl] --out clips/"""
from __future__ import annotations

import argparse
import json
import shutil
import sys
import time
from dataclasses import fields
from pathlib import Path

from .chat import LiveChat, fetch_vod_chat, load_chat_file, save_chat_jsonl
from .config import Settings
from .pipeline import run
from .sources import LiveBuffer, download_full, probe_duration, vod_resolver
from .spikes import Spike, activity_curve, detect_spikes, zscores


def build_parser() -> argparse.ArgumentParser:
    d = Settings()
    p = argparse.ArgumentParser(prog="hypecut", description="Clip the moments where stream chat explodes.")
    p.add_argument("source", nargs="?", help="Twitch/YouTube URL, or a local video file")
    p.add_argument("--mode", choices=["live", "vod", "file"], help="default: file if source is a local path, else vod")
    p.add_argument("--chat", help="chat log (.jsonl/.csv/.txt/yt-dlp live_chat.json). Needed for local files")
    p.add_argument("--out", default="clips", help="output folder (default: clips/)")
    p.add_argument("--dry-run", action="store_true", help="only detect spikes and pick cuts, don't render video")
    p.add_argument("--demo", action="store_true", help="generate the demo stream + chat and clip it")
    p.add_argument("--clip-by", choices=["auto", "chat", "nochat", "video", "story"], default="auto",
                   help="auto (default): chat if the video has chat, otherwise video hype or best moments")
    p.add_argument("--allow-repeats", action="store_true", help="clip moments already clipped in earlier runs")
    g = p.add_argument_group("spike detection")
    g.add_argument("--z", dest="z_threshold", type=float, default=d.z_threshold, help="spike threshold (std-devs)")
    g.add_argument("--min-rate", type=float, default=d.min_rate)
    g.add_argument("--max-clips", type=int, default=d.max_clips)
    g.add_argument("--min-gap", type=float, default=d.min_gap)
    g.add_argument("--baseline", dest="baseline_seconds", type=float, default=d.baseline_seconds)
    g.add_argument("--reaction-delay", type=float, default=d.reaction_delay)
    g.add_argument("--pre", dest="pre_seconds", type=float, default=d.pre_seconds, help="seconds before the chat peak")
    g.add_argument("--post", dest="post_seconds", type=float, default=d.post_seconds, help="seconds after the chat peak")
    g.add_argument("--min-clip", type=float, default=d.min_clip)
    g.add_argument("--max-clip", type=float, default=d.max_clip)
    g = p.add_argument_group("whisper")
    g.add_argument("--whisper-model", default=d.whisper_model, help="faster-whisper size (tiny/base/small/...) or ggml .bin")
    g.add_argument("--whisper-cpp", default=d.whisper_cpp, help="path to whisper.cpp whisper-cli to use instead")
    g.add_argument("--language", default=None, help="e.g. en, hi (default: auto)")
    g = p.add_argument_group("render")
    g.add_argument("--layout", choices=["auto", "face", "blur", "crop"], default=d.layout)
    g.add_argument("--no-captions", dest="captions", action="store_false")
    g.add_argument("--words-per-caption", type=int, default=d.words_per_caption)
    g.add_argument("--font", dest="caption_font", default=d.caption_font)
    g.add_argument("--caption-style", choices=["bold", "clean", "box"], default=d.caption_style)
    g.add_argument("--hinglish", action="store_true", help="Roman-Hindi captions + post captions (needs GEMINI_API_KEY)")
    g.add_argument("--no-effects", dest="effects", action="store_false", help="no punch-zoom / chat pop-ups")
    g.add_argument("--no-hook", dest="hook_card", action="store_false", help="no hook text in the first 2.5 s")
    g.add_argument("--no-title", dest="title_overlay", action="store_false")
    g = p.add_argument_group("AI")
    g.add_argument("--no-gemini", dest="gemini", action="store_false")
    g.add_argument("--gemini-model", default=d.gemini_model)
    g.add_argument("--no-flow", dest="flow_prompts", action="store_false")
    g = p.add_argument_group("live")
    g.add_argument("--sync-offset", type=float, default=d.sync_offset, help="seconds to add to chat times")
    g.add_argument("--buffer-minutes", type=float, default=d.buffer_minutes)
    g.add_argument("--keep-buffer", action="store_true")
    g = p.add_argument_group("YouTube auto-upload")
    g.add_argument("--youtube-upload", dest="youtube_upload", action="store_true",
                   help="upload the finished clips to YouTube (needs OAuth credentials, see youtube.py)")
    g.add_argument("--youtube-visibility", dest="youtube_visibility", default=d.youtube_visibility,
                   choices=["private", "unlisted", "public"], help="privacy of the uploads (default: unlisted)")
    g.add_argument("--youtube-category", dest="youtube_category", default=d.youtube_category,
                   help="YouTube category id (default: 20 = Gaming)")
    g.add_argument("--youtube-schedule", dest="youtube_schedule", action="store_true",
                   help="upload as scheduled at your chosen best times (see --youtube-slots) instead of posting instantly")
    g.add_argument("--youtube-slots", dest="youtube_slots", default=d.youtube_slots,
                   help="comma-separated local post times, e.g. '12:00,15:00,18:00,21:30'")
    g.add_argument("--youtube-min-gap", dest="youtube_min_gap", type=int, default=d.youtube_min_gap,
                   help="minutes between two scheduled uploads (default: 45)")
    return p


def settings_from(args) -> Settings:
    s = Settings()
    for f in fields(Settings):
        if hasattr(args, f.name) and getattr(args, f.name) is not None:
            setattr(s, f.name, getattr(args, f.name))
    return s


def print_spikes(spikes: list[Spike]) -> None:
    if not spikes:
        print("No chat spikes found. Try a lower --z or --min-rate.")
    for n, sp in enumerate(spikes, 1):
        print(f"  spike {n}: peak {sp.peak:7.1f}s  z={sp.z:5.1f}  rate={sp.rate:5.1f}/s  "
              f"window {sp.start:.0f}-{sp.end:.0f}s  chat: {' | '.join(sp.top_messages[:3])}")


def estimate_minutes(duration: float, by: str, clips: int, online: bool) -> str:
    """Rough time on a normal laptop (base Whisper). Online videos add download time."""
    mins = duration / 60
    per_clip = 0.3 * clips  # transcribe the window + render, 2 renders at a time
    if by == "chat":
        t = 0.5 + 0.03 * mins + per_clip
    elif by == "story":
        t = 0.5 + 0.22 * mins + per_clip  # whole-video transcript
    else:
        t = 0.5 + 0.06 * mins + per_clip  # loudness + scene changes
    lo, hi = max(1, round(t * 0.8)), max(2, round(t * 1.3))
    return f"about {lo}-{hi} min for a {mins:.0f}-min video" + (" (+ download time)" if online and by != "chat" else "")


def clean_url(u: str) -> str:
    """Drop share/tracking bits (?si=, &feature=, &t=) so the same video is always the same video."""
    import urllib.parse as up

    if not u.startswith(("http://", "https://")):
        return u.strip().strip('"')
    p = up.urlparse(u.strip())
    q = [(k, v) for k, v in up.parse_qsl(p.query) if k not in ("si", "feature", "pp", "t", "ab_channel")]
    if p.netloc.endswith("youtu.be") and p.path.strip("/"):
        return "https://www.youtube.com/watch?v=" + p.path.strip("/")
    if "/shorts/" in p.path:
        return "https://www.youtube.com/watch?v=" + p.path.split("/shorts/")[1].split("/")[0]
    return up.urlunparse(p._replace(query=up.urlencode(q)))


def main(argv=None) -> int:
    try:
        return _main(argv)
    except KeyboardInterrupt:
        return 130
    except Exception as e:  # noqa: BLE001 - show a readable reason instead of a crash dump
        import traceback

        msg = str(e) or e.__class__.__name__
        hint = ""
        low = msg.lower()
        if "yt-dlp" in low or "yt_dlp" in low or "http error" in low or "unable to download" in low:
            hint = " Check the link is public and opens in your browser, and that you are online."
        elif "ffmpeg" in low or "ffprobe" in low:
            hint = " ffmpeg could not read this video. Try another file or link."
        elif "whisper" in low:
            hint = " Speech-to-text failed. Try a smaller Whisper model in Settings."
        print(f"[error] {msg}.{hint}", flush=True)
        traceback.print_exc()
        return 1


def _main(argv=None) -> int:
    argv = sys.argv[1:] if argv is None else argv
    if argv[:1] == ["app"]:
        from .app import main as app_main, make_shortcut

        if "--shortcut" in argv:
            return make_shortcut()
        try:
            return app_main(browser="--browser" in argv, headless="--headless" in argv)
        except Exception:  # noqa: BLE001 - desktop app has no console: keep a crash log
            import traceback

            log = Path.home() / ".hypecut" / "app_error.log"
            log.parent.mkdir(parents=True, exist_ok=True)
            log.write_text(traceback.format_exc(), encoding="utf-8")
            raise
    args = build_parser().parse_args(argv)
    s = settings_from(args)
    out = Path(args.out)

    if shutil.which("ffmpeg") is None or shutil.which("ffprobe") is None:
        print("ffmpeg/ffprobe not found on PATH. Install ffmpeg first (see README).")
        return 1

    if args.demo:
        from .demo import make_demo

        video, chat = make_demo(Path("samples"))
        args.source, args.chat, args.mode = str(video), str(chat), "file"
    if not args.source:
        build_parser().print_help()
        return 1

    args.source = clean_url(args.source)
    is_url = args.source.startswith(("http://", "https://"))
    mode = args.mode or ("vod" if is_url else "file")

    if mode == "live":
        return live_loop(args.source, out, s, args.dry_run)

    # ---- file / vod: whole video known up front
    out.mkdir(parents=True, exist_ok=True)
    work = out / "_work"
    work.mkdir(parents=True, exist_ok=True)
    local = args.source  # a local copy of the media (for no-chat analysis), if we have one
    if mode == "file":
        duration = probe_duration(args.source)
        msgs = load_chat_file(args.chat) if args.chat else []
        resolve = lambda sp: (args.source, 0.0)  # noqa: E731
    else:
        msgs = []
        if args.chat:
            msgs = load_chat_file(args.chat)
        elif args.clip_by in ("auto", "chat"):
            try:
                msgs = fetch_vod_chat(args.source, out)
            except (SystemExit, Exception) as e:  # noqa: BLE001 - no chat replay is fine
                print(f"[chat] no chat replay for this video ({e})")
        if msgs:
            save_chat_jsonl(msgs, out / "chat.jsonl")
        duration = msgs[-1].t + 1 if msgs else 0
        local = None
        resolve = vod_resolver(args.source, work)

    for m in msgs:
        m.t += s.sync_offset
    all_words = None
    by = args.clip_by
    if by == "auto":
        enough = duration and len(msgs) >= max(30, duration / 60 * 3)  # at least ~3 messages a minute
        by = "chat" if enough else "nochat"
    if by == "chat":
        print(f"[mode] CHAT CLIPS: {len(msgs)} chat messages over {duration / 60:.1f} min")
    else:
        if local is None:  # online video without chat: a small 360p copy to analyse (fast download);
            local = download_full(args.source, work)  # the clips themselves are cut from HD sections
            duration = probe_duration(local)
        from .moments import has_video, is_talking_video, loudness_curve, scene_cuts

        print("[analyse] listening to the audio and checking scene changes ...", flush=True)
        loud = loudness_curve(local)
        cuts = scene_cuts(local) if has_video(local) else []
        if by == "nochat":
            by = "story" if is_talking_video(loud, cuts) else "video"
        print("[mode] " + ("NORMAL CLIPS: talking video, picking the best stories/quotes" if by == "story"
                           else "CLIPS WITHOUT CHAT: loud moments + fast scene changes"))
    print(f"[estimate] {estimate_minutes(duration, by, s.max_clips, is_url)}", flush=True)
    key = history_key(args.source)
    skip = [] if (args.allow_repeats or args.demo) else load_history().get(key, [])
    if skip:
        print(f"[history] skipping {len(skip)} moment(s) already clipped from this video in earlier runs")
    if by == "chat":
        spikes = detect_spikes(msgs, s, duration, exclude=skip)
    elif by == "video":
        from .moments import video_hype_spikes

        spikes = video_hype_spikes(local, s, duration, skip, loud, cuts)
    else:
        from .moments import best_moment_spikes

        from .moments import transcribe_full

        all_words = transcribe_full(local, s, work, duration)
        spikes = best_moment_spikes(local, s, duration, work, skip, loud, all_words)
    print(f"[spikes] picked {len(spikes)} moment(s):")
    print_spikes(spikes)
    if not spikes:
        print("No new moments left in this video: all the best parts were already clipped. "
              "Use --allow-repeats to clip them again.")
        return 0
    clips = run(spikes, resolve, msgs, out, s, render=not args.dry_run, all_words=all_words)
    if not args.dry_run and not args.demo:
        remember(key, [(c["stream_start"], c["stream_end"]) for c in clips])
    if not args.dry_run and s.youtube_upload and clips:
        from .youtube import upload_new_clips
        results = upload_new_clips(out, visibility=s.youtube_visibility, category=s.youtube_category,
                                   schedule=s.youtube_schedule, slots=s.youtube_slots,
                                   min_gap=s.youtube_min_gap)
        ok = sum(1 for r in results if r.get("ok"))
        print(f"[youtube] uploaded {ok}/{len(results)} clip(s) to YouTube")
    print(f"\nDone: {len(clips)} clip(s) in {out.resolve()}  (see index.md)")
    return 0


# ---------------------------------------------------------------- history (no repeat clips)
HISTORY = Path.home() / ".hypecut" / "history.json"


def history_key(source: str) -> str:
    if source.startswith(("http://", "https://")):
        return source.split("&")[0].rstrip("/").lower()
    return str(Path(source).resolve()).lower()


def load_history() -> dict:
    try:
        return json.loads(HISTORY.read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return {}


def remember(key: str, ranges: list[tuple[float, float]]) -> None:
    h = load_history()
    h.setdefault(key, []).extend([list(r) for r in ranges])
    HISTORY.parent.mkdir(parents=True, exist_ok=True)
    HISTORY.write_text(json.dumps(h, indent=1), encoding="utf-8")


def live_loop(url: str, out: Path, s: Settings, dry_run: bool) -> int:
    out.mkdir(parents=True, exist_ok=True)
    work = out / "_work"
    buf = LiveBuffer(url, work, s).start()
    chat = LiveChat(url, buf.t0).start()
    done_peaks: list[float] = []
    n = 0
    print("[live] watching chat. Ctrl+C to stop.")
    try:
        while True:
            time.sleep(1)
            if (out / "STOP").exists():  # the desktop app's Stop button
                break
            msgs = chat.snapshot()
            now = time.time() - buf.t0
            if len(msgs) < 20:
                continue
            for m in msgs:
                m.t += s.sync_offset
            _, smooth = activity_curve(msgs, s, now)
            z = zscores(smooth, s)
            # look for a finished spike: peak at least post_seconds ago, video recorded past the window
            for sp in detect_spikes(msgs, s, now, fill=False):
                if any(abs(sp.peak - p) < s.min_gap for p in done_peaks):
                    continue
                if sp.peak + s.post_seconds > buf.recorded_until() - 1:
                    continue  # wait for the buffer to catch up
                done_peaks.append(sp.peak)
                n += 1
                print(f"\n[live] SPIKE at {sp.peak:.0f}s (z={sp.z:.1f}) -> clipping")
                src, off = buf.cut(sp.start, sp.end, work / f"live_{n:03d}.ts")
                run([sp], lambda _sp, src=src, off=off: (src, off), msgs, out / f"live_{n:03d}", s, render=not dry_run)
                if s.youtube_upload:
                    from .youtube import upload_new_clips
                    upload_new_clips(out / f"live_{n:03d}", visibility=s.youtube_visibility, category=s.youtube_category,
                                     schedule=s.youtube_schedule, slots=s.youtube_slots,
                                     min_gap=s.youtube_min_gap)
                if n >= s.max_clips:
                    print(f"\n[live] made {n} clips (the limit for this run). Stopping.")
                    return 0
            buf.prune()
            if int(now) % 15 == 0:
                print(f"\r[live] t={now:6.0f}s  chat msgs={len(msgs)}  z={z[-2] if len(z) > 1 else 0:4.1f}  clips={n}",
                      end="", flush=True)
    except KeyboardInterrupt:
        print("\n[live] stopping")
    finally:
        chat.stop()
        buf.stop()
    return 0


if __name__ == "__main__":
    sys.exit(main())
