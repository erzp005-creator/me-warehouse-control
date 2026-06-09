"""
Database connection and session management.
"""

import os

from sqlalchemy import create_engine
from sqlalchemy.orm import declarative_base, sessionmaker

DATABASE_URL = os.getenv(
    "DATABASE_URL", "postgresql://sentry:sentry@localhost:5432/sentry"
)

# pool_pre_ping validates a pooled connection with a lightweight ping before
# handing it out, transparently replacing one the server has dropped. Without
# it, a connection closed during idle (Postgres idle timeout, container/network
# recycling) surfaces as "server closed the connection unexpectedly" -> 500 on
# the first request after idle. pool_recycle proactively retires connections
# older than 30 min so they never reach that state.
engine = create_engine(DATABASE_URL, pool_pre_ping=True, pool_recycle=1800)
SessionLocal = sessionmaker(bind=engine)
Base = declarative_base()


def get_db():
    db = SessionLocal()
    try:
        yield db
    finally:
        db.close()
