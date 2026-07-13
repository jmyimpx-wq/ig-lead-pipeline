"""
Daily Instagram -> Snov.io lead pipeline.

Flow:
  1. Discover profiles via hashtags and follower-graph crawl (HikerAPI)
  2. Pull full bio/email/website/contact for new usernames (HikerAPI)
  3. FREE pre-filter (regex + MX record check + dedupe) -- this is what saves
     Snov.io credits, since we never send garbage to their paid verifier
  4. Send survivors to Snov.io Email Verifier API (this is where credits get spent)
  5. Push only "valid" results into your Snov.io prospect list

Run daily via GitHub Actions (see .github/workflows/daily_leads.yml).
State (seen usernames / emails) persists in state/*.json, committed back to repo
by the workflow so the next run doesn't reprocess the same profiles.

Two discovery sources run every day, so the lead pool keeps renewing instead
of exhausting a fixed hashtag list:
  SOURCE 1: Hashtag search, with an auto-expanding tag pool -- hashtags seen
            in scraped bios/captions get harvested, scored by frequency, and
            folded into future runs (state/discovered_hashtags.json).
  SOURCE 2: Follower-graph crawl of large "hub" accounts (config.SEED_ACCOUNTS)
            -- new people follow these hubs continuously, so this source
            refreshes on its own without you managing a tag list at all.

Required environment variables / GitHub Secrets:
  HIKERAPI_TOKEN           HikerAPI access key (hikerapi.com) -- handles ALL
                            discovery and profile lookups (hashtag search,
                            follower crawl, profile detail). ~$0.0006/request,
                            pay-as-you-go, no monthly minimum. Apify has been
                            fully retired from this pipeline as of this version.
  SNOV_CLIENT_ID
  SNOV_CLIENT_SECRET
  SNOV_LIST_ID             the Snov.io prospect list to push valid leads into
"""

import os
import re
import json
import time
import random
import requests
import dns.resolver
from bs4 import BeautifulSoup
from datetime import datetime
from concurrent.futures import ThreadPoolExecutor, as_completed

import config

STATE_DIR = "state"
SEEN_USERNAMES_FILE = os.path.join(STATE_DIR, "seen_usernames.json")
SEEN_EMAILS_FILE = os.path.join(STATE_DIR, "seen_emails.json")
DISCOVERED_HASHTAGS_FILE = os.path.join(STATE_DIR, "discovered_hashtags.json")
HASHTAG_TAG_REGEX = re.compile(r"#(\w{3,30})")

EMAIL_REGEX = re.compile(r"[a-zA-Z0-9._%+\-]+@[a-zA-Z0-9.\-]+\.[a-zA-Z]{2,}")

# Apify has been fully retired from this pipeline -- HikerAPI now handles
# hashtag discovery, follower discovery, and profile-detail lookups (roughly
# 4x cheaper per profile, and removes the Apify-monthly-usage-cap crash risk
# seen in earlier runs).
HIKERAPI_TOKEN = os.environ.get("HIKERAPI_TOKEN", "")
HIKERAPI_BASE = "https://api.hikerapi.com"

SNOV_CLIENT_ID = os.environ["SNOV_CLIENT_ID"]
SNOV_CLIENT_SECRET = os.environ["SNOV_CLIENT_SECRET"]
SNOV_LIST_ID = os.environ["SNOV_LIST_ID"]

SNOV_BASE = "https://api.snov.io"


# ---------- state helpers ----------

def load_json_set(path):
    if os.path.exists(path):
        with open(path) as f:
            return set(json.load(f))
    return set()


def save_json_set(path, data):
    os.makedirs(STATE_DIR, exist_ok=True)
    with open(path, "w") as f:
        json.dump(sorted(data), f, indent=2)


DAILY_REPORTS_FILE = os.path.join(STATE_DIR, "daily_reports.json")
MAX_REPORTS_KEPT = 120  # ~4 months of daily history


def save_daily_report(report):
    """Append today's run summary to the reports file the dashboard reads."""
    os.makedirs(STATE_DIR, exist_ok=True)
    reports = []
    if os.path.exists(DAILY_REPORTS_FILE):
        try:
            with open(DAILY_REPORTS_FILE) as f:
                reports = json.load(f)
        except Exception:
            reports = []
    reports.append(report)
    reports = reports[-MAX_REPORTS_KEPT:]
    with open(DAILY_REPORTS_FILE, "w") as f:
        json.dump(reports, f, indent=2)
    print(f"  [debug] saved daily report ({len(reports)} reports kept)")


# ---------- step 1: hashtag discovery ----------

def load_discovered_hashtags():
    if os.path.exists(DISCOVERED_HASHTAGS_FILE):
        with open(DISCOVERED_HASHTAGS_FILE) as f:
            return json.load(f)  # {tag: frequency_count}
    return {}


def save_discovered_hashtags(freq_map):
    os.makedirs(STATE_DIR, exist_ok=True)
    with open(DISCOVERED_HASHTAGS_FILE, "w") as f:
        json.dump(freq_map, f, indent=2)


AI_SUGGESTED_HASHTAGS_FILE = os.path.join(STATE_DIR, "ai_suggested_hashtags.json")
AI_VERIFIED_SEEDS_FILE = os.path.join(STATE_DIR, "ai_verified_seeds.json")
AI_RESEARCH_LOG_FILE = os.path.join(STATE_DIR, "ai_research_log.json")


