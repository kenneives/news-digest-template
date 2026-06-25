"""
Audiobookshelf Client

Triggers a library scan and provides the podcast URL for email links.

Requirements:
  - Audiobookshelf running in Docker on localhost:13378
  - API key from Settings > API Token
  - Library ID from: curl -H "Authorization: Bearer {token}" http://localhost:13378/api/libraries

Setup (Docker):
  docker run -d --name audiobookshelf \
    -p 13378:80 \
    -v /path/to/audio:/podcasts \
    -v /path/to/config:/config \
    -v /path/to/metadata:/metadata \
    --restart unless-stopped \
    ghcr.io/advplyr/audiobookshelf
"""

import requests


def trigger_library_scan(base_url: str, api_key: str, library_id: str) -> bool:
    """Trigger an Audiobookshelf library scan to pick up new audio files.

    Args:
        base_url: Audiobookshelf base URL (e.g., http://localhost:13378).
        api_key: API token from Audiobookshelf settings.
        library_id: Library UUID to scan.

    Returns:
        True if the scan was triggered successfully, False otherwise.
    """
    url = f"{base_url}/api/libraries/{library_id}/scan"
    headers = {"Authorization": f"Bearer {api_key}"}

    try:
        response = requests.post(url, headers=headers, timeout=30)
        response.raise_for_status()
        print(f"  Audiobookshelf library scan triggered successfully")
        return True
    except requests.RequestException as e:
        print(f"  Warning: Could not trigger Audiobookshelf scan: {e}")
        return False


def get_podcast_url(base_url: str) -> str:
    """Return the Audiobookshelf URL for linking in emails.

    Args:
        base_url: Audiobookshelf base URL (e.g., http://localhost:13378).

    Returns:
        The base URL suitable for use as a podcast link.
    """
    return base_url.rstrip("/")
