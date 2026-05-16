from dataclasses import dataclass

@dataclass
class User:
    id: int
    name: str

@dataclass
class Product:
    id: int
    price: float
