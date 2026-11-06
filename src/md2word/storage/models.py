from datetime import datetime, timezone
from typing import Literal
import uuid

from pydantic import BaseModel, ConfigDict, Field, field_validator

from ..domain.models import DocumentSession, InputMode
from ..domain.style_models import ResolvedStyle, StyleConfig
from ..domain.local_formatting import LocalParagraph, validate_formats


class StateModel(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True, strict=True, allow_inf_nan=False)


class Draft(StateModel):
    schema_version: Literal[2] = 2
    session_id: str
    original_text: str = Field(max_length=2_000_000)
    current_text: str = Field(max_length=2_000_000)
    mode: Literal["markdown", "plain"]
    content_revision: int = Field(ge=0, le=2**63 - 1)
    style_revision: int = Field(ge=0, le=2**63 - 1)
    dirty: bool
    style_snapshot: StyleConfig
    saved_at: str
    title: str = Field(max_length=256)
    local_formats: tuple[LocalParagraph, ...] = ()

    @field_validator("local_formats", mode="before")
    @classmethod
    def formats(cls,value):
        return validate_formats(value)

    @field_validator("session_id")
    @classmethod
    def session_uuid(cls, value):
        if str(uuid.UUID(value)) != value:
            raise ValueError("Noncanonical session ID")
        return value

    @field_validator("saved_at")
    @classmethod
    def timestamp(cls, value):
        if datetime.fromisoformat(value).utcoffset() is None:
            raise ValueError("Timestamp requires timezone")
        return value

    @classmethod
    def capture(cls, session, style, title):
        return cls(session_id=session.session_id, original_text=session.original_text,
                   current_text=session.current_text, mode=session.input_mode.value,
                   content_revision=session.content_revision, style_revision=session.style_revision,
                   dirty=session.dirty, style_snapshot=style.config,
                   saved_at=datetime.now(timezone.utc).isoformat(), title=title[:256],local_formats=session.local_formats)

    def session(self):
        return DocumentSession(self.session_id, self.original_text, self.current_text,
                               InputMode(self.mode), self.content_revision, self.style_revision, self.dirty,self.local_formats)

    def style(self):
        return ResolvedStyle(self.style_snapshot)


class Settings(StateModel):
    schema_version: Literal[1] = 1
    x: int = Field(default=80, ge=-100000, le=100000)
    y: int = Field(default=60, ge=-100000, le=100000)
    width: int = Field(default=1440, ge=620, le=16384)
    height: int = Field(default=960, ge=560, le=16384)
    maximized: bool = False
    columns: list[int] = Field(default_factory=lambda: [470, 650], min_length=2, max_length=2)
    vertical: list[int] = Field(default_factory=lambda: [650, 138], min_length=2, max_length=2)
    last_directory: str = Field(default="", max_length=4096)
    auto_save: bool = True
    motion: bool = True
    manual_preview: bool = False
    auto_expand_errors: bool = True

    @field_validator("columns", "vertical")
    @classmethod
    def valid_splitter(cls, values):
        if any(type(v) is not int or v < 0 or v > 32768 for v in values) or not any(values):
            raise ValueError("Invalid splitter sizes")
        return values
