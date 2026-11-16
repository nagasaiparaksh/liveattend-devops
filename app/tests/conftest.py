import os
import sys

# Tests use an in-memory SQLite database, never PostgreSQL.
os.environ["DATABASE_URL"] = "sqlite://"
os.environ["ADMIN_PASSWORD"] = "test-admin-pw"
os.environ["QR_ROTATE_SECONDS"] = "20"
sys.path.insert(0, os.path.dirname(os.path.dirname(__file__)))
