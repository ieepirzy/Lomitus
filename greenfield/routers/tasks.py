from __future__ import annotations

from typing import Optional

from fastapi import APIRouter
from greenfield.models.core import Task, TaskStatus

router = APIRouter(prefix="/tasks", tags=["tasks"])

# In-memory store for demonstration purposes
_tasks: list[Task] = []


@router.get("/", response_model=list[dict])
def list_tasks(status: Optional[TaskStatus] = None) -> list[Task]:
    """Return all tasks, optionally filtered by status."""
    if status is None:
        return _tasks
    return [task for task in _tasks if task.status == status]


@router.post("/", status_code=201)
def create_task(title: str, tags: list[str] = None) -> Task:
    """Create a new task."""
    task = Task(
        id=len(_tasks) + 1,
        title=title,
        tags=tags or [],
    )
    _tasks.append(task)
    return task
