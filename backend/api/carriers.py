"""Carriers API — list configured carriers from YAML configs."""

from fastapi import APIRouter

from backend.config_loader import get_config_store

router = APIRouter(prefix="/api/carriers", tags=["carriers"])


@router.get("")
async def list_carriers():
    """List all configured carriers with their display names and format counts."""
    store = get_config_store()
    carriers = []
    for key, config in store.get_all_carriers().items():
        formats = store.get_formats(key)
        carriers.append({
            "key": key,
            "name": config.name,
            "format_count": len(formats),
        })
    return {"carriers": carriers}
