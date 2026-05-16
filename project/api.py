from __future__ import annotations
from typing import List
from project.models import User, Item
from project import storage


def get_user(user_id: int) -> User:
    user = storage.get_user(user_id)
    if user is None:
        raise KeyError(f"User {user_id} not found")
    return user


def list_users() -> List[User]:
    return storage.list_users()


def create_user(username: str, email: str) -> User:
    return storage.create_user(username=username, email=email)


def delete_user(user_id: int) -> None:
    if not storage.delete_user(user_id):
        raise KeyError(f"User {user_id} not found")


def get_item(item_id: int) -> Item:
    item = storage.get_item(item_id)
    if item is None:
        raise KeyError(f"Item {item_id} not found")
    return item


def list_items(owner_id: int | None = None) -> List[Item]:
    return storage.list_items(owner_id=owner_id)


def create_item(name: str, owner_id: int, description: str = "") -> Item:
    get_user(owner_id)  # validates owner exists
    return storage.create_item(name=name, owner_id=owner_id, description=description)


def delete_item(item_id: int) -> None:
    if not storage.delete_item(item_id):
        raise KeyError(f"Item {item_id} not found")
