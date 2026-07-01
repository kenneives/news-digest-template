#!/usr/bin/env python3
"""
Daily News Digest Generator

Fetches news from RSS feeds, summarizes using Claude API,
and sends a personalized email digest.
"""

import hashlib
import json
import os
import random
import smtplib
import ssl
import sys
import time
import traceback
from dataclasses import dataclass
from datetime import datetime, timedelta, timezone
from email.mime.multipart import MIMEMultipart
from email.mime.text import MIMEText
from pathlib import Path
from typing import Optional
import re

import anthropic
import feedparser
import requests
from bs4 import BeautifulSoup
from dotenv import load_dotenv

from audio_generator import cleanup_old_audio, generate_audio
from audiobookshelf_client import get_podcast_url, trigger_library_scan
from podcast_generator import extract_text_from_html, generate_podcast_script, parse_script

load_dotenv()

# History file to track sent articles (prevents duplicates)
HISTORY_FILE = Path(__file__).parent / "digest_history.json"
MODEL_CACHE_FILE = Path(__file__).parent / "model_cache.json"
DEFAULT_LOG_FILE = Path(__file__).parent / "digest.log"

# =============================================================================
# Configuration
# =============================================================================

@dataclass
class Article:
    title: str
    link: str
    summary: str
    source: str
    published: Optional[datetime] = None


# Your interests - Claude will prioritize and contextualize based on these
INTERESTS = """
## PRIORITY INTERESTS (Feature prominently in Top Stories)

0. **My companies & must-watch topics** (ALWAYS include if ANY article matches — from ANY source)
   # Replace these with the companies/topics YOU want always-surfaced.
   _Example: your OWN companies (highest personal stakes — treat any mention as essential):_
   - Acme AI (example.com) — EXAMPLE: your startup. Briefly describe what it does so the model can
     recognize relevant news, competitors, funding signals, and partnerships. Treat as your #1 priority.
   - Example Co (example.com) — EXAMPLE: a second venture or topic you want tracked. Describe its space
     so category and fundraising-climate news gets surfaced too.
   _Example: a competitor to actively track:_
   - Example Competitor (example.com) — EXAMPLE: a direct rival. Note what to watch: new product
     launches, their developer program / open SDK / community ecosystem, funding rounds, and partnerships.
   - You have personal stakes in ALL of these — ANY news is personally important
   - Funding rounds, product launches, partnerships, and key announcements are CRITICAL
   - Include in Top Priority as ADDITIONAL items (never replace other top items)
   - SCAN ALL SOURCES: If any article mentions "Acme AI", "Example Co", or "Example Competitor",
     ALWAYS include it

1. **AI/ML & LLMs**
   - New AI tools, frameworks, and developer resources
   - Funding rounds and acquisitions in AI space
   - Business applications and industry adoption trends
   - Coding tips, tutorials, and best practices for AI development
   - Breakthrough research papers and their practical implications
   - Anthropic news specifically (Claude, company updates, research)

1b. **AI Agents — Trust, Identity, Reputation & Social** (an example deep-focus area — surface generously)
   - Trust scores, reputation systems, and verification/attestation for AI agents and bots
   - Agent identity: DIDs, verifiable credentials, agent authentication, agent registries/discovery
   - Autonomous agents transacting on blockchain/crypto: on-chain agents, agent wallets, agent payments
   - AI agents inside social networks; agent-to-agent social graphs; bots-as-peers platforms
   - Agent security & safety: scanning agent code, prompt-injection defense, agent safety standards
   - Agent marketplaces and emerging trust/identity standards or protocols for the "agent internet"
   - This is an example deep-focus niche; competitors, standards, funding, and research here are all
     highly relevant. Replace this tier with your own area of focus.

2. **Tech Job Market & Opportunities**
   - Companies actively hiring in tech/AI
   - Startup funding announcements (signals growth/hiring)
   - Layoffs or hiring freezes at major tech companies
   - Remote work trends and compensation data

3. **Robotics + AI Convergence**
   - Humanoid robots, industrial automation
   - AI-powered robotics breakthroughs
   - Companies like Boston Dynamics, Figure, Tesla Bot, etc.

4. **Bio-hacking & Longevity**
   - GLP-1 receptor agonists research (Ozempic, Mounjaro, etc.)
   - Supplement science and nootropics
   - Health optimization technology and wearables
   - Longevity research and anti-aging breakthroughs

## HIGH INTEREST (Include if noteworthy)

5. **Social Networks & Platforms**
   - Social media platform developments, policy changes, and new features
   - Decentralized social platforms (Bluesky, Mastodon, Farcaster, Lens)
   - Creator economy trends and monetization
   - Content moderation, algorithmic transparency, and platform governance
   - **Social + AI intersection**: AI-powered social features, recommendation systems, AI content detection
   - **Social + Blockchain/Web3 intersection**: decentralized identity, token-gated communities, on-chain social graphs
   - **Social + AI + Web3 convergence**: AI agents on social platforms, decentralized AI training on social data

6. **Web3 & Blockchain** (NO crypto price speculation)
   - Regulatory developments and legal clarity
   - Practical enterprise use cases
   - Infrastructure and developer tooling
   - SKIP: Price predictions, "to the moon" hype, memecoins

7. **Automotive Innovation**
   - Chinese EV manufacturers (BYD, NIO, Xpeng) and their tech
   - Suspension technology and driving dynamics
   - AI/self-driving developments (Tesla FSD, Waymo, etc.)
   - Range extension and battery technology
   - Performance car news relevant to your automotive interests (customize to your vehicles)

8. **Climate Tech**
   - Technology-driven environmental solutions
   - Marine conservation technology
   - Carbon capture and clean energy innovation
   - Sustainable transportation

## MODERATE INTEREST (Include selectively)

9. **Finance, Fintech & Crypto Industry**
   - M&A activity, major funding rounds, IPOs in fintech/crypto
   - Global macro signals: rate decisions, currency moves, recession indicators
   - Stock market catalysts: earnings surprises, sector rotations
   - Crypto industry news: exchange developments, DeFi milestones, institutional adoption
   - Fintech product launches: neobanks, payment rails, embedded finance
   - SKIP: Day-trading tips, price predictions, "get rich" schemes

10. **Legal & Regulatory Landscape**
    - Tech regulation: AI governance, antitrust actions, platform liability
    - Crypto/fintech regulation: SEC enforcement, stablecoin rules, CBDC developments
    - Automotive/EV policy: emissions rules, trade tariffs, safety mandates
    - Robotics & AI labor law: automation impact, liability frameworks
    - Global regulatory divergence: US vs EU vs Asia approaches
    - M&A antitrust: major deal approvals/blocks, FTC/DOJ actions
    - SKIP: Partisan framing, opinion pieces about regulation

11. **Entertainment**
   - Award-winning films and TV (Emmys, Oscars, critical acclaim)
   - Must-watch sci-fi releases
   - Popular streaming shows worth watching
   - SKIP: Celebrity gossip, relationship drama, tabloid content

12. **Space Exploration**
    - Major mission updates (NASA, SpaceX, etc.)
    - Scientific discoveries from space missions

13. **Biomedical Breakthroughs**
    - FDA approvals for significant treatments
    - Medical research with near-term patient impact

14. **Political & Economic Trends**
    - Factual policy changes affecting tech, business, or science
    - Economic indicators and market trends
    - SKIP: Partisan opinion pieces, political drama

## STRICT FILTERS (Always exclude)
- Celebrity gossip and entertainment drama
- Crypto price speculation and "get rich" schemes
- Partisan political commentary and opinion pieces
- Clickbait and sensationalized headlines
- Promotional content disguised as news
"""

