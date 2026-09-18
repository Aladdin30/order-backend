"""API v1 router assembly."""

from fastapi import APIRouter

from app.api.v1.analytics import router as analytics_router
from app.api.v1.audit import router as audit_router
from app.api.v1.auth import router as auth_router
from app.api.v1.branches import router as branches_router
from app.api.v1.floor import router as floor_router
from app.api.v1.financials import router as financials_router
from app.api.v1.kds import router as kds_router
from app.api.v1.menu import router as menu_router
from app.api.v1.orders import router as orders_router
from app.api.v1.payments import router as payments_router
from app.api.v1.pos import router as pos_router
from app.api.v1.qr import router as qr_router
from app.api.v1.qr_export import router as qr_export_router
from app.api.v1.service_requests import router as service_requests_router
from app.api.v1.sessions import router as sessions_router
from app.api.v1.staff_menu import router as staff_menu_router
from app.api.v1.staff_stations import router as staff_stations_router
from app.api.v1.websocket import router as websocket_router

api_v1_router = APIRouter()
api_v1_router.include_router(auth_router)
api_v1_router.include_router(branches_router)
api_v1_router.include_router(audit_router)
api_v1_router.include_router(qr_router)
api_v1_router.include_router(qr_export_router, prefix="/qr-export")
api_v1_router.include_router(sessions_router)
api_v1_router.include_router(menu_router)
api_v1_router.include_router(staff_menu_router)
api_v1_router.include_router(staff_stations_router)
api_v1_router.include_router(orders_router)
api_v1_router.include_router(pos_router)
api_v1_router.include_router(floor_router, prefix="/floor")
api_v1_router.include_router(financials_router, prefix="/financials")
api_v1_router.include_router(analytics_router, prefix="/analytics")
api_v1_router.include_router(service_requests_router)
api_v1_router.include_router(payments_router)
api_v1_router.include_router(websocket_router)
api_v1_router.include_router(kds_router)

__all__ = ["api_v1_router"]

