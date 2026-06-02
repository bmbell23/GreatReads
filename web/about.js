// About page — mostly static documentation. The render function is
// invoked with backend meta (version + build stamp) so the header card
// shows live values without round-tripping through localStorage.

function esc(s) {
    return String(s).replace(/[&<>"']/g, c => ({
        '&':'&amp;','<':'&lt;','>':'&gt;','"':'&quot;',"'":'&#39;'
    }[c]));
}

function readingMinMax() {
    const mn = parseInt(localStorage.getItem('ereader.settings.readingMinSec'), 10);
    const mx = parseInt(localStorage.getItem('ereader.settings.readingMaxSec'), 10);
    return {
        min: (Number.isFinite(mn) && mn > 0) ? mn : 10,
        max: (Number.isFinite(mx) && mx > 0) ? mx : 120,
    };
}

function render(meta) {
    const c = document.getElementById('container');
    const mm = readingMinMax();
    c.innerHTML = `
        <h2>GreatReads</h2>
        <div class="card">
            <div class="meta-row"><span class="k">App version</span><span class="v">${esc(meta.version)}</span></div>
            <div class="meta-row"><span class="k">Web build</span><span class="v">${esc(meta.buildStamp)}</span></div>
            <div class="meta-row"><span class="k">Backend</span><span class="v">100.69.184.113:8091</span></div>
        </div>
        <p>A minimal Calibre-backed EPUB / PDF reader. Source of truth for
        the book library is a Calibre Content Server; this app is a
        read-only client that adds highlights, bookmarks, progress sync,
        and a reading-speed estimator.</p>

        <h2>Reading Speed</h2>
        <p>The metrics row at the bottom of the EPUB reader (WPM, average
        seconds per page, chapter / book time remaining) is driven by a
        rolling buffer of recent page-turn samples. Here's exactly what's
        in that buffer and how it's filtered.</p>

        <h3>What gets measured</h3>
        <p>Each time you turn a page in an EPUB, the reader records a
        sample of the form:</p>
        <div class="formula">{ ms: &lt;time spent on the page&gt;,
  words: &lt;words-per-page at that moment&gt; }</div>
        <p><code>words</code> is derived from Calibre's
        <code>#word_count</code> custom column divided by the current
        paginated page count. If a book has no word count, that field is
        <code>null</code> and the reader falls back to averaging raw
        per-page time.</p>
        <p>The per-page timer pauses when the WebView is hidden (screen
        lock, app switch, force-stop), so background time isn't charged
        to the current page. Font-size changes, dual-page toggles, and
        foldable posture changes also discard the in-flight timer.</p>

        <h3>Two-stage outlier filter</h3>
        <p>Samples go through two independent rejection passes before
        they influence the displayed metrics.</p>

        <p><strong>Stage 1 — Absolute window (insert-time):</strong> any
        sample with <code>ms</code> outside
        <code>[${mm.min}s, ${mm.max}s]</code> is dropped at the moment
        of the page turn and never enters the buffer. This catches
        accidental swipes and phone-down time — the two failure modes
        that would otherwise dominate the median and ruin Stage 2. Both
        thresholds are configurable in Settings → Reading.</p>

        <p><strong>Stage 2 — MAD-based outlier rejection
        (compute-time):</strong> at every metrics tick, samples in the
        buffer are filtered against the buffer's own distribution using
        a robust modified-z-score test:</p>
        <div class="formula">M = 0.6745 · (x − median) / MAD
where MAD = median(|xᵢ − median|)

Reject the sample if  |M| &gt; 3.5</div>
        <p>This is the Iglewicz &amp; Hoaglin standard test. The 0.6745
        factor scales MAD so it's comparable to the standard deviation
        on normally-distributed data; the 3.5 threshold corresponds to
        roughly p &lt; 0.001. On a buffer of 30 typical samples that
        means essentially zero false rejections during steady-state
        reading, but it reliably catches things like:</p>
        <ul>
            <li>The 8-line epigraph page you flipped past in 12 seconds
            in the middle of a book where your typical page takes 60.</li>
            <li>The half-empty chapter-end page that took 18 seconds
            instead of the usual 55.</li>
            <li>A 110-second page where you actually paused to think,
            in a book where you typically read at 45 seconds per page.</li>
        </ul>
        <p>Why MAD instead of mean / standard deviation? Standard
        deviation is itself inflated by the outliers you're trying to
        detect — one bad sample can mask the next. The median and MAD
        are robust: a single anomalous value barely shifts either,
        which is exactly what we want for the cluttered, real-world
        distribution of page-turn times.</p>
        <p>The stage-2 filter is a pass-through until the buffer holds
        at least 5 samples — below that, the median and MAD aren't
        meaningful. If every sample happens to be identical (MAD = 0),
        the filter falls back to a relative-spread check
        (±50% of the median) so a uniform buffer doesn't accidentally
        whitelist an obvious outlier.</p>

        <h3>Why ms-per-word, not raw ms</h3>
        <p>When a word count is available, Stage 2 filters on
        <code>ms / words</code>, not on <code>ms</code> directly. This
        matters because:</p>
        <ul>
            <li>Toggling dual-page mode roughly doubles
            words-per-page. A "fast" page in dual mode would look
            anomalously fast against a buffer of single-page samples
            even though your underlying reading speed is unchanged.</li>
            <li>Adjusting font size changes words-per-page
            continuously. Filtering on ms-per-word means the buffer
            survives the change instead of dropping every sample for
            the next 30 page turns.</li>
        </ul>
        <p>The displayed average then projects ms-per-word back to
        ms-per-page using the <em>current</em> words-per-page, so the
        countdown reflects the layout you're actually reading in right
        now.</p>

        <h3>Buffer mechanics</h3>
        <ul>
            <li>FIFO ring of the last 30 samples (~15–30 minutes of
            reading history).</li>
            <li>Big enough that one slow page can't yank the average
            around; small enough to track within-session changes
            (fatigue, denser prose).</li>
            <li>Persisted in <code>progress.json</code> per book so a
            fresh install or device swap resumes your calibrated
            reading speed immediately.</li>
            <li>EPUB only — PDFs don't populate this.</li>
        </ul>

        <h3>Derived metrics</h3>
        <div class="formula">WPM           = 60000 / msPerWord
avg sec/page  = msPerWord × words-per-page  (or raw mean ms)
book remain   = (totalPages − pageIndex) × avg
chapter remain = (chapterEnd − pageIndex) × avg</div>
        <p><code>pageIndex</code> is 0-based, so both formulas count the
        page you are currently reading plus every page that follows —
        meaning the last page of a chapter still shows its full average
        reading time rather than 0:00.</p>
        <p>The chapter / book countdowns are recomputed once per page
        turn (using the freshly-updated average) and then tick down in
        real time off the per-page timer, so the seconds you actually
        see decrement match the time you've actually spent on the
        current page.</p>
    `;
}