# RSS Feeds organized by category
RSS_FEEDS = {
    # Tech & AI (Priority)
    "Hacker News": "https://hnrss.org/frontpage",
    "TechCrunch": "https://techcrunch.com/feed/",
    "TechCrunch AI": "https://techcrunch.com/category/artificial-intelligence/feed/",
    "Ars Technica": "https://feeds.arstechnica.com/arstechnica/index",
    "The Verge": "https://www.theverge.com/rss/index.xml",
    "MIT Tech Review": "https://www.technologyreview.com/feed/",
    "VentureBeat AI": "https://venturebeat.com/category/ai/feed/",
    "The Information": "https://www.theinformation.com/feed",

    # Robotics & Automation
    "IEEE Spectrum Robotics": "https://spectrum.ieee.org/feeds/topic/robotics",
    "The Robot Report": "https://www.therobotreport.com/feed/",

    # Automotive & EVs
    "Electrek": "https://electrek.co/feed/",
    "InsideEVs": "https://insideevs.com/rss/news/",
    "The Drive": "https://www.thedrive.com/feed",

    # Social Platforms & Policy
    "Platformer": "https://www.platformer.news/rss/",

    # Web3 & Blockchain (filtered by Claude for non-price content)
    "The Block": "https://www.theblock.co/rss.xml",
    "Decrypt": "https://decrypt.co/feed",
    "CoinDesk": "https://www.coindesk.com/arc/outboundfeeds/rss/",

    # Health & Longevity
    "STAT News": "https://www.statnews.com/feed/",
    "Longevity Technology": "https://longevity.technology/feed/",

    # Climate Tech
    "Canary Media": "https://www.canarymedia.com/feed",
    "CleanTechnica": "https://cleantechnica.com/feed/",

    # Major News Outlets
    "BBC News": "https://feeds.bbci.co.uk/news/rss.xml",
    "Reuters": "https://www.reutersagency.com/feed/",
    "NPR News": "https://feeds.npr.org/1001/rss.xml",
    "AP News": "https://rsshub.app/apnews/topics/apf-topnews",
    "Al Jazeera": "https://www.aljazeera.com/xml/rss/all.xml",

    # US News
    "Politico": "https://www.politico.com/rss/politicopicks.xml",
    "The Hill": "https://thehill.com/feed/",
    "USA Today": "https://www.usatoday.com/news/nation/",

    # Local News — add your local news RSS feeds here

    # Finance & Business
    "Bloomberg": "https://feeds.bloomberg.com/markets/news.rss",
    "Financial Times": "https://www.ft.com/rss/home",
    "MarketWatch": "https://feeds.marketwatch.com/marketwatch/topstories/",

    # Fintech & Crypto Industry
    "Finextra": "https://www.finextra.com/rss/headlines.aspx",
    "CoinTelegraph": "https://cointelegraph.com/rss",
    "TechCrunch Fintech": "https://techcrunch.com/category/fintech/feed/",
    "Crunchbase News": "https://news.crunchbase.com/feed/",

    # Legal & Regulatory
    "Reuters Legal": "https://www.reuters.com/legal/rss",
    "The Register": "https://www.theregister.com/headlines.atom",
    "Rest of World": "https://restofworld.org/feed/",

    # Science & Space
    "Science Daily": "https://www.sciencedaily.com/rss/all.xml",
    "Phys.org": "https://phys.org/rss-feed/",
    "Nature News": "https://www.nature.com/nature.rss",
    "NASA": "https://www.nasa.gov/rss/dyn/breaking_news.rss",
    "Space.com": "https://www.space.com/feeds/all",

    # Entertainment (filtered for quality)
    "The Hollywood Reporter": "https://www.hollywoodreporter.com/feed/",

    # Reddit — AI/Agent communities (public RSS, no auth needed)
    "Reddit r/artificial": "https://www.reddit.com/r/artificial/new/.rss",
    "Reddit r/MachineLearning": "https://www.reddit.com/r/MachineLearning/new/.rss",
    "Reddit r/LangChain": "https://www.reddit.com/r/LangChain/new/.rss",
    "Reddit r/AI_Agents": "https://www.reddit.com/r/AI_Agents/new/.rss",
    "Reddit r/LocalLLaMA": "https://www.reddit.com/r/LocalLLaMA/new/.rss",
    "Reddit r/programming": "https://www.reddit.com/r/programming/new/.rss",
    "Reddit r/SideProject": "https://www.reddit.com/r/SideProject/new/.rss",
}

# =============================================================================
# Duplicate Detection
# =============================================================================

def get_article_hash(article: Article) -> str:
    """Generate a unique hash for an article based on title and link."""
    unique_str = f"{article.title.lower().strip()}|{article.link.lower().strip()}"
    return hashlib.md5(unique_str.encode()).hexdigest()


def load_history() -> dict:
    """Load the history of sent articles."""
    if HISTORY_FILE.exists():
        try:
            with open(HISTORY_FILE, 'r') as f:
                return json.load(f)
        except (json.JSONDecodeError, IOError):
            return {"sent_articles": {}, "last_cleanup": None}
    return {"sent_articles": {}, "last_cleanup": None}


def save_history(history: dict) -> None:
    """Save the history of sent articles."""
    # Resolve and validate the write target stays within the project directory
    project_dir = Path(__file__).parent.resolve()
    target = HISTORY_FILE.resolve()
    if not str(target).startswith(str(project_dir) + os.sep) and target != project_dir:
        print(f"Warning: Refusing to write outside project dir: {target}")
        return
    try:
        with open(target, 'w') as f:
            json.dump(history, f, indent=2)
    except IOError as e:
        print(f"Warning: Could not save history file: {e}")
        return

    # Optionally sync history to a remote host (best-effort; skipped if unconfigured)
    sync_digest_to_remote()


def _alert_remote_sync_failure(detail: str) -> None:
    """Email when the remote sync fails so a stale downstream consumer is noticed."""
    try:
        send_error_email(
            "Remote sync of digest history failed",
            f"Syncing digest_history.json to the remote host failed: {detail}. "
            "Any downstream consumer that depends on this data will go stale "
            "until this is fixed.",
        )
    except Exception as e:
        print(f"⚠️ Could not send remote-sync alert email: {e}")


def sync_digest_to_remote() -> None:
    """Optionally copy digest_history.json to a remote host via scp.

    Non-blocking, best-effort — if it fails (or no host is configured) the
    digest still works. Useful when a downstream consumer on another machine
    reads the news/reddit signals from digest_history.json.

    Configure via env vars (all optional — sync is skipped if DIGEST_SYNC_HOST
    is unset):
      DIGEST_SYNC_HOST - remote hostname or IP
      DIGEST_SYNC_USER - remote SSH user (default: "user")
      DIGEST_SYNC_KEY  - path to the SSH private key (default: ~/.ssh/your-key.pem)
      DIGEST_SYNC_PATH - remote destination path for digest_history.json
    """
    import re
    import shutil
    import subprocess

    # No host configured -> nothing to sync to. Skip gracefully.
    sync_host = os.getenv("DIGEST_SYNC_HOST", "").strip()
    if not sync_host:
        return

    # Resolve scp to a full path — avoid relying on PATH
    scp_path = shutil.which("scp")
    if scp_path is None:
        print("⚠️ Remote sync skipped (scp not found)")
        return

    sync_user = os.getenv("DIGEST_SYNC_USER", "user")
    ssh_key = os.getenv(
        "DIGEST_SYNC_KEY",
        str(Path.home() / ".ssh" / "your-key.pem"),
    )
    remote_path = os.getenv(
        "DIGEST_SYNC_PATH",
        "/home/USER/news-digest/digest_history.json",
    )

    # Validate env-sourced inputs to prevent argument injection
    if not re.match(r'^[\w.:-]+$', sync_host):
        print(f"⚠️ Remote sync skipped (invalid host: {sync_host})")
        return
    if not re.match(r'^[\w-]+$', sync_user):
        print(f"⚠️ Remote sync skipped (invalid user: {sync_user})")
        return
    if not re.match(r'^[\w./-]+$', remote_path):
        print(f"⚠️ Remote sync skipped (invalid remote path: {remote_path})")
        return

    ssh_key_path = Path(ssh_key).resolve()
    if not ssh_key_path.exists():
        print("⏭️ Remote sync skipped (SSH key not found)")
        _alert_remote_sync_failure(f"SSH key not found at {ssh_key_path}")
        return

    try:
        result = subprocess.run(
            [
                scp_path, "-i", str(ssh_key_path),
                "-o", "StrictHostKeyChecking=accept-new",
                "-o", "ConnectTimeout=10",
                str(HISTORY_FILE),
                f"{sync_user}@{sync_host}:{remote_path}",
            ],
            capture_output=True,
            text=True,
            timeout=30,
        )
        if result.returncode == 0:
            print("☁️ Digest history synced to remote host")
        else:
            print(f"⚠️ Remote sync failed: {result.stderr.strip()}")
            _alert_remote_sync_failure(
                result.stderr.strip() or f"scp exited {result.returncode}")
    except subprocess.TimeoutExpired:
        print("⚠️ Remote sync timed out (30s)")
        _alert_remote_sync_failure("scp timed out after 30s")
    except Exception as e:
        print(f"⚠️ Remote sync error: {e}")
        _alert_remote_sync_failure(str(e))


