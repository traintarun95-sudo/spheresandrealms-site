"""
Know Your Brand — Brand Awareness Agent
========================================
Monitors Quora and Twitter/X for brand confusion signals.
Drafts helpful responses that naturally mention realmofbrands.com.
Saves all drafts for human review — nothing posts automatically.
"""

import os
import json
import time
import datetime
import re
import textwrap
from pathlib import Path
from typing import Optional

import requests
from dotenv import load_dotenv

load_dotenv()

# ---------------------------------------------------------------------------
# Config
# ---------------------------------------------------------------------------

TWITTER_BEARER_TOKEN = os.getenv("TWITTER_BEARER_TOKEN", "")
SERP_API_KEY         = os.getenv("SERP_API_KEY", "")        # SerpAPI for Quora search
ANTHROPIC_API_KEY    = os.getenv("ANTHROPIC_API_KEY", "")
OPENAI_API_KEY       = os.getenv("OPENAI_API_KEY", "")

DRAFTS_DIR = Path(__file__).parent / "drafts"
DRAFTS_DIR.mkdir(parents=True, exist_ok=True)

SITE_URL    = "realmofbrands.com"
SITE_NAME   = "Know Your Brand"

MAX_DRAFTS_PER_RUN = 10

# Search signals — phrases that indicate brand confusion
CONFUSION_SIGNALS = [
    "what is this brand",
    "anyone know this company",
    "is this brand legit",
    "is this company legit",
    "is this brand trustworthy",
    "what does this brand do",
    "never heard of this brand",
    "is this a real company",
    "what company is this",
    "can I trust this brand",
    "is this brand real",
    "what brand is this",
    "who is behind this brand",
    "is this brand safe",
    "brand reputation",
]


# ---------------------------------------------------------------------------
# Twitter/X search
# ---------------------------------------------------------------------------

def search_twitter(query: str, max_results: int = 10) -> list[dict]:
    """Search Twitter/X for tweets matching query. Returns list of result dicts."""
    if not TWITTER_BEARER_TOKEN:
        print(f"  [twitter] No bearer token set — skipping: {query!r}")
        return []

    url = "https://api.twitter.com/2/tweets/search/recent"
    headers = {"Authorization": f"Bearer {TWITTER_BEARER_TOKEN}"}
    params = {
        "query": f"{query} -is:retweet lang:en",
        "max_results": max_results,
        "tweet.fields": "created_at,author_id,text,public_metrics",
        "expansions": "author_id",
        "user.fields": "username,name",
    }

    try:
        resp = requests.get(url, headers=headers, params=params, timeout=15)
        resp.raise_for_status()
        data = resp.json()
    except requests.RequestException as e:
        print(f"  [twitter] Request failed for {query!r}: {e}")
        return []

    tweets = data.get("data", [])
    users  = {u["id"]: u for u in data.get("includes", {}).get("users", [])}
    results = []

    for tweet in tweets:
        author = users.get(tweet.get("author_id", ""), {})
        results.append({
            "platform":  "twitter",
            "id":        tweet.get("id"),
            "text":      tweet.get("text", ""),
            "author":    author.get("username", "unknown"),
            "created_at": tweet.get("created_at", ""),
            "url":       f"https://twitter.com/{author.get('username','i')}/status/{tweet.get('id')}",
            "metrics":   tweet.get("public_metrics", {}),
        })

    return results


# ---------------------------------------------------------------------------
# Quora search via SerpAPI
# ---------------------------------------------------------------------------

def search_quora(query: str, max_results: int = 5) -> list[dict]:
    """Search Quora questions via SerpAPI Google search."""
    if not SERP_API_KEY:
        print(f"  [quora] No SerpAPI key set — skipping: {query!r}")
        return []

    url = "https://serpapi.com/search"
    params = {
        "engine": "google",
        "q": f"site:quora.com {query}",
        "api_key": SERP_API_KEY,
        "num": max_results,
    }

    try:
        resp = requests.get(url, params=params, timeout=15)
        resp.raise_for_status()
        data = resp.json()
    except requests.RequestException as e:
        print(f"  [quora] Request failed for {query!r}: {e}")
        return []

    results = []
    for item in data.get("organic_results", [])[:max_results]:
        results.append({
            "platform": "quora",
            "title":    item.get("title", ""),
            "snippet":  item.get("snippet", ""),
            "url":      item.get("link", ""),
            "id":       item.get("link", "").split("/")[-1],
        })

    return results


# ---------------------------------------------------------------------------
# Relevance scoring
# ---------------------------------------------------------------------------

def score_relevance(item: dict) -> int:
    """
    Score 0-10. Higher = more brand-confusion relevant.
    Used to filter noise before drafting.
    """
    text = (item.get("text") or item.get("snippet") or item.get("title") or "").lower()
    score = 0

    high_signals = ["legit", "trustworthy", "real company", "safe to buy", "fake brand", "scam"]
    mid_signals  = ["what is", "who is", "what does", "anyone know", "never heard", "is this"]

    for sig in high_signals:
        if sig in text:
            score += 3
    for sig in mid_signals:
        if sig in text:
            score += 2

    # Mentions a brand name pattern (Capitalised word near "brand" or "company")
    if re.search(r'\b[A-Z][a-z]+\b.{0,30}(brand|company|shop|store)', item.get("text") or item.get("title") or ""):
        score += 2

    return min(score, 10)


