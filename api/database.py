"""
Подключение к PostgreSQL через асинхронный SQLAlchemy 2.0.

- В продакшене используется драйвер asyncpg.
- В тестах — SQLite в памяти через aiosqlite (тот же код моделей).
"""
from __future__ import annotations

from contextlib import asynccontextmanager
from typing import AsyncIterator
from urllib.parse import parse_qsl, urlencode, urlsplit, urlunsplit

from sqlalchemy.ext.asyncio import (
    AsyncEngine,
    AsyncSession,
    async_sessionmaker,
    create_async_engine,
)
from sqlalchemy.orm import DeclarativeBase
from sqlalchemy.pool import StaticPool

from api.config import get_settings


class Base(DeclarativeBase):
    """Базовый класс для всех ORM-моделей."""


def _prepare_url(url: str) -> tuple[str, dict]:
    """
    asyncpg не понимает параметр ?sslmode=... (его добавляют Railway/Heroku).
    Убираем его из URL и передаём SSL через connect_args.
    """
    connect_args: dict = {}
    if not url.startswith("postgresql+asyncpg://"):
        return url, connect_args
    parts = urlsplit(url)
    query = dict(parse_qsl(parts.query))
    sslmode = query.pop("sslmode", None)
    if sslmode in {"require", "verify-ca", "verify-full"}:
        connect_args["ssl"] = "require"
    clean = urlunsplit((parts.scheme, parts.netloc, parts.path, urlencode(query), parts.fragment))
    return clean, connect_args


def _build_engine() -> AsyncEngine:
    settings = get_settings()
    url, connect_args = _prepare_url(settings.async_database_url)
    if url.startswith("sqlite"):
        # Для SQLite в памяти нужна одна общая "соединительная" нить
        kwargs: dict = {"connect_args": {"check_same_thread": False}}
        if ":memory:" in url:
            kwargs["poolclass"] = StaticPool
        return create_async_engine(url, **kwargs)
    return create_async_engine(
        url,
        connect_args=connect_args,
        pool_size=5,
        max_overflow=10,
        pool_pre_ping=True,  # переподключение после простоя (Railway рвёт idle-соединения)
    )


engine: AsyncEngine = _build_engine()
SessionLocal = async_sessionmaker(engine, expire_on_commit=False, class_=AsyncSession)


async def init_db() -> None:
    """Создаёт таблицы, если их ещё нет. Вызывается при старте API и бота."""
    # Импорт нужен, чтобы модели зарегистрировались в Base.metadata
    from api import models  # noqa: F401

    async with engine.begin() as conn:
        await conn.run_sync(Base.metadata.create_all)


async def get_session() -> AsyncIterator[AsyncSession]:
    """FastAPI-зависимость: одна сессия на запрос."""
    async with SessionLocal() as session:
        yield session


@asynccontextmanager
async def session_scope() -> AsyncIterator[AsyncSession]:
    """Контекстный менеджер для бота и скриптов: commit при успехе, rollback при ошибке."""
    async with SessionLocal() as session:
        try:
            yield session
            await session.commit()
        except Exception:
            await session.rollback()
            raise
