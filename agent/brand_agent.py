"""
Know Your Brand — Brand Awareness Agent v2
==========================================
Core mechanism: rides existing brand news traffic.
Does not create demand. Intercepts demand that already exists.

Monitors brand news → identifies brand → checks card quality →
drafts response → saves for human review → nothing posts automatically.

Cost cap: $1.50/day. One run per day. Max 10 drafts (15 on major news days).
"""

import os
import json
import time
import datetime
import re
import textwrap
import hashlib
from pathlib import Path
from typing import Optional

import requests
from dotenv import load_dotenv

load_dotenv()

# ---------------------------------------------------------------------------
# Config
# ---------------------------------------------------------------------------

ANTHROPIC_API_KEY    = os.getenv("ANTHROPIC_API_KEY", "")
TAVILY_API_KEY       = os.getenv("TAVILY_API_KEY", "")
TWITTER_BEARER_TOKEN = os.getenv("TWITTER_BEARER_TOKEN", "")
KYB_API_URL          = os.getenv("KYB_API_URL", "https://know-your-brand-production.up.railway.app")

DRAFTS_DIR   = Path(__file__).parent / "drafts"
LOG_DIR      = Path(__file__).parent / "logs"
BRAND_LOG    = LOG_DIR / "brand_log.json"       # 7-day dedup log
DRAFTS_DIR.mkdir(parents=True, exist_ok=True)
LOG_DIR.mkdir(parents=True, exist_ok=True)

CANONICAL_URL   = "https://www.realmofbrands.com"
MAX_DRAFTS      = 10        # standard daily cap
MAX_DRAFTS_MAJOR = 15       # major news day cap (requires approval)
BRAND_LOG_DAYS  = 7         # days before same brand can be drafted again

# Voice reference — printed at top of every draft file
VOICE_REFERENCE = """
VOICE REFERENCE — read before reviewing drafts
===============================================
IS:    Precise. Calm. Direct. Names difficulty before offering solution.
IS NOT: Promotional. Urgent. Hollow. Overexplaining.

Reference sentences:
  "Brand legitimacy can be surprisingly hard to verify quickly."
  "A clean answer, not a stack of reviews."
  "People deserve clear sight."
  "You don't need to know what to ask. Just type the name."

Format: 3 sentences max. Name confusion → give answer → drop link. Stop.
Never mention AI. Never mention the engine. The structure speaks.
===============================================
"""

# ---------------------------------------------------------------------------
# Curated news sources — fixed list, no open web scraping
# ---------------------------------------------------------------------------

NEWS_SOURCES = [
    "techcrunch.com",
    "economictimes.indiatimes.com",
    "yourstory.com",
    "livemint.com",
    "gulfnews.com",
    "arabianbusiness.com",
    "techinasia.com",
    "businessdayng.com",
    "rappler.com",
    "forbesmiddleeast.com",
    "wamda.com",
    "inc42.com",
    "thebridge.jp",
    "bloomberglinea.com",
]

# News search queries — brand events worth riding
NEWS_QUERIES = [
    "brand raises funding round",
    "brand acquisition deal",
    "brand enters market launch",
    "brand rebrand new identity",
    "startup brand series funding",
    "brand expansion Middle East",
    "brand expansion India",
    "brand expansion Southeast Asia",
    "brand expansion Africa",
    "D2C brand growth",
    "brand IPO listing",
    "brand controversy response",
    "new brand launch consumer",
]

# Negative signal keywords — move to separate review pile
NEGATIVE_SIGNALS = [
    "fraud", "scam", "shutdown", "bankrupt", "lawsuit", "collapse",
    "fake", "ponzi", "arrested", "raided", "fined", "scandal",
    "recall", "toxic", "dangerous", "misleading",
]

# ---------------------------------------------------------------------------
# Brand deduplication log
# ---------------------------------------------------------------------------

def load_brand_log() -> dict:
    """Load the 7-day brand log. Returns dict of brand -> last_drafted date."""
    if not BRAND_LOG.exists():
        return {}
    try:
        with open(BRAND_LOG, "r") as f:
            return json.load(f)
    except Exception:
        return {}


def save_brand_log(log: dict) -> None:
    with open(BRAND_LOG, "w") as f:
        json.dump(log, f, indent=2)


