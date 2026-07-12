"""
Hashtag / keyword universe for each target vertical.
Edit freely — add/remove tags as you see which ones convert best.
Keep each vertical's list to ~8-12 tags; hashtag scraping cost scales with tag count.
"""

VERTICALS = {
    "wedding_designer": [
        "weddingdesigner", "weddingplanner", "luxuryweddingplanner",
        "bridalstylist", "weddingstylist", "eventdesigner",
        "weddingcoordinator", "destinationweddingplanner",
    ],
    "floral_designer": [
        "floraldesigner", "weddingflorist", "floralstylist",
        "luxuryflorist", "floralstudio", "eventflorist",
        "flowerdesigner", "floraldesign",
    ],
    "tabletop_buyer": [
        "tablescapedesigner", "tabletopstylist", "eventstyling",
        "luxurytablescape", "tablestyling", "tablescapedesign",
        "tabletopdecor",
    ],
    "home_decor_retail": [
        "homedecorstore", "boutiquehomedecor", "giftshopowner",
        "interiorboutique", "homedecorwholesale", "decorretailer",
        "lifestylestore", "giftshopfinds",
    ],
}

# Optional country/region modifiers — appended as separate hashtag searches
# (e.g. "weddingplannerusa") since IG hashtags don't support geo-filtering directly.
# Leave empty list to skip regional tags and just use the base list above.
REGION_SUFFIXES = ["usa", "uk", "dubai", "saudi", "uae"]

# How many hashtags to actually run per day (rotate through the pool so you
# don't hammer the same tags every run — spreads discovery + cost over the week)
HASHTAGS_PER_RUN = 10

# Results requested per hashtag from the hashtag scraper.
HASHTAG_RESULTS_PER_TAG = 80

# How many NEW usernames (post-dedupe) to send to the profile-detail scraper per run.
# This is your main cost lever — tune based on budget.
MAX_PROFILES_PER_RUN = 25  # TEMP: tiny batch to validate the HikerAPI migration before scaling back up

# Free-provider domains to exclude if you only want business-domain emails.
# Set to empty set [] if you want to keep gmail/yahoo/outlook leads too
# (many small florists/decor shops legitimately use these).
EXCLUDE_FREE_DOMAINS = set()  # e.g. {"gmail.com", "yahoo.com", "hotmail.com"}

# ---------------------------------------------------------------------------
# B2B relevance filter. A profile passes only if its Instagram category, bio,
# or display name contains at least one of these terms -- this is the real
# quality gate, since Instagram's own "business account" flag is unreliable
# (any hobbyist can flip it on) and lets through irrelevant accounts.
RELEVANT_KEYWORDS = [
    # tableware / tabletop -- your actual core product category
    "tableware", "tabletop", "table top", "dinnerware", "table setting",
    "tablescape", "table linen", "placemats", "napkin", "charger plate",
    "crockery", "glassware", "lacquer tray", "serving tray", "chargers",
    # home decor with design/theme -- matches your actual ICP (Mrs Alice,
    # Enchanted Home, Paolo Moschino, WH Hostess style)
    "home decor", "homeware", "home decorator", "home furnishing",
    "home accents", "giftware", "gift shop", "housewares",
    # design aesthetics you specifically work in
    "chinoiserie", "grandmillennial", "toile", "rattan", "ginger jar",
    # interior design (buys/specs decor for clients)
    "interior design", "interior designer", "interior decor", "design studio",
    # wholesale / import (buyer side, not manufacturer side -- see EXCLUDE_KEYWORDS)
    "wholesale buyer", "importer", "import export", "distributor",
    # wedding / floral / event (existing verticals)
    "wedding designer", "wedding planner", "bridal", "floral design",
    "florist", "flower shop", "event design", "event planner", "event stylist",
]
# NOTE: deliberately NOT included: generic terms like "boutique", "retailer",
# "retail store", "lifestyle store" -- these matched irrelevant verticals
# (e.g. a jewelry brand) in testing. Relevance must hit an actual decor/
# tabletop/design term, not just "sells things online."

# Known disposable/throwaway email domains -- reject outright, never worth
# a Snov.io credit or an SMTP check.
DISPOSABLE_EMAIL_DOMAINS = {
    "mailinator.com", "10minutemail.com", "guerrillamail.com", "tempmail.com",
    "yopmail.com", "trashmail.com", "throwawaymail.com", "getnada.com",
    "fakeinbox.com", "sharklasers.com", "dispostable.com", "maildrop.cc",
}

# ---------------------------------------------------------------------------
# Exclusion filter: you are an exporter/manufacturer yourself -- other
# exporters, manufacturers, factories, and suppliers are competitors, not
# buyers, and must never end up in the lead list even if their bio otherwise
# matches the relevance keywords above (e.g. a ceramics factory's bio might
# say "tableware manufacturer"). Checked against username, category, bio, and
# display name. If ANY of these terms appear, the profile is excluded.
EXCLUDE_KEYWORDS = [
    "exporter", "exporters", "export house", "export company",
    "manufacturer", "manufacturers", "manufacturing", "factory", "factories",
    "producer", "production house", "oem", "odm", "oem/odm",
    "supplier", "suppliers", "trading company", "trading co",
    "ready to ship", "factory direct", "wholesale supplier",
    "bulk manufacturer", "china factory", "made in china", "made in india",
    # irrelevant verticals seen in testing -- not your product category
    "jewelry", "jewellery", "necklace", "earrings", "bracelet", "rings",
    "fashion", "apparel", "clothing brand", "boutique fashion",
    "skincare", "beauty", "cosmetics", "makeup",
]

