from fastapi import APIRouter, HTTPException
from pydantic import BaseModel
from typing import Optional

from greenfield.models.core import Task, TaskStatus

router = APIRouter(prefix="/tasks", tags=["tasks"])

# In-memory store for demo purposes
_tasks: dict[str, Task] = {}


class CreateTaskRequest(BaseModel):
    id: str
    title: str
    description: str
    status: TaskStatus = TaskStatus.PENDING
    priority_score: float = 0.0


@router.post("/", response_model=dict, status_code=201)
def create_task(request: CreateTaskRequest):
    if not (0.0 <= request.priority_score <= 10.0):
        raise HTTPException(
            status_code=422,
            detail="priority_score must be between 0.0 and 10.0 (inclusive)",
        )

    task = Task(
        id=request.id,
        title=request.title,
        description=request.description,
        status=request.status,
        priority_score=request.priority_score,
    )
    _tasks[task.id] = task
    return {
        "id": task.id,
        "title": task.title,
        "description": task.description,
        "status": task.status,
        "priority_score": task.priority_score,
        "created_at": task.created_at.isoformat(),
    }


@router.get("/{task_id}", response_model=dict)
def get_task(task_id: str):
    task = _tasks.get(task_id)
    if task is None:
        raise HTTPException(status_code=404, detail="Task not found")
    return {
        "id": task.id,
        "title": task.title,
        "description": task.description,
        "status": task.status,
        "priority_score": task.priority_score,
        "created_at": task.created_at.isoformat(),
    }
