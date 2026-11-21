# ClipForge

A working AI clipping workspace — paste a link (YouTube, Twitch, Kick, 1000+ sites) or upload a file, describe what you want in plain language, and get viral-ready clips with animated captions and vertical reframing. Dark charcoal, Linear-inspired UI with a conversational agent.

## Quick start

1. Double-click `start.bat` (or run the command below).
2. Open **http://localhost:8000**.
3. Paste a link in the command bar, or open **Agent** and say *“clip the funniest moments from this stream”*.
4. Preview, download, or re-render with different settings.

```bash
venv\Scripts\python.exe -m uvicorn backend.main:app --host 127.0.0.1 --port 8000
```

First run downloads a ~75 MB Whisper model.

## What's new

### Link / livestream ingestion
- **Paste any URL** — YouTube, Twitch VODs/livestreams, Kick, and anything yt-dlp supports.
- **Subtitle-first**: if the source has captions (YouTube auto-captions, etc.), they're used instantly — no transcription, no video download.
- **Audio-only fallback**: otherwise only the audio track is downloaded (a few hundred MB for a 7h VOD, not multi-GB video), then transcribed locally.
- **Section downloads**: each selected clip is fetched as a short slice from the source, never the whole VOD. Verified: 10-second slice from a 7h38m Twitch VOD (your example link `twitch.tv/videos/2934887631`) downloaded in ~35s.

### Agent chat
- Natural-language requests: *“clip the funniest moments, 3 short clips, no captions”*.
- The agent extracts the URL, creative brief, clip count, length, captions and format from your message, starts the job, and reports back when it's done.
- Follow-ups like *“make them vertical with captions”* re-render existing clips.

### UI
- Charcoal/near-black workspace inspired by Linear, with an AI-native component vocabulary (task rows, tool chips, status states, elapsed-time loader, command bar).
- Three views: **Studio** (run jobs), **Agent** (chat), **Clips** (session results).
- Restrained accents, hairline borders, gliding hover states — no generic purple-gradient AI look.

## Honest limitations

- **Processing speed**: the 7h-in-30min claim of OpusClip/Klap comes from cloud GPU farms and cloud ASR. Locally, a 7-hour VOD with no caption track will take hours to transcribe on a CPU (a 34s clip takes ~15–60s). Sources with existing captions are near-instant.
- **YouTube bot checks**: unauthenticated YouTube may be blocked. Set `YT_COOKIES_FROM_BROWSER=chrome` in `.env` to use your browser cookies (or use Twitch/Kick/direct links).
- **Live streams**: resolves live URLs, but "realtime clipping while broadcasting" in the cloud sense isn't replicated — it processes VODs and recorded streams.
- The `ANYMODEL_API_KEY` is stored but unused (base URL unresolved). NVIDIA free models power all AI.

## Configuration

| Variable | Purpose |
|---|---|
| `NVIDIA_API_KEY` | Free NVIDIA NIM models for clip selection + agent (verified working) |
| `YT_COOKIES_FROM_BROWSER` | Optional browser name for YouTube cookie auth |
| `WHISPER_MODEL` | `tiny` (default, fast) or `base` (slower, more accurate) |
| `WHISPER_BEAM` | `1` (default) |

## Structure

```
backend/
  main.py       # API, job queue, URL + upload + chat endpoints
  ai.py         # free-model fallback chain, goal-aware clip selection
  agent.py      # intent parsing + conversational replies
  sources.py    # yt-dlp resolve, subtitle download, audio download, section download
  transcribe.py # faster-whisper (PyAV compat shim)
  clips.py      # ffmpeg cut, ASS captions, vertical reframe
frontend/       # index.html, style.css, app.js (Linear-inspired workspace)
work/           # uploads, audio, sections, subs, clips (gitignored)
```

## API

- `POST /api/source` (JSON `{"url":...}`) — resolve a link, return metadata
- `POST /api/clip-url` (form: url, num_clips, clip_length, captions, vertical, goal)
- `POST /api/upload` (multipart file + options)
- `POST /api/chat` (form: message, optional job_id)
- `GET /api/status/{id}`, `GET /api/clips/{id}/{n}`, `POST /api/render/{id}`, `GET /api/transcript/{id}`
