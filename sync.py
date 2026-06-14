"""
Track matching and rating computation for the Spotify import module.
"""
import re
import unicodedata
import datetime

from tinytag import TinyTag
from rapidfuzz import fuzz, process

import modules.music.db_models as music_db
import modules.spotify_import.db_models as spotify_db
from src.db_models import db

SCOPES = 'user-library-read user-top-read'


# ---------------------------------------------------------------------------
# Normalisation
# ---------------------------------------------------------------------------

_PARENS_RE = re.compile(r'[\(\[][^\)\]]*[\)\]]')
_FEAT_RE = re.compile(r'\s+(?:feat\.?|ft\.?|featuring)\s+.*', re.IGNORECASE)
_PUNCT_RE = re.compile(r'[^\w\s]')
_WS_RE = re.compile(r'\s+')


def _normalize(s: str) -> str:
    if not s:
        return ''
    s = s.lower()
    s = unicodedata.normalize('NFD', s)
    s = ''.join(c for c in s if unicodedata.category(c) != 'Mn')
    s = _PARENS_RE.sub('', s)
    s = _FEAT_RE.sub('', s)
    s = _PUNCT_RE.sub(' ', s)
    return _WS_RE.sub(' ', s).strip()


def _index_key(artist: str, title: str) -> str:
    return _normalize(artist) + ' ' + _normalize(title)


# ---------------------------------------------------------------------------
# Library index
# ---------------------------------------------------------------------------

def build_library_index(app) -> dict:
    """Return {normalized_artist_title: file_path} from the music_library table."""
    index = {}
    with app.app_context():
        entries = music_db.MusicLibrary.query.filter(
            music_db.MusicLibrary.file_path.isnot(None)
        ).all()
        file_paths = [e.file_path for e in entries]

    for file_path in file_paths:
        try:
            tag = TinyTag.get(file_path)
            title = (tag.title or '').strip()
            artist = (tag.artist or '').strip()
            if not title:
                continue
            key = _index_key(artist, title)
            if key and key not in index:
                index[key] = file_path
        except Exception:
            pass

    return index


# ---------------------------------------------------------------------------
# Matching
# ---------------------------------------------------------------------------

def match_track(artist: str, title: str, index: dict, threshold: float = 82.0):
    """
    Return (file_path, confidence) for the best local match, or (None, 0).
    Tries exact normalised match first, then rapidfuzz token_sort_ratio.
    """
    if not index:
        return None, 0.0

    query = _index_key(artist, title)

    if query in index:
        return index[query], 100.0

    result = process.extractOne(
        query,
        index.keys(),
        scorer=fuzz.token_sort_ratio,
        score_cutoff=threshold,
    )
    if result:
        best_key, score, _ = result
        return index[best_key], float(score)

    return None, 0.0


# ---------------------------------------------------------------------------
# Rating formula
# ---------------------------------------------------------------------------

def compute_rating(
    is_liked: bool,
    all_time_rank=None,
    medium_rank=None,
    is_in_saved_album: bool = False,
) -> float | None:
    """
    Map Spotify signals to a 1–5 user_rating.

    Priority (highest wins):
      liked + all-time top 10 → 5.0
      liked                   → 4.5
      all-time top 10         → 5.0
      all-time top 30         → 4.5
      all-time top 50         → 4.0
      medium-term top 10      → 4.0
      medium-term top 30      → 3.5
      medium-term top 50      → 3.0
      saved album only        → 3.0
      no signal               → None (let the model decide)
    """
    rating = None

    if is_in_saved_album:
        rating = 3.0

    if medium_rank is not None:
        if medium_rank <= 10:
            rating = max(rating or 0, 4.0)
        elif medium_rank <= 30:
            rating = max(rating or 0, 3.5)
        else:
            rating = max(rating or 0, 3.0)

    if all_time_rank is not None:
        if all_time_rank <= 10:
            rating = max(rating or 0, 5.0)
        elif all_time_rank <= 30:
            rating = max(rating or 0, 4.5)
        else:
            rating = max(rating or 0, 4.0)

    if is_liked:
        rating = max(rating or 0, 4.5)
        if all_time_rank is not None and all_time_rank <= 10:
            rating = 5.0

    return rating


# ---------------------------------------------------------------------------
# Main sync
# ---------------------------------------------------------------------------