def cleanup_old_history(history: dict, days: int = 7) -> dict:
    """Remove articles older than specified days from history."""
    cutoff = datetime.now(timezone.utc) - timedelta(days=days)
    cutoff_str = cutoff.isoformat()

    history["sent_articles"] = {
        k: v for k, v in history.get("sent_articles", {}).items()
        if v.get("sent_at", "") > cutoff_str
    }

    # Also prune stale Reddit thread details
    thread_details = history.get("reddit_thread_details", {})
    history["reddit_thread_details"] = {
        url: detail for url, detail in thread_details.items()
        if detail.get("fetched_at", "") > cutoff_str
    }

    history["last_cleanup"] = datetime.now(timezone.utc).isoformat()
    return history


def filter_duplicates(articles: list[Article], history: dict) -> list[Article]:
    """Remove articles that were already sent in previous digests."""
    new_articles = []
    sent_hashes = set(history.get("sent_articles", {}).keys())

    for article in articles:
        article_hash = get_article_hash(article)
        if article_hash not in sent_hashes:
            new_articles.append(article)
        else:
            print(f"  Skipping duplicate: {article.title[:50]}...")

    return new_articles


def mark_articles_as_sent(articles: list[Article], history: dict) -> dict:
    """Mark articles as sent in the history."""
    for article in articles:
        article_hash = get_article_hash(article)
        history["sent_articles"][article_hash] = {
            "title": article.title,
            "link": article.link,
            "source": article.source,
            "sent_at": datetime.now(timezone.utc).isoformat()
        }
    return history


# =============================================================================
# News Fetching
# =============================================================================

def fetch_rss_feed(name: str, url: str, max_articles: int = 5) -> list[Article]:
    """Fetch articles from an RSS feed."""
    articles = []
    try:
        # Fetch via requests so we control the User-Agent — Reddit (and a few
        # others) return 403 to feedparser's default UA, and feedparser swallows
        # that silently as 0 entries.
        resp = requests.get(
            url,
            headers={"User-Agent": "NewsDigest/1.0 (daily digest bot)"},
            timeout=15,
        )
        if resp.status_code != 200:
            print(f"  ⚠️ {name} HTTP {resp.status_code} — skipping")
            return articles
        feed = feedparser.parse(resp.content)
        if feed.bozo and not feed.entries:
            print(f"  ⚠️ {name} returned no entries (bozo={feed.bozo})")
            return articles
        cutoff = datetime.now(timezone.utc) - timedelta(days=1)

        for entry in feed.entries[:max_articles * 2]:  # Fetch extra to filter
            # Try to parse the published date
            published = None
            if hasattr(entry, 'published_parsed') and entry.published_parsed:
                published = datetime(*entry.published_parsed[:6], tzinfo=timezone.utc)
            elif hasattr(entry, 'updated_parsed') and entry.updated_parsed:
                published = datetime(*entry.updated_parsed[:6], tzinfo=timezone.utc)

            # Filter to last 24 hours if we have a date
            if published and published < cutoff:
                continue

            # Get summary/description
            summary = ""
            if hasattr(entry, 'summary'):
                summary = entry.summary[:500]  # Truncate long summaries
            elif hasattr(entry, 'description'):
                summary = entry.description[:500]

            articles.append(Article(
                title=entry.get('title', 'No title'),
                link=entry.get('link', ''),
                summary=summary,
                source=name,
                published=published
            ))

            if len(articles) >= max_articles:
                break

    except Exception as e:
        print(f"Error fetching {name}: {e}")

    return articles


def fetch_hacker_news_top(max_articles: int = 10) -> list[Article]:
    """Fetch top stories from Hacker News API for better quality."""
    articles = []
    try:
        # Get top story IDs
        response = requests.get(
            "https://hacker-news.firebaseio.com/v0/topstories.json",
            timeout=10
        )
        story_ids = response.json()[:max_articles]

        for story_id in story_ids:
            story_resp = requests.get(
                f"https://hacker-news.firebaseio.com/v0/item/{story_id}.json",
                timeout=10
            )
            story = story_resp.json()
            if story and story.get('title'):
                articles.append(Article(
                    title=story['title'],
                    link=story.get('url', f"https://news.ycombinator.com/item?id={story_id}"),
                    summary=f"Score: {story.get('score', 0)} | Comments: {story.get('descendants', 0)}",
                    source="Hacker News (Top)",
                    published=datetime.fromtimestamp(story.get('time', 0), tz=timezone.utc) if story.get('time') else None
                ))
    except Exception as e:
        print(f"Error fetching HN API: {e}")

    return articles


def fetch_all_news() -> list[Article]:
    """Fetch news from all configured sources."""
    all_articles = []
    max_per_source = int(os.getenv('MAX_ARTICLES_PER_SOURCE', 20))

    # Fetch from RSS feeds
    for name, url in RSS_FEEDS.items():
        if name == "Hacker News":
            continue  # We'll use the API instead
        print(f"Fetching {name}...")
        articles = fetch_rss_feed(name, url, max_per_source)
        all_articles.extend(articles)
        print(f"  Got {len(articles)} articles")

    # Fetch Hacker News via API for better data
    print("Fetching Hacker News (API)...")
    hn_articles = fetch_hacker_news_top(max_per_source * 2)
    all_articles.extend(hn_articles)
    print(f"  Got {len(hn_articles)} articles")

    return all_articles


def fetch_reddit_thread_details(
    articles: list[Article],
    history: dict,
    max_threads: int = 20,
) -> dict:
    """Fetch full thread details for Reddit articles and cache in history.

    A downstream consumer can read thread details (selftext, top comments)
    from digest_history.json — e.g. to generate contextual replies. Reddit
    blocks some hosts/IPs, so we fetch the details here and pass them along
    via digest_history.json.

    Args:
        articles: All fetched articles (will filter to Reddit only).
        history: The digest history dict to update.
        max_threads: Max number of threads to fetch details for.

    Returns:
        Updated history dict with reddit_thread_details populated.
    """
    reddit_articles = [a for a in articles if "reddit.com" in a.link]
    if not reddit_articles:
        # The Reddit RSS feeds returned nothing — with 6 subs this means the
        # feeds got blocked/broke (don't fail silently like the .json block did).
        print("  ⚠️ No Reddit articles in any feed — sending alert email")
        try:
            send_error_email(
                "Reddit RSS feeds returned no articles",
                "The Reddit RSS feeds produced 0 articles this run (normally ~90 "
                "across the 6 subs). Reddit likely blocked the .rss feeds too — "
                "any downstream Reddit consumer will go dry. Check the feeds.",
            )
        except Exception as e:
            print(f"  ⚠️ Could not send Reddit alert email: {e}")
        return history

    if "reddit_thread_details" not in history:
        history["reddit_thread_details"] = {}

    existing = history["reddit_thread_details"]
    to_fetch = [a for a in reddit_articles if a.link not in existing][:max_threads]

    if not to_fetch:
        print(f"  All {len(reddit_articles)} Reddit threads already cached")
        return history

    # Reddit blocks the unauthenticated .json endpoint (403), which silently
    # emptied reddit_thread_details. Build details from the RSS feed content the
    # digest already fetched (title + post body + link) instead — no .json, no
    # OAuth. RSS carries no comments, so top_comments is left empty.
    print(f"  Building details for {len(to_fetch)} Reddit threads from RSS...")
    fetched = 0
    for article in to_fetch:
        try:
            selftext = BeautifulSoup(
                article.summary or "", "html.parser",
            ).get_text(" ", strip=True)
            # Drop Reddit's RSS boilerplate ("submitted by /u/x [link] [comments]")
            selftext = re.sub(r"\s*submitted by\s*/u/\S+.*$", "", selftext).strip()
            m = re.search(r"reddit\.com/r/([^/]+)/", article.link)
            subreddit = m.group(1) if m else ""
            existing[article.link] = {
                "title": article.title,
                "selftext": selftext[:2000],
                "author": "",
                "score": 0,
                "num_comments": 0,
                "subreddit": subreddit,
                "created_utc": (
                    article.published.timestamp() if article.published else 0
                ),
                "url": article.link,
                "top_comments": [],
                "fetched_at": datetime.now(timezone.utc).isoformat(),
            }
            fetched += 1
        except Exception as e:
            print(f"    Error building {article.title[:50]}...: {e}")
            continue

    print(f"  ✓ Built {fetched} Reddit thread details from RSS ({len(existing)} total cached)")

    # Don't fail silently: we had threads to build (to_fetch non-empty) but built
    # none — that's what happened when Reddit killed the .json endpoint. Alert.
    if fetched == 0:
        print("  ⚠️ Built 0 Reddit thread details — sending alert email")
        try:
            send_error_email(
                "Reddit thread building produced 0 results",
                f"fetch_reddit_thread_details processed {len(to_fetch)} Reddit "
                "threads from RSS but built 0 of them. Any downstream Reddit "
                "consumer will go dry until this is fixed — Reddit likely changed "
                "access again, or the RSS feed/parsing broke. Check the output.",
            )
        except Exception as e:
            print(f"  ⚠️ Could not send Reddit alert email: {e}")

    history["reddit_thread_details"] = existing
    return history


