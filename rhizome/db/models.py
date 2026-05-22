import enum
from datetime import datetime, timezone

from sqlalchemy import (
    Boolean,
    CheckConstraint,
    DateTime,
    Enum,
    Float,
    ForeignKey,
    Integer,
    JSON,
    LargeBinary,
    String,
    Text,
    TypeDecorator,
    UniqueConstraint,
    func,
)
from sqlalchemy.orm import (
    DeclarativeBase,
    Mapped,
    mapped_column,
    relationship,
)


class UTCDateTime(TypeDecorator):
    """DateTime that enforces aware UTC values. SQLite stores/retrieves naive
    strings, so we reattach tzinfo=UTC on load and reject naive values on save."""

    impl = DateTime(timezone=True)
    cache_ok = True

    def process_bind_param(self, value, dialect):
        if value is None:
            return None
        if value.tzinfo is None:
            raise ValueError("UTCDateTime requires timezone-aware datetime")
        return value.astimezone(timezone.utc)

    def process_result_value(self, value, dialect):
        if value is None:
            return None
        if value.tzinfo is None:
            return value.replace(tzinfo=timezone.utc)
        return value.astimezone(timezone.utc)


class EntryType(enum.Enum):
    fact = "fact"
    exposition = "exposition"
    overview = "overview"


class LoadingPreference(enum.Enum):
    auto = "auto"
    context_stuff = "context_stuff"
    vector_store = "vector_store"


class Base(DeclarativeBase):
    pass


class Topic(Base):
    __tablename__ = "topic"
    __table_args__ = (UniqueConstraint("parent_id", "name"),)

    id: Mapped[int] = mapped_column(primary_key=True, autoincrement=True)
    parent_id: Mapped[int | None] = mapped_column(
        ForeignKey("topic.id", ondelete="CASCADE"), nullable=True, index=True
    )
    name: Mapped[str] = mapped_column(String, nullable=False)
    description: Mapped[str | None] = mapped_column(Text)
    created_at: Mapped[datetime] = mapped_column(nullable=False, server_default=func.now())
    updated_at: Mapped[datetime] = mapped_column(
        nullable=False, server_default=func.now(), onupdate=func.now()
    )

    parent: Mapped["Topic | None"] = relationship(
        back_populates="children", remote_side="Topic.id"
    )
    children: Mapped[list["Topic"]] = relationship(back_populates="parent")
    entries: Mapped[list["KnowledgeEntry"]] = relationship(
        back_populates="topic", cascade="all, delete-orphan"
    )

    def __repr__(self) -> str:
        return f"<Topic id={self.id} name={self.name!r}>"


class KnowledgeEntry(Base):
    __tablename__ = "knowledge_entry"

    id: Mapped[int] = mapped_column(primary_key=True, autoincrement=True)
    topic_id: Mapped[int] = mapped_column(
        ForeignKey("topic.id", ondelete="CASCADE"), nullable=False, index=True
    )
    title: Mapped[str] = mapped_column(String, nullable=False)
    content: Mapped[str] = mapped_column(Text, nullable=False)
    additional_notes: Mapped[str] = mapped_column(Text, nullable=False, server_default="")
    entry_type: Mapped[EntryType | None] = mapped_column(Enum(EntryType), nullable=True)
    difficulty: Mapped[int | None] = mapped_column(Integer, nullable=True)
    speed_testable: Mapped[bool] = mapped_column(Boolean, nullable=False, server_default="0")
    created_at: Mapped[datetime] = mapped_column(nullable=False, server_default=func.now())
    updated_at: Mapped[datetime] = mapped_column(
        nullable=False, server_default=func.now(), onupdate=func.now()
    )

    topic: Mapped["Topic"] = relationship(back_populates="entries")
    tags: Mapped[list["Tag"]] = relationship(
        secondary="knowledge_entry_tag", back_populates="entries"
    )

    # Entries this entry points TO (outgoing edges)
    related_targets: Mapped[list["RelatedKnowledgeEntries"]] = relationship(
        foreign_keys="RelatedKnowledgeEntries.source_entry_id",
        back_populates="source_entry",
        cascade="all, delete-orphan",
    )
    # Entries that point AT this entry (incoming edges)
    related_sources: Mapped[list["RelatedKnowledgeEntries"]] = relationship(
        foreign_keys="RelatedKnowledgeEntries.target_entry_id",
        back_populates="target_entry",
        cascade="all, delete-orphan",
    )
    # Reverse side of the flashcard ↔ entry M:N join. Used by the browser pane to bulk-load linked
    # flashcards alongside an entry page via ``selectinload``. No cascade — the FK on ``FlashcardEntry``
    # already cascades on either side's delete, and we don't want to imply ownership of flashcards
    # from this side.
    flashcard_entries: Mapped[list["FlashcardEntry"]] = relationship()

    def __repr__(self) -> str:
        return f"<KnowledgeEntry id={self.id} title={self.title!r}>"


