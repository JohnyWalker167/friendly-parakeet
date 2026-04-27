
from cachetools import TTLCache

# Cache for 10 minutes, max 1000 items
cache = TTLCache(maxsize=1000, ttl=600)

def invalidate_cache():
    cache.clear()
