from spotify_api import get_all_playlist_ids, TTLCache, get_all_tracks_for_playlist
import os
from dotenv import load_dotenv
import requests
import spotipy
from spotipy import SpotifyOAuth
import json
import tempfile
import subprocess
import time

load_dotenv('config.env')

client_id = os.getenv('SPOTIFY_CLIENT_ID')
client_secret = os.getenv('SPOTIFY_CLIENT_SECRET')
redirect_uri = os.getenv('SPOTIFY_REDIRECT_URI', 'http://localhost:8888/callback')
cache_ttl = int(os.getenv('CACHE_TTL_SECONDS', '3600'))

sp = spotipy.Spotify(auth_manager=SpotifyOAuth(
    client_id=client_id,
    client_secret=client_secret,
    redirect_uri=redirect_uri,
    scope="playlist-read-private playlist-read-collaborative playlist-modify-private playlist-modify-public"
))

cache = TTLCache("spotify_cache.sqlite")


def download_audio_robust(track_id, output_path):
    """
    Download audio using spotdl as primary method, yt-dlp as fallback
    """
    try:
        # Get track info from Spotify
        track_info = sp.track(track_id)
        track_name = track_info['name']
        artist_name = track_info['artists'][0]['name']
        
        # Verify the track ID matches what we expect
        if track_info['id'] != track_id:
            print(f"  WARNING: Track ID mismatch! Expected {track_id}, got {track_info['id']}")
            return False
        
        # Try spotdl first (primary method)
        print(f"  Trying spotdl: {track_name} by {artist_name}")
        spotify_url = f"https://open.spotify.com/track/{track_id}"
        print(f"  Spotify URL: {spotify_url}")
        try:
            cmd = [
                'spotdl', 'download',
                spotify_url,
                '--output', os.path.dirname(output_path),
                '--format', 'wav',
                '--overwrite', 'skip',
                '--threads', '8'
            ]
            
            print(f"  Running command: {' '.join(cmd)}")
            result = subprocess.run(cmd, timeout=120)
            
            if result.returncode == 0:
                # Find the downloaded file
                for file in os.listdir(os.path.dirname(output_path)):
                    if file.endswith('.wav'):
                        # Check if this is likely our track
                        src_path = os.path.join(os.path.dirname(output_path), file)
                        if os.path.getsize(src_path) > 1000:  # Valid audio file
                            os.rename(src_path, output_path)
                            print(f"  Successfully downloaded with spotdl")
                            return True
        except Exception as e:
            print(f"  spotdl failed: {e}")
        
        # Fallback to yt-dlp with better options
        print(f"  Trying yt-dlp as fallback...")
        clean_track_name = track_name.replace('(feat.', '').replace(')', '').replace(' - ', ' ').strip()
        clean_artist_name = artist_name.replace(' & ', ' ').replace(',', '').strip()
        
        search_queries = [
            f"{clean_artist_name} {clean_track_name}",
            f"{clean_artist_name} {clean_track_name} official",
            f"{clean_artist_name} {clean_track_name} audio",
        ]
        
        for search_query in search_queries:
            print(f"  Trying yt-dlp: {search_query}")
            
            cmd = [
                'yt-dlp',
                '--extract-audio',
                '--audio-format', 'wav',
                '--output', output_path,
                '--no-playlist',
                '--max-downloads', '1',
                '--ignore-errors',
                '--no-warnings',
                '--extractor-args', 'youtube:player_client=android',
                '--user-agent', 'Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36',
                f'ytsearch1:{search_query}'
            ]
            
            result = subprocess.run(cmd, timeout=120)
            
            if result.returncode == 0 and os.path.exists(output_path) and os.path.getsize(output_path) > 1000:
                print(f"  Successfully downloaded: {search_query}")
                return True
            else:
                print(f"  Failed: {result.stderr[:100]}...")
                continue
        
        print(f"  All download methods failed for {track_name}")
        return False
            
    except Exception as e:
        print(f"Error downloading audio: {e}")
        return False


def get_features(spotify_id, cache=None, ttl_seconds=86400):
    """
    Get audio features for a Spotify track ID.
    First tries reccobeats API, then falls back to audio file analysis.
    Uses TTL cache to avoid repeated API calls and downloads.
    """
    cache_key = f"audio_features:{spotify_id}:v1"
    
    # Check cache first
    if cache:
        cached = cache.get(cache_key, ttl_seconds)
        if cached is not None:
            print(f"Using cached features for {spotify_id}")
            return cached
    
    print(f"Fetching features for {spotify_id}...")
    
    # Try reccobeats API first
    try:
        response = requests.get(f"https://api.reccobeats.com/v1/audio-features?ids={spotify_id}", timeout=10)
        result = response.json()
        
        # Check if we got valid features
        if result.get("content") and len(result["content"]) > 0:
            # Cache the successful result
            if cache:
                cache.set(cache_key, result)
            return result
    except Exception as e:
        print(f"Reccobeats API failed: {e}")
    
    print(f"No features found in reccobeats for {spotify_id}, trying audio file analysis...")
    
    # Fallback: download audio and analyze
    with tempfile.NamedTemporaryFile(suffix='.wav', delete=False) as temp_file:
        temp_path = temp_file.name
    
    try:
        # Download audio using robust method
        if download_audio_robust(spotify_id, temp_path):
            # Upload to analysis API with retry logic
            max_retries = 3
            for attempt in range(max_retries):
                try:
                    print(f"  Uploading to analysis API (attempt {attempt + 1}/{max_retries})...")
                    with open(temp_path, 'rb') as audio_file:
                        files = {'audioFile': audio_file}
                        response = requests.post(
                            'https://api.reccobeats.com/v1/analysis/audio-features',
                            files=files,
                            timeout=120  # Increased timeout
                        )
                        
                        if response.status_code == 200:
                            result = response.json()
                            # Cache the successful result
                            if cache:
                                cache.set(cache_key, result)
                            return result
                        else:
                            print(f"Analysis API failed: {response.status_code} - {response.text}")
                            if attempt < max_retries - 1:
                                print(f"  Retrying in 5 seconds...")
                                time.sleep(5)
                except requests.exceptions.RequestException as e:
                    print(f"  Request error: {e}")
                    if attempt < max_retries - 1:
                        print(f"  Retrying in 5 seconds...")
                        time.sleep(5)
                    else:
                        raise
        else:
            print(f"Failed to download audio for {spotify_id}")
            
    except requests.exceptions.Timeout:
        print(f"Timeout downloading/analyzing {spotify_id}")
    except Exception as e:
        print(f"Error in audio analysis: {e}")
    finally:
        # Clean up temp file
        if os.path.exists(temp_path):
            os.unlink(temp_path)
    
    # Don't cache failures - allow retrying on subsequent runs
    return {"content": []}


ids = get_all_playlist_ids(sp, cache=cache, ttl_seconds=cache_ttl, ignore_playlist_ids=[os.getenv('TARGET_PLAYLIST_URL')])

songs = get_all_tracks_for_playlist(sp, ids[0], cache=cache, ttl_seconds=cache_ttl)

for i in range(len(songs)):
    id_ = songs[i].get("id")
    if id_:
        print("Name of track: ", songs[i].get("name"))
        features = get_features(id_, cache=cache, ttl_seconds=cache_ttl)
        print(f"Track {i+1}: {id_} - Features: {len(features.get('content', []))} items")
        if features.get("content"):
            print(f"  First feature: {features['content'][0]}")
        else:
            print(f"  No features available")

"""

https://api.reccobeats.com/v1/track/:id/audio-features
Gets audio features given a reccobeats track id


"""