# =============================================================================
# Claude Summarization
# =============================================================================

def cleanup_old_logs(retention_days: int) -> None:
    """Delete rotated log files older than retention_days."""
    if retention_days <= 0:
        return

    log_file = Path(os.getenv("LOG_FILE", str(DEFAULT_LOG_FILE)))
    if not log_file.parent.exists():
        return

    cutoff = datetime.now(timezone.utc) - timedelta(days=retention_days)
    base_name = log_file.name

    for path in log_file.parent.iterdir():
        if not path.is_file():
            continue
        if path.name == base_name:
            # Never delete the active log file.
            continue
        if not path.name.startswith(base_name + "."):
            continue
        try:
            mtime = datetime.fromtimestamp(path.stat().st_mtime, tz=timezone.utc)
        except OSError:
            continue
        if mtime < cutoff:
            try:
                path.unlink()
            except OSError:
                pass


def _parse_datetime(value: str) -> Optional[datetime]:
    if not value:
        return None
    try:
        dt = datetime.fromisoformat(value.replace("Z", "+00:00"))
        # Ensure timezone-aware (old cache entries may be naive UTC)
        if dt.tzinfo is None:
            dt = dt.replace(tzinfo=timezone.utc)
        return dt
    except ValueError:
        return None


def _select_latest_model(models: list, family: str) -> Optional[str]:
    family_lower = family.lower()
    candidates = []
    for model in models:
        # client.models.list() yields pydantic ModelInfo objects, not dicts.
        # (The old ternary bound `if isinstance(...)` to the whole `a or b`
        # expression, so every SDK object resolved to None and this function
        # silently returned None for all families.)
        if isinstance(model, dict):
            model_id = model.get("id")
            created_at = model.get("created_at")
        else:
            model_id = getattr(model, "id", None)
            created_at = getattr(model, "created_at", None)
        if not model_id:
            continue
        if family_lower not in model_id.lower():
            continue
        created_dt = _parse_datetime(created_at) if isinstance(created_at, str) else None
        candidates.append((created_dt, model_id))

    if not candidates:
        return None

    # Prefer newest created_at; fall back to lexical ID ordering only when no
    # candidate carries a date (mixing None into the sort key raised TypeError).
    dated = [c for c in candidates if c[0] is not None]
    if dated:
        dated.sort(key=lambda item: (item[0], item[1]))
        return dated[-1][1]
    candidates.sort(key=lambda item: item[1])
    return candidates[-1][1]


def resolve_model_order(client: anthropic.Anthropic) -> list[str]:
    """Resolve the model fallback order (sonnet -> opus -> haiku)."""

    use_latest = os.getenv("USE_LATEST_MODELS", "false").lower() == "true"
    refresh_days = int(os.getenv("MODEL_REFRESH_DAYS", "7"))
    default_models = {
        "sonnet": "claude-sonnet-4-6",
        "opus": "claude-opus-4-8",
        "haiku": "claude-haiku-4-5",
    }

    cache = {}
    if MODEL_CACHE_FILE.exists():
        try:
            cache = json.loads(MODEL_CACHE_FILE.read_text(encoding="utf-8"))
        except json.JSONDecodeError:
            cache = {}

    cached_models = cache.get("models", {}) if isinstance(cache, dict) else {}
    last_checked = _parse_datetime(cache.get("last_checked", "")) if isinstance(cache, dict) else None

    models_to_use = default_models.copy()

    if use_latest:
        cache_fresh = last_checked and (datetime.now(timezone.utc) - last_checked) <= timedelta(days=refresh_days)
        if cache_fresh and cached_models:
            models_to_use.update({k: v for k, v in cached_models.items() if v})
        else:
            try:
                response = client.models.list()
                model_list = getattr(response, "data", response)
                resolved = {
                    "sonnet": _select_latest_model(model_list, "sonnet"),
                    "opus": _select_latest_model(model_list, "opus"),
                    "haiku": _select_latest_model(model_list, "haiku"),
                }
                for key, value in resolved.items():
                    if value:
                        models_to_use[key] = value

                MODEL_CACHE_FILE.write_text(
                    json.dumps(
                        {"last_checked": datetime.now(timezone.utc).isoformat(), "models": models_to_use},
                        indent=2,
                    ),
                    encoding="utf-8",
                )
            except Exception as e:
                print(f"⚠️ Failed to refresh Claude model list, using cached/default models: {e}")
                if cached_models:
                    models_to_use.update({k: v for k, v in cached_models.items() if v})

    # Allow a manual override for the primary model, but keep fallbacks.
    primary_override = os.getenv("DIGEST_MODEL", "").strip()
    order = [models_to_use["sonnet"], models_to_use["opus"], models_to_use["haiku"]]
    if primary_override:
        order = [primary_override] + [m for m in order if m != primary_override]

    # De-duplicate while preserving order.
    deduped = []
    for model_id in order:
        if model_id and model_id not in deduped:
            deduped.append(model_id)

    return deduped


_RETRYABLE_CLAUDE_TYPES = {"overloaded_error", "rate_limit_error", "api_error"}


def _claude_error_type(e) -> str:
    """Extract Anthropic's error 'type' from an APIStatusError body.

    Overloaded/rate-limit errors can arrive *mid-stream* as an SSE 'error' event.
    When that happens the SDK builds the APIStatusError from the original 200-OK
    streaming response (response=self.response), so e.status_code is 200 — NOT
    429/529 — and the only reliable signal is the 'type' carried in the body
    (e.g. 'overloaded_error'). Status-code-only checks miss this entirely.
    """
    body = getattr(e, "body", None)
    if isinstance(body, dict):
        err = body.get("error")
        if isinstance(err, dict) and err.get("type"):
            return str(err["type"]).lower()
        if body.get("type") and body["type"] != "error":
            return str(body["type"]).lower()
    return ""


def _is_transient_claude_error(e) -> bool:
    """True for overloaded / rate-limit / transient server errors that are worth
    retrying and/or falling through to the next model — whether they surface at
    request time (status 429/5xx) or mid-stream (status 200 + error type)."""
    if getattr(e, "status_code", None) in (429, 500, 502, 503, 529):
        return True
    if _claude_error_type(e) in _RETRYABLE_CLAUDE_TYPES:
        return True
    blob = (_claude_error_type(e) + " " + str(e)).lower()
    return "overloaded" in blob or "rate_limit" in blob


def _extract_text(message) -> str:
    """Return the digest text from a Claude message.

    With extended-thinking models (e.g. Opus 4.8) the first content block is a
    ThinkingBlock, which has no `.text`. Blindly reading `content[0].text` then
    crashes with "'ThinkingBlock' object has no attribute 'text'". Instead we
    concatenate every block that actually carries text (type == "text" or a
    real `.text` attribute) and skip thinking/redacted_thinking/tool blocks.
    """
    parts = []
    for block in message.content:
        if getattr(block, "type", None) == "text":
            parts.append(block.text)
        elif getattr(block, "type", None) in ("thinking", "redacted_thinking"):
            continue
        else:
            text = getattr(block, "text", None)
            if isinstance(text, str):
                parts.append(text)
    result = "".join(parts).strip()
    if not result:
        types = ", ".join(getattr(b, "type", "?") for b in message.content) or "none"
        raise ValueError(
            f"Claude returned no text content (blocks: {types}); "
            "cannot build digest."
        )
    return result


