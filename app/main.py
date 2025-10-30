from fastapi import FastAPI
from app.api import pois
from app.api import itinerary
from app.core.config import settings
from fastapi.middleware.cors import CORSMiddleware

app = FastAPI(title="Fika API", version="0.1.0")

app.add_middleware(
    CORSMiddleware,
    allow_origins=settings.CORS_ORIGINS,
    allow_credentials=True,
    allow_methods=["*"],
    allow_headers=["*"],
)

# Routers
app.include_router(pois.router)
app.include_router(itinerary.router)

@app.get("/")
def read_root():
    return {
        "message": "Fika API is running.",
        "status": "healthy",
        "version": "0.1.0"
    }