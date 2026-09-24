from sqlalchemy import create_engine, event
from sqlalchemy.ext.declarative import declarative_base
from sqlalchemy.orm import sessionmaker

from app.config import settings


def _apply_sqlite_pragmas(dbapi_connection):
    cursor = dbapi_connection.cursor()
    cursor.execute("PRAGMA journal_mode=WAL")
    cursor.execute("PRAGMA busy_timeout=30000")
    cursor.execute("PRAGMA foreign_keys=ON")
    cursor.close()


# 常规读写引擎：SQLite 事务惰性开启（第一条语句），读不阻塞写。
engine = create_engine(
    settings.DATABASE_URL,
    connect_args={"check_same_thread": False, "timeout": 30},
    echo=False
)

# 强串行写引擎：每个事务以 BEGIN IMMEDIATE 启动，第一条语句即取得写锁并排队。
# 供“先检查后写入”的关键流程（派生关系建边、撤销发布）使用，使并发请求（包括同时
# 创建相反方向的两条边）真正串行提交，从根上杜绝检查-写入间隙造成的成环。
immediate_engine = create_engine(
    settings.DATABASE_URL,
    connect_args={"check_same_thread": False, "timeout": 30},
    echo=False
)

if engine.dialect.name == "sqlite":

    @event.listens_for(engine, "connect")
    def _sqlite_pragmas(dbapi_connection, connection_record):
        _apply_sqlite_pragmas(dbapi_connection)

    @event.listens_for(immediate_engine, "connect")
    def _sqlite_pragmas_immediate(dbapi_connection, connection_record):
        _apply_sqlite_pragmas(dbapi_connection)

    @event.listens_for(immediate_engine, "begin")
    def _sqlite_begin_immediate(connection):
        connection.exec_driver_sql("BEGIN IMMEDIATE")


SessionLocal = sessionmaker(autocommit=False, autoflush=False, bind=engine)
ImmediateSessionLocal = sessionmaker(autocommit=False, autoflush=False, bind=immediate_engine)

Base = declarative_base()


def get_db():
    db = SessionLocal()
    try:
        yield db
    finally:
        db.close()


def get_db_immediate():
    """以 BEGIN IMMEDIATE 串行化的写事务会话。"""
    db = ImmediateSessionLocal()
    try:
        yield db
    finally:
        db.close()
