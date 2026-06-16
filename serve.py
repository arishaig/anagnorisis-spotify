"""
serve.py — Spotify import module for Anagnorisis (offline GDPR-export based).

No Spotify API / OAuth. The user uploads their "Download your data" export and
we match its tracks to the local library and write 0–10 ratings.

HTTP:
  POST /spotify/upload   — receive the export .zip (multipart 'file')

SocketIO (incoming):
  emit_spotify_page_get_status        — upload presence + last-import stats
  emit_spotify_page_preview           — queue a dry-run (compute, write nothing)
  emit_spotify_page_commit            — queue a real import
  emit_spotify_page_get_unmatched     — paginated notable-unmatched tracks
  emit_spotify_page_dismiss_unmatched — hide a track from the unmatched list

SocketIO (outgoing):
  emit_spotify_result                 — {dry_run, distribution, matched, ...}
"""
import os

from flask import request
from omegaconf import OmegaConf

from src.socket_events import CommonSocketEvents
import modules.spotify_import.db_models as spotify_db
from modules.spotify_import import export_parser
from modules.spotify_import.sync import run_import, preview_distribution


def init_socket_events(socketio, app=None, cfg=None, data_folder='./project_data'):
    common = CommonSocketEvents(socketio, module_name='spotify')
    common.show_loading_status('Initializing Spotify import module...')

    upload_path = os.path.join(data_folder, 'spotify_import_upload.zip')

    def _raw_cfg() -> dict:
        return OmegaConf.to_container(cfg, resolve=True) if cfg is not None else {}

    def _queue_run(dry_run: bool):
        raw = _raw_cfg()
        recent_since = str((raw.get('spotify_import') or {}).get('recent_since') or '')

        def task(ctx):
            try:
                ctx.update(0, 'Reading export...')
                parsed = export_parser.parse(upload_path, recent_since=recent_since)
                if dry_run:
                    # Library-free: just the rating distribution. Returns instantly.
                    summary = preview_distribution(parsed, raw)
                else:
                    summary = run_import(
                        app, parsed, raw,
                        status_callback=lambda m: ctx.update(0, m),
                    )
                ctx.update(1.0, 'Preview ready' if dry_run else f"Imported — wrote {summary['written']} ratings")
                socketio.emit('emit_spotify_result', summary)
            except Exception as e:
                import traceback
                traceback.print_exc()
                ctx.update(1.0, f'Error: {e}')
                socketio.emit('emit_spotify_result', {'error': str(e), 'dry_run': dry_run})

        label = 'Spotify: preview ratings' if dry_run else 'Spotify: import ratings'
        app.task_manager.submit(label, task)

    # --- HTTP upload ----------------------------------------------------

    @app.route('/spotify/upload', methods=['POST'])
    @app.auth_decorator
    def spotify_upload():
        f = request.files.get('file')
        if not f or not f.filename:
            return {'error': 'No file provided.'}, 400
        os.makedirs(os.path.dirname(upload_path), exist_ok=True)
        f.save(upload_path)
        return {'ok': True}

    # --- SocketIO -------------------------------------------------------

    @socketio.on('emit_spotify_page_get_status')
    def get_status():
        state = spotify_db.SpotifySyncState.query.first()
        return {
            'has_upload': os.path.exists(upload_path),
            'last_synced': state.last_synced.isoformat() if state and state.last_synced else None,
            'matched_count': state.matched_count if state else 0,
            'unmatched_count': state.unmatched_count if state else 0,
        }

    @socketio.on('emit_spotify_page_preview')
    def preview():
        if not os.path.exists(upload_path):
            return {'error': 'No export uploaded yet.'}
        _queue_run(dry_run=True)
        return {'ok': True}

    @socketio.on('emit_spotify_page_commit')
    def commit():
        if not os.path.exists(upload_path):
            return {'error': 'No export uploaded yet.'}
        _queue_run(dry_run=False)
        return {'ok': True}

    @socketio.on('emit_spotify_page_get_unmatched')
    def get_unmatched(data):
        page = int(data.get('page', 1))
        per_page = int(data.get('per_page', 50))
        query = spotify_db.SpotifyTrackMapping.query.filter(
            spotify_db.SpotifyTrackMapping.file_path.is_(None),
            spotify_db.SpotifyTrackMapping.dismissed == False,
        ).order_by(spotify_db.SpotifyTrackMapping.applied_rating.desc())
        total = query.count()
        items = query.offset((page - 1) * per_page).limit(per_page).all()
        return {
            'total': total,
            'page': page,
            'items': [
                {
                    'id': m.id,
                    'artist': m.spotify_artist,
                    'title': m.spotify_title,
                    'album': m.spotify_album,
                    'rating': m.applied_rating,
                }
                for m in items
            ],
        }

    @socketio.on('emit_spotify_page_dismiss_unmatched')
    def dismiss_unmatched(data):
        mapping = spotify_db.SpotifyTrackMapping.query.get(int(data.get('id', 0)))
        if mapping:
            mapping.dismissed = True
            from src.db_models import db
            db.session.commit()
        return {'ok': True}

    common.show_loading_status('Spotify import module ready!')
