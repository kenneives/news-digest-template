"""Web-grounded sweep lanes for the news digest.

RSS tells you what the press published; a sweep goes looking. Each "lane" is
one research assignment (a brief + a target signal count) handed to Claude
with the server-side web_search tool. The model must ground every finding in
a page it actually visited and report it through a forced tool call with a
required source URL — ungrounded findings are dropped at normalization, so
hallucinated links are structurally impossible rather than merely discouraged.

Design notes:
- Generic engine: lane definitions are plain dicts (see EXAMPLE_LANES).
  Nothing in this file is specific to any person or company.
- Runs weekly (gated via history) inside the existing daily digest run —
  see should_run_sweep(). SWEEP_FORCE=true forces a run for testing.
- Severity is deliberately calibrated against crying wolf: most weeks have
  zero act_now signals, and a digest that cries wolf gets ignored.
- Every public entry point is best-effort; run_sweep() never raises, so a
  sweep failure can never take down the daily digest.

Env vars (all optional):
  SWEEP_ENABLED                 gate in news_digest.py (not read here)
  SWEEP_DAY                     weekday to run on (default: monday)
  SWEEP_FORCE                   "true" = run regardless of the weekly gate
  SWEEP_MAX_SEARCHES_PER_LANE   web searches per lane (default: 6)
  SWEEP_MAX_EMAIL_ITEMS         bullet cap in the email section (default: 18)
  SWEEP_CONCURRENCY             lanes run in parallel (default: 3)
  SWEEP_ACT_NOW_MAX_AGE_DAYS    act_now older than this (by observed_at) is
                                downgraded to notable + flagged catch_up (default: 14)
  SWEEP_PRIOR_SUBJECTS_DAYS     window of already-reported subjects fed back
                                into each lane prompt (default: 28)
"""

from __future__ import annotations

import hashlib
import html
import json
import os
import re
import uuid
from concurrent.futures import ThreadPoolExecutor
from datetime import datetime, timedelta, timezone
from urllib.parse import parse_qsl, urlencode, urlsplit

# =============================================================================
# Severity + example lanes
# =============================================================================

SEVERITY_ORDER = {"act_now": 0, "notable": 1, "info": 2}
SEVERITY_BADGE = {"act_now": "🔴", "notable": "🟡", "info": "▫️"}

# Generic starter lanes — replace with your own. Each lane is one web-grounded
# research pass; keep briefs concrete about WHAT to capture (numbers, price
# points, names), not just the subject area.
EXAMPLE_LANES = [
    {
        "key": "competitor",
        "topic": "ventures",
        "topic_label": "Competitive Landscape",
        "framing": (
            "Context on us: we make an example product in an example market. "
            "Judge relevance against that. (Replace this framing with a short "
            "paragraph on YOUR product, market, and closest analogs.)"
        ),
        "brief": (
            "Product, pricing, funding, or positioning moves by our competitors "
            "and adjacent companies (e.g. Acme AI, Example Co). Include new "
            "entrants: Product Hunt / Hacker News launches and GitHub-only "
            "projects that mainstream press would miss."
        ),
        "target_signals": 5,
    },
    {
        "key": "market-pricing",
        "topic": "ventures",
        "topic_label": "Competitive Landscape",
        "framing": (
            "Context on us: we make an example product in an example market. "
            "(Replace with your framing.)"
        ),
        "brief": (
            "Market and pricing signals in our category: best-seller movement, "
            "price anchors for comparable products, review themes (what buyers "
            "love/hate), notable roundup or gift-guide placements."
        ),
        "target_signals": 4,
    },
]

# =============================================================================
# Report tool (forced grounding)
# =============================================================================

