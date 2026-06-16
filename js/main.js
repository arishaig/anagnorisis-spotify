/* global socket */

let _unmatchedPage = 1;
let _unmatchedTotal = 0;
let _previewed = false;
const PER_PAGE = 50;

document.addEventListener('DOMContentLoaded', () => {
  const page = document.getElementById('spotify-page');
  if (page) page.style.display = '';

  const fileInput = document.getElementById('spotify-file');
  if (fileInput) {
    fileInput.addEventListener('change', () => {
      const name = fileInput.files[0] ? fileInput.files[0].name : 'No file selected';
      setText('spotify-file-name', name);
    });
  }

  // Background tasks emit their result here when done.
  socket.on('emit_spotify_result', renderResult);

  loadStatus();
});

// ---------------------------------------------------------------------------
// Status
// ---------------------------------------------------------------------------

function loadStatus() {
  socket.emit('emit_spotify_page_get_status', {}, (data) => {
    if (data.has_upload) {
      show('spotify-import-section');
    }
    if (data.unmatched_count > 0) {
      show('spotify-unmatched-section');
      loadUnmatched(1);
    }
  });
}

// ---------------------------------------------------------------------------
// Upload
// ---------------------------------------------------------------------------

window.spotifyUpload = function () {
  const input = document.getElementById('spotify-file');
  const btn = document.getElementById('spotify-upload-btn');
  const statusEl = document.getElementById('spotify-upload-status');
  const file = input && input.files[0];
  if (!file) {
    showStatus(statusEl, 'Choose a .zip first.');
    return;
  }
  if (btn) btn.classList.add('is-loading');
  showStatus(statusEl, 'Uploading…');

  const form = new FormData();
  form.append('file', file);
  fetch('/spotify/upload', { method: 'POST', body: form, credentials: 'same-origin' })
    .then(r => r.json().catch(() => ({})))
    .then(resp => {
      if (btn) btn.classList.remove('is-loading');
      if (resp && resp.ok) {
        showStatus(statusEl, 'Uploaded. Now run a preview below.');
        show('spotify-import-section');
        _previewed = false;
        const commit = document.getElementById('spotify-commit-btn');
        if (commit) commit.disabled = true;
      } else {
        showStatus(statusEl, `Upload failed: ${(resp && resp.error) || 'unknown error'}`);
      }
    })
    .catch(err => {
      if (btn) btn.classList.remove('is-loading');
      showStatus(statusEl, `Upload failed: ${err}`);
    });
};

// ---------------------------------------------------------------------------
// Preview / commit
// ---------------------------------------------------------------------------

window.spotifyRun = function (kind) {
  if (kind === 'commit' && !confirm('Write these ratings into your music library?')) return;

  const statusEl = document.getElementById('spotify-run-status');
  const btn = document.getElementById(kind === 'commit' ? 'spotify-commit-btn' : 'spotify-preview-btn');
  if (btn) btn.classList.add('is-loading');
  showStatus(statusEl, kind === 'commit' ? 'Importing… watch the task manager.' : 'Computing preview… watch the task manager.');

  socket.emit(`emit_spotify_page_${kind}`, {}, (resp) => {
    if (resp && resp.error) {
      if (btn) btn.classList.remove('is-loading');
      showStatus(statusEl, `Error: ${resp.error}`);
    }
    // The actual result arrives asynchronously via emit_spotify_result.
  });
};

