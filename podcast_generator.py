"""
Podcast Script Generator

Generates a two-host conversational podcast script from the daily news digest
using a local LLM via OpenAI-compatible endpoint (Ollama or LM Studio).

Hosts:
  - Alex (male): Enthusiastic about tech breakthroughs
  - Sam (female): Analytical, asks good questions

Requirements:
  - Ollama running locally (default: http://localhost:11434)
  - A model installed (e.g., qwen3.5:9b): ollama pull qwen3.5:9b
  - Test: curl http://localhost:11434/v1/models
"""

import os
import re
import time
import logging

from bs4 import BeautifulSoup
import requests

logger = logging.getLogger(__name__)


def extract_text_from_html(html_content: str) -> str:
    """Extract plain text from HTML digest content using BeautifulSoup."""
    soup = BeautifulSoup(html_content, "html.parser")

    # Remove script and style elements
    for element in soup(["script", "style"]):
        element.decompose()

    text = soup.get_text(separator="\n", strip=True)

    # Collapse multiple blank lines
    text = re.sub(r"\n{3,}", "\n\n", text)

    return text.strip()


# Per-size read timeout (seconds) for the LLM request.
# Larger models need more time to generate a full podcast script.
# Maps size bucket (in billions) to timeout seconds.
MODEL_TIMEOUT_S = {8: 600, 14: 900, 30: 1200}

# Size strings Ollama uses in model names and parameter_size fields
_SIZE_PATTERN = re.compile(r"(\d+(?:\.\d+)?)[bB]")


def _parse_size_b(text: str) -> float | None:
    """Extract parameter size in billions from a string like '14b' or '14.8B'.

    For Ollama model names like 'qwen3.5:9b', parse from the tag portion
    (after ':') first so the family version number isn't mistaken for size.
    """
    # If the name contains a colon tag, parse the tag portion first
    if ":" in text:
        tag = text.split(":", 1)[1]
        m = _SIZE_PATTERN.search(tag)
        if m:
            return float(m.group(1))
    m = _SIZE_PATTERN.search(text)
    return float(m.group(1)) if m else None


def _timeout_for_model(model_name: str) -> int:
    """Return the appropriate timeout in seconds based on model size."""
    size = _parse_size_b(model_name)
    if size is None:
        return 600  # safe default
    # Round to nearest known bucket
    buckets = sorted(MODEL_TIMEOUT_S.keys())
    bucket = min(buckets, key=lambda b: abs(b - size))
    return MODEL_TIMEOUT_S[bucket]


def _ensure_model_available(llm_url: str, model_name: str) -> None:
    """Check if the configured model is available locally; pull it if not."""
    try:
        resp = requests.get(f"{llm_url}/api/tags", timeout=10)
        resp.raise_for_status()
        models = resp.json().get("models", [])
    except (requests.ConnectionError, requests.Timeout, requests.HTTPError):
        # Can't check — let the generation call fail with a clear error later
        logger.warning("Ollama not reachable at %s — skipping model check", llm_url)
        return

    local_names = [m["name"] for m in models]
    logger.info("Ollama has %d model(s): %s", len(local_names), ", ".join(sorted(local_names)))

    if model_name in local_names:
        logger.info("Model '%s' is available locally", model_name)
        return

    # Model not found — try to pull it
    logger.warning("Model '%s' not found locally. Pulling...", model_name)
    print(f"  Pulling model '{model_name}' — this may take a while...")
    try:
        pull_resp = requests.post(
            f"{llm_url}/api/pull",
            json={"name": model_name, "stream": False},
            timeout=900,
        )
        pull_resp.raise_for_status()
        logger.info("Successfully pulled model '%s'", model_name)
    except Exception as exc:
        logger.error("Failed to pull model '%s': %s", model_name, exc)
        raise RuntimeError(
            f"Required model '{model_name}' could not be pulled. "
            f"Install manually with: ollama pull {model_name}"
        ) from exc