def brand_recently_drafted(brand: str, log: dict) -> bool:
    """Returns True if brand was drafted within the last 7 days."""
    brand_key = brand.lower().strip()
    if brand_key not in log:
        return False
    last_date = datetime.date.fromisoformat(log[brand_key])
    days_ago = (datetime.date.today() - last_date).days
    return days_ago < BRAND_LOG_DAYS


def mark_brand_drafted(brand: str, log: dict) -> dict:
    log[brand.lower().strip()] = datetime.date.today().isoformat()
    return log


# ---------------------------------------------------------------------------
# API health check
# ---------------------------------------------------------------------------

def check_kyb_api() -> bool:
    """Check if the KYB API is responding. Returns True if healthy."""
    try:
        resp = requests.get(f"{KYB_API_URL}/", timeout=3)
        return resp.status_code < 500
    except Exception:
        return False


# ---------------------------------------------------------------------------
# News search via Tavily
# ---------------------------------------------------------------------------

def search_brand_news(query: str, max_results: int = 5) -> list[dict]:
    """Search for brand news stories. Returns list of story dicts."""
    if not TAVILY_API_KEY:
        return []

    try:
        resp = requests.post(
            "https://api.tavily.com/search",
            json={
                "api_key":        TAVILY_API_KEY,
                "query":          query,
                "search_depth":   "basic",
                "max_results":    max_results,
                "days":           1,              # last 24 hours only
                "include_domains": NEWS_SOURCES,
            },
            timeout=15,
        )
        resp.raise_for_status()
        data = resp.json()
    except Exception as e:
        print(f"  [news] Search failed for {query!r}: {e}")
        return []

    stories = []
    for item in data.get("results", [])[:max_results]:
        stories.append({
            "title":   item.get("title", ""),
            "snippet": item.get("content", ""),
            "url":     item.get("url", ""),
            "source":  item.get("url", "").split("/")[2] if item.get("url") else "",
        })

    return stories


# ---------------------------------------------------------------------------
# Brand extraction from news story
# ---------------------------------------------------------------------------

def extract_brand_from_story(story: dict) -> Optional[str]:
    """
    Extract the primary brand name from a news story.
    Uses Claude Haiku — cheap, fast, accurate enough.
    """
    if not ANTHROPIC_API_KEY:
        return None

    title   = story.get("title", "")
    snippet = story.get("snippet", "")

    prompt = textwrap.dedent(f"""
        Extract the PRIMARY brand name from this news story.
        Return ONLY the brand name — nothing else. No explanation.
        If no clear brand name exists, return: NONE

        Title: {title}
        Snippet: {snippet}
    """).strip()

    try:
        headers = {
            "x-api-key":         ANTHROPIC_API_KEY,
            "anthropic-version": "2023-06-01",
            "content-type":      "application/json",
        }
        body = {
            "model":      "claude-haiku-4-5-20251001",
            "max_tokens": 30,
            "messages":   [{"role": "user", "content": prompt}],
        }
        resp = requests.post(
            "https://api.anthropic.com/v1/messages",
            headers=headers, json=body, timeout=15
        )
        resp.raise_for_status()
        brand = resp.json()["content"][0]["text"].strip()
        return None if brand.upper() == "NONE" else brand
    except Exception as e:
        print(f"  [extract] Brand extraction failed: {e}")
        return None


# ---------------------------------------------------------------------------
# Sentiment check
# ---------------------------------------------------------------------------

def is_negative_story(story: dict) -> bool:
    """Returns True if the story contains negative signals."""
    text = (story.get("title", "") + " " + story.get("snippet", "")).lower()
    return any(signal in text for signal in NEGATIVE_SIGNALS)


# ---------------------------------------------------------------------------
# Card retrieval from KYB API
# ---------------------------------------------------------------------------

def get_brand_card(brand: str) -> Optional[dict]:
    """
    Retrieve brand card from the KYB API.
    Returns card dict or None if failed.
    """
    try:
        resp = requests.post(
            f"{KYB_API_URL}/api/brand",
            json={"brand": brand},
            headers={"X-Agent": "brand-agent"},
            timeout=20,
        )
        if resp.status_code == 200:
            return resp.json()
        return None
    except Exception as e:
        print(f"  [card] Card retrieval failed for {brand!r}: {e}")
        return None


