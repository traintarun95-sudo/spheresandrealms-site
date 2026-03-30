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

import requests
from dotenv import load_dotenv

load_dotenv()

# ---------------------------------------------------------------------------
# Config
# ---------------------------------------------------------------------------

ANTHROPIC_API_KEY  = os.getenv("ANTHROPIC_API_KEY", "")
TAVILY_API_KEY     = os.getenv("TAVILY_API_KEY", "")
TELEGRAM_BOT_TOKEN = os.getenv("TELEGRAM_BOT_TOKEN", "")
TELEGRAM_CHAT_ID   = os.getenv("TELEGRAM_CHAT_ID", "")
KYB_API_URL        = os.getenv("KYB_API_URL", "https://know-your-brand-production.up.railway.app")

LOG_DIR   = Path(__file__).parent / "logs"
BRAND_LOG = LOG_DIR / "brand_log.json"
LOG_DIR.mkdir(parents=True, exist_ok=True)

CANONICAL_URL      = "https://www.realmofbrands.com"
MAX_BRANDS         = 5      # max brands to process per run
BRAND_LOG_DAYS     = 7      # days before same brand can repeat

DISCUSSION_PLATFORMS = ["quora.com", "reddit.com", "linkedin.com"]

NEWS_SOURCES = [
    "techcrunch.com", "economictimes.indiatimes.com", "yourstory.com",
    "livemint.com", "gulfnews.com", "arabianbusiness.com", "techinasia.com",
    "businessdayng.com", "rappler.com", "forbesmiddleeast.com",
    "wamda.com", "inc42.com", "thebridge.jp", "bloomberglinea.com",
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
# Tavily search — single function used everywhere
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
        return [
            {
                "title":   r.get("title", ""),
                "url":     r.get("url", ""),
                "snippet": r.get("content", ""),
                "score":   r.get("score", 0),
            }
            for r in resp.json().get("results", [])
        ]
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

def find_brand_discussions(brand: str) -> list[dict]:
    queries = [
        f"what is {brand}",
        f"has anyone heard of {brand}",
        f"{brand} worth it",
        f"is {brand} legit",
        f"anyone tried {brand}",
    ]
    results = []
    seen: set = set()

    for days_window in [2, 7]:
        for q in queries:
            for r in tavily_search(q, domains=DISCUSSION_PLATFORMS, days=days_window, max_results=2):
                if r["url"] not in seen:
                    seen.add(r["url"])
                    r["stream"] = "brand"
                    results.append(r)
            if len(results) >= 5:
                break
            time.sleep(0.2)
        if len(results) >= 5:
            break

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
    queries = derive_problem_queries(brand, card)
    results = []
    seen: set = set()

    for days_window in [2, 7]:
        for q in queries:
            for r in tavily_search(q, domains=DISCUSSION_PLATFORMS, days=days_window, max_results=2):
                if r["url"] not in seen:
                    seen.add(r["url"])
                    r["stream"] = "resonance"
                    results.append(r)
            if len(results) >= 5:
                break
            time.sleep(0.2)
        if len(results) >= 5:
            break

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
) -> Optional[str]:
    if not ANTHROPIC_API_KEY:
        return None

    platform = platform_from_url(thread["url"])
    stream   = thread.get("stream", "brand")

    card_context = ""
    if card and card.get("type") == "card":
        rivals = card.get("rivals", [])
        rivals_str = ", ".join(rivals) if isinstance(rivals, list) else str(rivals)
        growth = card.get("growth", {})
        growth_str = growth.get("detail", "") if isinstance(growth, dict) else str(growth)
        pricing = card.get("pricing", {})
        pricing_str = pricing.get("detail", "") if isinstance(pricing, dict) else str(pricing)
        card_context = (
            f"\nBrand card data:\n"
            f"- What it is: {card.get('what_is', '')}\n"
            f"- Sells: {card.get('sells', '')}\n"
            f"- For: {card.get('for', '')}\n"
            f"- Pricing: {pricing_str}\n"
            f"- Position: {card.get('position', '')}\n"
            f"- Rivals: {rivals_str}\n"
            f"- Underdog: {card.get('underdog', '')}\n"
            f"- Growth: {growth_str}\n"
            f"- Fun fact: {card.get('fun_fact', '')}\n"
        )

    if stream == "brand":
        context_note = f"The thread is directly about {brand}. News that surfaced it: {news_title}"
    else:
        context_note = f"The thread is about a problem space that {brand} operates in. News context: {news_title}"

    prompt = textwrap.dedent(f"""
        You are writing a response to this thread on behalf of a brand clarity tool.
        Your job is not to analyse. Your job is to clean the noise — tell people what a brand actually is.

        Thread title: {thread['title']}
        Platform: {platform}
        {context_note}
        {card_context}

        Write a response in exactly 3 sentences:
        1. One sentence saying what {brand} actually is — plain and factual, as if explaining to someone who just heard the name
        2. One sentence of simple context — what they do or who they're for, nothing more
        3. One sentence dropping {CANONICAL_URL} — for anyone in the thread who wants a clean picture

        Rules:
        - You are speaking to the person in the room who doesn't quite know yet — not the analysts
        - Even if the thread is full of experts, write for the one person wondering what this brand actually is
        - No analysis, no signals, no opinion, no trajectory
        - Drop the card and leave. That is the whole job.
        - Platform tone: quora = clear, reddit = direct, linkedin = clean
        - Never mention AI or any technology behind the product
        - No exclamation marks

        Return only the response text. Nothing else.
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
            timeout=20,
        )
        return resp.json()["content"][0]["text"].strip()
    except Exception as e:
        print(f"  [draft] Failed: {e}")
        return None


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

    message = (
        f"<b>{brand}</b>  —  {stream_label}\n"
        f"📰 {news_title}\n"
        f"───────────────\n"
        f"<b>{platform}</b>\n"
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

        # Get brand card
        card = get_brand_card(brand)
        print(f"  Card: {'retrieved' if card else 'unavailable'}")

        # Stream 1: brand discussions
        print("  [stream 1] Finding brand discussions...")
        brand_threads = find_brand_discussions(brand)
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
