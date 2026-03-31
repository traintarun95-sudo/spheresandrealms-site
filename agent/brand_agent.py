"""
Know Your Brand — Brand Awareness Agent v4
==========================================
Stack:
  - Apify actors for fresh thread discovery (Reddit, Quora, LinkedIn, Twitter, YouTube)
  - 12-hour hard recency filter
  - Claude Haiku for drafting
  - Telegram for delivery
  - Railway cron for scheduling

Flow:
  1. Find brands in today's news
  2. For each brand — search Reddit, Quora, Twitter for fresh threads
  3. Hard 12-hour filter — drop anything older
  4. Draft response specific to each thread
  5. Send to Telegram: exact URL + draft
  6. You post manually
"""

import os
import json
import time
import datetime
import textwrap
import urllib.parse
from pathlib import Path
from typing import Optional

import requests
from dotenv import load_dotenv

load_dotenv()

# ---------------------------------------------------------------------------
# Config
# ---------------------------------------------------------------------------

ANTHROPIC_API_KEY  = os.getenv("ANTHROPIC_API_KEY", "")
APIFY_API_TOKEN    = os.getenv("APIFY_API_TOKEN", "")
TELEGRAM_BOT_TOKEN = os.getenv("TELEGRAM_BOT_TOKEN", "")
TELEGRAM_CHAT_ID   = os.getenv("TELEGRAM_CHAT_ID", "")
KYB_API_URL        = os.getenv("KYB_API_URL", "https://know-your-brand-production.up.railway.app")
TAVILY_API_KEY     = os.getenv("TAVILY_API_KEY", "")  # kept for news search only

LOG_DIR   = Path(__file__).parent / "logs"
BRAND_LOG = LOG_DIR / "brand_log.json"
LOG_DIR.mkdir(parents=True, exist_ok=True)

CANONICAL_URL      = "https://www.realmofbrands.com"
MAX_BRANDS         = 5
BRAND_LOG_DAYS     = 7
RECENCY_HOURS      = 48    # hard cutoff — nothing older than this
APIFY_MEMORY_MB    = 256   # cap actor memory — forces HTTP-tier, 20x cheaper than browser
APIFY_DAILY_BUDGET = 0.50  # stop the run if daily Apify spend exceeds this (USD)

NEWS_SOURCES = [
    "techcrunch.com", "bloomberg.com", "reuters.com", "ft.com",
    "gulfnews.com", "arabianbusiness.com", "forbesmiddleeast.com",
    "wamda.com", "techinasia.com", "thebridge.jp",
    "bloomberglinea.com", "rappler.com", "businessdayng.com",
]

NEWS_QUERIES = [
    "brand raises funding round",
    "brand acquisition deal",
    "brand enters market launch",
    "brand rebrand new identity",
    "startup brand series funding",
    "brand expansion Middle East",
    "D2C brand growth",
    "new brand launch consumer",
]


# ---------------------------------------------------------------------------
# Brand deduplication log
# ---------------------------------------------------------------------------

def load_brand_log() -> dict:
    if not BRAND_LOG.exists():
        return {}
    try:
        with open(BRAND_LOG) as f:
            return json.load(f)
    except Exception:
        return {}


def save_brand_log(log: dict) -> None:
    with open(BRAND_LOG, "w") as f:
        json.dump(log, f, indent=2)


def brand_recently_drafted(brand: str, log: dict) -> bool:
    brand_key = brand.lower().strip()
    if brand_key not in log:
        return False
    last_date = datetime.date.fromisoformat(log[brand_key])
    return (datetime.date.today() - last_date).days < BRAND_LOG_DAYS


def mark_brand_drafted(brand: str, log: dict) -> dict:
    log[brand.lower().strip()] = datetime.date.today().isoformat()
    return log


# ---------------------------------------------------------------------------
# Recency filter — hard 12-hour cutoff
# ---------------------------------------------------------------------------

def is_fresh(published: str) -> bool:
    """Returns True if published date is within RECENCY_HOURS."""
    if not published:
        return True  # no date = assume fresh, let it through
    for fmt in ["%Y-%m-%dT%H:%M:%S.%fZ", "%Y-%m-%dT%H:%M:%SZ", "%Y-%m-%d"]:
        try:
            pub_dt = datetime.datetime.strptime(published[:19], fmt[:len(published[:19])])
            age = datetime.datetime.utcnow() - pub_dt
            return age.total_seconds() < RECENCY_HOURS * 3600
        except Exception:
            continue
    return True


