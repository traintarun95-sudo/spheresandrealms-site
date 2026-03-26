# Know Your Brand — Project Documentation

**Live at:** realmofbrands.com
**Product:** Structured brand intelligence cards
**Parent Studio:** Spheres & Realms (spheresandrealms.com)
**Last updated:** 2026-03-26

---

## What's Built

### Core Product
Know Your Brand delivers structured brand intelligence cards — clean, scannable summaries of what a brand is, what it does, who it's for, and whether it's legitimate.

**Pricing:**
- 3 free searches per device (no signup required)
- ₹349 / $4.99 for 150 searches (one-time purchase)

### Frontend (Static HTML)
| File | Purpose |
|------|---------|
| `index.html` | Landing page — hero, value prop, CTA |
| `search.html` | Main search interface — brand lookup, results display |
| `pricing.html` | Pricing page — plan details, purchase CTA |

### Backend (Node.js on Railway)
- **Server:** `server.js` — Node.js REST API
- **Host:** Railway
- **URL:** `https://know-your-brand-production.up.railway.app`
- **Responsibilities:**
  - Brand research orchestration (AI-powered)
  - Search quota tracking per device fingerprint
  - Payment verification (post-purchase quota unlock)
  - Returning structured brand intelligence JSON

### Infrastructure
| Layer | Service |
|-------|---------|
| Frontend hosting | GitHub Pages / Static host |
| Backend hosting | Railway |
| Domain | realmofbrands.com |
| Payments | (TBD — Razorpay / Stripe) |
| AI provider | (TBD — OpenAI / Anthropic) |

---

## Tech Stack

```
Frontend:   Static HTML + CSS + vanilla JS
Backend:    Node.js (Express)
Hosting:    Railway (backend), static host (frontend)
Domain:     realmofbrands.com
Payments:   TBD
AI:         TBD
```

---

## What's Next (Roadmap)

### Near-term
- [ ] Payment integration (Razorpay for INR, Stripe for USD)
- [ ] Device fingerprinting for free-tier quota enforcement
- [ ] Brand card design polish — shareable card format
- [ ] Search history (local, no account needed)
- [ ] Mobile responsiveness pass

### Medium-term
- [ ] Account system (optional — for cross-device quota)
- [ ] Brand comparison feature (X vs Y)
- [ ] Category browsing (by industry, country, size)
- [ ] API access tier for developers

### Long-term
- [ ] Browser extension — surface brand cards inline while browsing
- [ ] Embeddable brand widget for publishers
- [ ] Brand owner claim + verification system

---

## Agent Plans

### Brand Awareness Agent (Built: 2026-03-26)

A research and distribution agent that monitors Quora and Twitter/X for brand confusion signals and drafts helpful, non-spammy responses that organically mention realmofbrands.com.

**File:** `agent/brand_agent.py`

**What it does:**
1. Searches Quora and Twitter/X for queries like:
   - "what is this brand"
   - "anyone know this company"
   - "is X brand legit"
   - "is [brand] trustworthy"
   - "what does [brand] do"
2. Scores each result for relevance and brand confusion intent
3. Drafts 5–10 helpful response templates per run
4. Saves all drafts to `agent/drafts/` — **nothing posts automatically**
5. Presents daily review digest via CLI or HTML report

**What it does NOT do:**
- Post anything automatically
- Store personal data
- Create fake accounts or impersonate users

**Usage:**
```bash
cd agent
pip install -r requirements.txt
cp .env.example .env   # add your API keys
python brand_agent.py
# Review drafts in agent/drafts/YYYY-MM-DD/
```

---

## Repository Notes

- **GitHub repo:** `know-your-brand` (PRIVATE)
- **This file** should be committed to the repo root
- Frontend files are plain HTML — edit directly, no build step
- Backend changes require Railway redeploy (auto-deploys on push to main)

---

## Session Log

| Date | Work Done |
|------|-----------|
| 2026-03-26 | Initial PROJECT.md created; Brand Awareness Agent v1 built |