def call_claude_with_search(system_prompt, user_prompt, model=None, max_tokens=1500):
    """Call Claude with Anthropic's real hosted web_search tool enabled, so
    hashtag/seed research is grounded in an actual live search rather than
    the model's static training knowledge (which can be stale or, worse,
    hallucinate account handles that don't exist)."""
    model = model or config.SONNET_MODEL
    resp = requests.post(
        "https://api.anthropic.com/v1/messages",
        headers={
            "x-api-key": ANTHROPIC_API_KEY,
            "anthropic-version": "2023-06-01",
            "content-type": "application/json",
        },
        json={
            "model": model,
            "max_tokens": max_tokens,
            "system": system_prompt,
            "messages": [{"role": "user", "content": user_prompt}],
            "tools": [{"type": "web_search_20250305", "name": "web_search"}],
        },
        timeout=90,
    )
    resp.raise_for_status()
    data = resp.json()
    text_parts = [b["text"] for b in data.get("content", []) if b.get("type") == "text"]
    return "\n".join(text_parts)


def extract_json_array(text):
    """Sonnet's response may include prose around the JSON array (especially
    with web search results woven in) -- pull out just the array."""
    match = re.search(r"\[.*\]", text, re.DOTALL)
    if not match:
        return []
    try:
        return json.loads(match.group(0))
    except json.JSONDecodeError:
        return []


def should_run_ai_research():
    """AI research (hashtag + seed discovery) runs on an interval, not every
    day, to control API cost. Returns True if enough days have passed since
    the last run (or it's never run before)."""
    if os.path.exists(AI_RESEARCH_LOG_FILE):
        try:
            with open(AI_RESEARCH_LOG_FILE) as f:
                log = json.load(f)
            last_run = datetime.fromisoformat(log["last_run"].replace("Z", ""))
            days_since = (datetime.utcnow() - last_run).days
            return days_since >= config.AI_RESEARCH_INTERVAL_DAYS
        except Exception:
            return True
    return True


def mark_ai_research_ran():
    os.makedirs(STATE_DIR, exist_ok=True)
    with open(AI_RESEARCH_LOG_FILE, "w") as f:
        json.dump({"last_run": datetime.utcnow().isoformat() + "Z"}, f, indent=2)


def ai_research_new_hashtags():
    """Ask Sonnet (with real web search) to propose new hashtags for the niche,
    given the current pool, so the hashtag list keeps growing on its own."""
    existing = set()
    for tags in config.VERTICALS.values():
        existing.update(tags)
    if os.path.exists(AI_SUGGESTED_HASHTAGS_FILE):
        try:
            with open(AI_SUGGESTED_HASHTAGS_FILE) as f:
                existing.update(json.load(f))
        except Exception:
            pass

    prompt = (
        f"{config.ICP_DESCRIPTION}\n\n"
        f"Current Instagram hashtags already in use: {sorted(existing)}\n\n"
        "Search the web to find 15-20 NEW, currently active Instagram hashtags "
        "(not already in the list above) that people in this exact niche "
        "(tabletop/tableware retailers, interior designers, home decor boutiques "
        "with chinoiserie/grandmillennial/toile aesthetics, wedding/floral/event "
        "stylists) actually use today. Prefer specific, active hashtags over "
        "generic ones. Respond with ONLY a JSON array of lowercase hashtag "
        'strings without the # symbol, e.g. ["hashtag1", "hashtag2"].'
    )
    try:
        text = call_claude_with_search(config.ICP_DESCRIPTION, prompt)
        new_tags = [t.lower().strip() for t in extract_json_array(text) if isinstance(t, str)]
        new_tags = [t for t in new_tags if t and t not in existing]
        print(f"  [debug] AI hashtag research suggested {len(new_tags)} new tags: {new_tags}")
        if new_tags:
            os.makedirs(STATE_DIR, exist_ok=True)
            all_suggested = sorted(existing.union(new_tags) - {
                t for tags in config.VERTICALS.values() for t in tags
            })
            with open(AI_SUGGESTED_HASHTAGS_FILE, "w") as f:
                json.dump(all_suggested, f, indent=2)
    except Exception as e:
        print(f"  [debug] AI hashtag research failed (non-fatal, skipping this cycle): {e}")


def verify_candidate_seed_account(username):
    """Before trusting an AI-suggested seed account, confirm via a real
    HikerAPI call that it actually exists and has enough followers -- AI can
    misname or hallucinate handles, so nothing gets added on its word alone."""
    try:
        profile = hikerapi_get_profile(username)
        if not profile:
            return False, 0
        follower_count = profile.get("followerCount", 0) or 0
        return follower_count >= config.MIN_FOLLOWERS_FOR_AI_SUGGESTED_SEED, follower_count
    except Exception as e:
        print(f"  [debug] Seed verification failed for '{username}': {e}")
        return False, 0


def ai_research_new_seed_accounts():
    """Ask Sonnet (with real web search) to propose new B2B hub/trade-show
    Instagram accounts for the niche, then verify each candidate is real via
    HikerAPI before trusting it as a seed account."""
    existing = set(a.lower() for a in config.SEED_ACCOUNTS)
    if os.path.exists(AI_VERIFIED_SEEDS_FILE):
        try:
            with open(AI_VERIFIED_SEEDS_FILE) as f:
                existing.update(a.lower() for a in json.load(f))
        except Exception:
            pass

    prompt = (
        f"{config.ICP_DESCRIPTION}\n\n"
        f"Current seed accounts already in use: {sorted(existing)}\n\n"
        "Search the web to find 5-8 NEW large B2B trade show, wholesale "
        "marketplace, or industry association Instagram accounts (not already "
        "in the list above) whose followers would mostly be genuine buyers in "
        "this niche -- interior designers, home decor/tableware retailers, "
        "gift shop owners, wedding/floral industry professionals. Only suggest "
        "accounts you can find real evidence for (an official website, a "
        "verifiable Instagram handle). Respond with ONLY a JSON array of "
        'Instagram usernames (no @ symbol), e.g. ["highpointmarket", "ny_now"].'
    )
    try:
        text = call_claude_with_search(config.ICP_DESCRIPTION, prompt)
        candidates = [
            u.lower().strip().lstrip("@") for u in extract_json_array(text) if isinstance(u, str)
        ]
        candidates = [u for u in candidates if u and u not in existing]
        print(f"  [debug] AI seed research suggested {len(candidates)} candidates: {candidates}")

        verified = []
        for uname in candidates:
            is_valid, follower_count = verify_candidate_seed_account(uname)
            print(f"  [debug] verifying seed candidate '{uname}': {follower_count} followers, valid={is_valid}")
            if is_valid:
                verified.append(uname)

        print(f"  [debug] {len(verified)}/{len(candidates)} AI-suggested seeds passed verification")
        if verified:
            os.makedirs(STATE_DIR, exist_ok=True)
            all_verified = sorted(existing.union(verified) - set(a.lower() for a in config.SEED_ACCOUNTS))
            with open(AI_VERIFIED_SEEDS_FILE, "w") as f:
                json.dump(all_verified, f, indent=2)
    except Exception as e:
        print(f"  [debug] AI seed research failed (non-fatal, skipping this cycle): {e}")


