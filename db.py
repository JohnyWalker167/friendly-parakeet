from motor.motor_asyncio import AsyncIOMotorClient
from config import MONGO_URI


# MongoDB setup
mongo = AsyncIOMotorClient(MONGO_URI)
db = mongo["sharing_bot"]
files_col = db["files"]
tmdb_col = db["tmdb"]
tokens_col = db["tokens"]
auth_users_col = db["auth_users"]
otp_col = db["otp"]
allowed_channels_col = db["allowed_channels"]
users_col = db["users"]
comments_col = db["comments"]
genres_col = db["genres"]
stars_col = db["stars"]
directors_col = db["directors"]
languages_col = db["languages"]


''' JSON setup for Atlas Search'''

'''
This index definition should be applied to BOTH the `files_col` and `tmdb_col` collections in the Atlas UI.

{
  "analyzer": "custom_analyzer",
  "searchAnalyzer": "custom_analyzer",
  "mappings": {
    "dynamic": false,
    "fields": {
      "title": {
        "type": "string"
      },
      "file_name": {
        "type": "string"
      }
    }
  },
  "analyzers": [
    {
      "name": "custom_analyzer",
      "tokenizer": {
        "type": "regexSplit",
        "pattern": "[\\s._-]+"
      },
      "tokenFilters": [
        {
          "type": "lowercase"
        }
      ]
    }
  ]
}

'''
