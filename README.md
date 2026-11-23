# NoetharaClipz

A working AI clipping workspace — paste a link (YouTube, Twitch, Kick, 1000+ sites) or upload a file, describe what you want in plain language, and get viral-ready clips with animated captions and vertical reframing. Dark charcoal dashboard UI with your logo, NOCTX branding, and a conversational agent.

## Quick start

1. Double-click `start.bat` (or run the command below).
2. Open **http://localhost:8000** for the landing page.
3. Choose **Login** or **Create account** on **/auth**, then use the dashboard at **/app**. All app APIs require a Supabase session.
4. Paste a link in the command bar, or open **Agent** and say *“clip the funniest moments from this stream”*.
5. Preview, download, or re-render with different settings.

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

### Free speed + reliability
- Parallel rendering (2 workers by default, up to 3) with automatic NVENC/QSV/AMF detection **and a real test encode** — if a GPU driver is unavailable, it silently falls back to CPU `libx264` instead of failing every clip.
- Content-hash caches for sections, transcripts, audio signals and AI selections — reclipping a source avoids repeat work.
- Draft renders at 360p for review before spending time on finals.
- Stream-copy remux path when no reframing/captions are needed.
- Cooperative cancel, per-clip retry with fresh HLS URL, and fresh-URL retry on failed section downloads.
- AI timeout is short by default (`AI_TIMEOUT=20`) with fast models first, so a stalled free provider cannot freeze a job.

### Accounts + public site
- **`/`** modern landing page, **`/auth`** login/sign-up/password recovery, **`/app`** protected dashboard.
- Supabase Auth email/password sessions; the anon/publishable key is the only key sent to the browser.
- Create-account uses a rate-limited backend endpoint so it works even when public Supabase signups are disabled. The service-role key stays server-side.
- “Remember this device” stores the session in local storage; otherwise it stays in session storage for the current tab.
- Shared `?job=<id>` links work after sign-in and restore the clip grid.

### UI
- Charcoal/near-black **NoetharaClipz v2** dashboard with your logo, compact sidebar, composer, session rail, clip cards, and editor shell.
- Four views: **Studio** (run jobs), **Agent** (chat), **Clips** (session results), **Library** (sources grouped by streamer), plus a clip preview editor.
- Real backend wiring: link preview, URL/upload/transcript jobs, live progress, results, chat, downloads, and re-renders.
- **Review before render**: rank moments first, pick checkboxes, scrub 360p drafts, regenerate a weak moment, then render only the keepers.
- **Continue clipping**: find the next batch from a long VOD while excluding moments already used.
- **Payoff-aware timing**: extends and merges clips through reactions like the granny sequence and suppressor killing spree instead of stopping on the setup.
- **Publishing queue**: prepare TikTok + YouTube Shorts titles, copy and exactly 5 tags, save locally, and copy a post package.
- **Cloud clip storage**: finished MP4s upload to the Supabase `rendered-clips` bucket and are removed from local disk after verification. The dashboard streams them through short-lived signed URLs, so no long-lived key reaches the browser.
- **Local memory**: caption dictionary, per-streamer crop presets, kept/flopped notes, publish queue, and cached transcripts/audio/sections survive restarts. Job and clip metadata is also mirrored into `noethara_jobs` / `noethara_clips` in Supabase.
- **Editor extras**: choose between 3 generated hooks with a live overlay, save the current facecam position as a streamer preset, and use J/K/L playback + 1/2/3 view shortcuts.
- Restrained accents, hairline borders, gliding hover states — no generic purple-gradient AI look.

## Making "7 hours in 30 minutes" real

The bottleneck is **transcription**, not clipping. Three paths, fastest first:

### 1. Groq ASR (recommended — minutes, not hours)
Groq's `whisper-large-v3-turbo` on LPU is extremely fast. ClipForge splits the audio into 10-minute chunks and transcribes **8 chunks in parallel**. A 7h VOD = ~42 chunks → a couple of minutes.

- Get a **free** key at https://console.groq.com (no credit card; 14,400 requests/day).
- Add to `.env`: `GROQ_API_KEY=gsk_...`
- Restart. The topbar chip changes to `⚡ Groq turbo`.
- Tune with `ASR_CHUNK_SECONDS` (default 600) and `ASR_MAX_WORKERS` (default 2, capped 1–3 — higher values trigger Groq 429s).