# ---------------------------------------------------------------------------
# Tavily — news search only
# ---------------------------------------------------------------------------

def tavily_search(query: str, domains: list = None, days: int = 1, max_results: int = 3) -> list[dict]:
    if not TAVILY_API_KEY:
        return []
    payload = {
        "api_key": TAVILY_API_KEY,
        "query": query,
        "search_depth": "basic",
        "max_results": max_results,
        "days": days,
    }
    if domains:
        payload["include_domains"] = domains
    try:
        resp = requests.post("https://api.tavily.com/search", json=payload, timeout=15)
        resp.raise_for_status()
        return [
            {
                "title":     r.get("title", ""),
                "url":       r.get("url", ""),
                "snippet":   r.get("content", ""),
                "published": r.get("published_date", ""),
            }
            for r in resp.json().get("results", [])
        ]
    except Exception as e:
        print(f"  [tavily] Failed: {e}")
        return []


def search_brand_news() -> list[dict]:
    all_stories = []
    seen: set = set()
    for query in NEWS_QUERIES:
        for r in tavily_search(query, domains=NEWS_SOURCES, days=1, max_results=3):
            if r["url"] not in seen:
                seen.add(r["url"])
                all_stories.append(r)
        time.sleep(0.3)
    return all_stories


# ---------------------------------------------------------------------------
# Apify — thread discovery
# ---------------------------------------------------------------------------

def apify_daily_spend() -> float:
    """Return today's Apify usage in USD. Returns 0.0 on any error."""
    if not APIFY_API_TOKEN:
        return 0.0
    try:
        r = requests.get(
            "https://api.apify.com/v2/users/me",
            headers={"Authorization": f"Bearer {APIFY_API_TOKEN}"},
            timeout=10,
        )
        data = r.json().get("data", {})
        # monthlyUsage is in USD cents — Apify returns it in the plan block
        # The real-time spend is in usageCycleUsageUsd if available
        spend = data.get("usageCycleUsageUsd", data.get("monthlyUsage", 0))
        return float(spend) if spend else 0.0
    except Exception:
        return 0.0


def apify_run_actor(actor_id: str, input_data: dict, timeout_secs: int = 90) -> list[dict]:
    """Run an Apify actor and return results."""
    if not APIFY_API_TOKEN:
        return []
    try:
        # Start the actor run — body IS the input, timeout + memory are query params
        # memory=256MB forces HTTP/Cheerio tier — ~20x cheaper than browser actors
        run_resp = requests.post(
            f"https://api.apify.com/v2/acts/{actor_id}/runs",
            params={"timeout": timeout_secs, "memory": APIFY_MEMORY_MB},
            headers={
                "Authorization": f"Bearer {APIFY_API_TOKEN}",
                "Content-Type": "application/json",
            },
            json=input_data,
            timeout=30,
        )
        run_resp.raise_for_status()
        run_id = run_resp.json()["data"]["id"]

        # Poll until finished
        for _ in range(30):
            time.sleep(3)
            status_resp = requests.get(
                f"https://api.apify.com/v2/actor-runs/{run_id}",
                headers={"Authorization": f"Bearer {APIFY_API_TOKEN}"},
                timeout=15,
            )
            status = status_resp.json()["data"]["status"]
            if status in ("SUCCEEDED", "FAILED", "ABORTED", "TIMED-OUT"):
                break

        if status != "SUCCEEDED":
            print(f"  [apify] Actor {actor_id} finished with status: {status}")
            return []

        # Get results
        dataset_id = status_resp.json()["data"]["defaultDatasetId"]
        results_resp = requests.get(
            f"https://api.apify.com/v2/datasets/{dataset_id}/items",
            headers={"Authorization": f"Bearer {APIFY_API_TOKEN}"},
            params={"limit": 10},
            timeout=15,
        )
        return results_resp.json()

    except Exception as e:
        print(f"  [apify] Failed for {actor_id}: {e}")
        return []


