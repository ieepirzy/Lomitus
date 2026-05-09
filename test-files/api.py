def get_user(user_id: int): return {"id": user_id, "name": "Alice"}
def list_users(): return [{"id": 1}, {"id": 2}]
def create_user(name: str, email: str): return {"id": 3, "name": name, "email": email}
def delete_user(user_id: int): return {"deleted": user_id}
