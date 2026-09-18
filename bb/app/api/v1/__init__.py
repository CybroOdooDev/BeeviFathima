from fastapi import APIRouter

from app.api.v1 import admin, auth, connections, sync

api_router = APIRouter()
api_router.include_router(auth.router)
api_router.include_router(connections.router)
api_router.include_router(sync.router)
# Not tenant-scoped — see app/api/v1/admin.py. Behind get_platform_admin.
api_router.include_router(admin.router)

__all__ = ["api_router"]
