"""业务逻辑层：只允许 import repository 和更底层。"""
from types.models import User          # valid: service -> types
from repository.user_repo import UserRepository  # valid: service -> repository


class UserService:
    def __init__(self):
        self.repo = UserRepository()

    def get_user(self, user_id: int) -> User | None:
        return self.repo.get(user_id)

    def create_user(self, name: str, email: str) -> User:
        user = User(id=0, name=name, email=email)
        return self.repo.save(user)