def load_ai_suggested_hashtags():
    if os.path.exists(AI_SUGGESTED_HASHTAGS_FILE):
        try:
            with open(AI_SUGGESTED_HASHTAGS_FILE) as f:
                return json.load(f)
        except Exception:
            return []
    return []


def load_ai_verified_seeds():
    if os.path.exists(AI_VERIFIED_SEEDS_FILE):
        try:
            with open(AI_VERIFIED_SEEDS_FILE) as f:
                return json.load(f)
        except Exception:
            return []
    return []


def build_today_hashtags():
    """Blend the fixed seed pool with auto-discovered tags so the pool keeps growing."""
    pool = []
    for tags in config.VERTICALS.values():
        pool.extend(tags)
        for suffix in config.REGION_SUFFIXES:
            pool.append(f"{tags[0]}{suffix}")  # e.g. weddingdesignerusa

    if config.AUTO_EXPAND_HASHTAGS:
        discovered = load_discovered_hashtags()
        adopted = [
            tag for tag, freq in discovered.items()
            if freq >= config.MIN_HASHTAG_FREQUENCY_TO_ADOPT
        ]
        adopted.sort(key=lambda t: discovered[t], reverse=True)
        pool.extend(adopted[: config.MAX_AUTO_HASHTAGS_TO_ADD_PER_RUN])

    # Sonnet-researched hashtags (see ai_research_new_hashtags), refreshed weekly
    if config.ENABLE_AI_HASHTAG_RESEARCH:
        pool.extend(load_ai_suggested_hashtags())

    pool = list(dict.fromkeys(pool))  # dedupe, keep order
    random.shuffle(pool)
    return pool[: config.HASHTAGS_PER_RUN]


def harvest_hashtags(profiles):
    """Extract hashtags mentioned in scraped bios/recent captions, score by frequency,
    and merge into the persisted discovered-hashtags map for future runs."""
    if not config.AUTO_EXPAND_HASHTAGS:
        return
    freq_map = load_discovered_hashtags()
    existing_seed = {t for tags in config.VERTICALS.values() for t in tags}
    for profile in profiles:
        text_blobs = [profile.get("biography") or profile.get("bio") or ""]
        for post in profile.get("latestPosts", []) or []:
            text_blobs.append(post.get("caption", "") or "")
        for blob in text_blobs:
            for tag in HASHTAG_TAG_REGEX.findall(blob.lower()):
                if tag in existing_seed:
                    continue
                freq_map[tag] = freq_map.get(tag, 0) + 1
    save_discovered_hashtags(freq_map)


def hikerapi_hashtag_medias(hashtag_name, count=100):
    """Fetch recent/top posts for a hashtag via HikerAPI and extract owner
    usernames. Replaces the Apify hashtag actor entirely -- one less provider,
    and removes the Apify-monthly-limit crash risk seen earlier."""
    usernames = set()
    try:
        resp = requests.get(
            f"{HIKERAPI_BASE}/v2/hashtag/medias/top",
            params={"name": hashtag_name},
            headers={"x-access-key": HIKERAPI_TOKEN, "accept": "application/json"},
            timeout=20,
        )
        resp.raise_for_status()
        data = resp.json()
        items = data.get("response", {}).get("sections", []) if isinstance(data, dict) else data
        # Defensive parsing -- hashtag media responses can nest the owner under
        # a few different keys depending on API version; try the common ones.
        def extract_owner(obj):
            if not isinstance(obj, dict):
                return None
            for path in (("user", "username"), ("owner", "username")):
                node = obj
                for key in path:
                    node = node.get(key) if isinstance(node, dict) else None
                    if node is None:
                        break
                if isinstance(node, str):
                    return node
            return None

        def walk(obj):
            if isinstance(obj, dict):
                uname = extract_owner(obj)
                if uname:
                    usernames.add(uname)
                for v in obj.values():
                    walk(v)
            elif isinstance(obj, list):
                for v in obj:
                    walk(v)

        walk(items)
    except Exception as e:
        print(f"  [debug] HikerAPI hashtag lookup failed for '#{hashtag_name}': {e}")
    return usernames


def discover_usernames(hashtags):
    """Discover usernames by searching hashtags via HikerAPI (replaces the
    previous Apify hashtag actor)."""
    usernames = set()
    for tag in hashtags:
        tag_usernames = hikerapi_hashtag_medias(tag)
        print(f"  [debug] hashtag '#{tag}': {len(tag_usernames)} usernames")
        usernames.update(tag_usernames)
    return usernames


# ---------- source 2: follower-graph crawl (self-renewing, no tag list needed) ----------