def summarize_with_claude(articles: list[Article]) -> str:
    """Use Claude to create a personalized digest summary."""

    client = anthropic.Anthropic()
    model_order = resolve_model_order(client)
    if model_order:
        print(f"Claude model order: {', '.join(model_order)}")

    # Format articles for Claude
    articles_text = ""
    for i, article in enumerate(articles, 1):
        articles_text += f"""
---
Article {i}:
Source: {article.source}
Title: {article.title}
Link: {article.link}
Summary: {article.summary}
"""

    prompt = f"""You are creating a personalized daily news digest for Alex, a product manager interested in
tech. Alex wants to stay informed on industry trends, track a few companies and competitors of personal
interest, AND — Alex is always open to an exceptional next role — spot standout job opportunities and
companies worth watching.

## READER PROFILE
# This is an EXAMPLE reader profile. Edit it to describe yourself so the digest is tailored to you.
- Product manager with a technical background, interested in building with AI tools
- Tracks a few example companies of personal interest (see "must-watch topics" in INTERESTS below)
- Always open to the right next role — most drawn to AI/ML, agentic AI, AI video / generative media,
  and robotics. Surface notable roles AND companies in these spaces.
- Career background spans AI & autonomous agents, AI video / generative media, decentralized social & web3,
  fintech, and consumer hardware / gaming. Use this history to decide which companies are worth watching.
- Particularly interested in AI/ML, AI agents, robotics, and emerging tech
- Has automotive interests (customize to your vehicles) — relevant for automotive content
- Interested in health optimization and longevity

## INTERESTS (in priority order):

{INTERESTS}

## TODAY'S ARTICLES (pre-filtered to last 24 hours, duplicates from previous days removed):

{articles_text}

---

## INSTRUCTIONS

Create a well-organized, engaging daily digest email with these sections IN THIS ORDER:

---

### 🔥 TOP PRIORITY (Always include - most important section)
The 4-6 most significant stories from these priority areas ONLY:
- AI/ML breakthroughs, tools, and business news
- Job market signals and opportunities
- Robotics + AI convergence
- Social + AI + Web3 convergence (especially intersections of all three)
- Anthropic news (always include if present)

**ADDITIONALLY** (do NOT replace any of the above — add as extra items):
# Replace these examples with the companies/topics YOU want always-surfaced.
- **Your OWN companies/topics** — scan ALL articles from EVERY source and ALWAYS surface as Top Priority:
  - Acme AI (EXAMPLE) — "Acme AI" / "example.com", OR notable news in its space. Treat as the #1
    priority; lead with it when present.
  - Example Co (EXAMPLE) — "Example Co". A second venture or topic worth tracking, so category and
    fundraising-climate news matters too.
- **Competitor watch** — ALWAYS surface, explicitly framed as competitive intel:
  "Example Competitor", their product launches, developer program / open SDK / community ecosystem,
  or any funding.
- Any other companies/topics you want flagged. Scan ALL articles from EVERY source — if ANY article
  mentions them, ALWAYS include as additional Top Priority items. Funding rounds, product launches,
  and partnerships are CRITICAL — flag prominently.

For each article:
- 2-3 sentence summary
- Why it matters to a product executive
- Include the link

---

### 🚀 MY VENTURES & COMPETITIVE RADAR (Include ONLY when there is real, relevant content — omit the entire section on days with nothing)
# Replace these example sub-headers with the companies/topics YOU are tracking.
A focused tracker for the companies you're building or watching and the rivals you care about. Organize under
these sub-headers, and DROP any sub-header that has no items today (never pad with "no news today" filler):

- **Acme AI** (EXAMPLE — your top focus): Direct mentions, OR meaningful news in its space. For space
  (not-direct) news, add a one-line "Why it matters" angle (opportunity, threat, validation, or a feature idea).
- **Example Co** (EXAMPLE — a second venture/topic): Direct mentions, OR adjacent signals, and the fundraising
  climate for the relevant category.
- **Competitor — Example Competitor**: Any product launches, their developer program / open SDK / community
  ecosystem, funding, partnerships, press, or notable reception. Frame as competitive intel — what it implies
  for you and how you might respond.

For each item: 1-2 sentence summary, the strategic angle (1 line), and the link. Keep it tight and skimmable.

---

### 💼 JOB RADAR (Always include if ANY relevant signals exist — the reader is always open to the right role)
The reader is always watching for an exceptional next role. Tune to their background: AI/ML & agentic AI,
AI video / generative media, robotics, decentralized social & web3, fintech, and consumer hardware/gaming.
Actively scan for and flag:
- **Anthropic news**: ANY news about Anthropic — roles, research, launches (high interest)
- **Roles & teams worth knowing**: product/leadership openings or notable team build-outs at companies in the
  domains above (e.g. frontier AI labs, agent startups, AI-video/generative-media, robotics, web3-social)
- **Funding rounds**: Company, amount, stage — fresh capital signals hiring (weight AI / AI-video / robotics)
- **Companies to keep an eye on**: based on the reader's background & interests — who's emerging, pivoting, or
  scaling in these lanes, even if not hiring yet
- **Executive moves**: leadership changes that could open opportunities or signal a shift worth tracking

Format as a quick-scan list with company name bolded and 1-line context (note the fit angle when useful).

---

### 🏢 COMPANIES TO WATCH (Include if 2+ interesting companies mentioned)
Spotlight on startups or companies doing interesting things:
- Company name and what they do
- Why they're notable (funding, tech, growth)
- Link to the article
This helps track potential employers or industry movers.

---

### 🤖 AI & ROBOTICS (High priority section)
- New AI tools, frameworks, models
- Business adoption and trends
- Robotics breakthroughs
- Developer resources and coding tips

---

### 🧬 HEALTH & LONGEVITY (If relevant articles exist)
- GLP-1 research (Ozempic, Mounjaro, etc.)
- Longevity science
- Health optimization tech
- Supplement science with actual evidence

---

### 🚗 AUTOMOTIVE TECH (If relevant articles exist)
- Chinese EV innovations (BYD, NIO, Xpeng)
- Self-driving/ADAS developments
- Performance car platform news (customize to your vehicles)
- Battery and range breakthroughs

---

### 🌐 SOCIAL & WEB3 (Include if relevant articles exist - HIGH INTEREST)
- Social platform developments, policy changes, new features
- Decentralized social platforms (Bluesky, Farcaster, Lens, Mastodon)
- AI + social intersection (AI-powered features, content detection, recommendation systems)
- Web3 + social intersection (decentralized identity, on-chain social graphs)
- Web3 regulatory clarity and real use cases
- NO crypto price speculation

---

### 🌍 CLIMATE TECH (If relevant articles exist)
- Climate tech solutions
- Marine conservation technology

---

### 📊 FINANCE & FINTECH RADAR (Include if relevant - quick hits format)
3-6 one-liner quick hits from the global finance, fintech, crypto industry, and stock landscape:
- Major M&A, funding rounds, IPOs in fintech/crypto
- Market-moving macro signals (rate decisions, earnings surprises, sector rotations)
- Crypto industry milestones (exchange news, DeFi, institutional adoption)
- Fintech product launches and neobank developments
- Stock market catalysts worth noting
- World economics: trade policy shifts, GDP/inflation signals, central bank moves, emerging market developments
Format as punchy one-liners with source links. NO price predictions or day-trading tips.

---

### ⚖️ REGULATORY & LEGAL RADAR (Include if relevant - quick hits format)
2-4 one-liner quick hits on the legal and regulatory landscape across tech sectors:
- AI governance and regulation (executive orders, EU AI Act, liability frameworks)
- Crypto/fintech enforcement and rulemaking (SEC, CFTC, stablecoin legislation)
- Tech antitrust actions and major M&A approvals/blocks
- Automotive/EV/robotics policy (safety mandates, tariffs, labor impact)
- Global regulatory divergence (US vs EU vs Asia approaches)
Format as punchy one-liners with source links. NO partisan framing.

---

### 🌏 GLOBAL NEWS QUICK HITS (Include if relevant - quick hits format)
3-5 one-liner quick hits on major international developments:
- Geopolitical shifts, conflicts, diplomatic milestones
- International economic developments (trade deals, sanctions, emerging market moves)
- Global health, humanitarian, or environmental events
- Major elections, regime changes, or protests worldwide
Focus on events with broad significance. NO partisan framing. NO US-domestic stories (those go in USA News).
Format as punchy one-liners with source links.

---

### 🇺🇸 USA NEWS QUICK HITS (Include if relevant - quick hits format)
3-5 one-liner quick hits on significant US national developments:
- Federal policy, executive actions, congressional legislation
- Supreme Court decisions and major legal rulings
- National economic signals (jobs reports, infrastructure, housing)
- Major domestic events with national impact
Focus on substantive developments, not political horse-race coverage. NO partisan framing or opinion.
Format as punchy one-liners with source links.

---

### 📍 LOCAL NEWS (Include if relevant - quick hits format)
2-4 one-liner quick hits on news from your local area:
- City/county policy, infrastructure, transit, housing developments
- Local tech and startup ecosystem news
- Cultural events, openings, or closures of note
- Weather, safety, or environmental events affecting your area
Focus on news that matters to someone living and working in your area. NO celebrity gossip.
Format as punchy one-liners with source links.

---

### 📺 WORTH WATCHING (If relevant - entertainment/space/science)
- Award-winning films/TV
- Must-see sci-fi
- Space exploration milestones
- Major scientific discoveries

---

### ⚡ QUICK HITS (Optional, 3-5 items max)
One-liner mentions of interesting but non-essential articles

## STRICT FILTERING RULES - MUST FOLLOW

ALWAYS EXCLUDE:
- Celebrity gossip, relationship drama, tabloid content
- Crypto price predictions, "to the moon" hype, memecoin news
- Partisan political opinion pieces
- Clickbait and sensationalized headlines
- Promotional content disguised as news
- Minor incremental updates that aren't newsworthy

QUALITY CONTROL:
- When multiple sources cover the same story, pick the BEST one
- Skip sections entirely if fewer than 2 quality articles
- Total digest: 20-30 articles maximum
- Prioritize actionable intelligence over general news

## OUTPUT FORMAT - CRITICAL

You MUST return ONLY valid HTML (no markdown). Use this exact structure:

```html
<h1>🗞️ Daily News Digest</h1>
<p>Good morning! Here's your personalized news for [DATE].</p>

<h2>🔥 Top Priority</h2>
<ul>
  <li>
    <strong><a href="URL">Article Title</a></strong> (Source)<br>
    Summary of the article and why it matters.
  </li>
</ul>

<h2>💼 Job Radar</h2>
<ul>
  <li><strong>Company Name</strong> - What happened and why it's relevant</li>
</ul>

<!-- Continue with other sections using same pattern -->
```

HTML RULES:
- Use <h2> for section headers (with emoji)
- Use <ul> and <li> for article lists
- Use <strong> for emphasis
- Use <a href="URL">Title</a> for links
- Use <br> for line breaks within list items
- Use <p> for paragraphs
- Use <hr> to separate major sections if needed
- Do NOT use markdown syntax (no ##, no **, no - bullets)
- Do NOT wrap in ```html code blocks - return raw HTML only
"""

    max_retries = 3
    base_wait = 3
    max_wait = 20
    last_error = None

    for model in model_order:
        for attempt in range(max_retries):
            try:
                # Stream the response: 4096 used to truncate the richer digest
                # mid-section (Gmail showed a hard cutoff partway through).
                # Streaming lets us raise max_tokens well past the ~16K
                # non-streaming HTTP-timeout guard — the current always-latest
                # models support 64K output — without risking a dropped
                # connection on a long generation.
                with client.messages.stream(
                    model=model,
                    max_tokens=64000,
                    messages=[
                        {"role": "user", "content": prompt}
                    ]
                ) as stream:
                    message = stream.get_final_message()
                last_error = None
                if message.stop_reason == "max_tokens":
                    # Don't fail silently: the digest will be truncated mid-section.
                    print(
                        f"⚠ Claude hit max_tokens with model {model} — digest is "
                        "truncated. Consider raising max_tokens or trimming the prompt."
                    )
                print(f"✓ Claude response generated with model: {model}")
                break
            except (anthropic.APIStatusError,) as e:
                # A retired/invalid model returns 404 (or a 400 "model not_found").
                # Retrying the SAME model is pointless — record it and fall through
                # to the next fallback model. Without this, a retired primary (e.g.
                # Sonnet 4.0 hitting its retirement date) aborts the entire digest
                # even though Opus/Haiku are healthy, defeating the fallback order.
                msg = str(e).lower()
                model_missing = e.status_code == 404 or (
                    e.status_code == 400 and "model" in msg and "not_found" in msg
                )
                if model_missing:
                    last_error = e
                    print(
                        f"Claude API returned {e.status_code} for {model} "
                        "(model unavailable/retired), trying next fallback model..."
                    )
                    break
                if _is_transient_claude_error(e):
                    last_error = e
                    reason = _claude_error_type(e) or f"HTTP {e.status_code}"
                    if attempt < max_retries - 1:
                        wait = min(max_wait, base_wait * (2 ** attempt))
                        jitter = random.uniform(0.7, 1.3)
                        wait *= jitter
                        print(
                            f"Claude API transient error ({reason}) for {model}, "
                            f"retrying in {wait:.1f}s (attempt {attempt + 1}/{max_retries})..."
                        )
                        time.sleep(wait)
                    else:
                        print(
                            f"Claude API transient error ({reason}) for {model} after "
                            f"{max_retries} attempts, trying next fallback model..."
                        )
                        break
                else:
                    raise

        if last_error is None:
            break

    if last_error is not None:
        raise last_error

    html_content = _extract_text(message)

    # Clean up any markdown that slipped through
    html_content = clean_markdown_to_html(html_content)

    return html_content


