"""Business logic layer."""
from types.models import User
from repository.user_repo import UserRepo  # valid: service can import repository

class UserService:
    def __init__(self):
        self.repo = UserRepo()
