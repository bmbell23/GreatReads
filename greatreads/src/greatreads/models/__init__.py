"""Data models for GreatReads."""

from .tag import Tag, book_tags
from .book import Book
from .reading import Reading
from .inventory import Inventory
from .shelf import Shelf
from .user_settings import UserSettings
from .user import User
from .external_import import ExternalImport
from .news_item import NewsItem
from .wishlist_acquire import WishlistAcquireState  # #297 — registered so create_tables() makes it
from .card_borrow import CardBorrow  # #357 — card borrow budget history

from .content_anchor_auto import ContentAnchorAuto  # #359 — auto-detected story bounds

__all__ = ["Book", "Reading", "Inventory", "Shelf", "UserSettings", "User", "Tag", "book_tags", "ExternalImport", "NewsItem", "WishlistAcquireState", "CardBorrow", "ContentAnchorAuto"]