class Tag(Base):
    __tablename__ = "tag"

    id: Mapped[int] = mapped_column(primary_key=True, autoincrement=True)
    name: Mapped[str] = mapped_column(String, nullable=False, unique=True)

    entries: Mapped[list["KnowledgeEntry"]] = relationship(
        secondary="knowledge_entry_tag", back_populates="tags"
    )

    def __repr__(self) -> str:
        return f"<Tag id={self.id} name={self.name!r}>"


class KnowledgeEntryTag(Base):
    __tablename__ = "knowledge_entry_tag"

    knowledge_entry_id: Mapped[int] = mapped_column(
        ForeignKey("knowledge_entry.id", ondelete="CASCADE"), primary_key=True
    )
    tag_id: Mapped[int] = mapped_column(ForeignKey("tag.id", ondelete="CASCADE"), primary_key=True)


class RelatedKnowledgeEntries(Base):
    __tablename__ = "related_knowledge_entries"
    __table_args__ = (
        CheckConstraint("source_entry_id != target_entry_id", name="no_self_loop"),
    )

    source_entry_id: Mapped[int] = mapped_column(
        ForeignKey("knowledge_entry.id", ondelete="CASCADE"), primary_key=True
    )
    target_entry_id: Mapped[int] = mapped_column(
        ForeignKey("knowledge_entry.id", ondelete="CASCADE"), primary_key=True
    )
    relationship_type: Mapped[str] = mapped_column(String, nullable=False)

    source_entry: Mapped["KnowledgeEntry"] = relationship(
        foreign_keys=[source_entry_id], back_populates="related_targets"
    )
    target_entry: Mapped["KnowledgeEntry"] = relationship(
        foreign_keys=[target_entry_id], back_populates="related_sources"
    )

    def __repr__(self) -> str:
        return (
            f"<RelatedKnowledgeEntries "
            f"source={self.source_entry_id} -> target={self.target_entry_id} "
            f"type={self.relationship_type!r}>"
        )


class ReviewSession(Base):
    __tablename__ = "review_session"

    id: Mapped[int] = mapped_column(primary_key=True, autoincrement=True)
    ephemeral: Mapped[bool] = mapped_column(Boolean, nullable=False, default=False)
    created_at: Mapped[datetime] = mapped_column(nullable=False, server_default=func.now())
    started_at: Mapped[datetime] = mapped_column(nullable=False, server_default=func.now())
    completed_at: Mapped[datetime | None] = mapped_column(nullable=True)
    additional_args: Mapped[dict | None] = mapped_column(JSON, nullable=True)
    user_instructions: Mapped[str | None] = mapped_column(Text, nullable=True)
    plan: Mapped[str | None] = mapped_column(Text, nullable=True)
    final_summary: Mapped[str | None] = mapped_column(Text, nullable=True)

    session_topics: Mapped[list["ReviewSessionTopic"]] = relationship(
        cascade="all, delete-orphan"
    )
    session_entries: Mapped[list["ReviewSessionEntry"]] = relationship(
        cascade="all, delete-orphan"
    )
    interactions: Mapped[list["ReviewInteraction"]] = relationship(
        back_populates="session", cascade="all, delete-orphan"
    )
    flashcards: Mapped[list["Flashcard"]] = relationship(
        back_populates="session", passive_deletes=True
    )

    def __repr__(self) -> str:
        return f"<ReviewSession id={self.id} ephemeral={self.ephemeral} started_at={self.started_at}>"


