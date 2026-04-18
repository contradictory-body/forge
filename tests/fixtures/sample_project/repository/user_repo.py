"""Data access layer."""
from types.models import User  # valid: repository can import types

class UserRepo:
    def get(self, id: int) -> User:
        return User(id=id, name="test")
