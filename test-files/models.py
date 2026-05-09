from dataclasses import dataclass

@dataclass
class User:
    id: int
    name: str
    email: str
    admin: bool = False
    active: bool = True

@dataclass
class Product:
    id: int
    name: str
    price: float
    description: str = ""
