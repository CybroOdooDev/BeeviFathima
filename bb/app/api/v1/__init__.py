from fastapi import APIRouter

from app.api.v1 import auth, connections, sync

api_router = APIRouter()
api_router.include_router(auth.router)
api_router.include_router(connections.router)
api_router.include_router(sync.router)

__all__ = ["api_router"]