function renderResult(summary) {
  ['spotify-preview-btn', 'spotify-commit-btn'].forEach(id => {
    const b = document.getElementById(id); if (b) b.classList.remove('is-loading');
  });

  setText('spotify-stat-total', summary.distinct_tracks ?? 0);
  setText('spotify-stat-matched', summary.matched ?? 0);
  setText('spotify-stat-rated', summary.rated_matched ?? 0);
  setText('spotify-stat-unmatched', summary.unmatched_notable ?? 0);

  const note = document.getElementById('spotify-result-note');
  if (note) {
    note.textContent = summary.dry_run
      ? 'Preview only — nothing was written. Happy with the spread? Click “Import ratings”.'
      : `Done — wrote ${summary.written} ratings to your library.`;
  }

  const tbody = document.getElementById('spotify-dist-tbody');
  if (tbody) {
    const dist = summary.distribution || {};
    const max = Math.max(1, ...Object.values(dist));
    tbody.innerHTML = Object.keys(dist)
      .sort((a, b) => Number(b) - Number(a))
      .map(r => {
        const n = dist[r];
        const w = Math.round((n / max) * 100);
        return `<tr>
          <td><strong>${esc(r)}/10</strong></td>
          <td>${n}</td>
          <td style="width:50%"><progress class="progress is-info" value="${w}" max="100">${w}%</progress></td>
        </tr>`;
      }).join('');
  }

  show('spotify-result');

  const statusEl = document.getElementById('spotify-run-status');
  if (summary.dry_run) {
    _previewed = true;
    const commit = document.getElementById('spotify-commit-btn');
    if (commit) commit.disabled = false;
    showStatus(statusEl, 'Preview ready.');
  } else {
    showStatus(statusEl, `Import complete — wrote ${summary.written} ratings.`);
    loadStatus();
  }
}

// ---------------------------------------------------------------------------
// Unmatched tracks
// ---------------------------------------------------------------------------

function loadUnmatched(page) {
  _unmatchedPage = page;
  socket.emit('emit_spotify_page_get_unmatched', { page, per_page: PER_PAGE }, (data) => {
    _unmatchedTotal = data.total;
    renderUnmatched(data.items, data.total, page);
  });
}

function renderUnmatched(items, total, page) {
  const tbody = document.getElementById('spotify-unmatched-tbody');
  const badge = document.getElementById('spotify-unmatched-badge');
  const info = document.getElementById('spotify-unmatched-info');
  const prev = document.getElementById('spotify-unmatched-prev');
  const next = document.getElementById('spotify-unmatched-next');

  if (badge) badge.textContent = total;
  if (total > 0) show('spotify-unmatched-section');
  if (tbody) {
    tbody.innerHTML = items.map(m => `
      <tr id="unmatched-row-${m.id}">
        <td>${m.rating != null ? esc(m.rating) + '/10' : ''}</td>
        <td>${esc(m.artist)}</td>
        <td>${esc(m.title)}</td>
        <td>${esc(m.album)}</td>
        <td><button class="button is-small is-light" onclick="dismissUnmatched(${m.id})">Dismiss</button></td>
      </tr>
    `).join('');
  }

  const totalPages = Math.ceil(total / PER_PAGE);
  if (info) info.textContent = `Page ${page} of ${Math.max(totalPages, 1)} (${total} tracks)`;
  if (prev) prev.disabled = page <= 1;
  if (next) next.disabled = page >= totalPages;
}

window.spotifyUnmatchedPage = function (delta) {
  const newPage = _unmatchedPage + delta;
  const totalPages = Math.ceil(_unmatchedTotal / PER_PAGE);
  if (newPage >= 1 && newPage <= totalPages) loadUnmatched(newPage);
};

window.dismissUnmatched = function (id) {
  socket.emit('emit_spotify_page_dismiss_unmatched', { id }, () => {
    const row = document.getElementById(`unmatched-row-${id}`);
    if (row) row.remove();
    _unmatchedTotal = Math.max(0, _unmatchedTotal - 1);
    const badge = document.getElementById('spotify-unmatched-badge');
    if (badge) badge.textContent = _unmatchedTotal;
    if (_unmatchedTotal === 0) hide('spotify-unmatched-section');
  });
};

// ---------------------------------------------------------------------------
// Utilities
// ---------------------------------------------------------------------------

function show(id) { const el = document.getElementById(id); if (el) el.style.display = ''; }
function hide(id) { const el = document.getElementById(id); if (el) el.style.display = 'none'; }
function setText(id, val) { const el = document.getElementById(id); if (el) el.textContent = val; }
function showStatus(el, msg) { if (el) { el.style.display = ''; el.textContent = msg; } }
function esc(s) {
  return String(s ?? '').replace(/&/g, '&amp;').replace(/</g, '&lt;').replace(/>/g, '&gt;');
}
