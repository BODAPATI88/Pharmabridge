"""
pharmabridge/db/migrations/env.py
━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━
Alembic environment configuration.

Supports two modes:
  online  – connects to a live database and runs migrations directly
  offline – generates a .sql file for DBA review before applying

Usage:
  # Apply migrations
  cd pharmabridge && alembic -c db/migrations/alembic.ini upgrade head

  # Generate new migration from model changes
  alembic -c db/migrations/alembic.ini revision --autogenerate -m "add_auto_refill"

  # Generate SQL for DBA review
  alembic -c db/migrations/alembic.ini upgrade head --sql > migration.sql
"""

import os
import sys
from logging.config import fileConfig

from alembic import context
from sqlalchemy import engine_from_config, pool

# Add project root to path so orm_models can be imported
sys.path.insert(0, os.path.join(os.path.dirname(__file__), "..", ".."))

from db.orm_models import Base

# Alembic Config object – provides .ini file values
config = context.config

# Interpret the config file for Python logging
if config.config_file_name is not None:
    fileConfig(config.config_file_name)

# Metadata for autogenerate support
target_metadata = Base.metadata

# Override sqlalchemy.url from DATABASE_URL env var if set
# This lets K8s inject the real DB URL without touching alembic.ini
db_url = os.getenv("DATABASE_URL", "").replace("postgresql+asyncpg://", "postgresql+psycopg2://")
if db_url:
    config.set_main_option("sqlalchemy.url", db_url)


def run_migrations_offline() -> None:
    url = config.get_main_option("sqlalchemy.url")
    context.configure(
        url                      = url,
        target_metadata          = target_metadata,
        literal_binds            = True,
        dialect_opts             = {"paramstyle": "named"},
        compare_type             = True,   # Detect column type changes
        compare_server_default   = True,   # Detect default value changes
    )
    with context.begin_transaction():
        context.run_migrations()


def run_migrations_online() -> None:
    connectable = engine_from_config(
        config.get_section(config.config_ini_section, {}),
        prefix       = "sqlalchemy.",
        poolclass    = pool.NullPool,
    )
    with connectable.connect() as connection:
        context.configure(
            connection             = connection,
            target_metadata        = target_metadata,
            compare_type           = True,
            compare_server_default = True,
        )
        with context.begin_transaction():
            context.run_migrations()


if context.is_offline_mode():
    run_migrations_offline()
else:
    run_migrations_online()
