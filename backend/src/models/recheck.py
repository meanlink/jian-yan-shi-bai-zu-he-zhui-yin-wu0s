from sqlalchemy import String, JSON, Double, ForeignKey, Boolean, Integer, Index
from sqlalchemy.orm import Mapped, mapped_column
from src.config.database import Base


class Shift(Base):
    __tablename__ = 'recheck_shifts'
    id: Mapped[int] = mapped_column(primary_key=True)
    name: Mapped[str] = mapped_column(String(60), unique=True)
    start_minute: Mapped[int] = mapped_column(Integer)
    end_minute: Mapped[int] = mapped_column(Integer)
    weekdays: Mapped[list] = mapped_column(JSON, default=lambda: [0, 1, 2, 3, 4, 5, 6])
    active: Mapped[bool] = mapped_column(Boolean, default=True)
    created_at: Mapped[float] = mapped_column(Double)
    updated_at: Mapped[float] = mapped_column(Double)


class Instrument(Base):
    __tablename__ = 'recheck_instruments'
    id: Mapped[int] = mapped_column(primary_key=True)
    code: Mapped[str] = mapped_column(String(40), unique=True)
    name: Mapped[str] = mapped_column(String(120))
    shift_id: Mapped[int] = mapped_column(ForeignKey('recheck_shifts.id'))
    capabilities: Mapped[list] = mapped_column(JSON, default=list)
    item_minutes: Mapped[dict] = mapped_column(JSON, default=dict)
    prep_minutes: Mapped[int] = mapped_column(Integer, default=5)
    validity_minutes: Mapped[int] = mapped_column(Integer, default=240)
    active: Mapped[bool] = mapped_column(Boolean, default=True)
    version: Mapped[int] = mapped_column(Integer, default=1)
    updated_at: Mapped[float] = mapped_column(Double)


class Inspector(Base):
    __tablename__ = 'recheck_inspectors'
    id: Mapped[int] = mapped_column(primary_key=True)
    code: Mapped[str] = mapped_column(String(40), unique=True)
    name: Mapped[str] = mapped_column(String(120))
    shift_id: Mapped[int] = mapped_column(ForeignKey('recheck_shifts.id'))
    capabilities: Mapped[list] = mapped_column(JSON, default=list)
    active: Mapped[bool] = mapped_column(Boolean, default=True)
    version: Mapped[int] = mapped_column(Integer, default=1)
    updated_at: Mapped[float] = mapped_column(Double)


class Downtime(Base):
    __tablename__ = 'recheck_downtimes'
    id: Mapped[int] = mapped_column(primary_key=True)
    instrument_id: Mapped[int] = mapped_column(ForeignKey('recheck_instruments.id'), index=True)
    start_at: Mapped[float] = mapped_column(Double)
    end_at: Mapped[float] = mapped_column(Double)
    reason: Mapped[str] = mapped_column(String(300))
    created_by: Mapped[str] = mapped_column(String(150))
    created_at: Mapped[float] = mapped_column(Double)
    resolved_at: Mapped[float | None] = mapped_column(Double, nullable=True)
    resolved_by: Mapped[str | None] = mapped_column(String(150), nullable=True)
    impact: Mapped[list] = mapped_column(JSON, default=list)


class RecheckOrder(Base):
    __tablename__ = 'recheck_orders'
    id: Mapped[int] = mapped_column(primary_key=True)
    name: Mapped[str] = mapped_column(String(160))
    job_id: Mapped[int] = mapped_column(ForeignKey('jobs.id'), index=True)
    dataset_id: Mapped[int] = mapped_column(ForeignKey('datasets.id'))
    batches: Mapped[list] = mapped_column(JSON, default=list)
    items: Mapped[list] = mapped_column(JSON, default=list)
    status: Mapped[str] = mapped_column(String(20), default='pending', index=True)
    block_reasons: Mapped[list] = mapped_column(JSON, default=list)
    current_schedule_id: Mapped[int | None] = mapped_column(Integer, nullable=True)
    created_by: Mapped[str] = mapped_column(String(150))
    created_at: Mapped[float] = mapped_column(Double)
    updated_at: Mapped[float] = mapped_column(Double)