def hikerapi_get_profile(username):
    """Fetch full profile data for one username via HikerAPI (~$0.0006/request,
    vs Apify's ~$0.0023-0.0027/profile). Returns a dict normalized to match the
    field names the rest of the pipeline already expects (biography, website,
    isBusiness, publicEmail, category, followerCount) -- so filters/extractors
    written for the old Apify schema keep working unchanged."""
    try:
        resp = requests.get(
            f"{HIKERAPI_BASE}/v1/user/by/username",
            params={"username": username},
            headers={"x-access-key": HIKERAPI_TOKEN, "accept": "application/json"},
            timeout=15,
        )
        resp.raise_for_status()
        raw = resp.json()
        if not raw or "pk" not in raw:
            return None
        return {
            "id": raw.get("pk"),
            "username": raw.get("username"),
            "fullName": raw.get("full_name"),
            "biography": raw.get("biography"),
            "website": raw.get("external_url"),
            "bioLinks": [raw.get("external_url")] if raw.get("external_url") else [],
            "isBusiness": raw.get("is_business", False),
            "publicEmail": raw.get("public_email"),
            "category": raw.get("category_name") or raw.get("category"),
            "followerCount": raw.get("follower_count"),
            "followingCount": raw.get("following_count"),
            "isPrivate": raw.get("is_private"),
            "isVerified": raw.get("is_verified"),
        }
    except Exception as e:
        print(f"  [debug] HikerAPI profile lookup failed for '{username}': {e}")
        return None


def crawl_seed_followers():
    """Pull followers of large hub accounts via HikerAPI. This pool refreshes
    on its own as new people follow these hubs -- it doesn't exhaust the way a
    fixed hashtag list does.

    NOTE: like every provider (this was also true of the previous Apify setup),
    follower-list data is shallow -- username/name/id only, not bio/website.
    Those usernames still need hikerapi_get_profile() before filtering.

    Returns dict {username: {}} (empty dicts -- just used for the username keys,
    see .keys() usage in main())."""
    usernames_found = {}
    all_seeds = list(config.SEED_ACCOUNTS)
    if config.ENABLE_AI_SEED_RESEARCH:
        all_seeds += load_ai_verified_seeds()

    for seed in all_seeds:
        seed_profile = hikerapi_get_profile(seed)
        if not seed_profile or not seed_profile.get("id"):
            print(f"  [debug] couldn't resolve seed '{seed}' to a user id, skipping")
            continue
        user_id = seed_profile["id"]

        collected = 0
        end_cursor = None
        try:
            while collected < config.MAX_FOLLOWERS_PER_SEED_PER_RUN:
                params = {"user_id": user_id}
                if end_cursor:
                    params["end_cursor"] = end_cursor
                resp = requests.get(
                    f"{HIKERAPI_BASE}/gql/user/followers/chunk",
                    params=params,
                    headers={"x-access-key": HIKERAPI_TOKEN, "accept": "application/json"},
                    timeout=20,
                )
                resp.raise_for_status()
                data = resp.json()
                chunk = data[0] if isinstance(data, list) and len(data) > 0 else []
                end_cursor = data[1] if isinstance(data, list) and len(data) > 1 else None
                if not chunk:
                    break
                for follower in chunk:
                    uname = follower.get("username")
                    if uname:
                        usernames_found[uname] = {}
                collected += len(chunk)
                if not end_cursor:
                    break
            print(f"  [debug] HikerAPI followers: got {collected} for seed '{seed}'")
        except Exception as e:
            print(f"  [debug] HikerAPI follower crawl failed for seed '{seed}': {e}")

    return usernames_found


# ---------- step 2: profile / bio scraping ----------

def scrape_profiles(usernames, max_workers=15):
    """Fetch full profile data for each username via HikerAPI, in parallel
    (pay-per-request pricing means no batching penalty like Apify had --
    just fire many concurrent lookups). Skips (doesn't crash) on individual
    failures so one bad username never kills the whole run."""
    usernames = list(usernames)
    all_profiles = []
    with ThreadPoolExecutor(max_workers=max_workers) as executor:
        futures = {executor.submit(hikerapi_get_profile, u): u for u in usernames}
        for future in as_completed(futures):
            profile = future.result()
            if profile:
                all_profiles.append(profile)
    print(f"  [debug] HikerAPI profile scrape: {len(all_profiles)}/{len(usernames)} profiles returned")
    return all_profiles


def extract_email(profile):
    for field in ("email", "businessEmail", "publicEmail"):
        val = profile.get(field)
        if val and EMAIL_REGEX.match(val):
            return val.lower().strip()
    bio = profile.get("biography") or profile.get("bio") or ""
    match = EMAIL_REGEX.search(bio)
    return match.group(0).lower().strip() if match else None


def is_relevant_b2b_lead(profile):
    """True only if the profile's category/bio/name actually matches our target
    verticals (tableware, interior design, retail, wholesale/import, wedding/floral/
    event). This is the real quality gate -- Instagram's own isBusiness flag alone
    lets through plenty of irrelevant accounts that just happen to have it toggled on."""
    text = " ".join(
        str(profile.get(field, "") or "")
        for field in ("category", "categoryName", "biography", "bio", "fullName")
    ).lower()
    return any(keyword in text for keyword in config.RELEVANT_KEYWORDS)


def is_excluded_supplier(profile):
    """True if this looks like an exporter/manufacturer/factory/supplier -- i.e. a
    competitor rather than a buyer. Checked against username too, since many such
    accounts embed it directly in the handle (e.g. 'xyz_exports', 'abc_manufacturing')."""
    text = " ".join(
        str(profile.get(field, "") or "")
        for field in ("username", "category", "categoryName", "biography", "bio", "fullName")
    ).lower()
    return any(keyword in text for keyword in config.EXCLUDE_KEYWORDS)


def has_real_website(profile):
    """True if the profile has an actual external website or bio link. Accounts
    with no web presence at all are much less likely to be a real importing/
    reselling business -- just an Instagram-only hobby page."""
    website = profile.get("website")
    bio_links = profile.get("bioLinks")
    return bool(website) or bool(bio_links)


# ---------- step 3: free pre-filter (saves Snov.io credits) ----------

_mx_cache = {}