REPORT_SIGNALS_TOOL = {
    "name": "report_sweep_signals",
    "description": (
        "Report the signals you found via web_search. Call this exactly once, "
        "after your research, with every signal you could ground in a real "
        "source. Skip anything you could not source."
    ),
    "input_schema": {
        "type": "object",
        "properties": {
            "signals": {
                "type": "array",
                "description": "The grounded signals.",
                "items": {
                    "type": "object",
                    "properties": {
                        "signal_type": {
                            "type": "string",
                            "description": (
                                "Short snake_case subtype, e.g. product_launch, "
                                "price_move, funding_round, campaign_launch, "
                                "chart_move, partnership, standard_update, "
                                "review_theme."
                            ),
                        },
                        "subject": {
                            "type": "string",
                            "description": "The thing observed, e.g. 'Example Co Party Pack 3'.",
                        },
                        "summary": {
                            "type": "string",
                            "description": "1-2 sentences: what happened, with the concrete numbers.",
                        },
                        "relevance": {
                            "type": "string",
                            "description": "One sentence: why this matters (or doesn't much) for us specifically.",
                        },
                        "source_name": {"type": "string", "description": "Named source, e.g. 'Kickstarter'."},
                        "source_url": {
                            "type": "string",
                            "description": "Direct URL to the source. Required — this is the grounding.",
                        },
                        "observed_at": {
                            "type": "string",
                            "description": "When the event happened, YYYY-MM-DD (approximate is fine).",
                        },
                        "metrics": {
                            "type": "object",
                            "description": (
                                "Concrete figures as key/value pairs, e.g. "
                                "{\"pledged_usd\": 412000, \"backers\": 5100, \"price_usd\": 34.99}."
                            ),
                        },
                        "severity": {
                            "type": "string",
                            "description": (
                                "info = background texture; notable = worth 30 seconds; "
                                "act_now = time-sensitive opportunity or threat to respond "
                                "to this week."
                            ),
                        },
                        "entity": {
                            "type": "string",
                            "description": "Canonical company/project name the signal is about, if it is about one.",
                        },
                        "role": {
                            "type": "string",
                            "description": "From OUR perspective: competitor | collaborator | both. Omit if unclear.",
                        },
                        "bucket": {
                            "type": "string",
                            "description": "Strategic bucket, only if the lane brief lists a vocabulary. Omit otherwise.",
                        },
                    },
                    "required": ["signal_type", "subject", "summary", "source_url", "severity"],
                },
            },
        },
        "required": ["signals"],
    },
}


def build_lane_prompt(lane: dict, now_iso: str, prior_subjects: list[str] | None = None) -> str:
    lines = [
        "You run a weekly web-grounded intelligence sweep for a personal news "
        "digest. Sweep ONE lane and report structured signals.",
        "",
        lane.get("framing", ""),
        "",
        f"Today's date: {now_iso[:10]}.",
        "",
        f"THIS LANE ({lane['key']}): {lane['brief']}",
        "",
        "RULES:",
        "1. Use web_search; every signal must be grounded in a page you actually "
        "found — no memory, no speculation. Prefer developments from the last "
        "7-14 days.",
        f"2. Aim for about {lane.get('target_signals', 5)} signals. Rank severity "
        "honestly — most weeks have zero act_now signals, and a digest that "
        "cries wolf gets ignored.",
        "3. Put concrete figures in metrics, not prose.",
        "4. When done, call report_sweep_signals exactly once.",
    ]
    extra = lane.get("extra_rules")
    if extra:
        lines.extend(["", extra])
    if prior_subjects:
        # Weekly search windows overlap on purpose; without this the model
        # re-finds the same Kickstarter three weeks running.
        lines.extend([
            "",
            "ALREADY REPORTED in recent weeks — do NOT report these again unless "
            "there is a genuinely NEW development (a new date, number, price, or "
            "product), and then say what's new in the summary:",
        ])
        lines.extend(f"- {subj}" for subj in prior_subjects)
    return "\n".join(lines)


# =============================================================================
# Claude call (server-side web_search + forced report tool)
# =============================================================================

def _web_search_tool(version: str, max_searches: int) -> dict:
    return {"type": version, "name": "web_search", "max_uses": max_searches}


