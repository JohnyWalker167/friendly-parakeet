from cachetools import TTLCache

# Unified in-memory cache
cache = TTLCache(maxsize=1000, ttl=86400)

def invalidate_cache():
    """Clears the entire in-memory cache."""
    cache.clear()
