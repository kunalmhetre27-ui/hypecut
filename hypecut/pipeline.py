"""Spike -> window -> Whisper -> best cut -> hooks/captions (Gemini or offline) -> vertical clip + sidecars."""
from __future__ import annotations

import json
from pathlib import Path
from typing import Callable

import numpy as np

from .ai import flow_prompt, gemini_enrich, offline_caption, to_hinglish
from .bestcut import best_cut
from .chat import ChatMessage
from .config import Settings
from .hooks import hook_score, offline_hook, viral_score
from .render import render_vertical, write_ass
from .spikes import Spike, message_weight
from .transcribe import Word, extract_wav, transcribe

# resolver(spike) -> (video path, stream time that the file's 0:00 corresponds to)
Resolver = Callable[[Spike], tuple[str, float]]


def analyse_spike(i: int, spike: Spike, src: str, src_offset: float, msgs: list[ChatMessage],
                  work: Path, s: Settings, all_words: list | None = None) -> dict:
    win_start_in_src = spike.start - src_offset
    win_len = spike.end - spike.start
    wav = extract_wav(src, win_start_in_src, win_len, work / f"window_{i:02d}.wav")
    if all_words is not None:  # already have the whole-video transcript: just slice it
        a, b = spike.start, spike.end
        words = [Word(w.start - a, w.end - a, w.text) for w in all_words if w.start >= a and w.end <= b]
    else:
        print(f"[whisper] clip {i}: transcribing {win_len:.0f}s window ...")
        words = transcribe(wav, s)

    # chat activity inside the window, shifted back by the reaction delay (chat mode only)
    delay = s.reaction_delay if spike.kind == "chat" else 0.0
    curve = np.zeros(int(np.ceil(win_len)) + 1)
    for m in msgs:
        k = int(m.t - delay - spike.start)
        if 0 <= k < len(curve):
            curve[k] += message_weight(m.text, s.hype_words)
    event_t = min(max(spike.peak - delay - spike.start, 0.0), win_len)
    cut = best_cut(words, wav, win_len, curve, event_t, s.min_clip, s.max_clip)

    rel_words = [{"start": round(w.start - cut.start, 2), "end": round(w.end - cut.start, 2), "text": w.text}
                 for w in cut.words]
    return {
        "id": f"clip_{i:02d}",
        "source": src,
        "src_start": round(win_start_in_src + cut.start, 2),
        "stream_start": round(spike.start + cut.start, 2),
        "stream_end": round(spike.start + cut.end, 2),
        "duration": round(cut.end - cut.start, 2),
        "moment_at": round(event_t - cut.start, 2),  # seconds into the clip where the moment happens
        "chat_peak": spike.peak,
        "found_by": {"chat": "chat spike", "video": "video hype (no chat)", "story": "best moment (talking)"}[spike.kind],
        "z": round(spike.z, 2),
        "chat_rate": round(spike.rate, 2),
        "cut_score": round(cut.score, 3),
        "transcript": " ".join(w.text for w in cut.words),
        "top_chat": spike.top_messages,
        "words": rel_words,
    }


def add_hooks_and_captions(clips: list[dict], s: Settings) -> None:
    ai = gemini_enrich(clips, s.gemini_model, s.hinglish) if (s.gemini and clips) else None
    by_id = {a.get("id"): a for a in (ai or []) if isinstance(a, dict)}
    for c in clips:
        hook, title = offline_hook(c["transcript"], c["top_chat"])
        caption, tags = offline_caption(hook, title, c["transcript"])
        c.update({"hook": hook, "title": title, "caption": caption, "hashtags": tags, "ai": "offline"})
        a = by_id.get(c["id"])
        if a:
            c.update({k: a[k] for k in ("rank", "title", "hook", "caption", "hashtags", "why") if a.get(k)})
            c["ai"] = "gemini"
        c["hook_score"] = int(a["hook_score"]) if a and str(a.get("hook_score", "")).isdigit() else hook_score(c["hook"])
        c["viral_score"] = viral_score(c["z"], c["cut_score"], c["hook_score"])
        if s.hinglish and c["words"]:
            roman = to_hinglish([w["text"] for w in c["words"]], s.gemini_model)
            if roman:
                for w, r in zip(c["words"], roman):
                    w["text"] = r
        if s.flow_prompts:
            c["flow_prompt"] = flow_prompt(c)
    clips.sort(key=lambda c: (c.get("rank", 999), -c["viral_score"]))


