"""
serve.py — Spotify import module for Anagnorisis.

Registers:
  GET  /spotify/auth       — redirect to Spotify OAuth consent screen
  GET  /spotify/callback   — handle OAuth callback, store tokens
  GET  /spotify/disconnect — revoke local token

SocketIO events (incoming):
  emit_spotify_page_get_status       — connection + last-sync stats
  emit_spotify_page_sync             — queue a background sync task
  emit_spotify_page_get_unmatched    — paginated list of unmatched tracks
  emit_spotify_page_dismiss_unmatched — hide a track from the unmatched list
"""
import datetime
import secrets

import spotipy
from flask import redirect, request
from omegaconf import OmegaConf
from spotipy.cache_handler import CacheHandler
from spotipy.oauth2 import SpotifyOAuth

from src.db_models import db
from src.socket_events import CommonSocketEvents
import modules.spotify_import.db_models as spotify_db
from modules.spotify_import.sync import SCOPES, run_sync


# ---------------------------------------------------------------------------
# DB-backed token cache for spotipy
# ---------------------------------------------------------------------------

class _DBCacheHandler(CacheHandler):
    def __init__(self, app):
        self._app = app

    def get_cached_token(self):
        with self._app.app_context():
            row = spotify_db.SpotifyToken.query.first()
            if not row or not row.access_token:
                return None
            return {
                'access_token': row.access_token,
                'refresh_token': row.refresh_token,
                'expires_at': row.token_expiry.timestamp() if row.token_expiry else 0,
                'scope': row.scope or '',
                'token_type': 'Bearer',
            }

    def save_token_to_cache(self, token_info):
        with self._app.app_context():
            row = spotify_db.SpotifyToken.query.first()
            if not row:
                row = spotify_db.SpotifyToken()
                db.session.add(row)
            row.access_token = token_info['access_token']
            if token_info.get('refresh_token'):
                row.refresh_token = token_info['refresh_token']
            expires_at = token_info.get('expires_at')
            row.token_expiry = datetime.datetime.fromtimestamp(expires_at) if expires_at else None
            row.scope = token_info.get('scope', '')
            row.updated_at = datetime.datetime.utcnow()
            db.session.commit()


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------

def _config_cfg(cfg):
    """Credentials declared in config.yaml (the fallback source)."""
    return OmegaConf.select(cfg, 'spotify_import', default={}) or {}


def _resolve_credentials(cfg) -> dict:
    """
    Resolve Spotify app credentials. UI-saved settings (DB) take precedence;
    config.yaml is the fallback. Must be called within an app context.
    """
    defaults = _config_cfg(cfg)
    row = spotify_db.SpotifySettings.query.first()

    def pick(field):
        val = getattr(row, field, None) if row else None
        return val if val else defaults.get(field)

    return {
        'client_id': pick('client_id'),
        'client_secret': pick('client_secret'),
        'redirect_uri': pick('redirect_uri'),
    }


def _is_configured(cfg) -> bool:
    s = _resolve_credentials(cfg)
    return bool(s.get('client_id') and s.get('client_secret') and s.get('redirect_uri'))


def _make_oauth(cfg, cache_handler) -> SpotifyOAuth:
    s = _resolve_credentials(cfg)
    return SpotifyOAuth(
        client_id=s.get('client_id'),
        client_secret=s.get('client_secret'),
        redirect_uri=s.get('redirect_uri'),
        scope=SCOPES,
        cache_handler=cache_handler,
        show_dialog=False,
    )


def _get_sync_state():
    return spotify_db.SpotifySyncState.query.first()


# ---------------------------------------------------------------------------
# Module entry point
# ---------------------------------------------------------------------------

