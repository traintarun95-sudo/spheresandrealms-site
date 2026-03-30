"""
Know Your Brand — Brand Awareness Agent v3
==========================================
Flow:
  1. Find brand news from curated sources
  2. For each brand — run two search streams:
     Stream 1: 5 live discussions about that brand on Quora/LinkedIn/Reddit
     Stream 2: 5 live problem-space threads KYB directly answers
  3. Draft a response for each of the 10 threads
  4. Send to Telegram: exact URL + draft

Nothing posts automatically. You open the URL, paste, done.
"""

import os
import json
import time
import datetime
import textwrap
from pathlib import Path
from typing import Optional

import urllib.parse
import requests
from dotenv import load_dotenv

load_dotenv()

# ---------------------------------------------------------------------------
# Config
# ---------------------------------------------------------------------------

ANTHROPIC_API_KEY  = os.getenv("ANTHROPIC_API_KEY", "")
TAVILY_API_KEY     = os.getenv("TAVILY_API_KEY", "")
BRAVE_API_KEY      = os.getenv("BRAVE_API_KEY", "")
TELEGRAM_BOT_TOKEN = os.getenv("TELEGRAM_BOT_TOKEN", "")
TELEGRAM_CHAT_ID   = os.getenv("TELEGRAM_CHAT_ID", "")
KYB_API_URL        = os.getenv("KYB_API_URL", "https://know-your-brand-production.up.railway.app")

LOG_DIR   = Path(__file__).parent / "logs"
BRAND_LOG = LOG_DIR / "brand_log.json"
LOG_DIR.mkdir(parents=True, exist_ok=True)

CANONICAL_URL      = "https://www.realmofbrands.com"

def brand_card_url(brand: str) -> str:
    """Direct link to the brand's card on KYB."""
    return f"{CANONICAL_URL}/search.html?brand={urllib.parse.quote_plus(brand)}"

# URL patterns that are pages, not conversations — filter these out
MAX_BRANDS         = 5      # max brands to process per run
BRAND_LOG_DAYS     = 7      # days before same brand can repeat

DISCUSSION_PLATFORMS = ["quora.com", "reddit.com", "linkedin.com"]

# Block these URL patterns — pages and profiles, not discussions
BLOCKED_URL_PATTERNS = [
    "linkedin.com/company/",
    "linkedin.com/in/",
    "linkedin.com/showcase/",
    "linkedin.com/pulse/",
    "linkedin.com/news/",
    "/jobs", "/life",
    "reddit.com/r/",  # subreddit homepage — not a thread
]

def is_discussion_url(url: str) -> bool:
    """Return True only if URL is an actual discussion thread, not a page or profile."""
    url_lower = url.lower()
    for pattern in BLOCKED_URL_PATTERNS:
        if pattern in url_lower:
            return False
    # Reddit must be a specific post (contains /comments/)
    if "reddit.com" in url_lower and "/comments/" not in url_lower:
        return False
    # LinkedIn must be a post (contains /posts/)
    if "linkedin.com" in url_lower and "/posts/" not in url_lower:
        return False
    return True

