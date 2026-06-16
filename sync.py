"""
sync.py — match Spotify export tracks to the local library and compute ratings.

Data comes from a GDPR "Download your data" export (see export_parser.py), not
the Spotify API. Anagnorisis uses a 0–10 rating scale.

Rating model (all values configurable under `spotify_import` in config.yaml):

  base       = play-count tier (most plays → highest tier); None if 0 plays
  + playlist : if the track is in one of your playlists AND was played at least
               once, floor the rating at `playlist_rating` (a grabbed-but-never
               -played playlist track earns nothing)
  + liked    : floor at `liked_rating` (applies even with 0 plays)
  + disliked : OVERRIDE to `disliked_rating` — a deliberate "no" beats everything

Tracks that end up with rating None are left unrated for Anagnorisis's model.
"""
import datetime
import re
import unicodedata
from collections import Counter

# Heavy / Anagnorisis-environment imports are deferred into the functions that
# need them so the pure rating logic (compute_rating, _model) stays importable
# and unit-testable on its own.


# ---------------------------------------------------------------------------
# Rating-model defaults (overridable via config.yaml -> spotify_import)
# ---------------------------------------------------------------------------

DEFAULT_MODEL = {
    'play_count_ratings': [
        {'min_plays': 15, 'rating': 10},
        {'min_plays': 10, 'rating': 9},
        {'min_plays': 7,  'rating': 8},
        {'min_plays': 5,  'rating': 7},
        {'min_plays': 3,  'rating': 6},
        {'min_plays': 2,  'rating': 5},
        {'min_plays': 1,  'rating': 4},
    ],
    'liked_rating': 9,
    'playlist_rating': 7,
    'disliked_rating': 1,
}


def _model(cfg: dict) -> dict:
    s = (cfg or {}).get('spotify_import', {}) or {}
    model = dict(DEFAULT_MODEL)
    for key in ('play_count_ratings', 'liked_rating', 'playlist_rating', 'disliked_rating'):
        if s.get(key) is not None:
            model[key] = s[key]
    # tiers high→low so the first satisfied threshold wins
    model['_tiers'] = sorted(model['play_count_ratings'], key=lambda t: t['min_plays'], reverse=True)
    return model


def compute_rating(sig, model: dict):
    """Map a TrackSignals to a 0–10 rating, or None for 'no signal'."""
    rating = None

    for tier in model['_tiers']:
        if sig.plays >= tier['min_plays']:
            rating = tier['rating']
            break

    # Playlist membership only boosts something you actually listened to.
    if sig.in_playlist and sig.plays >= 1:
        rating = max(rating if rating is not None else 0, model['playlist_rating'])

    # A like is a deliberate positive — counts even if never streamed.
    if sig.liked:
        rating = max(rating if rating is not None else 0, model['liked_rating'])

    # A dislike is the strongest signal there is — overrides everything.
    if sig.disliked:
        rating = model['disliked_rating']

    return rating


# ---------------------------------------------------------------------------
# Normalisation + local library index
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


def build_library_index(app) -> dict:
    """Return {normalized_artist_title: file_path} from the music_library table."""
    from tinytag import TinyTag
    import modules.music.db_models as music_db

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


def match_track(artist: str, title: str, index: dict, threshold: float = 82.0):
    """(file_path, confidence) for the best local match, or (None, 0)."""
    from rapidfuzz import fuzz, process

    if not index:
        return None, 0.0
    query = _index_key(artist, title)
    if query in index:
        return index[query], 100.0
    result = process.extractOne(
        query, index.keys(), scorer=fuzz.token_sort_ratio, score_cutoff=threshold,
    )
    if result:
        best_key, score, _ = result
        return index[best_key], float(score)
    return None, 0.0


# ---------------------------------------------------------------------------
# Import
# ---------------------------------------------------------------------------

# Below this rating, an unmatched track isn't worth surfacing for review
# (you played it once or twice and don't own it — not actionable).
NOTABLE_UNMATCHED_RATING = 7


def preview_distribution(parsed, cfg: dict) -> dict:
    """
    Fast, library-free preview: the rating distribution implied by the export.

    This is what the dry run shows — it answers "does my rating model look
    right?" without the expensive local-library matching, so it returns
    instantly. Actual matching happens only at import time (run_import).
    """
    model = _model(cfg)
    distribution = Counter()
    for sig in parsed.tracks.values():
        rating = compute_rating(sig, model)
        if rating is not None:
            distribution[rating] += 1
    return {
        'dry_run': True,
        'distinct_tracks': len(parsed.tracks),
        'matched': None,            # unknown until import (matching is skipped here)
        'rated_matched': None,
        'rated_total': sum(distribution.values()),
        'unmatched_notable': None,
        'written': 0,
        'distribution': {str(k): v for k, v in sorted(distribution.items(), reverse=True)},
        'sources': parsed.summary(),
    }


