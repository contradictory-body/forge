"""正确：handler 只 import service 层。"""
from service.user_service import UserService  # valid: handler -> service


def handle_get_user(user_id: int):
    svc = UserService()
    return svc.get_user(user_id)


def handle_create_user(name: str, email: str):
    svc = UserService()
    return svc.create_user(name, email)