class ReviewSessionTopic(Base):
    __tablename__ = "review_session_topic"
    __table_args__ = (UniqueConstraint("session_id", "topic_id"),)

    session_id: Mapped[int] = mapped_column(
        ForeignKey("review_session.id", ondelete="CASCADE"), primary_key=True
    )
    topic_id: Mapped[int] = mapped_column(
        ForeignKey("topic.id", ondelete="CASCADE"), primary_key=True
    )

    def __repr__(self) -> str:
        return f"<ReviewSessionTopic session={self.session_id} topic={self.topic_id}>"


class ReviewSessionEntry(Base):
    __tablename__ = "review_session_entry"
    __table_args__ = (UniqueConstraint("session_id", "entry_id"),)

    session_id: Mapped[int] = mapped_column(
        ForeignKey("review_session.id", ondelete="CASCADE"), primary_key=True
    )
    entry_id: Mapped[int] = mapped_column(
        ForeignKey("knowledge_entry.id", ondelete="CASCADE"), primary_key=True
    )

    def __repr__(self) -> str:
        return f"<ReviewSessionEntry session={self.session_id} entry={self.entry_id}>"


class Flashcard(Base):
    __tablename__ = "flashcard"

    id: Mapped[int] = mapped_column(primary_key=True, autoincrement=True)
    session_id: Mapped[int | None] = mapped_column(
        ForeignKey("review_session.id", ondelete="SET NULL"), nullable=True, index=True
    )
    topic_id: Mapped[int] = mapped_column(
        ForeignKey("topic.id", ondelete="CASCADE"), nullable=False, index=True
    )
    question_text: Mapped[str] = mapped_column(Text, nullable=False)
    answer_text: Mapped[str] = mapped_column(Text, nullable=False)
    testing_notes: Mapped[str | None] = mapped_column(Text, nullable=True)

    # --- FSRS scheduling state ---
    # due IS NULL means the card is parked (not scheduled for spaced repetition).
    # Integer values for fsrs_state match py-fsrs's State IntEnum:
    # 1=Learning, 2=Review, 3=Relearning.
    fsrs_state: Mapped[int] = mapped_column(Integer, nullable=False, server_default="1")
    fsrs_step: Mapped[int | None] = mapped_column(Integer, nullable=True, server_default="0")
    stability: Mapped[float | None] = mapped_column(Float, nullable=True)
    difficulty: Mapped[float | None] = mapped_column(Float, nullable=True)
    due: Mapped[datetime | None] = mapped_column(UTCDateTime, nullable=True, index=True)
    last_review: Mapped[datetime | None] = mapped_column(UTCDateTime, nullable=True)

    session: Mapped["ReviewSession | None"] = relationship(back_populates="flashcards")
    flashcard_entries: Mapped[list["FlashcardEntry"]] = relationship(
        cascade="all, delete-orphan"
    )

    def __repr__(self) -> str:
        return f"<Flashcard id={self.id} topic={self.topic_id} session={self.session_id}>"


