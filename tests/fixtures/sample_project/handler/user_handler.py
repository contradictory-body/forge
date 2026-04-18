"""Compliant handler: only imports service and types."""
from types.models import User
from service.user_service import UserService  # valid

def handle_get_user(user_id: int):
    svc = UserService()
    return svc.repo.get(user_id)
