// GreatReads JavaScript Application

// Global configuration
// Get the base path from the window variable set in base.html (handles reverse proxy)
const BASE_PATH = window.APP_BASE_PATH || '';
const API_BASE = `${BASE_PATH}/api`;

// Utility functions
function showToast(message, type = 'info') {
    // Create toast element
    const toastHtml = `
        <div class="toast align-items-center text-white bg-${type} border-0" role="alert" aria-live="assertive" aria-atomic="true">
            <div class="d-flex">
                <div class="toast-body">
                    ${message}
                </div>
                <button type="button" class="btn-close btn-close-white me-2 m-auto" data-bs-dismiss="toast"></button>
            </div>
        </div>
    `;
    
    // Add to toast container (create if doesn't exist)
    let toastContainer = document.getElementById('toast-container');
    if (!toastContainer) {
        toastContainer = document.createElement('div');
        toastContainer.id = 'toast-container';
        toastContainer.className = 'toast-container position-fixed top-0 end-0 p-3';
        toastContainer.style.zIndex = '1055';
        document.body.appendChild(toastContainer);
    }
    
    toastContainer.insertAdjacentHTML('beforeend', toastHtml);
    
    // Show toast
    const toastElement = toastContainer.lastElementChild;
    const toast = new bootstrap.Toast(toastElement);
    toast.show();
    
    // Remove from DOM after hiding
    toastElement.addEventListener('hidden.bs.toast', () => {
        toastElement.remove();
    });
}

function formatDate(dateString) {
    if (!dateString) return 'Not set';
    // Parse as local date to avoid timezone conversion issues
    // Date strings from API are in YYYY-MM-DD format
    const [year, month, day] = dateString.split('-').map(Number);
    const date = new Date(year, month - 1, day); // month is 0-indexed
    return date.toLocaleDateString('en-US', {
        year: 'numeric',
        month: 'short',
        day: 'numeric'
    });
}

function formatRating(rating) {
    if (!rating) return '';

    rating = parseFloat(rating);

    // Convert from 1-10 scale to 1-5 scale if needed
    if (rating > 5) {
        rating = rating / 2;
    }

    // Clamp rating to 1-5 range
    rating = Math.max(1, Math.min(5, rating));

    let html = '';
    for (let i = 1; i <= 5; i++) {
        if (rating >= i) {
            // Full star
            html += '<i class="fas fa-star text-warning"></i>';
        } else if (rating >= i - 0.5) {
            // Half star
            html += '<i class="fas fa-star-half-alt text-warning"></i>';
        } else {
            // Empty star
            html += '<i class="far fa-star text-muted"></i>';
        }
    }
    return html;
}

// Emoji Rating Component
function getEmojiForType(type) {
    const emojis = {
        'star': '⭐',
        'blood': '🩸',
        'pepper': '🌶️'
    };
    return emojis[type] || '⭐';
}

function initEmojiRating(container) {
    const ratingType = container.dataset.ratingType;
    const emojiType = container.dataset.emoji || 'star';
    const hiddenInput = container.querySelector('input[type="hidden"]');
    const display = container.querySelector('.emoji-rating-display');
    const emoji = getEmojiForType(emojiType);

    // Create 5 emoji items
    display.innerHTML = '';
    for (let i = 1; i <= 5; i++) {
        const item = document.createElement('span');
        item.className = 'emoji-rating-item empty';
        item.textContent = emoji;
        item.dataset.value = i;

        // Click to set rating
        item.addEventListener('click', (e) => {
            e.stopPropagation();
            const value = parseInt(item.dataset.value);
            setEmojiRating(container, value);
        });

        // Hover preview
        item.addEventListener('mouseenter', () => {
            previewEmojiRating(container, i);
        });

        display.appendChild(item);
    }

    // Reset preview on mouse leave
    display.addEventListener('mouseleave', () => {
        const currentValue = parseInt(hiddenInput.value) || 0;
        updateEmojiDisplay(container, currentValue);
    });

    // Set initial value
    const initialValue = parseInt(hiddenInput.value) || 0;
    updateEmojiDisplay(container, initialValue);
}

function setEmojiRating(container, value) {
    const hiddenInput = container.querySelector('input[type="hidden"]');
    const currentValue = parseInt(hiddenInput.value) || 0;

    // If clicking the same value, clear the rating
    if (currentValue === value) {
        hiddenInput.value = '0';
        updateEmojiDisplay(container, 0);
    } else {
        hiddenInput.value = value.toString();
        updateEmojiDisplay(container, value);
    }
}

function previewEmojiRating(container, value) {
    updateEmojiDisplay(container, value);
}

function updateEmojiDisplay(container, value) {
    const items = container.querySelectorAll('.emoji-rating-item');
    items.forEach((item, index) => {
        if (index < value) {
            item.classList.remove('empty');
            item.classList.add('filled');
        } else {
            item.classList.remove('filled');
            item.classList.add('empty');
        }
    });
}

// Initialize all emoji ratings on page
function initAllEmojiRatings() {
    document.querySelectorAll('.emoji-rating').forEach(container => {
        initEmojiRating(container);
    });
}

function getStatusBadge(reading) {
    if (reading.is_finished) {
        return `<span class="status-badge status-finished">Finished</span>`;
    } else if (reading.is_started) {
        return `<span class="status-badge status-in-progress">In Progress</span>`;
    } else {
        return `<span class="status-badge status-not-started">Not Started</span>`;
    }
}

function getStatusClass(reading) {
    if (reading.is_finished) {
        return 'finished';
    } else if (reading.is_started) {
        return 'in-progress';
    } else {
        return 'not-started';
    }
}

// API helper functions
async function apiCall(endpoint, options = {}) {
    try {
        const response = await axios({
            url: `${API_BASE}${endpoint}`,
            ...options
        });
        return response.data;
    } catch (error) {
        console.error('API call failed:', error);
        // Callers that render their own status-aware message pass { silent: true } so we
        // don't also flash a generic "Server error" (e.g. Libby borrow → 409/403/502).
        if (!options.silent) {
            if (error.response) {
                const message = error.response.data?.detail || error.response.data?.message || 'Server error';
                showToast(message, 'danger');
            } else if (error.request) {
                showToast('Network error - please check your connection', 'danger');
            } else {
                showToast('An unexpected error occurred', 'danger');
            }
        }
        throw error;
    }
}

// ── Cover fallback (#143) — shared by the Books grid and the cover-tap popup ──
// Google Books serves an "image not available" placeholder (a fixed 575×750 PNG,
// HTTP 200) for coverless volumes, so onerror never fires. On that placeholder (or
// a real load error) try the Apple Books fallback (/api/news/cover) once, then the
// parchment/placeholder. Only meaningful for remote (news/Libby) covers, which
// carry data-title/data-author.
function grShowParchment(img) {
    img.style.display = 'none';
    if (img.nextElementSibling) img.nextElementSibling.style.display = 'flex';
}
function grCoverFallback(img) {
    if (img.dataset.fb || !img.dataset.title) { grShowParchment(img); return; }
    img.dataset.fb = '1';
    img.src = `${API_BASE}/news/cover?title=${encodeURIComponent(img.dataset.title)}&author=${encodeURIComponent(img.dataset.author || '')}`;
}
function grCoverOnload(img) {
    if (img.naturalWidth === 575 && img.naturalHeight === 750) grCoverFallback(img);
}
function grCoverError(img) { grCoverFallback(img); }

// Calibre synopses are HTML (comments.text). Strip to plain text but keep paragraph
// breaks, and decode entities safely (textarea never executes markup).
function grSynopsisText(html) {
    if (!html) return '';
    let t = String(html)
        .replace(/<\s*br\s*\/?>/gi, '\n')
        .replace(/<\/\s*(p|div|li)\s*>/gi, '\n')
        .replace(/<[^>]+>/g, '');
    const ta = document.createElement('textarea');
    ta.innerHTML = t;
    return ta.value.replace(/\n{3,}/g, '\n\n').trim();
}

// Reading management functions
async function updateReading(readingId, data) {
    return await apiCall(`/readings/${readingId}`, {
        method: 'PUT',
        data: data
    });
}

async function finishReading(readingId) {
    return await apiCall(`/readings/${readingId}/finish`, {
        method: 'POST'
    });
}

// Finish a reading, then open the ratings/review screen (#127) so the user can
// rate it on the spot (#212 — this used to open the full Edit Reading form
// (#108), but the focused review screen is the right landing after a finish;
// Edit Reading stays reachable for date fixes). The finish endpoint handles the
// finish date + chain logic. `reload` refreshes the calling page's list. Pages
// without the ratings modal fall back to a plain finish toast.
async function finishAndReview(readingId, reload) {
    try {
        await finishReading(readingId);
        if (typeof reload === 'function') await reload();
        const modal = document.getElementById('viewRatingsModal');
        if (!modal) { showToast('Reading marked as finished!', 'success'); return; }
        showToast('Finished — rate and review it!', 'success');
        await grOpenRatings(readingId);
    } catch (e) { /* errors surfaced by apiCall */ }
}

async function pauseReading(readingId) {
    return await apiCall(`/readings/${readingId}/pause`, {
        method: 'POST'
    });
}

async function unpauseReading(readingId) {
    return await apiCall(`/readings/${readingId}/unpause`, {
        method: 'POST'
    });
}

async function startReading(readingId, startDate = null) {
    return await apiCall(`/readings/${readingId}/start`, {
        method: 'POST',
        params: startDate ? { start_date: startDate } : {}
    });
}

async function reorderReadings(readingId, newPosition) {
    return await apiCall('/readings/reorder', {
        method: 'POST',
        params: {
            reading_id: readingId,
            new_position: newPosition
        }
    });
}

async function recalculateChains() {
    return await apiCall('/chains/recalculate', {
        method: 'POST'
    });
}

// Modal management
function showEditModal(reading) {
    const modal = document.getElementById('editReadingModal');
    if (!modal) {
        console.error('Edit modal not found');
        return;
    }

    // Populate form fields
    document.getElementById('editReadingId').value = reading.id;
    // Stash the book's ereader progress keys so "Clear Reading Progress" can also
    // clear the linked ebook/audiobook progress, not just the reading field (#111).
    { const b = reading.book || {}, keys = [];
      if (b.calibre_id) keys.push(String(b.calibre_id));
      if (b.abs_id) keys.push('abs:' + b.abs_id);
      modal.dataset.progressKeys = keys.join(','); }
    // Reveal "Reset word-credit mark" only when the #79 mark is stuck ahead of
    // progress for this book (#86 → Edit Reading, #111).
    { const rc = document.getElementById('editResetCreditBtn');
      if (rc) { rc.classList.add('d-none'); rc.dataset.key = '';
        const key = (modal.dataset.progressKeys || '').split(',').filter(Boolean)[0];
        if (key) fetch(`${GR_EREADER_API}/progress/${encodeURIComponent(key)}`)
            .then(r => r.ok ? r.json() : null)
            .then(p => { if (p && typeof p.progress === 'number' && typeof p.maxProgress === 'number'
                && p.maxProgress > p.progress + 0.005) { rc.dataset.key = key; rc.classList.remove('d-none'); } })
            .catch(() => {}); } }
    document.getElementById('editBookTitle').textContent = reading.book.title;
    document.getElementById('editAuthor').textContent = reading.book.author;
    document.getElementById('editMedia').value = reading.media || '';
    document.getElementById('editDateStarted').value = reading.date_started || '';
    document.getElementById('editDateFinished').value = reading.date_finished_actual || '';

    // Show/hide progress section for In Progress books
    const progressSection = document.getElementById('progressSection');
    if (progressSection) {
        if (reading.is_started && !reading.is_finished) {
            progressSection.style.display = 'block';

            // Populate progress fields with current calculated values
            const currentPercentField = document.getElementById('editCurrentPercent');
            const currentPageField = document.getElementById('editCurrentPage');
            const totalPagesSpan = document.getElementById('totalPages');

            const currentPercent = reading.current_progress_percent || 0;
            const currentPage = reading.current_progress_page || 0;
            const totalPages = reading.book?.page_count || 0;

            if (currentPercentField) {
                currentPercentField.value = currentPercent.toFixed(1);
                currentPercentField.dataset.totalPages = totalPages;
                currentPercentField.dataset.originalValue = currentPercent.toFixed(1); // Track original value
            }

            if (currentPageField) {
                currentPageField.value = currentPage;
                currentPageField.max = totalPages;
                currentPageField.dataset.totalPages = totalPages;
            }

            if (totalPagesSpan) {
                totalPagesSpan.textContent = `/ ${totalPages}`;
            }

            // Add event listeners to sync percentage and page
            if (currentPercentField && currentPageField && totalPages > 0) {
                currentPercentField.addEventListener('input', function() {
                    const percent = parseFloat(this.value) || 0;
                    const page = Math.round((percent / 100) * totalPages);
                    currentPageField.value = page;
                });

                currentPageField.addEventListener('input', function() {
                    const page = parseInt(this.value) || 0;
                    const percent = (page / totalPages) * 100;
                    currentPercentField.value = percent.toFixed(1);
                });
            }
        } else {
            progressSection.style.display = 'none';
        }
    }

    // Show/hide buttons based on reading status
    // (These buttons only exist on TBR page, not journal page)
    const startBtn = document.getElementById('startReadingBtn');
    const startManualBtn = document.getElementById('startReadingManualBtn');
    const pauseBtn = document.getElementById('pauseReadingBtn');
    const unpauseBtn = document.getElementById('unpauseReadingBtn');
    const finishBtn = document.getElementById('finishReadingBtn');

    // Finish button was removed from the edit-reading modals (#110) — finishing
    // now happens only from the Home in-progress action / reader / physical session.
    // Guard finishBtn so the Start/Pause/Unpause toggling still works where it's gone.
    if (startBtn) {
        if (!reading.date_started) {
            // Not started yet - show Start buttons
            startBtn.style.display = 'inline-block';
            if (startManualBtn) startManualBtn.style.display = 'inline-block';
            if (pauseBtn) pauseBtn.style.display = 'none';
            if (unpauseBtn) unpauseBtn.style.display = 'none';
            if (finishBtn) finishBtn.style.display = 'none';
        } else if (reading.status === 'paused') {
            // Paused - show Unpause button
            startBtn.style.display = 'none';
            if (startManualBtn) startManualBtn.style.display = 'none';
            if (pauseBtn) pauseBtn.style.display = 'none';
            if (unpauseBtn) unpauseBtn.style.display = 'inline-block';
            if (finishBtn) finishBtn.style.display = 'inline-block';
        } else {
            // In progress - show Pause button
            startBtn.style.display = 'none';
            if (startManualBtn) startManualBtn.style.display = 'none';
            if (pauseBtn) pauseBtn.style.display = 'inline-block';
            if (unpauseBtn) unpauseBtn.style.display = 'none';
            if (finishBtn) finishBtn.style.display = 'inline-block';
        }
    }

    // Set book cover image
    const coverImg = document.getElementById('editBookCoverImg');
    const fallbackIcon = document.querySelector('#editBookCover .book-cover-fallback');

    coverImg.src = `${window.APP_BASE_PATH}/static/covers/${reading.book.id}.jpg`;
    coverImg.style.display = 'block';
    fallbackIcon.style.display = 'none';

    // Handle image load error
    coverImg.onerror = function() {
        this.style.display = 'none';
        fallbackIcon.style.display = 'block';
    };
    
    // Initialize emoji ratings first
    initAllEmojiRatings();

    // Populate ratings (convert from 1-10 to 1-5 scale if needed, then clamp to whole numbers)
    const ratings = ['horror', 'spice', 'world_building', 'writing', 'characters', 'readability', 'enjoyment'];
    ratings.forEach(rating => {
        const field = document.getElementById(`editRating${rating.charAt(0).toUpperCase() + rating.slice(1).replace('_', '')}`);
        if (field) {
            let value = reading[`rating_${rating}`];
            // Convert and clamp value to 1-5 range if it exists
            if (value !== null && value !== undefined && value !== '') {
                value = parseFloat(value);
                // If value is > 5, it's on the old 1-10 scale, so convert it
                if (value > 5) {
                    value = value / 2;
                }
                // Round to nearest whole number and clamp to 1-5 range
                value = Math.round(value);
                value = Math.max(0, Math.min(5, value));
            } else {
                value = 0;
            }
            field.value = value;

            // Update the emoji display
            const container = field.closest('.emoji-rating');
            if (container) {
                updateEmojiDisplay(container, value);
            }
        }
    });

    // Read picker for multi-read books (#127).
    grPopulateEditReadDropdown(reading);

    // #157: if this was launched from the book popup, hide it and arm the return.
    grHidePopupForEdit();

    // Show modal
    const bsModal = new bootstrap.Modal(modal);
    bsModal.show();
}

