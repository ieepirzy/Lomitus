from __future__ import annotations

from dataclasses import dataclass, field
from datetime import datetime
from enum import Enum
from typing import Optional


class TaskStatus(str, Enum):
    PENDING = "pending"
    IN_PROGRESS = "in_progress"
    REVIEW = "review"
    DONE = "done"
    BLOCKED = "blocked"
    CANCELLED = "cancelled"


@dataclass
class User:
    id: int
    username: str
    email: str
    is_admin: bool = False
    avatar_url: Optional[str] = None


@dataclass
class Task:
    id: int
    title: str
    status: TaskStatus = TaskStatus.PENDING
    priority_score: float = 0.0
    tags: list[str] = field(default_factory=list)
    created_at: datetime = field(default_factory=datetime.utcnow)