def has_mx_record(domain):
    if domain in _mx_cache:
        return _mx_cache[domain]
    try:
        answers = dns.resolver.resolve(domain, "MX", lifetime=5)
        ok = len(answers) > 0
    except Exception:
        ok = False
    _mx_cache[domain] = ok
    return ok


def prefilter(email, seen_emails):
    if not EMAIL_REGEX.fullmatch(email):
        return False
    domain = email.split("@")[-1]
    if config.EXCLUDE_FREE_DOMAINS and domain in config.EXCLUDE_FREE_DOMAINS:
        return False
    if email in seen_emails:
        return False
    if not has_mx_record(domain):
        return False
    return True


# ---------- step 3b: own SMTP-level verification (primary verifier, not Snov.io) ----------

import smtplib
import socket

_mailbox_cache = {}


def smtp_check_mailbox(email):
    """Verify a mailbox actually exists by talking to its real mail server directly
    (MAIL FROM + RCPT TO handshake, without sending anything) -- this is the same
    core technique paid verifiers use, done ourselves instead of spending credits.

    Returns "valid", "invalid", or "unknown" (server refused to say / connection
    blocked / catch-all domain that accepts everything). "unknown" results are the
    only ones handed to Snov.io downstream, as a fallback -- not the primary check.

    Caveat: some hosting environments (including GitHub-hosted Actions runners)
    block outbound port 25 to prevent spam abuse. If every check comes back
    "unknown", that's almost certainly what's happening here -- the function
    fails safe (never falsely rejects) in that case, it just can't confirm.
    """
    if email in _mailbox_cache:
        return _mailbox_cache[email]

    domain = email.split("@")[-1]
    if domain in config.DISPOSABLE_EMAIL_DOMAINS:
        _mailbox_cache[email] = "invalid"
        return "invalid"

    try:
        answers = dns.resolver.resolve(domain, "MX", lifetime=5)
        mx_host = str(sorted(answers, key=lambda r: r.preference)[0].exchange).rstrip(".")
    except Exception:
        _mailbox_cache[email] = "invalid"  # no mail server at all -> definitely can't be valid
        return "invalid"

    try:
        smtp = smtplib.SMTP(timeout=8)
        smtp.connect(mx_host, 25)
        smtp.helo("verify.local")
        smtp.mail("verify@verify.local")
        code, _ = smtp.rcpt(email)
        # Also probe an almost-certainly-nonexistent address at the same domain,
        # to detect catch-all domains (which accept everything and can't be trusted).
        probe_code, _ = smtp.rcpt(f"nonexistent-probe-xyz123@{domain}")
        smtp.quit()

        if probe_code == 250:
            result = "unknown"  # catch-all domain, can't distinguish real from fake
        elif code == 250:
            result = "valid"
        elif code in (550, 551, 553, 554):
            result = "invalid"
        else:
            result = "unknown"
    except (socket.timeout, ConnectionRefusedError, OSError, smtplib.SMTPException):
        result = "unknown"  # couldn't connect -- likely port 25 blocked in this environment

    _mailbox_cache[email] = result
    return result


# ---------- AI quality gate (Claude Haiku) ----------

ANTHROPIC_API_KEY = os.environ.get("ANTHROPIC_API_KEY", "")


def ai_judge_lead(profile):
    """Ask Claude Haiku whether this profile is a genuine qualified buyer lead,
    using the ICP description in config.py. Haiku now judges niche-relevance AND
    competitor-exclusion holistically in one pass (replacing the old blunt
    keyword filters, which had real false positive/negative problems).
    Returns (is_qualified: bool, confidence: "high"/"low", reason: str).
    Fails open (treats as qualified, high confidence) if the API call errors,
    so a transient API issue never silently drops good leads."""
    if not config.ENABLE_AI_QUALITY_GATE or not ANTHROPIC_API_KEY:
        return True, "high", "AI gate disabled"

    profile_summary = {
        "username": profile.get("username"),
        "fullName": profile.get("fullName"),
        "category": profile.get("category") or profile.get("categoryName"),
        "biography": profile.get("biography") or profile.get("bio"),
        "website": profile.get("website"),
        "followerCount": profile.get("followerCount"),
    }

    try:
        resp = requests.post(
            "https://api.anthropic.com/v1/messages",
            headers={
                "x-api-key": ANTHROPIC_API_KEY,
                "anthropic-version": "2023-06-01",
                "content-type": "application/json",
            },
            json={
                "model": config.AI_MODEL,
                "max_tokens": 150,
                "system": config.ICP_DESCRIPTION,
                "messages": [
                    {
                        "role": "user",
                        "content": (
                            "Profile to screen (judge BOTH niche-relevance and whether "
                            "this looks like a competitor -- exporter/manufacturer/factory/"
                            "supplier -- not just a keyword match):\n"
                            f"{json.dumps(profile_summary, indent=2)}\n\n"
                            "Respond with ONLY a JSON object: "
                            '{"is_qualified": true/false, '
                            '"confidence": "high"/"low" (low if the bio is too vague/generic '
                            "to judge confidently from Instagram data alone and would benefit "
                            "from actually reading their website), "
                            '"reason": "one short sentence"}'
                        ),
                    }
                ],
            },
            timeout=20,
        )
        resp.raise_for_status()
        text = resp.json()["content"][0]["text"]
        # Strip any stray markdown fences before parsing
        text = text.strip().removeprefix("```json").removeprefix("```").removesuffix("```").strip()
        result = json.loads(text)
        return (
            bool(result.get("is_qualified")),
            result.get("confidence", "low"),
            result.get("reason", ""),
        )
    except Exception as e:
        print(f"  [debug] AI judge call failed for '{profile.get('username')}', keeping lead: {e}")
        return True, "high", "AI check failed, kept by default"


