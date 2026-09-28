from typing import Annotated, List
from fastapi import APIRouter, Depends, HTTPException, status

import db
from models.schemas import UserOut, UserCreate, UserUpdate
from auth.password import get_password_hash
from auth.dependencies import get_current_user, require_role

router = APIRouter(
    prefix="/api/users", 
    tags=["users"],
    dependencies=[Depends(require_role("administrator"))]
)

@router.get("", response_model=List[UserOut])
async def list_users():
    return db.list_users()

@router.post("", response_model=UserOut)
async def create_user(user_in: UserCreate):
    existing = db.get_user_by_username(user_in.username)
    if existing:
        raise HTTPException(status_code=status.HTTP_409_CONFLICT, detail="Username already exists")
        
    hashed = get_password_hash(user_in.password)
    new_user = db.create_user(
        username=user_in.username,
        password_hash=hashed,
        role=user_in.role,
        is_active=user_in.is_active
    )
    return new_user

@router.get("/{user_id}", response_model=UserOut)
async def get_user(user_id: int):
    user = db.get_user(user_id)
    if not user:
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="User not found")
    return user

@router.put("/{user_id}", response_model=UserOut)
async def update_user(user_id: int, user_in: UserUpdate, current_user: Annotated[dict, Depends(get_current_user)]):
    user = db.get_user(user_id)
    if not user:
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="User not found")

    update_data = user_in.model_dump(exclude_unset=True)
    
    if "username" in update_data and update_data["username"] != user["username"]:
        existing = db.get_user_by_username(update_data["username"])
        if existing:
            raise HTTPException(status_code=status.HTTP_409_CONFLICT, detail="Username already exists")

    # Protection against self-demotion or self-disable
    if user_id == current_user["id"]:
        if update_data.get("role") == "guest":
            raise HTTPException(status_code=status.HTTP_400_BAD_REQUEST, detail="You cannot demote your own account")
        if update_data.get("is_active") is False:
            raise HTTPException(status_code=status.HTTP_400_BAD_REQUEST, detail="You cannot disable your own account")

    if "password" in update_data:
        update_data["password_hash"] = get_password_hash(update_data.pop("password"))
        
    updated = db.update_user(user_id, **update_data)
    return updated

@router.delete("/{user_id}", status_code=status.HTTP_204_NO_CONTENT)
async def delete_user(user_id: int, current_user: Annotated[dict, Depends(get_current_user)]):
    user = db.get_user(user_id)
    if not user:
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="User not found")
        
    if user_id == current_user["id"]:
        raise HTTPException(status_code=status.HTTP_400_BAD_REQUEST, detail="You cannot delete your own account")
        
    # Check if deleting last active admin
    if user["role"] == "administrator":
        users = db.list_users()
        active_admins = [u for u in users if u["role"] == "administrator" and u["is_active"]]
        if len(active_admins) <= 1:
            raise HTTPException(status_code=status.HTTP_400_BAD_REQUEST, detail="Cannot delete the last active administrator")
            
    db.delete_user(user_id)
    return None
