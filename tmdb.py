
import re
import aiohttp
import asyncio
import PTN
from imdbinfo import get_movie
from config import TMDB_API_KEY, logger
from db import tmdb_col, genres_col, stars_col, directors_col, languages_col
from utility import remove_redandent

POSTER_BASE_URL = 'https://image.tmdb.org/t/p/original'

GENRE_EMOJI_MAP = { 
    "Action": "🥊", "Adventure": "🌋", "Animation": "🎬", "Comedy": "😂", 
    "Crime": "🕵️", "Documentary": "🎥", "Drama": "🎭", "Family": "👨‍👩‍👧‍👦", 
    "Fantasy": "🧙", "History": "📜", "Horror": "👻", "Music": "🎵", 
    "Mystery": "🕵️‍♂️", "Romance": "❤️", "ScienceFiction": "🤖", 
    "Sci-Fi": "🤖", "SciFi": "🤖", "TV Movie": "📺", "Thriller": "🔪", 
    "War": "⚔️", "Western": "🤠", "Sport": "🏆", "Biography": "📖" 
}

def format_duration(minutes):
    if not minutes:
        return ""
    hours, mins = divmod(minutes, 60)
    return f"{hours}h {mins}m" if hours else f"{mins}m"

def clean_genre_name(genre): 
    return re.sub(r'[^A-Za-z0-9]', '', genre) 

def genre_tag_with_emoji(genre): 
    clean_name = clean_genre_name(genre) 
    emoji = GENRE_EMOJI_MAP.get(clean_name, "") 
    return f"#{clean_name}{' ' + emoji if emoji else ''}" 

def extract_genres(data): 
    genres = [] 
    for genre in data.get('genres', []): 
        if '&' in genre['name']: 
            parts = [g.strip() for g in genre['name'].split('&')] 
            genres.extend(parts) 
        else: 
            genres.append(genre['name']) 
    return genres 

async def get_or_create_person(person_data, collection):
    person = await collection.find_one({"name": person_data["name"]})
    if person:
        return person["_id"]
    else:
        result = await collection.insert_one(person_data)
        return result.inserted_id

async def get_or_create_genre(genre_name):
    genre = await genres_col.find_one({"name": genre_name})
    if genre:
        return genre["_id"]
    else:
        result = await genres_col.insert_one({"name": genre_name})
        return result.inserted_id

async def get_or_create_language(language_name):
    language = await languages_col.find_one({"name": language_name})
    if language:
        return language["_id"]
    else:
        result = await languages_col.insert_one({"name": language_name})
        return result.inserted_id

async def get_imdb_details(imdb_id):
    """
    Fetch rating and plot using IMDb ID using the imdbinfo library.
    """
    if not imdb_id:
        return {}
    if not imdb_id.startswith('tt'):
        imdb_id = f"tt{imdb_id}"

    try:
        movie = await asyncio.to_thread(get_movie, imdb_id)
        if movie:
            return {
                "rating": float(movie.rating) if movie.rating else None,
                "plot": movie.plot
            }
    except Exception as e:
        logger.error(f"imdbinfo error for {imdb_id}: {e}")

    return {}
        
async def get_cast_and_crew(session, tmdb_type, tmdb_id):
    cast = []
    directors = []
    credits_url = f'https://api.themoviedb.org/3/{tmdb_type}/{tmdb_id}/credits?api_key={TMDB_API_KEY}&language=en-US'
    async with session.get(credits_url) as response:
        credits_data = await response.json()
        for member in credits_data.get('cast', [])[:6]:
            cast.append({'name': member['name'], 'profile_path': member['profile_path']})
        for member in credits_data.get('crew', []):
            if member['job'] == 'Director':
                directors.append({'name': member['name'], 'profile_path': member['profile_path']})
    return {"cast": cast, "directors": directors}

async def get_tv_imdb_id(session, tv_id):
    url = f"https://api.themoviedb.org/3/tv/{tv_id}/external_ids?api_key={TMDB_API_KEY}"
    async with session.get(url) as resp:
        data = await resp.json()
        return data.get("imdb_id")

