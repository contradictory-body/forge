"""数据访问层：只允许 import config 和 types。"""
from types.models import User     # valid: repository -> types
from config.settings import DATABASE_URL  # valid: repository -> config


class UserRepository:
    def get(self, user_id: int) -> User | None:
        # 模拟数据库查询
        if user_id == 1:
            return User(id=1, name="Alice", email="alice@example.com")
        return None

    def save(self, user: User) -> User:
        return user

    def delete(self, user_id: int) -> bool:
        return True
