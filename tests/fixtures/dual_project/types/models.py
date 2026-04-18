"""类型层：Pydantic schemas。不允许 import 任何项目内其它层。"""
from dataclasses import dataclass


@dataclass
class User:
    id: int
    name: str
    email: str


@dataclass
class Order:
    id: int
    user_id: int
    amount: float
    status: str  # pending / paid / cancelled
