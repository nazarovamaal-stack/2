import os

from sqlalchemy import MetaData, create_engine

engine = create_engine(os.environ.get('DATABASE_URL', 'postgresql+psycopg://marketplace:marketplace@localhost:5432/marketplace'), pool_pre_ping=True)
metadata = MetaData()


def initialize():
    metadata.reflect(bind=engine, only=['users', 'products', 'orders', 'order_items', 'promo_codes', 'user_operations'])


def table(name):
    return metadata.tables[name]
