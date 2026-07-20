"""Versioned API router."""

from fastapi import APIRouter

from app.api.routes.health import router as health_router
from app.api.routes.portfolio import router as portfolio_router
from app.api.routes.taxonomy import router as taxonomy_router

api_router = APIRouter()
api_router.include_router(health_router)
api_router.include_router(portfolio_router)
api_router.include_router(taxonomy_router)
