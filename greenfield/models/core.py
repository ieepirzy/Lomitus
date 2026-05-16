from dataclasses import dataclass, field
from typing import Optional
from datetime import datetime


@dataclass
class User:
    id: int
    username: str
    email: str
    is_admin: bool = False
    avatar_url: Optional[str] = None
    created_at: datetime = field(default_factory=datetime.utcnow)
