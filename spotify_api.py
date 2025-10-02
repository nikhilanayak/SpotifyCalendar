"""
Requires:
  pip install spotipy python-dotenv

Scopes you'll likely need:
  - playlist-read-private
  - playlist-read-collaborative   (if you want collab playlists too)
"""

import json
import os
import sqlite3
import time
from typing import List, Optional

import spotipy
from spotipy.oauth2 import SpotifyOAuth
from dotenv import load_dotenv
from tqdm import tqdm

# Load environment variables from config.env
load_dotenv('config.env')


# -------------------- Simple SQLite TTL Cache --------------------

class TTLCache:
    def __init__(self, db_path: str = "spotify_cache.sqlite"):
        self.db_path = db_path
        os.makedirs(os.path.dirname(db_path), exist_ok=True) if os.path.dirname(db_path) else None
        self._init()

    def _init(self):
        with sqlite3.connect(self.db_path) as conn:
            conn.execute("""
                CREATE TABLE IF NOT EXISTS kv_cache (
                    k TEXT PRIMARY KEY,
                    v TEXT NOT NULL,
                    ts INTEGER NOT NULL
                )
            """)
            conn.commit()

    def get(self, key: str, ttl_seconds: int) -> Optional[dict]:
        """Return cached JSON (as Python object) if not expired."""
        now = int(time.time())
        with sqlite3.connect(self.db_path) as conn:
            row = conn.execute("SELECT v, ts FROM kv_cache WHERE k = ?", (key,)).fetchone()
        if not row:
            return None
        v_json, ts = row
        if now - ts > ttl_seconds:
            # expired; drop it to avoid growing the DB
            self.delete(key)
            return None
        return json.loads(v_json)

    def set(self, key: str, obj: dict):
        with sqlite3.connect(self.db_path) as conn:
            conn.execute(
                "REPLACE INTO kv_cache (k, v, ts) VALUES (?, ?, ?)",
                (key, json.dumps(obj), int(time.time()))
            )
            conn.commit()

    def delete(self, key: str):
        with sqlite3.connect(self.db_path) as conn:
            conn.execute("DELETE FROM kv_cache WHERE k = ?", (key,))
            conn.commit()


# -------------------- Spotify Helpers --------------------

def _fetch_all_items(sp: spotipy.Spotify, first_page: dict) -> List[dict]:
    """Generic paginator for endpoints that return {items, next} structure."""
    items = []
    page = first_page
    while True:
        items.extend(page.get("items", []))
        if page.get("next"):
            page = sp.next(page)
        else:
            break
    return items


# -------------------- API: Get ALL playlist IDs --------------------

def get_all_playlist_ids(
    sp: spotipy.Spotify,
    *,
    cache: Optional[TTLCache] = None,
    ttl_seconds: int = 3600,
    force_refresh: bool = False,
    ignore_playlist_ids: Optional[List[str]] = None
) -> List[str]:
    """
    Returns every playlist ID for playlists owned by the current user (handles pagination).
    Uses SQLite TTL cache if provided.
    """
    user_id = sp.current_user()["id"]
    # Include ignore list in cache key to ensure different ignore lists get different cache entries
    ignore_key = ",".join(sorted(ignore_playlist_ids or []))
    cache_key = f"playlists:{user_id}:owned:v1:ignore={ignore_key}"

    if cache and not force_refresh:
        cached = cache.get(cache_key, ttl_seconds)
        if cached is not None:
            # No need to apply ignore filter here since it's already included in cache key
            return cached["ids"]

    first = sp.current_user_playlists(limit=50)
    all_items = _fetch_all_items(sp, first)
    # Filter to only include playlists owned by the current user
    ids = [p["id"] for p in all_items if p.get("id") and p.get("owner", {}).get("id") == user_id]

    # Apply ignore filter before caching
    if ignore_playlist_ids:
        ids = [pid for pid in ids if pid not in ignore_playlist_ids]

    if cache:
        cache.set(cache_key, {"ids": ids})

    return ids


# -------------------- API: Get ALL track IDs for a playlist --------------------