async def get_info(tmdb_type, tmdb_id):
    api_url = f"https://api.themoviedb.org/3/{tmdb_type}/{tmdb_id}?api_key={TMDB_API_KEY}&language=en-US"

    async with aiohttp.ClientSession() as session:
        async with session.get(api_url) as detail_response:
            if detail_response.status != 200:
                return {"message": f"Error: TMDB API returned status {detail_response.status}"}
            data = await detail_response.json()

        imdb_id = data.get('imdb_id') if tmdb_type == 'movie' else await get_tv_imdb_id(session, tmdb_id)
        imdb_info = await get_imdb_details(imdb_id) if imdb_id else {}
        cast_crew = await get_cast_and_crew(session, tmdb_type, tmdb_id)
        trailer_url = None
        video_url = f'https://api.themoviedb.org/3/{tmdb_type}/{tmdb_id}/videos?api_key={TMDB_API_KEY}'
        async with session.get(video_url) as video_response:
            video_data = await video_response.json()
            for video in video_data.get('results', []):
                if video['site'] == 'YouTube' and video['type'] == 'Trailer':
                    trailer_url = f"https://www.youtube.com/watch?v={video['key']}"
                    break

        info = {
            "tmdb_id": tmdb_id,
            "tmdb_type": tmdb_type,
            "imdb_id": imdb_id,
            "title": data.get('title') if tmdb_type == 'movie' else data.get('name'),
            "year": (data.get('release_date', '')[:4] if tmdb_type == 'movie' else data.get('first_air_date', '')[:4]),
            "rating": imdb_info.get('rating'),
            "plot": truncate_overview(imdb_info.get('plot')),
            "poster_path":  data.get('poster_path'),
            "poster_url": f"{POSTER_BASE_URL}{data.get('poster_path')}" if data.get('poster_path') else None,
            "trailer_url": trailer_url,
            "genres": extract_genres(data),
            "cast": cast_crew.get('cast', []),
            "directors": cast_crew.get('directors', []),
            "spoken_languages": [lang.get('name', '') for lang in data.get('spoken_languages', [])],
            "runtime": data.get('runtime'),
        }

        if tmdb_type == 'tv':
            info['directors'] = [{'name': creator['name'], 'profile_path': creator['profile_path']} for creator in data.get('created_by', [])]
            seasons = []
            for season in data.get('seasons', []):
                if season.get('season_number') == 0:
                    continue
                seasons.append({'season_number': season.get('season_number'), 'poster_path': season.get('poster_path'), 'episode_count': season.get('episode_count')})
            info['seasons'] = seasons

        info['message'] = await format_tmdb_info(info, data)
        return info

