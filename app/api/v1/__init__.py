"""API v1 router assembly."""

from fastapi import APIRouter

from app.api.v1.audit import router as audit_router
from app.api.v1.auth import router as auth_router
from app.api.v1.menu import router as menu_router
from app.api.v1.orders import router as orders_router
from app.api.v1.payments import router as payments_router
from app.api.v1.qr import router as qr_router
from app.api.v1.service_requests import router as service_requests_router
from app.api.v1.sessions import router as sessions_router

api_v1_router = APIRouter()
api_v1_router.include_router(auth_router)
api_v1_router.include_router(audit_router)
api_v1_router.include_router(qr_router)
api_v1_router.include_router(sessions_router)
api_v1_router.include_router(menu_router)
api_v1_router.include_router(orders_router)
api_v1_router.include_router(service_requests_router)
api_v1_router.include_router(payments_router)

__all__ = ["api_v1_router"]
