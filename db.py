from motor.motor_asyncio import AsyncIOMotorClient
from config import MONGO_URI


# MongoDB setup
mongo = AsyncIOMotorClient(MONGO_URI)
db = mongo["sharing_bot"]
files_col = db["files"]
auth_users_col = db["auth_users"]
otp_col = db["otp"]
allowed_channels_col = db["allowed_channels"]
users_col = db["users"]

''' JSON setup for Atlas Search'''

'''
This index definition should be applied to BOTH the `files_col` collection in the Atlas UI.

{
  "analyzer": "custom_analyzer",
  "searchAnalyzer": "custom_analyzer",
  "mappings": {
    "dynamic": false,
    "fields": {
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