def run_import(app, parsed, cfg: dict, status_callback=None, dry_run: bool = False) -> dict:
    """
    Match every signalled track to the local library and compute ratings.

    Returns a summary dict including the rating distribution. When `dry_run` is
    True, nothing is written — used to preview the outcome before committing.
    """
    from src.db_models import db

    def status(msg, frac=None):
        if status_callback:
            status_callback(msg, frac)

    s = (cfg or {}).get('spotify_import', {}) or {}
    threshold = float(s.get('match_threshold', 82))
    overwrite_mode = s.get('overwrite_ratings', 'if_higher')
    apply_dislikes = bool(s.get('apply_dislikes', True))
    model = _model(cfg)

    status('Reading tags across your music library…', 0.0)
    index = build_library_index(app)
    total = len(parsed.tracks)
    status(f'Local library: {len(index)} tracks. Matching {total} Spotify tracks…', 0.1)

    distribution = Counter()      # rating value -> count (matched tracks)
    matched = unmatched_notable = written = 0
    notable_examples = []

    with app.app_context():
        for i, sig in enumerate(parsed.tracks.values()):
            if i % 250 == 0:
                # Matching spans 10%→95% of the bar; index build was the first 10%.
                status(f'Matching {i}/{total}…', 0.1 + 0.85 * (i / total if total else 1))

            rating = compute_rating(sig, model)
            file_path, confidence = match_track(sig.artist, sig.title, index, threshold)

            if file_path:
                matched += 1
                if rating is not None:
                    distribution[rating] += 1
                if not dry_run:
                    _record_mapping(sig, file_path, confidence, rating)
                    if rating is not None:
                        written += _maybe_write_rating(
                            file_path, rating, sig.disliked, overwrite_mode, apply_dislikes)
            else:
                # Only surface unmatched tracks you clearly care about.
                if rating is not None and rating >= NOTABLE_UNMATCHED_RATING:
                    unmatched_notable += 1
                    if len(notable_examples) < 200 and not dry_run:
                        _record_mapping(sig, None, 0.0, rating)

        status('Saving…', 0.97)
        if not dry_run:
            _save_state(parsed, matched, unmatched_notable, written)
            db.session.commit()

    status(f'Import complete: wrote {written} ratings.', 1.0)
    return {
        'dry_run': dry_run,
        'distinct_tracks': len(parsed.tracks),
        'matched': matched,
        'rated_matched': sum(distribution.values()),
        'unmatched_notable': unmatched_notable,
        'written': written,
        'distribution': {str(k): v for k, v in sorted(distribution.items(), reverse=True)},
        'sources': parsed.summary(),
    }


def _maybe_write_rating(file_path, rating, is_dislike, overwrite_mode, apply_dislikes) -> int:
    import modules.music.db_models as music_db
    entry = music_db.MusicLibrary.query.filter_by(file_path=file_path).first()
    if not entry:
        return 0
    existing = entry.user_rating
    if is_dislike and apply_dislikes:
        should = True                      # a deliberate dislike always applies
    elif overwrite_mode == 'always':
        should = True
    elif existing is None:
        should = True
    elif overwrite_mode == 'if_higher':
        should = rating > existing
    else:                                  # 'never'
        should = False
    if should:
        entry.user_rating = rating
        entry.user_rating_date = datetime.datetime.utcnow()
        return 1
    return 0


def _record_mapping(sig, file_path, confidence, rating):
    import modules.spotify_import.db_models as spotify_db
    from src.db_models import db
    spotify_id = sig.uri.rsplit(':', 1)[-1] if sig.uri else (sig.artist + '|' + sig.title)
    mapping = spotify_db.SpotifyTrackMapping.query.filter_by(spotify_id=spotify_id).first()
    if mapping and mapping.dismissed:
        return
    if not mapping:
        mapping = spotify_db.SpotifyTrackMapping(spotify_id=spotify_id)
        db.session.add(mapping)
    mapping.spotify_artist = sig.artist
    mapping.spotify_title = sig.title
    mapping.spotify_album = sig.album
    mapping.file_path = file_path
    mapping.confidence = confidence if file_path else None
    mapping.matched_at = datetime.datetime.utcnow() if file_path else None
    mapping.applied_rating = rating


def _save_state(parsed, matched, unmatched_notable, written):
    import modules.spotify_import.db_models as spotify_db
    state = spotify_db.SpotifySyncState.query.first()
    if not state:
        state = spotify_db.SpotifySyncState()
        db.session.add(state)
    state.last_synced = datetime.datetime.utcnow()
    state.liked_count = parsed.summary().get('liked', 0)
    state.matched_count = matched
    state.unmatched_count = unmatched_notable
    state.status = 'ok'
