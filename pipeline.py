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
    os.environ.get("APIFY_HASHTAG_ACTOR", "apify/instagram-hashtag-scraper")
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


def run_apify_actor(actor_id, run_input, poll_interval=5, max_wait=1800):
    """Start an Apify actor run asynchronously and poll for completion, instead of using
    the synchronous run-sync-get-dataset-items endpoint. The sync endpoint holds one HTTP
    connection open for the entire run duration, which gets killed by intermediate proxies
    on longer runs (observed: RemoteDisconnected around the ~5 minute mark). This async
    start+poll+fetch pattern avoids that entirely."""
    start_resp = requests.post(
        f"{APIFY_BASE}/acts/{actor_id}/runs",
        params={"token": APIFY_TOKEN},
        json=run_input,
        timeout=30,
    )
    start_resp.raise_for_status()
    run_id = start_resp.json()["data"]["id"]

    started_at = time.time()
    while time.time() - started_at < max_wait:
        status_resp = requests.get(
            f"{APIFY_BASE}/actor-runs/{run_id}",
            params={"token": APIFY_TOKEN},
            timeout=30,
        )
        status_resp.raise_for_status()
        run_data = status_resp.json()["data"]
        status = run_data["status"]

        if status == "SUCCEEDED":
            dataset_id = run_data["defaultDatasetId"]
            print(f"  [debug] run {run_id} succeeded, dataset {dataset_id}")
            items_resp = requests.get(
                f"{APIFY_BASE}/datasets/{dataset_id}/items",
                params={"token": APIFY_TOKEN, "format": "json", "clean": "true"},
                timeout=60,
            )
            items_resp.raise_for_status()
            return items_resp.json()

        if status in ("FAILED", "ABORTED", "TIMED-OUT"):
            raise RuntimeError(f"Apify run {run_id} ({actor_id}) ended with status {status}")

        time.sleep(poll_interval)

    raise TimeoutError(f"Apify run {run_id} ({actor_id}) did not finish within {max_wait}s")


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
    """Run the Apify hashtag scraper and collect owner usernames.
    Schema confirmed for the official apify/instagram-hashtag-scraper (Maintained by Apify):
    {"hashtags": [...], "keywordSearch": false, "resultsLimit": N}"""
    run_input = {
        "hashtags": hashtags,
        "keywordSearch": False,
        "resultsLimit": config.HASHTAG_RESULTS_PER_TAG,
    }
    items = run_apify_actor(APIFY_HASHTAG_ACTOR, run_input)
    print(f"  [debug] hashtag actor returned {len(items)} raw items")
    if items:
        print(f"  [debug] sample item keys: {list(items[0].keys())}")
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
            items = run_apify_actor(APIFY_FOLLOWERS_ACTOR, run_input)
            print(f"  [debug] follower actor returned {len(items)} raw items for seed '{seed}'")
            if items:
                print(f"  [debug] sample item keys: {list(items[0].keys())}")
            for item in items:
                uname = item.get("username")
                if uname and uname.lower() != seed.lower():  # skip the seed account itself
                    usernames.add(uname)
        except (requests.RequestException, RuntimeError, TimeoutError) as e:
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
    return run_apify_actor(APIFY_PROFILE_ACTOR, run_input, max_wait=3600)


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
    skipped_non_business = 0
    skipped_irrelevant = 0
    skipped_competitor = 0
    for profile in profiles:
        uname = profile.get("username")
        seen_usernames.add(uname) if uname else None

        # Signal 1: Instagram's own business-account flag (weak on its own).
        if not profile.get("isBusiness", False):
            skipped_non_business += 1
            continue

        # Exclude competitors: exporters/manufacturers/factories/suppliers are
        # sellers like you, not buyers -- never leads, regardless of niche match.
        if is_excluded_supplier(profile):
            skipped_competitor += 1
            continue

        # Signal 2: real quality gate -- does this account actually match our
        # target verticals (tableware, interior design, retail, wholesale/import,
        # wedding/floral/event)? This is what filters out "business accounts"
        # that aren't actually relevant businesses.
        if not is_relevant_b2b_lead(profile):
            skipped_irrelevant += 1
            continue

        email = extract_email(profile)
        if email and prefilter(email, seen_emails):
            candidates.append(
                {
                    "email": email,
                    "username": uname,
                    "source_url": f"https://instagram.com/{uname}" if uname else "",
                }
            )
    print(
        f"  [debug] skipped {skipped_non_business} non-business, "
        f"{skipped_competitor} competitor (exporter/manufacturer), "
        f"{skipped_irrelevant} off-niche profiles"
    )

    print(f"{len(candidates)} candidates passed free pre-filter (dedupe/regex/MX)")

    # Save state now -- usernames/profiles already scraped (and Apify credits spent)
    # should never be reprocessed, even if the verification step below fails.
    save_json_set(SEEN_USERNAMES_FILE, seen_usernames)

    if not candidates:
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

    valid = list(own_valid)

    if own_unknown:
        try:
            token = snov_get_token()
            emails = [c["email"] for c in own_unknown]
            statuses = snov_verify_emails(token, emails)
            fallback_valid = [c for c in own_unknown if statuses.get(c["email"]) == "valid"]
            print(f"Snov.io fallback: {len(fallback_valid)}/{len(own_unknown)} verified valid")
            valid.extend(fallback_valid)
        except requests.RequestException as e:
            print(f"Snov.io fallback step failed (unknown-status candidates dropped this run): {e}")

    print(f"{len(valid)}/{len(candidates)} total verified valid")

    if valid:
        token = snov_get_token()
        snov_add_to_list(token, valid)
        for v in valid:
            seen_emails.add(v["email"])
        save_json_set(SEEN_EMAILS_FILE, seen_emails)

    print(f"Done. Pushed {len(valid)} new verified leads to Snov.io list {SNOV_LIST_ID}.")


if __name__ == "__main__":
    main()