NEWS_SOURCES = [
    # Global
    "techcrunch.com",
    "bloomberg.com",
    "reuters.com",
    "ft.com",
    # Gulf
    "gulfnews.com",
    "arabianbusiness.com",
    "forbesmiddleeast.com",
    "wamda.com",
    # Asia/emerging
    "techinasia.com",
    "thebridge.jp",
    "bloomberglinea.com",
    "rappler.com",
    "businessdayng.com",
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

NEGATIVE_SIGNALS = [
    "fraud", "scam", "shutdown", "bankrupt", "lawsuit", "collapse",
    "fake", "ponzi", "arrested", "raided", "fined", "scandal",
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
# Brave Search — for finding discussion threads (freshness-aware)
# ---------------------------------------------------------------------------

def brave_search(
    query: str,
    freshness: str = "pw",   # pw=past week, pm=past month
    max_results: int = 5,
) -> list[dict]:
    """Search using Brave API. Returns discussion-quality results."""
    if not BRAVE_API_KEY:
        return []
    try:
        resp = requests.get(
            "https://api.search.brave.com/res/v1/web/search",
            headers={
                "Accept": "application/json",
                "Accept-Encoding": "gzip",
                "X-Subscription-Token": BRAVE_API_KEY,
            },
            params={
                "q":         query,
                "count":     max_results,
                "freshness": freshness,
                "search_lang": "en",
            },
            timeout=15,
        )
        resp.raise_for_status()
        results = []
        for r in resp.json().get("web", {}).get("results", []):
            results.append({
                "title":     r.get("title", ""),
                "url":       r.get("url", ""),
                "snippet":   r.get("description", ""),
                "score":     1.0,
                "published": r.get("page_age", ""),
            })
        return results
    except Exception as e:
        print(f"  [brave] Failed ({query[:40]}): {e}")
        return []


# ---------------------------------------------------------------------------
# Tavily search — used for news only
# ---------------------------------------------------------------------------

def tavily_search(
    query: str,
    domains: list = None,
    days: int = 7,
    max_results: int = 5,
) -> list[dict]:
    if not TAVILY_API_KEY:
        return []
    payload = {
        "api_key":      TAVILY_API_KEY,
        "query":        query,
        "search_depth": "basic",
        "max_results":  max_results,
        "days":         days,
    }
    if domains:
        payload["include_domains"] = domains
    try:
        resp = requests.post("https://api.tavily.com/search", json=payload, timeout=15)
        resp.raise_for_status()
        results = []
        for r in resp.json().get("results", []):
            published = r.get("published_date", "")
            # Hard date filter — drop anything older than 30 days
            if published:
                try:
                    pub_date = datetime.datetime.strptime(published[:10], "%Y-%m-%d").date()
                    age_days = (datetime.date.today() - pub_date).days
                    if age_days > 30:
                        continue
                except Exception:
                    pass
            results.append({
                "title":     r.get("title", ""),
                "url":       r.get("url", ""),
                "snippet":   r.get("content", ""),
                "score":     r.get("score", 0),
                "published": published,
            })
        return results
    except Exception as e:
        print(f"  [tavily] Failed ({query[:40]}): {e}")
        return []


# ---------------------------------------------------------------------------
# Step 1: Find brand news
# ---------------------------------------------------------------------------

def search_brand_news() -> list[dict]:
    all_stories = []
    seen_urls: set = set()
    for query in NEWS_QUERIES:
        for r in tavily_search(query, domains=NEWS_SOURCES, days=1, max_results=3):
            if r["url"] not in seen_urls:
                seen_urls.add(r["url"])
                all_stories.append(r)
        time.sleep(0.3)
    return all_stories


# ---------------------------------------------------------------------------
# Brand extraction from news story
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
# KYB API — brand card retrieval
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


# ---------------------------------------------------------------------------
# Stream 1: Brand discussions — live threads about this specific brand
# ---------------------------------------------------------------------------

MIN_SCORE = 0.7   # minimum Tavily relevance score — only high confidence threads

def find_brand_discussions(brand: str, news_title: str) -> list[dict]:
    # Use site-specific queries to force actual discussion posts, not pages
    year = datetime.date.today().year
    queries = [
        f'site:linkedin.com/posts "{brand}" {year}',
        f'site:linkedin.com/posts "{brand}"',
        f'site:quora.com "{brand}"',
        f'site:quora.com "what is {brand}"',
        f'site:quora.com "{brand}" {year}',
    ]
    results = []
    seen: set = set()

    for q in queries:
        for r in tavily_search(q, days=30, max_results=3):
            url = r["url"]
            if url not in seen and is_discussion_url(url) and r.get("score", 0) >= MIN_SCORE:
                seen.add(url)
                r["stream"] = "brand"
                results.append(r)
        if len(results) >= 5:
            break
        time.sleep(0.2)

    results.sort(key=lambda x: x.get("score", 0), reverse=True)
    return results[:5]


# ---------------------------------------------------------------------------
# Stream 2: Problem-space threads — threads KYB directly answers
# ---------------------------------------------------------------------------

def derive_problem_queries(brand: str, card: Optional[dict]) -> list[str]:
    """Use Claude Haiku to derive problem-space search queries from the brand card."""
    if not ANTHROPIC_API_KEY or not card:
        return [
            "how to evaluate a brand before buying",
            "brand legitimacy check",
            "how to research a company",
        ]

    card_summary = (
        f"Brand: {brand}\n"
        f"What it is: {card.get('what_is', '')}\n"
        f"Sells: {card.get('sells', '')}\n"
        f"For: {card.get('for', '')}\n"
        f"Position: {card.get('position', '')}\n"
    )

    prompt = textwrap.dedent(f"""
        Given this brand card, generate 5 search queries to find live Quora, Reddit, or LinkedIn threads
        where people are asking questions that a brand intelligence tool would directly answer.
        Focus on the pain: brand confusion, evaluating companies, purchase decisions, industry questions.
        Do NOT include the brand name in the queries — these are problem-space threads, not brand-specific.
        Include geographic context where relevant — Gulf, UAE, Dubai, India, Southeast Asia, Africa.
        Prioritise threads where someone genuinely doesn't know and is asking — not expert debates.

        {card_summary}

        Return exactly 5 queries, one per line. No numbering. No explanation.
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
                "max_tokens": 200,
                "messages":   [{"role": "user", "content": prompt}],
            },
            timeout=15,
        )
        text = resp.json()["content"][0]["text"].strip()
        return [q.strip() for q in text.split("\n") if q.strip()][:5]
    except Exception:
        return [
            "how to evaluate a brand before buying",
            "brand legitimacy check",
            "how to research a company",
        ]


def find_resonance_threads(brand: str, card: Optional[dict]) -> list[dict]:
    year = datetime.date.today().year
    base_queries = derive_problem_queries(brand, card)
    # Force site-specific queries for actual discussion posts
    queries = []
    for q in base_queries:
        queries.append(f'site:quora.com "{q}"')
        queries.append(f'site:linkedin.com/posts "{q}" {year}')

    results = []
    seen: set = set()

    for q in queries:
        for r in tavily_search(q, days=30, max_results=2):
            url = r["url"]
            if url not in seen and is_discussion_url(url) and r.get("score", 0) >= MIN_SCORE:
                seen.add(url)
                r["stream"] = "resonance"
                results.append(r)
        if len(results) >= 5:
            break
        time.sleep(0.2)

    results.sort(key=lambda x: x.get("score", 0), reverse=True)
    return results[:5]


# ---------------------------------------------------------------------------
# Draft generation — specific to each thread
# ---------------------------------------------------------------------------

def platform_from_url(url: str) -> str:
    if "quora.com" in url:   return "quora"
    if "reddit.com" in url:  return "reddit"
    if "linkedin.com" in url: return "linkedin"
    return "linkedin"


def generate_draft(
    brand: str,
    news_title: str,
    thread: dict,
    card: Optional[dict],
) -> str:
    card_url = brand_card_url(brand)

    if not ANTHROPIC_API_KEY:
        return f"Here is a quick snapshot of what {brand} is.\nFor the full card — {card_url}"

    what_is = card.get("what_is", "") if card and card.get("type") == "card" else ""

    prompt = textwrap.dedent(f"""
        Thread: {thread['title']}
        Brand: {brand}
        What {brand} is: {what_is}
        News: {news_title}

        Write one sentence — specific to this thread, for the person who doesn't know this brand yet.
        Orient them: what is this brand, why does this thread matter to them.
        Not analysis. Not opinion. Just a clean, specific opening line.
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
# Telegram notifications
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
    platform     = platform_from_url(thread["url"]).upper()
    stream_label = "Brand thread" if thread.get("stream") == "brand" else "Problem-space thread"

    score = thread.get("score", 0)
    published = thread.get("published", "")
    meta = f"score {score:.2f}" + (f" · {published[:10]}" if published else "")

    message = (
        f"<b>{brand}</b>  —  {stream_label}\n"
        f"📰 {news_title}\n"
        f"───────────────\n"
        f"<b>{platform}</b>  {meta}\n"
        f"{thread['url']}\n"
        f"───────────────\n"
        f"{draft}"
    )

    sent = send_telegram(message)
    if not sent:
        # Fallback: print to terminal if Telegram not configured
        print(f"\n  [{platform}] {thread['url']}")
        print(f"  {draft}")


# ---------------------------------------------------------------------------
# Main run
# ---------------------------------------------------------------------------

def run():
    print("=" * 60)
    print("Know Your Brand — Brand Awareness Agent v3")
    print(f"Started: {datetime.datetime.now().isoformat()}")
    print("=" * 60)

    brand_log = load_brand_log()

    # Step 1: Find brand news
    print("\n[news] Searching brand news...")
    stories = search_brand_news()
    print(f"  {len(stories)} stories found")

    brands_processed = 0

    for story in stories:
        if brands_processed >= MAX_BRANDS:
            break

        # Extract brand name
        brand = extract_brand_from_story(story)
        if not brand:
            continue

        if brand_recently_drafted(brand, brand_log):
            print(f"  [skip] {brand} — drafted within {BRAND_LOG_DAYS} days")
            continue

        print(f"\n[brand] {brand}")
        print(f"  News: {story['title'][:80]}")

        # Get brand card — skip if not a clean card (disambig or unknown)
        card = get_brand_card(brand)
        if not card or card.get("type") != "card":
            print(f"  [skip] {brand} — no clean card (type: {card.get('type') if card else 'none'})")
            continue
        print(f"  Card: retrieved")

        # Stream 1: brand discussions anchored to today's news
        print("  [stream 1] Finding brand discussions...")
        brand_threads = find_brand_discussions(brand, story["title"])
        print(f"    {len(brand_threads)} threads")

        # Stream 2: problem-space threads
        print("  [stream 2] Finding problem-space threads...")
        resonance_threads = find_resonance_threads(brand, card)
        print(f"    {len(resonance_threads)} threads")

        # Draft and send each thread
        sent = 0
        for thread in brand_threads + resonance_threads:
            draft = generate_draft(brand, story["title"], thread, card)
            if not draft:
                continue
            notify_result(brand, story["title"], thread, draft)
            print(f"  → {platform_from_url(thread['url']).upper()} — {thread['url'][:60]}...")
            sent += 1
            time.sleep(0.3)

        print(f"  {sent} results sent to Telegram")
        brand_log = mark_brand_drafted(brand, brand_log)
        brands_processed += 1

    save_brand_log(brand_log)

    print(f"\n[done] {brands_processed} brands processed.")
    if TELEGRAM_BOT_TOKEN:
        print("Check Telegram for thread URLs and drafts.")
    else:
        print("Add TELEGRAM_BOT_TOKEN and TELEGRAM_CHAT_ID to .env to receive drafts on Telegram.")


if __name__ == "__main__":
    run()
