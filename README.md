# Ereader

A custom ereader Android app that connects to your Calibre library. Built because all other ereader apps suck.

## Features

- 📚 **Calibre Integration** - Direct access to your Calibre Content Server
- 🏷️ **Full Metadata** - Authors, series, tags, cover art, descriptions
- ⬇️ **Download to phone** - Save books locally for offline reading
- 📖 **EPUB & PDF Reader** - Custom rendering with pagination and TOC
- 🖊️ **Highlights** - Tap to select, edit with custom drag handles and magnifier
- 🌐 **VPN-ready** - Works over local network or VPN
- 🔮 **Foldable Support** - Optimized for Pixel 10 Pro Fold posture changes

## Architecture

```
┌─────────────────┐         ┌──────────────────┐         ┌──────────────────┐
│  Android App    │◄────────┤  Flask Backend   │◄────────┤ Calibre Content  │
│ (WebView + JS)  │  HTTP   │  (API Proxy)     │  HTTP   │     Server       │
│                 │         │                  │         │                  │
│  IndexedDB      │         │                  │         │  Calibre Library │
└─────────────────┘         └──────────────────┘         └──────────────────┘
```

**Backend**: Flask REST API that proxies and enhances Calibre's API
**Calibre**: Your existing Calibre Content Server with all your books and metadata
**Frontend**: Native Android WebView wrapper (`simple-app/`) serving local static files (`web/`)

## Highlight Editing

GreatReads uses a custom highlight system designed for mobile touch:
1. **Create**: Long-press and drag to select text. Release to save.
2. **Select**: Tap any existing highlight to bring up the edit handles.
3. **Adjust**: Drag the blue circles to change the start/end bounds.
4. **Magnifier**: A 2x zoom lens appears while dragging handles to help with precise caret placement.
5. **Lookup**: Direct links to dictionary, Wikipedia, or custom URLs for the selected text.

## Quick Start

👉 **See [QUICKSTART.md](QUICKSTART.md) for step-by-step instructions**

### TL;DR

1. Start backend (connects to your Calibre server):
   ```bash
   cd backend
   ./run.sh
   ```

2. Build the Android app:
   ```bash
   ./build-app.sh
   ```

3. Install APK on your phone (via ADB or HTTP)

4. Configure server URL in app settings

5. Start reading!

## Requirements

### Linux Server
- Python 3.7+
- Calibre Content Server (already running on port 8083)
- Your Calibre library

### For Building
- Node.js 18+
- Android SDK or Android Studio
- JDK 17

### For Running
- Android phone (API 24+ / Android 7.0+)
- Both devices on same network/VPN

## Project Structure

```
Ereader/
├── backend/           # Flask server
│   ├── server.py      # Main API server
│   └── run.sh
├── simple-app/       # Android WebView app
│   └── app/src/main   # Java bridge + WebView logic
├── web/              # Static frontend (HTML/JS/CSS)
│   ├── reader.html    # Core EPUB/PDF reading engine
│   └── index.html     # Library browser
├── build-app.sh      # Automated build & stage script
└── QUICKSTART.md     # Quick start guide
```

## Supported Formats

| Format | Status |
|--------|--------|
| EPUB   | ✅ Supported (Custom Pagination) |
| PDF    | ✅ Supported (PDF.js) |
| MOBI   | 🔜 Coming soon |
| AZW3   | 🔜 Planned |

## API Endpoints

The backend server provides:

- `GET /api/books` - List all books
- `GET /api/books/<id>` - Get book info
- `GET /api/books/<id>/download` - Download book
- `GET /api/health` - Health check

## Development

### Backend Development
```bash
cd backend
python3 -m venv venv
source venv/bin/activate
pip install -r requirements.txt
export BOOKS_DIR="/path/to/books"
python server.py
```

### App Development
```bash
# Just edit web/*.html and refresh the app (or adb shell am force-stop / start)
# To rebuild the native bridge:
./build-app.sh
```

## Future Plans

- [x] EPUB reader support
- [x] Font size and style customization
- [x] Bookmarks and highlights
- [ ] Reading progress tracking across devices
- [ ] Night mode / custom themes
- [ ] Web interface for desktop reading
- [ ] Collections/categories
- [ ] Search functionality
- [ ] Book metadata editing

## Why Build This?

Because every ereader app out there either:
- Locks you into an ecosystem
- Has terrible UX
- Doesn't support your own library
- Costs money for basic features
- Harvests your data

This is YOUR library, YOUR app, YOUR way.

## License

MIT - Do whatever you want with it

## Contributing

Feel free to fork, modify, and make it your own!

## Troubleshooting

See [SETUP.md](SETUP.md) for detailed troubleshooting.

Common issues:
- **Can't connect**: Check VPN/network and firewall
- **Build fails**: Verify JDK 17 and Android SDK installation
- **No books**: Check BOOKS_DIR path and file permissions
