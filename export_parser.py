"""
export_parser.py — read a Spotify "Download your data" (GDPR) export.

No Spotify API involved. Handles, in any combination, from a .zip or a directory:

  - Extended Streaming History:  Streaming_History_Audio_*.json
        → per-track qualifying play counts (total + recent), last played
  - Account data:                YourLibrary.json
        → liked tracks, saved albums, banned/disliked tracks
                                 Playlist*.json
        → membership in your created playlists

The parser only aggregates raw signals; the rating model lives in sync.py.

Spotify renames export fields periodically, so every field read goes through a
candidate-key fallback. The streaming-history shape is verified against a real
2026 export; the account-data shapes (YourLibrary/Playlist) follow the
documented structure and should be re-verified against a real account export —
search for VERIFY-ON-ACCOUNT-EXPORT below.
"""
import io
import json
import os
import zipfile
from dataclasses import dataclass, field

# A "qualifying" play: at least 30s and not skipped — mirrors how Spotify itself
# counts a stream, and filters out accidental opens / quick skips.
MIN_PLAY_MS = 30_000


@dataclass
class TrackSignals:
    uri: str
    artist: str = ''
    title: str = ''
    album: str = ''
    plays: int = 0
    recent_plays: int = 0
    last_played: str = ''          # ISO timestamp of most recent qualifying play
    liked: bool = False
    disliked: bool = False
    in_playlist: bool = False
    playlist_count: int = 0


@dataclass
class ParsedExport:
    tracks: dict = field(default_factory=dict)   # uri -> TrackSignals
    has_streaming: bool = False
    has_library: bool = False
    has_playlists: bool = False

    def summary(self) -> dict:
        t = self.tracks.values()
        return {
            'has_streaming': self.has_streaming,
            'has_library': self.has_library,
            'has_playlists': self.has_playlists,
            'distinct_tracks': len(self.tracks),
            'with_plays': sum(1 for s in t if s.plays > 0),
            'liked': sum(1 for s in t if s.liked),
            'disliked': sum(1 for s in t if s.disliked),
            'in_playlist': sum(1 for s in t if s.in_playlist),
        }


# ---------------------------------------------------------------------------
# Source loading (zip or directory)
# ---------------------------------------------------------------------------

def _iter_json_files(source):
    """Yield (basename, parsed_json) for every *.json in a zip path/bytes or dir."""
    if isinstance(source, (bytes, bytearray)):
        source = io.BytesIO(source)

    if isinstance(source, str) and os.path.isdir(source):
        for root, _dirs, files in os.walk(source):
            for name in files:
                if name.lower().endswith('.json'):
                    with open(os.path.join(root, name), 'rb') as fh:
                        yield name, _safe_json(fh.read())
        return

    # zip path or file-like
    with zipfile.ZipFile(source) as zf:
        for info in zf.infolist():
            if info.is_dir() or not info.filename.lower().endswith('.json'):
                continue
            yield os.path.basename(info.filename), _safe_json(zf.read(info))


def _safe_json(raw):
    try:
        return json.loads(raw)
    except (ValueError, TypeError):
        return None


# ---------------------------------------------------------------------------
# Field helpers (tolerant of Spotify's renames)
# ---------------------------------------------------------------------------

def _first(d, *keys):
    """Return the first present, non-empty value among keys."""
    if not isinstance(d, dict):
        return None
    for key in keys:
        if key in d and d[key] not in (None, ''):
            return d[key]
    return None


def _uri_to_id(uri):
    """spotify:track:ID -> ID; pass through anything else."""
    if isinstance(uri, str) and uri.startswith('spotify:'):
        return uri.rsplit(':', 1)[-1]
    return uri


# ---------------------------------------------------------------------------
# Parsing
# ---------------------------------------------------------------------------

def parse(source, recent_since: str = '') -> ParsedExport:
    """
    Parse an export. `recent_since` is an ISO date prefix (e.g. '2025'); plays
    with ts >= it count toward `recent_plays`. Empty disables the recent split.
    """
    result = ParsedExport()

    for name, data in _iter_json_files(source):
        if data is None:
            continue
        lname = name.lower()
        if lname.startswith('streaming_history_audio'):
            _parse_streaming(data, result, recent_since)
            result.has_streaming = True
        elif lname.startswith('yourlibrary'):
            _parse_library(data, result)
            result.has_library = True
        elif lname.startswith('playlist'):
            _parse_playlists(data, result)
            result.has_playlists = True

    return result


