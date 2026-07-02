"""
Google Books API Client

Provides methods to query Google Books API for book discovery.
"""

import re
import requests
from typing import List, Dict, Optional
from time import sleep


def _redact(msg: object) -> str:
    """Strip any `key=…` (the API key) out of an error/URL string before logging."""
    return re.sub(r'key=[\w-]+', 'key=REDACTED', str(msg))


class GoogleBooksClient:
    """Client for interacting with Google Books API."""
    
    BASE_URL = "https://www.googleapis.com/books/v1/volumes"
    
    def __init__(self, api_key: Optional[str] = None):
        """
        Initialize the Google Books client.
        
        Args:
            api_key: Optional API key for higher rate limits
        """
        self.api_key = api_key
        self.session = requests.Session()
    
    def search_by_author(self, author: str, max_results: int = 40, language: str = 'en', debug: bool = False) -> List[Dict]:
        """
        Search for all books by a specific author.

        Args:
            author: Author name to search for
            max_results: Maximum number of results to return (max 40 per request)
            language: Language code to filter results (default: 'en' for English)

        Returns:
            List of book dictionaries with normalized data
        """
        all_books = []
        start_index = 0

        # Google Books API limits to 40 results per request
        # We'll make multiple requests if needed
        while len(all_books) < max_results:
            params = {
                'q': f'inauthor:"{author}"',
                'maxResults': 40,  # Always request max to account for filtering
                'startIndex': start_index,
                'orderBy': 'newest',  # Get newest books first
            }

            # Add language restriction if specified
            if language:
                params['langRestrict'] = language
            
            if self.api_key:
                params['key'] = self.api_key
            
            try:
                response = self.session.get(self.BASE_URL, params=params, timeout=10)
                response.raise_for_status()
                data = response.json()

                if debug:
                    print(f"DEBUG: Got {len(data.get('items', []))} items from API (start_index={start_index})")

                if 'items' not in data or not data['items']:
                    # No more results
                    if debug:
                        print(f"DEBUG: No more items, breaking")
                    break
                
                # Parse and normalize the results
                for item in data['items']:
                    book = self._normalize_book_data(item)
                    if book:
                        # Filter by language if specified
                        if language and book.get('language'):
                            # Check if language matches (case-insensitive)
                            if book.get('language').lower() != language.lower():
                                continue
                        all_books.append(book)
                
                # Update start index for next request
                start_index += len(data['items'])

                # If we got 0 items, we're done
                if len(data['items']) == 0:
                    break
                
                # Be nice to the API - small delay between requests
                sleep(0.5)
                
            except requests.exceptions.RequestException as e:
                print(f"Error querying Google Books API: {_redact(e)}")
                break
        
        return all_books
    
    def _normalize_book_data(self, item: Dict) -> Optional[Dict]:
        """
        Normalize a Google Books API response item into our standard format.
        
        Args:
            item: Raw item from Google Books API
            
        Returns:
            Normalized book dictionary or None if data is invalid
        """
        try:
            volume_info = item.get('volumeInfo', {})
            
            # Skip if no title
            if 'title' not in volume_info:
                return None
            
            # Extract authors (can be multiple)
            authors = volume_info.get('authors', [])
            if not authors:
                return None
            
            # Get publication date
            published_date = volume_info.get('publishedDate', '')
            year = None
            if published_date:
                # publishedDate can be YYYY, YYYY-MM, or YYYY-MM-DD
                year = published_date.split('-')[0] if '-' in published_date else published_date
                try:
                    year = int(year)
                except (ValueError, TypeError):
                    year = None
            
            # Get ISBNs
            isbn_10 = None
            isbn_13 = None
            for identifier in volume_info.get('industryIdentifiers', []):
                if identifier.get('type') == 'ISBN_10':
                    isbn_10 = identifier.get('identifier')
                elif identifier.get('type') == 'ISBN_13':
                    isbn_13 = identifier.get('identifier')
            
            # Series number (when Google provides it via seriesInfo) — name isn't here.
            series_number = None
            bd = volume_info.get('seriesInfo', {}).get('bookDisplayNumber')
            if bd:
                try:
                    series_number = float(bd)
                except (ValueError, TypeError):
                    series_number = None

            # Build normalized book data
            book = {
                'title': volume_info.get('title'),
                'subtitle': volume_info.get('subtitle'),
                'authors': authors,
                'primary_author': authors[0] if authors else None,
                'published_date': published_date,
                'year': year,
                'description': volume_info.get('description'),
                'page_count': volume_info.get('pageCount'),
                'categories': volume_info.get('categories', []),
                # Community rating on Google's 0–5 scale (#158) — separate from the
                # user's own rating; only present when Google has enough ratings.
                'average_rating': volume_info.get('averageRating'),
                'series_number': series_number,
                'language': volume_info.get('language'),
                'isbn_10': isbn_10,
                'isbn_13': isbn_13,
                'google_books_id': item.get('id'),
                'thumbnail': self._best_image_url(volume_info.get('imageLinks', {})),
                'preview_link': volume_info.get('previewLink'),
            }
            
            return book

        except Exception as e:
            print(f"Error normalizing book data: {e}")
            return None

    @staticmethod
    def _best_image_url(image_links: dict) -> Optional[str]:
        """Best available cover URL from a volume's ``imageLinks``, upgraded for quality.

        Google's list response usually only carries ``smallThumbnail``/``thumbnail``
        (~128px, on the content endpoint with a ``zoom=1&edge=curl`` page-curl graphic).
        Prefer any larger key it happens to return, then upgrade the URL: force https,
        drop the curl overlay, and swap ``zoom=1``→``zoom=0`` (the content endpoint
        returns a much larger image at zoom 0). #130
        """
        if not image_links:
            return None
        url = None
        for key in ("extraLarge", "large", "medium", "small", "thumbnail", "smallThumbnail"):
            if image_links.get(key):
                url = image_links[key]
                break
        if not url:
            return None
        url = url.replace("http://", "https://")
        url = url.replace("&edge=curl", "").replace("edge=curl&", "").replace("edge=curl", "")
        url = url.replace("zoom=1", "zoom=0")
        return url
    
    def get_editions(self, title: str, author: str, max_results: int = 20) -> List[Dict]:
        """All editions of one specific work (intitle+inauthor), for reprint detection.

        Returns normalized records; the caller takes the min published year across them
        to decide whether a "new" listing is really a reprint of an old book, and to pick
        a cleaner title / a cover when the author-search edition lacked one.
        """
        params = {'q': f'intitle:"{title}" inauthor:"{author}"',
                  'maxResults': min(max_results, 40), 'langRestrict': 'en'}
        if self.api_key:
            params['key'] = self.api_key
        try:
            response = self.session.get(self.BASE_URL, params=params, timeout=10)
            response.raise_for_status()
            data = response.json()
            out = []
            for item in data.get('items', []):
                b = self._normalize_book_data(item)
                if b:
                    out.append(b)
            return out
        except requests.exceptions.RequestException as e:
            print(f"Error querying Google Books API: {_redact(e)}")
            return []

    def get_book_by_isbn(self, isbn: str) -> Optional[Dict]:
        """
        Get book details by ISBN.
        
        Args:
            isbn: ISBN-10 or ISBN-13
            
        Returns:
            Normalized book dictionary or None if not found
        """
        params = {
            'q': f'isbn:{isbn}',
        }
        
        if self.api_key:
            params['key'] = self.api_key
        
        try:
            response = self.session.get(self.BASE_URL, params=params, timeout=10)
            response.raise_for_status()
            data = response.json()
            
            if 'items' in data and data['items']:
                return self._normalize_book_data(data['items'][0])
            
        except requests.exceptions.RequestException as e:
            print(f"Error querying Google Books API: {e}")
        
        return None