# ---------------------------------------------------------------------------
# AI quality gate (Claude Haiku). Applied AFTER the cheap keyword filters above,
# as a final check on the small pool that already passed -- this keeps API cost
# minimal while catching subtle false positives the keyword list lets through
# (e.g. a jewelry account that also mentions "gift shop" in its bio) and
# borderline judgment calls keyword matching can't make.
ENABLE_AI_QUALITY_GATE = True
AI_MODEL = "claude-haiku-4-5-20251001"

# Second, deeper verification tier: only runs for candidates Haiku already
# approved, and actually reads the lead's real website content (not just their
# Instagram bio) before final confirmation. More expensive per-call than Haiku,
# but only touches the small pool that already survived the cheap first pass.
ENABLE_SONNET_WEBSITE_VERIFY = True
SONNET_MODEL = "claude-sonnet-4-6"

# ---------------------------------------------------------------------------
# AI rescue pass (Tier 0, runs BEFORE the keyword filter's rejection is final):
# profiles that failed ONLY the niche-relevance keyword match (not the website
# or competitor checks, which are stronger/cheaper signals) get a second look
# from Haiku, in case the keyword list was just too narrow for a genuinely
# qualified lead's bio wording.
ENABLE_AI_RESCUE_PASS = True

# ---------------------------------------------------------------------------
# AI research automation: Sonnet (with real web search) periodically proposes
# new hashtags and new seed accounts on its own, so the hashtag/seed pool
# keeps growing without manual curation. Runs on an interval (not every day)
# to control cost. Seed account suggestions are always verified via a real
# Apify profile lookup before being trusted -- AI can name accounts that don't
# exist or misremember follower counts, so nothing gets added on AI's word alone.
ENABLE_AI_HASHTAG_RESEARCH = True
ENABLE_AI_SEED_RESEARCH = True
AI_RESEARCH_INTERVAL_DAYS = 7
MIN_FOLLOWERS_FOR_AI_SUGGESTED_SEED = 20000  # sanity floor once verified via Apify

ICP_DESCRIPTION = """
You are screening Instagram business profiles as potential wholesale buyer leads
for Jimmy Impex, an Indian manufacturer/exporter of tableware and home decor
(rattan, brass, lacquerware, metal decor) with a focus on design-driven pieces
in aesthetics like chinoiserie, grandmillennial, and toile.

QUALIFIED leads look like: boutique home decor / tableware / tablescape retailers,
interior designers who spec decor for clients, gift shops with a curated home
aesthetic, small importers/distributors of home goods, wedding/event/floral
stylists who buy tabletop pieces. Real reference examples of the kind of
business this is for: Mrs. Alice (mrsalice.com), Enchanted Home
(enchantedhome.com), Paolo Moschino (paolomoschino.com), WH Hostess
(whhostess.com), Rail & Stile (therailandstile.com) -- boutique/curated home
and tabletop retailers and designers, generally with a real e-commerce site or
active online shop.

NOT QUALIFIED: other exporters/manufacturers/factories/wholesale suppliers
(competitors, not buyers), jewelry/fashion/beauty/apparel accounts, personal
lifestyle bloggers with no real shop, accounts with no genuine business
substance behind the bio, mass consumer retailers.
"""

# ---------------------------------------------------------------------------
# SOURCE 2: Network/follower-graph expansion.
# These are large "hub" accounts in your niches -- wholesale marketplaces,
# trade associations, big industry hashtag campaigns. Their FOLLOWERS get
# crawled as an additional (and much larger, continuously-refreshing)
# discovery source, separate from hashtag search. New people follow these
# hubs every day, so this pool doesn't dry up the way a fixed hashtag list does.
# Add/replace with real handles relevant to your niches -- these are placeholders.
SEED_ACCOUNTS = [
    "americasmartatl",   # AmericasMart Atlanta - wholesale gift/decor/lifestyle trade show, 177K followers
    "highpointmarket",   # High Point Market - world's largest furniture/home-decor trade show
    "lasvegasmarket",    # Las Vegas Market - wholesale furniture/gift/home decor, 121K followers
    "ny_now",            # NY NOW - New York Gift Show, 105K followers
    "dallasmarket",      # Dallas Market Center - tabletop/housewares/gift trade show, 140K followers
]
# All four accounts above are B2B wholesale marketplaces whose followers are
# verified designers, retailers, wholesalers, and buyers -- exactly your target
# audience. @flowersliving (a single florist's own account) and @target (mass
# consumer retailer) were considered and skipped: their followers are fans/consumers,
# not B2B buyers.
MAX_FOLLOWERS_PER_SEED_PER_RUN = 500  # cost lever for network crawl

# ---------------------------------------------------------------------------
# SOURCE 1 support: auto hashtag discovery.
# When True, the pipeline harvests hashtags used in scraped bios/captions,
# scores them by frequency, and folds the top ones into future runs'
# hashtag pool automatically (persisted in state/discovered_hashtags.json).
AUTO_EXPAND_HASHTAGS = True
MAX_AUTO_HASHTAGS_TO_ADD_PER_RUN = 10
MIN_HASHTAG_FREQUENCY_TO_ADOPT = 3  # must appear this many times before being adopted
