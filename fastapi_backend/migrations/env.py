import os
from pathlib import Path

from alembic import context
from sqlalchemy import engine_from_config, pool


# Reading DATABASE_URL from environment (optionally loading fastapi_backend/.env when present):
try:
    from dotenv import load_dotenv
    backend_env = Path(__file__).resolve().parents[1] / ".env"
    if backend_env.exists():
        load_dotenv(str(backend_env))
except Exception:
    # Not failing if dotenv isn't available in migration context:
    pass


config = context.config

# Using explicit DATABASE_URL if provided via env, otherwise relying on alembic config:   
db_uri = os.getenv("DATABASE_URL")
if db_uri:
    config.set_main_option("sqlalchemy.url", db_uri)

# Avoiding importing the application's models/settings here to prevent
# strict env requirements during migration runs. No autogeneration is used:
target_metadata = None


def run_migrations_offline() -> None:
    url = config.get_main_option("sqlalchemy.url")
    context.configure(
        url=url,
        target_metadata=target_metadata,
        literal_binds=True,
        dialect_opts={"paramstyle": "named"},
    )

    with context.begin_transaction():
        context.run_migrations()


def run_migrations_online() -> None:
    configuration = config.get_section(config.config_ini_section) or {}
    connectable = engine_from_config(
        configuration,
        prefix="sqlalchemy.",
        poolclass=pool.NullPool,
    )

    with connectable.connect() as connection:
        context.configure(connection=connection, target_metadata=target_metadata)

        with context.begin_transaction():
            context.run_migrations()


if context.is_offline_mode():
    run_migrations_offline()
else:
    run_migrations_online()


