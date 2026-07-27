import logging
from fastapi import APIRouter

logger = logging.getLogger(__name__)
router = APIRouter()


@router.get("/dashboard/stats")
async def dashboard_stats():
    return {
        "active_calls": 0,
        "today_calls": 0,
        "total_calls": 0,
        "booking_rate": 0,
        "avg_duration": 0,
        "calls_by_day": [],
        "language_breakdown": [],
        "recent_calls": [],
    }
