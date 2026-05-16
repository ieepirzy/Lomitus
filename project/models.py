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
    created_at: datetime = field(default_factory=datetime.utcnow)


@dataclass
class Item:
    id: int
    name: str
    owner_id: int
    description: str = ""
    created_at: datetime = field(default_factory=datetime.utcnow)
