"""Shared helper for enriching a serialized book dict with inventory and
external-source (Calibre / Audiobookshelf) information.

Both the library feed and the readings (TBR / Journal) feeds need the same
extra fields so the unified cover-tap popup can offer Read/Listen and show
owned-media / shelf-location details. Keep this in one place so the two
endpoints can't drift apart.
"""

from sqlalchemy.orm import Session

from ..models.inventory import Inventory
from ..models.external_import import ExternalImport


def enrich_book_dict(book_data: dict, book_id: int, db: Session) -> dict:
    """Add inventory, owned-media, and source-link fields to a book dict.

    Mutates and returns ``book_data`` (the output of ``Book.to_dict()``).
    Mirrors the enrichment done in the library books endpoint.
    """
    # Inventory rows (physical shelf location, owned formats).
    inventory = db.query(Inventory).filter(Inventory.book_id == book_id).all()
    book_data["inventory"] = [i.to_dict() for i in inventory]

    media_owned = []
    for inv in inventory:
        if inv.owned_audio:
            media_owned.append("Audio")
        if inv.owned_ebook:
            media_owned.append("Ebook")
        if inv.owned_physical:
            media_owned.append("Physical")
    book_data["media_owned"] = list(set(media_owned))

    # External source links (Calibre / Audiobookshelf). Books with neither are
    # tracking-only (physical / manually added).
    ext = db.query(ExternalImport).filter(ExternalImport.book_id == book_id).all()
    book_data["calibre_id"] = next(
        (e.external_id for e in ext if e.source == "calibre"), None
    )
    book_data["abs_id"] = next(
        (e.external_id for e in ext if e.source == "audiobookshelf"), None
    )

    return book_data
