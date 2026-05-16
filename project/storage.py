from __future__ import annotations
from typing import Dict, List, Optional
from project.models import User, Item


_users: Dict[int, User] = {}
_items: Dict[int, Item] = {}
_next_user_id = 1
_next_item_id = 1


def create_user(username: str, email: str) -> User:
    global _next_user_id
    user = User(id=_next_user_id, username=username, email=email)
    _users[user.id] = user
    _next_user_id += 1
    return user


def get_user(user_id: int) -> Optional[User]:
    return _users.get(user_id)


def list_users() -> List[User]:
    return list(_users.values())


def delete_user(user_id: int) -> bool:
    if user_id in _users:
        del _users[user_id]
        return True
    return False


def create_item(name: str, owner_id: int, description: str = "") -> Item:
    global _next_item_id
    item = Item(id=_next_item_id, name=name, owner_id=owner_id, description=description)
    _items[item.id] = item
    _next_item_id += 1
    return item


def get_item(item_id: int) -> Optional[Item]:
    return _items.get(item_id)


def list_items(owner_id: Optional[int] = None) -> List[Item]:
    items = list(_items.values())
    if owner_id is not None:
        items = [i for i in items if i.owner_id == owner_id]
    return items


def delete_item(item_id: int) -> bool:
    if item_id in _items:
        del _items[item_id]
        return True
    return False
