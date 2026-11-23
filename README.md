<p align="center"><img src="assets/hypecut-banner.png" alt="HypeCut: AI viral clips auto-editor" width="640"></p>

# HypeCut

**Chat knows what's clip-worthy before anyone else does.** This tool watches a stream's chat, and the moment it explodes (`KEKW`, `CLIP IT`, `WASTED`, `5 STARS`...) it grabs the 30 seconds around it, lets Whisper find the best 15–30 s inside, and renders a vertical 9:16 clip with word-by-word captions, a hook, a title and a ready-to-post caption.

Built for gaming streams. Works on any Twitch / YouTube stream or VOD.

**Free. Runs on your own laptop. All you need is a free Gemini API key** ([get one here](https://aistudio.google.com/apikey)).

**6 clips per run**, each from a different hype moment. Run the same video again and you get 6 new moments, never repeats.

```
 Twitch / YouTube chat ──► spike detector ──► 30 s window ──► Whisper ──► best 15-30 s cut
   (live IRC or replay)    (msgs/sec z-score     (20 s before,   (word       (chat hype + loudness
                            + hype-word weights)  10 s after)     timings)     + speech, snapped
                                                                               to sentence edges)
                                                                                    │
      clips/clip_01.mp4  ◄── ffmpeg render ◄── hook + title + caption ◄────────────┘
      clip_01.md / .json     9:16, captions,    (Gemini if GEMINI_API_KEY,
      index.md               punch-zoom, chat    otherwise offline rules)
      AGENT_BRIEF.md         pop-ups
```

## What you get per clip

| File | What's in it |
|---|---|
| `clip_01.mp4` | 720×1280 (720p) vertical clip: hook card (first 2.5 s), title bar, word-by-word captions, punch-zoom on the moment, chat messages flying in |
| `clip_01.md` | Viral score (0–100), hook, post caption + 3 hashtags, transcript, what chat said, a Google Flow prompt |
| `clip_01.json` | Everything above, machine-readable (timestamps, word timings, scores) |
| `index.md` | All clips ranked best first |
| `AGENT_BRIEF.md` | Optional prompt to finish the clips with an AI agent (better hooks, captions, extra effects) |

## Easiest install: let Claude Code do it

If you have [Claude Code](https://claude.com/claude-code), open it in any folder and paste:

> Install HypeCut for me from https://github.com/<you>/hypecut . Install Python 3.10+ and ffmpeg if they're missing, clone the repo, run `pip install -e .` inside it, then run `python -m hypecut app --shortcut` to put the HypeCut icon on my Desktop, and open the app with `python -m hypecut app`.

Then paste your Gemini key in the app's **Settings** tab and you're done.

## Install by hand (5 minutes)

1. **Python 3.10+** and **ffmpeg**
   - Windows: `winget install Gyan.FFmpeg`
   - macOS: `brew install ffmpeg`
   - Linux: `sudo apt install ffmpeg`
2. Get the code and the Python packages:
   ```bash
   git clone https://github.com/<you>/hypecut
   cd hypecut
   pip install -e .
   ```
   This installs the HypeCut app and the `hypecut` command.
3. Put a HypeCut icon on your Desktop (optional):
   ```bash
   python -m hypecut app --shortcut
   ```

## Open the app

Double-click the **HypeCut** icon, or run:
```bash
python -m hypecut app
```
HypeCut opens in its own window. Everything runs on your computer.

- **Clip tab:** paste a **Video link** (finished stream) or a **Channel link** (live). You can also pick your own video + chat file, or run the **Demo**. Press **Start**. Progress shows live under **Runs**; a live run keeps clipping until you press **Stop** (or reaches its clip limit).
- **My clips tab:** every clip plays right in the app, ranked by viral score. **Copy caption** copies the caption and hashtags, **Download** saves the MP4, and **Flow prompt** copies the AI-intro prompt.
- **Settings tab:** paste your free Gemini key (https://aistudio.google.com/apikey) for better hooks, titles, captions and Hinglish. It's saved only on your computer. You can also pick the clips folder and the Whisper model.
- **Founder tab:** who made HypeCut.
- **Clip options:** clips per run (default 6), clip length, spike sensitivity, language, caption style, layout, effects.

Clips are saved in `Desktop/HypeCut clips/`. Prefer the terminal? Everything below works without the app.

## Try it in 1 minute (no stream needed)

```bash
hypecut --demo
```
This builds a 3-minute fake "stream" (a test pattern plus your computer's text-to-speech voice) and a matching chat log in `samples/`, then clips it. Open `clips/index.md`. The first run downloads the Whisper model (~150 MB).

## Use it

```bash
# A finished stream (VOD): reads the chat replay, downloads only the spike windows
hypecut "https://www.twitch.tv/videos/123456789" --out clips/
hypecut "https://www.youtube.com/watch?v=XXXX" --out clips/

# A live stream: keeps a rolling 10-min buffer and clips while it runs (Ctrl+C to stop)
hypecut "https://www.twitch.tv/somechannel" --mode live --out clips/

# Your own recording + a chat log
hypecut my_stream.mp4 --chat my_chat.jsonl --out clips/

# Just show the spikes and the chosen cuts, don't render video
hypecut my_stream.mp4 --chat my_chat.jsonl --dry-run
```

Chat log formats for local files: `.jsonl` (`{"t": 12.3, "user": "x", "text": "KEKW"}`), `.csv` (`t,user,text`), `.txt` (`[00:01:23] user: message`), or a yt-dlp `live_chat.json`. See `samples/demo_chat.jsonl`.

## Useful options

| Flag | Default | What it does |
|---|---|---|
| `--z` | 3.0 | Spike threshold in standard deviations above the recent baseline. Lower = more clips |
| `--min-rate` | 2.0 | Ignore spikes in tiny chats below this many weighted messages/sec |
| `--max-clips` | 5 | Keep the N strongest spikes |
| `--pre` / `--post` | 20 / 10 | Seconds kept before / after the chat peak |
| `--min-clip` / `--max-clip` | 15 / 30 | Length range for the best cut |
| `--reaction-delay` | 3 | Chat reacts a few seconds after the moment; shifts the scoring back |
| `--whisper-model` | base | `tiny`, `base`, `small`, `medium`, `large-v3` (bigger = slower, better) |
| `--whisper-cpp PATH` | | Use a whisper.cpp `whisper-cli` instead of faster-whisper (with `--whisper-model ggml-*.bin`) |
| `--language` | auto | Force the speech language, e.g. `en`, `hi` |
| `--hinglish` | off | Roman-Hindi captions + Hinglish post captions (needs a Gemini key) |
| `--layout` | blur | `blur` (video centred on a blurred copy) or `crop` (fill the screen) |
| `--caption-style` | bold | `bold` (yellow active word), `clean`, `box` |
| `--no-effects` / `--no-hook` / `--no-title` | | Turn off punch-zoom + chat pop-ups / hook card / title bar |
| `--no-gemini` / `--no-flow` | | Skip the AI step / the Flow prompts |
| `--sync-offset` | 0 | Seconds to add to chat times if chat and video drift apart |
| `--youtube-upload` | off | Upload the finished clips to YouTube after the run (VOD and live mode) |
| `--youtube-visibility` | unlisted | `private`, `unlisted` or `public` |
| `--youtube-category` | 20 | YouTube category id (20 = Gaming) |

All settings live in `hypecut/config.py`. Hype words and their weights are in `hypecut/spikes.py` (`HYPE_WORDS`); add your community's emotes there.

## How a clip is picked

1. **Spike:** chat messages per second, each weighted (1 + bonus for hype words, emotes, ALL CAPS, 😂💀), smoothed over 3 s and compared with the median of the previous 60 s. A z-score above `--z` is a spike. Spikes closer than 30 s are merged.
2. **Window:** 20 s before the chat peak to 10 s after.
3. **Best cut:** Whisper transcribes the window with word timings. Every start/end at a sentence edge or pause is tried. Score = 50% chat hype inside the cut (shifted back by the reaction delay) + 30% loudness + 20% how much of it has speech. The cut must contain the moment.
4. **Hook + caption:** Gemini ranks the clips and writes the hook, title, caption and hashtags. Without a key, offline rules turn what chat spammed (gator, 5 stars, wasted...) into the hook. Every hook gets a 0–100 score; the clip gets a 0–100 viral score (chat spike 55%, cut 20%, hook 25%).

## YouTube auto-upload (Shorts)

After a run finishes, HypeCut can upload every clip straight to YouTube — title
from Gemini, caption + hashtags in the description, tags from the hashtags,
category Gaming, and privacy `unlisted` by default (switch to `public` when
you're ready). 15–30 s vertical clips become YouTube **Shorts** automatically.

**One-time setup (free, ~5 minutes):**

1. Go to [Google Cloud Console](https://console.cloud.google.com/) and create a project.
2. **APIs & Services → Library** → search **YouTube Data API v3** → **Enable**.
3. **APIs & Services → Credentials → Create credentials → OAuth client ID** →
   Application type: **Desktop app** → Create.
4. Copy the **client ID** and **client secret** into the app's
   **Settings → YouTube auto-upload**, press **Connect YouTube**, and sign in
   with the Google account that owns the channel you want to upload to.
5. Pick a visibility, optionally tick **Auto-upload after every run**, and save.

From the terminal the same thing works with flags:

```bash
hypecut "https://www.youtube.com/watch?v=XXXX" --out clips/ --youtube-upload --youtube-visibility public
```

- Uploads are resumable and show up in the run's log. Each clip is only
  uploaded once: the result is written back into its `clip_XX.json`.
- In **My clips**, a **⬆ YouTube** button uploads that one clip; once uploaded
  it becomes a **▶ YouTube** link.
- Credits go to the streamer: the description always includes the source link,
  and the default visibility is `unlisted` so nothing goes public by accident.

## Gemini and Google Flow

- **Gemini** (optional): set `GEMINI_API_KEY`. Models are tried in order (`--gemini-model "gemini-2.5-flash,gemini-2.5-flash-lite"`). Only text is sent: transcripts and chat lines. No video leaves your computer. Transcripts and chat are passed as data, and the prompt tells the model to ignore any instructions inside them.
- **Google Flow** has no public API. Each clip's `.md` has a ready-to-paste Flow prompt for a 3-second AI intro shot. Paste it into Flow yourself and put the result in front of the clip.

## Limitations

- Live mode and Twitch VOD chat need extra tools: `pip install streamlink chat-downloader`. Live mode's chat/video sync is approximate; use `--sync-offset` if clips start late or early.
- Chat replay for YouTube VODs comes from yt-dlp `live_chat`; it only exists for streams that had live chat replay.
- Spikes measure hype, not quality. Raids, giveaways and "!command" spam can trigger clips; raise `--z` or remove those words.
- Whisper `base` is fast but makes mistakes on game noise and Hindi. Use `--whisper-model small` or larger for better captions. Hindi works better with `--language hi`.
- Captions use the Arial font by default. Hindi (Devanagari) needs a font that has it, e.g. `--font "Nirmala UI"` on Windows.

## Please clip responsibly

This tool is for **your own streams, or streams you have permission to clip**. Streamers own their content. Respect Twitch's, YouTube's and the streamer's rules, credit the streamer in every post, and don't re-upload whole streams. GTA 6 is a trademark of Rockstar Games/Take-Two; this project is not affiliated with them.

## Credits

Ideas borrowed from other open-source clippers, e.g. [PeakCut](https://github.com/haziqahmedqasmi/PeakCut) (MIT): AI models tried in order with fallbacks, a no-key fallback, and treating transcripts as data in prompts. Built with [faster-whisper](https://github.com/SYSTRAN/faster-whisper), [whisper.cpp](https://github.com/ggerganov/whisper.cpp), [yt-dlp](https://github.com/yt-dlp/yt-dlp), [streamlink](https://streamlink.github.io/), [chat-downloader](https://github.com/xenova/chat-downloader) and ffmpeg.

© 2026 Kunal Mhetre. HypeCut is source-available, not open source: you can read the code and use HypeCut for yourself, but you may not copy, redistribute, sell or rebrand it. See [LICENSE](LICENSE). "HypeCut" and the HypeCut logo are trademarks of JR Revenue Systems. Made by [@kunalthinks_ai](https://instagram.com/kunalthinks_ai).
