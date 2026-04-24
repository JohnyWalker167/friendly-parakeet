#!/usr/bin/env python3
import asyncio
import logging
from motor.motor_asyncio import AsyncIOMotorClient
from tmdb import get_info
from config import MONGO_URI, TMDB_API_KEY

# Configure logging
logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s - %(levelname)s - %(message)s",
    handlers=[logging.StreamHandler()],
)
logger = logging.getLogger(__name__)

async def main():
    """
    Main function to find and update TMDB documents with missing ratings.
    """
    mongo_uri = MONGO_URI
    tmdb_api_key = TMDB_API_KEY

    if not mongo_uri or not tmdb_api_key:
        logger.error("MONGO_URI and TMDB_API_KEY environment variables must be set.")
        return

    try:
        client = AsyncIOMotorClient(mongo_uri)
        db = client["sharing_bot"]
        tmdb_col = db["tmdb"]
        logger.info("Successfully connected to the database.")
    except Exception as e:
        logger.error(f"Failed to connect to the database: {e}")
        return
        
    query = {
        "tmdb_id": {"$exists": True, "$ne": None},
        "tmdb_type": {"$exists": True, "$ne": None},
    }
    
    try:
        docs_to_update = await tmdb_col.find(query).to_list(length=None)
        total_docs = len(docs_to_update)
        logger.info(f"Found {total_docs} documents with missing ratings.")

        updated_count = 0

        for i, doc in enumerate(docs_to_update):
            tmdb_id = doc.get("tmdb_id")
            tmdb_type = doc.get("tmdb_type")

            if not tmdb_id or not tmdb_type:
                logger.warning(
                    f"Skipping document with missing tmdb_id or tmdb_type: {doc.get('_id')}"
                )
                continue

            try:
                logger.info(
                    f"({i+1}/{total_docs}) Fetching info for {tmdb_type}/{tmdb_id}..."
                )
                info = await get_info(tmdb_type, tmdb_id)

                if info and not info.get("message", "").startswith("Error"):
                    update_data = {
                        "title": info.get("title"),
                        "poster_path": info.get("poster_path"),
                        "year": info.get("year"),
                        "rating": info.get("rating"),
                        "plot": info.get("plot"),
                        "trailer_url": info.get("trailer_url"),
                        "imdb_id": info.get("imdb_id"),
                    }

                    update_data = {k: v for k, v in update_data.items() if v is not None}

                    await tmdb_col.update_one({"_id": doc["_id"]}, {"$set": update_data})
                    logger.info(f"Successfully updated {tmdb_type}/{tmdb_id}.")
                    updated_count += 1
                else:
                    logger.error(
                        f"Failed to fetch or got error for {tmdb_type}/{tmdb_id}. Response: {info}"
                    )

                await asyncio.sleep(2)  # basic rate-limit protection

            except Exception as e:
                logger.error(
                    f"An error occurred while processing {tmdb_type}/{tmdb_id}: {e}"
                )

        logger.info(
            f"Update complete. {updated_count}/{total_docs} documents were updated."
        )

    except Exception as e:
        logger.error(f"An error occurred while querying the database: {e}")
    finally:
        client.close()
        logger.info("Database connection closed.")


if __name__ == "__main__":
    logger.info("Starting the rating update script.")
    asyncio.run(main())
    logger.info("Script finished.")


