# Daily News Digest

A configurable personal daily news digest powered by Claude. A Python pipeline that fetches news from 50+ RSS feeds, uses Claude to create a personalized summary tuned to *your* interests, optionally generates a two-host AI podcast, and delivers everything via email with an optional Audiobookshelf podcast link.

Fork it, drop in your API keys, edit the `INTERESTS` / reader profile / RSS feeds to match what you care about, and schedule it to run every morning.

## Features

- **50+ RSS sources** across tech, AI, robotics, finance, fintech, crypto, legal/regulatory, science, health, climate, automotive, global news, US news, local news, and more
- **Claude summarization** — filters hundreds of articles down to the best 20-30, organized by topic and prioritized by your configured interests
- **Two-host AI podcast** — generates a natural conversation between hosts "Alex" and "Sam" from the digest
- **Multi-voice TTS** — ElevenLabs (premium) with automatic Edge-TTS fallback
- **Audiobookshelf integration** — auto-uploads podcast episodes for streaming
- **Duplicate detection** — 7-day article history prevents repeating content
- **Error notifications** — emails you if the script fails, API credits run out, or no new articles are found
- **Graceful degradation** — podcast failures don't block email delivery; TTS failures fall back silently

---

## Architecture

```
RSS Feeds (50+)
      │
      ▼
 news_digest.py ──── Claude ──── HTML Email
      │
      ▼
 podcast_generator.py ──── Local LLM (Ollama)
      │
      ▼
 audio_generator.py ──── ElevenLabs / Edge-TTS
      │
      ▼
 audiobookshelf_client.py ──── Audiobookshelf library scan
```

| File | Purpose |
|------|---------|
| `news_digest.py` | Main entry point — fetches RSS, calls Claude, sends email, orchestrates podcast pipeline |
| `podcast_generator.py` | Generates a two-host podcast script via local LLM (Ollama/LM Studio) |
| `audio_generator.py` | Converts script segments to multi-voice MP3 with pydub assembly |
| `audiobookshelf_client.py` | Triggers Audiobookshelf library scan and provides podcast URL for email |

---

## Quick Start

### 1. Install dependencies

```bash
pip install -r requirements.txt
```

