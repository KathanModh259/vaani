#!/usr/bin/env python3
"""Compatibility entry point: `uvicorn main:app` and `python main.py`."""

from app import app

if __name__ == "__main__":
    from scripts.config import settings
    import uvicorn

    uvicorn.run(
        app,
        host=settings.HOST,
        port=settings.PORT,
        reload=settings.DEBUG,
        access_log=False,
    )