def score_card_richness(card: dict) -> tuple[int, str]:
    """
    Score card richness 0-10.
    Returns (score, quality_label).
    """
    if not card:
        return 0, "none"

    fields = [
        card.get("sells"),
        card.get("pricing"),
        card.get("rivals"),
        card.get("underdog"),
        card.get("for"),
        card.get("growth"),
        card.get("fun_fact"),
        card.get("position"),
    ]

    populated = sum(1 for f in fields if f and str(f).strip() and str(f).strip().lower() not in ["unknown", "n/a", "none"])

    if populated >= 6:
        return populated, "rich"
    elif populated >= 4:
        return populated, "decent"
    elif populated >= 2:
        return populated, "thin"
    else:
        return populated, "very_thin"


# ---------------------------------------------------------------------------
# Draft generation
# ---------------------------------------------------------------------------

def determine_platform(story: dict) -> str:
    """Determine best platform for this story based on source."""
    url = story.get("url", "").lower()
    if any(s in url for s in ["yourstory", "inc42", "techcrunch", "arabianbusiness", "forbesmiddleeast"]):
        return "linkedin"
    elif any(s in url for s in ["quora"]):
        return "quora"
    else:
        return "linkedin"   # default to LinkedIn — highest conversion market


def generate_draft(
    brand: str,
    story: dict,
    card: Optional[dict],
    card_quality: str,
    platform: str,
    api_available: bool,
) -> Optional[str]:
    """
    Generate a draft response for the given brand news story.
    Never mentions AI. Never pitches. Structure speaks.
    """
    if not ANTHROPIC_API_KEY:
        return None

    title   = story.get("title", "")
    snippet = story.get("snippet", "")

    # Build card context for the prompt
    if card and card_quality in ("rich", "decent"):
        card_context = f"""
Available brand card data:
- What it sells: {card.get('sells', 'N/A')}
- Pricing: {card.get('pricing', {}).get('detail', 'N/A') if isinstance(card.get('pricing'), dict) else card.get('pricing', 'N/A')}
- For: {card.get('for', 'N/A')}
- Position: {card.get('position', 'N/A')}
- Rivals: {', '.join(card.get('rivals', [])) if isinstance(card.get('rivals'), list) else card.get('rivals', 'N/A')}
- Underdog: {card.get('underdog', 'N/A')}
- Growth: {card.get('growth', {}).get('detail', 'N/A') if isinstance(card.get('growth'), dict) else card.get('growth', 'N/A')}
- Fun fact: {card.get('fun_fact', 'N/A')}
"""
        format_instruction = f"Use the card data above to write a specific, accurate response. End with: {CANONICAL_URL}"
    elif card_quality == "thin":
        card_context = "The brand card exists but has limited data — this is an emerging brand."
        format_instruction = f'Use "early read" framing. End with: {CANONICAL_URL}'
    else:
        card_context = "No card data available — API unavailable or brand too new."
        format_instruction = f"Write a link-only response. End with: {CANONICAL_URL}"

    prompt = textwrap.dedent(f"""
        A news story about {brand} has appeared on {platform}:
        Title: {title}
        Snippet: {snippet}

        {card_context}

        Write a response to post on {platform} that:
        1. Opens with one sentence naming what this brand actually is
           or what makes this news moment interesting
        2. Gives one sentence of genuine useful context
        3. Ends with one sentence mentioning {CANONICAL_URL} naturally
           as a place to get a clean picture of the brand

        Rules:
        - Maximum 3 sentences total
        - Do NOT mention AI, machine learning, or any technology
        - Do NOT use exclamation marks
        - Do NOT say "check out" or "amazing" or "great"
        - Do NOT pitch or sell — just inform
        - Sound like a knowledgeable person, not a marketing account
        - {format_instruction}

        Return only the draft text. Nothing else.
    """).strip()

    try:
        headers = {
            "x-api-key":         ANTHROPIC_API_KEY,
            "anthropic-version": "2023-06-01",
            "content-type":      "application/json",
        }
        body = {
            "model":      "claude-haiku-4-5-20251001",
            "max_tokens": 200,
            "messages":   [{"role": "user", "content": prompt}],
        }
        resp = requests.post(
            "https://api.anthropic.com/v1/messages",
            headers=headers, json=body, timeout=20
        )
        resp.raise_for_status()
        return resp.json()["content"][0]["text"].strip()
    except Exception as e:
        print(f"  [draft] Draft generation failed: {e}")
        return None


# ---------------------------------------------------------------------------
# Major news detection
# ---------------------------------------------------------------------------

