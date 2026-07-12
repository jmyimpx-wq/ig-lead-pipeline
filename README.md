# Instagram → Snov.io Daily Lead Pipeline

Daily automation: discover Instagram profiles in your niches (wedding
designers, floral designers, tabletop stylists, home decor retail/wholesale) →
scrape public bio/email → free pre-filter (dedupe, regex, MX record check) →
Snov.io email verification (only on pre-filtered survivors, to save credits) →
push valid leads straight into your Snov.io campaign list. Runs free on GitHub
Actions, no server needed.

**Two discovery sources, so the pool keeps renewing instead of running out:**
- **Hashtag search** with an auto-expanding tag pool — hashtags spotted in
  scraped bios/captions get scored by frequency and folded into future runs
  (`state/discovered_hashtags.json`), so the tag list grows on its own instead
  of staying fixed at the ~30 seed tags in `config.py`.
- **Follower-graph crawl** of large hub accounts (`config.SEED_ACCOUNTS`) —
  wholesale marketplaces, trade associations, big industry pages. New people
  follow these hubs every day, so this source refreshes without any tag
  management at all, and is usually your biggest volume driver.

**Two more quality layers sit on top of discovery:**
- **B2B relevance filter** — a profile only becomes a candidate if its
  Instagram category, bio, or name actually matches your target verticals
  (tableware, interior design, retail, wholesale/import, wedding/floral/event)
  — see `config.RELEVANT_KEYWORDS`. Instagram's own "business account" flag is
  checked too, but isn't enough alone -- plenty of irrelevant accounts have it
  toggled on.
- **Own SMTP-level email verification** — before anything touches Snov.io, the
  pipeline connects directly to each email's real mail server and checks
  whether the mailbox exists (the same core technique paid verifiers use).
  Snov.io is only called as a fallback for leftover "unknown" cases (catch-all
  domains, unresponsive servers) -- not as the primary verifier, which cuts
  credit usage substantially.
  **Caveat:** GitHub-hosted Actions runners commonly block outbound port 25 to
  prevent spam abuse. If a run's logs show 100% "unknown" from the own-SMTP
  step, that's what's happening -- the check fails safe (never falsely
  approves or rejects); everything just falls through to the Snov.io fallback
  instead, so leads aren't lost, only verified by a different path than
  intended.

## 1. One-time setup

