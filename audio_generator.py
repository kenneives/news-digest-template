"""
Audio Generator

Converts podcast script segments into a multi-voice MP3 using ElevenLabs
(preferred) or Edge-TTS (fallback) with pydub for assembly.

ElevenLabs provides higher-quality voices but requires an API key and has
usage quotas. If ElevenLabs is not configured or fails for any reason, the
entire episode falls back to Edge-TTS so the daily digest is never blocked.

Both engines rotate through curated voice pools daily so each episode
sounds fresh. ElevenLabs voices can be pinned via env vars; Edge-TTS
always rotates from its pool.

Requirements:
  - pip install edge-tts elevenlabs pydub
  - ffmpeg installed and on PATH (choco install ffmpeg, or https://ffmpeg.org/download.html)
  - Optional: ELEVENLABS_API_KEY + voice IDs for premium TTS
  - Optional: INTRO_MUSIC_PATH / OUTRO_MUSIC_PATH env vars pointing to MP3 files
"""

import asyncio
import hashlib
import os
import tempfile
from datetime import datetime, timedelta
from pathlib import Path

import edge_tts
from elevenlabs import ElevenLabs
from pydub import AudioSegment


# ---------------------------------------------------------------------------
# Curated voice pools for daily rotation
# ---------------------------------------------------------------------------

# Edge-TTS: en-US neural voices (free, decent quality)
EDGE_TTS_MALE_VOICES = [
    ("Guy", "en-US-GuyNeural"),
    ("Christopher", "en-US-ChristopherNeural"),
    ("Eric", "en-US-EricNeural"),
    ("Roger", "en-US-RogerNeural"),
    ("Steffan", "en-US-SteffanNeural"),
]
EDGE_TTS_FEMALE_VOICES = [
    ("Jenny", "en-US-JennyNeural"),
    ("Aria", "en-US-AriaNeural"),
    ("Michelle", "en-US-MichelleNeural"),
]

# ElevenLabs: premade voices suited for news/podcast style
ELEVENLABS_MALE_VOICES = [
    ("Brian", "nPczCjzI2devNBz1zQrb"),      # Deep narration
    ("Daniel", "onwK4e9ZLuTAKqWW03F9"),     # British news presenter
    ("Drew", "29vD33N1CtxCmqQRPOHJ"),       # Well-rounded news
    ("Charlie", "IKne3meq5aSn9XLyUdCD"),    # Casual conversational
    ("Chris", "iP95p4xoKVk53GoZ742B"),      # Casual conversational
    ("Bill", "pqHfZKP75CvOlQylNhV4"),       # Strong documentary
    ("Josh", "TxGEqnHWrfWFTfGW9XjX"),       # Deep narration
    ("Liam", "TX3LPaxmHKxFdv7VOQHJ"),      # Young narrator
]
ELEVENLABS_FEMALE_VOICES = [
    ("Alice", "Xb7hH8MSUJpSbSDYk0k2"),     # Confident news
    ("Sarah", "EXAVITQu4vr4xnSDxMaL"),     # Soft news
    ("Matilda", "XrExE9yKIg1WjnnlVkGX"),   # Warm audiobook
    ("Rachel", "21m00Tcm4TlvDq8ikWAM"),    # Calm narration
    ("Lily", "pFZP5JQG7iQjIQuC4Bku"),      # Raspy British narration
]

# Silence between speaker changes (milliseconds)
SPEAKER_PAUSE_MS = 300


def _pick_daily_voice(
    pool: list[tuple[str, str]], date_str: str, role: str
) -> tuple[str, str]:
    """Deterministically pick a voice from a pool based on date and role.

    Same date + role always returns the same voice. Different roles get
    different indices so Alex and Sam don't accidentally land on the same
    voice in mixed-gender pools.

    Returns:
        (display_name, voice_identifier) tuple.
    """
    key = f"{date_str}-{role}".encode()
    idx = int(hashlib.md5(key).hexdigest(), 16) % len(pool)
    return pool[idx]