// Form submission handlers
async function saveReadingChanges() {
    const form = document.getElementById('editReadingForm');
    const formData = new FormData(form);
    const readingId = formData.get('reading_id');
    
    // Build update data
    const updateData = {};

    // Basic fields
    if (formData.get('media')) updateData.media = formData.get('media');

    // Handle date_started - allow clearing by explicitly setting to null
    const dateStarted = formData.get('date_started');
    if (dateStarted !== null) {
        updateData.date_started = dateStarted || null;
    }

    // Handle date_finished_actual - allow clearing by explicitly setting to null
    const dateFinished = formData.get('date_finished_actual');
    if (dateFinished !== null) {
        updateData.date_finished_actual = dateFinished || null;
    }

    // Progress tracking will be handled separately via the progress API if changed

    // Ratings (whole numbers 0-5, where 0 means no rating)
    const ratings = ['horror', 'spice', 'world_building', 'writing', 'characters', 'readability', 'enjoyment'];
    ratings.forEach(rating => {
        const value = formData.get(`rating_${rating}`);
        if (value !== null && value !== undefined && value !== '') {
            let numValue = parseInt(value);
            // Clamp to 0-5 range (0 = no rating)
            numValue = Math.max(0, Math.min(5, numValue));
            // Only include in update if > 0
            if (numValue > 0) {
                updateData[`rating_${rating}`] = numValue;
            } else {
                // Explicitly set to null to clear the rating
                updateData[`rating_${rating}`] = null;
            }
        }
    });
    
    try {
        // First, update the basic reading data
        await updateReading(readingId, updateData);

        // Then, check if progress was changed and update it separately
        const currentPercentField = document.getElementById('editCurrentPercent');
        if (currentPercentField && currentPercentField.value) {
            const newPercent = parseFloat(currentPercentField.value);
            const originalPercent = parseFloat(currentPercentField.dataset.originalValue || '0');

            console.log(`Progress check: original=${originalPercent}%, new=${newPercent}%, diff=${Math.abs(newPercent - originalPercent)}`);

            // Only update if it's a valid number AND it changed
            if (!isNaN(newPercent) && newPercent >= 0 && newPercent <= 100 &&
                Math.abs(newPercent - originalPercent) > 0.01) {
                console.log(`Calling progress API: ${newPercent}%`);
                await apiCall(`/readings/${readingId}/progress`, {
                    method: 'PUT',
                    params: { current_percent: newPercent }
                });
                console.log('Progress API call completed');
            } else {
                console.log('Progress not changed, skipping API call');
            }
        }

        showToast('Reading updated successfully!', 'success');

        // Close modal
        const modal = bootstrap.Modal.getInstance(document.getElementById('editReadingModal'));
        modal.hide();

        // Refresh the page or update the display
        if (typeof refreshReadings === 'function') {
            refreshReadings();
        } else {
            location.reload();
        }
    } catch (error) {
        // Error already handled by apiCall
    }
}

// Initialize drag and drop
function initializeDragAndDrop(containerId) {
    const container = document.getElementById(containerId);
    if (!container) return;
    
    new Sortable(container, {
        animation: 150,
        ghostClass: 'sortable-ghost',
        chosenClass: 'sortable-chosen',
        dragClass: 'sortable-drag',
        handle: '.drag-handle',
        onEnd: async function(evt) {
            const readingId = evt.item.dataset.readingId;
            const newPosition = evt.newIndex;
            
            try {
                await reorderReadings(readingId, newPosition);
                showToast('Reading reordered successfully!', 'success');
            } catch (error) {
                // Revert the change
                if (evt.oldIndex < evt.newIndex) {
                    evt.to.insertBefore(evt.item, evt.to.children[evt.oldIndex]);
                } else {
                    evt.to.insertBefore(evt.item, evt.to.children[evt.oldIndex + 1]);
                }
            }
        }
    });
}

// Global event listeners
document.addEventListener('DOMContentLoaded', function() {
    // Initialize tooltips
    const tooltipTriggerList = [].slice.call(document.querySelectorAll('[data-bs-toggle="tooltip"]'));
    tooltipTriggerList.map(function(tooltipTriggerEl) {
        return new bootstrap.Tooltip(tooltipTriggerEl);
    });
    
    // Initialize popovers
    const popoverTriggerList = [].slice.call(document.querySelectorAll('[data-bs-toggle="popover"]'));
    popoverTriggerList.map(function(popoverTriggerEl) {
        return new bootstrap.Popover(popoverTriggerEl);
    });
});

// Smart short date: "Mon D" (omits the year when it's the current year, else
// "Mon D, YYYY"). Parses YYYY-MM-DD as a *local* date to avoid timezone day-shift.
// Shared so TBR and Journal cards format start/end dates identically.
function formatDateSmart(dateString) {
    if (!dateString) return '';
    let date;
    if (typeof dateString === 'string' && /^\d{4}-\d{2}-\d{2}/.test(dateString)) {
        const [y, m, d] = dateString.split('-').map(Number);
        date = new Date(y, m - 1, d);
    } else {
        date = new Date(dateString);
    }
    if (isNaN(date)) return '';
    const opts = { month: 'short', day: 'numeric' };
    if (date.getFullYear() !== new Date().getFullYear()) opts.year = 'numeric';
    return date.toLocaleDateString('en-US', opts);
}

// Popup "extra info" section for a reading: a progress bar for an in-progress
// book, or planned start–end for a scheduled (not-yet-started) one. '' otherwise.
function readingExtraInfoHtml(reading) {
    if (!reading) return '';
    const started = reading.date_started;
    const finished = reading.date_finished_actual;
    const mediaColors = { 'Ebook': '#0066CC', 'Physical': '#6B4BA3', 'Audio': '#FF6600' };
    const color = mediaColors[reading.media] || '#28a745';

    if (reading.status === 'paused') {
        const at = reading.current_progress_percent
            ? ' at ' + reading.current_progress_percent.toFixed(1) + '%' : '';
        return `<div class="open-extra"><div class="open-extra-label">
                  <i class="fas fa-pause me-1"></i>Paused${at}</div></div>`;
    }
    if (started && !finished) {
        const pct = reading.current_progress_percent || 0;
        const page = reading.current_progress_page || 0;
        const label = pct > 0 ? `${pct.toFixed(1)}%${page ? ' · p. ' + page : ''}` : 'In progress';
        return `
          <div class="open-extra">
            <div class="open-extra-label"><i class="fas fa-bookmark me-1"></i>Currently reading <span class="small fw-normal opacity-75">· tap to log progress</span></div>
            <div class="open-progress">
              <div class="open-progress-fill" style="width:${pct}%;background:${color};"></div>
              <div class="open-progress-text">${label}</div>
            </div>
          </div>`;
    }
    if (!started) {
        const s = reading.date_est_start;
        const e = reading.date_est_end || reading.date_finished_estimate;
        if (s || e) {
            return `<div class="open-extra">
                      <div class="open-extra-label"><i class="fas fa-calendar me-1"></i>Planned</div>
                      <div class="small text-muted">${s ? formatDateSmart(s) : 'TBD'} – ${e ? formatDateSmart(e) : 'TBD'}</div>
                    </div>`;
        }
    }
    return '';
}

// ---- Shared cover-tap popup -------------------------------------------------
// Ereader (Flask) API base — used to fetch per-book highlight counts for the popup.
// (Named distinctly from pages' own EREADER_API const to avoid global redeclaration.)
const GR_EREADER_API = 'http://100.69.184.113:8092/api';

// Open a readable book in the Ereader reader/player. Both are served same-origin
// at the site root via the :8090 proxy, so root-absolute paths work. Cache-bust
// with a stamp so reader/player HTML edits show up immediately.
function grOpenEbook(calibreId, titleEnc) {
    const v = Date.now();
    window.location.href = `/reader.html?v=${v}&id=${encodeURIComponent(calibreId)}&format=epub&title=${titleEnc}`;
}
// calibreId (optional) links a matching ebook so the player can offer in-book
// search / read-while-listening (dual-format).
function grOpenAudio(absId, titleEnc, authorEnc, calibreId) {
    const v = Date.now();
    let url = `/player.html?v=${v}&absId=${encodeURIComponent(absId)}&title=${titleEnc}&author=${authorEnc}`;
    if (calibreId) {
        url += `&bookId=${encodeURIComponent(calibreId)}&format=epub&hasEbook=1`;
    }
    window.location.href = url;
}

// The in-progress physical reading backing the currently-open cover popup, so the
// tappable Physical card's onclick can open the progress editor for it (#33).
let grActivePhysicalReading = null;
// The book behind the currently-open popup, so GreatReads.openSeries() can read its
// series/universe without embedding strings in inline onclick handlers (#120).
let grActiveBook = null;

// #157: remember the popup we opened Edit Book / Edit Reading FROM, so cancelling
// (or saving) the editor returns to that book-detail popup instead of dropping to
// the underlying page. grLastPopup holds the args to re-open; grReturnToPopup is set
// only when an editor is opened while the popup was actually visible.
let grLastPopup = null;
let grReturnToPopup = false;

// Called by the editors (Edit Book: book_edit.js; Edit Reading: showEditModal) as they
// open. Hides the popup and, if it was visible, arms the return so the editor's close
// re-opens it. Safe no-op when no popup is open.
function grHidePopupForEdit() {
    const el = document.getElementById('openBookModal');
    if (el && el.classList.contains('show')) grReturnToPopup = true;
    bootstrap.Modal.getInstance(el)?.hide();
}

// Fired on the editors' hidden.bs.modal. Re-opens the source popup, refreshed from the
// server (so edits show immediately), falling back to the cached book on any error.
async function grReopenPopupAfterEdit() {
    if (!grReturnToPopup || !grLastPopup) return;
    grReturnToPopup = false;
    const { opts, keepNav } = grLastPopup;
    let book = grLastPopup.book;
    if (book && book.id != null) {
        try { const fresh = await apiCall(`/books/${book.id}/details`); if (fresh) book = fresh; }
        catch (e) { /* keep cached book */ }
    }
    grOpenBookActions(book, opts, keepNav);
}

document.addEventListener('DOMContentLoaded', () => {
    ['bkEditModal', 'editReadingModal'].forEach(id => {
        const el = document.getElementById(id);
        if (el) el.addEventListener('hidden.bs.modal', grReopenPopupAfterEdit);
    });
});