def _run_lane_once(client, model: str, prompt: str, ws_version: str, max_searches: int):
    """One lane pass on one model. Handles pause_turn continuations.

    Returns the report tool's input dict. A turn that ends WITHOUT the report
    tool call is an error, never a silent empty — an empty week must arrive as
    an explicit signals:[] report, so "quiet" and "broken" stay distinguishable.
    """
    tools = [_web_search_tool(ws_version, max_searches), REPORT_SIGNALS_TOOL]
    messages = [{"role": "user", "content": prompt}]
    response = None
    for _ in range(4):  # initial call + up to 3 pause_turn continuations
        # Stream and accumulate: a web-search research turn can run for
        # minutes, and a non-streaming call hits the SDK HTTP timeout.
        with client.messages.stream(
            model=model, max_tokens=16384, tools=tools, messages=messages,
        ) as stream:
            response = stream.get_final_message()
        if response.stop_reason == "pause_turn":
            messages = [
                {"role": "user", "content": prompt},
                {"role": "assistant", "content": response.content},
            ]
            continue
        break

    for block in response.content:
        if getattr(block, "type", "") == "tool_use" and getattr(block, "name", "") == REPORT_SIGNALS_TOOL["name"]:
            return block.input
    block_types = [getattr(b, "type", "?") for b in response.content]
    raise RuntimeError(
        f"model {model} never called {REPORT_SIGNALS_TOOL['name']} "
        f"(stop_reason={response.stop_reason}, blocks={block_types[-4:]})"
    )


def run_lane(client, model_order: list[str], lane: dict, max_searches: int,
             prior_subjects: list[str] | None = None):
    """Run one lane, trying models in order and downgrading the web_search tool
    version for models that don't support the newer one. Raises on total failure."""
    prompt = build_lane_prompt(lane, datetime.now().strftime("%Y-%m-%d"), prior_subjects)
    last_error: Exception | None = None
    for model in (model_order or ["claude-sonnet-4-6"]):
        for ws_version in ("web_search_20260209", "web_search_20250305"):
            try:
                return _run_lane_once(client, model, prompt, ws_version, max_searches)
            except Exception as e:
                last_error = e
                msg = str(e).lower()
                # Older/smaller models reject the newer web_search variant with a
                # 400 naming the tool — retry with the basic variant, then move on.
                if "web_search" in msg or "tool" in msg and "type" in msg:
                    continue
                break  # non-tool error: try the next model
    raise last_error if last_error else RuntimeError("no model available for sweep")


# =============================================================================
# Normalization
# =============================================================================

def _clean(v) -> str:
    return v.strip() if isinstance(v, str) else ""


def signal_id(subject: str, url: str) -> str:
    """Same md5-of-'title|link' recipe as the digest's article hash, so sweep
    entries dedupe cleanly against feed-sourced entries in shared sinks."""
    unique = f"{subject.lower().strip()}|{url.lower().strip()}"
    return hashlib.md5(unique.encode()).hexdigest()


# Story-level dedup keys. signal_id (subject|url) is the storage identity, but
# the model rewrites subjects week to week, so repeats are detected on the
# normalized URL and on (entity, signal_type, month) as well.
_TRACKING_PARAM_RE = re.compile(
    r"^(utm_|ref$|ref_|fbclid|gclid|mc_cid|mc_eid|igshid|cmpid|ncid|si$|s$|source$|share)",
    re.IGNORECASE,
)


def normalize_url(url: str) -> str:
    """Host + path + non-tracking query, lowercase host, no www/fragment/trailing
    slash — so the same article via two share links is one story."""
    try:
        p = urlsplit(url.strip())
    except ValueError:
        return url.strip().lower()
    host = p.netloc.lower()
    host = host[4:] if host.startswith("www.") else host
    path = re.sub(r"/+$", "", p.path) or "/"
    query = sorted((k, v) for k, v in parse_qsl(p.query, keep_blank_values=True)
                   if not _TRACKING_PARAM_RE.match(k))
    return host + path + (f"?{urlencode(query)}" if query else "")


