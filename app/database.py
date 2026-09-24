from sqlalchemy import create_engine, event, text
from sqlalchemy.ext.declarative import declarative_base
from sqlalchemy.orm import sessionmaker

from app.config import settings

engine = create_engine(
    settings.DATABASE_URL,
    connect_args={"check_same_thread": False},
    echo=False
)


@event.listens_for(engine, "connect")
def _set_sqlite_pragma(dbapi_connection, connection_record):
    # 多线程并发写入时等待解锁而不是立即抛 database is locked
    cursor = dbapi_connection.cursor()
    cursor.execute("PRAGMA busy_timeout = 10000")
    cursor.close()


SessionLocal = sessionmaker(autocommit=False, autoflush=False, bind=engine)

Base = declarative_base()


def get_db():
    db = SessionLocal()
    try:
        yield db
    finally:
        db.close()


def acquire_lineage_lock(db) -> None:
    """抢占谱系哨兵行（INSERT OR IGNORE）。

    SQLite 在该语句上获取 RESERVED 写锁，未提交期间其他派生创建会在同一行上
    阻塞，从而把“查重 + 成环检测 + 插入”串行化，防止并发相反方向关系成环。
    必须在当前事务的首条写语句执行，调用方提交/回滚后自动释放。
    """
    db.execute(text("INSERT OR IGNORE INTO lineage_lock (id) VALUES (1)"))