def fetch_website_text(url, timeout=10, max_chars=2500):
    """Fetch a lead's real website and extract readable text (title, meta
    description, visible body text) for Sonnet to actually read -- not just
    guess from an Instagram bio. Returns None if the site is unreachable/slow/
    broken, so callers can fall back gracefully rather than fail the lead."""
    if not url:
        return None
    if not url.startswith("http"):
        url = f"https://{url}"
    try:
        resp = requests.get(
            url,
            timeout=timeout,
            headers={"User-Agent": "Mozilla/5.0 (compatible; LeadResearchBot/1.0)"},
        )
        resp.raise_for_status()
        soup = BeautifulSoup(resp.text, "html.parser")

        title = soup.title.string.strip() if soup.title and soup.title.string else ""
        meta_desc_tag = soup.find("meta", attrs={"name": "description"})
        meta_desc = meta_desc_tag.get("content", "").strip() if meta_desc_tag else ""

        for tag in soup(["script", "style", "nav", "footer", "header"]):
            tag.decompose()
        body_text = " ".join(soup.get_text(separator=" ").split())

        combined = f"TITLE: {title}\nMETA DESCRIPTION: {meta_desc}\nPAGE TEXT: {body_text}"
        return combined[:max_chars]
    except Exception as e:
        print(f"  [debug] website fetch failed for {url}: {e}")
        return None


def ai_deep_verify_with_website(profile):
    """Tier 2 AI check (Claude Sonnet): only called for candidates Haiku already
    approved. Actually reads the lead's real website content and makes a final,
    grounded judgment -- catches cases where the Instagram bio alone was too
    generic/ambiguous to tell (e.g. bio just says 'shop online' with no detail,
    but the website itself is clearly not a home decor/tableware business).
    Returns (is_qualified: bool, reason: str). Fails open on error or unreachable
    website -- never silently drops a lead just because their site was slow."""
    if not config.ENABLE_SONNET_WEBSITE_VERIFY or not ANTHROPIC_API_KEY:
        return True, "Sonnet website verify disabled"

    website = profile.get("website")
    website_text = fetch_website_text(website)
    if not website_text:
        return True, "Website unreachable, kept on Haiku's judgment"

    try:
        resp = requests.post(
            "https://api.anthropic.com/v1/messages",
            headers={
                "x-api-key": ANTHROPIC_API_KEY,
                "anthropic-version": "2023-06-01",
                "content-type": "application/json",
            },
            json={
                "model": config.SONNET_MODEL,
                "max_tokens": 200,
                "system": config.ICP_DESCRIPTION,
                "messages": [
                    {
                        "role": "user",
                        "content": (
                            f"Instagram username: {profile.get('username')}\n"
                            f"Instagram bio: {profile.get('biography') or profile.get('bio')}\n\n"
                            f"Their actual website content (fetched directly):\n{website_text}\n\n"
                            "Based on the REAL website content above (not just the Instagram bio), "
                            "is this a genuinely qualified buyer lead matching the ICP? "
                            'Respond with ONLY a JSON object: {"is_qualified": true/false, "reason": "one short sentence"}'
                        ),
                    }
                ],
            },
            timeout=30,
        )
        resp.raise_for_status()
        text = resp.json()["content"][0]["text"]
        text = text.strip().removeprefix("```json").removeprefix("```").removesuffix("```").strip()
        result = json.loads(text)
        return bool(result.get("is_qualified")), result.get("reason", "")
    except Exception as e:
        print(f"  [debug] Sonnet deep-verify failed for '{profile.get('username')}', keeping lead: {e}")
        return True, "Sonnet check failed, kept by default"


# ---------- step 4: Snov.io verification ----------

def snov_get_token():
    resp = requests.post(
        f"{SNOV_BASE}/v1/oauth/access_token",
        data={
            "grant_type": "client_credentials",
            "client_id": SNOV_CLIENT_ID,
            "client_secret": SNOV_CLIENT_SECRET,
        },
        timeout=30,
    )
    resp.raise_for_status()
    return resp.json()["access_token"]


def snov_verify_emails(token, emails):
    """Submit emails for verification and poll for results using Snov.io's current v2 API:
    POST /v2/email-verification/start -> {"data": {"task_hash": "..."}}
    GET  /v2/email-verification/result?task_hash=... -> {"status": "completed"/"in_progress", "data": [...]}
    Auth via 'Authorization: Bearer <token>' header (the old v1 access_token-as-param
    endpoints this pipeline used earlier are deprecated and were silently failing).
    Returns dict {email: status} where status is one of "valid"/"not_valid"/"unknown"."""
    headers = {"Authorization": f"Bearer {token}"}
    results = {}
    batch_size = 10  # Snov.io's email-verification/start endpoint caps at 10 emails/request

    for i in range(0, len(emails), batch_size):
        batch = emails[i : i + batch_size]

        start_resp = requests.post(
            "https://api.snov.io/v2/email-verification/start",
            headers=headers,
            json={"emails": batch},
            timeout=60,
        )
        if start_resp.status_code == 422:
            # JSON body rejected -- print the exact validation message, then retry
            # with form-encoded emails[] (matches the pattern Snov.io's own PHP
            # examples use for this family of endpoints).
            print(f"  [debug] 422 on JSON body: {start_resp.text}")
            start_resp = requests.post(
                "https://api.snov.io/v2/email-verification/start",
                headers=headers,
                data={"emails[]": batch},
                timeout=60,
            )
            if start_resp.status_code == 422:
                print(f"  [debug] 422 on form body too: {start_resp.text}")
        start_resp.raise_for_status()
        start_data = start_resp.json()
        print(f"  [debug] snov verification start response: {start_data}")
        task_hash = start_data.get("data", {}).get("task_hash")
        if not task_hash:
            print("  [debug] no task_hash returned, skipping this batch")
            continue

        # Poll for completion
        for attempt in range(30):  # up to ~5 minutes (30 x 10s)
            time.sleep(10)
            result_resp = requests.get(
                "https://api.snov.io/v2/email-verification/result",
                headers=headers,
                params={"task_hash": task_hash},
                timeout=60,
            )
            result_resp.raise_for_status()
            result_data = result_resp.json()
            status = result_data.get("status")
            if status == "completed":
                print(f"  [debug] snov verification completed: {result_data}")
                for row in result_data.get("data", []):
                    email = row.get("email")
                    result_obj = row.get("result", {}) or {}
                    email_status = result_obj.get("smtp_status")
                    if email:
                        results[email] = email_status
                break
            print(f"  [debug] snov verification status: {status}, waiting...")
        else:
            print(f"  [debug] snov verification for task {task_hash} did not complete in time")

    return results