class FlashcardEntry(Base):
    __tablename__ = "flashcard_entry"
    __table_args__ = (UniqueConstraint("flashcard_id", "entry_id"),)

    flashcard_id: Mapped[int] = mapped_column(
        ForeignKey("flashcard.id", ondelete="CASCADE"), primary_key=True
    )
    # ``index=True`` matters here: the composite PK indexes ``(flashcard_id, entry_id)`` so SQLite can
    # only use it for queries whose leading column is ``flashcard_id``. Queries that filter by
    # ``entry_id`` alone — including the M:N reverse load (``selectinload(KnowledgeEntry.flashcard_entries)``
    # → ``WHERE entry_id IN (...)``) — would otherwise fall back to a table scan.
    entry_id: Mapped[int] = mapped_column(
        ForeignKey("knowledge_entry.id", ondelete="CASCADE"), primary_key=True, index=True
    )

    def __repr__(self) -> str:
        return f"<FlashcardEntry flashcard={self.flashcard_id} entry={self.entry_id}>"


class ReviewInteraction(Base):
    __tablename__ = "review_interaction"
    __table_args__ = (
        CheckConstraint("score >= 1 AND score <= 4", name="score_range"),
    )

    id: Mapped[int] = mapped_column(primary_key=True, autoincrement=True)
    session_id: Mapped[int] = mapped_column(
        ForeignKey("review_session.id", ondelete="CASCADE"), nullable=False, index=True
    )
    flashcard_id: Mapped[int | None] = mapped_column(
        ForeignKey("flashcard.id", ondelete="SET NULL"), nullable=True, index=True
    )
    summary: Mapped[str | None] = mapped_column(Text, nullable=True)
    score: Mapped[int | None] = mapped_column(Integer, nullable=True)
    position: Mapped[int] = mapped_column(Integer, nullable=False)

    session: Mapped["ReviewSession"] = relationship(back_populates="interactions")
    interaction_entries: Mapped[list["ReviewInteractionEntry"]] = relationship(
        cascade="all, delete-orphan"
    )

    def __repr__(self) -> str:
        return (
            f"<ReviewInteraction id={self.id} session={self.session_id} "
            f"pos={self.position} score={self.score}>"
        )


class ReviewInteractionEntry(Base):
    __tablename__ = "review_interaction_entry"
    __table_args__ = (UniqueConstraint("interaction_id", "entry_id"),)

    interaction_id: Mapped[int] = mapped_column(
        ForeignKey("review_interaction.id", ondelete="CASCADE"), primary_key=True
    )
    entry_id: Mapped[int] = mapped_column(
        ForeignKey("knowledge_entry.id", ondelete="CASCADE"), primary_key=True
    )

    def __repr__(self) -> str:
        return f"<ReviewInteractionEntry interaction={self.interaction_id} entry={self.entry_id}>"


class Resource(Base):
    __tablename__ = "resource"

    id: Mapped[int] = mapped_column(primary_key=True, autoincrement=True)
    name: Mapped[str] = mapped_column(String, nullable=False)
    content_hash: Mapped[str | None] = mapped_column(String, nullable=True)
    summary: Mapped[str | None] = mapped_column(Text, nullable=True)
    estimated_tokens: Mapped[int | None] = mapped_column(Integer, nullable=True)
    source_type: Mapped[str | None] = mapped_column(String, nullable=True)
    loading_preference: Mapped[LoadingPreference] = mapped_column(
        Enum(LoadingPreference), nullable=False, server_default="auto"
    )
    created_at: Mapped[datetime] = mapped_column(nullable=False, server_default=func.now())
    updated_at: Mapped[datetime] = mapped_column(
        nullable=False, server_default=func.now(), onupdate=func.now()
    )

    content: Mapped["ResourceContent | None"] = relationship(
        back_populates="resource", cascade="all, delete-orphan", uselist=False,
    )
    chunks: Mapped[list["ResourceChunk"]] = relationship(
        back_populates="resource", cascade="all, delete-orphan"
    )
    sections: Mapped[list["ResourceSection"]] = relationship(
        back_populates="resource", cascade="all, delete-orphan"
    )

    def __repr__(self) -> str:
        return f"<Resource id={self.id} name={self.name!r}>"


