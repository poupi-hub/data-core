from logging.config import fileConfig

from sqlalchemy import engine_from_config, pool

from alembic import context
from app.analytics import models as analytics_models
from app.data_quality import models as data_quality_models
from app.documentation import models as documentation_models
from app.modules.real_estate import models as real_estate_models
from app.modules.nba import models as nba_models
from app.modules.sports_odds import models as sports_odds_models
from app.normalization import models as normalization_models
from app.raw import models as raw_models
from core.config import settings
from database.models import Base

_ = real_estate_models
_ = nba_models
_ = sports_odds_models
_ = raw_models
_ = normalization_models
_ = analytics_models
_ = data_quality_models
_ = documentation_models

config = context.config
config.set_main_option("sqlalchemy.url", settings.database_url)

if config.config_file_name is not None:
    fileConfig(config.config_file_name)

target_metadata = Base.metadata


def run_migrations_offline() -> None:
    context.configure(
        url=settings.database_url,
        target_metadata=target_metadata,
        literal_binds=True,
        dialect_opts={"paramstyle": "named"},
    )

    with context.begin_transaction():
        context.run_migrations()


def run_migrations_online() -> None:
    connectable = engine_from_config(
        config.get_section(config.config_ini_section, {}),
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