class RecheckSchedule(Base):
    __tablename__ = 'recheck_schedules'
    id: Mapped[int] = mapped_column(primary_key=True)
    order_id: Mapped[int] = mapped_column(ForeignKey('recheck_orders.id'), index=True)
    instrument_id: Mapped[int] = mapped_column(ForeignKey('recheck_instruments.id'), index=True)
    inspector_id: Mapped[int] = mapped_column(ForeignKey('recheck_inspectors.id'), index=True)
    prep_start: Mapped[float] = mapped_column(Double)
    prep_end: Mapped[float] = mapped_column(Double)
    test_start: Mapped[float] = mapped_column(Double)
    test_end: Mapped[float] = mapped_column(Double)
    validity_end: Mapped[float] = mapped_column(Double)
    prep_minutes: Mapped[int] = mapped_column(Integer)
    test_minutes: Mapped[int] = mapped_column(Integer)
    validity_minutes: Mapped[int] = mapped_column(Integer)
    status: Mapped[str] = mapped_column(String(20), default='scheduled', index=True)
    plan_version: Mapped[int] = mapped_column(Integer, default=1)
    basis: Mapped[dict] = mapped_column(JSON, default=dict)
    created_at: Mapped[float] = mapped_column(Double)
    updated_at: Mapped[float] = mapped_column(Double)
    started_at: Mapped[float | None] = mapped_column(Double, nullable=True)
    finished_at: Mapped[float | None] = mapped_column(Double, nullable=True)


class RecheckOccupancy(Base):
    __tablename__ = 'recheck_occupancies'
    id: Mapped[int] = mapped_column(primary_key=True)
    schedule_id: Mapped[int] = mapped_column(ForeignKey('recheck_schedules.id'), index=True)
    resource_type: Mapped[str] = mapped_column(String(20), index=True)
    resource_id: Mapped[int] = mapped_column(Integer, index=True)
    start_at: Mapped[float] = mapped_column(Double)
    end_at: Mapped[float] = mapped_column(Double)
    active: Mapped[bool] = mapped_column(Boolean, default=True, index=True)
    created_at: Mapped[float] = mapped_column(Double)
    __table_args__ = (Index('ix_recheck_occupancy_lookup', 'resource_type', 'resource_id', 'active', 'start_at', 'end_at'),)


class RecheckPreview(Base):
    __tablename__ = 'recheck_previews'
    id: Mapped[int] = mapped_column(primary_key=True)
    order_id: Mapped[int] = mapped_column(ForeignKey('recheck_orders.id'), index=True)
    token: Mapped[str] = mapped_column(String(80), unique=True, index=True)
    feasible: Mapped[bool] = mapped_column(Boolean, default=False)
    plan: Mapped[dict | None] = mapped_column(JSON, nullable=True)
    reasons: Mapped[list] = mapped_column(JSON, default=list)
    basis: Mapped[dict] = mapped_column(JSON, default=dict)
    created_at: Mapped[float] = mapped_column(Double)
    expires_at: Mapped[float] = mapped_column(Double)
    consumed_schedule_id: Mapped[int | None] = mapped_column(Integer, nullable=True)
    actor: Mapped[str] = mapped_column(String(150), default='')


class RecheckClaim(Base):
    __tablename__ = 'recheck_claims'
    id: Mapped[int] = mapped_column(primary_key=True)
    order_id: Mapped[int] = mapped_column(ForeignKey('recheck_orders.id'), index=True)
    schedule_id: Mapped[int] = mapped_column(ForeignKey('recheck_schedules.id'), index=True)
    schedule_version: Mapped[int] = mapped_column(Integer)
    inspector_id: Mapped[int] = mapped_column(ForeignKey('recheck_inspectors.id'))
    token: Mapped[str] = mapped_column(String(80), unique=True, index=True)
    status: Mapped[str] = mapped_column(String(20), default='issued', index=True)
    issued_by: Mapped[str] = mapped_column(String(150))
    issued_at: Mapped[float] = mapped_column(Double)
    started_at: Mapped[float | None] = mapped_column(Double, nullable=True)
    completed_at: Mapped[float | None] = mapped_column(Double, nullable=True)
    note: Mapped[str] = mapped_column(String(300), default='')


class RecheckRevision(Base):
    __tablename__ = 'recheck_revisions'
    id: Mapped[int] = mapped_column(primary_key=True)
    schedule_id: Mapped[int] = mapped_column(ForeignKey('recheck_schedules.id'), index=True)
    order_id: Mapped[int] = mapped_column(ForeignKey('recheck_orders.id'), index=True)
    version: Mapped[int] = mapped_column(Integer)
    reason: Mapped[str] = mapped_column(String(160))
    plan: Mapped[dict] = mapped_column(JSON, default=dict)
    basis: Mapped[dict] = mapped_column(JSON, default=dict)
    actor: Mapped[str] = mapped_column(String(150))
    at: Mapped[float] = mapped_column(Double)


class RecheckConfirmation(Base):
    __tablename__ = 'recheck_confirmations'
    id: Mapped[int] = mapped_column(primary_key=True)
    order_id: Mapped[int] = mapped_column(ForeignKey('recheck_orders.id'), index=True)
    idempotency_key: Mapped[str] = mapped_column(String(100), unique=True, index=True)
    schedule_id: Mapped[int] = mapped_column(ForeignKey('recheck_schedules.id'))
    preview_id: Mapped[int | None] = mapped_column(ForeignKey('recheck_previews.id'), nullable=True)
    created_at: Mapped[float] = mapped_column(Double)
