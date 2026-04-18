"""配置层：只允许 import types 层。"""
import os
from types.models import User   # valid: config -> types


DATABASE_URL = os.getenv("DATABASE_URL", "sqlite:///test.db")
MAX_CONNECTIONS = 10
DEBUG = os.getenv("DEBUG", "false").lower() == "true"

DEFAULT_ADMIN = User(id=0, name="admin", email="admin@example.com")