def search_reddit(brand: str, news_title: str) -> list[dict]:
    """Search Reddit for fresh threads about this brand using Apify."""
    results = apify_run_actor(
        "oAuCIx3ItNrs2okjQ",  # reddit-scraper-lite
        {
            "searches": [
                f"{brand}",
                f"what is {brand}",
                f"is {brand} legit",
            ],
            "type": "posts",
            "sort": "new",
            "maxItems": 10,
        }
    )

    threads = []
    for r in results:
        published = r.get("createdAt", r.get("created_utc", ""))
        if isinstance(published, (int, float)):
            published = datetime.datetime.utcfromtimestamp(published).isoformat()
        url = r.get("url", r.get("permalink", ""))
        if url and not url.startswith("http"):
            url = f"https://www.reddit.com{url}"
        title = r.get("title", "")
        if not url or not title:
            continue
        threads.append({
            "title":     title,
            "url":       url,
            "snippet":   r.get("selftext", r.get("body", ""))[:200],
            "published": published,
            "platform":  "reddit",
            "score":     r.get("score", r.get("ups", 0)),
            "comments":  r.get("numComments", r.get("num_comments", 0)),
        })

    # Filter by recency and sort by engagement
    fresh = [t for t in threads if is_fresh(t["published"])]
    fresh.sort(key=lambda x: x.get("comments", 0), reverse=True)
    return fresh[:5]


def search_quora(brand: str) -> list[dict]:
    """Search Quora via Google site: operator using Apify Google Search actor."""
    year = datetime.date.today().year
    results = apify_run_actor(
        "nFJndFXA5zjCTuudP",  # google-search-scraper
        {
            "queries": [f'site:quora.com "{brand}" {year}'],
            "maxPagesPerQuery": 1,
            "resultsPerPage": 5,
        }
    )

    threads = []
    for r in results:
        organic = r.get("organicResults", [])
        for item in organic:
            url = item.get("url", "")
            title = item.get("title", "")
            if "quora.com" not in url or not title:
                continue
            threads.append({
                "title":     title,
                "url":       url,
                "snippet":   item.get("description", ""),
                "published": "",
                "platform":  "quora",
                "score":     0,
                "comments":  0,
            })

    return threads[:5]


def search_linkedin(brand: str) -> list[dict]:
    """Search LinkedIn public company posts using Apify."""
    results = apify_run_actor(
        "kfiWbq3boy3dWKbiL",  # linkedin-post-search-scraper
        {
            "keywords": brand,
            "maxResults": 5,
        }
    )

    threads = []
    for r in results:
        url = r.get("postUrl", r.get("url", ""))
        title = r.get("text", r.get("content", ""))[:100]
        published = r.get("postedAt", r.get("date", ""))
        if not url or not title:
            continue
        threads.append({
            "title":     title,
            "url":       url,
            "snippet":   r.get("text", "")[:200],
            "published": published,
            "platform":  "linkedin",
            "score":     0,
            "comments":  r.get("commentsCount", 0),
        })

    fresh = [t for t in threads if is_fresh(t["published"])]
    return fresh[:5]


def search_twitter(brand: str) -> list[dict]:
    """Search Twitter for fresh conversations about this brand using Apify."""
    results = apify_run_actor(
        "nfp1fpt5gUlBwPcor",  # twitter-scraper-lite
        {
            "searchTerms": [
                brand,
                f"what is {brand}",
                f"anyone use {brand}",
            ],
            "maxItems":  10,
            "sort":      "Latest",
        }
    )

    threads = []
    for r in results:
        url = r.get("url", r.get("tweetUrl", ""))
        text = r.get("text", r.get("full_text", r.get("content", "")))
        title = text[:100] if text else ""
        published = r.get("createdAt", r.get("created_at", ""))
        if isinstance(published, (int, float)):
            published = datetime.datetime.utcfromtimestamp(published).isoformat()
        if not url or not title:
            continue
        threads.append({
            "title":     title,
            "url":       url,
            "snippet":   text[:200] if text else "",
            "published": published,
            "platform":  "twitter",
            "score":     r.get("likeCount", r.get("favorite_count", 0)),
            "comments":  r.get("replyCount", r.get("reply_count", 0)),
        })

    fresh = [t for t in threads if is_fresh(t["published"])]
    fresh.sort(key=lambda x: x.get("comments", 0), reverse=True)
    return fresh[:5]