async def format_tmdb_info(info, data):
    tmdb_type = info['tmdb_type']
    if tmdb_type == 'movie':
        genre_tags = " ".join([genre_tag_with_emoji(g) for g in info['genres']])
        director = ', '.join([d['name'] for d in info['directors']])
        starring = ", ".join([s['name'] for s in info['cast']])
        spoken_languages = ", ".join(info['spoken_languages'])
        runtime = format_duration(info['runtime'])
        rating_str = f"{info['rating']}" if info['rating'] is not None else None
        message = f"<b>🎬 Title:</b> {info['title']}\n"
        message += f"<b>📆 Release:</b> {info['year']}\n" if info['year'] else ""
        message += f"<b>⭐ Rating:</b> {rating_str} / 10\n" if rating_str else ""
        message += f"<b>⏳️ Duration:</b> {runtime}\n" if runtime else ""
        message += f"<b>🅰️ Languages:</b> {spoken_languages}\n" if spoken_languages else ""
        message += f"<b>🔞 Adult:</b> Yes\n" if data.get('adult') else ""
        message += f"<b>⚙️ Genre:</b> {genre_tags}\n" if genre_tags else ""
        message += "\n"
        message += f"<b>📝 Story:</b> {info['plot']}\n\n" if info['plot'] else ""
        message += f"<b>🎬 Director:</b> {director}\n" if director else ""
        message += f"<b>🎭 Stars:</b> {starring}\n" if starring else ""
        return message.strip()
    elif tmdb_type == 'tv':
        genre_tags = " ".join([genre_tag_with_emoji(g) for g in info['genres']])
        director = ", ".join([d['name'] for d in info['directors']])
        starring = ", ".join([s['name'] for s in info['cast']])
        spoken_languages = ", ".join(info['spoken_languages'])
        rating_str = f"{info['rating']}" if info['rating'] is not None else None
        message = f"<b>📺 Title:</b> {info['title']}\n"
        message += f"<b>📅 Release:</b> {info['year']}\n" if info['year'] else ""
        message += f"<b>📺 Seasons:</b> {data.get('number_of_seasons', '')}\n" if data.get('number_of_seasons') else ""
        message += f"<b>📺 Episodes:</b> {data.get('number_of_episodes', '')}\n" if data.get('number_of_episodes') else ""
        message += f"<b>⭐ Rating:</b> {rating_str} / 10\n" if rating_str else ""
        message += f"<b>🅰️ Languages:</b> {spoken_languages}\n" if spoken_languages else ""
        message += f"<b>🔞 Adult:</b> Yes\n" if data.get('adult') else ""
        message += f"<b>⚙️ Genre:</b> {genre_tags}\n" if genre_tags else ""
        message += "\n"
        message += f"<b>📝 Story:</b> {info['plot']}\n\n" if info['plot'] else ""
        message += f"<b>🎬 Director:</b> {director}\n" if director else ""
        message += f"<b>🎭 Stars:</b> {starring}\n" if starring else ""
        return message.strip()
    else:
        return "Unknown type. Unable to format information."

async def format_tmdb_info_from_db(tmdb_document):
    # Fetch names from referenced collections
    genre_names = [genre['name'] async for genre in genres_col.find({'_id': {'$in': tmdb_document.get('genres', [])}})]
    star_names = [star['name'] async for star in stars_col.find({'_id': {'$in': tmdb_document.get('cast', [])}})]
    director_names = [director['name'] async for director in directors_col.find({'_id': {'$in': tmdb_document.get('directors', [])}})]
    language_names = [lang['name'] async for lang in languages_col.find({'_id': {'$in': tmdb_document.get('spoken_languages', [])}})]

    tmdb_type = tmdb_document.get('tmdb_type')
    if not tmdb_type or tmdb_type not in ['movie', 'tv']:
        return "Unknown type. Unable to format information."

    genre_tags = " ".join([genre_tag_with_emoji(g) for g in genre_names])
    director = ', '.join(director_names)
    starring = ", ".join(star_names)
    spoken_languages = ", ".join(language_names)
    rating_str = f"{tmdb_document['rating']}" if tmdb_document.get('rating') is not None else None

    title_emoji = "🎬" if tmdb_type == 'movie' else "📺"
    message = f"<b>{title_emoji} Title:</b> {tmdb_document['title']}\n"
    message += f"<b>📆 Release:</b> {tmdb_document['year']}\n" if tmdb_document.get('year') else ""

    if tmdb_type == 'movie':
        runtime = format_duration(tmdb_document.get('runtime'))
        message += f"<b>⏳️ Duration:</b> {runtime}\n" if runtime else ""
    elif tmdb_type == 'tv':
        seasons_data = tmdb_document.get('seasons', [])
        num_seasons = len(seasons_data)
        num_episodes = sum(s.get('episode_count', 0) for s in seasons_data)
        message += f"<b>📺 Seasons:</b> {num_seasons}\n" if num_seasons > 0 else ""
        message += f"<b>📺 Episodes:</b> {num_episodes}\n" if num_episodes > 0 else ""

    message += f"<b>⭐ Rating:</b> {rating_str} / 10\n" if rating_str else ""
    message += f"<b>🅰️ Languages:</b> {spoken_languages}\n" if spoken_languages else ""
    message += f"<b>⚙️ Genre:</b> {genre_tags}\n" if genre_tags else ""
    message += "\n"
    message += f"<b>📝 Story:</b> {tmdb_document['plot']}\n\n" if tmdb_document.get('plot') else ""
    message += f"<b>🎬 Director:</b> {director}\n" if director else ""
    message += f"<b>🎭 Stars:</b> {starring}\n" if starring else ""
    
    return message.strip()