def is_major_news(stories: list[dict]) -> Optional[dict]:
    """
    Detect if any story represents a major brand news moment.
    Returns the major story dict or None.
    """
    major_signals = [
        "ipo", "series d", "series e", "series f", "unicorn",
        "acquisition", "acquired", "billion", "merger",
        "major launch", "global expansion", "enters india",
        "enters uae", "enters dubai", "enters middle east",
    ]

    for story in stories:
        text = (story.get("title", "") + " " + story.get("snippet", "")).lower()
        if any(signal in text for signal in major_signals):
            return story

    return None


def print_major_news_notification(story: dict, brand: str) -> bool:
    """
    Print major news notification and ask for cap increase approval.
    Returns True if user approves increase to 15.
    """
    print("\n" + "=" * 60)
    print("⚡ MAJOR NEWS SIGNAL")
    print("=" * 60)
    print(f"Brand:  {brand}")
    print(f"Story:  {story.get('title', '')}")
    print(f"Source: {story.get('source', '')}")
    print(f"\nCurrent cap: {MAX_DRAFTS} drafts (~$1.50)")
    print(f"Increase to: {MAX_DRAFTS_MAJOR} drafts (~$1.80)")
    print("\nIncrease today's cap to 15? (y/n): ", end="")

    try:
        answer = input().strip().lower()
        return answer == "y"
    except Exception:
        return False


# ---------------------------------------------------------------------------
# Save drafts
# ---------------------------------------------------------------------------

def save_drafts(drafts: list[dict], api_available: bool) -> Path:
    """Save drafts to dated folder. Returns file path."""
    today    = datetime.date.today().isoformat()
    run_time = datetime.datetime.now().strftime("%H%M%S")
    out_dir  = DRAFTS_DIR / today
    out_dir.mkdir(parents=True, exist_ok=True)

    out_file = out_dir / f"drafts_{run_time}.txt"

    with open(out_file, "w", encoding="utf-8") as f:
        f.write(VOICE_REFERENCE)
        f.write(f"\nDate: {today}  |  Run: {run_time}\n")
        f.write(f"API status: {'✓ available' if api_available else '✗ unavailable — link-only drafts'}\n")
        f.write(f"Drafts generated: {len(drafts)}\n")
        f.write("Nothing has been posted. All posting is manual.\n")
        f.write("\n" + "=" * 60 + "\n\n")

        for i, d in enumerate(drafts, 1):
            status = ""
            if d.get("negative_news"):
                status = "⚠️  NEGATIVE NEWS — review carefully before using"
            elif d.get("card_quality") in ("thin", "very_thin"):
                status = "⚡ THIN CARD — early read framing used"

            f.write(f"DRAFT #{i}")
            if status:
                f.write(f"  {status}")
            f.write(f"\n{'─' * 50}\n")
            f.write(f"Brand:    {d['brand']}\n")
            f.write(f"Platform: {d['platform'].upper()}\n")
            f.write(f"Card:     {d['card_quality']} ({d['card_fields']} fields)\n")
            f.write(f"Source:   {d['story_url']}\n")
            f.write(f"News:     {d['story_title']}\n")
            f.write(f"\nDRAFT RESPONSE:\n{d['draft']}\n")
            f.write(f"\nAction: [ ] Approved  [ ] Skipped\n")
            f.write(f"\n{'=' * 60}\n\n")

    # Also save machine-readable JSON
    json_file = out_dir / f"drafts_{run_time}.json"
    with open(json_file, "w", encoding="utf-8") as f:
        json.dump(drafts, f, indent=2, ensure_ascii=False)

    return out_file


# ---------------------------------------------------------------------------
# Main run
# ---------------------------------------------------------------------------