1. Create a new **private** GitHub repo, push these files to it.
2. Get an **Apify** account (apify.com) — free $5/month credit to start testing.
   - Actors confirmed and already wired into `pipeline.py`:
     - Hashtag discovery: `instaprism/instagram-hashtag-scraper`
     - Profile details + follower crawl: `apidojo/instagram-user-scraper` (same actor
       does both jobs — `getFollowers: false` for plain profile lookups,
       `getFollowers: true` for crawling a hub account's followers)
   - **Before your first real daily run**, do one manual test of the followers mode
     (`getFollowers: true`) directly in the Apify console on a small seed account,
     and open one dataset item to confirm the exact field name each follower's
     username sits under. `crawl_seed_followers()` in `pipeline.py` already checks
     a couple of likely shapes, but confirm against a real run before trusting the
     numbers.
   - Note: actor IDs and exact input field names (`hashtags`, `usernames`,
     `resultsLimit`) can differ slightly between actors/versions — open the
     actor's "Input" tab on Apify Console and adjust `pipeline.py`'s
     `run_input` dicts to match if needed.
3. Get **Snov.io API credentials**: Account Settings → API tab → API User ID
   (client_id) and Secret (client_secret). Your plan needs to support API +
   bulk verification credits (check current Snov.io pricing for your volume —
   free plan only gives 50 credits/month, nowhere near 500-1000/day).
4. In your GitHub repo: **Settings → Secrets and variables → Actions**, add:
   - `APIFY_TOKEN`
   - `APIFY_HASHTAG_ACTOR` → `instaprism/instagram-hashtag-scraper`
   - `APIFY_PROFILE_ACTOR` → `apidojo/instagram-user-scraper`
   - `APIFY_FOLLOWERS_ACTOR` → `apidojo/instagram-user-scraper` (same actor, different flags)
   - `SNOV_CLIENT_ID`
   - `SNOV_CLIENT_SECRET`
   - `SNOV_LIST_ID` (the Snov.io list you want leads pushed into — create a
     dedicated list per vertical/campaign if you want to keep them separate)

Also open `config.py` and replace the placeholder `SEED_ACCOUNTS` handles with
real large hub accounts in your niches (wholesale marketplaces, trade
associations, big event/floral/tabletop industry pages) — their followers are
your second, self-renewing lead source.

## 2. Test it manually first

Go to the **Actions** tab in your repo → "Daily Instagram Lead Pipeline" →
**Run workflow** (this uses the `workflow_dispatch` trigger). Watch the logs.
Start with a small `MAX_PROFILES_PER_RUN` in `config.py` (e.g. 200) to check:
- Actor field names are correct
- Email extraction is picking things up properly
- Snov.io verification is returning statuses correctly

Once it's clean, raise `MAX_PROFILES_PER_RUN` toward your real daily target.

## 3. Cost control knobs (all in `config.py`)

- `HASHTAGS_PER_RUN` — fewer tags = cheaper hashtag-discovery runs
- `MAX_PROFILES_PER_RUN` — your main lever; directly controls Apify spend
- `EXCLUDE_FREE_DOMAINS` — set to `{"gmail.com","yahoo.com","hotmail.com"}`
  if you only want business-domain emails (fewer candidates, higher quality)

The MX-record + dedupe pre-filter in `pipeline.py` runs **before** anything
touches Snov.io, so you only spend verification credits on emails that are
already format-valid, not-yet-seen, and have a real mail server — this
typically cuts wasted Snov.io credits by 30-50% compared to sending every
scraped email straight to their verifier.

## 4. Daily reality at your target scale (500-1000 verified/day)

Only ~15-25% of business IG profiles in these niches list an email in bio, so
expect to need ~3,000-6,000 profile scrapes/day to net 500-1000 valid emails.
At current Apify per-profile pricing that's a real recurring cost (roughly
$30-120/day depending on which actor you use) — this isn't something the
automation removes, it's the cost of the raw data. Recommend starting at
200-300 leads/day for a week, checking actual conversion + Snov.io
deliverability numbers, then scaling `MAX_PROFILES_PER_RUN` up with real
numbers instead of estimates.

## 5. Things worth doing before going live

- Read Instagram's current Terms of Service and Meta's automated-access
  policy, and check applicable data-privacy law (GDPR if targeting EU
  florists/designers) — public data doesn't automatically mean unrestricted
  commercial use everywhere.
- Verify the exact Snov.io API endpoint names/params against
  https://snov.io/api before relying on this in production — API responses
  and field names shown here were accurate as of this build but Snov.io (like
  any SaaS) updates its API periodically.

## 6. Two-tier AI verification (Claude Haiku + Sonnet)

On top of the keyword filters, candidates go through two AI checks:
- **Tier 1 — Haiku**: cheap, fast pass on the Instagram bio/category text
  against the real ICP (`config.ICP_DESCRIPTION`). Filters clear rejects
  before anything more expensive runs.
- **Tier 2 — Sonnet + real website**: only for candidates Haiku already
  approved. The pipeline fetches the lead's actual website and has Sonnet
  read the genuine page content (title, meta description, body text) before
  final approval -- catches cases where the bio alone was too vague to judge.

Both tiers fail open (never silently drop a lead) on API errors or an
unreachable website. Requires an `ANTHROPIC_API_KEY` secret (from
console.anthropic.com -- a separate developer API key, not your Claude.ai
subscription). If unset, both tiers are skipped automatically and the
pipeline falls back to keyword filtering only. Toggle either tier off in
`config.py` via `ENABLE_AI_QUALITY_GATE` / `ENABLE_SONNET_WEBSITE_VERIFY`.
