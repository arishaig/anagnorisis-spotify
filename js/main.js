/* global socket */

let _unmatchedPage = 1;
let _unmatchedTotal = 0;
const PER_PAGE = 50;

// ---------------------------------------------------------------------------
// Init
// ---------------------------------------------------------------------------

document.addEventListener('DOMContentLoaded', () => {
  // Show the page tab if Anagnorisis injected it
  const page = document.getElementById('spotify-page');
  if (page) page.style.display = '';

  // Check for redirect param after OAuth
  if (new URLSearchParams(window.location.search).get('spotify_connected') === '1') {
    window.history.replaceState({}, '', window.location.pathname);
  }

  loadSettings();
  loadStatus();
});

// ---------------------------------------------------------------------------
// App credentials
// ---------------------------------------------------------------------------

function loadSettings() {
  socket.emit('emit_spotify_page_get_settings', {}, (data) => {
    setValue('spotify-client-id', data.client_id || '');
    // Prefill redirect URI with a sensible default derived from the current host.
    const redirect = data.redirect_uri || `${window.location.origin}/spotify/callback`;
    setValue('spotify-redirect-uri', redirect);

    const secretInput = document.getElementById('spotify-client-secret');
    const hint = document.getElementById('spotify-secret-hint');
    if (data.client_secret_set) {
      if (secretInput) secretInput.placeholder = '••••••••  (saved — leave blank to keep)';
      if (hint) hint.style.display = '';
    } else {
      if (hint) hint.style.display = 'none';
    }
  });
}

window.spotifySaveSettings = function () {
  const btn = document.getElementById('spotify-save-settings');
  const statusEl = document.getElementById('spotify-settings-status');
  if (btn) btn.disabled = true;
  if (statusEl) statusEl.textContent = 'Saving...';

  const payload = {
    client_id: getValue('spotify-client-id'),
    client_secret: getValue('spotify-client-secret'),
    redirect_uri: getValue('spotify-redirect-uri'),
  };

  socket.emit('emit_spotify_page_save_settings', payload, (resp) => {
    if (btn) btn.disabled = false;
    if (resp && resp.ok) {
      if (statusEl) statusEl.textContent = 'Saved.';
      setValue('spotify-client-secret', ''); // never keep the secret in the field
      loadSettings();
      loadStatus();
    } else {
      if (statusEl) statusEl.textContent = 'Save failed.';
    }
  });
};

// ---------------------------------------------------------------------------
// Status
// ---------------------------------------------------------------------------

function loadStatus() {
  socket.emit('emit_spotify_page_get_status', {}, (data) => {
    const settings = document.getElementById('spotify-settings');

    if (!data.configured) {
      if (settings) settings.open = true; // expand the credentials form
      hide('spotify-configured');
      return;
    }

    if (settings) settings.open = false; // collapse once configured
    show('spotify-configured');

    if (data.connected) {
      setText('spotify-connection-status', 'Connected');
      hide('spotify-connect-btn');
      show('spotify-disconnect-btn');
      show('spotify-sync-section');
      renderSyncStats(data);
      if (data.unmatched_count > 0) {
        loadUnmatched(1);
      }
    } else {
      setText('spotify-connection-status', 'Not connected');
      show('spotify-connect-btn');
      hide('spotify-disconnect-btn');
      hide('spotify-sync-section');
    }
  });
}

function renderSyncStats(data) {
  if (data.last_synced) {
    const d = new Date(data.last_synced);
    setText('spotify-last-synced', d.toLocaleString());
  } else {
    setText('spotify-last-synced', 'Never');
  }
  setText('spotify-liked-count', data.liked_count ?? 0);
  setText('spotify-matched-count', data.matched_count ?? 0);
  setText('spotify-unmatched-count', data.unmatched_count ?? 0);

  const unmatchedSection = document.getElementById('spotify-unmatched-section');
  if (unmatchedSection) {
    unmatchedSection.style.display = (data.unmatched_count > 0) ? '' : 'none';
  }
}

// ---------------------------------------------------------------------------
// Sync
// ---------------------------------------------------------------------------

window.spotifySync = function () {
  const btn = document.getElementById('spotify-sync-btn');
  const statusEl = document.getElementById('spotify-sync-status');
  if (btn) btn.disabled = true;
  if (statusEl) { statusEl.style.display = ''; statusEl.textContent = 'Starting sync...'; }

  socket.emit('emit_spotify_page_sync', {}, (resp) => {
    if (resp && resp.error) {
      if (statusEl) statusEl.textContent = `Error: ${resp.error}`;
      if (btn) btn.disabled = false;
      return;
    }
    if (statusEl) statusEl.textContent = 'Sync queued. Check the task manager for progress.';
    // Re-enable and refresh after a delay
    setTimeout(() => {
      if (btn) btn.disabled = false;
      loadStatus();
    }, 3000);
  });
};

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
  if (tbody) {
    tbody.innerHTML = items.map(m => `
      <tr id="unmatched-row-${m.id}">
        <td>${esc(m.artist)}</td>
        <td>${esc(m.title)}</td>
        <td>${esc(m.album)}</td>
        <td>
          <button class="button is-small is-light" onclick="dismissUnmatched(${m.id})">
            Dismiss
          </button>
        </td>
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
  if (newPage >= 1 && newPage <= totalPages) {
    loadUnmatched(newPage);
  }
};

window.dismissUnmatched = function (id) {
  socket.emit('emit_spotify_page_dismiss_unmatched', { id }, () => {
    const row = document.getElementById(`unmatched-row-${id}`);
    if (row) row.remove();
    _unmatchedTotal = Math.max(0, _unmatchedTotal - 1);
    const badge = document.getElementById('spotify-unmatched-badge');
    if (badge) badge.textContent = _unmatchedTotal;
    const info = document.getElementById('spotify-unmatched-info');
    if (info) {
      const totalPages = Math.ceil(_unmatchedTotal / PER_PAGE);
      info.textContent = `Page ${_unmatchedPage} of ${Math.max(totalPages, 1)} (${_unmatchedTotal} tracks)`;
    }
    if (_unmatchedTotal === 0) {
      hide('spotify-unmatched-section');
    }
  });
};

// ---------------------------------------------------------------------------
// Utilities
// ---------------------------------------------------------------------------

function show(id) { const el = document.getElementById(id); if (el) el.style.display = ''; }
function hide(id) { const el = document.getElementById(id); if (el) el.style.display = 'none'; }
function setText(id, val) { const el = document.getElementById(id); if (el) el.textContent = val; }
function getValue(id) { const el = document.getElementById(id); return el ? el.value : ''; }
function setValue(id, val) { const el = document.getElementById(id); if (el) el.value = val; }
function esc(s) {
  return String(s ?? '').replace(/&/g, '&amp;').replace(/</g, '&lt;').replace(/>/g, '&gt;');
}