def clean_markdown_to_html(content: str) -> str:
    """Convert any remaining markdown syntax to HTML and clean up formatting."""
    import re

    # Remove code block wrappers if present
    content = re.sub(r'^```html\s*\n?', '', content, flags=re.MULTILINE)
    content = re.sub(r'\n?```\s*$', '', content, flags=re.MULTILINE)
    content = content.strip()

    # Check if content already looks like proper HTML (starts with HTML tag)
    if content.startswith('<h1>') or content.startswith('<div') or content.startswith('<!'):
        # Already HTML, just do minimal cleanup
        # Convert any remaining markdown bold
        content = re.sub(r'\*\*(.+?)\*\*', r'<strong>\1</strong>', content)
        # Convert any remaining markdown links
        content = re.sub(r'\[([^\]]+)\]\(([^)]+)\)', r'<a href="\2">\1</a>', content)
        return content

    # Content appears to be markdown or mixed - do full conversion

    # Convert markdown headers to HTML
    content = re.sub(r'^### (.+)$', r'<h3>\1</h3>', content, flags=re.MULTILINE)
    content = re.sub(r'^## (.+)$', r'<h2>\1</h2>', content, flags=re.MULTILINE)
    content = re.sub(r'^# (.+)$', r'<h1>\1</h1>', content, flags=re.MULTILINE)

    # Convert markdown bold **text** to <strong>text</strong>
    content = re.sub(r'\*\*(.+?)\*\*', r'<strong>\1</strong>', content)

    # Convert markdown italic *text* to <em>text</em> (but not inside URLs)
    content = re.sub(r'(?<![:/])\*([^*]+)\*(?![/])', r'<em>\1</em>', content)

    # Convert markdown links [text](url) to <a href="url">text</a>
    content = re.sub(r'\[([^\]]+)\]\(([^)]+)\)', r'<a href="\2">\1</a>', content)

    # Convert markdown horizontal rules
    content = re.sub(r'^---+$', '<hr>', content, flags=re.MULTILINE)

    # Convert markdown bullet points to HTML list items
    lines = content.split('\n')
    result = []
    in_list = False

    for line in lines:
        stripped = line.strip()

        # Check if this is a markdown bullet point (starts with - or *)
        is_bullet = (stripped.startswith('- ') or stripped.startswith('* ')) and not stripped.startswith('---')

        if is_bullet:
            if not in_list:
                result.append('<ul>')
                in_list = True
            # Remove the bullet marker and wrap in li
            item_content = stripped[2:]
            result.append(f'<li>{item_content}</li>')
        else:
            if in_list and stripped and not stripped.startswith('<li'):
                result.append('</ul>')
                in_list = False

            # Handle the line
            if stripped:
                # Don't wrap lines that are already HTML tags
                if stripped.startswith('<') or stripped.endswith('>'):
                    result.append(line)
                # Don't wrap lines that are continuations of list items
                elif in_list:
                    result.append(line)
                # Wrap plain text in paragraphs
                else:
                    result.append(f'<p>{stripped}</p>')
            else:
                result.append(line)

    if in_list:
        result.append('</ul>')

    return '\n'.join(result)