def entity_key(entity: str, canonicalize=None) -> str:
    """Lowercase canonical entity. `canonicalize(name) -> canonical | None` is
    an optional hook (e.g. a private alias list); the generic fallback strips
    parentheticals and takes the first ' / '-separated name."""
    if not entity:
        return ""
    if canonicalize is not None:
        try:
            canon = canonicalize(entity)
        except Exception:
            canon = None
        if canon:
            return canon.lower()
    e = re.sub(r"\(.*?\)", " ", entity.lower()).split(" / ")[0]
    e = re.sub(r"[^\w.&+ ]+", " ", e)
    return " ".join(e.split())


# Event types that happen at most once per entity per month, so a second
# outlet's coverage is the same story. Launches/updates are NOT here — a
# company can ship twice in a month and both are news.
_EVENT_DEDUP_TYPES = {"funding_round", "acquisition", "ipo", "shutdown",
                      "campaign_launch", "campaign_end", "layoffs"}


def event_key(signal: dict) -> str:
    """(entity, signal_type, observed month) for one-off event types — the
    same funding round from a second outlet is a repeat, not news."""
    ek = signal.get("entity_key") or ""
    obs = signal.get("observed_at") or ""
    stype = (signal.get("signal_type") or "").lower()
    if ek and len(obs) >= 7 and stype in _EVENT_DEDUP_TYPES:
        return f"{ek}|{stype}|{obs[:7]}"
    return ""


def _keys_for(signal: dict, canonicalize=None) -> tuple[str, str, str]:
    """(id, url_key, event_key) for any signal, including pre-dedup history
    rows that lack the stored keys."""
    url_key = signal.get("url_key") or normalize_url(signal.get("source_url", ""))
    if not signal.get("entity_key"):
        signal = dict(signal, entity_key=entity_key(signal.get("entity", ""), canonicalize))
    return signal.get("id", ""), url_key, event_key(signal)


def normalize_signals(raw, lane: dict, batch_id: str, today_str: str,
                      canonicalize=None) -> list[dict]:
    """Turn a raw report_sweep_signals payload into stored signals, dropping
    ungrounded entries (no subject/summary/http source URL) and deduping by
    subject+type. Applies the act_now age rule. Never throws."""
    signals: list[dict] = []
    seen: set[str] = set()
    max_age = int(os.getenv("SWEEP_ACT_NOW_MAX_AGE_DAYS", "14"))
    try:
        today = datetime.strptime(today_str, "%Y-%m-%d")
    except ValueError:
        today = datetime.now()
    items = raw.get("signals") if isinstance(raw, dict) else None
    for entry in items if isinstance(items, list) else []:
        s = entry if isinstance(entry, dict) else {}
        subject = _clean(s.get("subject"))
        summary = _clean(s.get("summary"))
        url = _clean(s.get("source_url"))
        if not subject or not summary or not url.lower().startswith(("http://", "https://")):
            continue
        key = f"{subject}|{_clean(s.get('signal_type'))}".lower()
        if key in seen:
            continue
        seen.add(key)
        severity = _clean(s.get("severity"))
        observed = _clean(s.get("observed_at"))
        observed = observed if len(observed) == 10 else ""
        metrics = s.get("metrics")
        # Age rule: an event older than max_age days is never act_now — both
        # this sweep and QTG's flagged a June funding round as urgent months
        # later. It stays visible as notable, tagged catch-up.
        catch_up = False
        if severity == "act_now" and observed:
            try:
                age_days = (today - datetime.strptime(observed, "%Y-%m-%d")).days
            except ValueError:
                age_days = 0
            if age_days > max_age:
                severity = "notable"
                catch_up = True
        entity = _clean(s.get("entity"))
        signals.append({
            "id": signal_id(subject, url),
            "batch": batch_id,
            "date": today_str,
            "lane": lane["key"],
            "topic": lane.get("topic", "general"),
            "topic_label": lane.get("topic_label", "Web Sweep"),
            "signal_type": _clean(s.get("signal_type")) or "observation",
            "subject": subject,
            "summary": summary,
            "relevance": _clean(s.get("relevance")),
            "source_name": _clean(s.get("source_name")),
            "source_url": url,
            "observed_at": observed,
            "metrics": metrics if isinstance(metrics, dict) else {},
            "severity": severity if severity in SEVERITY_ORDER else "info",
            "catch_up": catch_up,
            "entity": entity,
            "entity_key": entity_key(entity, canonicalize),
            "url_key": normalize_url(url),
            "role": _clean(s.get("role")),
            "bucket": _clean(s.get("bucket")),
        })
    return signals


