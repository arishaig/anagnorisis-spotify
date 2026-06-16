"""
Standalone tests for the Spotify import module's pure logic (no Anagnorisis app
or DB required): the rating model and the export parser.

Run from the module directory:  python -m pytest tests/ -q
"""
import io
import json
import os
import sys
import zipfile

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

import export_parser as ep
import sync


MODEL = sync._model({})  # defaults


def sig(**kw):
    base = dict(uri='spotify:track:x', artist='A', title='T')
    base.update(kw)
    return ep.TrackSignals(**base)


# --- rating model ----------------------------------------------------------

def test_play_count_tiers():
    assert sync.compute_rating(sig(plays=20), MODEL) == 10
    assert sync.compute_rating(sig(plays=15), MODEL) == 10
    assert sync.compute_rating(sig(plays=10), MODEL) == 9
    assert sync.compute_rating(sig(plays=3), MODEL) == 6
    assert sync.compute_rating(sig(plays=1), MODEL) == 4
    assert sync.compute_rating(sig(plays=0), MODEL) is None


def test_liked_floors_even_without_plays():
    assert sync.compute_rating(sig(plays=0, liked=True), MODEL) == 9
    # a like never lowers a stronger play-count rating
    assert sync.compute_rating(sig(plays=20, liked=True), MODEL) == 10


def test_dislike_overrides_everything():
    assert sync.compute_rating(sig(plays=0, disliked=True), MODEL) == 1
    assert sync.compute_rating(sig(plays=20, disliked=True), MODEL) == 1
    assert sync.compute_rating(sig(plays=20, liked=True, disliked=True), MODEL) == 1


def test_playlist_boost_only_when_played():
    # in a playlist but never played -> no rating (grabbed-but-unlistened)
    assert sync.compute_rating(sig(plays=0, in_playlist=True), MODEL) is None
    # played once + in a playlist -> boosted to the playlist floor
    assert sync.compute_rating(sig(plays=1, in_playlist=True), MODEL) == 7
    # already above the floor -> unchanged
    assert sync.compute_rating(sig(plays=15, in_playlist=True), MODEL) == 10


# --- parser ----------------------------------------------------------------

def _zip(files: dict) -> bytes:
    buf = io.BytesIO()
    with zipfile.ZipFile(buf, 'w') as zf:
        for name, obj in files.items():
            zf.writestr(name, json.dumps(obj))
    return buf.getvalue()


def test_streaming_filters_and_counts():
    history = [
        {'spotify_track_uri': 'spotify:track:1', 'ms_played': 200000, 'skipped': False,
         'master_metadata_track_name': 'Song', 'master_metadata_album_artist_name': 'Band',
         'master_metadata_album_album_name': 'Album', 'ts': '2024-01-01T00:00:00Z'},
        {'spotify_track_uri': 'spotify:track:1', 'ms_played': 200000, 'skipped': False,
         'ts': '2025-06-01T00:00:00Z'},
        {'spotify_track_uri': 'spotify:track:1', 'ms_played': 5000, 'skipped': False,    # too short
         'ts': '2025-06-01T00:00:00Z'},
        {'spotify_track_uri': 'spotify:track:1', 'ms_played': 200000, 'skipped': True,   # skipped
         'ts': '2025-06-01T00:00:00Z'},
        {'spotify_track_uri': None, 'ms_played': 200000, 'skipped': False},              # podcast
    ]
    p = ep.parse(_zip({'Streaming_History_Audio_2024.json': history}), recent_since='2025')
    s = p.tracks['1']
    assert s.plays == 2          # only the two qualifying plays
    assert s.recent_plays == 1   # one of them in 2025
    assert s.last_played == '2025-06-01T00:00:00Z'
    assert p.summary()['distinct_tracks'] == 1


def test_account_data_flags_merge_onto_streaming():
    history = [{'spotify_track_uri': 'spotify:track:1', 'ms_played': 200000, 'skipped': False,
                'master_metadata_track_name': 'Song', 'master_metadata_album_artist_name': 'Band',
                'ts': '2024-01-01T00:00:00Z'}]
    library = {
        'tracks': [{'uri': 'spotify:track:1', 'artist': 'Band', 'track': 'Song', 'album': 'Album'}],
        'bannedTracks': [{'uri': 'spotify:track:2', 'artist': 'X', 'track': 'Bad', 'album': 'Y'}],
    }
    playlists = {'playlists': [{'name': 'mix', 'items': [
        {'track': {'trackUri': 'spotify:track:1', 'trackName': 'Song', 'artistName': 'Band'}}]}]}

    p = ep.parse(_zip({
        'Streaming_History_Audio_2024.json': history,
        'YourLibrary.json': library,
        'Playlist1.json': playlists,
    }))
    assert p.tracks['1'].liked is True
    assert p.tracks['1'].in_playlist is True
    assert p.tracks['1'].plays == 1
    # banned track exists with no plays, flagged disliked
    assert p.tracks['2'].disliked is True
    assert p.tracks['2'].plays == 0
    assert sync.compute_rating(p.tracks['2'], MODEL) == 1   # dislike override