def run(spikes: list[Spike], resolve: Resolver, msgs: list[ChatMessage], out: Path, s: Settings,
        render: bool = True, all_words: list | None = None) -> list[dict]:
    out.mkdir(parents=True, exist_ok=True)
    work = out / "_work"
    work.mkdir(exist_ok=True)
    clips = []
    for i, sp in enumerate(spikes, 1):
        try:
            src, off = resolve(sp)
        except Exception as e:  # noqa: BLE001 - a moment whose window can't be downloaded is skipped
            print(f"[warn] moment {i} skipped (couldn't download its window): {e}", flush=True)
            continue
        print(f"[progress] analysing clip {i}/{len(spikes)}", flush=True)
        try:
            clips.append(analyse_spike(i, sp, src, off, msgs, work, s, all_words))
        except Exception as e:  # noqa: BLE001 - skip a broken moment, keep going
            print(f"[warn] moment {i} skipped: {e}", flush=True)

    add_hooks_and_captions(clips, s)

    def make(c: dict) -> dict:
        if render:
            words = [Word(w["start"], w["end"], w["text"]) for w in c["words"]]
            ass = write_ass(words, out / f"{c['id']}.ass", s, c.get("title"), c["duration"], c.get("hook"),
                            c["top_chat"], c["moment_at"]) if s.captions else None
            mp4 = out / f"{c['id']}.mp4"
            try:
                render_vertical(c["source"], c["src_start"], c["duration"], mp4, ass, s, c["moment_at"])
            except Exception as e:  # noqa: BLE001 - one bad clip must not stop the others
                print(f"[render] {mp4.name} failed with effects ({e}); retrying plain", flush=True)
                plain = Settings(**{**s.__dict__, "effects": False, "layout": "blur"})
                render_vertical(c["source"], c["src_start"], c["duration"], mp4, ass, plain, c["moment_at"])
            c["file"] = mp4.name
            print(f"[render] done {mp4.name}  {c['duration']:.1f}s  viral {c['viral_score']}/100", flush=True)
        (out / f"{c['id']}.json").write_text(json.dumps(c, indent=2, ensure_ascii=False), encoding="utf-8")
        (out / f"{c['id']}.md").write_text(_markdown(c), encoding="utf-8")
        return c

    print(f"[render] rendering {len(clips)} clips ...", flush=True)
    from concurrent.futures import ThreadPoolExecutor

    with ThreadPoolExecutor(max_workers=2 if len(clips) > 1 else 1) as ex:
        done = []
        for c in clips:
            done.append(ex.submit(make, c))
        for f in done:
            try:
                f.result()
            except Exception as e:  # noqa: BLE001
                print(f"[render] a clip could not be rendered: {e}", flush=True)
    clips = [c for c in clips if c.get("file") or not render]
    _index(clips, out)
    _claude_brief(clips, out)
    return clips


def _fmt(t: float) -> str:
    return f"{int(t // 3600)}:{int(t % 3600 // 60):02d}:{t % 60:04.1f}"


def _markdown(c: dict) -> str:
    tags = c["hashtags"] if isinstance(c.get("hashtags"), list) else [str(c.get("hashtags", ""))]
    lines = [f"# {c.get('title') or c['id']}", "",
             f"- **Viral score:** {c['viral_score']}/100 (hook {c['hook_score']}/100, chat z = {c['z']})",
             f"- **Hook (on screen, first 2.5 s):** {c['hook']}",
             f"- **Stream time:** {_fmt(c['stream_start'])} -> {_fmt(c['stream_end'])} ({c['duration']}s)",
             f"- **Chat spike:** {c['chat_rate']} weighted msgs/sec at {_fmt(c['chat_peak'])}",
             f"- **File:** {c.get('file', '(dry run, not rendered)')}  |  written by: {c['ai']}",
             "", "## Post caption (copy-paste)", "", c["caption"], "", " ".join(tags),
             "", "## Transcript", "", c["transcript"] or "_(no speech detected)_", "",
             "## What chat said", ""] + [f"- {m}" for m in c["top_chat"]]
    if c.get("flow_prompt"):
        lines += ["", "## Google Flow prompt (manual: paste into labs.google/flow)", "", "```", c["flow_prompt"], "```"]
    return "\n".join(lines) + "\n"


def _index(clips: list[dict], out: Path) -> None:
    rows = ["# Clips (best first)", "", "| # | file | viral | stream time | length | hook |", "|---|---|---|---|---|---|"]
    for n, c in enumerate(clips, 1):
        rows.append(f"| {n} | {c.get('file', c['id'])} | {c['viral_score']} | {_fmt(c['stream_start'])} | "
                    f"{c['duration']}s | {c['hook']} |")
    (out / "index.md").write_text("\n".join(rows) + "\n", encoding="utf-8")


def _claude_brief(clips: list[dict], out: Path) -> None:
    """Hand-off prompt for an AI coding agent (e.g. Claude Code) with creator skills installed."""
    files = ", ".join(c.get("file", c["id"]) for c in clips)
    text = f"""# Finish these clips with an AI agent (optional)

Paste this into Claude Code (or any agent) opened in this folder. It uses whatever creator skills you have
installed; skip the lines for skills you don't have.

> These are GTA 6 stream clips cut automatically where chat exploded: {files}.
> For each clip, read its .json (transcript, chat, hook, moment_at = seconds where the moment happens).
> 1. Hooks: write 3 hook options per clip (spoken + on-screen), score them, keep the best (e.g. an `ig-reel` skill).
> 2. Caption: rewrite the post caption and 3 hashtags (e.g. `ig-caption`, then `ig-human` to de-AI it).
> 3. Effects: add punch-zooms / emphasis on the moment and SFX on the hook (e.g. `add-zooms`, `effects`, `sound-effects`).
> 4. Keep every original file; write new versions as clip_XX_v2.mp4.
"""
    (out / "AGENT_BRIEF.md").write_text(text, encoding="utf-8")
