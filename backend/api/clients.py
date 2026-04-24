"""Clients API — list + get + find-or-create.

Keeps the existing Upload flow untouched; clients are an additive layer.
Master-data lookup + CRUD for client_reference_data entries will be added in
follow-up commits as the organic-population flow fills data in.
"""

import logging
from typing import Optional

from fastapi import APIRouter, Depends
from pydantic import BaseModel
from sqlalchemy import select, func
from sqlalchemy.ext.asyncio import AsyncSession

from backend.models.database import get_db
from backend.models.orm import Client, ClientReferenceData, Upload

logger = logging.getLogger(__name__)

router = APIRouter(prefix="/api/clients", tags=["clients"])


class ClientOut(BaseModel):
    id: str
    name: str
    notes: Optional[str] = None
    project_count: int = 0
    reference_data_count: int = 0


@router.get("")
async def list_clients(db: AsyncSession = Depends(get_db)) -> dict:
    """List every client with counts for projects + master-data entries."""
    # Single query with subquery joins to avoid N+1.
    project_count_sq = (
        select(Upload.client_id, func.count().label("n"))
        .where(Upload.client_id.isnot(None))
        .group_by(Upload.client_id)
        .subquery()
    )
    crd_count_sq = (
        select(ClientReferenceData.client_id, func.count().label("n"))
        .group_by(ClientReferenceData.client_id)
        .subquery()
    )
    stmt = (
        select(
            Client.id,
            Client.name,
            Client.notes,
            func.coalesce(project_count_sq.c.n, 0).label("project_count"),
            func.coalesce(crd_count_sq.c.n, 0).label("reference_data_count"),
        )
        .outerjoin(project_count_sq, project_count_sq.c.client_id == Client.id)
        .outerjoin(crd_count_sq, crd_count_sq.c.client_id == Client.id)
        .order_by(Client.name)
    )
    rows = (await db.execute(stmt)).all()
    return {
        "clients": [
            ClientOut(
                id=str(r.id),
                name=r.name,
                notes=r.notes,
                project_count=int(r.project_count),
                reference_data_count=int(r.reference_data_count),
            ).model_dump()
            for r in rows
        ]
    }


@router.get("/{client_id}")
async def get_client(client_id: str, db: AsyncSession = Depends(get_db)) -> dict:
    """Get one client by id with full reference-data listing."""
    import uuid
    try:
        cid = uuid.UUID(client_id)
    except ValueError:
        return {"error": "invalid client id"}
    client = (await db.execute(select(Client).where(Client.id == cid))).scalar_one_or_none()
    if not client:
        return {"error": "not found"}
    crd_rows = (
        await db.execute(
            select(ClientReferenceData).where(ClientReferenceData.client_id == cid)
        )
    ).scalars().all()
    return {
        "id": str(client.id),
        "name": client.name,
        "notes": client.notes,
        "reference_data": [
            {
                "id": str(r.id),
                "kind": r.kind,
                "carrier": r.carrier,
                "account_number": r.account_number,
                "key_fields": r.key_fields,
                "values": r.values,
                "source": r.source,
                "confirmed_by": r.confirmed_by,
                "confirmed_at": r.confirmed_at.isoformat() if r.confirmed_at else None,
            }
            for r in crd_rows
        ],
    }


# ── Helper used by the upload-classify flow to find-or-create a client ──

async def find_or_create_client(name: str, db: AsyncSession) -> Optional[Client]:
    """Find an existing client by case-insensitive name, or create a new one.

    Returns None when `name` is empty so uploads without a client remain valid.
    """
    if not name or not name.strip():
        return None
    cleaned = name.strip()
    existing = (
        await db.execute(
            select(Client).where(func.lower(Client.name) == cleaned.lower())
        )
    ).scalar_one_or_none()
    if existing:
        return existing
    new_client = Client(name=cleaned)
    db.add(new_client)
    await db.flush()
    logger.info(f"Auto-created client: {cleaned}")
    return new_client
