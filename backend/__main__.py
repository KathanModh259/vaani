#!/usr/bin/env python3
"""Entry point for the AI Phone Receptionist."""

from app import app
from scripts.config import settings

if __name__ == "__main__":
    import uvicorn
    uvicorn.run(
        app,
        host=settings.HOST,
        port=settings.PORT,
        reload=settings.DEBUG,
        access_log=False,
    )
