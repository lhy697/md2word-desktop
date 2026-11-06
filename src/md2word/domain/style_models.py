"""Strict immutable layout configuration. Units are part of field names."""
from dataclasses import dataclass, field
from typing import Annotated, Literal
from pydantic import BaseModel, ConfigDict, Field, ValidationError, field_validator, model_validator
from .models import digest, failure

class StrictModel(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True, strict=True, allow_inf_nan=False)

class MultipleSpacing(StrictModel):
    mode: Literal["multiple"]
    multiplier: float = Field(ge=0.8, le=3)

class FixedSpacing(StrictModel):
    mode: Literal["fixed"]
    points: float = Field(ge=8, le=72)

LineSpacing = Annotated[MultipleSpacing | FixedSpacing, Field(discriminator="mode")]

class FontStyle(StrictModel):
    font_zh: str = Field(min_length=1, max_length=80)
    font_latin: str = Field(min_length=1, max_length=80)
    size_pt: float = Field(ge=6, le=48)

    @field_validator("font_zh", "font_latin")
    @classmethod
    def valid_font(cls, value):
        if value != value.strip() or any(ord(c) < 32 or c in "/\\:" for c in value):
            raise ValueError("Use a font family name, not a path")
        return value

class ParagraphStyle(FontStyle):
    line_spacing: LineSpacing
    before_pt: float = Field(ge=0, le=72)
    after_pt: float = Field(ge=0, le=72)

class BodyStyle(ParagraphStyle):
    first_line_chars: float = Field(ge=0, le=4)

class HeadingStyle(ParagraphStyle):
    level: int = Field(ge=1, le=6)
    keep_next: bool
    page_break_before: bool

class PageStyle(StrictModel):
    width_mm: float = Field(ge=140, le=300)
    height_mm: float = Field(ge=180, le=450)
    top_mm: float = Field(ge=5, le=60)
    bottom_mm: float = Field(ge=5, le=60)
    left_mm: float = Field(ge=5, le=60)
    right_mm: float = Field(ge=5, le=60)
    page_numbers: bool

    @model_validator(mode="after")
    def printable_area(self):
        if self.width_mm > self.height_mm:
            raise ValueError("Only portrait pages are supported")
        if self.width_mm - self.left_mm - self.right_mm < 80 or self.height_mm - self.top_mm - self.bottom_mm < 100:
            raise ValueError("Printable area is too small")
        return self

class ListStyle(StrictModel):
    indent_mm: float = Field(ge=3, le=15)
    hanging_mm: float = Field(ge=1, le=8)

    @model_validator(mode="after")
    def indentation(self):
        if self.hanging_mm > self.indent_mm:
            raise ValueError("Hanging exceeds indentation")
        return self

class TableStyle(ParagraphStyle):
    cell_vertical_mm: float = Field(ge=0.5, le=5)
    cell_horizontal_mm: float = Field(ge=0.5, le=5)
    header_fill: str = Field(pattern=r"^[0-9A-F]{6}$")
    border_color: str = Field(pattern=r"^[0-9A-F]{6}$")

class CodeStyle(ParagraphStyle):
    fill: str = Field(pattern=r"^[0-9A-F]{6}$")

class QuoteStyle(ParagraphStyle):
    indent_mm: float = Field(ge=0, le=25)

class MathStyle(StrictModel):
    font: str = Field(min_length=1, max_length=80)
    before_pt: float = Field(ge=0, le=72)
    after_pt: float = Field(ge=0, le=72)

    @field_validator("font")
    @classmethod
    def valid_font(cls, value):
        return FontStyle.valid_font(value)

class StyleConfig(StrictModel):
    schema_version: Literal[1]
    id: str = Field(pattern=r"^(lecture|default|user\.[0-9a-f]{32})$")
    name: str = Field(min_length=1, max_length=60)
    template_version: Literal["lecture-v1"]
    base_template_id: Literal["lecture"]
    page: PageStyle
    body: BodyStyle
    headings: tuple[HeadingStyle, ...]
    lists: ListStyle
    table: TableStyle
    code: CodeStyle
    quote: QuoteStyle
    math: MathStyle

    @field_validator("schema_version", mode="before")
    @classmethod
    def strict_version(cls, value):
        if type(value) is not int or value != 1:
            raise ValueError("Unsupported schema version")
        return value

    @field_validator("name")
    @classmethod
    def valid_name(cls, value):
        if value != value.strip() or any(ord(c) < 32 for c in value):
            raise ValueError("Invalid display name")
        return value

    @field_validator("headings", mode="before")
    @classmethod
    def json_array(cls, value):
        return tuple(value) if isinstance(value, list) else value

    @model_validator(mode="after")
    def heading_levels(self):
        if tuple(h.level for h in self.headings) != (1, 2, 3, 4, 5, 6):
            raise ValueError("Exactly six ordered heading levels are required")
        return self

    @classmethod
    def parse_config(cls, raw):
        if not isinstance(raw, dict) or type(raw.get("schema_version")) is not int or raw.get("schema_version") != 1:
            raise failure("STYLE_SCHEMA_UNSUPPORTED", "样式版本无法识别，未修改原文件。",
                          "使用兼容版本或恢复内置样式。", "style")
        try:
            return cls.model_validate(raw)
        except ValidationError:
            raise failure("STYLE_INVALID", "样式参数缺失、未知或超出允许范围。",
                          "检查参数、单位和行距模式。", "style") from None

@dataclass(frozen=True)
class ResolvedStyle:
    config: StyleConfig
    resolved_hash: str = field(init=False)

    def __post_init__(self):
        # Revalidate rather than trust Pydantic model_copy(update=...) bypasses.
        valid = StyleConfig.parse_config(self.config.model_dump(mode="json"))
        object.__setattr__(self, "config", valid)
        object.__setattr__(self, "resolved_hash", digest(valid.model_dump(mode="json")))

    @property
    def template_version(self):
        return self.config.template_version

def mm_to_twips(value: float) -> int:
    return round(value * 1440 / 25.4)

def pt_to_twips(value: float) -> int:
    return round(value * 20)

def spacing_values(spacing: LineSpacing):
    return (round(spacing.multiplier * 240), "auto") if isinstance(spacing, MultipleSpacing) else (
        pt_to_twips(spacing.points), "exact")
