import logging
from fastapi import FastAPI
from fastapi.responses import JSONResponse
from fastapi.middleware.cors import CORSMiddleware
from config import CF_DOMAIN

api = FastAPI()

api.add_middleware(
    CORSMiddleware,
    allow_origins=[f"{CF_DOMAIN}"],  # Allow all origins
    allow_credentials=True,
    allow_methods=["*"],
    allow_headers=["*"],
)

@api.get("/")
async def root():
    return JSONResponse({"message": "👋 Hola Amigo!"})
