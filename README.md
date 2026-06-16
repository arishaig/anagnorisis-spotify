# anagnorisis-spotify

Spotify import module for [Anagnorisis](https://github.com/volotat/Anagnorisis). It turns your
Spotify listening history into ratings in your local `music_library` — **entirely offline**, from
Spotify's GDPR "Download your data" export. No API, no developer app, no OAuth.

> Spotify froze new API app creation for individuals (and gated dev-mode behind Premium + a 5-user
> cap), so this module reads the data export instead. It works regardless of API access.

## What it reads

| Export | File(s) | Signal |
|---|---|---|
| **Extended streaming history** | `Streaming_History_Audio_*.json` | per-track play counts |
| **Account data** | `YourLibrary.json` | liked songs, saved albums, **disliked/hidden** |
| **Account data** | `Playlist*.json` | playlist membership |

Either export works on its own; upload both for the richest signal.

## Rating model (Anagnorisis uses a 0–10 scale)

A "qualifying play" is ≥30s and not skipped. The rating is the highest tier a track qualifies for:

```
play count   15+ →10   10–14 →9   7–9 →8   5–6 →7   3–4 →6   2 →5   1 →4
+ in a playlist (and played at least once) → at least 7
+ liked                                     → at least 9   (even with 0 plays)
+ disliked / hidden                         → overrides to 1  (wins over everything)
```

Tracks below the lowest play threshold and with no other signal are left **unrated** so
Anagnorisis's own model decides them. Every number is configurable (see below), and you always
**preview** before anything is written.

## Installation

Clone into your Anagnorisis `modules/` directory **with this exact name** (Python imports it as
`modules.spotify_import`):

```bash
cd /path/to/Anagnorisis/modules
git clone https://github.com/arishaig/anagnorisis-spotify spotify_import
pip install -r modules/spotify_import/requirements.txt   # rapidfuzz (tinytag ships with Anagnorisis)
```

Restart Anagnorisis. A **Spotify import** tab appears automatically — modules are discovered by folder.

## Getting your export

1. Go to [spotify.com/account/privacy](https://www.spotify.com/account/privacy/).
2. Tick **Extended streaming history** (play counts) and, for likes/dislikes/playlists, **Account data**.
3. Spotify emails a `.zip` — the streaming history typically arrives in a few days, account data sooner.

## Usage

1. **Upload** the `.zip` on the Spotify import tab.
2. **Preview (dry run)** — see the rating distribution and match counts; nothing is written.
3. Tweak the model in `config.yaml` if you like, re-preview, then **Import ratings**.
4. Review **Unmatched** — highly-rated tracks not found locally (worth acquiring or tagging).

## Configuration

All optional — sensible defaults ship in `config.defaults.yaml`.

```yaml
spotify_import:
  match_threshold: 82          # fuzzy match strictness (0–100)
  overwrite_ratings: if_higher # never | if_higher | always
  apply_dislikes: true         # apply dislikes even under if_higher
  play_count_ratings:          # min_plays → rating (0–10); highest match wins
    - {min_plays: 15, rating: 10}
    - {min_plays: 10, rating: 9}
    - {min_plays: 7,  rating: 8}
    - {min_plays: 5,  rating: 7}
    - {min_plays: 3,  rating: 6}
    - {min_plays: 2,  rating: 5}
    - {min_plays: 1,  rating: 4}
  liked_rating: 9
  playlist_rating: 7
  disliked_rating: 1
```

## How matching works

Each local file's tags (artist + title) are read with TinyTag and indexed under a normalised key
(lowercased, accents stripped, parentheticals/`feat.` removed). Export tracks are matched by exact
normalised key first, then `rapidfuzz.token_sort_ratio` above `match_threshold`. Note the export's
artist is the *album* artist, so features/compilations may need the unmatched-review pass.
