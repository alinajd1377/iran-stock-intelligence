from datetime import date, datetime
from pydantic import BaseModel, ConfigDict

class AnnouncementOut(BaseModel):
    model_config = ConfigDict(from_attributes=True)
    id: int
    tracing_no: str
    symbol_ticker: str
    title: str
    publish_at: datetime | None = None
    period_end: date | None = None
    report_type: str | None = None
    is_audited: bool | None = None