### 2. Free GPU notebook (no API key)
Run `notebooks/free_gpu_asr.ipynb` on **Kaggle** (30 GPU h/week, T4), **Colab** (T4), **Lightning AI** (80 GPU h/month), or **SageMaker Studio Lab**. It downloads audio, transcribes with `large-v3` on the T4 GPU, and exports `transcript.json`. Then in ClipForge click **↥ Import transcript** and paste the same URL — clip selection and rendering happen locally with zero local transcription.

### 3. Source captions (instant)
YouTube/Twitch sources with caption tracks are used directly — no ASR at all.

### 4. Local Whisper (free, slow)
Default when no Groq key. Fine for short videos; a 7h VOD takes hours on a CPU.

## Honest limitations

- **Processing speed without Groq/GPU**: OpusClip/Klap's 7h-in-30min comes from cloud GPU farms. On CPU-only local Whisper, a 7-hour VOD with no captions takes hours. Sources with existing captions are near-instant.
- **YouTube bot checks**: unauthenticated YouTube may be blocked. Set `YT_COOKIES_FROM_BROWSER=chrome` in `.env` to use your browser cookies (or use Twitch/Kick/direct links).
- **Live streams**: resolves live URLs, but "realtime clipping while broadcasting" in the cloud sense isn't replicated — it processes VODs and recorded streams.
- The `ANYMODEL_API_KEY` is stored but unused (base URL unresolved). NVIDIA free models power all AI.

## Configuration

| Variable | Purpose |
|---|---|
| `NVIDIA_API_KEY` | Free NVIDIA NIM models for clip selection + agent (verified working) |
| `YT_COOKIES_FROM_BROWSER` | Optional browser name for YouTube cookie auth |
| `ASR_BACKEND` | `auto` (default), `groq`, or `local` |
| `GROQ_API_KEY` | Free key from console.groq.com — enables fast parallel ASR |
| `GROQ_ASR_MODEL` | `whisper-large-v3-turbo` (default) |
| `ASR_CHUNK_SECONDS` | `600` (10 min chunks for parallel transcription) |
| `ASR_MAX_WORKERS` | `2` parallel chunk requests (capped 1–3; higher values trigger Groq 429s) |
| `AI_TIMEOUT` | `20` seconds per free-model call before failover |
| `RENDER_WORKERS` | `2` parallel clip renders (capped 1–3) |
| `RENDER_TIMEOUT` | `480` seconds per render before a hung ffmpeg is stopped |
| `SECTION_TIMEOUT` | `240` seconds per HLS section fetch before retrying |
| `WHISPER_MODEL` | `tiny` (local default, fast) or `base` (slower, more accurate) |
| `WHISPER_BEAM` | `1` (default) |
| `SUPABASE_URL` | Project REST URL, e.g. `https://<ref>.supabase.co` |
| `SUPABASE_ANON_KEY` | Publishable key exposed to the browser for Supabase Auth |
| `SUPABASE_SERVICE_ROLE_KEY` | Server-only key used for Storage uploads, metadata sync, and account creation |
| `SUPABASE_BUCKET` | Storage bucket for finished clips (`rendered-clips`) |
| `SUPABASE_SIGNED_TTL` | `3600` seconds — lifetime of playback URLs |
| `SUPABASE_TIMEOUT` | `120` seconds per upload |

To recreate the cloud tables in a fresh project, run `backend/supabase_schema.sql`.
One-time migration of existing local clips: `venv/Scripts/python.exe backend/migrate_to_supabase.py`.
`GET /api/cloud/status` reports the active bucket and connection state.

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
- `POST /api/clip-url` (form: url, num_clips, clip_length, captions, vertical, goal, review, draft)
- `POST /api/upload` (multipart file + options)
- `POST /api/chat` (form: message, optional job_id)
- `GET /api/status/{id}`, `GET /api/clips/{id}/{n}`, `POST /api/render/{id}`, `GET /api/transcript/{id}`
- `POST /api/render-selected/{id}`, `POST /api/continue/{id}`, `POST /api/regenerate/{id}/{n}`
- `POST /api/cancel/{id}`, `POST /api/retry/{id}/{n}`, `POST /api/retry-failed/{id}` (renders every failed or queued clip)
- `GET /api/library`, `GET /api/jobs/{id}`, `PUT /api/clips/{id}/{n}/note`
- `GET/POST /api/presets`, `POST /api/presets/bulk`, `GET/POST /api/dictionary`, `GET/POST /api/publish-queue`
- `GET /api/hardware`, `GET /api/asr/status`
