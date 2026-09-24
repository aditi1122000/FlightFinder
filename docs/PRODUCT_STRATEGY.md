# GarudaX Product Strategy

**Last updated:** July 2026  
**Status:** Planning / re-onboarding

---

## Vision

GarudaX is a **pluggable AI flight advisor** — any travel company attaches their inventory; we turn it into conversational recommendations ranked by **fit** and **value**.

> *"We help you pick the right flight at a good price — not hunt across 15 sites for hidden coupons."*

---

## Core Product Model

You can have **both** best flight **and** good deals — but only if you split the problem correctly:

| Layer | Who owns it | What it does |
|-------|-------------|--------------|
| **Decision intelligence** | GarudaX (your moat) | Conversation, memory, fit scoring, value scoring, tradeoff explanations |
| **Inventory & pricing** | Partners (OTAs, agencies, aggregators) | Real fares, discounts, deeplinks, commission |

**Do not try to own both layers yourself.** Deals come from someone else's inventory. "Best flight" comes from your brain.

---

## Architecture

```
┌──────────────────────────────────────────────────────────┐
│  GARUDAX (you own this — your moat)                      │
│  • Conversation (LangGraph + Mistral)                    │
│  • User memory / preferences (Supabase)                  │
│  • Fit scoring (schedule, stops, reliability)            │
│  • Value scoring (price within THIS inventory)           │
│  • Tradeoff explanations ("₹2k more but non-stop")       │
└────────────────────────┬─────────────────────────────────┘
                         │  standard JSON in / out
┌────────────────────────▼─────────────────────────────────┐
│  PROVIDER ADAPTER LAYER (pluggable)                      │
│  BookingProvider │ DuffelProvider │ PartnerProvider │ ... │
└────────────────────────┬─────────────────────────────────┘
                         │
        ┌────────────────┼────────────────┐
        ▼                ▼                ▼
   Booking.com      Duffel / TMC      Expedia / MMT /
   (current)        (dev + B2B)       Cleartrip API
```

---

## Dual Scoring: Fit + Value

These are **two scores**, not one price sort.

| Score | What it measures | Data source |
|-------|------------------|-------------|
| **Fit score** | Right timing, stops, airline, comfort, user prefs | Schedules, rules, Supabase memory, reliability APIs |
| **Value score** | Good price *within available inventory* | Partner API (Expedia, Booking, Duffel, agency GDS) |

### Output format — always 3 recommendations

1. **Best overall** — highest combined score
2. **Best value** — strong fit + lowest price in results (not "cheapest on the internet")
3. **Best schedule** — fastest / best departure window

### Example explanation line

> *"This is ₹1,800 more than the cheapest option here, but non-stop and matches your 9am preference — best overall pick."*

**Product rule:** Always show *"Verify final price on [Partner] before booking."* You are a decision tool, not a price guarantee engine.

---

## Free Airline APIs — Honest Answer

**There is no free, production-grade airline fare API** that gives real discounts at scale.

| What exists "free" | What you actually get | Good for |
|--------------------|----------------------|----------|
| **Duffel test mode** | Sandbox flights, fake prices | Building integration |
| **Travelpayouts Data API** | Cached trends, popular routes | Content, not live search |
| **Aviationstack / AeroDataBox** | Schedules, delays, airport data | Reliability scoring, not fares |
| **RapidAPI proxies** (Booking.com) | One OTA's inventory, paid per call | MVP only |
| **Airline "direct" APIs** | Almost never public; need commercial deals | Not for startups without volume |

Airlines do not publish open APIs with negotiated fares. Discount depth lives inside **OTAs, GDS, NDC aggregators, and affiliate networks** — all gated behind partnerships.

---

## What NOT To Do

| Option | Verdict |
|--------|---------|
| Scrape Expedia / Skyscanner | ❌ No — legal risk, brittle, kills B2B deals |
| Build your own aggregator | ❌ No — years of work, millions in GDS deals |
| Free airline APIs for deals | ❌ Does not exist at production scale |
| Wait for Skyscanner official API before shipping | ❌ Approval takes months; don't block on it |

Use scraping only as a **personal prototype hack**, never as product infrastructure or something you pitch to partners.

---

## What TO Do — Use What Exists

