
import asyncio
import aiohttp
from config import TMDB_API_KEY, logger
from db import tmdb_col
from tmdb import POSTER_BASE_URL

async def get_poster_path(session, tmdb_type, tmdb_id):
    # Optimize by using append_to_response=images
    api_url = f"https://api.themoviedb.org/3/{tmdb_type}/{tmdb_id}?api_key={TMDB_API_KEY}&language=en-US"
    try:
        async with session.get(api_url) as resp:
            if resp.status == 200:
                data = await resp.json()
                poster_path = data.get('poster_path')
                return poster_path
    except Exception as e:
        logger.error(f"Error fetching poster for {tmdb_type} {tmdb_id}: {e}")
        return None

async def migrate_to_poster(reply_func=None):
    async with aiohttp.ClientSession() as session:
        cursor = tmdb_col.find({})
        total_docs = await tmdb_col.count_documents({})
        msg = f"Starting migration for {total_docs} documents..."
        logger.info(msg)
        if reply_func:
            await reply_func(msg)
        
        count = 0
        updated = 0
        
        async for doc in cursor:
            count += 1
            tmdb_id = doc.get('tmdb_id')
            tmdb_type = doc.get('tmdb_type')
            
            if not tmdb_id or not tmdb_type:
                logger.warning(f"Skipping document with missing TMDB info: {doc.get('_id')}")
                continue
                
            poster_path = await get_poster_path(session, tmdb_type, tmdb_id)
            
            if poster_path:
                await tmdb_col.update_one(
                    {"_id": doc["_id"]},
                    {"$set": {
                        "poster_path": poster_path,
                    }}
                )
                updated += 1
                if updated % 10 == 0:
                    msg = f"Progress: {count}/{total_docs} processed, {updated} updated."
                    logger.info(msg)
                    if reply_func:
                        await reply_func(msg)
            else:
                logger.warning(f"No poster found for {tmdb_type} {tmdb_id}")
            
            # Rate limiting
            await asyncio.sleep(0.25)
            
        final_msg = f"Migration complete. Total processed: {count}, Total updated: {updated}"
        logger.info(final_msg)
        if reply_func:
            await reply_func(final_msg)

if __name__ == "__main__":
    loop = asyncio.get_event_loop()
    loop.run_until_complete(migrate_to_poster())
