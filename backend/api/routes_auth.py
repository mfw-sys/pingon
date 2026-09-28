from fastapi import APIRouter, Depends, HTTPException, status
from fastapi.security import OAuth2PasswordRequestForm
from typing import Annotated

import db
from models.schemas import Token, UserOut, ChangePasswordRequest
from auth.password import verify_password, get_password_hash
from auth.jwt import create_access_token
from auth.dependencies import get_current_user

router = APIRouter(prefix="/api/auth", tags=["auth"])

@router.post("/login", response_model=Token)
async def login(form_data: Annotated[OAuth2PasswordRequestForm, Depends()]):
    user = db.get_user_by_username(form_data.username.strip().lower())
    if not user:
        raise HTTPException(
            status_code=status.HTTP_401_UNAUTHORIZED,
            detail="Invalid username or password"
        )
        
    if not verify_password(form_data.password, user["password_hash"]):
        raise HTTPException(
            status_code=status.HTTP_401_UNAUTHORIZED,
            detail="Invalid username or password"
        )
        
    if not user["is_active"]:
        raise HTTPException(
            status_code=status.HTTP_401_UNAUTHORIZED,
            detail="Invalid username or password"
        )

    # Detect if user must change default password
    must_change = bool(user.get("must_change_password")) or verify_password("admin123", user["password_hash"])
    user_dict = dict(user)
    user_dict["must_change_password"] = must_change

    access_token = create_access_token(
        data={"sub": str(user["id"]), "username": user["username"], "role": user["role"]}
    )
    
    return {
        "access_token": access_token,
        "token_type": "bearer",
        "user": user_dict
    }

@router.get("/me", response_model=UserOut)
async def get_me(current_user: Annotated[dict, Depends(get_current_user)]):
    must_change = bool(current_user.get("must_change_password")) or verify_password("admin123", current_user["password_hash"])
    user_dict = dict(current_user)
    user_dict["must_change_password"] = must_change
    return user_dict

@router.post("/change-password")
async def change_password(
    payload: ChangePasswordRequest,
    current_user: Annotated[dict, Depends(get_current_user)]
):
    if payload.new_password == "admin123":
        raise HTTPException(
            status_code=status.HTTP_400_BAD_REQUEST,
            detail="New password cannot be the default password 'admin123'"
        )

    if not verify_password(payload.old_password, current_user["password_hash"]):
        raise HTTPException(
            status_code=status.HTTP_400_BAD_REQUEST,
            detail="Old password is incorrect"
        )
        
    if payload.old_password == payload.new_password:
        raise HTTPException(
            status_code=status.HTTP_400_BAD_REQUEST,
            detail="New password must be different from old password"
        )

    new_hash = get_password_hash(payload.new_password)
    db.update_user(current_user["id"], password_hash=new_hash, must_change_password=False)
    return {"message": "Password changed successfully"}