class ResourceContent(Base):
    __tablename__ = "resource_content"

    id: Mapped[int] = mapped_column(primary_key=True, autoincrement=True)
    resource_id: Mapped[int] = mapped_column(
        ForeignKey("resource.id", ondelete="CASCADE"), nullable=False, unique=True,
    )
    raw_text: Mapped[str | None] = mapped_column(Text, nullable=True)
    source_bytes: Mapped[bytes | None] = mapped_column(LargeBinary, nullable=True)
    source_metadata: Mapped[dict | None] = mapped_column(JSON, nullable=True)

    resource: Mapped["Resource"] = relationship(back_populates="content")

    def __repr__(self) -> str:
        return f"<ResourceContent resource={self.resource_id}>"


class TopicResource(Base):
    __tablename__ = "topic_resource"

    topic_id: Mapped[int] = mapped_column(
        ForeignKey("topic.id", ondelete="CASCADE"), primary_key=True
    )
    resource_id: Mapped[int] = mapped_column(
        ForeignKey("resource.id", ondelete="CASCADE"), primary_key=True
    )

    def __repr__(self) -> str:
        return f"<TopicResource topic={self.topic_id} resource={self.resource_id}>"


class ResourceChunk(Base):
    __tablename__ = "resource_chunk"

    id: Mapped[int] = mapped_column(primary_key=True, autoincrement=True)
    resource_id: Mapped[int] = mapped_column(
        ForeignKey("resource.id", ondelete="CASCADE"), nullable=False, index=True
    )
    chunk_index: Mapped[int] = mapped_column(Integer, nullable=False)
    start_offset: Mapped[int] = mapped_column(Integer, nullable=False)
    end_offset: Mapped[int] = mapped_column(Integer, nullable=False)
    context_tag: Mapped[dict | None] = mapped_column(JSON, nullable=True)
    embedding: Mapped[bytes | None] = mapped_column(LargeBinary, nullable=True)

    resource: Mapped["Resource"] = relationship(back_populates="chunks")
    sections: Mapped[list["ResourceSection"]] = relationship(
        secondary="resource_chunk_section", back_populates="chunks"
    )

    def __repr__(self) -> str:
        return f"<ResourceChunk id={self.id} resource={self.resource_id} index={self.chunk_index}>"


class ResourceSection(Base):
    __tablename__ = "resource_section"
    __table_args__ = (UniqueConstraint("resource_id", "position"),)

    id: Mapped[int] = mapped_column(primary_key=True, autoincrement=True)
    resource_id: Mapped[int] = mapped_column(
        ForeignKey("resource.id", ondelete="CASCADE"), nullable=False, index=True
    )
    parent_id: Mapped[int | None] = mapped_column(
        ForeignKey("resource_section.id", ondelete="CASCADE"), nullable=True, index=True
    )
    title: Mapped[str] = mapped_column(String, nullable=False)
    depth: Mapped[int] = mapped_column(Integer, nullable=False)
    position: Mapped[int] = mapped_column(Integer, nullable=False)
    page_start: Mapped[int | None] = mapped_column(Integer, nullable=True)
    page_end: Mapped[int | None] = mapped_column(Integer, nullable=True)
    start_offset: Mapped[int | None] = mapped_column(Integer, nullable=True)

    resource: Mapped["Resource"] = relationship(back_populates="sections")
    parent: Mapped["ResourceSection | None"] = relationship(
        back_populates="children", remote_side="ResourceSection.id"
    )
    children: Mapped[list["ResourceSection"]] = relationship(back_populates="parent")
    chunks: Mapped[list["ResourceChunk"]] = relationship(
        secondary="resource_chunk_section", back_populates="sections"
    )

    def __repr__(self) -> str:
        return f"<ResourceSection id={self.id} resource={self.resource_id} title={self.title!r}>"


class ResourceChunkSection(Base):
    __tablename__ = "resource_chunk_section"

    chunk_id: Mapped[int] = mapped_column(
        ForeignKey("resource_chunk.id", ondelete="CASCADE"), primary_key=True
    )
    section_id: Mapped[int] = mapped_column(
        ForeignKey("resource_section.id", ondelete="CASCADE"), primary_key=True
    )
