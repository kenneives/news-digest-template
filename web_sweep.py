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
  SWEEP_MAX_EMAIL_ITEMS         bullet cap in the email section (default: 14)
"""

from __future__ import annotations

import hashlib
import html
import json
import os
import uuid
from datetime import datetime, timedelta, timezone

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


def build_lane_prompt(lane: dict, now_iso: str) -> str:
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
    return "\n".join(lines)


# =============================================================================
# Claude call (server-side web_search + forced report tool)
# =============================================================================

def _web_search_tool(version: str, max_searches: int) -> dict:
    return {"type": version, "name": "web_search", "max_uses": max_searches}


def _run_lane_once(client, model: str, prompt: str, ws_version: str, max_searches: int):
    """One lane pass on one model. Handles pause_turn continuations.

    Returns the report tool's input dict, or None if the model never called it.
    """
    tools = [_web_search_tool(ws_version, max_searches), REPORT_SIGNALS_TOOL]
    messages = [{"role": "user", "content": prompt}]
    response = None
    for _ in range(4):  # initial call + up to 3 pause_turn continuations
        # Stream and accumulate: a web-search research turn can run for
        # minutes, and a non-streaming call hits the SDK HTTP timeout.
        with client.messages.stream(
            model=model, max_tokens=8192, tools=tools, messages=messages,
        ) as stream:
            response = stream.get_final_message()
        if response.stop_reason == "pause_turn":
            messages = [
                {"role": "user", "content": prompt},
                {"role": "assistant", "content": response.content},
            ]
            continue
        break

    if response is None:
        return None
    for block in response.content:
        if getattr(block, "type", "") == "tool_use" and getattr(block, "name", "") == REPORT_SIGNALS_TOOL["name"]:
            return block.input
    return None


def run_lane(client, model_order: list[str], lane: dict, max_searches: int):
    """Run one lane, trying models in order and downgrading the web_search tool
    version for models that don't support the newer one. Raises on total failure."""
    prompt = build_lane_prompt(lane, datetime.now().strftime("%Y-%m-%d"))
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


def normalize_signals(raw, lane: dict, batch_id: str, today_str: str) -> list[dict]:
    """Turn a raw report_sweep_signals payload into stored signals, dropping
    ungrounded entries (no subject/summary/http source URL) and deduping by
    subject+type. Never throws."""
    signals: list[dict] = []
    seen: set[str] = set()
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
        metrics = s.get("metrics")
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
            "observed_at": observed if len(observed) == 10 else "",
            "metrics": metrics if isinstance(metrics, dict) else {},
            "severity": severity if severity in SEVERITY_ORDER else "info",
            "entity": _clean(s.get("entity")),
            "role": _clean(s.get("role")),
            "bucket": _clean(s.get("bucket")),
        })
    return signals


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


def update_sweep_history(history: dict, signals: list[dict], batch_id: str, days: int = 45) -> dict:
    """Merge signals into history['web_sweep'] (rolling ~45 days, deduped by id)
    and stamp the weekly gate. history rides the existing remote sync."""
    merged: dict[str, dict] = {}
    for s in history.get("web_sweep", []) + signals:
        if s.get("id"):
            merged[s["id"]] = s
    cutoff = (datetime.now(timezone.utc) - timedelta(days=days)).strftime("%Y-%m-%d")
    history["web_sweep"] = [s for s in merged.values() if (s.get("date") or "") >= cutoff]
    history["web_sweep_meta"] = {
        "last_run_date": datetime.now().strftime("%Y-%m-%d"),
        "last_batch": batch_id,
    }
    return history


# =============================================================================
# Email section
# =============================================================================

def build_email_section(signals: list[dict], batch_id: str, lane_errors: list[str]) -> str:
    """Deterministic HTML section (matches the digest's h2/ul/li structure so
    the email CSS applies). Built in code, not by the model, so URLs and
    numbers arrive exactly as reported."""
    if not signals and not lane_errors:
        return ""
    max_items = int(os.getenv("SWEEP_MAX_EMAIL_ITEMS", "14"))
    ordered = sorted(signals, key=lambda s: (SEVERITY_ORDER.get(s["severity"], 2), s["topic_label"]))
    shown = ordered[:max_items]

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
        parts.append(
            f"<li>{badge} <strong>{html.escape(s['subject'])}</strong> "
            f"({html.escape(s['lane'])}): {html.escape(s['summary'])}{rel} "
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
    if lane_errors:
        footer += f" · {len(lane_errors)} lane(s) failed"
    parts.append(f"<p><em>{html.escape(footer)}</em></p>")
    return "\n".join(parts)


# =============================================================================
# Orchestrator
# =============================================================================

def run_sweep(client, model_order: list[str], lanes: list[dict], history: dict):
    """Run every lane, fold results into history, and return
    (signals, email_html). Best-effort: lane failures are collected, not
    raised, and this function itself never raises."""
    try:
        batch_id = uuid.uuid4().hex
        today_str = datetime.now().strftime("%Y-%m-%d")
        max_searches = int(os.getenv("SWEEP_MAX_SEARCHES_PER_LANE", "6"))
        all_signals: list[dict] = []
        lane_errors: list[str] = []

        for lane in lanes:
            try:
                raw = run_lane(client, model_order, lane, max_searches)
                lane_signals = normalize_signals(raw, lane, batch_id, today_str) if raw else []
                all_signals.extend(lane_signals)
                print(f"  🔎 sweep lane {lane['key']}: {len(lane_signals)} signals")
            except Exception as e:
                lane_errors.append(f"{lane['key']}: {e}")
                print(f"  ⚠️ sweep lane {lane['key']} failed: {e}")

        update_sweep_history(history, all_signals, batch_id)
        section = build_email_section(all_signals, batch_id, lane_errors)
        print(f"🔎 Web sweep: {len(all_signals)} signals across {len(lanes)} lanes "
              f"({len(lane_errors)} failed)")
        return all_signals, section
    except Exception as e:
        print(f"⚠️ Web sweep failed entirely (digest unaffected): {e}")
        return [], ""
