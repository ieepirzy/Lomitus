from __future__ import annotations
from dataclasses import dataclass, field
from datetime import datetime
from typing import Optional


@dataclass
class User:
    id: int
    username: str
    email: str
    is_active: bool = True
    role: str = "member"
    created_at: datetime = field(default_factory=datetime.utcnow)


@dataclass
class Item:
    id: int
    name: str
    owner_id: int
    description: str = ""
    price: float = 0.0
    tags: list[str] = field(default_factory=list)
    weight: float = 0.0
    created_at: datetime = field(default_factory=datetime.utcnow)