**System requirement:** [ffmpeg](https://ffmpeg.org/) must be on your PATH (required for MP3 encoding).

### 2. Configure environment variables

```bash
cp .env.example .env
```

Edit `.env` with your credentials. At minimum you need:

| Variable | Description |
|----------|-------------|
| `ANTHROPIC_API_KEY` | Get from https://console.anthropic.com/ |
| `GMAIL_ADDRESS` | Your Gmail address (sender) |
| `GMAIL_APP_PASSWORD` | 16-char app password (Google Account > Security > 2-Step Verification > App passwords) |
| `RECIPIENT_EMAIL` | Comma-separated recipient list |

### 3. Make it yours

Open `news_digest.py` and customize:

- **`INTERESTS`** — the priority-tiered list of topics Claude weights. The top "must-watch topics" tier ships with clearly-labeled `Acme AI` / `Example Co` / `Example Competitor` placeholders — replace them with the companies or topics you always want surfaced.
- **`RSS_FEEDS`** — add or remove sources. Add your local news feeds where marked.
- **Reader profile** — inside `summarize_with_claude()` there's an example "READER PROFILE" (the fictional reader "Alex, a product manager interested in tech"). Edit it to describe yourself so the digest and Job Radar are tuned to your background.

### 4. Run

```bash
python news_digest.py
```

This fetches news, generates the digest email, and (if the podcast is configured) generates and uploads a podcast episode.

---

## Podcast Setup (Optional)

The podcast pipeline requires a local LLM and a TTS engine. If `AUDIO_OUTPUT_DIR` is not set, the podcast step is skipped entirely.

### Local LLM (Ollama)

Install [Ollama](https://ollama.ai/) and pull a model:

```bash
ollama pull qwen2.5:14b
```

### Audiobookshelf (Optional)

Run Audiobookshelf via Docker to host podcast episodes:

```bash
docker run -d --name audiobookshelf \
  -p 13378:80 \
  -v /path/to/audio:/podcasts \
  -v /path/to/config:/config \
  -v /path/to/metadata:/metadata \
  --restart unless-stopped \
  ghcr.io/advplyr/audiobookshelf
```

### Podcast environment variables

| Variable | Default | Description |
|----------|---------|-------------|
| `AUDIO_OUTPUT_DIR` | *(none)* | Directory for generated MP3s — **setting this enables the podcast pipeline** |
| `LOCAL_LLM_URL` | `http://localhost:11434` | Ollama API endpoint |
| `LOCAL_LLM_MODEL` | `qwen3.5:9b` | Model for script generation |
| `AUDIOBOOKSHELF_URL` | `http://localhost:13378` | Audiobookshelf instance |
| `AUDIOBOOKSHELF_API_KEY` | *(none)* | API token from Audiobookshelf settings |
| `AUDIOBOOKSHELF_LIBRARY_ID` | *(none)* | Library UUID to scan |
| `ELEVENLABS_API_KEY` | *(none)* | Premium TTS (falls back to Edge-TTS if unset) |
| `ELEVENLABS_VOICE_ALEX` | *(auto-rotate)* | Pin a specific ElevenLabs voice for Alex |
| `ELEVENLABS_VOICE_SAM` | *(auto-rotate)* | Pin a specific ElevenLabs voice for Sam |
| `ELEVENLABS_MODEL` | `eleven_multilingual_v2` | ElevenLabs model |
| `INTRO_MUSIC_PATH` | *(none)* | MP3 file for intro music with fade-in |
| `OUTRO_MUSIC_PATH` | *(none)* | MP3 file for outro music with fade-out |
| `PODCAST_TEST_MODE` | `false` | Generate a ~2-minute sample instead of full episode |

### Audio details

- Output format: 128kbps CBR mono 44.1kHz MP3
- Filename pattern: `digest-YYYY-MM-DD.mp3`
- 300ms silence between speaker changes
- Daily voice rotation: same date always produces the same voice pairing
- Old audio files auto-cleaned after 10 days

---

## Optional: Remote sync

If a downstream consumer on another machine needs the fetched news/reddit signals, the script can best-effort `scp` `digest_history.json` to a remote host after each run. This is **off by default** — set `DIGEST_SYNC_HOST` (and optionally `DIGEST_SYNC_USER`, `DIGEST_SYNC_KEY`, `DIGEST_SYNC_PATH`) in `.env` to enable it. If `DIGEST_SYNC_HOST` is blank, the sync is skipped entirely.

---

## Scheduling

### macOS/Linux (cron)

```bash
crontab -e
```

Add:
```
0 8 * * * cd /path/to/news-digest && /usr/bin/python3 news_digest.py >> digest.log 2>&1
```

Optional log rotation (Linux/macOS):

1. Create `news-digest-logrotate.conf`:

```conf
/path/to/news-digest/digest.log {
  daily
  rotate 30
  compress
  missingok
  notifempty
  copytruncate
}
```

2. Run it daily:

```bash
0 7 * * * /usr/sbin/logrotate /path/to/news-digest/news-digest-logrotate.conf
```

The script also deletes rotated `digest.log.*` files older than `LOG_RETENTION_DAYS` (default 30).

Verify:
```bash
crontab -l
```

> **Note:** On macOS, cron may require Full Disk Access in System Preferences > Privacy.

### Windows (Task Scheduler)

**Option A: GUI**

1. `Win + R` > `taskschd.msc`
2. **Create Basic Task** > Name: `Daily News Digest`
3. Trigger: **Daily** at **8:00 AM**
4. Action: **Start a program**
   - Program: `python`
   - Arguments: `news_digest.py`
   - Start in: `C:\path\to\news-digest`
5. In Settings, enable "Run task as soon as possible after a scheduled start is missed"

**Option B: PowerShell**

```powershell
$action = New-ScheduledTaskAction -Execute "python" -Argument "news_digest.py" -WorkingDirectory "C:\path\to\news-digest"
$trigger = New-ScheduledTaskTrigger -Daily -At 8:00AM
$settings = New-ScheduledTaskSettingsSet -StartWhenAvailable -RestartCount 3 -RestartInterval (New-TimeSpan -Hours 1)
Register-ScheduledTask -TaskName "DailyNewsDigest" -Action $action -Trigger $trigger -Settings $settings -Description "Fetches news and emails daily digest"
```

---

## RSS Sources

| Category | Sources |
|----------|---------|
| **Tech & AI** | Hacker News, TechCrunch, TechCrunch AI, Ars Technica, The Verge, MIT Tech Review, VentureBeat AI, The Information |
| **Robotics** | IEEE Spectrum Robotics, The Robot Report |
| **Automotive & EVs** | Electrek, InsideEVs, The Drive |
| **Social** | Platformer |
| **Web3** | The Block, Decrypt, CoinDesk |
| **Fintech & Crypto Industry** | Finextra, CoinTelegraph, TechCrunch Fintech, Crunchbase News |
| **Legal & Regulatory** | Reuters Legal, The Register, Rest of World |
| **Health & Longevity** | STAT News, Longevity Technology |
| **Climate** | Canary Media, CleanTechnica |
| **Major News** | BBC News, Reuters, NPR, AP News, Al Jazeera |
| **US News** | Politico, The Hill, USA Today |
| **Local News** | *(add your local news feeds in `RSS_FEEDS`)* |
| **Finance** | Bloomberg, Financial Times, MarketWatch |
| **Science & Space** | Science Daily, Phys.org, Nature, NASA, Space.com |
| **Entertainment** | The Hollywood Reporter |

---

## Error Notifications

The script automatically emails you when something goes wrong:

| Error Type | What It Means |
|------------|---------------|
| **API Credits Exhausted** | Your Anthropic account has run out of credits |
| **API Rate Limit Exceeded** | Too many requests; wait or upgrade your plan |
| **API Authentication Failed** | Your API key is invalid or revoked |
| **Email Sending Failed** | Gmail credentials are wrong or app password expired |
| **No New Articles Found** | All articles were already sent previously (slow news day or feed issue) |
| **Unexpected Error** | Something else went wrong (full traceback included) |

---

## Customization

### Modify your interests

Edit the `INTERESTS` variable in `news_digest.py` to change what topics Claude prioritizes. Interests are organized by priority tier (Priority > High > Moderate) with strict exclusion filters. Replace the `Acme AI` / `Example Co` / `Example Competitor` placeholders in the top tier with the companies/topics you always want surfaced.

### Add/remove news sources

Edit the `RSS_FEEDS` dictionary in `news_digest.py`.

### Change article limits

Set `MAX_ARTICLES_PER_SOURCE` in `.env` (default: 20).

### Use a different Claude model

By default the script auto-resolves the latest Sonnet/Opus/Haiku (`USE_LATEST_MODELS=true`). Pin a primary model with `DIGEST_MODEL` in `.env` if you want a specific one.

---

## Troubleshooting

### Gmail "Less secure app" errors
- Make sure 2-Step Verification is enabled on your Google account
- Use an App Password, not your regular password
- App passwords are 16 characters with no spaces

### Cron job not running (macOS/Linux)
- Check the log file: `tail -f digest.log`
- Ensure Python path is correct: `which python3`
- macOS may require Full Disk Access for cron in System Preferences > Privacy

### Task Scheduler not running (Windows)
- Open Task Scheduler and check the task's **History** tab for errors
- Ensure "Run whether user is logged on or not" is set if needed
- Verify the Python path: `where python` in Command Prompt

### RSS feed errors
- Some feeds may be rate-limited or require different parsing
- Check console output for specific feed errors

### Podcast not generating
- Verify Ollama is running: `curl http://localhost:11434/v1/models`
- Ensure model is installed: `ollama pull qwen2.5:14b`
- Check that `ffmpeg` is on your PATH: `ffmpeg -version`
- Verify `AUDIO_OUTPUT_DIR` exists and is writable

### API credit issues
- Check your usage at https://console.anthropic.com/
- The script will email you when credits run low or are exhausted

---

## License

MIT — fork it, modify it, make it yours.