def snov_add_to_list(token, valid_prospects):
    """Push valid {email, username, source_url} rows into the target Snov.io list."""
    for p in valid_prospects:
        requests.post(
            f"{SNOV_BASE}/v1/add-prospect-to-list",
            data={
                "access_token": token,
                "listId": SNOV_LIST_ID,
                "email": p["email"],
                "source": p.get("source_url", ""),
            },
            timeout=30,
        )


# ---------- main ----------

def main():
    seen_usernames = load_json_set(SEEN_USERNAMES_FILE)
    seen_emails = load_json_set(SEEN_EMAILS_FILE)

    report = {
        "date": datetime.utcnow().strftime("%Y-%m-%d"),
        "timestamp": datetime.utcnow().isoformat() + "Z",
        "haiku_rejections": [],
        "sonnet_rejections": [],
        "pushed_leads": [],
    }

    # Weekly AI research: Sonnet (with real web search) proposes new hashtags and
    # new seed accounts on its own. Runs on an interval, not every day, to control
    # API cost -- see config.AI_RESEARCH_INTERVAL_DAYS.
    if ANTHROPIC_API_KEY and should_run_ai_research():
        print("Running weekly AI research (new hashtags + seed accounts)...")
        if config.ENABLE_AI_HASHTAG_RESEARCH:
            ai_research_new_hashtags()
        if config.ENABLE_AI_SEED_RESEARCH:
            ai_research_new_seed_accounts()
        mark_ai_research_ran()

    hashtags = build_today_hashtags()
    print(f"Today's hashtags ({len(hashtags)}): {hashtags}")

    # Source 1: hashtag search -- only gives usernames. Wrapped so that if HikerAPI
    # is unavailable (rate limit, monthly cap, transient error) the pipeline
    # still runs on the network source alone instead of crashing entirely.
    try:
        hashtag_usernames = discover_usernames(hashtags)
        print(f"Hashtag source: {len(hashtag_usernames)} usernames")
    except Exception as e:
        print(f"Hashtag discovery failed (continuing with network source only): {e}")
        hashtag_usernames = set()
        report["hashtag_discovery_error"] = str(e)

    # Source 2: follower-graph crawl. CORRECTION (previous version of this code
    # assumed this call returns full profile data for every follower and could
    # skip a second scrape -- that was wrong. Instagram's follower-list endpoint
    # only returns shallow data per follower (username, name); bio/website/
    # category/email come back empty/undefined for followers even though the
    # schema has those keys. Only the SEED account's own row is fully populated.
    # So network-sourced usernames need the same paid profile-detail scrape as
    # hashtag-sourced ones -- there's no shortcut here.
    network_usernames = set(crawl_seed_followers().keys())
    print(f"Network source: {len(network_usernames)} usernames")

    combined_usernames = hashtag_usernames | network_usernames
    new_usernames = list(combined_usernames - seen_usernames)[: config.MAX_PROFILES_PER_RUN]
    print(f"Combined: {len(combined_usernames)} usernames, {len(new_usernames)} new to scrape")

    report["hashtag_source_discovered"] = len(hashtag_usernames)
    report["network_source_discovered"] = len(network_usernames)
    report["profiles_scraped_this_run"] = len(new_usernames)

    if not new_usernames:
        print("No new usernames today, exiting.")
        save_daily_report(report)
        return

    profiles = scrape_profiles(new_usernames)
    print(f"  [debug] scrape_profiles returned {len(profiles)} total profiles (requested {len(new_usernames)} usernames)")
    report["profiles_actually_returned"] = len(profiles)
    harvest_hashtags(profiles)  # feed tomorrow's auto-expanded hashtag pool

    if profiles:
        sample = profiles[0]
        report["debug_sample_scraped_profile"] = {
            "username": sample.get("username"),
            "isBusiness": sample.get("isBusiness"),
            "category": sample.get("category"),
            "biography": (sample.get("biography") or "")[:150],
            "website": sample.get("website"),
            "bioLinks": sample.get("bioLinks"),
        }

    candidates = []  # [{email, username, source_url}] -- profiles with a website, ready for Haiku's full judgment
    username_to_profile = {}
    skipped_no_website = 0
    non_business_but_kept = 0
    for profile in profiles:
        uname = profile.get("username")
        seen_usernames.add(uname) if uname else None

        # NOTE: Instagram's isBusiness flag is intentionally NOT a hard filter --
        # it has both false positives and false negatives. Tracked for
        # visibility only.
        if not profile.get("isBusiness", False):
            non_business_but_kept += 1

        # The ONLY free hard filter left: no website/bio link at all -> much
        # less likely to be a real importing/reselling business, just an
        # Instagram-only page. This alone prunes the majority of junk for
        # free, before anything reaches a paid AI call.
        if not has_real_website(profile):
            skipped_no_website += 1
            continue

        # Relevance and competitor-exclusion are now judged by Haiku directly
        # (see ai_judge_lead below) instead of blunt keyword matching -- this
        # was rejecting genuine leads whose bio wording didn't match the
        # keyword list, and letting through false positives (e.g. a jewelry
        # account matching on "boutique"). Haiku's ICP prompt already covers
        # both dimensions in one holistic judgment.
        email = extract_email(profile)
        if email and prefilter(email, seen_emails):
            candidates.append(
                {
                    "email": email,
                    "username": uname,
                    "source_url": f"https://instagram.com/{uname}" if uname else "",
                }
            )
            username_to_profile[uname] = profile
    print(
        f"  [debug] skipped {skipped_no_website} no-website profiles "
        f"({non_business_but_kept} of the considered pool weren't marked 'business' by Instagram but were still evaluated)"
    )
    report["skipped_no_website"] = skipped_no_website
    report["candidates_after_keyword_filters"] = len(candidates)

    print(f"{len(candidates)} candidates passed free pre-filter (website + dedupe/regex/MX)")

    # AI quality gate -- Haiku judges niche-relevance AND competitor-exclusion
    # holistically for every website-having candidate (replaces the old blunt
    # keyword filters). High-confidence verdicts are trusted directly; only
    # "low confidence" (ambiguous bio) cases get escalated to Sonnet for a
    # deeper look at their real website -- this keeps the expensive Sonnet
    # tier small while still catching genuinely unclear cases properly.
    confident_qualified = []
    uncertain_pool = []  # -> escalated to Sonnet
    haiku_rejected_count = 0

    if config.ENABLE_AI_QUALITY_GATE and candidates:
        for c in candidates:
            profile = username_to_profile.get(c["username"], {})
            is_qualified, confidence, reason = ai_judge_lead(profile)
            if not is_qualified and confidence == "high":
                haiku_rejected_count += 1
                print(f"  [debug] Haiku rejected '{c['username']}' (high confidence): {reason}")
                report["haiku_rejections"].append({"username": c["username"], "reason": reason})
            elif is_qualified and confidence == "high":
                confident_qualified.append(c)
            else:
                # low confidence, either direction -- worth Sonnet reading the real website
                uncertain_pool.append(c)
        print(
            f"Haiku: {len(confident_qualified)} confident-qualified, "
            f"{len(uncertain_pool)} uncertain (-> Sonnet), "
            f"{haiku_rejected_count} confident-rejected"
        )
        candidates = confident_qualified + uncertain_pool
    report["haiku_confident_qualified"] = len(confident_qualified)
    report["haiku_uncertain_escalated_to_sonnet"] = len(uncertain_pool)

    # Tier 2: Sonnet + real website content -- ONLY for the "uncertain" pool
    # Haiku flagged, not every Haiku-approved lead. This is the main cost lever
    # on the expensive tier: most candidates should resolve at the Haiku stage.
    if config.ENABLE_SONNET_WEBSITE_VERIFY and uncertain_pool:
        sonnet_passed = []
        sonnet_rejected_count = 0
        for c in uncertain_pool:
            profile = username_to_profile.get(c["username"], {})
            is_qualified, reason = ai_deep_verify_with_website(profile)
            if is_qualified:
                sonnet_passed.append(c)
            else:
                sonnet_rejected_count += 1
                print(f"  [debug] Sonnet rejected '{c['username']}' after reading website: {reason}")
                report["sonnet_rejections"].append({"username": c["username"], "reason": reason})
        print(f"AI quality gate (Sonnet+website): {len(sonnet_passed)}/{len(uncertain_pool)} passed ({sonnet_rejected_count} rejected)")
        candidates = confident_qualified + sonnet_passed

    # Save state now -- usernames/profiles already scraped (and HikerAPI credits spent)
    # should never be reprocessed, even if the verification step below fails.
    save_json_set(SEEN_USERNAMES_FILE, seen_usernames)

    if not candidates:
        save_daily_report(report)
        return

    # Step A: our own SMTP-level mailbox check -- this is the PRIMARY verifier,
    # not Snov.io. Snov.io is only used as a fallback for the "unknown" leftovers
    # (catch-all domains, or environments where outbound port 25 is blocked).
    own_valid, own_invalid, own_unknown = [], [], []
    for c in candidates:
        result = smtp_check_mailbox(c["email"])
        if result == "valid":
            own_valid.append(c)
        elif result == "invalid":
            own_invalid.append(c)
        else:
            own_unknown.append(c)
    print(
        f"Own SMTP verification: {len(own_valid)} valid, "
        f"{len(own_invalid)} invalid (dropped), {len(own_unknown)} unknown "
        f"(sending to Snov.io as fallback)"
    )
    report["own_smtp_valid"] = len(own_valid)
    report["own_smtp_invalid"] = len(own_invalid)
    report["own_smtp_unknown"] = len(own_unknown)

    valid = list(own_valid)

    if own_unknown:
        try:
            token = snov_get_token()
            emails = [c["email"] for c in own_unknown]
            statuses = snov_verify_emails(token, emails)
            fallback_valid = [c for c in own_unknown if statuses.get(c["email"]) == "valid"]
            print(f"Snov.io fallback: {len(fallback_valid)}/{len(own_unknown)} verified valid")
            report["snov_fallback_valid"] = len(fallback_valid)
            valid.extend(fallback_valid)
        except requests.RequestException as e:
            print(f"Snov.io fallback step failed (unknown-status candidates dropped this run): {e}")
            report["snov_fallback_error"] = str(e)

    print(f"{len(valid)}/{len(candidates)} total verified valid")
    report["total_verified_valid"] = len(valid)

    if valid:
        token = snov_get_token()
        snov_add_to_list(token, valid)
        for v in valid:
            seen_emails.add(v["email"])
        save_json_set(SEEN_EMAILS_FILE, seen_emails)
        report["pushed_leads"] = [{"username": v["username"], "email": v["email"]} for v in valid]

    print(f"Done. Pushed {len(valid)} new verified leads to Snov.io list {SNOV_LIST_ID}.")
    report["pushed_to_snovio"] = len(valid)
    save_daily_report(report)


if __name__ == "__main__":
    main()
