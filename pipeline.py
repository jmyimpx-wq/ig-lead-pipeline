"""
Daily Instagram -> Snov.io lead pipeline.

Flow:
  1. Discover profiles via hashtags (Apify Instagram Hashtag/Search Scraper)
  2. Pull bio/email/contact for new usernames (Apify Instagram Profile Scraper)
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
  APIFY_TOKEN
  APIFY_HASHTAG_ACTOR      confirmed: "instaprism/instagram-hashtag-scraper"
  APIFY_PROFILE_ACTOR      confirmed: "apidojo/instagram-user-scraper" (getFollowers=false)
  APIFY_FOLLOWERS_ACTOR    confirmed: "apidojo/instagram-user-scraper" (getFollowers=true, same actor)
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

import config

STATE_DIR = "state"
SEEN_USERNAMES_FILE = os.path.join(STATE_DIR, "seen_usernames.json")
SEEN_EMAILS_FILE = os.path.join(STATE_DIR, "seen_emails.json")
DISCOVERED_HASHTAGS_FILE = os.path.join(STATE_DIR, "discovered_hashtags.json")
HASHTAG_TAG_REGEX = re.compile(r"#(\w{3,30})")

EMAIL_REGEX = re.compile(r"[a-zA-Z0-9._%+\-]+@[a-zA-Z0-9.\-]+\.[a-zA-Z]{2,}")

APIFY_TOKEN = os.environ["APIFY_TOKEN"]


def _to_api_actor_id(actor_id: str) -> str:
    """Apify's REST API requires 'username~actor-name' in URLs, not 'username/actor-name'
    (the slash form is only used in the Store UI / task JSON). Convert automatically so
    secrets can be stored in either format without breaking API calls."""
    return actor_id.replace("/", "~")


APIFY_HASHTAG_ACTOR = _to_api_actor_id(
    os.environ.get("APIFY_HASHTAG_ACTOR", "instaprism/instagram-hashtag-scraper")
)
APIFY_PROFILE_ACTOR = _to_api_actor_id(
    os.environ.get("APIFY_PROFILE_ACTOR", "apidojo/instagram-user-scraper")
)
APIFY_FOLLOWERS_ACTOR = _to_api_actor_id(
    os.environ.get("APIFY_FOLLOWERS_ACTOR", "apidojo/instagram-user-scraper")
)
SNOV_CLIENT_ID = os.environ["SNOV_CLIENT_ID"]
SNOV_CLIENT_SECRET = os.environ["SNOV_CLIENT_SECRET"]
SNOV_LIST_ID = os.environ["SNOV_LIST_ID"]

APIFY_BASE = "https://api.apify.com/v2"
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


def discover_usernames(hashtags):
    """Run the Apify hashtag scraper synchronously and collect owner usernames.
    Schema confirmed for instaprism/instagram-hashtag-scraper:
    {"hashtags": [...], "limit": N, "extractEmails": true}"""
    run_input = {
        "hashtags": hashtags,
        "limit": 200,
        "extractEmails": True,  # bonus: this actor can pull emails straight from captions too
    }
    resp = requests.post(
        f"{APIFY_BASE}/acts/{APIFY_HASHTAG_ACTOR}/run-sync-get-dataset-items",
        params={"token": APIFY_TOKEN},
        json=run_input,
        timeout=600,
    )
    resp.raise_for_status()
    items = resp.json()
    usernames = set()
    for item in items:
        uname = item.get("ownerUsername") or item.get("username")
        if uname:
            usernames.add(uname)
    return usernames


# ---------- source 2: follower-graph crawl (self-renewing, no tag list needed) ----------

def crawl_seed_followers():
    """Pull followers of large hub accounts using apidojo/instagram-user-scraper
    with getFollowers=true. This pool refreshes on its own as new people follow
    these hubs -- it doesn't exhaust the way a fixed hashtag list does.
    Schema confirmed via real test run: {"startUrls": [...], "getFollowers": bool,
    "getFollowings": bool, "maxItems": N}. Output is a flat list where item 0 is the
    seed account's own profile and the rest are followers, each with "username" directly
    on the item (no nesting)."""
    usernames = set()
    for seed in config.SEED_ACCOUNTS:
        run_input = {
            "startUrls": [f"https://www.instagram.com/{seed}/"],
            "getFollowers": True,
            "getFollowings": False,
            "maxItems": config.MAX_FOLLOWERS_PER_SEED_PER_RUN,
        }
        try:
            resp = requests.post(
                f"{APIFY_BASE}/acts/{APIFY_FOLLOWERS_ACTOR}/run-sync-get-dataset-items",
                params={"token": APIFY_TOKEN},
                json=run_input,
                timeout=600,
            )
            resp.raise_for_status()
            for item in resp.json():
                uname = item.get("username")
                if uname and uname.lower() != seed.lower():  # skip the seed account itself
                    usernames.add(uname)
        except requests.RequestException as e:
            print(f"Follower crawl failed for seed '{seed}': {e}")
    return usernames


# ---------- step 2: profile / bio scraping ----------

def scrape_profiles(usernames):
    """Run apidojo/instagram-user-scraper on a batch of usernames (profile-details mode:
    getFollowers/getFollowings both false), return raw profile dicts.
    Schema confirmed: {"startUrls": [...], "getFollowers": false, "getFollowings": false, "maxItems": N}"""
    start_urls = [f"https://www.instagram.com/{u}/" for u in usernames]
    run_input = {
        "startUrls": start_urls,
        "getFollowers": False,
        "getFollowings": False,
        "maxItems": len(start_urls),
    }
    resp = requests.post(
        f"{APIFY_BASE}/acts/{APIFY_PROFILE_ACTOR}/run-sync-get-dataset-items",
        params={"token": APIFY_TOKEN},
        json=run_input,
        timeout=900,
    )
    resp.raise_for_status()
    return resp.json()


def extract_email(profile):
    for field in ("email", "businessEmail", "publicEmail"):
        val = profile.get(field)
        if val and EMAIL_REGEX.match(val):
            return val.lower().strip()
    bio = profile.get("biography") or profile.get("bio") or ""
    match = EMAIL_REGEX.search(bio)
    return match.group(0).lower().strip() if match else None


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
    """Submit emails for verification, poll for status, return dict {email: status}."""
    results = {}
    batch_size = 100
    for i in range(0, len(emails), batch_size):
        batch = emails[i : i + batch_size]
        # Add for verification
        requests.post(
            f"{SNOV_BASE}/v1/add-emails-for-verification",
            data={"access_token": token, "emails[]": batch},
            timeout=60,
        )
        # NOTE: Snov.io verification is asynchronous for larger batches.
        # Give it time, then poll status. Adjust sleep/retries based on
        # actual turnaround you observe -- check current Snov.io API docs
        # (https://snov.io/api) for the exact status-check method/params,
        # since response shape can change.
        time.sleep(30)
        status_resp = requests.get(
            f"{SNOV_BASE}/v1/get-emails-status",
            params={"access_token": token, "emails[]": batch},
            timeout=60,
        )
        if status_resp.ok:
            for row in status_resp.json().get("data", []):
                results[row.get("email")] = row.get("status")
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

    hashtags = build_today_hashtags()
    print(f"Today's hashtags ({len(hashtags)}): {hashtags}")

    # Source 1: hashtag search (fixed seed + auto-discovered tags)
    hashtag_discovered = discover_usernames(hashtags)
    print(f"Hashtag source: {len(hashtag_discovered)} usernames")

    # Source 2: follower-graph crawl of hub accounts (self-renewing)
    network_discovered = crawl_seed_followers()
    print(f"Network source: {len(network_discovered)} usernames")

    discovered = hashtag_discovered | network_discovered
    new_usernames = list(discovered - seen_usernames)[: config.MAX_PROFILES_PER_RUN]
    print(f"Combined: {len(discovered)} usernames, {len(new_usernames)} new to scrape")

    if not new_usernames:
        print("No new usernames today, exiting.")
        return

    profiles = scrape_profiles(new_usernames)
    harvest_hashtags(profiles)  # feed tomorrow's auto-expanded hashtag pool

    candidates = []  # [{email, username, source_url}]
    for profile in profiles:
        uname = profile.get("username")
        seen_usernames.add(uname) if uname else None
        email = extract_email(profile)
        if email and prefilter(email, seen_emails):
            candidates.append(
                {
                    "email": email,
                    "username": uname,
                    "source_url": f"https://instagram.com/{uname}" if uname else "",
                }
            )

    print(f"{len(candidates)} candidates passed free pre-filter (dedupe/regex/MX)")

    if not candidates:
        save_json_set(SEEN_USERNAMES_FILE, seen_usernames)
        return

    token = snov_get_token()
    emails = [c["email"] for c in candidates]
    statuses = snov_verify_emails(token, emails)

    valid = [c for c in candidates if statuses.get(c["email"]) == "valid"]
    print(f"{len(valid)}/{len(candidates)} verified valid by Snov.io")

    if valid:
        snov_add_to_list(token, valid)
        for v in valid:
            seen_emails.add(v["email"])

    save_json_set(SEEN_USERNAMES_FILE, seen_usernames)
    save_json_set(SEEN_EMAILS_FILE, seen_emails)

    print(f"Done. Pushed {len(valid)} new verified leads to Snov.io list {SNOV_LIST_ID}.")


if __name__ == "__main__":
    main()
