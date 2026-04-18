"""违规：handler 直接 import repository（跳过 service 层）。"""
from repository.user_repo import UserRepository  # VIOLATION: handler -> repository (skip service)


def handle_get_user(user_id: int):
    repo = UserRepository()
    return repo.get(user_id)
