from asyncio import to_thread
import os
from spotify_api import sample_weighted_songs, TTLCache, add_tracks_to_playlist_by_internal_id, add_tracks_to_playlist_by_url, get_all_playlist_ids
import spotipy
from spotipy import SpotifyOAuth
from math import sqrt
import math
from datetime import datetime, timezone
from datetime import date, timedelta
import matplotlib.pyplot as plt
from tqdm import tqdm

client_id = os.getenv('SPOTIFY_CLIENT_ID')
client_secret = os.getenv('SPOTIFY_CLIENT_SECRET')
redirect_uri = os.getenv('SPOTIFY_REDIRECT_URI', 'http://localhost:8888/callback')
cache_ttl = int(os.getenv('CACHE_TTL_SECONDS', '3600'))

if not client_id or not client_secret:
    print("Error: Please set SPOTIFY_CLIENT_ID and SPOTIFY_CLIENT_SECRET in config.env")
    exit(1)

sp = spotipy.Spotify(auth_manager=SpotifyOAuth(
    client_id=client_id,
    client_secret=client_secret,
    redirect_uri=redirect_uri,
    scope="playlist-read-private playlist-read-collaborative playlist-modify-private playlist-modify-public"
))

cache = TTLCache("spotify_cache.sqlite")

# Single call: get N weighted-sampled tracks with custom scoring and temperature
N = int(os.getenv('SAMPLE_SIZE', '10'))
temperature = float(os.getenv('SAMPLING_TEMPERATURE', '1.0'))

def score(added_date, frequency):

    today = datetime.now(timezone.utc)

    if (today - added_date).days > 730:
        return -4

    #return -(today - added_date).days

    if added_date is not None:
        seasonal_delta = math.cos(2 * math.pi * (today - added_date).days / 365.0)
        #if (today - added_date).days > 0:
        #    seasonal_delta *= 0.5
        days_delta = (today - added_date).days

        return ((seasonal_delta) - days_delta / 750)# * math.sqrt(frequency)
        
    else:
        # No date available - give low score
        final_score = 0.1

    return final_score


def graph():
    # Plot so that the x-axis goes from newest (now) to oldest (3 years ago)
    start = datetime.now(timezone.utc)
    end = start - timedelta(days=365 * 3)
    # Go from newest to oldest
    dates = [start - timedelta(days=i) for i in range(0, (start - end).days)]
    scores = [score(date, 1) for date in dates]
    plt.plot(dates, scores)
    plt.gca().invert_xaxis()  # Make the x-axis go backwards (newest on left, oldest on right)
    plt.show()

#graph()
#exit()

# Extract playlist ID from target URL to ignore it during sampling
target_playlist_url = os.getenv('TARGET_PLAYLIST_URL')
ignore_playlist_ids = []
if target_playlist_url and "open.spotify.com/playlist/" in target_playlist_url:
    playlist_id = target_playlist_url.split("open.spotify.com/playlist/")[1].split("?")[0]
    ignore_playlist_ids = [playlist_id]

sampled_tracks = sample_weighted_songs(
    sp,
    n=N,
    cache=cache,
    ttl_seconds=cache_ttl,
    score_function=score,
    temperature=temperature,
    ignore_playlist_ids=ignore_playlist_ids
)




"""
playlist_ids = get_all_playlist_ids(sp, cache=cache, ttl_seconds=cache_ttl, ignore_playlist_ids=[os.getenv('TARGET_PLAYLIST_URL')])
playlists = []
for pid in tqdm(playlist_ids):
    playlist = sp.playlist(pid, fields="name")
    playlists.append(playlist.get("name"))
    print(playlist.get("name"))


"""

result_track_ids = [t.get('id') for t in sampled_tracks]
print(f"Using configured playlist URL: {target_playlist_url}")
add_tracks_to_playlist_by_url(sp, playlist_url=target_playlist_url, track_ids=result_track_ids)