def generate_podcast_script(digest_text: str, test_mode: bool = False) -> str:
    """Generate a two-host podcast script from digest text via local LLM.

    Args:
        digest_text: Plain-text version of the daily news digest.
        test_mode: If True, truncate input and target a ~2-minute script.

    Returns:
        Formatted script with ``Alex:`` / ``Sam:`` speaker labels.
    """
    llm_url = os.getenv("LOCAL_LLM_URL", "http://localhost:11434")
    llm_model = os.getenv("LOCAL_LLM_MODEL", "qwen3.5:9b")
    api_url = f"{llm_url}/api/chat"

    # Ensure the configured model is available (auto-pull if missing)
    _ensure_model_available(llm_url, llm_model)
    request_timeout = _timeout_for_model(llm_model)

    # Truncate digest text to fit within context window
    # Rough estimate: 1 token ≈ 4 characters. Reserve ~2000 tokens for system prompt + output.
    max_chars = 40000 if not test_mode else 4000
    if len(digest_text) > max_chars:
        digest_text = digest_text[:max_chars] + "\n\n[Content truncated for length]"
        print(f"  Digest text truncated to {max_chars} characters")

    duration_target = "about 2 minutes" if test_mode else "15-20 minutes"

    system_prompt = f"""You are a podcast script writer. Write a natural, engaging conversation \
between two hosts discussing today's tech news digest.

HOSTS:
- Alex (male): Enthusiastic about tech breakthroughs, gets excited about new developments, \
uses vivid analogies, sometimes makes pop-culture references.
- Sam (female): Analytical and thoughtful, asks probing follow-up questions, connects dots \
between stories, brings the business/practical perspective.

RULES:
1. Open with a brief, energetic intro where both hosts greet the audience.
2. Cover the most interesting stories from the digest naturally — do NOT just read headlines.
3. Use natural transitions between topics ("Speaking of AI...", "That reminds me of...", etc.).
4. Maximum 2-3 consecutive lines from the same speaker before the other responds.
5. Include genuine reactions: surprise, humor, skepticism, excitement.
6. End with a quick recap of the top takeaway and a sign-off.
7. Target length: {duration_target} of spoken audio (roughly 150 words per minute).
8. Do NOT include stage directions, sound effects, or parenthetical notes.
9. Each line MUST start with exactly "Alex:" or "Sam:" followed by a space and their dialogue.
10. Keep individual lines to 1-3 sentences for natural pacing.

OUTPUT FORMAT:
Return ONLY the script. Each line must begin with the speaker label.
Example:
Alex: Hey everyone, welcome back to the Daily Digest!
Sam: Great to be here. We've got some fascinating stories today.
Alex: Let's dive right in..."""

    user_prompt = f"""Here is today's news digest. Write the podcast script based on this content:

{digest_text}

"""

    # Embed system prompt in user message for better compatibility (some models ignore system role)
    combined_prompt = f"{system_prompt}\n\n---\n\n{user_prompt}"

    payload = {
        "model": llm_model,
        "messages": [
            {"role": "user", "content": combined_prompt},
        ],
        "stream": False,
        "keep_alive": "5m",
        "think": False,
        "options": {
            "num_predict": 4096 if not test_mode else 1024,
            "num_ctx": 8192,
            "temperature": 0.8,
        },
    }

    # Retry logic: Ollama may need time to load the model on first request,
    # or another process (e.g. trading bot) may be swapping models.
    max_retries = 5
    base_delay = 15  # seconds
    retryable_statuses = {400, 404, 409, 500, 503}
    for attempt in range(1, max_retries + 1):
        retry_delay = base_delay * (2 ** (attempt - 1))  # exponential backoff
        print(f"  Calling local LLM at {api_url}... (attempt {attempt}/{max_retries})")
        try:
            response = requests.post(api_url, json=payload, timeout=request_timeout)
        except requests.ConnectionError:
            if attempt < max_retries:
                print(f"  Local LLM not reachable, retrying in {retry_delay}s...")
                time.sleep(retry_delay)
                continue
            raise
        except requests.ReadTimeout:
            if attempt < max_retries:
                print(f"  Local LLM read timed out after {request_timeout}s, retrying in {retry_delay}s...")
                time.sleep(retry_delay)
                continue
            raise

        if response.status_code == 200:
            break

        if response.status_code in retryable_statuses and attempt < max_retries:
            reason = response.text[:200] if response.text else "(no body)"
            print(f"  Local LLM returned {response.status_code} ({reason}), "
                  f"retrying in {retry_delay}s...")
            # If model went missing (another process removed it), try to pull it again
            if response.status_code in (404, 400) and "not found" in response.text.lower():
                print(f"  Model '{llm_model}' appears to have been removed — re-pulling...")
                _ensure_model_available(llm_url, llm_model)
            time.sleep(retry_delay)
            continue

        print(f"  Local LLM error {response.status_code}: {response.text}")
        response.raise_for_status()

    result = response.json()
    script = result["message"]["content"].strip()

    # Strip <think> blocks that reasoning models (e.g. Qwen3.5) may emit
    script = re.sub(r"<think>.*?</think>\s*", "", script, flags=re.DOTALL)

    return script


def parse_script(script: str) -> list[tuple[str, str]]:
    """Parse a podcast script into speaker/dialogue segments.

    Args:
        script: Raw script text with ``Alex:`` and ``Sam:`` labels.

    Returns:
        List of (speaker, dialogue) tuples.
    """
    segments = []
    current_speaker = None
    current_lines = []

    for line in script.split("\n"):
        line = line.strip()
        if not line:
            continue

        # Check for speaker label at start of line
        match = re.match(r"^(Alex|Sam)\s*:\s*(.+)", line, re.IGNORECASE)
        if match:
            # Save previous segment
            if current_speaker and current_lines:
                segments.append((current_speaker, " ".join(current_lines)))

            current_speaker = match.group(1).capitalize()
            current_lines = [match.group(2).strip()]
        elif current_speaker:
            # Continuation of current speaker's dialogue
            current_lines.append(line)

    # Don't forget the last segment
    if current_speaker and current_lines:
        segments.append((current_speaker, " ".join(current_lines)))

    if not segments:
        raise ValueError("Could not parse any speaker segments from the script. "
                         "Expected lines starting with 'Alex:' or 'Sam:'.")

    return segments