def _get(result, uri, artist='', title='', album=''):
    """Fetch/create a TrackSignals for a uri, filling metadata if still blank."""
    key = _uri_to_id(uri)
    sig = result.tracks.get(key)
    if sig is None:
        sig = TrackSignals(uri=uri or '', artist=artist or '', title=title or '', album=album or '')
        result.tracks[key] = sig
    else:
        if not sig.artist and artist:
            sig.artist = artist
        if not sig.title and title:
            sig.title = title
        if not sig.album and album:
            sig.album = album
    return sig


def _parse_streaming(records, result, recent_since):
    if not isinstance(records, list):
        return
    for r in records:
        if not isinstance(r, dict):
            continue
        uri = _first(r, 'spotify_track_uri', 'trackUri', 'track_uri')
        if not uri:
            continue  # podcast / audiobook / video row — skip
        ms = _first(r, 'ms_played', 'msPlayed') or 0
        skipped = bool(r.get('skipped'))
        if ms < MIN_PLAY_MS or skipped:
            continue

        title = _first(r, 'master_metadata_track_name', 'trackName')
        artist = _first(r, 'master_metadata_album_artist_name', 'artistName')
        album = _first(r, 'master_metadata_album_album_name', 'albumName')
        sig = _get(result, uri, artist, title, album)
        sig.plays += 1

        ts = _first(r, 'ts', 'endTime') or ''
        if ts > sig.last_played:
            sig.last_played = ts
        if recent_since and ts >= recent_since:
            sig.recent_plays += 1


def _parse_library(data, result):
    """YourLibrary.json — liked tracks, saved albums, banned/disliked tracks.

    VERIFY-ON-ACCOUNT-EXPORT: exact key names ('tracks', 'bannedTracks', etc.)
    follow the documented structure; confirm against a real account export.
    """
    if not isinstance(data, dict):
        return

    liked = _first(data, 'tracks', 'likedSongs', 'savedTracks') or []
    for item in liked if isinstance(liked, list) else []:
        uri = _first(item, 'uri', 'trackUri')
        if uri:
            sig = _get(result, uri,
                       _first(item, 'artist', 'artistName') or '',
                       _first(item, 'track', 'trackName') or '',
                       _first(item, 'album', 'albumName') or '')
            sig.liked = True

    banned = _first(data, 'bannedTracks', 'banned', 'dislikedTracks', 'hiddenTracks') or []
    for item in banned if isinstance(banned, list) else []:
        uri = _first(item, 'uri', 'trackUri')
        if uri:
            sig = _get(result, uri,
                       _first(item, 'artist', 'artistName') or '',
                       _first(item, 'track', 'trackName') or '',
                       _first(item, 'album', 'albumName') or '')
            sig.disliked = True


def _parse_playlists(data, result):
    """Playlist*.json — flag tracks that appear in any of your created playlists.

    Note: playlist membership is only used as a *boost* on already-played tracks
    in sync.py (grabbed-but-unplayed playlists shouldn't earn a rating), so we
    just record the raw flag here.

    VERIFY-ON-ACCOUNT-EXPORT: confirm the playlists/items/track nesting.
    """
    if not isinstance(data, dict):
        return
    playlists = _first(data, 'playlists', 'playlist') or []
    for pl in playlists if isinstance(playlists, list) else []:
        items = _first(pl, 'items', 'tracks') or []
        for item in items if isinstance(items, list) else []:
            track = item.get('track') if isinstance(item, dict) else None
            track = track if isinstance(track, dict) else item
            uri = _first(track, 'trackUri', 'uri', 'track_uri')
            if uri:
                sig = _get(result, uri,
                           _first(track, 'artistName', 'artist') or '',
                           _first(track, 'trackName', 'track') or '',
                           _first(track, 'albumName', 'album') or '')
                sig.in_playlist = True
                sig.playlist_count += 1
