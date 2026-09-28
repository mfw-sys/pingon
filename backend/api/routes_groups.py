"""Group management endpoints.

Endpoints for managing target groups.
"""

from __future__ import annotations

import asyncio
from typing import Annotated

from fastapi import APIRouter, HTTPException, Depends
from auth.dependencies import get_current_user, require_role

import db
from models.schemas import GroupCreate, GroupOut, GroupUpdate

router = APIRouter(prefix="/api/groups", tags=["groups"])


@router.get("", response_model=list[GroupOut])
async def list_groups(current_user: Annotated[dict, Depends(get_current_user)]) -> list[dict]:
    groups = await asyncio.to_thread(db.list_groups)
    return groups


@router.post("", response_model=GroupOut, status_code=201)
async def create_group(
    payload: GroupCreate,
    current_user: Annotated[dict, Depends(require_role("administrator"))]
) -> dict:
    existing = await asyncio.to_thread(db.get_group_by_name, payload.name)
    if existing:
        raise HTTPException(status_code=409, detail=f"Group '{payload.name}' already exists")
    
    group = await asyncio.to_thread(db.create_group, payload.name, payload.description)
    return group


@router.get("/{group_id}", response_model=GroupOut)
async def get_group(
    group_id: int,
    current_user: Annotated[dict, Depends(get_current_user)]
) -> dict:
    group = await asyncio.to_thread(db.get_group, group_id)
    if not group:
        raise HTTPException(status_code=404, detail="Group not found")
    return group


@router.put("/{group_id}", response_model=GroupOut)
async def update_group(
    group_id: int,
    payload: GroupUpdate,
    current_user: Annotated[dict, Depends(require_role("administrator"))]
) -> dict:
    existing = await asyncio.to_thread(db.get_group, group_id)
    if not existing:
        raise HTTPException(status_code=404, detail="Group not found")
    
    fields = payload.model_dump(exclude_unset=True)
    if not fields:
        return existing
    
    # Check if this is the Root group and user is trying to rename it
    if existing["name"].lower() == "root" and "name" in fields and fields["name"]:
        if fields["name"].lower() != "root":
            raise HTTPException(status_code=400, detail="Cannot rename the default Root group")
    
    # If name changed, check uniqueness
    if "name" in fields and fields["name"] and fields["name"].lower() != existing["name"].lower():
        other = await asyncio.to_thread(db.get_group_by_name, fields["name"])
        if other and other["id"] != group_id:
            raise HTTPException(status_code=409, detail=f"Group '{fields['name']}' already exists")
    
    updated = await asyncio.to_thread(db.update_group, group_id, **fields)
    return updated


@router.delete("/{group_id}", status_code=204)
async def delete_group(
    group_id: int,
    current_user: Annotated[dict, Depends(require_role("administrator"))]
) -> None:
    existing = await asyncio.to_thread(db.get_group, group_id)
    if not existing:
        raise HTTPException(status_code=404, detail="Group not found")
    
    if existing["name"].lower() == "root":
        raise HTTPException(status_code=400, detail="Cannot delete the default Root group")
    
    deleted = await asyncio.to_thread(db.delete_group, group_id)
    if not deleted:
        raise HTTPException(status_code=400, detail="Failed to delete group")
