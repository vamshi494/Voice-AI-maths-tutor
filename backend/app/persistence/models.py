# app/persistence/models.py
from typing import Any, Literal
from sqlalchemy import (
    CheckConstraint,
    DateTime,
    ForeignKey,
    Integer,
    JSON,
    String,
    Text,
    Float,
    UniqueConstraint,
)
from sqlalchemy.dialects.postgresql import JSONB, UUID, TIMESTAMP
from sqlalchemy.orm import DeclarativeBase, Mapped, mapped_column, relationship
from sqlalchemy.sql import func

from app.contracts.base import CamelModel
from app.contracts.diagram import VerifiedDiagram
from app.contracts.scene import SceneDocument, ValidationReport
from app.contracts.turn_plan import TurnPlan


class Base(DeclarativeBase):
    pass


# Stored inside turns.scene_artifacts (validated with this model on write AND read)
class SceneArtifacts(CamelModel):
    kind: Literal["lesson", "doubt", "resume"]
    page_id: str
    continues_board: bool
    status: Literal["complete", "partial"]          # partial = flushed before a doubt or end
    turn_plan: TurnPlan | None = None
    solver_projection: dict[str, Any] | None = None
    verified_diagram: VerifiedDiagram | None = None        # stored so reopening never recompiles
    paused_note: str | None = None                  # why the turn was cut short, e.g. "the lesson paused here for a doubt"


# Column type helpers with SQLite variants for unit testing without live Postgres
JSON_COLUMN = JSONB().with_variant(JSON(), "sqlite")
UUID_COLUMN = UUID(as_uuid=False).with_variant(String(36), "sqlite")
TIMESTAMP_COLUMN = TIMESTAMP(timezone=True).with_variant(DateTime(timezone=True), "sqlite")


class Board(Base):
    __tablename__ = "boards"

    id: Mapped[str] = mapped_column(String, primary_key=True)
    user_id: Mapped[str] = mapped_column(String, index=True)
    title: Mapped[str] = mapped_column(String, default="new board")
    preview: Mapped[str] = mapped_column(String, default="")
    archived_at = mapped_column(TIMESTAMP_COLUMN, nullable=True)
    created_at = mapped_column(TIMESTAMP_COLUMN, server_default=func.now())
    updated_at = mapped_column(TIMESTAMP_COLUMN, server_default=func.now(), onupdate=func.now())

    turns = relationship("Turn", back_populates="board", cascade="all, delete-orphan")


class Turn(Base):
    __tablename__ = "turns"

    id = mapped_column(UUID_COLUMN, primary_key=True)
    board_id: Mapped[str] = mapped_column(ForeignKey("boards.id", ondelete="CASCADE"), index=True)
    user_id: Mapped[str] = mapped_column(String, index=True)
    order_index: Mapped[int] = mapped_column(Integer)
    question: Mapped[str] = mapped_column(Text)
    raw_response: Mapped[str] = mapped_column(Text)            # raw teaching-stream text
    speed_multiplier: Mapped[float] = mapped_column(Float, default=1.0)
    trace_id: Mapped[str | None] = mapped_column(String, nullable=True)
    scene_document = mapped_column(JSON_COLUMN, nullable=True)       # SceneDocument (validated)
    scene_engine_version: Mapped[str | None] = mapped_column(String, nullable=True)
    validation_report = mapped_column(JSON_COLUMN, nullable=True)    # ValidationReport
    visual_status: Mapped[str | None] = mapped_column(String, nullable=True)
    scene_artifacts = mapped_column(JSON_COLUMN, nullable=True)      # SceneArtifacts
    created_at = mapped_column(TIMESTAMP_COLUMN, server_default=func.now(), index=True)

    board = relationship("Board", back_populates="turns")
    segments = relationship(
        "Segment",
        back_populates="turn",
        cascade="all, delete-orphan",
        order_by="Segment.order_index",
    )

    __table_args__ = (
        UniqueConstraint("board_id", "order_index"),
        CheckConstraint(
            "visual_status IN ('validated','text_only','retry_required')",
            name="ck_turn_visual_status",
        ),
    )

    def validate_and_set_artifacts(self, artifacts: SceneArtifacts | dict[str, Any]) -> None:
        """Validate on write."""
        if isinstance(artifacts, SceneArtifacts):
            self.scene_artifacts = artifacts.model_dump(by_alias=True, exclude_none=True)
        else:
            val = SceneArtifacts.model_validate(artifacts)
            self.scene_artifacts = val.model_dump(by_alias=True, exclude_none=True)

    def get_validated_artifacts(self) -> SceneArtifacts | None:
        """Validate on read."""
        if self.scene_artifacts is None:
            return None
        return SceneArtifacts.model_validate(self.scene_artifacts)

    def validate_and_set_scene_document(self, doc: SceneDocument | dict[str, Any] | None) -> None:
        if doc is None:
            self.scene_document = None
        elif isinstance(doc, SceneDocument):
            self.scene_document = doc.model_dump(by_alias=True, exclude_none=True)
        else:
            val = SceneDocument.model_validate(doc)
            self.scene_document = val.model_dump(by_alias=True, exclude_none=True)

    def get_validated_scene_document(self) -> SceneDocument | None:
        if self.scene_document is None:
            return None
        return SceneDocument.model_validate(self.scene_document)

    def validate_and_set_validation_report(self, report: ValidationReport | dict[str, Any] | None) -> None:
        if report is None:
            self.validation_report = None
        elif isinstance(report, ValidationReport):
            self.validation_report = report.model_dump(by_alias=True, exclude_none=True)
        else:
            val = ValidationReport.model_validate(report)
            self.validation_report = val.model_dump(by_alias=True, exclude_none=True)

    def get_validated_validation_report(self) -> ValidationReport | None:
        if self.validation_report is None:
            return None
        return ValidationReport.model_validate(self.validation_report)