def init_socket_events(socketio, app=None, cfg=None, data_folder='./project_data'):
    common_socket_events = CommonSocketEvents(socketio, module_name="spotify")
    common_socket_events.show_loading_status('Initializing Spotify module...')

    cache_handler = _DBCacheHandler(app)

    # --- OAuth routes ---------------------------------------------------

    @app.route('/spotify/auth')
    def spotify_auth():
        if not _is_configured(cfg):
            return (
                'Spotify is not configured. '
                'Set spotify_import.client_id, client_secret, and redirect_uri in config.yaml.',
                400,
            )
        state = secrets.token_urlsafe(16)
        with app.app_context():
            sync_state = _get_sync_state() or spotify_db.SpotifySyncState()
            sync_state.oauth_state = state
            db.session.add(sync_state)
            db.session.commit()
        oauth = _make_oauth(cfg, cache_handler)
        return redirect(oauth.get_authorize_url(state=state))

    @app.route('/spotify/callback')
    def spotify_callback():
        error = request.args.get('error')
        if error:
            return f'Spotify authorization failed: {error}', 400

        code = request.args.get('code')
        state = request.args.get('state')

        with app.app_context():
            sync_state = _get_sync_state()
            expected = sync_state.oauth_state if sync_state else None
            if not expected or state != expected:
                return 'State mismatch — possible CSRF. Please try again.', 400
            sync_state.oauth_state = None
            db.session.commit()

        oauth = _make_oauth(cfg, cache_handler)
        oauth.get_access_token(code, as_dict=False)  # saves via cache_handler
        return redirect('/?spotify_connected=1')

    @app.route('/spotify/disconnect')
    def spotify_disconnect():
        with app.app_context():
            spotify_db.SpotifyToken.query.delete()
            db.session.commit()
        return redirect('/')

    # --- SocketIO handlers ----------------------------------------------

    @socketio.on('emit_spotify_page_get_settings')
    def get_settings():
        """Return current credentials for the settings form. The secret is never
        sent back — only whether one is stored."""
        creds = _resolve_credentials(cfg)
        config_creds = _config_cfg(cfg)
        return {
            'client_id': creds.get('client_id') or '',
            'redirect_uri': creds.get('redirect_uri') or '',
            'client_secret_set': bool(creds.get('client_secret')),
            # Surface which fields come from config.yaml so the UI can hint that
            # they're managed there (DB still overrides if the user saves a value).
            'from_config': {
                'client_id': bool(config_creds.get('client_id')),
                'client_secret': bool(config_creds.get('client_secret')),
                'redirect_uri': bool(config_creds.get('redirect_uri')),
            },
        }

    @socketio.on('emit_spotify_page_save_settings')
    def save_settings(data):
        row = spotify_db.SpotifySettings.query.first()
        if not row:
            row = spotify_db.SpotifySettings()
            db.session.add(row)
        row.client_id = (data.get('client_id') or '').strip() or None
        row.redirect_uri = (data.get('redirect_uri') or '').strip() or None
        # Only overwrite the secret when a new value is supplied; a blank field
        # leaves the stored secret untouched. An explicit clear wipes it.
        secret = (data.get('client_secret') or '').strip()
        if secret:
            row.client_secret = secret
        elif data.get('clear_secret'):
            row.client_secret = None
        row.updated_at = datetime.datetime.utcnow()
        db.session.commit()
        return {'ok': True, 'configured': _is_configured(cfg)}

    @socketio.on('emit_spotify_page_get_status')
    def get_status():
        configured = _is_configured(cfg)
        if not configured:
            return {'configured': False, 'connected': False}

        token = spotify_db.SpotifyToken.query.first()
        connected = bool(token and token.access_token)

        sync_state = _get_sync_state()
        return {
            'configured': True,
            'connected': connected,
            'last_synced': sync_state.last_synced.isoformat() if sync_state and sync_state.last_synced else None,
            'liked_count': sync_state.liked_count if sync_state else 0,
            'matched_count': sync_state.matched_count if sync_state else 0,
            'unmatched_count': sync_state.unmatched_count if sync_state else 0,
        }

    @socketio.on('emit_spotify_page_sync')
    def trigger_sync():
        if not _is_configured(cfg):
            return {'error': 'Spotify not configured.'}

        token = spotify_db.SpotifyToken.query.first()
        if not token or not token.access_token:
            return {'error': 'Not connected to Spotify. Please authorize first.'}

        raw_cfg = OmegaConf.to_container(cfg, resolve=True)

        def task(ctx):
            # _make_oauth resolves credentials from the DB, so it needs an app
            # context (run_sync manages its own context internally).
            with app.app_context():
                oauth = _make_oauth(cfg, cache_handler)
            sp = spotipy.Spotify(auth_manager=oauth)
            matched, unmatched = run_sync(app, sp, raw_cfg, status_callback=lambda m: ctx.update(0, m))
            ctx.update(1.0, f'Done: {matched} matched, {unmatched} unmatched.')

        app.task_manager.submit('Spotify: sync ratings', task)
        return {'ok': True}

    @socketio.on('emit_spotify_page_get_unmatched')
    def get_unmatched(data):
        page = int(data.get('page', 1))
        per_page = int(data.get('per_page', 50))
        query = spotify_db.SpotifyTrackMapping.query.filter(
            spotify_db.SpotifyTrackMapping.file_path.is_(None),
            spotify_db.SpotifyTrackMapping.dismissed == False,
        )
        total = query.count()
        items = query.offset((page - 1) * per_page).limit(per_page).all()
        return {
            'total': total,
            'page': page,
            'items': [
                {
                    'id': m.id,
                    'spotify_id': m.spotify_id,
                    'artist': m.spotify_artist,
                    'title': m.spotify_title,
                    'album': m.spotify_album,
                }
                for m in items
            ],
        }

    @socketio.on('emit_spotify_page_dismiss_unmatched')
    def dismiss_unmatched(data):
        mapping = spotify_db.SpotifyTrackMapping.query.get(int(data.get('id', 0)))
        if mapping:
            mapping.dismissed = True
            db.session.commit()
        return {'ok': True}

    common_socket_events.show_loading_status('Spotify module ready!')