def prior_subjects(history: dict, lane: dict, days: int | None = None, cap: int = 30) -> list[str]:
    """Subjects already reported for this lane's topic in the last `days`,
    newest first, for the prompt's ALREADY REPORTED block."""
    days = days or int(os.getenv("SWEEP_PRIOR_SUBJECTS_DAYS", "28"))
    cutoff = (datetime.now() - timedelta(days=days)).strftime("%Y-%m-%d")
    topic = lane.get("topic", "general")
    rows = [s for s in history.get("web_sweep", [])
            if s.get("topic") == topic and (s.get("date") or "") >= cutoff]
    rows.sort(key=lambda s: s.get("date", ""), reverse=True)
    out: list[str] = []
    seen: set[str] = set()
    for s in rows:
        subj = (s.get("subject") or "").strip()
        if subj and subj.lower() not in seen:
            seen.add(subj.lower())
            out.append(subj)
        if len(out) >= cap:
            break
    return out


# =============================================================================
# Weekly gate + history
# =============================================================================

def should_run_sweep(history: dict, force: bool = False) -> bool:
    """Weekly gate, evaluated inside the daily run. Runs when forced, on the
    configured weekday, on first deploy (no prior run), or if 8+ days have
    somehow passed (e.g. the box was off on sweep day)."""
    if force or os.getenv("SWEEP_FORCE", "false").lower() == "true":
        return True
    today = datetime.now()
    today_str = today.strftime("%Y-%m-%d")
    last = (history.get("web_sweep_meta") or {}).get("last_run_date", "")
    if last == today_str:
        return False
    if not last:
        return True
    try:
        days_since = (today - datetime.strptime(last, "%Y-%m-%d")).days
    except ValueError:
        return True
    sweep_day = os.getenv("SWEEP_DAY", "monday").strip().lower()
    return today.strftime("%A").lower() == sweep_day or days_since >= 8


def update_sweep_history(history: dict, signals: list[dict], batch_id: str, days: int = 45,
                         lane_stats: list[dict] | None = None,
                         lane_errors: list[str] | None = None) -> dict:
    """Merge signals into history['web_sweep'] (rolling ~45 days, deduped by id)
    and stamp the weekly gate. history rides the existing remote sync — lane
    stats/errors are stamped into the meta so a run is diagnosable from the
    synced copy, not just the local digest.log."""
    merged: dict[str, dict] = {}
    for s in history.get("web_sweep", []) + signals:
        if s.get("id"):
            merged[s["id"]] = s
    cutoff = (datetime.now(timezone.utc) - timedelta(days=days)).strftime("%Y-%m-%d")
    history["web_sweep"] = [s for s in merged.values() if (s.get("date") or "") >= cutoff]
    history["web_sweep_meta"] = {
        "last_run_date": datetime.now().strftime("%Y-%m-%d"),
        "last_batch": batch_id,
        "lanes": lane_stats or [],
        "lane_errors": lane_errors or [],
    }
    return history


# =============================================================================
# Email section
# =============================================================================

def _fair_pick(signals: list[dict], max_items: int) -> list[dict]:
    """Pick up to max_items for the email without starving any section.

    A plain severity-then-label sort lets one busy topic fill the whole cap
    (the alphabetically-last section can get fully cut on a busy week).
    Instead: walk severity tiers in order, round-robining across topic_labels
    within each tier, then re-sort the picks for grouped display.
    """
    by_tier: dict[int, dict[str, list[dict]]] = {}
    for s in signals:
        tier = SEVERITY_ORDER.get(s["severity"], 2)
        by_tier.setdefault(tier, {}).setdefault(s["topic_label"], []).append(s)
    picked: list[dict] = []
    for tier in sorted(by_tier):
        groups = [by_tier[tier][label] for label in sorted(by_tier[tier])]
        while len(picked) < max_items and any(groups):
            for grp in groups:
                if grp and len(picked) < max_items:
                    picked.append(grp.pop(0))
        if len(picked) >= max_items:
            break
    return sorted(picked, key=lambda s: (s["topic_label"],
                                         SEVERITY_ORDER.get(s["severity"], 2)))


