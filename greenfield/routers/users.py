from typing import Optional

from fastapi import APIRouter, HTTPException
from pydantic import BaseModel

from ..db import get_connection

router = APIRouter(prefix="/users", tags=["users"])


class UserCreate(BaseModel):
    username: str
    email: str
    is_admin: bool = False
    avatar_url: Optional[str] = None


class UserUpdate(BaseModel):
    avatar_url: Optional[str] = None


@router.post("/", status_code=201)
def create_user(payload: UserCreate):
    conn = get_connection()
    try:
        cursor = conn.execute(
            "INSERT INTO users (username, email, is_admin, avatar_url) VALUES (?, ?, ?, ?)",
            (payload.username, payload.email, int(payload.is_admin), payload.avatar_url),
        )
        conn.commit()
        user_id = cursor.lastrowid
        row = conn.execute("SELECT * FROM users WHERE id = ?", (user_id,)).fetchone()
        return dict(row)
    finally:
        conn.close()


@router.get("/{user_id}")
def get_user(user_id: int):
    conn = get_connection()
    try:
        row = conn.execute("SELECT * FROM users WHERE id = ?", (user_id,)).fetchone()
        if row is None:
            raise HTTPException(status_code=404, detail="User not found")
        return dict(row)
    finally:
        conn.close()


@router.patch("/{user_id}")
def update_user(user_id: int, payload: UserUpdate):
    conn = get_connection()
    try:
        row = conn.execute("SELECT * FROM users WHERE id = ?", (user_id,)).fetchone()
        if row is None:
            raise HTTPException(status_code=404, detail="User not found")
        conn.execute(
            "UPDATE users SET avatar_url = ? WHERE id = ?",
            (payload.avatar_url, user_id),
        )
        conn.commit()
        updated = conn.execute("SELECT * FROM users WHERE id = ?", (user_id,)).fetchone()
        return dict(updated)
    finally:
        conn.close()