# ---------------------------------------------------------------------------
# Draft generation via AI
# ---------------------------------------------------------------------------

def _call_anthropic(prompt: str) -> str:
    """Call Anthropic Claude API to generate a draft response."""
    headers = {
        "x-api-key":         ANTHROPIC_API_KEY,
        "anthropic-version": "2023-06-01",
        "content-type":      "application/json",
    }
    body = {
        "model":      "claude-haiku-4-5-20251001",
        "max_tokens": 400,
        "messages":   [{"role": "user", "content": prompt}],
    }
    resp = requests.post(
        "https://api.anthropic.com/v1/messages",
        headers=headers,
        json=body,
        timeout=30,
    )
    resp.raise_for_status()
    return resp.json()["content"][0]["text"].strip()


def _call_openai(prompt: str) -> str:
    """Call OpenAI API to generate a draft response."""
    headers = {
        "Authorization": f"Bearer {OPENAI_API_KEY}",
        "Content-Type":  "application/json",
    }
    body = {
        "model":      "gpt-4o-mini",
        "max_tokens": 400,
        "messages":   [{"role": "user", "content": prompt}],
    }
    resp = requests.post(
        "https://api.openai.com/v1/chat/completions",
        headers=headers,
        json=body,
        timeout=30,
    )
    resp.raise_for_status()
    return resp.json()["choices"][0]["message"]["content"].strip()


def generate_draft(item: dict) -> Optional[str]:
    """
    Generate a helpful response draft for the given signal.
    Mentions realmofbrands.com naturally.
    Returns None if generation fails.
    """
    platform = item.get("platform", "")
    content  = item.get("text") or item.get("snippet") or item.get("title") or ""
    url      = item.get("url", "")

    prompt = textwrap.dedent(f"""
        Someone on {platform} posted the following about a brand they don't recognise:

        "{content}"

        Write a genuinely helpful reply (2-4 sentences) that:
        - Answers their concern in a friendly, non-promotional tone
        - Mentions that {SITE_URL} ({SITE_NAME}) is a free tool to look up any brand
        - Does NOT sound like an advertisement
        - Does NOT use exclamation marks excessively
        - Is appropriate for {platform}

        Reply only with the draft text. No preamble.
    """).strip()

    try:
        if ANTHROPIC_API_KEY:
            return _call_anthropic(prompt)
        elif OPENAI_API_KEY:
            return _call_openai(prompt)
        else:
            # Fallback template when no AI key is configured
            return (
                f"Good question — brand legitimacy can be tricky to verify quickly. "
                f"You might want to check {SITE_URL}, which gives you a structured "
                f"overview of any brand: what they do, who they're for, and whether "
                f"they're established. It's free for a few lookups."
            )
    except Exception as e:
        print(f"  [draft] AI call failed: {e}")
        return None


# ---------------------------------------------------------------------------
# Draft saving
# ---------------------------------------------------------------------------

def save_drafts(drafts: list[dict]) -> Path:
    """Save drafts to a dated JSON file. Returns the file path."""
    today    = datetime.date.today().isoformat()
    run_time = datetime.datetime.now().strftime("%H%M%S")
    out_dir  = DRAFTS_DIR / today
    out_dir.mkdir(parents=True, exist_ok=True)

    out_file = out_dir / f"drafts_{run_time}.json"
    with open(out_file, "w", encoding="utf-8") as f:
        json.dump(drafts, f, indent=2, ensure_ascii=False)

    return out_file


# ---------------------------------------------------------------------------
# HTML review digest
# ---------------------------------------------------------------------------