def build_email_section(signals: list[dict], batch_id: str, lane_errors: list[str],
                        n_repeats: int = 0) -> str:
    """Deterministic HTML section (matches the digest's h2/ul/li structure so
    the email CSS applies). Built in code, not by the model, so URLs and
    numbers arrive exactly as reported. `signals` here is the email-facing set
    (repeats already filtered out); n_repeats keeps the footer honest so an
    all-repeat week still renders a section instead of a silent empty."""
    if not signals and not lane_errors and not n_repeats:
        return ""
    max_items = int(os.getenv("SWEEP_MAX_EMAIL_ITEMS", "18"))
    shown = _fair_pick(signals, max_items)

    parts = ["<h2>🔎 Weekly Web Sweep</h2>"]
    current_label = None
    open_list = False
    for s in shown:
        if s["topic_label"] != current_label:
            if open_list:
                parts.append("</ul>")
            current_label = s["topic_label"]
            parts.append(f"<h3>{html.escape(current_label)}</h3>")
            parts.append("<ul>")
            open_list = True
        badge = SEVERITY_BADGE.get(s["severity"], "▫️")
        rel = f" — <em>{html.escape(s['relevance'])}</em>" if s["relevance"] else ""
        src = html.escape(s["source_name"] or "source")
        tag = f"{s['lane']}, catch-up {s['observed_at']}" if s.get("catch_up") else s["lane"]
        parts.append(
            f"<li>{badge} <strong>{html.escape(s['subject'])}</strong> "
            f"({html.escape(tag)}): {html.escape(s['summary'])}{rel} "
            f"<a href=\"{html.escape(s['source_url'], quote=True)}\">{src}</a></li>"
        )
    if open_list:
        parts.append("</ul>")

    counts = {sev: sum(1 for s in signals if s["severity"] == sev) for sev in SEVERITY_ORDER}
    footer = (
        f"act_now {counts['act_now']} · notable {counts['notable']} · "
        f"info {counts['info']} · batch {batch_id[:8]}"
    )
    if len(signals) > len(shown):
        footer += f" · +{len(signals) - len(shown)} more in history"
    if n_repeats:
        footer += f" · {n_repeats} repeat(s) from prior weeks suppressed"
    if lane_errors:
        footer += f" · {len(lane_errors)} lane(s) failed"
    parts.append(f"<p><em>{html.escape(footer)}</em></p>")
    return "\n".join(parts)


# =============================================================================
# Orchestrator
# =============================================================================