def run():
    print("=" * 60)
    print("Know Your Brand — Brand Awareness Agent v2")
    print(f"Started: {datetime.datetime.now().isoformat()}")
    print(f"Cost cap: $1.50/day | Max drafts: {MAX_DRAFTS} standard")
    print("=" * 60)

    # Load dedup log
    brand_log = load_brand_log()

    # Check KYB API health
    print("\n[health] Checking KYB API...")
    api_available = check_kyb_api()
    if api_available:
        print("  ✓ API available")
    else:
        print("  ✗ API unavailable — will use link-only drafts this run")

    # Collect news stories
    print("\n[news] Searching for brand news...")
    all_stories: list[dict] = []

    for query in NEWS_QUERIES[:8]:    # limit queries to control SerpAPI cost
        stories = search_brand_news(query, max_results=3)
        all_stories.extend(stories)
        print(f"  {query!r} → {len(stories)} stories")
        time.sleep(0.3)

    print(f"\n[collect] Raw stories: {len(all_stories)}")

    # Deduplicate stories by URL
    seen_urls: set = set()
    unique_stories: list[dict] = []
    for s in all_stories:
        url = s.get("url", "")
        if url and url not in seen_urls:
            seen_urls.add(url)
            unique_stories.append(s)

    print(f"[dedup] Unique stories: {len(unique_stories)}")

    # Check for major news
    current_cap = MAX_DRAFTS
    if unique_stories:
        major_story = is_major_news(unique_stories)
        if major_story:
            brand_check = extract_brand_from_story(major_story) or "Unknown brand"
            approved = print_major_news_notification(major_story, brand_check)
            if approved:
                current_cap = MAX_DRAFTS_MAJOR
                print(f"  Cap increased to {current_cap} drafts for today.")
            else:
                print(f"  Keeping standard cap of {current_cap} drafts.")

    # Process stories and generate drafts
    drafts: list[dict] = []
    negative_drafts: list[dict] = []
    processed_brands: set = set()

    print(f"\n[process] Processing stories (cap: {current_cap} drafts)...")

    for story in unique_stories:
        if len(drafts) + len(negative_drafts) >= current_cap:
            break

        # Extract brand
        brand = extract_brand_from_story(story)
        if not brand:
            continue

        # Skip duplicates within this run
        brand_key = brand.lower().strip()
        if brand_key in processed_brands:
            continue

        # Skip recently drafted brands (7-day window)
        if brand_recently_drafted(brand, brand_log):
            print(f"  [skip] {brand} — drafted within last {BRAND_LOG_DAYS} days")
            continue

        processed_brands.add(brand_key)

        print(f"  [process] {brand}")

        # Check sentiment
        negative = is_negative_story(story)

        # Get card if API available
        card = None
        card_quality = "none"
        card_fields  = 0

        if api_available:
            card = get_brand_card(brand)
            card_fields, card_quality = score_card_richness(card)
            print(f"    Card: {card_quality} ({card_fields} fields)")
        else:
            card_quality = "none"
            print(f"    Card: API unavailable — link only")

        # Determine platform
        platform = determine_platform(story)

        # Generate draft
        draft_text = generate_draft(
            brand, story, card, card_quality, platform, api_available
        )

        if not draft_text:
            continue

        draft_entry = {
            "brand":        brand,
            "platform":     platform,
            "card_quality": card_quality,
            "card_fields":  card_fields,
            "story_title":  story.get("title", ""),
            "story_url":    story.get("url", ""),
            "story_source": story.get("source", ""),
            "draft":        draft_text,
            "negative_news": negative,
            "generated_at": datetime.datetime.now().isoformat(),
            "posted":       False,
        }

        if negative:
            negative_drafts.append(draft_entry)
            print(f"    → Flagged as negative news — moved to review pile")
        else:
            drafts.append(draft_entry)
            print(f"    → Draft generated [{platform}]")

        # Mark brand as drafted
        brand_log = mark_brand_drafted(brand, brand_log)

        time.sleep(0.5)

    # Save brand log
    save_brand_log(brand_log)

    # Combine — negatives go at end with warning flags
    all_drafts = drafts + negative_drafts

    if not all_drafts:
        print("\n[done] No drafts generated this run.")
        print("Check: TAVILY_API_KEY set? News sources returning results?")
        return

    # Save drafts
    draft_file = save_drafts(all_drafts, api_available)

    # Terminal summary
    print("\n" + "=" * 60)
    print(f"DRAFT SUMMARY — {datetime.date.today().isoformat()}")
    print("=" * 60)
    print(f"Clean drafts:   {len(drafts)}")
    print(f"Flagged drafts: {len(negative_drafts)} (negative news — review carefully)")
    print(f"Total:          {len(all_drafts)}")
    print(f"\nDraft file: {draft_file}")
    print("\nOpen the draft file. Read the voice reference at the top.")
    print("Approve 3-5. Post manually. Nothing has been posted.")
    print("=" * 60)


if __name__ == "__main__":
    run()