def run_sync(app, sp, cfg: dict, status_callback=None) -> tuple[int, int]:
    """
    Pull data from Spotify, match to the local library, and write ratings.
    Returns (matched_count, unmatched_count).
    """
    def status(msg):
        if status_callback:
            status_callback(msg)

    spotify_cfg = cfg.get('spotify_import', {})
    threshold = float(spotify_cfg.get('match_threshold', 82))
    overwrite = bool(spotify_cfg.get('overwrite_ratings', False))

    # 1. Liked tracks
    status('Fetching liked tracks from Spotify...')
    all_tracks = {}  # spotify_id → signal dict

    offset = 0
    while True:
        results = sp.current_user_saved_tracks(limit=50, offset=offset)
        items = results.get('items', [])
        if not items:
            break
        for item in items:
            track = item.get('track')
            if not track or not track.get('id'):
                continue
            tid = track['id']
            all_tracks[tid] = {
                'spotify_id': tid,
                'artist': ', '.join(a['name'] for a in track.get('artists', [])),
                'title': track['name'],
                'album': track.get('album', {}).get('name', ''),
                'is_liked': True,
                'all_time_rank': None,
                'medium_rank': None,
                'is_in_saved_album': False,
            }
        offset += len(items)
        status(f'Fetched {offset} liked tracks...')
        if not results.get('next'):
            break

    liked_ids = set(all_tracks.keys())
    status(f'Fetched {len(liked_ids)} liked tracks. Fetching top tracks...')

    # 2. Top tracks
    for time_range, rank_key in [
        ('long_term', 'all_time_rank'),
        ('medium_term', 'medium_rank'),
    ]:
        results = sp.current_user_top_tracks(limit=50, time_range=time_range)
        for rank, item in enumerate(results.get('items', []), start=1):
            tid = item.get('id')
            if not tid:
                continue
            if tid not in all_tracks:
                all_tracks[tid] = {
                    'spotify_id': tid,
                    'artist': ', '.join(a['name'] for a in item.get('artists', [])),
                    'title': item['name'],
                    'album': item.get('album', {}).get('name', ''),
                    'is_liked': False,
                    'all_time_rank': None,
                    'medium_rank': None,
                    'is_in_saved_album': False,
                }
            all_tracks[tid][rank_key] = rank

    # 3. Saved albums (track IDs only — we only set the flag on tracks already known)
    status('Fetching saved albums...')
    offset = 0
    while True:
        results = sp.current_user_saved_albums(limit=50, offset=offset)
        items = results.get('items', [])
        if not items:
            break
        for item in items:
            for track in item.get('album', {}).get('tracks', {}).get('items', []):
                tid = track.get('id')
                if tid and tid in all_tracks:
                    all_tracks[tid]['is_in_saved_album'] = True
        offset += len(items)
        if not results.get('next'):
            break

    status(f'Processing {len(all_tracks)} tracks. Building library index...')
    index = build_library_index(app)
    status(f'Library index: {len(index)} entries. Matching...')

    matched_count = 0
    unmatched_count = 0

    with app.app_context():
        for i, (tid, info) in enumerate(all_tracks.items()):
            if i % 100 == 0:
                status(f'Matching {i}/{len(all_tracks)}...')

            existing = spotify_db.SpotifyTrackMapping.query.filter_by(spotify_id=tid).first()
            if existing and existing.dismissed:
                continue

            file_path, confidence = match_track(
                info['artist'], info['title'], index, threshold=threshold
            )

            rating = compute_rating(
                is_liked=info['is_liked'],
                all_time_rank=info.get('all_time_rank'),
                medium_rank=info.get('medium_rank'),
                is_in_saved_album=info.get('is_in_saved_album', False),
            )

            if existing:
                mapping = existing
            else:
                mapping = spotify_db.SpotifyTrackMapping(spotify_id=tid)
                db.session.add(mapping)

            mapping.spotify_artist = info['artist']
            mapping.spotify_title = info['title']
            mapping.spotify_album = info['album']
            mapping.file_path = file_path
            mapping.confidence = confidence if file_path else None
            mapping.matched_at = datetime.datetime.utcnow() if file_path else None
            mapping.applied_rating = rating if file_path else None

            if file_path and rating is not None:
                music_entry = music_db.MusicLibrary.query.filter_by(file_path=file_path).first()
                if music_entry:
                    if overwrite or music_entry.user_rating is None or rating > music_entry.user_rating:
                        music_entry.user_rating = rating
                        music_entry.user_rating_date = datetime.datetime.utcnow()
                matched_count += 1
            elif not file_path:
                unmatched_count += 1

        sync_state = spotify_db.SpotifySyncState.query.first()
        if not sync_state:
            sync_state = spotify_db.SpotifySyncState()
            db.session.add(sync_state)
        sync_state.last_synced = datetime.datetime.utcnow()
        sync_state.liked_count = len(liked_ids)
        sync_state.matched_count = matched_count
        sync_state.unmatched_count = unmatched_count
        sync_state.status = 'ok'

        db.session.commit()

    status(f'Sync complete: {matched_count} matched, {unmatched_count} unmatched.')
    return matched_count, unmatched_count