def extract_top_topics(html_content: str) -> list[str]:
    """Extract top topic titles from the digest HTML for the podcast email section.

    Pulls article titles from the Top Priority section, falling back to any
    ``<strong><a>`` links found in the first ``<ul>`` block.

    Returns:
        List of up to 5 topic title strings.
    """
    soup = BeautifulSoup(html_content, "html.parser")
    topics = []

    # Look for Top Priority section (first h2 typically)
    for h2 in soup.find_all("h2"):
        if "top" in h2.get_text().lower() or "priority" in h2.get_text().lower():
            # Get the <ul> that follows this h2
            ul = h2.find_next_sibling("ul")
            if ul:
                for li in ul.find_all("li"):
                    link = li.find("a")
                    if link:
                        topics.append(link.get_text(strip=True))
                    elif li.find("strong"):
                        topics.append(li.find("strong").get_text(strip=True))
            break

    # Fallback: grab first few linked titles from any section
    if not topics:
        for a_tag in soup.find_all("a", href=True):
            text = a_tag.get_text(strip=True)
            if text and len(text) > 10:
                topics.append(text)
            if len(topics) >= 5:
                break

    return topics[:5]


# =============================================================================
# Email Sending
# =============================================================================

def send_error_email(error_type: str, error_message: str, full_traceback: str = "") -> bool:
    """Send an error notification email."""

    sender_email = os.getenv('GMAIL_ADDRESS')
    app_password = os.getenv('GMAIL_APP_PASSWORD')
    recipient_str = os.getenv('RECIPIENT_EMAIL')

    if not all([sender_email, app_password, recipient_str]):
        print("Error: Missing email configuration - cannot send error notification")
        return False

    recipients = [r.strip() for r in recipient_str.split(',') if r.strip()]

    msg = MIMEMultipart('alternative')
    msg['Subject'] = f"⚠️ News Digest Failed - {error_type} - {datetime.now().strftime('%A, %B %d, %Y')}"
    msg['From'] = f"News Digest <{sender_email}>"
    msg['To'] = ', '.join(recipients)

    html_content = f"""
<!DOCTYPE html>
<html>
<head>
    <style>
        body {{ font-family: -apple-system, BlinkMacSystemFont, 'Segoe UI', Roboto, sans-serif;
               line-height: 1.6; color: #333; max-width: 700px; margin: 0 auto; padding: 20px; }}
        .error-box {{ background: #fee; border: 1px solid #c00; border-radius: 5px; padding: 15px; margin: 20px 0; }}
        .error-title {{ color: #c00; margin: 0 0 10px 0; }}
        pre {{ background: #f5f5f5; padding: 15px; border-radius: 5px; overflow-x: auto; font-size: 12px; }}
        .action {{ background: #fffbe6; border: 1px solid #ffe58f; border-radius: 5px; padding: 15px; margin: 20px 0; }}
    </style>
</head>
<body>
    <h1>⚠️ News Digest Error</h1>
    <p>Your daily news digest failed to generate on {datetime.now().strftime('%Y-%m-%d at %H:%M')}.</p>

    <div class="error-box">
        <h3 class="error-title">{error_type}</h3>
        <p>{error_message}</p>
    </div>

    {"<div class='action'><h3>Suggested Action</h3><p>Check your Anthropic API credits at <a href='https://console.anthropic.com/'>console.anthropic.com</a></p></div>" if "credit" in error_message.lower() or "rate" in error_message.lower() or "billing" in error_message.lower() else ""}

    {f"<h3>Full Error Details</h3><pre>{full_traceback}</pre>" if full_traceback else ""}

    <hr style="margin-top: 40px; border: none; border-top: 1px solid #ddd;">
    <p style="color: #666; font-size: 0.85em;">
        This is an automated error notification from your News Digest bot.
    </p>
</body>
</html>
"""

    plain_text = f"News Digest Error: {error_type}\n\n{error_message}\n\n{full_traceback}"

    msg.attach(MIMEText(plain_text, 'plain'))
    msg.attach(MIMEText(html_content, 'html'))

    try:
        context = ssl.create_default_context()
        with smtplib.SMTP_SSL('smtp.gmail.com', 465, context=context) as server:
            server.login(sender_email, app_password)
            server.sendmail(sender_email, recipients, msg.as_string())
        print(f"✓ Error notification sent to {', '.join(recipients)}")
        return True
    except Exception as e:
        print(f"✗ Failed to send error notification: {e}")
        return False


def send_email(html_content: str, podcast_url: str | None = None, top_topics: list[str] | None = None) -> bool:
    """Send the digest email via Gmail SMTP."""

    sender_email = os.getenv('GMAIL_ADDRESS')
    app_password = os.getenv('GMAIL_APP_PASSWORD')
    recipient_str = os.getenv('RECIPIENT_EMAIL')

    if not all([sender_email, app_password, recipient_str]):
        print("Error: Missing email configuration in .env file")
        return False

    recipients = [r.strip() for r in recipient_str.split(',') if r.strip()]

    # Create message
    msg = MIMEMultipart('alternative')
    msg['Subject'] = f"📰 Daily News Digest — {datetime.now().strftime('%A, %B %d, %Y')}"
    msg['From'] = f"News Digest <{sender_email}>"
    msg['To'] = ', '.join(recipients)

    # Create plain text version (fallback)
    plain_text = "Your daily news digest is ready. Please view this email in an HTML-capable client."

    # Build podcast section HTML (before template wrapping)
    podcast_section = ""
    if podcast_url:
        topics_html = ""
        if top_topics:
            topic_items = "".join(f"<li>{topic}</li>" for topic in top_topics)
            topics_html = (
                '<p style="margin-top: 12px; font-weight: 600; color: #4a5568;">'
                "Today's top topics:</p><ul>" + topic_items + "</ul>"
            )
        podcast_section = (
            '<div style="margin-top: 32px; padding: 20px; background: #f0f7ff; '
            'border-radius: 10px; border: 1px solid #bee3f8;">'
            '<h2 style="color: #2b6cb0; margin-top: 0;">🎧 Daily News Podcast</h2>'
            "<p style=\"margin: 8px 0;\">Listen to today's digest as a podcast with hosts Alex &amp; Sam:</p>"
            f'<p><a href="{podcast_url}" style="display: inline-block; padding: 10px 20px; '
            "background: #3182ce; color: #ffffff; border-radius: 6px; text-decoration: none; "
            'font-weight: 600;">Listen Now</a></p>'
            '<p style="font-size: 13px; color: #718096;">Available anywhere — log in with your Audiobookshelf account.</p>'
            + topics_html
            + "</div>"
        )

    # Wrap HTML content in a styled email template
    if not html_content.strip().startswith('<!DOCTYPE') and not html_content.strip().startswith('<html'):
        html_content = f"""
<!DOCTYPE html>
<html>
<head>
    <meta charset="utf-8">
    <meta name="viewport" content="width=device-width, initial-scale=1.0">
    <style>
        body {{
            font-family: -apple-system, BlinkMacSystemFont, 'Segoe UI', Roboto, 'Helvetica Neue', Arial, sans-serif;
            line-height: 1.7;
            color: #2d3748;
            max-width: 680px;
            margin: 0 auto;
            padding: 20px;
            background-color: #ffffff;
        }}
        h1 {{
            color: #1a202c;
            font-size: 28px;
            font-weight: 700;
            margin-bottom: 8px;
            border-bottom: 3px solid #3182ce;
            padding-bottom: 12px;
        }}
        h2 {{
            color: #2b6cb0;
            font-size: 20px;
            font-weight: 600;
            margin-top: 32px;
            margin-bottom: 16px;
            padding-bottom: 8px;
            border-bottom: 1px solid #e2e8f0;
        }}
        h3 {{
            color: #4a5568;
            font-size: 16px;
            font-weight: 600;
            margin-top: 20px;
            margin-bottom: 12px;
        }}
        p {{
            margin: 12px 0;
            color: #4a5568;
        }}
        a {{
            color: #3182ce;
            text-decoration: none;
            font-weight: 500;
        }}
        a:hover {{
            text-decoration: underline;
            color: #2c5282;
        }}
        ul {{
            padding-left: 0;
            list-style: none;
            margin: 16px 0;
        }}
        li {{
            margin: 16px 0;
            padding: 14px 16px;
            background: #f7fafc;
            border-radius: 8px;
            border-left: 4px solid #3182ce;
        }}
        li strong {{
            color: #1a202c;
        }}
        li a {{
            font-size: 15px;
        }}
        hr {{
            border: none;
            border-top: 1px solid #e2e8f0;
            margin: 28px 0;
        }}
        .intro {{
            font-size: 16px;
            color: #718096;
            margin-bottom: 24px;
        }}
        .footer {{
            margin-top: 40px;
            padding-top: 20px;
            border-top: 1px solid #e2e8f0;
            color: #a0aec0;
            font-size: 13px;
        }}
        .source {{
            color: #718096;
            font-size: 13px;
            font-weight: normal;
        }}
        /* Special styling for job radar section */
        h2:has(+ ul li strong) {{
            color: #2f855a;
        }}
    </style>
</head>
<body>
{html_content}
{podcast_section}
<div class="footer">
    <p>Generated on {datetime.now().strftime('%A, %B %d, %Y at %H:%M')} by your News Digest bot.</p>
    <p>Powered by Claude AI • Filtering {len(RSS_FEEDS)} sources for the news that matters to you.</p>
</div>
</body>
</html>
"""

    msg.attach(MIMEText(plain_text, 'plain'))
    msg.attach(MIMEText(html_content, 'html'))

    # Send via Gmail SMTP
    try:
        context = ssl.create_default_context()
        with smtplib.SMTP_SSL('smtp.gmail.com', 465, context=context) as server:
            server.login(sender_email, app_password)
            server.sendmail(sender_email, recipients, msg.as_string())
        print(f"✓ Email sent successfully to {', '.join(recipients)}")
        return True
    except Exception as e:
        print(f"✗ Failed to send email: {e}")
        return False


