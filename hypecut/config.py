"""All tunable settings in one place. Every field maps to a CLI flag (see cli.py)."""
from __future__ import annotations

import os
from dataclasses import dataclass, field


@dataclass
class Settings:
    # --- spike detection -------------------------------------------------
    smooth_seconds: float = 3.0       # chat activity is summed over this sliding window
    baseline_seconds: float = 60.0    # rolling baseline length (seconds before the moment)
    z_threshold: float = 3.0          # how many std-devs above baseline counts as a spike
    min_rate: float = 2.0             # ignore "spikes" below this weighted msgs/sec (dead chats)
    min_gap: float = 30.0             # two spikes closer than this are merged
    max_clips: int = 6                # clips per run (strongest spikes first, then next-best moments)
    reaction_delay: float = 3.0       # chat reacts ~N s after the moment happened on stream

    # --- clip window -----------------------------------------------------
    pre_seconds: float = 20.0         # window starts this long before the chat peak
    post_seconds: float = 10.0        # ...and ends this long after it
    min_clip: float = 15.0            # best-cut length bounds (seconds)
    max_clip: float = 30.0

    # --- whisper -----------------------------------------------------------
    whisper_model: str = os.environ.get("HYPECUT_WHISPER_MODEL", "base")  # faster-whisper size OR ggml .bin path
    whisper_cpp: str | None = os.environ.get("HYPECUT_WHISPER_CPP")       # path to whisper-cli(.exe) to use whisper.cpp
    language: str | None = None       # None = auto-detect ("en", "hi", ...)

    # --- render ------------------------------------------------------------
    layout: str = "auto"              # auto (face tracking if a clear face, else blur) | face | blur | crop
    width: int = 720                  # output is 720p vertical (720x1280): small files, fast renders
    height: int = 1280
    captions: bool = True
    words_per_caption: int = 3
    caption_font: str = "Arial"
    caption_style: str = "bold"       # bold (yellow active word) | clean (white, no caps) | box (word on a yellow box)
    title_overlay: bool = True        # title bar at the top after the hook
    hook_card: bool = True            # big hook text for the first 2.5 s
    effects: bool = True              # punch-zoom on the moment + chat messages popping in
    hinglish: bool = False            # Roman-Hindi captions (Gemini transliteration of Hindi speech)

    # --- AI extras ---------------------------------------------------------
    gemini: bool = True               # used only if GEMINI_API_KEY is set
    gemini_model: str = os.environ.get("GEMINI_MODEL", "gemini-2.5-flash,gemini-2.5-flash-lite")  # tried in order
    flow_prompts: bool = True         # write a ready-to-paste Google Flow prompt per clip

    # --- YouTube auto-upload (see youtube.py) -------------------------------
    youtube_upload: bool = False      # CLI: upload finished clips after a run
    youtube_visibility: str = "public"  # private | unlisted | public (default: public posts)
    youtube_category: str = "20"      # YouTube category id (20 = Gaming)
    youtube_schedule: bool = False    # CLI: upload as scheduled at the best times
    youtube_slots: str = "12:00,15:00,18:00,21:30"  # local post times
    youtube_min_gap: int = 45         # minutes between two scheduled uploads

    # --- live mode -----------------------------------------------------------
    segment_seconds: int = 6          # rolling buffer segment size
    buffer_minutes: float = 10.0      # how much stream video to keep on disk
    sync_offset: float = 0.0          # add to chat times if chat and video drift apart
    keep_buffer: bool = False

    hype_words: dict[str, float] = field(default_factory=dict)  # extra/override weights