def run_sweep(client, model_order: list[str], lanes: list[dict], history: dict,
              canonicalize=None):
    """Run every lane (concurrently — a single research lane takes minutes,
    so sequential lanes would stretch the digest run), fold results into
    history, and return (signals, email_html). Best-effort: lane failures are
    collected, not raised, and this function itself never raises.

    `canonicalize(name) -> canonical | None` is an optional hook so a private
    alias list can fold 'Board / board.fun' and 'board.fun' into one entity
    for dedup; without it a generic normalization is used."""
    try:
        batch_id = uuid.uuid4().hex
        today_str = datetime.now().strftime("%Y-%m-%d")
        max_searches = int(os.getenv("SWEEP_MAX_SEARCHES_PER_LANE", "6"))
        workers = max(1, int(os.getenv("SWEEP_CONCURRENCY", "3")))
        all_signals: list[dict] = []
        lane_errors: list[str] = []
        lane_stats: list[dict] = []

        def _do_lane(lane: dict):
            raw = run_lane(client, model_order, lane, max_searches,
                           prior_subjects(history, lane))
            n_raw = len(raw.get("signals", [])) if isinstance(raw, dict) else 0
            return normalize_signals(raw, lane, batch_id, today_str, canonicalize), n_raw

        with ThreadPoolExecutor(max_workers=workers) as pool:
            futures = [(lane, pool.submit(_do_lane, lane)) for lane in lanes]
            for lane, future in futures:
                try:
                    lane_signals, n_raw = future.result()
                    all_signals.extend(lane_signals)
                    lane_stats.append({"lane": lane["key"], "reported": n_raw,
                                       "kept": len(lane_signals)})
                    print(f"  🔎 sweep lane {lane['key']}: {n_raw} reported → "
                          f"{len(lane_signals)} kept after grounding checks")
                except Exception as e:
                    lane_errors.append(f"{lane['key']}: {e}")
                    print(f"  ⚠️ sweep lane {lane['key']} failed: {e}")

        # Email shows only signals not already in a prior week's history —
        # the 7-14 day search window overlaps on purpose, so re-finds are
        # expected. A repeat is the same id, the same story URL, or the same
        # (entity, type, month) event. Snapshot prior keys BEFORE the merge;
        # everything still lands in history and the returned list (ledger
        # dedupes by id).
        prior_ids: set[str] = set()
        prior_urls: set[str] = set()
        prior_events: set[str] = set()
        for s in history.get("web_sweep", []):
            pid, purl, pev = _keys_for(s, canonicalize)
            prior_ids.add(pid)
            prior_urls.add(purl)
            if pev:
                prior_events.add(pev)
        update_sweep_history(history, all_signals, batch_id,
                             lane_stats=lane_stats, lane_errors=lane_errors)
        fresh = [s for s in all_signals
                 if s["id"] not in prior_ids
                 and s["url_key"] not in prior_urls
                 and (not event_key(s) or event_key(s) not in prior_events)]
        n_repeats = len(all_signals) - len(fresh)
        section = build_email_section(fresh, batch_id, lane_errors, n_repeats)
        print(f"🔎 Web sweep: {len(all_signals)} signals across {len(lanes)} lanes "
              f"({n_repeats} repeats suppressed, {len(lane_errors)} failed)")
        return all_signals, section
    except Exception as e:
        print(f"⚠️ Web sweep failed entirely (digest unaffected): {e}")
        return [], ""


# =============================================================================
# Standalone test mode:  python web_sweep.py [lane_key ...]
# Runs lanes and prints results. No email, no history writes, no ledger.
# =============================================================================

if __name__ == "__main__":
    import sys

    import anthropic

    try:
        from dotenv import load_dotenv
        load_dotenv()
    except ImportError:
        pass  # fine if ANTHROPIC_API_KEY is already in the environment

    _canon = None
    try:
        import sweep_lanes_private
        lanes = sweep_lanes_private.get_lanes()
        _canon = getattr(sweep_lanes_private, "canonical_entity", None)
    except ImportError:
        lanes = EXAMPLE_LANES
    if sys.argv[1:]:
        lanes = [l for l in lanes if l["key"] in sys.argv[1:]]
        if not lanes:
            sys.exit(f"no lanes match {sys.argv[1:]}")

    _client = anthropic.Anthropic()
    try:  # resolve models exactly like the production digest run
        from news_digest import resolve_model_order
        _models = resolve_model_order(_client)
    except Exception as _e:
        print(f"(model resolution unavailable: {_e} — using default)")
        _models = []
    print(f"Testing {len(lanes)} lane(s) on models {_models or ['claude-sonnet-4-6']}: "
          f"{', '.join(l['key'] for l in lanes)}")
    _signals, _html = run_sweep(_client, _models, lanes, history={}, canonicalize=_canon)
    for _s in sorted(_signals, key=lambda s: SEVERITY_ORDER.get(s["severity"], 2)):
        print(f"\n[{_s['severity']}] ({_s['lane']}) {_s['subject']}")
        print(f"  {_s['summary']}")
        if _s["relevance"]:
            print(f"  → {_s['relevance']}")
        print(f"  {_s['source_url']}")
    print(f"\n{len(_signals)} signals total; email section {len(_html)} chars. "
          "(Nothing was saved or emailed — test mode.)")