# =============================================================================
# Main
# =============================================================================

def main():
    """Main function to generate and send the daily digest."""
    print(f"\n{'='*60}")
    print(f"Daily News Digest - {datetime.now().strftime('%Y-%m-%d %H:%M')}")
    print(f"{'='*60}\n")

    try:
        cleanup_old_logs(int(os.getenv("LOG_RETENTION_DAYS", "30")))

        # Load history for duplicate detection
        print("📂 Loading article history...")
        history = load_history()

        # Cleanup old history entries (older than 7 days)
        history = cleanup_old_history(history, days=7)
        print(f"  History contains {len(history.get('sent_articles', {}))} recent articles\n")

        # Step 1: Fetch news
        print("📥 Fetching news from sources...")
        articles = fetch_all_news()
        print(f"\n✓ Fetched {len(articles)} total articles")

        # Step 1b: Fetch Reddit thread details for any downstream consumer
        print("\n🔗 Fetching Reddit thread details...")
        history = fetch_reddit_thread_details(articles, history)

        # Step 2: Filter out duplicates from previous digests
        print("\n🔍 Filtering duplicates...")
        articles = filter_duplicates(articles, history)
        print(f"✓ {len(articles)} new articles after filtering\n")

        if not articles:
            send_error_email(
                "No New Articles Found",
                "The digest could not find any NEW articles from the configured sources. "
                "All fetched articles were already sent in previous digests. "
                "This might be normal on slow news days, or there could be a feed issue."
            )
            print("No new articles found. Error notification sent.")
            sys.exit(1)

        # Step 2: Summarize with Claude
        print("🤖 Generating digest with Claude...")
        try:
            digest_html = summarize_with_claude(articles)
        except anthropic.RateLimitError as e:
            send_error_email(
                "API Rate Limit Exceeded",
                "The Claude API rate limit has been exceeded. This usually means you've "
                "hit your usage cap or need to wait before making more requests.",
                str(e)
            )
            print(f"Rate limit error: {e}")
            sys.exit(1)
        except anthropic.AuthenticationError as e:
            send_error_email(
                "API Authentication Failed",
                "The Anthropic API key is invalid or has been revoked. Please check your "
                "ANTHROPIC_API_KEY in the .env file.",
                str(e)
            )
            print(f"Authentication error: {e}")
            sys.exit(1)
        except anthropic.BadRequestError as e:
            error_msg = str(e)
            if "credit" in error_msg.lower() or "billing" in error_msg.lower():
                send_error_email(
                    "API Credits Exhausted",
                    "Your Anthropic API credits have run out. Please add more credits at "
                    "console.anthropic.com to continue receiving digests.",
                    error_msg
                )
            else:
                send_error_email("API Request Error", error_msg, traceback.format_exc())
            print(f"API error: {e}")
            sys.exit(1)
        except anthropic.APIError as e:
            error_message = str(e)
            error_type = "Claude API Error"
            # A mid-stream overload arrives with status_code 200 (the stream opened
            # fine), so classify on the error type in the body too — not just status.
            etype = _claude_error_type(e) if isinstance(e, anthropic.APIStatusError) else ""
            status = getattr(e, "status_code", None)
            if "overloaded" in etype or status == 529:
                error_type = "Claude API Overloaded"
                error_message = (
                    "Claude is overloaded. The provider could not handle the request "
                    "after retries and fallbacks across all models."
                )
            elif "rate_limit" in etype or status == 429:
                error_type = "Claude API Rate Limited"
                error_message = (
                    "Claude returned a rate-limit error. The provider could not handle the "
                    "request after retries and fallbacks across all models."
                )

            send_error_email(error_type, f"An error occurred while calling the Claude API: {error_message}",
                            traceback.format_exc())
            print(f"API error: {e}")
            sys.exit(1)

        print("✓ Digest generated\n")

        # Step 3: Podcast Audio Pipeline
        podcast_url = None
        top_topics = []
        test_mode = os.getenv("PODCAST_TEST_MODE", "false").lower() == "true"
        audio_output_dir = os.getenv("AUDIO_OUTPUT_DIR", "")

        if audio_output_dir:
            try:
                print("🎙️ Generating podcast audio...")
                digest_text = extract_text_from_html(digest_html)
                top_topics = extract_top_topics(digest_html)

                print("  Generating podcast script via local LLM...")
                script = generate_podcast_script(digest_text, test_mode)
                print("  ✓ Script generated")

                segments = parse_script(script)
                print(f"  ✓ Parsed {len(segments)} dialogue segments")

                audio_path = generate_audio(segments, audio_output_dir, test_mode)
                print(f"  ✓ Audio saved to {audio_path}")

                cleanup_old_audio(audio_output_dir)

                # Trigger Audiobookshelf library scan
                abs_url = os.getenv("AUDIOBOOKSHELF_URL", "")
                api_key = os.getenv("AUDIOBOOKSHELF_API_KEY", "")
                library_id = os.getenv("AUDIOBOOKSHELF_LIBRARY_ID", "")

                if all([abs_url, api_key, library_id]):
                    trigger_library_scan(abs_url, api_key, library_id)
                    podcast_url = get_podcast_url(abs_url)
                else:
                    print("  Skipping Audiobookshelf scan (not configured)")

                print("✓ Podcast pipeline complete\n")
            except Exception as e:
                print(f"⚠️ Podcast generation failed: {e}")
                send_error_email("Podcast Generation Failed", str(e), traceback.format_exc())
                # Continue — email digest still sends without audio
        else:
            print("⏭️ Podcast pipeline skipped (AUDIO_OUTPUT_DIR not set)\n")

        # Step 4: Send email
        print("📧 Sending email...")
        success = send_email(digest_html, podcast_url=podcast_url, top_topics=top_topics)

        if success:
            # Mark articles as sent so they won't be included tomorrow
            print("💾 Saving article history...")
            history = mark_articles_as_sent(articles, history)
            save_history(history)
            print(f"✓ Marked {len(articles)} articles as sent\n")

            print(f"{'='*60}")
            print("✓ Daily digest completed successfully!")
            print(f"{'='*60}\n")
        else:
            send_error_email(
                "Email Sending Failed",
                "The digest was generated but could not be sent. Check your Gmail "
                "configuration in the .env file (GMAIL_ADDRESS, GMAIL_APP_PASSWORD)."
            )
            sys.exit(1)

    except Exception as e:
        # Catch-all for unexpected errors
        send_error_email(
            "Unexpected Error",
            f"An unexpected error occurred: {e}",
            traceback.format_exc()
        )
        print(f"Unexpected error: {e}")
        traceback.print_exc()
        sys.exit(1)


if __name__ == "__main__":
    main()