| Stage | Inventory source | Purpose |
|-------|------------------|---------|
| **Now** | Booking.com via RapidAPI (already built) | Keep demo working |
| **Dev** | [Duffel](https://duffel.com/) test mode | Learn multi-airline schema; global routes |
| **Affiliate (optional)** | [Travelpayouts](https://www.travelpayouts.com/) | Commission on click/book |
| **India B2B** | MakeMyTrip, Cleartrip, Yatra, local TMCs | More reachable than Expedia for India-first |
| **Global B2B** | Expedia Travel Redirect API | Paused new applications (2026) — apply anyway, don't block on it |
| **Reliability** | AeroDataBox / Aviationstack free tiers | On-time % for fit scoring |
| **Distribution** | MCP server | Claude/Cursor distribution without rebuilding UI |

---

## B2B Business Models

### Model A: "Bring Your Own Inventory" (BYOI)

Any partner (Expedia, Indian OTA, travel agency, corporate TMC) plugs in via a standard contract.

**Request:**

```json
POST /garudax/search
{
  "slots": {
    "origin": "DEL",
    "destination": "DXB",
    "departure_date": "2026-08-15"
  },
  "provider": "expedia",
  "provider_credentials": { "...": "their keys" }
}
```

**Response:**

```json
{
  "recommendations": [
    {
      "type": "best_overall",
      "flight": { "...": "normalized fields" },
      "fit_score": 0.91,
      "value_score": 0.78,
      "reference_price": 28400,
      "reasons": ["Non-stop", "Morning departure", "Within budget"],
      "tradeoff": "₹1,200 more than cheapest in results",
      "book_url": "https://expedia.com/..."
    }
  ]
}
```

- **Partner provides:** fares, discounts, deeplinks, commission
- **You provide:** conversation, ranking, explanations, memory, MCP/Streamlit UI

### Model B: White-label widget

Partner embeds GarudaX chat on their site. Their API keys stay on their backend. User never leaves their brand until booking.

### Model C: MCP plugin

"Install GarudaX MCP + your credentials" — works in Claude Desktop / Cursor. Good for demos and developer distribution.

---

## Expedia Specifically

[Expedia Travel Redirect API](https://developers.expediagroup.com/travel-redirect-api):

- Search their flight inventory on your site
- Return prices + deeplinks
- Earn commission on bookings

**Catch:** They have **paused new API applications** (2026) due to volume.

**Action:**

1. Apply and get in the queue
2. In parallel, pitch smaller Indian OTAs / travel agencies
3. Build BYOI adapter so when Expedia says yes, it's a config change — not a rewrite

**Pitch to Expedia:**

> *"We're a conversational AI layer that drives qualified traffic to your inventory. Users describe trips in natural language; we rank your flights by fit and value; they book via your deeplink. You keep commission and booking; we handle the AI UX."*

---

## Who To Pitch

| Target | Why | What they bring |
|--------|-----|-----------------|
| **Indian travel agencies** | Fast decisions, need AI help | GDS / consolidator access |
| **Cleartrip / Yatra / ixigo** | India-focused, AI interest | Inventory + deeplinks |
| **Corporate travel (Itilite, etc.)** | Policy + "best flight" fits perfectly | API + B2B budget |
| **Fintech / card apps** | "Book flights in chat" | Distribution + maybe API |
| **Expedia** | Global, commission model | Inventory — harder to get in door |

Start with **agencies and mid-size OTAs**. One pilot with real inventory beats six months waiting for Expedia approval.

---

## Revenue Options

| Model | Who pays | Why it works without owning inventory |
|-------|----------|---------------------------------------|
| **B2B SaaS** (best near-term) | Travel agencies, D2C travel brands | Pay for agent productivity, not fare depth |
| **White-label API / MCP** | Fintech, card apps, corporate travel | Embedded conversational picker |
| **Per-seat / per-search API** | Developers using MCP | Usage-based |
| **Corporate pilot** | SMBs with travel policy | "Best flight within policy" |
| **Affiliate** (secondary) | Consumer traffic | Travelpayouts / partner deeplinks |

---

## Technical Build Plan (8 Weeks)

### Weeks 1–2: Provider adapter (foundation)

Refactor `flight_services.py` into:

```
src/
  providers/
    base.py          # FlightProvider interface
    booking.py       # current RapidAPI logic
    duffel.py        # sandbox
    partner_json.py  # B2B passthrough — partner sends normalized JSON
  scoring/
    fit.py           # schedule, stops, prefs
    value.py         # price percentile within result set
    rank.py          # combine → best_overall, best_value, best_schedule
```

One `NormalizedFlight` schema every provider must map to. Extend the existing `_normalize_booking_flight_offer()` pattern.

### Weeks 3–4: Dual scoring + UI

- Rank by fit + value, not price alone
- Show 3 recommendation cards with "why" and tradeoffs
- Disclaimer: *"Prices from [Partner]. Verify before booking."*

### Weeks 5–6: BYOI API + demo

- `POST /search` with `provider` + credentials or pre-configured partner
- Demo: GarudaX with Booking inventory → swap JSON → same UI, different partner inventory
- This demo is what you show Expedia / agencies

### Weeks 7–8: MCP + outreach

- Ship GarudaX MCP (`search_flights`, `get_recommendations`)
- Outreach to 10 targets: 5 Indian travel agencies, 3 OTAs, 2 corporate travel tools
- LinkedIn: "We built an AI flight advisor that plugs into any inventory API"

---

## Core Data Object

```python
{
  "flight": {
    "airline": "IndiGo",
    "flight_number": "6E-204",
    "departure_time": "09:00",
    "arrival_time": "11:15",
    "duration_minutes": 135,
    "stops": 0,
    "origin": "DEL",
    "destination": "BOM"
  },
  "reference_price": 24771,
  "fit_score": 0.87,
  "value_score": 0.72,
  "scores": {
    "schedule": 0.9,
    "reliability": 0.8,
    "convenience": 0.95,
    "price_within_budget": 1.0
  },
  "reasons": [
    "Departs 9am — matches your morning preference",
    "Non-stop — saves 3h vs connecting options"
  ],
  "tradeoffs": "₹2,400 more than cheapest option in results, but 4h faster",
  "book_url": "https://partner.com/..."
}
```

---

## Agent Behavior Changes

| Stop doing | Start doing |
|------------|-------------|
| "Found 12 cheaper flights" | "Here are 3 flights that fit your trip best" |
| Default refinement = price filter | Default = shorter layover / earlier / different airline |
| Lead UI with ₹ price | Lead with departure, duration, stops, why recommended |
| "Which is cheapest?" as primary use case | "Which should I take?" / "I have a meeting at 2pm" |
| Compete with Skyscanner | Compete with asking a friend who knows travel |

### Refinement intents (beyond price)

- Earlier / later departure
- Fewer stops
- Different airline
- Avoid red-eye
- More legroom / business class
- Safer layover (minimum connection time)
- Same as last trip (session + Supabase memory)

Price refinement stays available but optional, with honest framing.

---

## Positioning

**Weak:** "AI flight search that finds cheap flights"

**Strong:** "AI flight advisor that tells you which flight to take — based on your schedule, preferences, and reliability — ranked against the best deals available from our partner inventory"

### One-liner for sales / social

> **GarudaX is a pluggable AI flight advisor — any travel company attaches their inventory; we turn it into conversational recommendations ranked by fit and value.**

---

## Current Codebase Gaps (as of March 2026)

| Gap | Notes |
|-----|-------|
| Single provider (Booking.com) | Needs provider adapter layer |
| Price-first ranking | Needs dual scoring |
| INR / India-biased | Needs market/currency config |
| Round-trip not wired to API | `return_date` in slots but API sends one-way only |
| No tests / CI | Add smoke tests before B2B demos |
| No monetization hooks | Affiliate deeplinks, partner config |

---

## Immediate Next Steps

1. Deploy demo on Streamlit Cloud
2. Commit uncommitted refinement routing fixes
3. Scaffold `providers/` + `scoring/` modules
4. Add Duffel sandbox as second provider
5. Build BYOI `partner_json` adapter for B2B demos
6. Create Canva landing page + LinkedIn "we're back" post
7. Outreach to 5 Indian travel agencies with demo link

---

## Related Docs

- [README.md](../README.md) — setup and run instructions
- [DEVELOPER_FLOW.md](./DEVELOPER_FLOW.md) — architecture (partially stale)
- [WORKFLOW_DIAGRAM.md](./WORKFLOW_DIAGRAM.md) — LangGraph workflow
