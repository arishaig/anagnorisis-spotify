# anagnorisis-spotify

Spotify import module for [Anagnorisis](https://github.com/volotat/Anagnorisis). Pulls your liked tracks, top tracks, and saved albums from Spotify, matches them to your local music library, and writes ratings into the Anagnorisis `music_library` table.

## Rating formula

| Signal | Rating |
|---|---|
| Liked + all-time top 10 | 5.0 |
| Liked | 4.5 |
| All-time top 10 | 5.0 |
| All-time top 11–30 | 4.5 |
| All-time top 31–50 | 4.0 |
| Medium-term top 10 | 4.0 |
| Medium-term top 11–30 | 3.5 |
| Medium-term top 31–50 | 3.0 |
| Saved album (not liked) | 3.0 |
| No signal | *(null — model decides)* |

Existing ratings are only overwritten if the Spotify-derived rating is higher (configurable).

## Installation

Clone into your Anagnorisis `modules/` directory **with this exact name** so Python can import it:

```bash
cd /path/to/Anagnorisis/modules
git clone https://github.com/YOUR_USERNAME/anagnorisis-spotify spotify_import
```

Then install the Python dependencies inside the Anagnorisis environment:

```bash
pip install -r modules/spotify_import/requirements.txt
```

## Spotify app setup

1. Go to [developer.spotify.com/dashboard](https://developer.spotify.com/dashboard) and create an app.
2. Under **Edit Settings → Redirect URIs**, add:
   ```
   https://YOUR_ANAGNORISIS_HOST/spotify/callback
   ```
3. Note your **Client ID** and **Client Secret**.

## Configuration

There are two ways to supply your Client ID, Client Secret, and redirect URI.

### Option A — in the UI (recommended)

Open the **Spotify** tab. If credentials aren't set yet, the **Spotify app credentials**
form is expanded: paste your Client ID and Secret, confirm the pre-filled redirect URI,
and click **Save credentials**. They're stored in the database — nothing to put in
`config.yaml`, and the secret is never sent back to the browser once saved.

### Option B — in `config.yaml`

Useful for disaster recovery or declarative setups. UI-saved values override these.

```yaml
spotify_import:
  client_id: "YOUR_CLIENT_ID"
  client_secret: "YOUR_CLIENT_SECRET"
  redirect_uri: "https://YOUR_ANAGNORISIS_HOST/spotify/callback"
```

### Optional tuning (either method)

```yaml
spotify_import:
  match_threshold: 82          # 0–100 fuzzy match strictness (default: 82)
  overwrite_ratings: if_higher # never | if_higher | always (default: if_higher)
```

Restart Anagnorisis (or just open the tab if it's already running). A **Spotify** tab
appears in the UI automatically — the module is discovered from its folder name.

## Usage

1. Open the **Spotify** tab and click **Connect to Spotify**.
2. Authorize the app on Spotify's consent screen.
3. Click **Sync Now**. The task runs in the background — watch the task manager for progress.
4. After sync, review **Unmatched tracks**: these are in your Spotify library but couldn't be found locally. Dismiss tracks you don't own locally.

Re-run sync any time to pick up new liked tracks.

## How matching works

Each local file's tags (artist + title) are read via TinyTag and indexed with normalised keys (lowercased, accents stripped, parenthetical suffixes removed). Spotify tracks are matched against this index using exact normalised lookup first, then `rapidfuzz.token_sort_ratio` with a configurable threshold. Matches and non-matches are cached in the `spotify_track_mapping` table so subsequent syncs are fast.