def get_all_tracks_for_playlist(
    sp: spotipy.Spotify,
    playlist_id: str,
    *,
    cache: Optional[TTLCache] = None,
    ttl_seconds: int = 3600,
    force_refresh: bool = False,
    include_local: bool = False
) -> List[dict]:
    """
    Returns every track in a given playlist with full track information (handles pagination).
    - Skips local/undiscoverable tracks by default (id=None) unless include_local=True.
    - Returns full track objects with all available metadata.
    """
    cache_key = f"playlist_tracks:{playlist_id}:local={include_local}:full:v1"

    if cache and not force_refresh:
        cached = cache.get(cache_key, ttl_seconds)
        if cached is not None:
            return cached["tracks"]

    # Fetch full track information with added_at date
    first = sp.playlist_items(
        playlist_id=playlist_id,
        additional_types=("track",),
        limit=100,
        fields="items(track,added_at),next"
    )
    all_items = _fetch_all_items(sp, first)

    tracks: List[dict] = []
    for it in all_items:
        t = it.get("track") or {}
        tid = t.get("id")
        is_local = t.get("is_local", False)
        added_at = it.get("added_at")  # Get added_at from the playlist item

        if tid:  # normal Spotify track
            # Add the added_at date to the track object
            t["added_at"] = added_at
            tracks.append(t)
        elif include_local and is_local:
            # For local tracks, include them if requested
            t["added_at"] = added_at
            tracks.append(t)

    if cache:
        cache.set(cache_key, {"tracks": tracks})

    return tracks


# -------------------- API: Sample weighted songs (frequency + recency) --------------------

def sample_weighted_songs(
    sp: spotipy.Spotify,
    *,
    n: int,
    cache: Optional[TTLCache] = None,
    ttl_seconds: int = 3600,
    score_function: Optional[callable] = None,
    temperature: float = 1.0,
    include_local: bool = False,
    ignore_playlist_ids: Optional[List[str]] = None,
) -> List[dict]:
    """
    Returns N tracks sampled without replacement across all playlists OWNED by the current user,
    using a custom scoring function and temperature-based sampling.

    Args:
        sp: Spotify client
        n: Number of tracks to sample
        cache: Optional cache for API calls
        ttl_seconds: Cache TTL
        score_function: Function that takes (track_add_date, frequency) and returns a score
        temperature: Sampling temperature (higher = more random, lower = more deterministic)
        include_local: Whether to include local tracks

    The score_function should have signature: score_function(added_date, frequency) -> float
    where:
        - added_date: datetime object or None
        - frequency: int (how many times track appears across playlists)
    
    Returns list of track objects with highest scores after temperature sampling.
    """
    if n <= 0:
        return []

    # Default scoring function (frequency + recency)
    if score_function is None:
        from datetime import datetime, timezone
        def default_score_function(added_date, frequency):
            # Frequency component (0-1)
            freq_score = min(frequency / 10.0, 1.0)  # Cap at 10 occurrences
            
            # Recency component (0-1)
            if added_date is None:
                recency_score = 0.0
            else:
                now = datetime.now(timezone.utc)
                days_ago = (now - added_date).days
                recency_score = max(0, 1.0 - (days_ago / 365.0))  # Decay over 365 days
            
            return (freq_score + recency_score) / 2.0

    # 1) Gather all owned playlists
    playlist_ids = get_all_playlist_ids(sp, cache=cache, ttl_seconds=ttl_seconds, ignore_playlist_ids=ignore_playlist_ids)

    # 2) Fetch all tracks for each playlist
    all_tracks: List[dict] = []
    for playlist_id in playlist_ids:
        tracks = get_all_tracks_for_playlist(
            sp,
            playlist_id,
            cache=cache,
            ttl_seconds=ttl_seconds,
            force_refresh=False,
            include_local=include_local,
        )
        # Only consider tracks with valid Spotify IDs
        all_tracks.extend([t for t in tracks if t.get("id")])

    if not all_tracks:
        return []

    # 3) Calculate frequency per track ID
    from collections import Counter
    id_counts = Counter(t["id"] for t in all_tracks)

    # 4) Find most recent added_at per track ID
    from datetime import datetime, timezone
    most_recent_added_at: dict = {}
    for t in all_tracks:
        tid = t.get("id")
        added_at = t.get("added_at")
        if not tid:
            continue
        
        if added_at:
            try:
                dt = datetime.fromisoformat(added_at.replace('Z', '+00:00'))
            except Exception:
                dt = None
        else:
            dt = None
            
        prev = most_recent_added_at.get(tid)
        if dt is not None and (prev is None or dt > prev):
            most_recent_added_at[tid] = dt

    # 5) Calculate scores for each unique track
    track_scores: dict = {}
    for tid in id_counts.keys():
        added_date = most_recent_added_at.get(tid)
        frequency = id_counts[tid]
        score = score_function(added_date, frequency)
        track_scores[tid] = score

    # 6) Temperature-based sampling
    import random
    import math
    
    # Convert scores to probabilities using temperature
    if temperature <= 0:
        # Deterministic: pick highest scores
        sorted_tracks = sorted(track_scores.items(), key=lambda x: x[1], reverse=True)
        sampled_ids = [tid for tid, _ in sorted_tracks[:n]]
    else:
        # Probabilistic sampling with temperature
        # Apply temperature: score / temperature, then softmax
        temp_scores = {tid: score / temperature for tid, score in track_scores.items()}
        
        # Softmax normalization
        max_score = max(temp_scores.values())
        exp_scores = {tid: math.exp(score - max_score) for tid, score in temp_scores.items()}
        total_exp = sum(exp_scores.values())
        probabilities = {tid: exp_score / total_exp for tid, exp_score in exp_scores.items()}
        
        # Sample without replacement
        sampled_ids = []
        remaining_tracks = list(probabilities.keys())
        remaining_probs = list(probabilities.values())
        
        for _ in range(min(n, len(remaining_tracks))):
            if not remaining_tracks:
                break
                
            # Sample one track
            r = random.random()
            cumulative = 0.0
            chosen_idx = 0
            
            for idx, prob in enumerate(remaining_probs):
                cumulative += prob
                if cumulative >= r:
                    chosen_idx = idx
                    break
            
            # Add chosen track and remove from candidates
            chosen_tid = remaining_tracks.pop(chosen_idx)
            remaining_probs.pop(chosen_idx)
            sampled_ids.append(chosen_tid)
            
            # Renormalize remaining probabilities
            if remaining_probs:
                total_remaining = sum(remaining_probs)
                remaining_probs = [p / total_remaining for p in remaining_probs]

    # 7) Return the most recent occurrence of each sampled track id
    sampled_tracks: List[dict] = []
    by_id: dict = {}
    for t in all_tracks:
        tid = t.get("id")
        if not tid or tid not in set(sampled_ids):
            continue
        prev = by_id.get(tid)
        t_dt = t.get("added_at")
        try:
            t_parsed = datetime.fromisoformat(t_dt.replace('Z', '+00:00')) if t_dt else None
        except Exception:
            t_parsed = None
        if prev is None:
            by_id[tid] = (t_parsed, t)
        else:
            prev_dt, _ = prev
            if t_parsed is not None and (prev_dt is None or t_parsed > prev_dt):
                by_id[tid] = (t_parsed, t)

    for tid in sampled_ids:
        entry = by_id.get(tid)
        if entry is not None:
            sampled_tracks.append(entry[1])

    return sampled_tracks


