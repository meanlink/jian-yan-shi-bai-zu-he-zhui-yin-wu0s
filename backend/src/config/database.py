from sqlalchemy import create_engine
from sqlalchemy.orm import DeclarativeBase, sessionmaker
from .settings import DATABASE_URL

class Base(DeclarativeBase):
    pass

# MySQL 默认 REPEATABLE READ 会让占用检查读到事务开始时的旧快照，
# 并发确认时可能互相看不见对方刚提交的占位。改为 READ COMMITTED。
engine_options = {'pool_pre_ping': True, 'pool_recycle': 1800}
if DATABASE_URL.startswith('mysql'):
    engine_options['isolation_level'] = 'READ COMMITTED'
engine = create_engine(DATABASE_URL, **engine_options)
Session = sessionmaker(engine, expire_on_commit=False)

def get_db():
    with Session() as db:
        yield db
