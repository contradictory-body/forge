"""跨层工具：任何层都可 import。"""
import hashlib
import logging

logger = logging.getLogger(__name__)


def hash_password(plain: str) -> str:
    return hashlib.sha256(plain.encode()).hexdigest()


def validate_email(email: str) -> bool:
    return "@" in email and "." in email.split("@")[-1]
