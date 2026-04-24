
import random
import string
from cache import cache

def generate_query_id(length=8):
    """Generate a short random string for query IDs."""
    return ''.join(random.choices(string.ascii_letters + string.digits, k=length))

def store_query(query):
    """
    Store the query and return its short ID.
    """
    query_id = generate_query_id()
    while query_id in cache:
        query_id = generate_query_id()
    cache[query_id] = query
    return query_id

def get_query_by_id(query_id):
    """
    Retrieve the query string by its ID.
    Returns "" if not found or expired.
    """
    return cache.get(query_id, "")