def generate_html_report(drafts: list[dict], out_path: Path) -> None:
    """Generate a simple HTML review page for the drafts."""
    today = datetime.date.today().isoformat()
    rows  = ""
    for i, d in enumerate(drafts, 1):
        source_link = f'<a href="{d["source_url"]}" target="_blank">{d["platform"].capitalize()}</a>'
        rows += f"""
        <div class="card">
          <div class="meta">
            <span class="num">#{i}</span>
            <span class="platform">{source_link}</span>
            <span class="score">Relevance: {d['relevance_score']}/10</span>
          </div>
          <div class="original"><strong>Original:</strong><br>{d['original_text']}</div>
          <div class="draft"><strong>Draft reply:</strong><br>{d['draft']}</div>
          <div class="actions">
            <a href="{d['source_url']}" target="_blank" class="btn approve">Open to reply</a>
          </div>
        </div>
        """

    html = f"""<!DOCTYPE html>
<html lang="en">
<head>
<meta charset="UTF-8">
<title>Brand Agent Drafts — {today}</title>
<style>
  body {{ font-family: system-ui, sans-serif; max-width: 860px; margin: 2rem auto; padding: 0 1rem; color: #1c1c1a; background: #f8f4ef; }}
  h1   {{ font-size: 1.4rem; margin-bottom: 0.3rem; }}
  .sub {{ color: #666; font-size: 0.9rem; margin-bottom: 2rem; }}
  .card {{ background: #fff; border: 1px solid #e0dbd4; border-radius: 8px; padding: 1.2rem 1.5rem; margin-bottom: 1.2rem; }}
  .meta {{ display: flex; gap: 1rem; font-size: 0.8rem; color: #888; margin-bottom: 0.8rem; }}
  .num  {{ font-weight: 700; color: #333; }}
  .original {{ background: #f5f5f5; border-left: 3px solid #b8daea; padding: 0.6rem 0.8rem; font-size: 0.88rem; margin-bottom: 0.8rem; border-radius: 0 4px 4px 0; }}
  .draft    {{ background: #fffbf0; border-left: 3px solid #c4a882; padding: 0.6rem 0.8rem; font-size: 0.9rem; margin-bottom: 0.8rem; border-radius: 0 4px 4px 0; line-height: 1.6; }}
  .btn      {{ display: inline-block; padding: 0.4rem 1rem; font-size: 0.8rem; border-radius: 4px; text-decoration: none; }}
  .approve  {{ background: #1c1c1a; color: #fff; }}
  .approve:hover {{ background: #4a93b8; }}
</style>
</head>
<body>
  <h1>Brand Agent — Daily Draft Review</h1>
  <p class="sub">{today} &nbsp;|&nbsp; {len(drafts)} draft(s) ready for review &nbsp;|&nbsp; Nothing has been posted.</p>
  {rows if rows else '<p>No drafts generated this run.</p>'}
</body>
</html>"""

    with open(out_path, "w", encoding="utf-8") as f:
        f.write(html)


# ---------------------------------------------------------------------------
# Main run
# ---------------------------------------------------------------------------

def run():
    print("=" * 60)
    print("Know Your Brand — Brand Awareness Agent")
    print(f"Run started: {datetime.datetime.now().isoformat()}")
    print("=" * 60)

    all_signals: list[dict] = []

    # Collect signals from both platforms
    for signal in CONFUSION_SIGNALS:
        print(f"\n[search] {signal!r}")

        twitter_results = search_twitter(signal, max_results=5)
        quora_results   = search_quora(signal, max_results=3)

        all_signals.extend(twitter_results)
        all_signals.extend(quora_results)

        time.sleep(0.5)  # be polite to APIs

    print(f"\n[collect] Total raw signals: {len(all_signals)}")

    # Score and sort
    for item in all_signals:
        item["relevance_score"] = score_relevance(item)

    all_signals.sort(key=lambda x: x["relevance_score"], reverse=True)

    # Deduplicate by URL
    seen_urls: set[str] = set()
    unique_signals: list[dict] = []
    for item in all_signals:
        url = item.get("url", "")
        if url and url not in seen_urls:
            seen_urls.add(url)
            unique_signals.append(item)

    # Take top N
    top_signals = unique_signals[:MAX_DRAFTS_PER_RUN]
    print(f"[filter] Top signals selected: {len(top_signals)}")

    # Generate drafts
    drafts: list[dict] = []
    for item in top_signals:
        print(f"  drafting for [{item['platform']}] score={item['relevance_score']} — {(item.get('text') or item.get('title') or '')[:60]}...")
        draft_text = generate_draft(item)
        if not draft_text:
            continue
        drafts.append({
            "platform":        item["platform"],
            "source_url":      item.get("url", ""),
            "original_text":   item.get("text") or item.get("snippet") or item.get("title") or "",
            "draft":           draft_text,
            "relevance_score": item["relevance_score"],
            "generated_at":    datetime.datetime.now().isoformat(),
            "posted":          False,  # always starts False — human review required
        })

    print(f"\n[drafts] {len(drafts)} draft(s) generated")

    if not drafts:
        print("[done] No drafts to save. Check your API keys and signals.")
        return

    # Save JSON
    json_path = save_drafts(drafts)
    print(f"[save] JSON drafts saved to: {json_path}")

    # Save HTML report
    html_path = json_path.with_suffix(".html")
    generate_html_report(drafts, html_path)
    print(f"[save] HTML report saved to: {html_path}")

    # Print summary to terminal
    print("\n" + "=" * 60)
    print(f"DRAFT REVIEW SUMMARY — {datetime.date.today().isoformat()}")
    print("=" * 60)
    for i, d in enumerate(drafts, 1):
        print(f"\n#{i} [{d['platform'].upper()}] score={d['relevance_score']}/10")
        print(f"    Source: {d['source_url']}")
        original_short = d['original_text'][:100].replace('\n', ' ')
        print(f"    Original: {original_short}...")
        print(f"    Draft:\n{textwrap.indent(d['draft'], '      ')}")

    print("\n" + "=" * 60)
    print(f"Open {html_path} in your browser to review all drafts.")
    print("Nothing has been posted. All posting is manual.")
    print("=" * 60)


if __name__ == "__main__":
    run()