class Segment(Base):
    __tablename__ = "segments"

    id = mapped_column(UUID_COLUMN, primary_key=True)
    turn_id = mapped_column(ForeignKey("turns.id", ondelete="CASCADE"), index=True)
    order_index: Mapped[int] = mapped_column(Integer)
    narration: Mapped[str] = mapped_column(Text, default="")    # spoken_text
    spoken_text: Mapped[str] = mapped_column(Text, default="")  # after TTS text transforms
    command = mapped_column(JSON_COLUMN, nullable=True)               # list[BoardOp] actually drawn
    audio_url: Mapped[str | None] = mapped_column(String, nullable=True)   # always NULL in v1 (deferred)
    duration_ms: Mapped[int | None] = mapped_column(Integer, nullable=True)
    timings = mapped_column(JSON_COLUMN, nullable=True)               # optional word timings
    created_at = mapped_column(TIMESTAMP_COLUMN, server_default=func.now())

    turn = relationship("Turn", back_populates="segments")


# ---------------------------------------------------------------------------
# Board-state tables. Created by Base.metadata.create_all; no
# ALTER of the existing tables, so old rows are never touched.
# ---------------------------------------------------------------------------


class BoardState(Base):
    __tablename__ = "board_state"

    board_id: Mapped[str] = mapped_column(ForeignKey("boards.id", ondelete="CASCADE"), primary_key=True)
    version: Mapped[int] = mapped_column(Integer, default=2)
    state = mapped_column(JSON_COLUMN, nullable=True)                 # BoardStateDoc JSON
    lease_owner: Mapped[str | None] = mapped_column(String, nullable=True)
    lease_until = mapped_column(TIMESTAMP_COLUMN, nullable=True)
    updated_at = mapped_column(TIMESTAMP_COLUMN, server_default=func.now(), onupdate=func.now())


class PageRow(Base):
    __tablename__ = "pages"

    page_id: Mapped[str] = mapped_column(String, primary_key=True)    # sub-page id
    board_id: Mapped[str] = mapped_column(ForeignKey("boards.id", ondelete="CASCADE"), index=True)
    lesson_id: Mapped[str] = mapped_column(String, index=True)
    root_page_id: Mapped[str] = mapped_column(String, index=True)
    page_index: Mapped[int] = mapped_column(Integer, default=0)
    sub_index: Mapped[int] = mapped_column(Integer, default=1)
    title: Mapped[str] = mapped_column(String, default="")
    layout = mapped_column(JSON_COLUMN, nullable=True)                # PageCommit without steps
    diagram = mapped_column(JSON_COLUMN, nullable=True)               # legacy VerifiedDiagram
    ops = mapped_column(JSON_COLUMN)                                  # [{"op": BoardOp, "acked": bool}]
    summary: Mapped[str] = mapped_column(Text, default="")
    created_at = mapped_column(TIMESTAMP_COLUMN, server_default=func.now())


class TurnMetrics(Base):
    __tablename__ = "turn_metrics"

    turn_id: Mapped[str] = mapped_column(String, primary_key=True)
    board_id: Mapped[str] = mapped_column(String, index=True)
    marks = mapped_column(JSON_COLUMN)
    kpis = mapped_column(JSON_COLUMN)
    created_at = mapped_column(TIMESTAMP_COLUMN, server_default=func.now())
