from __future__ import annotations

from pathlib import Path
from typing import Callable

from sqlalchemy import create_engine
from sqlalchemy.orm import sessionmaker, Session

from apps.oms.infrastructure.persistence.schema import Base
from apps.oms.shared.logger import get_logger

log = get_logger(__name__)


class Database:
    '''
    Bootstrap class for the OMS database.

    Creates the SQLAlchemy engine, initializes tables, and
    provides a session_factory for all repository classes.

    Usage:
        db = Database("sqlite:///data/oms.db")
        db.init()
        session_factory = db.session_factory
        repo = OrderRepository(session_factory)
    '''

    def __init__(self, database_url: str):
        '''
        Args:
            database_url: SQLAlchemy connection URL.
                          SQLite:     "sqlite:///data/oms.db"
                          PostgreSQL: "postgresql://user:pass@host/oms"
        '''
        self._url    = database_url
        self._engine = None
        self._SessionFactory = None

        log.debug(f"Database configured: {database_url}")

    def init(self) -> None:
        '''
        Create engine and all tables.
        Safe to call multiple times — CREATE TABLE IF NOT EXISTS.
        Must be called before any repository operations.
        '''
        
        
        # Ensure data/ directory exists for SQLite
        if self._url.startswith("sqlite:///"):
            path = self._url.replace("sqlite:///", "")
            Path(path).parent.mkdir(parents=True, exist_ok=True)

        self._engine = create_engine(
            self._url,
            echo=False,   # Set True to log SQL statements for debugging
            # For SQLite: enable WAL mode for better concurrent reads
            connect_args={"check_same_thread": False}
                          if "sqlite" in self._url else {},
        )

        # Create all tables defined in schema.py
        Base.metadata.create_all(self._engine)

        self._SessionFactory = sessionmaker(
            bind=self._engine,
            expire_on_commit=False,  # Keep objects usable after commit
        )

        log.info(f"Database initialized: {self._url}")

    @property
    def session_factory(self) -> Callable[[], Session]:
        '''
        Returns a callable that creates new database sessions.
        Pass this to repository constructors.

        Usage in repositories:
            with self._sf() as session:
                ...
        '''
        if not self._SessionFactory:
            raise RuntimeError(
                "Database.init() must be called before accessing session_factory"
            )
        return self._SessionFactory

    def dispose(self) -> None:
        '''Close all connections in the connection pool. Call at shutdown.'''
        if self._engine:
            self._engine.dispose()
            log.info("Database connections disposed.")