# -------------------- API: Add tracks to playlist by URL --------------------

def add_tracks_to_playlist_by_url(
    sp: spotipy.Spotify,
    *,
    playlist_url: str,
    track_ids: List[str],
    replace_existing: bool = True,
) -> str:
    """
    Add tracks to an existing playlist by URL.
    
    Args:
        sp: Spotify client
        playlist_url: Full Spotify playlist URL (e.g., https://open.spotify.com/playlist/37i9dQZF1DXcBWIGoYBM5M)
        track_ids: List of track IDs to add
        replace_existing: If True, remove all existing tracks before adding new ones
    
    Returns the Spotify playlist ID.
    """
    if not track_ids:
        print("No tracks to add")
        return None
    
    # Extract playlist ID from URL
    # Handle both open.spotify.com and spotify:playlist: formats
    if "open.spotify.com/playlist/" in playlist_url:
        playlist_id = playlist_url.split("open.spotify.com/playlist/")[1].split("?")[0]
    elif "spotify:playlist:" in playlist_url:
        playlist_id = playlist_url.split("spotify:playlist:")[1]
    else:
        # Assume it's already just the playlist ID
        playlist_id = playlist_url
    
    print(f"Adding {len(track_ids)} tracks to playlist {playlist_id}")
    
    # Verify playlist exists and get info
    try:
        playlist_info = sp.playlist(playlist_id, fields="name,owner.id")
        print(f"Found playlist: {playlist_info.get('name')}")
    except Exception as e:
        raise RuntimeError(f"Playlist not found or not accessible: {e}")
    
    if replace_existing:
        # Remove all existing tracks from the playlist
        print("Removing existing tracks...")
        existing_tracks = []
        results = sp.playlist_items(playlist_id=playlist_id, fields="items.track.id,next", limit=None)

        while True:
            existing_tracks.extend([item["track"]["id"] for item in results.get("items", []) if item.get("track") and item["track"].get("id")])
            if results.get("next"):
                results = sp.next(results)
            else:
                break
        
        # Remove in chunks of 100 (Spotify API limit)
        CHUNK_SIZE = 100
        for i in range(0, len(existing_tracks), CHUNK_SIZE):
            chunk = existing_tracks[i:i + CHUNK_SIZE]
            if chunk:
                sp.playlist_remove_all_occurrences_of_items(playlist_id=playlist_id, items=chunk)
        
        print(f"Removed {len(existing_tracks)} existing tracks")

    # Add tracks (in chunks of 100 per Spotify API limits)
    CHUNK_SIZE = 100
    for i in tqdm(range(0, len(track_ids), CHUNK_SIZE), desc="Adding tracks"):
        chunk = track_ids[i:i + CHUNK_SIZE]
        if chunk:
            sp.playlist_add_items(playlist_id=playlist_id, items=chunk)

    print(f"Successfully added {len(track_ids)} tracks to playlist")
    return playlist_id


