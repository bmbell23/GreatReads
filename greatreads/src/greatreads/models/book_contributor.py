"""Book contributors — additional (and primary) authors & narrators (#192).

The primary author/narrator still live denormalized on the Book (author_name_*,
narrator) for fast card rendering, but EVERY contributor (primary + secondary) also
gets a row here so author/narrator search can span both roles uniformly ("what else
did this person write or narrate" returns work where they were primary OR additional).
"""

from sqlalchemy import Column, Integer, String, Boolean, ForeignKey, Index

from ..database import Base


class BookContributor(Base):
    __tablename__ = "book_contributors"

    id = Column(Integer, primary_key=True)
    book_id = Column(Integer, ForeignKey("books.id"), nullable=False, index=True)
    role = Column(String, nullable=False)      # 'author' | 'narrator'
    first = Column(String)
    last = Column(String)
    is_primary = Column(Boolean, nullable=False, default=False)
    position = Column(Integer, nullable=False, default=0)   # order within the role

    @property
    def name(self) -> str:
        return " ".join(p for p in (self.first, self.last) if p).strip()

    def to_dict(self) -> dict:
        return {
            "id": self.id,
            "book_id": self.book_id,
            "role": self.role,
            "first": self.first,
            "last": self.last,
            "name": self.name,
            "is_primary": bool(self.is_primary),
            "position": self.position,
        }


Index("idx_bc_role_name", BookContributor.role, BookContributor.last, BookContributor.first)