async def upsert_tmdb_info(tmdb_id, tmdb_type, info):
    genre_ids = [await get_or_create_genre(genre) for genre in info.get("genres", [])]
    star_ids = [await get_or_create_person(star, stars_col) for star in info.get("cast", [])[:6]]
    director_ids = [await get_or_create_person(director, directors_col) for director in info.get("directors", [])[:6]]
    language_ids = [await get_or_create_language(lang) for lang in info.get("spoken_languages", [])]
    
    tmdb_document = {
        "tmdb_id": info["tmdb_id"],
        "tmdb_type": info["tmdb_type"],
        "title": info["title"],
        "year": info["year"],
        "rating": info["rating"],
        "plot": info["plot"],
        "poster_path": info["poster_path"],
        "trailer_url": info["trailer_url"],
        "imdb_id": info["imdb_id"],
        "genres": genre_ids,
        "cast": star_ids,
        "directors": director_ids,
        "spoken_languages": language_ids,
        "runtime": info.get("runtime")
    }
    if tmdb_type == 'tv':
        tmdb_document['seasons'] = info.get('seasons', [])
    await tmdb_col.update_one(
        {"tmdb_id": tmdb_id, "tmdb_type": tmdb_type},
        {"$set": tmdb_document},
        upsert=True
    )

async def process_tmdb_info(bot, file_info):
    try:
        title = remove_redandent(file_info["file_name"])
        parsed_data = PTN.parse(title)
        title = parsed_data.get("title", "").replace("_", " ").replace("-", " ").replace(":", " ")
        title = ' '.join(title.split())
        aka_pattern = r'\sA[.\s]?K[.\s]?A[.]?\s+'
        if re.search(aka_pattern, title, re.IGNORECASE):
            title = re.split(aka_pattern, title, maxsplit=1, flags=re.IGNORECASE)[0].strip()
        year = parsed_data.get("year")
        season = parsed_data.get("season")
        episode = parsed_data.get("episode")
        if season:
            file_info["season_number"] = season
        if season or episode:
            result = await get_tv_id(title, year)
        else:
            result = await get_movie_id(title, year)
        if not result:
            return None
        
        tmdb_id, tmdb_type = result['id'], result['media_type']
        file_info['tmdb_id'] = tmdb_id
        file_info['tmdb_type'] = tmdb_type
        exists = await tmdb_col.find_one({"tmdb_id": tmdb_id, "tmdb_type": tmdb_type})
        if not exists:
            info = await get_info(tmdb_type, tmdb_id)
            if info and not ("message" in info and info["message"].startswith("Error")):
                await upsert_tmdb_info(tmdb_id, tmdb_type, info)
                await asyncio.sleep(1)
        return tmdb_id, tmdb_type
    except Exception as e:
        logger.error(f"Info not found {title}: {e}")
        return None

async def get_movie_id(title, year=None):
    search_url = f'https://api.themoviedb.org/3/search/movie?api_key={TMDB_API_KEY}&query={title}'
    if year:
        search_url += f'&year={year}'
    async with aiohttp.ClientSession() as session:
        async with session.get(search_url) as response:
            data = await response.json()
            if data.get('results'):
                return {'id': data['results'][0]['id'], 'media_type': 'movie'}
    return None

async def get_tv_id(title, year=None):
    search_url = f'https://api.themoviedb.org/3/search/tv?api_key={TMDB_API_KEY}&query={title}'
    if year:
        search_url += f'&first_air_date_year={year}'
    async with aiohttp.ClientSession() as session:
        async with session.get(search_url) as response:
            data = await response.json()
            if data.get('results'):
                return {'id': data['results'][0]['id'], 'media_type': 'tv'}
    return None

def truncate_overview(overview):
    if not overview:
        return None
    MAX_OVERVIEW_LENGTH = 600
    if len(overview) > MAX_OVERVIEW_LENGTH:
        return overview[:MAX_OVERVIEW_LENGTH] + "..."
    return overview
