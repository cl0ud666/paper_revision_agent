import os
from contextlib import contextmanager
from threading import Lock

from sqlalchemy import create_engine
from sqlalchemy.orm import Session

from app.web.settings import settings

_engines = {}
_lock = Lock()


def engine():
    url = settings().database_url
    key = (os.getpid(), url)
    with _lock:
        if key not in _engines:
            kwargs = {"pool_pre_ping": True}
            if url.startswith("sqlite"):
                kwargs["connect_args"] = {"check_same_thread": False}
            _engines[key] = create_engine(url, **kwargs)
        return _engines[key]


@contextmanager
def transaction():
    with Session(engine(), expire_on_commit=False) as session:
        with session.begin():
            yield session


def reset_engines():
    with _lock:
        for item in _engines.values():
            item.dispose()
        _engines.clear()
