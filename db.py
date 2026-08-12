"""Postgres models and session helpers for Time Intelligence."""

from __future__ import annotations

import os
from datetime import datetime, timezone
from typing import Iterable, Optional

from dotenv import load_dotenv

load_dotenv()

from sqlalchemy import (
    DateTime,
    Float,
    Index,
    Integer,
    String,
    Text,
    UniqueConstraint,
    create_engine,
    select,
    func,
)
from sqlalchemy.dialects.postgresql import JSONB, insert as pg_insert
from sqlalchemy.orm import DeclarativeBase, Mapped, Session, mapped_column, sessionmaker

DATABASE_URL = os.getenv(
    "DATABASE_URL",
    "postgresql+psycopg2://time:time@localhost:5432/time",
)

engine = create_engine(DATABASE_URL, pool_pre_ping=True)
SessionLocal = sessionmaker(bind=engine, autoflush=False, autocommit=False)


class Base(DeclarativeBase):
    pass


class TimeEntry(Base):
    __tablename__ = "time_entries"
    __table_args__ = (
        UniqueConstraint("entry_id", name="uq_time_entries_entry_id"),
        Index("ix_time_entries_date", "date"),
        Index("ix_time_entries_user", "user"),
        Index("ix_time_entries_team", "team"),
        Index("ix_time_entries_client", "client"),
    )

    id: Mapped[int] = mapped_column(Integer, primary_key=True, autoincrement=True)
    date: Mapped[str] = mapped_column(String(32), default="")
    client: Mapped[str] = mapped_column(String(512), default="")
    task: Mapped[str] = mapped_column(Text, default="")
    user: Mapped[str] = mapped_column(String(256), default="")
    hours: Mapped[float] = mapped_column(Float, default=0.0)
    minutes: Mapped[int] = mapped_column(Integer, default=0)
    source: Mapped[str] = mapped_column(String(64), default="")
    url: Mapped[str] = mapped_column(Text, default="")
    entry_id: Mapped[str] = mapped_column(String(512), nullable=False)
    space: Mapped[str] = mapped_column(String(256), default="")
    team: Mapped[str] = mapped_column(String(256), default="")
    match_via: Mapped[Optional[str]] = mapped_column(String(128), nullable=True)
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True),
        default=lambda: datetime.now(timezone.utc),
    )

    def to_api_dict(self) -> dict:
        """Keys match the old Sheet columns the dashboard expects."""
        return {
            "Date": self.date or "",
            "Client": self.client or "",
            "Task": self.task or "",
            "User": self.user or "",
            "Hours": self.hours if self.hours is not None else 0,
            "Minutes": self.minutes if self.minutes is not None else 0,
            "Source": self.source or "",
            "URL": self.url or "",
            "EntryId": self.entry_id or "",
            "Space": self.space or "",
            "Team": self.team or "",
        }


class SyncRun(Base):
    __tablename__ = "sync_runs"

    id: Mapped[int] = mapped_column(Integer, primary_key=True, autoincrement=True)
    job: Mapped[str] = mapped_column(String(64), nullable=False)
    started_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True),
        default=lambda: datetime.now(timezone.utc),
    )
    finished_at: Mapped[Optional[datetime]] = mapped_column(DateTime(timezone=True), nullable=True)
    rows_inserted: Mapped[int] = mapped_column(Integer, default=0)
    status: Mapped[str] = mapped_column(String(32), default="running")
    debug: Mapped[Optional[dict]] = mapped_column(JSONB, nullable=True)


def init_db() -> None:
    Base.metadata.create_all(bind=engine)


def get_session() -> Session:
    return SessionLocal()


def existing_entry_ids(session: Session) -> set[str]:
    rows = session.execute(select(TimeEntry.entry_id)).scalars().all()
    return set(rows)


def delete_stale_entries(
    session: Session,
    *,
    source: str,
    days: list[str],
    valid_entry_ids: set[str],
    user_names: set[str] | None = None,
) -> int:
    """Drop rows for synced days/source that no longer exist at the source API."""
    from sqlalchemy import and_, delete

    if not days:
        return 0

    conditions = [
        TimeEntry.source == source,
        TimeEntry.date.in_(days),
    ]
    if valid_entry_ids:
        conditions.append(TimeEntry.entry_id.notin_(valid_entry_ids))
    if user_names:
        names = {n.strip() for n in user_names if n and str(n).strip()}
        if names:
            conditions.append(TimeEntry.user.in_(names))

    result = session.execute(delete(TimeEntry).where(and_(*conditions)))
    session.commit()
    return result.rowcount or 0


def delete_stale_meeting_entries(
    session: Session,
    *,
    days: list[str],
    valid_entry_ids: set[str],
    user_names: set[str] | None = None,
) -> int:
    return delete_stale_entries(
        session,
        source="meeting",
        days=days,
        valid_entry_ids=valid_entry_ids,
        user_names=user_names,
    )


def upsert_entries(session: Session, rows: Iterable[dict]) -> int:
    """Insert or update rows keyed by entry_id. Returns affected row count."""
    payload = []
    for r in rows:
        entry_id = str(r.get("entryId") or r.get("entry_id") or "").strip()
        if not entry_id:
            continue
        payload.append(
            {
                "date": str(r.get("date") or ""),
                "client": str(r.get("client") or ""),
                "task": str(r.get("task") or ""),
                "user": str(r.get("user") or ""),
                "hours": float(r.get("hours") or 0),
                "minutes": int(r.get("minutes") or 0),
                "source": str(r.get("source") or ""),
                "url": str(r.get("url") or ""),
                "entry_id": entry_id,
                "space": str(r.get("space") or ""),
                "team": str(r.get("team") or ""),
                "match_via": r.get("matchVia") or r.get("match_via"),
            }
        )
    if not payload:
        return 0

    affected = 0
    chunk = 500
    for i in range(0, len(payload), chunk):
        batch = payload[i : i + chunk]
        stmt = pg_insert(TimeEntry).values(batch)
        stmt = stmt.on_conflict_do_update(
            index_elements=["entry_id"],
            set_={
                "date": stmt.excluded.date,
                "client": stmt.excluded.client,
                "task": stmt.excluded.task,
                "user": stmt.excluded.user,
                "hours": stmt.excluded.hours,
                "minutes": stmt.excluded.minutes,
                "source": stmt.excluded.source,
                "url": stmt.excluded.url,
                "space": stmt.excluded.space,
                "team": stmt.excluded.team,
                "match_via": stmt.excluded.match_via,
            },
        )
        result = session.execute(stmt)
        affected += result.rowcount or 0
    session.commit()
    return affected


def fetch_all_entries(session: Session) -> list[dict]:
    rows = session.execute(select(TimeEntry).order_by(TimeEntry.date.desc(), TimeEntry.id.desc())).scalars()
    return [r.to_api_dict() for r in rows]


def count_entries(session: Session) -> int:
    return session.execute(select(func.count()).select_from(TimeEntry)).scalar_one()


def record_sync_run(
    session: Session,
    *,
    job: str,
    rows_inserted: int,
    status: str,
    debug: Optional[dict],
    started_at: datetime,
) -> None:
    run = SyncRun(
        job=job,
        started_at=started_at,
        finished_at=datetime.now(timezone.utc),
        rows_inserted=rows_inserted,
        status=status,
        debug=debug,
    )
    session.add(run)
    session.commit()
