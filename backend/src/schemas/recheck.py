from pydantic import BaseModel, Field


class ShiftInput(BaseModel):
    name: str = Field(min_length=2, max_length=60)
    start_minute: int = Field(ge=0, le=1439)
    end_minute: int = Field(ge=1, le=1440)
    weekdays: list[int] = Field(default_factory=lambda: [0, 1, 2, 3, 4, 5, 6])
    active: bool = True


class InstrumentInput(BaseModel):
    code: str = Field(min_length=1, max_length=40)
    name: str = Field(min_length=2, max_length=120)
    shift_id: int
    capabilities: list[str] = Field(min_length=1)
    item_minutes: dict = Field(default_factory=dict)
    prep_minutes: int = Field(default=5, ge=1, le=600)
    validity_minutes: int = Field(default=240, ge=1, le=10080)
    active: bool = True


class InstrumentPatch(BaseModel):
    name: str | None = Field(default=None, min_length=2, max_length=120)
    shift_id: int | None = None
    capabilities: list[str] | None = None
    item_minutes: dict | None = None
    prep_minutes: int | None = Field(default=None, ge=1, le=600)
    validity_minutes: int | None = Field(default=None, ge=1, le=10080)
    active: bool | None = None


class InspectorInput(BaseModel):
    code: str = Field(min_length=1, max_length=40)
    name: str = Field(min_length=2, max_length=120)
    shift_id: int
    capabilities: list[str] = Field(min_length=1)
    active: bool = True


class InspectorPatch(BaseModel):
    name: str | None = Field(default=None, min_length=2, max_length=120)
    shift_id: int | None = None
    capabilities: list[str] | None = None
    active: bool | None = None


class DowntimeInput(BaseModel):
    instrument_id: int
    start_at: float | None = None
    end_at: float
    reason: str = Field(min_length=2, max_length=300)


class OrderInput(BaseModel):
    name: str = Field(min_length=2, max_length=160)
    job_id: int
    batches: list[str] = Field(min_length=1, max_length=200)
    items: list[str] = Field(min_length=1, max_length=5)


class ConfirmInput(BaseModel):
    preview_token: str = Field(min_length=8, max_length=80)
    idempotency_key: str | None = Field(default=None, min_length=8, max_length=100)


class ClaimInput(BaseModel):
    inspector_id: int | None = None
    note: str = Field(default='', max_length=300)


class ClaimStartInput(BaseModel):
    token: str | None = Field(default=None, max_length=80)


class DisposeInput(BaseModel):
    action: str = Field(pattern='^(resume|replan|cancel)$')
    note: str = Field(default='', max_length=300)