async def _generate_segment_audio(text: str, voice: str, output_path: str) -> None:
    """Generate a single audio segment using Edge-TTS."""
    communicate = edge_tts.Communicate(text, voice)
    await communicate.save(output_path)


def _generate_segment_elevenlabs(
    text: str, voice_id: str, model_id: str, output_path: str
) -> None:
    """Generate a single audio segment using ElevenLabs."""
    client = ElevenLabs(api_key=os.getenv("ELEVENLABS_API_KEY"))
    audio_iter = client.text_to_speech.convert(
        text=text,
        voice_id=voice_id,
        model_id=model_id,
        output_format="mp3_44100_128",
    )
    with open(output_path, "wb") as f:
        for chunk in audio_iter:
            f.write(chunk)


def generate_audio(
    script_segments: list[tuple[str, str]],
    output_dir: str | Path,
    test_mode: bool = False,
) -> Path:
    """Generate a combined MP3 from podcast script segments.

    Args:
        script_segments: List of (speaker, dialogue) tuples from parse_script().
        output_dir: Directory to save the final MP3.
        test_mode: Currently unused (test mode is handled at script generation).

    Returns:
        Path to the final digest MP3 file.
    """
    output_dir = Path(output_dir)
    output_dir.mkdir(parents=True, exist_ok=True)

    today = datetime.now().strftime("%Y-%m-%d")
    output_file = output_dir / f"digest-{today}.mp3"

    print(f"  Generating audio for {len(script_segments)} segments...")

    # --- Voice selection (daily rotation) ---
    elevenlabs_key = os.getenv("ELEVENLABS_API_KEY", "")
    elevenlabs_model = os.getenv("ELEVENLABS_MODEL", "eleven_multilingual_v2")
    use_elevenlabs = bool(elevenlabs_key)

    if use_elevenlabs:
        # Pinned voices from env, or auto-rotate from curated pool
        pinned_alex = os.getenv("ELEVENLABS_VOICE_ALEX", "")
        pinned_sam = os.getenv("ELEVENLABS_VOICE_SAM", "")
        if pinned_alex and pinned_sam:
            elevenlabs_voices = {"Alex": pinned_alex, "Sam": pinned_sam}
            print(f"  ElevenLabs voices: Alex={pinned_alex}, Sam={pinned_sam} (pinned)")
        else:
            alex_name, alex_id = _pick_daily_voice(ELEVENLABS_MALE_VOICES, today, "Alex")
            sam_name, sam_id = _pick_daily_voice(ELEVENLABS_FEMALE_VOICES, today, "Sam")
            elevenlabs_voices = {"Alex": alex_id, "Sam": sam_id}
            print(f"  ElevenLabs voices: Alex={alex_name}, Sam={sam_name} (daily rotation)")

    # Edge-TTS always rotates
    edge_alex_name, edge_alex_voice = _pick_daily_voice(EDGE_TTS_MALE_VOICES, today, "Alex")
    edge_sam_name, edge_sam_voice = _pick_daily_voice(EDGE_TTS_FEMALE_VOICES, today, "Sam")
    edge_voices = {"Alex": edge_alex_voice, "Sam": edge_sam_voice}

    # Generate each segment as a temp file
    segment_files: list[str] = []
    with tempfile.TemporaryDirectory() as tmpdir:
        if use_elevenlabs:
            try:
                print("  Using ElevenLabs TTS...")
                for i, (speaker, dialogue) in enumerate(script_segments):
                    voice_id = elevenlabs_voices.get(
                        speaker, elevenlabs_voices["Alex"]
                    )
                    tmp_path = os.path.join(tmpdir, f"segment_{i:04d}.mp3")
                    _generate_segment_elevenlabs(
                        dialogue, voice_id, elevenlabs_model, tmp_path
                    )
                    segment_files.append(tmp_path)
                    print(
                        f"    Segment {i + 1}/{len(script_segments)} "
                        f"({speaker}): OK [ElevenLabs]"
                    )
            except Exception as e:
                print(f"  Warning: ElevenLabs failed: {e}")
                print("  Falling back to Edge-TTS for entire episode...")
                segment_files.clear()
                use_elevenlabs = False

        if not use_elevenlabs:
            print(f"  Using Edge-TTS — Alex={edge_alex_name}, Sam={edge_sam_name} (daily rotation)")
            for i, (speaker, dialogue) in enumerate(script_segments):
                voice = edge_voices.get(speaker, edge_voices["Alex"])
                tmp_path = os.path.join(tmpdir, f"segment_{i:04d}.mp3")
                asyncio.run(_generate_segment_audio(dialogue, voice, tmp_path))
                segment_files.append(tmp_path)
                print(
                    f"    Segment {i + 1}/{len(script_segments)} "
                    f"({speaker}): OK [Edge-TTS]"
                )

        # Assemble with pydub
        print("  Assembling final audio...")
        silence = AudioSegment.silent(duration=SPEAKER_PAUSE_MS)
        combined = AudioSegment.empty()

        prev_speaker = None
        for idx, (speaker, _) in enumerate(script_segments):
            segment_audio = AudioSegment.from_mp3(segment_files[idx])

            # Add silence between different speakers
            if prev_speaker is not None and speaker != prev_speaker:
                combined += silence

            combined += segment_audio
            prev_speaker = speaker

        # Optional intro music: fade in, play, fade out, 1s crossfade into podcast
        intro_path = os.getenv("INTRO_MUSIC_PATH", "")
        if intro_path and Path(intro_path).is_file():
            print("  Adding intro music...")
            intro = AudioSegment.from_file(intro_path)
            intro = intro.fade_in(1000).fade_out(2000)
            combined = intro.append(combined, crossfade=1000)

        # Optional outro music: 1s crossfade from podcast, fade in, play, fade out
        outro_path = os.getenv("OUTRO_MUSIC_PATH", "")
        if outro_path and Path(outro_path).is_file():
            print("  Adding outro music...")
            outro = AudioSegment.from_file(outro_path)
            outro = outro.fade_in(2000).fade_out(2000)
            combined = combined.append(outro, crossfade=1000)

        # Export as 128kbps CBR mono 44.1kHz MP3
        print(f"  Exporting to {output_file}...")
        combined.export(
            str(output_file),
            format="mp3",
            bitrate="128k",
            parameters=["-ac", "1", "-ar", "44100"],
        )

    print(f"  Audio saved: {output_file} ({len(combined) / 1000:.1f}s)")
    return output_file


def cleanup_old_audio(audio_dir: str | Path, keep_days: int = 10) -> None:
    """Delete digest MP3 files older than keep_days.

    Skips today's file to avoid deleting the just-generated episode.

    Args:
        audio_dir: Directory containing digest-*.mp3 files.
        keep_days: Number of days of audio to keep.
    """
    audio_dir = Path(audio_dir)
    if not audio_dir.is_dir():
        return

    today_str = datetime.now().strftime("%Y-%m-%d")
    cutoff = datetime.now() - timedelta(days=keep_days)

    for mp3_file in audio_dir.glob("digest-*.mp3"):
        # Skip today's file
        if today_str in mp3_file.name:
            continue

        # Extract date from filename: digest-YYYY-MM-DD.mp3
        try:
            date_part = mp3_file.stem.replace("digest-", "")
            file_date = datetime.strptime(date_part, "%Y-%m-%d")
            if file_date < cutoff:
                mp3_file.unlink()
                print(f"  Cleaned up old audio: {mp3_file.name}")
        except (ValueError, OSError) as e:
            print(f"  Warning: Could not process {mp3_file.name}: {e}")
