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
        
        if (error.response) {
            // Server responded with error status
            const message = error.response.data?.detail || error.response.data?.message || 'Server error';
            showToast(message, 'danger');
        } else if (error.request) {
            // Request made but no response
            showToast('Network error - please check your connection', 'danger');
        } else {
            // Something else happened
            showToast('An unexpected error occurred', 'danger');
        }
        
        throw error;
    }
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

// Finish a reading, then open the full Edit Reading form so the user can add
// ratings/review (and adjust dates) and Save (#108). The finish endpoint handles
// the finish date + chain logic; we then re-fetch the now-finished reading (with
// its book) and open the shared edit modal. `reload` refreshes the calling page's
// list. Pages without the edit modal (Library) fall back to a plain finish.
async function finishAndReview(readingId, reload) {
    try {
        await finishReading(readingId);
        if (typeof reload === 'function') await reload();
        const modal = document.getElementById('editReadingModal');
        if (!modal) { showToast('Reading marked as finished!', 'success'); return; }
        const reading = await apiCall(`/readings/${readingId}`);
        showToast('Finished — add your ratings and Save.', 'success');
        showEditModal(reading);
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
function grOpenBookActions(book, opts = {}) {
    if (!book) return;
    const canRead = !!book.calibre_id;
    const canListen = !!book.abs_id;
    // encodeURIComponent leaves ' unescaped, which would break the inline onclick
    // string for titles with apostrophes — escape it explicitly.
    const titleEnc = encodeURIComponent(book.title || '').replace(/'/g, '%27');
    const authorEnc = encodeURIComponent(book.author || '').replace(/'/g, '%27');

    document.getElementById('openBookTitle').textContent = opts.title || book.title || '';

    // Detail rows: author / series / words / pages, plus any caller extras (WPD…).
    const rows = [];
    if (book.author) rows.push(['Author', book.author]);
    if (book.series) {
        const num = (book.series_number != null) ? ' #' + book.series_number : '';
        rows.push(['Series', `${book.universe ? book.universe + ': ' : ''}${book.series}${num}`]);
    }
    if (book.word_count) rows.push(['Words', Number(book.word_count).toLocaleString()]);
    if (book.page_count) rows.push(['Pages', Number(book.page_count).toLocaleString()]);
    if (Array.isArray(opts.detailRows)) rows.push(...opts.detailRows.filter(Boolean));

    // Logging progress / a physical session belongs to the READING, not inventory:
    // any in-progress reading qualifies — a physical library book you don't own, or
    // one in progress as ebook/audio you also read physically (#41). The tap entry
    // is the progress display below; pages convert to %, so shared progress stays
    // consistent and physical time logs separately.
    const r = opts.reading;
    const ipReading = (r && r.is_started && !r.is_finished && r.status !== 'paused') ? r : null;
    grActivePhysicalReading = ipReading;

    // Physical card — shown when a physical copy is owned (shelf location). Also a
    // tap-to-log shortcut when in progress (same as tapping the progress below).
    // (The progress display is the universal entry incl. non-owned library books.)
    let locationCard = '';
    const phys = (book.inventory || []).find(i => i.owned_physical);
    if (phys) {
        let loc = phys.location || '';
        if (phys.shelf_bookshelf) {
            loc = `Shelf ${phys.shelf_bookshelf}` +
                  (phys.shelf_shelf != null ? `-${phys.shelf_shelf}` : '') +
                  (phys.shelf_position != null ? `, pos ${phys.shelf_position}` : '');
        }
        const locLine = loc ? `<div class="small">${loc}</div>` : '';
        let progLine = '', clickAttrs = '';
        if (ipReading) {
            const pct = ipReading.current_progress_percent || 0;
            const pg = ipReading.current_progress_page || 0;
            const prog = pct > 0 ? `${pct.toFixed(1)}%${pg ? ' · p. ' + pg : ''}` : 'Tap to log progress';
            progLine = `<div class="small">${prog}</div>`;
            clickAttrs = ' onclick="GreatReads.updatePhysicalProgress()" style="cursor:pointer"';
        }
        locationCard = `
            <div class="col-12">
                <div class="open-type-btn open-type-physical open-type-static"${clickAttrs}>
                    <i class="fas fa-book fa-lg me-2"></i>
                    <div class="text-start">
                        <div class="fw-bold">Physical</div>
                        ${locLine}
                        ${progLine}
                    </div>
                </div>
            </div>`;
    }

    const details = rows.length ? `
        <div class="col-12">
            <div class="open-details">
                ${rows.map(([k, v]) => `<div class="d-flex justify-content-between gap-3">
                    <span class="text-muted">${k}</span><span class="fw-medium text-end">${v}</span>
                </div>`).join('')}
            </div>
        </div>` : '';

    const big = (canRead || canListen) ? `
        <div class="col-12">
            <div class="row g-2">
                ${canRead ? `<div class="col">
                    <button type="button" class="open-type-btn open-type-ebook"
                            onclick="grOpenEbook('${book.calibre_id}', '${titleEnc}')">
                        <i class="fas fa-book-open fa-2x mb-2"></i><div class="fw-bold">Read</div>
                    </button>
                </div>` : ''}
                ${canListen ? `<div class="col">
                    <button type="button" class="open-type-btn open-type-audio"
                            onclick="grOpenAudio('${book.abs_id}', '${titleEnc}', '${authorEnc}', '${book.calibre_id || ''}')">
                        <i class="fas fa-headphones fa-2x mb-2"></i><div class="fw-bold">Listen</div>
                    </button>
                </div>` : ''}
            </div>
        </div>` : `
        <div class="col-12 text-center text-muted small pb-1">
            <i class="fas fa-book me-1"></i>Tracked book — no ebook or audiobook file linked.
        </div>`;

    // The progress display is the tap-to-log entry for any in-progress reading
    // (update %/page, start a physical session) — independent of inventory. (#41)
    const extraInfo = opts.extraInfoHtml
        ? `<div class="col-12"${ipReading ? ' onclick="GreatReads.updatePhysicalProgress()" style="cursor:pointer"' : ''}>${opts.extraInfoHtml}</div>`
        : '';

    // Highlights link — shown on any page when the book is a linked ebook. Hidden
    // until the async count below confirms there are some. (Library/TBR/Journal all
    // get this for free.) Opt out with opts.highlights === false.
    const showHl = opts.highlights !== false && !!book.calibre_id;
    const hlLink = showHl ? `
                <a id="hlActionBtn" class="btn btn-sm btn-outline-secondary d-none"
                   href="/greatreads/highlights?book=${book.calibre_id}&title=${titleEnc}">
                    <i class="fas fa-highlighter me-2" style="color:#e0a800;"></i>Highlights
                    <span id="hlCount" class="badge bg-secondary ms-auto">0</span>
                </a>` : '';

    // Edit Book — jump to the book's edit view (page count, etc.). Shown on every
    // page via the deep-link library?editBook=<id> (#27). Opt out with
    // opts.editBook === false.
    const editBookLink = (opts.editBook !== false && book.id != null) ? `
                <a class="btn btn-sm btn-outline-secondary"
                   href="${BASE_PATH}/library?editBook=${book.id}">
                    <i class="fas fa-pen-to-square me-2 text-primary"></i>Edit Book
                </a>` : '';

    // "See Reading Sessions" — read-only session history (#77). Hidden until the
    // async summary below confirms there are qualified sessions; shown on every
    // caller (Home in-progress, Journal finished) via book.id. Opt out with
    // opts.sessions === false.
    const showSessions = opts.sessions !== false && book.id != null;
    const sessionsBtn = showSessions ? `
                <button type="button" id="seeSessionsBtn" class="btn btn-sm btn-outline-secondary"
                        onclick="GreatReads.showReadingSessions(${book.id}, '${titleEnc}')">
                    <i class="fas fa-clock-rotate-left me-2 text-info"></i>See Reading Sessions
                </button>` : '';

    // "Reset word-credit mark" (#86): unsticks the #79 high-water-mark when it got
    // poisoned (e.g. a broken EPUB reported a spurious-high spot) so forward reading
    // credits words again. Hidden until the async progress fetch below confirms the
    // mark is actually ahead of the current position.
    const progKey = book.calibre_id ? String(book.calibre_id) : (book.abs_id ? 'abs:' + book.abs_id : '');
    const resetCreditBtn = progKey ? `
                <button type="button" id="resetCreditBtn" class="btn btn-sm btn-outline-secondary d-none"
                        onclick="GreatReads.resetCreditMark('${progKey.replace(/'/g, '%27')}')">
                    <i class="fas fa-rotate-left me-2 text-warning"></i>Reset word-credit mark
                </button>` : '';

    const secondaryInner = `${hlLink}${opts.actionsHtml || ''}${sessionsBtn}${resetCreditBtn}${editBookLink}`;
    const secondary = secondaryInner.trim() ? `
        <div class="col-12">
            <div class="open-secondary">${secondaryInner}</div>
        </div>` : '';

    document.getElementById('openBookOptions').innerHTML =
        locationCard + big + extraInfo + details + secondary;
    bootstrap.Modal.getOrCreateInstance(document.getElementById('openBookModal')).show();

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

    // Reading-session stats (#77): count + average sitting length, and reveal the
    // "See Reading Sessions" button. One summary call serves both; only shows once
    // there are qualified sessions (books read before session logging show nothing).
    if (showSessions) {
        // Always show the two rows (even at 0 / —) so they're visible before any
        // session is logged; the button is always present too.
        const renderSessionStats = (sessions, minutes) => {
            const det = document.querySelector('#openBookOptions .open-details');
            if (!det) return;
            const avg = sessions > 0 ? formatDuration(minutes / sessions) : '—';
            det.insertAdjacentHTML('beforeend',
                `<div class="d-flex justify-content-between gap-3">
                    <span class="text-muted">Reading Sessions</span>
                    <span class="fw-medium text-end">${sessions}</span>
                </div>
                <div class="d-flex justify-content-between gap-3">
                    <span class="text-muted">Average Session Time</span>
                    <span class="fw-medium text-end">${avg}</span>
                </div>`);
        };
        fetch(`${GR_EREADER_API}/sessions-summary-gr/${book.id}`)
            .then(r => r.ok ? r.json() : null)
            .then(d => renderSessionStats((d && d.sessions) || 0, (d && d.minutes) || 0))
            .catch(() => renderSessionStats(0, 0));
    }

    // Reading position + word-credit mark (#86): show the current position, and if
    // the #79 high-water-mark is ahead of it (so words aren't crediting), surface it
    // and reveal the reset button.
    if (progKey) {
        fetch(`${GR_EREADER_API}/progress/${encodeURIComponent(progKey)}`)
            .then(r => r.ok ? r.json() : null)
            .then(p => {
                if (!p || typeof p.progress !== 'number') return;
                const det = document.querySelector('#openBookOptions .open-details');
                const pct = Math.round(p.progress * 100);
                const hwm = (typeof p.maxProgress === 'number') ? p.maxProgress : null;
                const stuck = hwm != null && hwm > p.progress + 0.005;
                if (det) {
                    let rows = `<div class="d-flex justify-content-between gap-3">
                        <span class="text-muted">Reading position</span>
                        <span class="fw-medium text-end">${pct}%${p.page ? ` · p. ${p.page}` : ''}</span></div>`;
                    if (stuck) rows += `<div class="d-flex justify-content-between gap-3">
                        <span class="text-muted">Word credit resumes past</span>
                        <span class="fw-medium text-end" style="color:#dc3545;">${Math.round(hwm * 100)}%</span></div>`;
                    det.insertAdjacentHTML('beforeend', rows);
                }
                if (stuck) { const b = document.getElementById('resetCreditBtn'); if (b) b.classList.remove('d-none'); }
            })
            .catch(() => {});
    }

    if (typeof opts.onShow === 'function') opts.onShow(book);
}

// "See Reading Sessions" (#77): read-only list of every qualified sitting for a
// book. Stacks above the cover popup. Columns: Date & Time (YYMMDD - HH:MM-HH:MM),
// Duration, Words, WPM — newest first.
function grShowReadingSessions(bookId, titleEnc) {
    const modalEl = document.getElementById('readingSessionsModal');
    if (!modalEl) return;
    const title = decodeURIComponent(titleEnc || '');
    document.getElementById('rsModalTitle').textContent =
        title ? `Reading Sessions — ${title}` : 'Reading Sessions';
    const body = document.getElementById('rsModalBody');
    body.innerHTML = '<div class="text-center text-muted py-3"><i class="fas fa-spinner fa-spin"></i></div>';
    bootstrap.Modal.getOrCreateInstance(modalEl).show();

    // YYMMDD - HH:MM-HH:MM from epoch-ms start/end (local time, matching activity_date).
    const fmtDT = (start, end) => {
        const s = new Date(start), e = new Date(end), p = n => String(n).padStart(2, '0');
        const ymd = p(s.getFullYear() % 100) + p(s.getMonth() + 1) + p(s.getDate());
        const hm = d => p(d.getHours()) + ':' + p(d.getMinutes());
        return `${ymd} - ${hm(s)}-${hm(e)}`;
    };

    fetch(`${GR_EREADER_API}/sessions-list-gr/${bookId}`)
        .then(r => r.ok ? r.json() : null)
        .then(d => {
            const sessions = (d && d.sessions) || [];
            if (!sessions.length) {
                body.innerHTML = '<p class="text-muted mb-0 text-center py-3">No reading sessions recorded.</p>';
                return;
            }
            const rowsHtml = sessions.map(ss => `
                <tr>
                    <td class="text-nowrap">${fmtDT(ss.startedAt, ss.endedAt)}</td>
                    <td class="text-end">${formatDuration(ss.minutes)}</td>
                    <td class="text-end">${Number(ss.words || 0).toLocaleString()}</td>
                    <td class="text-end">${ss.wpm != null ? ss.wpm : '—'}</td>
                </tr>`).join('');
            body.innerHTML = `
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
                </div>`;
        })
        .catch(() => {
            body.innerHTML = '<p class="text-danger mb-0 text-center py-3">Error loading sessions.</p>';
        });
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
        const params = { current_percent: pct };
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
    showReadingSessions: grShowReadingSessions,
    resetCreditMark: grResetCreditMark,
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
