from alembic import context
from bag.config import Settings
from sqlalchemy import create_engine
from sqlalchemy.pool import NullPool

url = Settings().database_url.get_secret_value()
if url.startswith("postgresql://"):
    url = url.replace("postgresql://", "postgresql+psycopg://", 1)
engine = create_engine(url, poolclass=NullPool)
with engine.connect() as connection:
    context.configure(connection=connection, transactional_ddl=True)
    with context.begin_transaction():
        context.run_migrations()