# -------------------- API: Upsert playlist by internal ID and add tracks --------------------

def add_tracks_to_playlist_by_internal_id(
    sp: spotipy.Spotify,
    *,
    internal_id: str,
    playlist_name: str,
    track_ids: List[str],
    cache: Optional[TTLCache] = None,
    ttl_seconds: int = 86400,
    public: bool = False,
    description: Optional[str] = None,
) -> str:
    """
    Ensure a playlist exists for a given `internal_id` and add the provided tracks to it.

    - If `internal_id` is found in cache → use cached playlist_id
    - Else → create the playlist, cache its ID, then add tracks

    Returns the Spotify playlist ID.
    """
    if not track_ids:
        # Nothing to add; short-circuit but still ensure playlist exists
        track_ids = []

    me = sp.current_user()
    user_id = me.get("id")
    cache_key = f"playlist_alias:{user_id}:{internal_id}:v1"

    playlist_id: Optional[str] = None
    if cache is not None:
        cached = cache.get(cache_key, ttl_seconds)
        print(f"Cache lookup for key '{cache_key}': {cached}")
        if cached and isinstance(cached, dict):
            playlist_id = cached.get("playlist_id")
            print(f"Found cached playlist_id: {playlist_id}")

    # Create playlist if we do not have one yet
    if not playlist_id:
        print(f"Creating new playlist '{playlist_name}' for internal_id '{internal_id}'")
        created = sp.user_playlist_create(
            user=user_id,
            name=playlist_name,
            public=public,
            description=description or ""
        )
        playlist_id = created.get("id")
        if not playlist_id:
            raise RuntimeError("Failed to create playlist")
        if cache is not None:
            cache.set(cache_key, {"playlist_id": playlist_id})
            print(f"Cached playlist_id {playlist_id} for key '{cache_key}'")
    else:
        print(f"Using existing playlist_id {playlist_id} for internal_id '{internal_id}'")

    # Remove all songs from the playlist before adding new ones
    # Fetch all track URIs currently in the playlist (handle pagination)
    existing_tracks = []
    results = sp.playlist_items(playlist_id=playlist_id, fields="items.track.id,next", limit=None)

    while True:
        existing_tracks.extend([item["track"]["id"] for item in results.get("items", []) if item.get("track") and item["track"].get("id")])
        if results.get("next"):
            results = sp.next(results)
        else:
            break
    # Remove in chunks of 100 (Spotify API limit)
    CHUNK_SIZE = 100
    for i in range(0, len(existing_tracks), CHUNK_SIZE):
        chunk = existing_tracks[i:i + CHUNK_SIZE]
        if chunk:
            sp.playlist_remove_all_occurrences_of_items(playlist_id=playlist_id, items=chunk)

    # Add tracks (in chunks of 100 per Spotify API limits)
    CHUNK_SIZE = 100
    for i in range(0, len(track_ids), CHUNK_SIZE):
        chunk = track_ids[i:i + CHUNK_SIZE]
        if chunk:
            sp.playlist_add_items(playlist_id=playlist_id, items=chunk)

    return playlist_id