def search_youtube(brand: str) -> list[dict]:
    """Find YouTube videos discussing this brand via Google site: search."""
    year = datetime.date.today().year
    results = apify_run_actor(
        "nFJndFXA5zjCTuudP",  # google-search-scraper (reused)
        {
            "queries":          [f'site:youtube.com "{brand}" {year}'],
            "maxPagesPerQuery": 1,
            "resultsPerPage":   5,
        }
    )

    threads = []
    for r in results:
        organic = r.get("organicResults", [])
        for item in organic:
            url = item.get("url", "")
            title = item.get("title", "")
            if "youtube.com/watch" not in url or not title:
                continue
            threads.append({
                "title":     title,
                "url":       url,
                "snippet":   item.get("description", ""),
                "published": "",
                "platform":  "youtube",
                "score":     0,
                "comments":  0,
            })

    return threads[:3]


# ---------------------------------------------------------------------------
# Brand extraction from news
# ---------------------------------------------------------------------------

def extract_brand_from_story(story: dict) -> Optional[str]:
    if not ANTHROPIC_API_KEY:
        return None
    prompt = (
        "Extract the PRIMARY brand name from this news story.\n"
        "Return ONLY the brand name — nothing else. No explanation.\n"
        "If no clear brand name exists, return: NONE\n\n"
        f"Title: {story['title']}\n"
        f"Snippet: {story['snippet']}"
    )
    try:
        resp = requests.post(
            "https://api.anthropic.com/v1/messages",
            headers={
                "x-api-key": ANTHROPIC_API_KEY,
                "anthropic-version": "2023-06-01",
                "content-type": "application/json",
            },
            json={
                "model":      "claude-haiku-4-5-20251001",
                "max_tokens": 30,
                "messages":   [{"role": "user", "content": prompt}],
            },
            timeout=15,
        )
        brand = resp.json()["content"][0]["text"].strip()
        return None if brand.upper() == "NONE" else brand
    except Exception:
        return None


# ---------------------------------------------------------------------------
# KYB API
# ---------------------------------------------------------------------------

def get_brand_card(brand: str) -> Optional[dict]:
    try:
        resp = requests.post(
            f"{KYB_API_URL}/api/brand",
            json={"brand": brand},
            headers={"X-Agent": "brand-agent"},
            timeout=20,
        )
        return resp.json() if resp.status_code == 200 else None
    except Exception as e:
        print(f"  [card] Failed for {brand}: {e}")
        return None


def brand_card_url(brand: str) -> str:
    return f"{CANONICAL_URL}/search.html?brand={urllib.parse.quote_plus(brand)}"


# ---------------------------------------------------------------------------
# Draft generation
# ---------------------------------------------------------------------------

def generate_draft(brand: str, thread: dict, card: Optional[dict]) -> str:
    card_url = brand_card_url(brand)

    if not ANTHROPIC_API_KEY:
        return f"Here is a quick snapshot of what {brand} is.\nFor the full card — {card_url}"

    what_is = card.get("what_is", "") if card and card.get("type") == "card" else ""
    platform = thread.get("platform", "quora")

    prompt = textwrap.dedent(f"""
        Thread: {thread['title']}
        Platform: {platform}
        Brand: {brand}
        What {brand} is: {what_is}

        Write one sentence — specific to this thread, for the person who just heard this brand name and doesn't know what it is.
        Plain, factual, no opinion. Orient them simply.
        Then on the next line write exactly: For the full card — {card_url}

        Two lines total. Nothing else.
    """).strip()

    try:
        resp = requests.post(
            "https://api.anthropic.com/v1/messages",
            headers={
                "x-api-key": ANTHROPIC_API_KEY,
                "anthropic-version": "2023-06-01",
                "content-type": "application/json",
            },
            json={
                "model":      "claude-haiku-4-5-20251001",
                "max_tokens": 100,
                "messages":   [{"role": "user", "content": prompt}],
            },
            timeout=20,
        )
        return resp.json()["content"][0]["text"].strip()
    except Exception:
        return f"Here is a quick snapshot of what {brand} is.\nFor the full card — {card_url}"


# ---------------------------------------------------------------------------
# Telegram
# ---------------------------------------------------------------------------

