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
MAX_PROFILES_PER_RUN = 650

# Free-provider domains to exclude if you only want business-domain emails.
# Set to empty set [] if you want to keep gmail/yahoo/outlook leads too
# (many small florists/decor shops legitimately use these).
EXCLUDE_FREE_DOMAINS = set()  # e.g. {"gmail.com", "yahoo.com", "hotmail.com"}

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
    "jimmyimpex1994",    # small known account, kept as a sanity-check seed
]
# All four trade-show accounts above are B2B wholesale marketplaces whose followers
# are verified designers, retailers, wholesalers, and buyers -- exactly your target
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