// ── Shared series-strip + author-reads sections for the book popup (#120) ──────────
function grEsc(s) {
    return String(s == null ? '' : s).replace(/[&<>"']/g, c =>
        ({ '&': '&amp;', '<': '&lt;', '>': '&gt;', '"': '&quot;', "'": '&#39;' }[c]));
}
// Cover URL for a /news/series card: remote (cover_url) or local (/static/covers/{id}.jpg).
function grCardCover(c) {
    const base = window.APP_BASE_PATH || '';
    if (c.cover_url) return c.cover_url;
    if (c.has_cover && c.book_id) return `${base}/static/covers/${c.book_id}.jpg?v=${c.cover_version || 0}`;
    return '';
}
function grSeriesMini(c, currentId, withCaption) {
    const num = c.series_number != null ? `#${c.series_number}` : '';
    const title = (c.title && String(c.title).trim())
        ? c.title : (c.series ? `${c.series}${num ? ' ' + num : ''}` : 'Untitled');
    const src = grCardCover(c);
    // Parchment fallback: hidden when it trails an <img> (the flex row would otherwise
    // show it beside the cover — the cutoff-on-the-right bug); shown via onerror, or
    // standalone when there's no cover at all.
    const ph = disp => `<div class="gba-ph" style="display:${disp};">${grEsc(title)}</div>`;
    const cover = src
        ? `<img src="${src}" alt="" loading="lazy" onerror="this.style.display='none';this.nextElementSibling.style.display='flex';">${ph('none')}`
        : ph('flex');
    const cur = (currentId != null && c.book_id === currentId)
        ? ' style="outline:2px solid var(--bs-primary);outline-offset:1px;border-radius:5px;"' : '';
    const caption = withCaption
        ? `<div class="small text-truncate mt-1" title="${grEsc(title)}">${num ? num + ' · ' : ''}${grEsc(title)}</div>`
        : (num ? `<div class="text-muted text-center" style="font-size:.65rem;line-height:1.4;">${num}</div>` : '');
    // Click a sibling to open ITS details in the same popup (#120 Phase 2) — only when
    // it's a real DB book (has book_id); news-only upcoming entries aren't openable yet.
    const clickable = c.book_id != null;
    const cls = clickable ? 'gba-mini gba-clickable' : 'gba-mini';
    const click = clickable ? ` onclick="GreatReads.openBookById(${c.book_id})"` : '';
    return `<div class="col-4 col-sm-3 col-lg-2 mb-2"><div class="${cls}"${cur}${click}>
        <div class="gba-mini-cover">${cover}</div></div>${caption}</div>`;
}
async function grFetchSeries(series, universe) {
    const q = `name=${encodeURIComponent(series)}` + (universe ? `&universe=${encodeURIComponent(universe)}` : '');
    const d = await GreatReads.apiCall('/news/series?' + q);
    return d.cards || [];
}
async function grInjectSeriesStrip(book) {
    const wrap = document.getElementById('gbaSeries');
    if (!wrap) return;
    if (!book || !book.series) { wrap.innerHTML = ''; return; }
    wrap.innerHTML = '<div class="text-muted small">Loading series…</div>';
    let list;
    try { list = await grFetchSeries(book.series, book.universe); }
    catch (e) { wrap.innerHTML = ''; return; }
    if (!list || list.length <= 1) { wrap.innerHTML = ''; return; }   // nothing for a singleton
    const head = `<div class="d-flex justify-content-between align-items-center mb-2">
        <span class="text-muted small">In this series (${list.length})</span>
        <a href="#" class="small" onclick="GreatReads.openSeries();return false;">View series <i class="fas fa-arrow-right ms-1"></i></a></div>`;
    wrap.innerHTML = head + `<div class="row g-2">${list.map(c => grSeriesMini(c, book.id, false)).join('')}</div>`;
}
async function grOpenSeries() {
    const b = grActiveBook;
    if (!b || !b.series) return;
    document.getElementById('grSeriesTitle').textContent = b.universe ? `${b.universe}: ${b.series}` : b.series;
    document.getElementById('grSeriesGrid').innerHTML = '<div class="col-12 text-muted text-center py-4">Loading…</div>';
    bootstrap.Modal.getOrCreateInstance(document.getElementById('grSeriesModal')).show();
    let list;
    try { list = await grFetchSeries(b.series, b.universe); } catch (e) { list = []; }
    document.getElementById('grSeriesGrid').innerHTML = list.length
        ? list.map(c => grSeriesMini(c, b.id, true)).join('')
        : '<div class="col-12 text-muted text-center py-4">No books found in this series.</div>';
}
// Genre view (#155): all DB books with this genre, shown in the Series modal (reused).
async function grOpenGenre(genreEnc) {
    const genre = decodeURIComponent(genreEnc || '');   // chip passes a URL-encoded token
    if (!genre) return;
    document.getElementById('grSeriesTitle').textContent = genre;
    document.getElementById('grSeriesGrid').innerHTML = '<div class="col-12 text-muted text-center py-4">Loading…</div>';
    bootstrap.Modal.getOrCreateInstance(document.getElementById('grSeriesModal')).show();
    let cards;
    try { const d = await GreatReads.apiCall('/news/genre?name=' + encodeURIComponent(genre)); cards = d.cards || []; }
    catch (e) { cards = []; }
    const cur = grActiveBook && grActiveBook.id;
    document.getElementById('grSeriesGrid').innerHTML = cards.length
        ? cards.map(c => grSeriesMini(c, cur, true)).join('')
        : '<div class="col-12 text-muted text-center py-4">No books found in this genre.</div>';
}
// Author view (#155): all DB books by this author, shown in the Series modal (reused).
async function grOpenAuthor() {
    const b = grActiveBook;
    const author = b && b.author;
    if (!author) return;
    document.getElementById('grSeriesTitle').textContent = author;
    document.getElementById('grSeriesGrid').innerHTML = '<div class="col-12 text-muted text-center py-4">Loading…</div>';
    bootstrap.Modal.getOrCreateInstance(document.getElementById('grSeriesModal')).show();
    let cards;
    try { const d = await GreatReads.apiCall('/news/author?name=' + encodeURIComponent(author)); cards = d.cards || []; }
    catch (e) { cards = []; }
    document.getElementById('grSeriesGrid').innerHTML = cards.length
        ? cards.map(c => grSeriesMini(c, b && b.id, true)).join('')
        : '<div class="col-12 text-muted text-center py-4">No books found by this author.</div>';
}
// #192 — render a role's contributors: primary name(s) linked, plus a (+N) toggle that
// reveals the additional names (each linked to that person's works, spanning both roles).
function grContribLinks(list, role) {
    if (!list || !list.length) return '';
    const opener = role === 'author' ? 'openAuthorName' : 'openNarrator';
    const link = c => `<a href="#" class="gba-link" onclick="GreatReads.${opener}('${encodeURIComponent(c.name).replace(/'/g, '%27')}');return false;">${grEsc(c.name)}</a>`;
    let prim = list.filter(c => c.is_primary);
    if (!prim.length) prim = [list[0]];
    const extra = list.filter(c => !prim.includes(c));
    let html = prim.map(link).join(', ');
    if (extra.length) {
        html += ` <a href="#" class="gba-link small text-muted" onclick="GreatReads.showContribExtra(this);return false;" title="${extra.map(c => grEsc(c.name)).join(', ')}">(+${extra.length})</a>`
             + `<span class="gba-contrib-extra" style="display:none;">, ${extra.map(link).join(', ')}</span>`;
    }
    return html;
}
function grShowContribExtra(a) {
    const span = a.parentNode.querySelector('.gba-contrib-extra');
    if (span) span.style.display = '';
    a.style.display = 'none';
}
async function grOpenAuthorName(nameEnc) {
    const name = decodeURIComponent(nameEnc || '');
    if (!name) return;
    document.getElementById('grSeriesTitle').textContent = name;
    document.getElementById('grSeriesGrid').innerHTML = '<div class="col-12 text-muted text-center py-4">Loading…</div>';
    bootstrap.Modal.getOrCreateInstance(document.getElementById('grSeriesModal')).show();
    let cards;
    try { const d = await GreatReads.apiCall('/news/author?name=' + encodeURIComponent(name)); cards = d.cards || []; }
    catch (e) { cards = []; }
    document.getElementById('grSeriesGrid').innerHTML = cards.length
        ? cards.map(c => grSeriesMini(c, null, true)).join('')
        : '<div class="col-12 text-muted text-center py-4">No books found by this author.</div>';
}
async function grOpenNarrator(nameEnc) {
    const name = decodeURIComponent(nameEnc || '');
    if (!name) return;
    document.getElementById('grSeriesTitle').textContent = name;
    document.getElementById('grSeriesGrid').innerHTML = '<div class="col-12 text-muted text-center py-4">Loading…</div>';
    bootstrap.Modal.getOrCreateInstance(document.getElementById('grSeriesModal')).show();
    let cards;
    try { const d = await GreatReads.apiCall('/news/narrator?name=' + encodeURIComponent(name)); cards = d.cards || []; }
    catch (e) { cards = []; }
    document.getElementById('grSeriesGrid').innerHTML = cards.length
        ? cards.map(c => grSeriesMini(c, null, true)).join('')
        : '<div class="col-12 text-muted text-center py-4">No audiobooks found for this narrator.</div>';
}
async function grInjectAuthorReads(author) {
    const el = document.getElementById('gbaAuthorReads');
    if (!el) return;
    if (!author) { el.innerHTML = ''; return; }
    let d;
    try { d = await GreatReads.apiCall('/news/author-reads?author=' + encodeURIComponent(author)); }
    catch (e) { el.innerHTML = ''; return; }
    if (!d.books || !d.books.length) { el.innerHTML = ''; return; }
    const base = window.APP_BASE_PATH || '';
    const rows = d.books.map(b => {
        const cover = b.has_cover
            ? `<img src="${base}/static/covers/${b.id}.jpg" alt="" loading="lazy" style="width:100%;height:100%;object-fit:cover;" onerror="this.style.display='none';this.nextElementSibling.style.display='flex';"><i class="fas fa-book" style="display:none;color:#adb5bd;"></i>`
            : `<i class="fas fa-book" style="color:#adb5bd;"></i>`;
        const sub = b.series ? `<div class="text-muted text-truncate" style="font-size:.7rem;">${grEsc(b.series)}${b.series_number != null ? ' #' + b.series_number : ''}</div>` : '';
        // Click a read book to open its details in the same popup (#120 Phase 2).
        return `<div class="col-6 col-md-4"><div class="d-flex align-items-center gap-2 mb-1 gba-clickable" onclick="GreatReads.openBookById(${b.id})">
            <div style="width:30px;height:45px;flex:0 0 30px;background:#e9ecef;border-radius:3px;overflow:hidden;display:flex;align-items:center;justify-content:center;">${cover}</div>
            <div class="small" style="min-width:0;"><div class="text-truncate">${grEsc(b.title)}</div>${sub}</div></div></div>`;
    }).join('');
    el.innerHTML = `<div class="text-muted small mb-2">You've read ${d.books.length} by ${grEsc(author)}:</div><div class="row g-1">${rows}</div>`;
}

// Open the shared popup for ANY book by id (#120 Phase 2). Fetches full details so a
// series sibling / author's other book can be opened even if this page never loaded it.
// Re-renders the already-open popup in place → smooth, no close/reopen flash.
// Fetch full details for a local book id and open the popup. keepNav preserves the
// prev/next list (used when stepping through a nav list); direct opens clear it.
async function grFetchAndOpen(id, extraOpts, keepNav) {
    let book;
    try { book = await GreatReads.apiCall('/books/' + id + '/details'); }
    catch (e) { return; }
    const rs = book.readings || [];
    const relevant = rs.find(r => r.date_started && !r.date_finished_actual && r.status !== 'paused')
        || rs.find(r => !r.date_started)
        || rs[rs.length - 1] || null;
    const editReadingHtml = relevant
        ? `<button type="button" class="btn btn-sm btn-outline-secondary" onclick="GreatReads.editReadingById(${relevant.id})"><i class="fas fa-edit me-2 text-primary"></i>Edit Reading</button>`
        : '';
    // A page can supply per-book popup opts (e.g. Library's "Add to TBR") so prev/next
    // nav keeps that page's custom actions on every step (#2).
    const pageOpts = (typeof grNavOptsBuilder === 'function') ? (grNavOptsBuilder(book) || {}) : {};
    grOpenBookActions(book, Object.assign({
        reading: relevant,
        extraInfoHtml: (typeof GreatReads.readingExtraInfoHtml === 'function')
            ? GreatReads.readingExtraInfoHtml(relevant) : '',
        editReadingHtml,
    }, pageOpts, extraOpts || {}), keepNav);
}
// Optional per-page builder: (book) => opts merged into the nav popup (#2).
let grNavOptsBuilder = null;
function grSetNavOptsBuilder(fn) { grNavOptsBuilder = fn; }

// Open the metadata compare window straight from the popup (#177): reuses Edit Book's
// enrichment flow (opens the editor, then the compare window; #157 returns to the popup).
async function grPopupRequestMeta(id) {
    if (id == null || typeof window.bkeOpen !== 'function') return;
    try { await window.bkeOpen(id); } catch (e) { return; }
    if (typeof window.bkeRequestMetadata === 'function') window.bkeRequestMetadata();
}
async function grOpenBookById(id) {
    if (id == null) return;
    bootstrap.Modal.getInstance(document.getElementById('grSeriesModal'))?.hide();  // came from grid
    grNav = { items: [], index: -1 };   // sibling/author click → single, no prev/next arrows
    grFetchAndOpen(id, {});
}

// ── Prev/next nav through a list of books (#100 / #120): items are book ids (local)
// or remote news cards. Used by the Books-page grid; swipe + arrow keys step it. ──
let grNav = { items: [], index: -1 };
function grOpenBookNav(items, index) {
    grNav = { items: items || [], index: index || 0 };
    grOpenNavCurrent();
}
function grOpenNavCurrent() {
    const it = grNav.items[grNav.index];
    if (it == null) return;
    if (typeof it === 'object') grOpenBookActions(grCardToBook(it), grNewsOpts(it), true);  // remote
    else grFetchAndOpen(it, {}, true);                                                      // local id
}
function grNavStep(delta) {
    if (!grNav.items.length) return;
    const i = grNav.index + delta;
    if (i < 0 || i >= grNav.items.length) return;   // clamp at the ends (no wrap)
    grNav.index = i;
    grOpenNavCurrent();
}
function grUpdateNavButtons() {
    const prev = document.getElementById('gbaPrev'), next = document.getElementById('gbaNext');
    const hasNav = grNav.items.length > 1;
    if (prev) { prev.style.display = hasNav ? '' : 'none'; prev.disabled = grNav.index <= 0; }
    if (next) { next.style.display = hasNav ? '' : 'none'; next.disabled = grNav.index < 0 || grNav.index >= grNav.items.length - 1; }
}
// Swipe (touch) + arrow keys step the popup's prev/next nav when a list is active.
function grBindPopupNav() {
    const modal = document.getElementById('openBookModal');
    if (!modal || modal.dataset.navBound) return;
    modal.dataset.navBound = '1';
    let sx = 0, sy = 0;
    modal.addEventListener('touchstart', e => { const t = e.changedTouches[0]; sx = t.clientX; sy = t.clientY; }, { passive: true });
    modal.addEventListener('touchend', e => {
        const t = e.changedTouches[0], dx = t.clientX - sx, dy = t.clientY - sy;
        if (Math.abs(dx) > 60 && Math.abs(dx) > Math.abs(dy) * 1.5) grNavStep(dx < 0 ? 1 : -1);
    }, { passive: true });
    document.addEventListener('keydown', e => {
        if (!modal.classList.contains('show')) return;
        if (e.key === 'ArrowRight') grNavStep(1);
        else if (e.key === 'ArrowLeft') grNavStep(-1);
    });
}
// A remote (news) card → the book-like shape the popup renders (no DB id/inventory).
function grCardToBook(c) {
    return {
        id: (c.book_id != null ? c.book_id : null),
        title: c.title, author: c.author, series: c.series, universe: c.universe,
        series_number: c.series_number, word_count: c.word_count, page_count: c.page_count,
        date_published: c.date, genre: c.genre, cover_url: c.cover_url || null,
        cover: false, inventory: [], readings: [],
    };
}
// News-item actions (Books page only — saveLocally/ignoreRelease live there).
function grNewsOpts(c) {
    const b = [];
    if (c.preview_link) b.push(`<a class="btn btn-sm btn-outline-secondary" href="${c.preview_link}" target="_blank" rel="noopener"><i class="fas fa-eye me-2"></i>Preview</a>`);
    if (c.news_id != null) {
        b.push(`<button class="btn btn-sm btn-outline-success" onclick="saveLocally(${c.news_id})"><i class="fas fa-bookmark me-2"></i>Add to Wishlist</button>`);
        b.push(`<button class="btn btn-sm btn-outline-info" onclick="saveLocally(${c.news_id}, true)"><i class="fas fa-magnifying-glass me-2"></i>Get metadata</button>`);
        b.push(`<button class="btn btn-sm btn-outline-secondary" onclick="ignoreRelease(${c.news_id})"><i class="fas fa-ban me-2"></i>Ignore</button>`);
    }
    return { actionsHtml: b.join(''), editBook: false, sessions: false, highlights: false };
}

// Edit Reading for a nav-opened book — reads the reading off grActiveBook (no page
// context needed) and opens the shared Edit Reading modal (#120 Phase 2).
// Open the shared Edit Reading modal for a reading id. Fetch the reading fresh (page
// data doesn't always carry a full readings[] array — the old grActiveBook.readings
// lookup silently no-op'd on TBR/Home), attach the current book for title/author/progress
// context, then open the modal.
async function grOpenEditReading(rid) {
    if (rid == null) return;
    let rd;
    try { rd = await apiCall('/readings/' + rid); } catch (e) { return; }
    rd.book = grActiveBook || rd.book || {};
    GreatReads.showEditModal(rd);
}
function grEditReadingById(rid) { grOpenEditReading(rid); }

// Edit Reading read-picker (#127): populate the modal's read dropdown from the book's
// readings (hidden for single-read); switching re-opens the modal for that read.
async function grPopulateEditReadDropdown(reading) {
    const row = document.getElementById('editReadRow');
    const sel = document.getElementById('editReadSelect');
    if (!row || !sel || !reading) return;
    const bid = reading.book && reading.book.id;
    const readings = await grLoadReadings(bid, reading.id);
    if (readings.length <= 1) { row.style.display = 'none'; return; }
    sel.innerHTML = readings.map((rd, i) =>
        `<option value="${rd.id}"${rd.id === reading.id ? ' selected' : ''}>${grReadLabel(rd, i)}</option>`).join('');
    row.style.display = '';
}
async function grEditSelectRead(rid) {
    rid = parseInt(rid, 10);
    let rd;
    try { rd = await apiCall('/readings/' + rid); } catch (e) { return; }
    rd.book = grActiveBook || rd.book || {};
    showEditModal(rd);
}

// Build & show the unified cover-tap popup. Used by Library, TBR, and Journal.
//   book : enriched book dict (calibre_id, abs_id, inventory, series, counts…)
//   opts : {
//     title        : override modal title (default book.title),
//     extraInfoHtml: HTML shown between the Read/Listen buttons and details
//                    (e.g. progress / planned-reading section),
//     detailRows   : extra [label, value] pairs appended to the details list
//                    (e.g. WPD),
//     actionsHtml  : HTML for the stacked secondary action buttons at the bottom,
//     onShow       : callback(book) run after the modal is shown (async fills).
//   }
function grOpenBookActions(book, opts = {}, keepNav = false) {
    if (!book) return;
    grActiveBook = book;   // for GreatReads.openSeries() + the injected sections (#120)
    if (!keepNav) grNav = { items: [], index: -1 };   // direct open → no prev/next arrows
    // Read/Listen tiles require BOTH a working source link AND that the user owns
    // the format. calibre_id/abs_id come from ExternalImport, which can go stale
    // (not pruned when a book leaves Calibre/ABS — see #129), so gating on the link
    // alone showed a Read tile for un-owned/gone ebooks (#128). owned_ebook/owned_audio
    // ride along on every book dict via enrich_book_dict → Inventory.to_dict.
    const grOwnsFormat = f => (book.inventory || []).some(i => i[f]);
    const canRead = !!book.calibre_id && grOwnsFormat('owned_ebook');
    const canListen = !!book.abs_id && grOwnsFormat('owned_audio');
    // encodeURIComponent leaves ' unescaped, which would break the inline onclick
    // string for titles with apostrophes — escape it explicitly.
    const titleEnc = encodeURIComponent(book.title || '').replace(/'/g, '%27');
    const authorEnc = encodeURIComponent(book.author || '').replace(/'/g, '%27');

    document.getElementById('openBookTitle').textContent = opts.title || book.title || '';

    // Detail rows: author / series / words / pages, plus any caller extras (WPD…).
    const rows = [];
    // Author is a link → all books by this author (mirrors Series) (#155). With #192,
    // show the primary + a (+N) reveal for additional authors when present.
    const _ct = book.contributors || {};
    if (_ct.authors && _ct.authors.length) rows.push(['Author', grContribLinks(_ct.authors, 'author')]);
    else if (book.author) rows.push(['Author',
        `<a href="#" class="gba-link" onclick="GreatReads.openAuthorName('${encodeURIComponent(book.author).replace(/'/g, '%27')}');return false;">${grEsc(book.author)}</a>`]);
    if (book.series) {
        const num = (book.series_number != null) ? ' #' + book.series_number : '';
        rows.push(['Series', `${book.universe ? book.universe + ': ' : ''}${book.series}${num}`]);
    }
    if (_ct.narrators && _ct.narrators.length) {   // #192 primary + (+N) additional
        rows.push(['Narrator', grContribLinks(_ct.narrators, 'narrator')]);
    } else if (book.narrator) {   // #190 fallback — split the flat string into links
        const nlinks = String(book.narrator).split(',').map(s => s.trim()).filter(Boolean).map(n =>
            `<a href="#" class="gba-link" onclick="GreatReads.openNarrator('${encodeURIComponent(n).replace(/'/g, '%27')}');return false;">${grEsc(n)}</a>`).join(', ');
        rows.push(['Narrator', nlinks]);
    }
    if (book.date_published) {
        // date-only ISO → parse at local midnight so the day doesn't shift a TZ back.
        const iso = String(book.date_published);
        const d = new Date(iso.length <= 10 ? iso + 'T00:00:00' : iso);
        if (!isNaN(d.getTime())) {
            rows.push(['Published', d.toLocaleDateString(undefined, { year: 'numeric', month: 'short', day: 'numeric' })]);
        }
    }
    // Length: words + pages on one line to keep the field list short (#127). When we have
    // pages but no real word count (a Wishlist/release book), show an ESTIMATE derived
    // from pages (~300 wpp) — never stored, so the real Calibre count always wins once we
    // actually get the book (#168).
    const lenParts = [];
    if (book.word_count) lenParts.push(`${Number(book.word_count).toLocaleString()} words`);
    else if (book.page_count) lenParts.push(`≈ ${Math.round(book.page_count * 300 / 1000)}k words (est.)`);
    if (book.page_count) lenParts.push(`${Number(book.page_count).toLocaleString()} pages`);
    // Audiobook runtime from ABS (#213) — '13h 42m' (or '54m' under an hour).
    if (book.audio_duration_seconds) {
        const totMin = Math.round(book.audio_duration_seconds / 60);
        const h = Math.floor(totMin / 60), m = totMin % 60;
        lenParts.push(`🎧 ${h ? `${h}h ${m}m` : `${m}m`}`);
    }
    if (lenParts.length) rows.push(['Length', lenParts.join(' • ')]);
    // Ratings (#150): your private rating (mean of each rated read's overall — the
    // average of the 5 core sub-ratings, EXCLUDING spice/horror — averaged across the
    // book's rated reads) shown beside the public/community rating. Editing still lives
    // behind the View Ratings button below. Only shown when we have at least one.
    {
        const coreKeys = ['rating_enjoyment', 'rating_writing', 'rating_characters',
            'rating_world_building', 'rating_readability'];
        const rOverall = x => {
            if (x && x.rating_overall != null) return x.rating_overall;
            const vals = coreKeys.map(k => x && x[k]).filter(v => v != null && v > 0);
            return vals.length ? vals.reduce((a, b) => a + b, 0) / vals.length : null;
        };
        const privVals = (Array.isArray(book.readings) ? book.readings : []).map(rOverall).filter(v => v != null);
        const priv = privVals.length ? privVals.reduce((a, b) => a + b, 0) / privVals.length : null;
        const parts = [];
        if (priv != null) parts.push(`You ${priv.toFixed(1)}`);
        if (book.public_rating != null) parts.push(`Public ${Number(book.public_rating).toFixed(1)}`);
        if (parts.length) rows.push(['Ratings', `★ ${parts.join(' · ')}`]);
    }
    if (Array.isArray(opts.detailRows)) rows.push(...opts.detailRows.filter(Boolean));

    // Logging progress / a physical session belongs to the READING, not inventory:
    // any in-progress reading qualifies — a physical library book you don't own, or
    // one in progress as ebook/audio you also read physically (#41). The tap entry
    // is the progress display below; pages convert to %, so shared progress stays
    // consistent and physical time logs separately.
    const r = opts.reading;
    const ipReading = (r && r.is_started && !r.is_finished && r.status !== 'paused') ? r : null;
    grActivePhysicalReading = ipReading;

    // ── Cover (top-left) + compact format tiles (#120) ───────────────────────────────
    const base = window.APP_BASE_PATH || '';
    const coverUrl = book.cover_url                                   // remote (news) cover
        ? book.cover_url
        : ((book.cover && book.id != null)                            // local cover file
            ? `${base}/static/covers/${book.id}.jpg?v=${Date.now()}` : '');
    // Remote (news/Libby) books have no DB id → give the popup cover the same Apple
    // Books fallback + 575×750-placeholder detection the grid cards get (#143), so a
    // book that shows a cover in the grid doesn't render "image not available" here.
    const grRemote = book.id == null && !!book.title;
    const grDta = grRemote ? ` data-title="${grEsc(book.title || '')}" data-author="${grEsc(book.author || '')}"` : '';
    const grOnload = grRemote ? ' onload="grCoverOnload(this)"' : '';
    const grOnerr = grRemote ? 'grCoverError(this)' : "this.style.display='none';this.nextElementSibling.style.display='flex';";
    const grPh = `<div class="gba-ph" style="display:none;">${grEsc(book.title || '')}</div>`;
    let coverInner;
    if (coverUrl) {
        coverInner = `<img src="${coverUrl}"${grDta}${grOnload} alt="" onerror="${grOnerr}">${grPh}`;
    } else if (grRemote) {
        // No cover URL at all → go straight to the Apple Books fallback endpoint.
        coverInner = `<img src="${API_BASE}/news/cover?title=${encodeURIComponent(book.title)}&author=${encodeURIComponent(book.author || '')}"${grDta} data-fb="1" alt="" onerror="grCoverError(this)">${grPh}`;
    } else {
        coverInner = `<div class="gba-ph">${grEsc(book.title || '')}</div>`;
    }

    // Owned physical copy → shelf location string, shown as a caption below the tiles
    // (keeps all format tiles the same size).
    const phys = (book.inventory || []).find(i => i.owned_physical);
    let shelfLoc = '';
    if (phys) {
        shelfLoc = phys.location || '';
        if (phys.shelf_bookshelf) {
            shelfLoc = `Shelf ${phys.shelf_bookshelf}` +
                (phys.shelf_shelf != null ? `-${phys.shelf_shelf}` : '') +
                (phys.shelf_position != null ? `, pos ${phys.shelf_position}` : '');
        }
    }
    // Shelf location + Planned dates are normal detail rows now (not separate blocks).
    if (phys && shelfLoc) rows.push(['Shelf', shelfLoc]);
    const planned = !!(r && !r.date_started && !r.date_finished_actual);
    if (planned) {
        const es = r.date_est_start, ee = r.date_est_end || r.date_finished_estimate;
        if (es || ee) rows.push(['Planned', `${es ? formatDateSmart(es) : 'TBD'} – ${ee ? formatDateSmart(ee) : 'TBD'}`]);
    }
    // Reading Position (in-progress): current % with the high-water mark appended async.
    if (ipReading) {
        const cur = Math.round(ipReading.current_progress_percent || 0);
        const pages = book.page_count || 0;
        const pg = pages ? ` · p. ${Math.max(1, Math.round((cur / 100) * pages))} of ${pages.toLocaleString()}` : '';
        rows.push(['Reading Position', `<span id="gbaPos">${cur}%</span><span id="gbaPage" class="text-muted">${pg}</span>`]);
    }
    // Reading Sessions summary (N @ avg) — filled async from the sessions list (#127).
    const startedOrFinished = !!(r && (r.is_started || r.is_finished)) || (book.read_count || 0) > 0;
    if (book.id != null && startedOrFinished) {
        rows.push(['Reading Sessions', '<span id="gbaSess" class="text-muted">…</span>']);
    }

    // Compact format tiles, laid out in a ROW beneath the cover + details. Physical is
    // a peer tile ("Read", purple, book icon); tap-to-log only when in progress.
    const tiles = [];
    if (canRead) tiles.push(`<div class="col"><button type="button" class="open-type-btn open-type-sm open-type-ebook"
        onclick="grOpenEbook('${book.calibre_id}', '${titleEnc}')">
        <i class="fas fa-tablet-alt"></i><span class="fw-bold">Read</span></button></div>`);
    if (canListen) tiles.push(`<div class="col"><button type="button" class="open-type-btn open-type-sm open-type-audio"
        onclick="grOpenAudio('${book.abs_id}', '${titleEnc}', '${authorEnc}', '${book.calibre_id || ''}')">
        <i class="fas fa-headphones"></i><span class="fw-bold">Listen</span></button></div>`);
    if (phys) {
        const physClickable = !!ipReading;
        const cls = 'open-type-btn open-type-sm open-type-physical' + (physClickable ? '' : ' open-type-plain');
        const click = physClickable ? ' onclick="GreatReads.updatePhysicalProgress()"' : '';
        tiles.push(`<div class="col"><button type="button" class="${cls}"${click}>
            <i class="fas fa-book"></i><span class="fw-bold">Read</span></button></div>`);
    }
    const noFormats = !canRead && !canListen && !phys;
    // Row 1: cover top-left, text fields top-right.
    const detailsInner = rows.length ? `<div class="open-details">
        ${rows.map(([k, v]) => `<div class="d-flex justify-content-between gap-3">
            <span class="text-muted">${k}</span><span class="fw-medium text-end">${v}</span>
        </div>`).join('')}</div>` : '';
    const row1 = `
        <div class="col-12"><div class="d-flex gap-3 align-items-start">
            <div class="gba-detail-cover flex-shrink-0">${coverInner}</div>
            <div class="flex-grow-1">${detailsInner}</div>
        </div></div>`;
    // Row 2: format reading buttons in a row, under the cover + fields.
    const formatRow = noFormats
        ? '<div class="col-12 text-muted small"><i class="fas fa-book me-1"></i>Not in your library.</div>'
        : `<div class="col-12"><div class="row g-2">${tiles.join('')}</div></div>`;

    // The progress display is the tap-to-log entry for any in-progress reading
    // (update %/page, start a physical session) — independent of inventory. (#41)
    // The "Currently reading · tap to log progress" block is gone from the popup:
    // Planned is a field, physical progress logs via the Physical tile, and ebook/audio
    // track automatically. (#120)
    const extraInfo = '';

    // Highlights link — shown on any page when the book is a linked ebook. Hidden
    // until the async count below confirms there are some. (Library/TBR/Journal all
    // get this for free.) Opt out with opts.highlights === false.
    // Highlights — shown for a linked ebook only once the async count confirms
    // there are some (revealed below).
    const showHl = opts.highlights !== false && !!book.calibre_id;
    const hlLink = showHl ? `
                <a id="hlActionBtn" class="btn btn-sm btn-outline-secondary d-none"
                   href="/greatreads/highlights?book=${book.calibre_id}&title=${titleEnc}">
                    <i class="fas fa-highlighter me-2" style="color:#e0a800;"></i>Highlights
                    <span id="hlCount" class="badge bg-secondary ms-auto">0</span>
                </a>` : '';

    // Edit Book — open the shared in-place editor (#110). bkeOpen is global on
    // every authed page (book_edit.js). This is the single Edit Book action for
    // every openBookActions popup; pages must NOT add their own. Opt out with
    // opts.editBook === false.
    const editBookLink = (opts.editBook !== false && book.id != null) ? `
                <button type="button" class="btn btn-sm btn-outline-secondary"
                        onclick="bkeOpen(${book.id})">
                    <i class="fas fa-pen-to-square me-2 text-primary"></i>Edit Book
                </button>` : '';

    // Check Libby (#154): for a book you don't own, search Libby by title+author and
    // open Borrow/Hold. Only where the Libby feature is present (grCheckLibby exists →
    // the Store page) and only for non-owned books, so owned/Library books never show it.
    const grNotOwned = !grOwnsFormat('owned_ebook') && !grOwnsFormat('owned_audio')
        && !grOwnsFormat('owned_physical') && !book.is_owned;
    const libbyCheckLink = (grNotOwned && book.title && typeof grCheckLibby === 'function') ? `
                <button type="button" class="btn btn-sm btn-outline-info"
                        onclick="grCheckLibby('${titleEnc}','${authorEnc}')">
                    <i class="fas fa-building-columns me-2"></i>Check Libby
                </button>` : '';

    // Request metadata straight from the popup (#177) — saved books only (needs an id).
    const metaLink = (opts.editBook !== false && book.id != null && typeof window.bkeOpen === 'function') ? `
                <button type="button" class="btn btn-sm btn-outline-secondary"
                        onclick="GreatReads.popupRequestMeta(${book.id})">
                    <i class="fas fa-magnifying-glass me-2 text-info"></i>Request metadata
                </button>` : '';

    // "See Reading Sessions" — read-only session history (#77). Hidden until the
    // async summary below confirms there are qualified sessions; shown on every
    // caller (Home in-progress, Journal finished) via book.id. Opt out with
    // opts.sessions === false.
    // Session stats (Reading Sessions + Avg) show for any started book — in-progress
    // OR finished; the "See Reading Sessions" button is finished-only (#111).
    const startedNotFinished = !!(r && r.is_started && !r.is_finished);
    const hasFinishedRead = !!(r && r.is_finished) ||
        (Array.isArray(book.readings) && book.readings.some(x => x.date_finished_actual));
    // A not-started reread (TBR) has prior finished reads worth viewing even though the
    // current reading isn't started — detect via read_count (prior completions). (#127)
    const hasHistory = hasFinishedRead || (book.read_count || 0) > 0;
    // Show for any started reading OR a reread with history (paired with View Ratings).
    const showSessionsBtn = opts.sessions !== false && book.id != null && (startedNotFinished || hasHistory);
    // Default the sessions view to the clicked read only when it's actually started;
    // a not-started (planned) read → 'All reads' (null) since it has no session window.
    const sessRid = (r && r.is_started) ? r.id : 'null';
    const sessionsBtn = showSessionsBtn ? `
                <button type="button" id="seeSessionsBtn" class="btn btn-sm btn-outline-secondary"
                        onclick="GreatReads.showReadingSessions(${book.id}, '${titleEnc}', ${sessRid})">
                    <i class="fas fa-clock-rotate-left me-2 text-info"></i>View Reading Sessions
                </button>` : '';

    // Word-credit mark (#86): the popup still surfaces the "Word credit resumes past
    // X%" row when the #79 high-water-mark is stuck ahead of progress, but the RESET
    // action now lives in the Edit Reading modal (#111).
    const progKey = book.calibre_id ? String(book.calibre_id) : (book.abs_id ? 'abs:' + book.abs_id : '');

    // View Ratings (#111): only when the passed reading has any category rating.
    // Opens a separate popup (keeps the main popup clean and, later, handles a book
    // read/rated more than once).
    const ratingKeys = ['rating_enjoyment', 'rating_writing', 'rating_characters',
        'rating_world_building', 'rating_readability', 'rating_horror', 'rating_spice'];
    const hasRatingsIn = x => !!x && x.id != null && ratingKeys.some(k => (x[k] || 0) > 0);
    // Look across ALL of the book's readings, not just the popup's reading — e.g. on
    // Library the popup's reading is the in-progress/not-started one, but a *past*
    // reading may be the rated one (#111). (Multi-rated → picker in a later phase.)
    const ratedReadings = (Array.isArray(book.readings) ? book.readings : []).filter(hasRatingsIn);
    const ratingReadingId = hasRatingsIn(r) ? r.id : (ratedReadings[0] ? ratedReadings[0].id : null);
    // Rating target: a rated reading, else a finished one, else the in-progress one — so
    // both finished AND in-progress books get a ratings button ("Not Yet Rated" if none).
    const finishedReading = (r && r.is_finished) ? r
        : (Array.isArray(book.readings) ? book.readings.find(x => x.date_finished_actual) : null);
    // Fall back to the current reading id for a reread with history (its finished reads
    // may not be in this page's book.readings) — the Ratings screen loads all reads (#127).
    const rateTargetId = ratingReadingId != null ? ratingReadingId
        : (finishedReading ? finishedReading.id
        : (ipReading ? ipReading.id
        : (hasHistory && r ? r.id : null)));
    let viewRatingsBtn = '';
    if (rateTargetId != null) {
        const rated = ratingReadingId != null;
        // Rated → View Ratings (read-only). Unrated → "Not Yet Rated" opens the rating editor.
        const icon = rated ? '<i class="fas fa-star me-2" style="color:#e0a800;"></i>'
                           : '<i class="far fa-star me-2 text-muted"></i>';
        const onclick = rated ? `GreatReads.showRatings(${rateTargetId})` : `GreatReads.editRatings(${rateTargetId})`;
        viewRatingsBtn = `
                <button type="button" class="btn btn-sm btn-outline-secondary" onclick="${onclick}">
                    ${icon}${rated ? 'View Ratings' : 'Not Yet Rated'}
                </button>`;
    }

    // Unified button order (#111): Highlights → variable primary action → View
    // Ratings → [extras] → See Reading Sessions → Edit Reading → Edit Book.
    // opts.primaryActionHtml = the one state-specific action (Finish / Start / Add
    // to TBR / Add reread); opts.editReadingHtml = the Edit Reading button (pages
    // with a reading modal); opts.actionsHtml = remaining extras (temporary).
    // View Ratings + See Reading Sessions share a row (both finished-only, so they go
    // together); Edit Reading + Edit Book share the row below.
    const statsPair = `${viewRatingsBtn}${sessionsBtn}`;
    const statsRow = statsPair.trim() ? `<div class="gba-edit-row">${statsPair}</div>` : '';
    const editPair = `${opts.editReadingHtml || ''}${metaLink}${editBookLink}`;
    const editRow = editPair.trim() ? `<div class="gba-edit-row">${editPair}</div>` : '';
    const libbyRow = libbyCheckLink ? `<div class="gba-edit-row">${libbyCheckLink}</div>` : '';
    const secondaryInner = `${hlLink}${opts.primaryActionHtml || ''}${statsRow}${opts.actionsHtml || ''}${libbyRow}${editRow}`;
    const secondary = secondaryInner.trim() ? `
        <div class="col-12">
            <div class="open-secondary">${secondaryInner}</div>
        </div>` : '';

    // Genre tags (chips) + synopsis — shown below the actions (the "rich" details
    // the user asked to match: #149).
    const tagChips = (Array.isArray(book.tags) && book.tags.length)
        ? `<div class="col-12"><div class="d-flex flex-wrap gap-1 mb-1">${
              book.tags.slice(0, 24).map(t => `<a href="#" class="badge bg-light text-dark border text-decoration-none gba-genre-chip" onclick="GreatReads.openGenre('${encodeURIComponent(t).replace(/'/g, '%27')}');return false;">${grEsc(t)}</a>`).join('')
           }</div></div>` : '';
    const synText = grSynopsisText(book.description);
    const synBlock = synText
        ? `<div class="col-12 mt-1"><div class="text-muted small fw-semibold mb-1">Synopsis</div>
             <div class="small" style="white-space:pre-line;max-height:16em;overflow:auto;">${grEsc(synText)}</div></div>` : '';

    document.getElementById('openBookOptions').innerHTML =
        row1 + formatRow + extraInfo + secondary + tagChips + synBlock;
    grLastPopup = { book, opts, keepNav };   // #157: so editors can return here
    bootstrap.Modal.getOrCreateInstance(document.getElementById('openBookModal')).show();

    // #120: enrich with the "In this series" strip + "You've read # by <author>" section
    // (async; clear-then-fill so they don't carry over between books). Universe already
    // shows in the Series detail row above.
    grInjectSeriesStrip(book);
    grInjectAuthorReads(book.author);
    grUpdateNavButtons();

    // Fill the highlights count (reveal the link only if there are any).
    if (showHl) {
        fetch(`${GR_EREADER_API}/highlights?type=highlight&bookId=${encodeURIComponent(book.calibre_id)}`)
            .then(r => r.ok ? r.json() : null)
            .then(d => {
                const n = d && Array.isArray(d.items) ? d.items.length : 0;
                const btn = document.getElementById('hlActionBtn');
                const cnt = document.getElementById('hlCount');
                if (btn && n > 0) { cnt.textContent = n; btn.classList.remove('d-none'); }
            })
            .catch(() => {});
    }

    // Real reading/listening time for this book (#30). Lazy-fetched and only shown
    // once there's logged time — books read before activity logging show nothing.
    if (book.id != null) {
        apiCall(`/stats/book-time/${book.id}`).then(d => {
            if (!d || !(d.total_minutes > 0)) return;
            const det = document.querySelector('#openBookOptions .open-details');
            if (!det) return;
            const parts = [];
            for (const fmt of ['Ebook', 'Audio', 'Physical']) {
                const f = d.formats && d.formats[fmt];
                if (f && f.minutes > 0) {
                    parts.push(`<div class="d-flex justify-content-between gap-3">
                        <span class="text-muted">Time read${parts.length || hasOther(d, fmt) ? ` (${fmt})` : ''}</span>
                        <span class="fw-medium text-end">${formatDuration(f.minutes)}${f.wpm ? ` · ${f.wpm} wpm` : ''}</span>
                    </div>`);
                }
            }
            det.insertAdjacentHTML('beforeend', parts.join(''));
        }).catch(() => {});
    }

    // Reading-session summary (#77, #127): count + avg sitting length on ONE line —
    // "18 @ 17m avg." — filling the placeholder added to the details above.
    if (book.id != null && startedOrFinished) {
        const fillSess = (sessions, minutes) => {
            const el = document.getElementById('gbaSess');
            if (!el) return;
            if (sessions > 0) {
                el.textContent = `${sessions} @ ${formatDuration(minutes / sessions)} avg`;
                el.classList.remove('text-muted');
            } else { el.textContent = 'None yet'; }
        };
        fetch(`${GR_EREADER_API}/sessions-summary-gr/${book.id}`)
            .then(r => r.ok ? r.json() : null)
            .then(d => fillSess((d && d.sessions) || 0, (d && d.minutes) || 0))
            .catch(() => fillSess(0, 0));
    }

    // Reading Position (#86, #127): live current % with the #79 high-water mark folded
    // in — "83% (85% max)", or just "83%" when at the mark. Fills the placeholder above.
    if (ipReading && progKey) {
        fetch(`${GR_EREADER_API}/progress/${encodeURIComponent(progKey)}`)
            .then(r => r.ok ? r.json() : null)
            .then(p => {
                const el = document.getElementById('gbaPos');
                if (!el || !p || typeof p.progress !== 'number') return;
                const pct = Math.round(p.progress * 100);
                const hwm = (typeof p.maxProgress === 'number') ? Math.round(p.maxProgress * 100) : null;
                el.textContent = (hwm != null && hwm > pct) ? `${pct}% (${hwm}% max)` : `${pct}%`;
                const pel = document.getElementById('gbaPage');
                const pages = book.page_count || 0;
                if (pel && pages) pel.textContent = ` · p. ${Math.max(1, Math.round((pct / 100) * pages))} of ${pages.toLocaleString()}`;
            })
            .catch(() => {});
    }

    if (typeof opts.onShow === 'function') opts.onShow(book);
}

// ── Shared Edit Reading modal handlers (#111) ────────────────────────────────
// The modal markup lives in _edit_reading_modal.html (mounted via base.html) so
// every page — including Library — can edit a reading. These are the button
// handlers; reload happens via the page's global refreshReadings() (full reload
// as a fallback). Moved here from index/tbr so they exist everywhere.
function _erCloseEditModal() { bootstrap.Modal.getInstance(document.getElementById('editReadingModal'))?.hide(); }
function _erReload() { if (typeof refreshReadings === 'function') refreshReadings(); else location.reload(); }

async function startReadingFromModal() {
    const id = document.getElementById('editReadingId').value;
    if (!confirm("Start this reading with today's date?")) return;
    try {
        await apiCall(`/readings/${id}`, { method: 'PUT', data: { date_started: new Date().toISOString().split('T')[0], status: 'In Progress' } });
        showToast('Reading started!', 'success'); _erCloseEditModal(); _erReload();
    } catch (e) {}
}
async function startReadingManualFromModal() {
    const id = document.getElementById('editReadingId').value;
    const d = prompt('Enter start date (YYYY-MM-DD):', new Date().toISOString().split('T')[0]);
    if (!d) return;
    try { await startReading(id, d); showToast('Reading started with custom date!', 'success'); _erCloseEditModal(); _erReload(); } catch (e) {}
}
async function pauseReadingFromModal() {
    const id = document.getElementById('editReadingId').value;
    if (!confirm('Pause this reading? Progress will be frozen at the current point.')) return;
    try { await pauseReading(id); showToast('Reading paused!', 'success'); _erCloseEditModal(); _erReload(); } catch (e) {}
}
async function unpauseReadingFromModal() {
    const id = document.getElementById('editReadingId').value;
    if (!confirm('Unpause this reading? Progress will resume from where it was paused.')) return;
    try { await unpauseReading(id); showToast('Reading unpaused!', 'success'); _erCloseEditModal(); _erReload(); } catch (e) {}
}
async function deleteReadingFromModal() {
    const id = document.getElementById('editReadingId').value;
    if (!confirm('Delete this reading? This action cannot be undone.')) return;
    try {
        await apiCall(`/readings/${id}`, { method: 'DELETE' });
        showToast('Reading deleted', 'success');
        _erCloseEditModal();
        bootstrap.Modal.getInstance(document.getElementById('openBookModal'))?.hide();
        _erReload();
    } catch (e) { showToast('Failed to delete reading', 'danger'); }
}
async function clearProgressFromModal() {
    const id = document.getElementById('editReadingId').value;
    if (!id) return;
    if (!confirm('Clear reading progress for this reading?')) return;
    try {
        await apiCall(`/readings/${id}/progress`, { method: 'PUT', params: { current_percent: 0 } });
        // also clear any linked ereader progress when we know the book's keys
        const el = document.getElementById('editReadingModal');
        const keys = (el && el.dataset.progressKeys) ? el.dataset.progressKeys.split(',').filter(Boolean) : [];
        for (const k of keys) await fetch(`${GR_EREADER_API}/progress/${encodeURIComponent(k)}`, { method: 'DELETE' }).catch(() => {});
        showToast('Reading progress cleared', 'success');
        _erCloseEditModal();
        bootstrap.Modal.getInstance(document.getElementById('openBookModal'))?.hide();
        _erReload();
    } catch (e) { showToast('Failed to clear progress', 'danger'); }
}
// Reset the #79 word-credit high-water-mark (#86) from the Edit Reading modal —
// only revealed by showEditModal when the mark is actually stuck ahead of progress.
function resetCreditFromModal() {
    const btn = document.getElementById('editResetCreditBtn');
    const key = btn && btn.dataset.key;
    if (!key) return;
    fetch(`${GR_EREADER_API}/progress/${encodeURIComponent(key)}/reset-credit-mark`, { method: 'POST' })
        .then(r => r.ok ? r.json() : null)
        .then(d => {
            showToast(d ? 'Word-credit mark reset — forward reading will count again'
                        : 'Could not reset (no saved progress yet)', d ? 'success' : 'warning');
            _erCloseEditModal();
            bootstrap.Modal.getInstance(document.getElementById('openBookModal'))?.hide();
            _erReload();
        })
        .catch(() => showToast('Reset failed', 'warning'));
}

// ── Shared read selector (#127): a book has 1..N readings; the ratings/edit/sessions
// screens let you pick which read. Chronological order (Read #1 = earliest). ──
function grSortReadings(readings) {
    // Chronological (Read #1 = oldest). A planned/not-started read has no start/finish,
    // so key off its est-start, else a far-future sentinel so it sorts LAST (newest read).
    const key = r => r.date_started || r.date_finished_actual || r.date_est_start || '9999-12-31';
    return [...(readings || [])].sort((a, b) => String(key(a)).localeCompare(String(key(b))));
}
function grReadLabel(reading, index) {
    let sub;
    if (reading.date_finished_actual) sub = 'finished ' + formatDateSmart(reading.date_finished_actual);
    else if (reading.date_started) sub = 'in progress';
    else sub = 'not started';
    return `Read #${index + 1} · ${sub}`;
}
// A <select> of the book's reads (hidden when there's only one). extraOpts prepends
// custom entries (e.g. sessions' "All reads").
function grReadDropdown(readings, selectedRid, onchangeFn, extraOpts) {
    if ((!readings || readings.length <= 1) && !(extraOpts && extraOpts.length)) return '';
    const pre = (extraOpts || []).map(([v, label]) =>
        `<option value="${v}"${String(v) === String(selectedRid) ? ' selected' : ''}>${label}</option>`).join('');
    const opts = readings.map((rd, i) =>
        `<option value="${rd.id}"${rd.id === selectedRid ? ' selected' : ''}>${grReadLabel(rd, i)}</option>`).join('');
    return `<div class="mb-3"><select class="form-select form-select-sm" onchange="${onchangeFn}(this.value)">${pre}${opts}</select></div>`;
}
// Load a book's readings for the pickers (robust: page data may lack a full readings[]).
async function grLoadReadings(bid, fallbackRid) {
    if (bid != null) {
        try { const d = await apiCall('/books/' + bid + '/details'); if (d.readings) return grSortReadings(d.readings); }
        catch (e) { /* fall through */ }
    }
    if (fallbackRid != null) {
        try { return [await apiCall('/readings/' + fallbackRid)]; } catch (e) { /* */ }
    }
    return [];
}

// ── Ratings screen (#127): view + edit a read's ratings, with a read picker. ──
const GR_RATING_CATS = [
    ['enjoyment', 'Enjoyment', 'star'], ['writing', 'Writing Quality', 'star'],
    ['characters', 'Characters', 'star'], ['world_building', 'World Building', 'star'],
    ['readability', 'Readability', 'star'], ['horror', 'Horror', 'blood'], ['spice', 'Spice', 'pepper'],
];
let grRatingsState = { readings: [], rid: null };
async function grOpenRatings(rid) {
    const body = document.getElementById('vrModalBody');
    if (!body) return;
    body.innerHTML = '<div class="text-center text-muted py-3"><i class="fas fa-spinner fa-spin"></i></div>';
    bootstrap.Modal.getOrCreateInstance(document.getElementById('viewRatingsModal')).show();
    const bid = grActiveBook && grActiveBook.id;
    let readings = await grLoadReadings(bid, rid);
    // Stale-context guard (#212): opened straight from a finish (no popup), the
    // last-viewed book may not own this reading — reload by the reading itself.
    if (rid != null && readings.length && !readings.find(x => x.id === rid)) {
        readings = await grLoadReadings(null, rid);
    }
    if (!readings.length) { body.innerHTML = '<div class="text-danger small">Could not load ratings.</div>'; return; }
    // Default selection: the passed read, but if it's a planned/not-started read (reread
    // opened from TBR) prefer the most recent FINISHED read — that's what has ratings.
    let sel = readings.find(x => x.id === rid);
    if (sel && !sel.date_started && !sel.date_finished_actual) {
        const fin = grSortReadings(readings.filter(x => x.date_finished_actual));
        if (fin.length) sel = fin[fin.length - 1];
    }
    grRatingsState = { readings, rid: (sel ? sel.id : readings[readings.length - 1].id) };
    grRenderRatings();
}
function grRenderRatings() {
    const body = document.getElementById('vrModalBody');
    const { readings, rid } = grRatingsState;
    // "All reads (avg)" — read-only average per category across reads that rated it.
    const extra = readings.length > 1 ? [['all', '★ All reads (avg)']] : [];
    const dropdown = grReadDropdown(readings, rid, 'GreatReads.ratingsSelectRead', extra);
    if (rid === 'all') {
        const rows = GR_RATING_CATS.map(([key, label, emoji]) => {
            const vals = readings.map(x => x['rating_' + key] || 0).filter(v => v > 0);
            if (!vals.length) return '';
            const avg = vals.reduce((a, b) => a + b, 0) / vals.length;
            return `<div class="d-flex justify-content-between gap-3 py-1 border-bottom">
                <span class="text-muted">${label}</span>
                <span class="fw-medium text-end">${avg.toFixed(1)} ${getEmojiForType(emoji)}
                    <span class="small text-muted">(avg of ${vals.length})</span></span>
            </div>`;
        }).filter(Boolean).join('');
        body.innerHTML = dropdown + (rows || '<div class="text-muted small py-2">No ratings across reads yet.</div>');
        return;
    }
    const reading = readings.find(x => x.id === rid) || readings[0];
    const blocks = GR_RATING_CATS.map(([key, label, emoji]) => `
        <div class="col-6 mb-3">
            <label class="form-label small mb-1">${label}</label>
            <div class="emoji-rating" data-rating-type="${key}" data-emoji="${emoji}">
                <input type="hidden" id="vrRating_${key}" value="${reading['rating_' + key] || 0}">
                <div class="emoji-rating-display"></div>
            </div>
        </div>`).join('');
    body.innerHTML = `
        ${dropdown}
        <div class="row">${blocks}</div>
        <div class="d-flex justify-content-end mt-1">
            <button type="button" class="btn btn-sm btn-primary" onclick="GreatReads.saveRatings()">
                <i class="fas fa-check me-1"></i>Save ratings</button>
        </div>`;
    body.querySelectorAll('.emoji-rating').forEach(c => initEmojiRating(c));
}
function grRatingsSelectRead(sel) { grRatingsState.rid = (sel === 'all') ? 'all' : parseInt(sel, 10); grRenderRatings(); }
async function grSaveRatings() {
    const rid = grRatingsState.rid;
    if (rid == null || rid === 'all') return;   // 'all' is a read-only average view
    const data = {};
    GR_RATING_CATS.forEach(([key]) => {
        const el = document.getElementById('vrRating_' + key);
        const v = el ? (parseInt(el.value) || 0) : 0;
        data['rating_' + key] = v > 0 ? v : null;
    });
    try { await apiCall('/readings/' + rid, { method: 'PUT', data }); }
    catch (e) { if (typeof showToast === 'function') showToast('Save failed', 'danger'); return; }
    const reading = grRatingsState.readings.find(x => x.id === rid);
    if (reading) GR_RATING_CATS.forEach(([key]) => { reading['rating_' + key] = data['rating_' + key]; });
    if (typeof showToast === 'function') showToast('Ratings saved', 'success');
    if (typeof window.refreshReadings === 'function') { try { window.refreshReadings(); } catch (e) {} }
}

// Reading Sessions (#77, #127): every qualified sitting for a book, with a read picker
// (multi-read) that filters sessions to the selected read's date window. "All reads"
// shows everything. Columns: Date & Time (YYMMDD - HH:MM-HH:MM), Duration, Words, WPM.
let grSessState = { readings: [], sessions: [], sel: 'all' };
async function grShowReadingSessions(bookId, titleEnc, rid) {
    const modalEl = document.getElementById('readingSessionsModal');
    if (!modalEl) return;
    const title = decodeURIComponent(titleEnc || '');
    document.getElementById('rsModalTitle').textContent =
        title ? `Reading Sessions — ${title}` : 'Reading Sessions';
    const body = document.getElementById('rsModalBody');
    body.innerHTML = '<div class="text-center text-muted py-3"><i class="fas fa-spinner fa-spin"></i></div>';
    bootstrap.Modal.getOrCreateInstance(modalEl).show();
    const [readings, sessionsResp] = await Promise.all([
        grLoadReadings(bookId, rid),
        fetch(`${GR_EREADER_API}/sessions-list-gr/${bookId}`).then(r => r.ok ? r.json() : null).catch(() => null),
    ]);
    grSessState = {
        bookId,
        readings,
        sessions: (sessionsResp && sessionsResp.sessions) || [],
        // Default to the clicked read when it exists, else all.
        sel: (rid != null && readings.find(x => x.id === rid)) ? rid : 'all',
    };
    grRenderSessions();
}
function grSessSelectRead(sel) { grSessState.sel = (sel === 'all') ? 'all' : parseInt(sel, 10); grRenderSessions(); }
function grRenderSessions() {
    const body = document.getElementById('rsModalBody');
    const { readings, sessions, sel } = grSessState;
    // Filter sessions to the selected read's [start → finish|now] window (by activity date).
    let shown = sessions;
    if (sel !== 'all') {
        const rd = readings.find(x => x.id === sel);
        if (rd) {
            const startMs = rd.date_started ? new Date(rd.date_started + 'T00:00:00').getTime() : -Infinity;
            const endMs = rd.date_finished_actual ? new Date(rd.date_finished_actual + 'T23:59:59').getTime() : Infinity;
            shown = sessions.filter(ss => ss.startedAt >= startMs && ss.startedAt <= endMs);
        }
    }
    const fmtDT = (start, end) => {
        const s = new Date(start), e = new Date(end), p = n => String(n).padStart(2, '0');
        const ymd = p(s.getFullYear() % 100) + p(s.getMonth() + 1) + p(s.getDate());
        const hm = d => p(d.getHours()) + ':' + p(d.getMinutes());
        return `${ymd} - ${hm(s)}-${hm(e)}`;
    };
    const picker = grReadDropdown(readings, sel, 'GreatReads.sessionsSelectRead', [['all', 'All reads']]);
    // Footer tool (#136): recompute ebook word-credit from each session's progress
    // range + reset the high-water-mark, for when a poisoned mark zeroed real reading.
    const fixTool = `<div class="d-flex justify-content-end mt-2">
        <button type="button" class="btn btn-sm btn-outline-secondary" onclick="GreatReads.recreditSessions()"
            title="Recompute ebook words from each session's progress range and reset the word-credit mark">
            <i class="fas fa-wand-magic-sparkles me-1"></i>Fix word credit</button></div>`;
    if (!shown.length) {
        body.innerHTML = picker + '<p class="text-muted mb-0 text-center py-3">No reading sessions recorded.</p>' + fixTool;
        return;
    }
    const rowsHtml = shown.map(ss => `
                <tr>
                    <td class="text-nowrap">${fmtDT(ss.startedAt, ss.endedAt)}</td>
                    <td class="text-end">${formatDuration(ss.minutes)}</td>
                    <td class="text-end">${Number(ss.words || 0).toLocaleString()}</td>
                    <td class="text-end">${ss.wpm != null ? ss.wpm : '—'}</td>
                </tr>`).join('');
    body.innerHTML = picker + `
                <div class="table-responsive">
                    <table class="table table-sm mb-0 align-middle">
                        <thead><tr>
                            <th>Date &amp; Time</th>
                            <th class="text-end">Duration</th>
                            <th class="text-end">Words</th>
                            <th class="text-end">WPM</th>
                        </tr></thead>
                        <tbody>${rowsHtml}</tbody>
                    </table>
                </div>` + fixTool;
}

// #136: recompute this book's ebook session word-credit from progress ranges +
// reset the high-water-mark, then refresh the sessions view to show the new words.
async function grRecreditSessions() {
    const bookId = grSessState && grSessState.bookId;
    if (!bookId) return;
    if (!confirm('Recompute ebook word-credit for this book from each session’s progress range, and reset the credit mark?')) return;
    try {
        const r = await fetch(`${GR_EREADER_API}/sessions-gr/${bookId}/recredit`, { method: 'POST' });
        const d = r.ok ? await r.json() : null;
        if (d) {
            showToast(`Recredited ${Number(d.recredited_words || 0).toLocaleString()} words across ${d.days} day(s)`, 'success');
            const sessionsResp = await fetch(`${GR_EREADER_API}/sessions-list-gr/${bookId}`).then(x => x.ok ? x.json() : null).catch(() => null);
            grSessState.sessions = (sessionsResp && sessionsResp.sessions) || [];
            grRenderSessions();
        } else {
            showToast('Nothing to recredit (no ebook sessions)', 'warning');
        }
    } catch (e) { showToast('Recredit failed', 'warning'); }
}

// Reset the #79 word-credit high-water-mark to the current position (#86), so
// forward reading credits words again after it got poisoned (e.g. a broken EPUB).
function grResetCreditMark(key) {
    if (!key) return;
    fetch(`${GR_EREADER_API}/progress/${encodeURIComponent(key)}/reset-credit-mark`, { method: 'POST' })
        .then(r => r.ok ? r.json() : null)
        .then(d => {
            showToast(d ? 'Word-credit mark reset — forward reading will count again'
                        : 'Could not reset (no saved progress yet)', d ? 'success' : 'warning');
            const m = bootstrap.Modal.getInstance(document.getElementById('openBookModal'));
            if (m) m.hide();
        })
        .catch(() => showToast('Reset failed', 'warning'));
}

// Tap the Physical card (in-progress physical book) → lightweight progress popup.
// Reuses PUT /readings/{id}/progress, which sets the manual override + recalcs
// days_estimate/chains; physical progress then keeps advancing via WPD (#33).
function grUpdatePhysicalProgress(addMinutes, session) {
    const reading = grActivePhysicalReading;
    if (!reading) return;

    const coverModal = bootstrap.Modal.getInstance(document.getElementById('openBookModal'));
    if (coverModal) coverModal.hide();

    const modalEl = document.getElementById('physProgressModal');
    if (!modalEl) return;

    const totalPages = (reading.book && reading.book.page_count) || 0;
    const pageWrap = document.getElementById('upPageWrap');
    const pctWrap = document.getElementById('upPercentWrap');
    const pageEl = document.getElementById('upPage');
    const pctEl = document.getElementById('upPercent');

    document.getElementById('upReadingId').value = reading.id;
    document.getElementById('upBookTitle').textContent = (reading.book && reading.book.title) || '';

    // "Time read today" = today's running total (persists across opens, resets at
    // midnight = a new day's row). Load the saved total, then add any minutes
    // handed in from a just-finished reading session. The +/- stepper edits the
    // total; Save overwrites today's logged minutes with it. (#39/#40)
    const minEl = document.getElementById('upMinutes');
    const extra = Math.max(0, parseInt(addMinutes) || 0);
    minEl.value = extra;
    // Load today's running total, then add any session minutes. Save() awaits this
    // promise so it never reads a racy 0 and wipes the logged total. (#44)
    let _minutesLoaded = false, _minutesTouched = false;
    const minutesLoad = apiCall(`/readings/${reading.id}/today-minutes`)
        .then(d => {
            // Don't clobber a value the user already adjusted while the GET was in flight.
            if (!_minutesTouched) minEl.value = ((d && typeof d.minutes === 'number') ? d.minutes : 0) + extra;
            _minutesLoaded = true;
        })
        .catch(() => {});
    const stepMin = (delta) => { _minutesTouched = true; minEl.value = Math.max(0, (parseInt(minEl.value) || 0) + delta); };
    document.getElementById('upMinutesMinus').onclick = () => stepMin(-5);
    document.getElementById('upMinutesPlus').onclick = () => stepMin(5);

    // Physical progress is entered by PAGE (percent is derived and shown on the
    // card/progress, not prompted here). Percent input is only a fallback for a
    // book with no page count, since the API stores a percent. (#33)
    if (totalPages > 0) {
        pageWrap.style.display = '';
        pctWrap.style.display = 'none';
        pageEl.value = reading.current_progress_page || '';
        pageEl.max = totalPages;
        document.getElementById('upTotalPages').textContent = `of ${totalPages}`;
    } else {
        pageWrap.style.display = 'none';
        pctWrap.style.display = '';
        const curPct = reading.current_progress_percent || 0;
        pctEl.value = curPct ? curPct.toFixed(1) : '';
    }

    document.getElementById('upSaveBtn').onclick = async () => {
        let pct;
        if (totalPages > 0) {
            const p = parseInt(pageEl.value);
            if (isNaN(p)) { showToast('Enter a page number', 'warning'); return; }
            pct = (p / totalPages) * 100;
        } else {
            pct = parseFloat(pctEl.value);
            if (isNaN(pct)) { showToast('Enter a percent', 'warning'); return; }
        }
        pct = Math.max(0, Math.min(100, pct));
        // Wait for today's total to load before reading the field, so a quick Save
        // never sends 0 and wipes today's logged minutes. If it never loaded (and the
        // user didn't touch it), omit minutes_read so the backend preserves it. (#44)
        await minutesLoad;
        const params = { current_percent: pct, physical: true };  // #183: credit physical words even if the reading's media is Audio/Ebook
        if (_minutesLoaded || _minutesTouched) params.minutes_read = Math.max(0, parseInt(minEl.value) || 0);
        // From a timed reading session → also record a per-sitting Physical session
        // row (#78): minutes from the timer, words from this session's % delta.
        if (session && session.startMs) {
            params.session_id = (window.crypto && window.crypto.randomUUID)
                ? window.crypto.randomUUID()
                : ('phys-' + Date.now() + '-' + Math.floor(Math.random() * 1e9));
            params.session_start_ms = session.startMs;
            params.session_seconds = Math.round(session.seconds || 0);
            params.start_percent = session.startPct || 0;
        }
        try {
            await apiCall(`/readings/${reading.id}/progress`, {
                method: 'PUT',
                params
            });
            const inst = bootstrap.Modal.getInstance(modalEl);
            if (inst) inst.hide();
            showToast('Progress updated', 'success');
            // Re-fetch so the new % / page / WPD-derived estimates show everywhere.
            if (typeof refreshReadings === 'function') refreshReadings();
            else location.reload();
        } catch (e) { /* handled by apiCall */ }
    };

    bootstrap.Modal.getOrCreateInstance(modalEl).show();
}

// ---- Reading session (Start Reading → live timer) --------------------------
// Launched from the physical progress popup. Full-screen timer, keeps the screen
// awake, auto-pauses on screen lock / background, prompts to resume on return.
// "Done" hands the elapsed minutes back to the popup to add to today's total. (#40)
let _sess = null;          // { readingId, accSec, startMs, running, tid }
let _grWake = null;
let _grForceWake = false;   // true during an active reading session → always awake

// Global "keep screen awake" — honors the app-wide setting (ereader.settings.keepAwake)
// EVERYWHERE in the app (not just while actively reading; not released on pause).
// Re-applied on return-to-foreground and first touch because Android WebView drops
// the lock. The reader (reader.html) has its own equivalent. (#40)
function _grKeepAwakeWanted() {
    try { return localStorage.getItem('ereader.settings.keepAwake') === '1'; } catch (_) { return false; }
}
async function grApplyGlobalWakeLock() {
    if (_grForceWake || _grKeepAwakeWanted()) {
        if (window.Android && typeof window.Android.keepScreenOn === 'function') {
            try { window.Android.keepScreenOn(true); return; } catch (_) {}
        }
        if ('wakeLock' in navigator) {
            try { if (!_grWake || _grWake.released) _grWake = await navigator.wakeLock.request('screen'); } catch (_) {}
        }
    } else {
        if (window.Android && typeof window.Android.keepScreenOn === 'function') {
            try { window.Android.keepScreenOn(false); } catch (_) {}
        }
        if (_grWake && !_grWake.released) { try { _grWake.release(); } catch (_) {} }
        _grWake = null;
    }
}
document.addEventListener('DOMContentLoaded', grApplyGlobalWakeLock);
document.addEventListener('DOMContentLoaded', grBindPopupNav);
document.addEventListener('visibilitychange', () => { if (document.visibilityState === 'visible') grApplyGlobalWakeLock(); });
document.addEventListener('touchstart', grApplyGlobalWakeLock, { passive: true });

function _sessElapsedSec() { return _sess.accSec + (_sess.running ? (Date.now() - _sess.startMs) / 1000 : 0); }
function _fmtTimer(sec) {
    sec = Math.floor(sec);
    const h = Math.floor(sec / 3600), m = Math.floor((sec % 3600) / 60), s = sec % 60;
    const ss = String(s).padStart(2, '0');
    return h ? `${h}:${String(m).padStart(2, '0')}:${ss}` : `${m}:${ss}`;
}
function _sessTick() { const t = document.getElementById('sessTimer'); if (t) t.textContent = _fmtTimer(_sessElapsedSec()); }

function grStartReadingSession() {
    const reading = grActivePhysicalReading;
    if (!reading) return;
    // Physical reading is page-based — require a page count first (#41). Route to
    // Edit Book to set it if missing.
    if (!(reading.book && reading.book.page_count)) {
        if (confirm("This book has no page count, which physical reading needs. Set it now?")) {
            window.location.href = `${BASE_PATH}/library?editBook=${reading.book ? reading.book.id : ''}`;
        }
        return;
    }
    const pm = bootstrap.Modal.getInstance(document.getElementById('physProgressModal'));
    if (pm) pm.hide();

    const _sessNow = Date.now();
    _sess = { readingId: reading.id, accSec: 0, startMs: _sessNow, origStartMs: _sessNow, running: true, tid: null,
              startPct: (reading.current_progress_percent || 0) };  // origStartMs = true session start, never reset on resume (#89); startMs = current running segment's start; startPct = session start position (#78)
    _grForceWake = true;   // never let the screen time out during a reading session
    document.getElementById('sessBookTitle').textContent = (reading.book && reading.book.title) || '';
    document.getElementById('sessState').textContent = 'Reading…';
    document.getElementById('sessResumeBtn').classList.add('d-none');
    document.getElementById('sessPauseBtn').classList.remove('d-none');
    _sessTick();
    _sess.tid = setInterval(_sessTick, 1000);
    grApplyGlobalWakeLock();

    document.getElementById('sessPauseBtn').onclick = grSessionPause;
    document.getElementById('sessResumeBtn').onclick = grSessionResume;
    document.getElementById('sessDoneBtn').onclick = grSessionDone;
    document.addEventListener('visibilitychange', _sessVis);

    bootstrap.Modal.getOrCreateInstance(document.getElementById('readingSessionModal')).show();
    grSessAmbientStart(reading);   // auto-play ambient music (Start Reading is a gesture)
    grSessBrightnessStart();       // apply remembered screen brightness
}

function grSessionPause() {
    if (!_sess || !_sess.running) return;
    _sess.accSec = _sessElapsedSec();
    _sess.running = false;
    if (_sess.tid) { clearInterval(_sess.tid); _sess.tid = null; }
    document.getElementById('sessPauseBtn').classList.add('d-none');
    document.getElementById('sessResumeBtn').classList.remove('d-none');
    document.getElementById('sessState').textContent = 'Paused';
    grApplyGlobalWakeLock();
    grSessAmbientSetEditable(true);   // track change allowed only while paused
}

function grSessionResume() {
    if (!_sess || _sess.running) return;
    _sess.startMs = Date.now();
    _sess.running = true;
    _sessTick();
    _sess.tid = setInterval(_sessTick, 1000);
    document.getElementById('sessResumeBtn').classList.add('d-none');
    document.getElementById('sessPauseBtn').classList.remove('d-none');
    document.getElementById('sessState').textContent = 'Reading…';
    grApplyGlobalWakeLock();
    grSessAmbientSetEditable(false);   // running → lock the track picker
}

// Screen lock / app background → auto-pause; the Resume button shows on return.
function _sessVis() { if (document.hidden && _sess && _sess.running) grSessionPause(); }

function grSessionDone() {
    if (!_sess) return;
    const mins = Math.round(_sessElapsedSec() / 60);
    // Capture the sitting's timing + start position before tearing _sess down, so
    // the save can record a Physical reading_sessions row (#78).
    // startMs = the TRUE session start (origStartMs), so started_at→ended_at spans the
    // real elapsed window even across pauses; seconds = active reading time (pauses
    // excluded) → session-time for WPM. (#89)
    const session = { startMs: _sess.origStartMs, seconds: _sessElapsedSec(), startPct: _sess.startPct || 0 };
    if (_sess.tid) clearInterval(_sess.tid);
    document.removeEventListener('visibilitychange', _sessVis);
    _grForceWake = false;             // session over → revert to the global setting
    grApplyGlobalWakeLock();
    _sess = null;
    grSessAmbientStop();
    grSessBrightnessStop();           // restore system brightness on leaving
    const sm = bootstrap.Modal.getInstance(document.getElementById('readingSessionModal'));
    if (sm) sm.hide();
    grUpdatePhysicalProgress(mins, session);   // reopen popup; also logs a Physical session row (#78)
}

// ---- Ambient music for the physical reading session (#32) ------------------
// Auto-plays on Start Reading (a gesture), loops, single play/pause, track
// picker enabled only when the session is paused. Track + position remembered
// per physical book (by book id, separate from the ebook's choice).
const _AMB_BASE = GR_EREADER_API + '/ambient';
let _appAmb = null, _appAmbKey = null, _appAmbTrackId = null, _appAmbTracks = [], _appAmbSaveT = 0;

function _appAmbIcon() {
    const b = document.getElementById('sessAmbientBtn');
    if (b) b.innerHTML = (_appAmb && !_appAmb.paused) ? '<i class="fas fa-pause"></i>' : '<i class="fas fa-play"></i>';
}
function grSessAmbientSave() {
    if (_appAmb && _appAmbKey && !isNaN(_appAmb.currentTime)) {
        try { localStorage.setItem('gr_ambient_pos_' + _appAmbKey, String(_appAmb.currentTime)); } catch (_) {}
    }
}
function grSessAmbientSetEditable(on) {
    const sel = document.getElementById('sessAmbientTrack');
    if (sel) sel.disabled = !on;
}
function grSessAmbientLoad(tid, wantPlay) {
    _appAmbTrackId = tid;
    _appAmb.src = _AMB_BASE + '/' + encodeURIComponent(tid);
    const pos = parseFloat(localStorage.getItem('gr_ambient_pos_' + _appAmbKey) || '0');
    _appAmb.addEventListener('loadedmetadata', function once() {
        _appAmb.removeEventListener('loadedmetadata', once);
        if (pos > 0 && pos < (_appAmb.duration || Infinity)) { try { _appAmb.currentTime = pos; } catch (_) {} }
        if (wantPlay) _appAmb.play().catch(() => {});
    });
    _appAmb.load();
}
function grSessAmbientToggle() {
    if (!_appAmb || !_appAmbTrackId) return;
    if (_appAmb.paused) _appAmb.play().catch(() => {});
    else { _appAmb.pause(); grSessAmbientSave(); }
    _appAmbIcon();
}
function grSessAmbientSetTrack(tid) {
    if (!tid) return;
    try {
        localStorage.setItem('gr_ambient_track_' + _appAmbKey, tid);
        localStorage.setItem('gr_ambient_last_track', tid);
        localStorage.setItem('gr_ambient_pos_' + _appAmbKey, '0');
    } catch (_) {}
    grSessAmbientLoad(tid, true);
}
async function grSessAmbientStart(reading) {
    _appAmb = document.getElementById('sessAmbientAudio');
    if (!_appAmb) return;
    _appAmb.loop = true;
    _appAmb.onplay = _appAmbIcon;
    _appAmb.onpause = _appAmbIcon;
    _appAmb.ontimeupdate = () => { const n = Date.now(); if (n - _appAmbSaveT > 5000) { _appAmbSaveT = n; grSessAmbientSave(); } };
    _appAmbKey = 'phys_' + ((reading.book && reading.book.id) || reading.id);
    document.getElementById('sessAmbientBtn').onclick = grSessAmbientToggle;
    const sel = document.getElementById('sessAmbientTrack');
    if (sel) sel.onchange = () => grSessAmbientSetTrack(sel.value);
    if (!_appAmbTracks.length) {
        try { _appAmbTracks = ((await (await fetch(_AMB_BASE + '/tracks')).json()).tracks) || []; } catch (_) {}
    }
    if (sel) sel.innerHTML = _appAmbTracks.map(t => `<option value="${t.id}">${t.name}</option>`).join('');
    let tid = localStorage.getItem('gr_ambient_track_' + _appAmbKey) || localStorage.getItem('gr_ambient_last_track');
    if (tid && !_appAmbTracks.some(t => t.id === tid)) tid = null;   // saved track was deleted
    if (!tid && _appAmbTracks.length) tid = _appAmbTracks[0].id;
    grSessAmbientSetEditable(false);   // running → locked
    _appAmbIcon();
    if (!tid) return;
    if (sel) sel.value = tid;
    grSessAmbientLoad(tid, true);      // auto-play
}
function grSessAmbientStop() {
    if (_appAmb) { grSessAmbientSave(); try { _appAmb.pause(); } catch (_) {} }
    _appAmbIcon();
}

// ---- Reading session brightness slider (#40) ------------------------------
// Adjusts screen brightness while in the session. Native window brightness via
// window.Android.setBrightness (0-1, -1 resets); falls back to a dim overlay when
// the native bridge isn't present (desktop / pre-brightness APK). Remembered
// globally; restored to system default when the session ends.
function _sessBrightHasNative() { return !!(window.Android && typeof window.Android.setBrightness === 'function'); }
function grSessApplyBrightness(level) {
    level = Math.max(1, Math.min(100, parseInt(level) || 100));
    try { localStorage.setItem('gr_session_brightness', String(level)); } catch (_) {}
    const dim = document.getElementById('sessDim');
    // Below this knee, the panel is at/near its hardware minimum, so we stack a black
    // overlay to dim much further than the screen can on its own — for "barely lit,
    // save battery" reading. Above the knee, pure hardware brightness, no overlay.
    const KNEE = 30;          // % of the slider where overlay-dimming begins
    const MAX_OVERLAY = 0.92; // darkest the overlay goes (at slider = 1)
    const overlay = level < KNEE ? (1 - level / KNEE) * MAX_OVERLAY : 0;
    if (_sessBrightHasNative()) {
        // Hardware brightness ramps across the whole slider, floored very low so the
        // bottom genuinely bottoms out the panel; overlay then takes it darker still.
        try { window.Android.setBrightness(Math.max(0.01, level / 100)); } catch (_) {}
        if (dim) dim.style.opacity = String(overlay);
    } else if (dim) {
        // No native bridge (desktop / old APK): overlay does all the dimming.
        dim.style.opacity = String(1 - level / 100);
    }
}
function grSessBrightnessStart() {
    const slider = document.getElementById('sessBright');
    const saved = Math.max(1, Math.min(100, parseInt(localStorage.getItem('gr_session_brightness') || '100')));
    if (slider) { slider.value = saved; slider.oninput = function () { grSessApplyBrightness(this.value); }; }
    grSessApplyBrightness(saved);
}
function grSessBrightnessStop() {
    // Restore the system default brightness on leaving the session.
    if (_sessBrightHasNative()) { try { window.Android.setBrightness(-1); } catch (_) {} }
    const dim = document.getElementById('sessDim');
    if (dim) dim.style.opacity = '0';
}

// True if the book has logged time in a format OTHER than `fmt` (so we know to
// qualify the "Time read" label by format instead of leaving it bare).
function hasOther(d, fmt) {
    return ['Ebook', 'Audio', 'Physical'].some(f =>
        f !== fmt && d.formats && d.formats[f] && d.formats[f].minutes > 0);
}

// Minutes → "2h 15m" / "45m" / "30s" for small values.
function formatDuration(min) {
    if (min == null) return '';
    if (min < 1) return `${Math.round(min * 60)}s`;
    const h = Math.floor(min / 60);
    const m = Math.round(min % 60);
    return h > 0 ? `${h}h ${m}m` : `${m}m`;
}

// Export functions for global use
window.GreatReads = {
    showToast,
    formatDate,
    formatRating,
    getStatusBadge,
    getStatusClass,
    apiCall,
    updateReading,
    finishReading,
    finishAndReview,
    pauseReading,
    unpauseReading,
    startReading,
    reorderReadings,
    recalculateChains,
    showEditModal,
    openBookActions: grOpenBookActions,
    openSeries: grOpenSeries,
    openAuthor: grOpenAuthor,
    openAuthorName: grOpenAuthorName,
    openNarrator: grOpenNarrator,
    showContribExtra: grShowContribExtra,
    openGenre: grOpenGenre,
    popupRequestMeta: grPopupRequestMeta,
    openBookById: grOpenBookById,
    openBookNav: grOpenBookNav,
    setNavOptsBuilder: grSetNavOptsBuilder,
    navStep: grNavStep,
    editReadingById: grEditReadingById,
    editSelectRead: grEditSelectRead,
    showReadingSessions: grShowReadingSessions,
    sessionsSelectRead: grSessSelectRead,
    showRatings: grOpenRatings,          // View Ratings → view+edit screen (#127)
    editRatings: grOpenRatings,          // "Not Yet Rated" → same screen, editable
    ratingsSelectRead: grRatingsSelectRead,
    saveRatings: grSaveRatings,
    resetCreditMark: grResetCreditMark,
    recreditSessions: grRecreditSessions,
    updatePhysicalProgress: grUpdatePhysicalProgress,
    startReadingSession: grStartReadingSession,
    formatDateSmart,
    readingExtraInfoHtml,
    saveReadingChanges,
    initializeDragAndDrop,
    initEmojiRating,
    initAllEmojiRatings,
    setEmojiRating,
    updateEmojiDisplay
};

// #205: Check Libby works in place on every page — the full implementation
// (search → match → borrow/hold popup) lives in the shared libby_check.js,
// loaded right after this file for logged-in users.
