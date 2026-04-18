"""Violation: handler directly imports repository (skipping service layer)."""
from repository.user_repo import UserRepo  # VIOLATION: handler -> repository

def handle_direct():
    repo = UserRepo()
    return repo.get(1)
