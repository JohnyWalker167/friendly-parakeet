
class Cache(dict):
    def invalidate_cache(self):
        self.clear()

cache = Cache()

def invalidate_cache():
    cache.invalidate_cache()