def send_telegram(message: str) -> bool:
    if not TELEGRAM_BOT_TOKEN or not TELEGRAM_CHAT_ID:
        return False
    try:
        resp = requests.post(
            f"https://api.telegram.org/bot{TELEGRAM_BOT_TOKEN}/sendMessage",
            json={
                "chat_id":                  TELEGRAM_CHAT_ID,
                "text":                     message,
                "parse_mode":               "HTML",
                "disable_web_page_preview": True,
            },
            timeout=10,
        )
        return resp.status_code == 200
    except Exception:
        return False


def notify_result(brand: str, news_title: str, thread: dict, draft: str) -> None:
    platform = thread.get("platform", "").upper()
    comments = thread.get("comments", 0)
    published = thread.get("published", "")[:16].replace("T", " ")
    engagement = f"{comments} comments" if comments else ""
    meta = " · ".join(filter(None, [published, engagement]))

    message = (
        f"<b>{brand}</b>\n"
        f"📰 {news_title}\n"
        f"───────────────\n"
        f"<b>{platform}</b>  {meta}\n"
        f"{thread['url']}\n"
        f"───────────────\n"
        f"{draft}"
    )

    sent = send_telegram(message)
    if not sent:
        print(f"\n  [{platform}] {thread['url']}")
        print(f"  {draft}")


# ---------------------------------------------------------------------------
# Main run
# ---------------------------------------------------------------------------

def run():
    print("=" * 60)
    print("Know Your Brand — Brand Awareness Agent v5 (Reddit · Quora · LinkedIn · Twitter · YouTube)")
    print(f"Started: {datetime.datetime.now().isoformat()}")
    print(f"Recency filter: {RECENCY_HOURS} hours")
    print("=" * 60)

    # Spending guard — stop before burning the free tier
    spend = apify_daily_spend()
    print(f"Apify usage this cycle: ${spend:.3f} / ${APIFY_DAILY_BUDGET:.2f} daily cap")
    if spend >= APIFY_DAILY_BUDGET:
        print(f"[abort] Apify spend ${spend:.3f} >= daily cap ${APIFY_DAILY_BUDGET:.2f}. Stopping.")
        return

    brand_log = load_brand_log()

    print("\n[news] Searching brand news...")
    stories = search_brand_news()
    print(f"  {len(stories)} stories found")

    brands_processed = 0

    for story in stories:
        if brands_processed >= MAX_BRANDS:
            break

        brand = extract_brand_from_story(story)
        if not brand:
            continue

        if brand_recently_drafted(brand, brand_log):
            print(f"  [skip] {brand} — drafted within {BRAND_LOG_DAYS} days")
            continue

        # Validate card exists
        card = get_brand_card(brand)
        if not card or card.get("type") != "card":
            print(f"  [skip] {brand} — no clean card")
            continue

        print(f"\n[brand] {brand}")
        print(f"  News: {story['title'][:80]}")

        # Search all platforms
        print("  [reddit] Searching...")
        reddit_threads = search_reddit(brand, story["title"])
        print(f"    {len(reddit_threads)} fresh threads")

        print("  [quora] Searching...")
        quora_threads = search_quora(brand)
        print(f"    {len(quora_threads)} threads")

        print("  [linkedin] Searching...")
        linkedin_threads = search_linkedin(brand)
        print(f"    {len(linkedin_threads)} fresh threads")

        print("  [twitter] Searching...")
        twitter_threads = search_twitter(brand)
        print(f"    {len(twitter_threads)} fresh threads")

        print("  [youtube] Searching...")
        youtube_threads = search_youtube(brand)
        print(f"    {len(youtube_threads)} videos found")

        all_threads = reddit_threads + quora_threads + linkedin_threads + twitter_threads + youtube_threads

        if not all_threads:
            print(f"  No fresh threads found for {brand} — skipping")
            continue

        sent = 0
        for thread in all_threads:
            draft = generate_draft(brand, thread, card)
            notify_result(brand, story["title"], thread, draft)
            print(f"  → {thread['platform'].upper()} — {thread['url'][:60]}...")
            sent += 1
            time.sleep(0.3)

        print(f"  {sent} results sent to Telegram")
        brand_log = mark_brand_drafted(brand, brand_log)
        brands_processed += 1

    save_brand_log(brand_log)
    print(f"\n[done] {brands_processed} brands processed.")


if __name__ == "__main__":
    run()
