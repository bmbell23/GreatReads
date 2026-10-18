"""Auto-detected ebook story bounds (#359).

One row per GreatReads book. The e-reader detects front/back matter from the EPUB's TOC
(acknowledgements, excerpts, "also by", ...) and saves the resulting story start/end here
as percent-of-file (0..100) — the same space as ``books.content_start_pct`` /
``content_end_pct`` (#10). Kept in its own table so the books table needs no migration; the
manual anchors on ``books`` always win over these. Either side may be NULL (only one end was
detected). ``toc_titles`` is a JSON list of the TOC titles that were matched.
Created by create_tables() at startup (brand-new table: plain create_all)."""

from datetime import datetime
from sqlalchemy import Column, Integer, Float, DateTime, Text

from ..database import Base


class ContentAnchorAuto(Base):
    __tablename__ = "content_anchor_auto"

    book_id = Column(Integer, primary_key=True)
    start_pct = Column(Float, nullable=True)
    end_pct = Column(Float, nullable=True)
    detected_at = Column(DateTime, nullable=False, default=datetime.utcnow)
    toc_titles = Column(Text, nullable=True)
