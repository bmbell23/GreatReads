#!/usr/bin/env python3
"""
Test script to verify Calibre Content Server connection
"""

import requests
import sys

CALIBRE_URL = "http://localhost:8083"
CALIBRE_LIBRARY = "library"

def test_connection():
    print("🧪 Testing Calibre Content Server Connection")
    print("=" * 50)
    
    # Test 1: Library info
    print("\n1. Testing library info endpoint...")
    try:
        response = requests.get(f'{CALIBRE_URL}/ajax/library-info', timeout=5)
        response.raise_for_status()
        data = response.json()
        print(f"   ✅ Connected to Calibre!")
        print(f"   Available libraries: {', '.join(data.get('library_map', {}).keys())}")
        print(f"   Default library: {data.get('default_library', 'N/A')}")
    except Exception as e:
        print(f"   ❌ Failed to connect: {e}")
        sys.exit(1)
    
    # Test 2: Search books
    print("\n2. Testing search endpoint...")
    try:
        response = requests.get(
            f'{CALIBRE_URL}/ajax/search',
            params={'library_id': CALIBRE_LIBRARY, 'num': 5},
            timeout=10
        )
        response.raise_for_status()
        data = response.json()
        total = data.get('total_num', 0)
        book_ids = data.get('book_ids', [])
        print(f"   ✅ Found {total} books in library")
        print(f"   Sample book IDs: {book_ids[:5]}")
    except Exception as e:
        print(f"   ❌ Failed to search: {e}")
        sys.exit(1)
    
    # Test 3: Get book metadata
    if book_ids:
        print("\n3. Testing book metadata endpoint...")
        book_id = book_ids[0]
        try:
            response = requests.get(
                f'{CALIBRE_URL}/ajax/book/{book_id}/{CALIBRE_LIBRARY}',
                timeout=10
            )
            response.raise_for_status()
            book = response.json()
            print(f"   ✅ Retrieved book metadata")
            print(f"   Title: {book.get('title', 'N/A')}")
            print(f"   Author(s): {', '.join(book.get('authors', ['N/A']))}")
            print(f"   Formats: {', '.join(book.get('formats', ['N/A']))}")
        except Exception as e:
            print(f"   ❌ Failed to get metadata: {e}")
            sys.exit(1)
    
    print("\n" + "=" * 50)
    print("✅ All tests passed! Calibre is ready to use.")
    print("\nYour backend server will connect to:")
    print(f"  Calibre URL: {CALIBRE_URL}")
    print(f"  Library: {CALIBRE_LIBRARY}")
    print(f"  Total Books: {total}")

if __name__ == '__main__':
    test_connection()
