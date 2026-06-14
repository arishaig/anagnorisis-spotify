from datetime import datetime
from src.db_models import db


class SpotifyToken(db.Model):
    __tablename__ = 'spotify_token'
    id = db.Column(db.Integer, primary_key=True)
    access_token = db.Column(db.String, nullable=True)
    refresh_token = db.Column(db.String, nullable=True)
    token_expiry = db.Column(db.DateTime, nullable=True)
    scope = db.Column(db.String, nullable=True)
    updated_at = db.Column(db.DateTime, default=datetime.utcnow)


class SpotifyTrackMapping(db.Model):
    __tablename__ = 'spotify_track_mapping'
    id = db.Column(db.Integer, primary_key=True)
    spotify_id = db.Column(db.String, unique=True, nullable=False, index=True)
    spotify_artist = db.Column(db.String, nullable=True)
    spotify_title = db.Column(db.String, nullable=True)
    spotify_album = db.Column(db.String, nullable=True)
    file_path = db.Column(db.String, nullable=True)
    confidence = db.Column(db.Float, nullable=True)
    matched_at = db.Column(db.DateTime, nullable=True)
    dismissed = db.Column(db.Boolean, default=False, server_default='0')
    applied_rating = db.Column(db.Float, nullable=True)


class SpotifySyncState(db.Model):
    __tablename__ = 'spotify_sync_state'
    id = db.Column(db.Integer, primary_key=True)
    last_synced = db.Column(db.DateTime, nullable=True)
    liked_count = db.Column(db.Integer, default=0)
    matched_count = db.Column(db.Integer, default=0)
    unmatched_count = db.Column(db.Integer, default=0)
    status = db.Column(db.String, nullable=True)
    oauth_state = db.Column(db.String, nullable=True)
