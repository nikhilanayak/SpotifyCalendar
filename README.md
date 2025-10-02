# SpotifyCalendar

A Python application for interacting with Spotify playlists using the Spotify Web API.

## Setup

1. Install dependencies:
   ```bash
   pip install -r requirements.txt
   ```

2. Create a Spotify app at [Spotify Developer Dashboard](https://developer.spotify.com/dashboard)

3. Copy the example configuration file:
   ```bash
   cp config.env.example config.env
   ```

4. Edit `config.env` and add your Spotify app credentials:
   ```
   SPOTIFY_CLIENT_ID=your_client_id_here
   SPOTIFY_CLIENT_SECRET=your_client_secret_here
   SPOTIFY_REDIRECT_URI=http://localhost:8888/callback
   ```

5. Run the application:
   ```bash
   python main.py
   ```

## Features

- Fetch all playlist IDs for the current user
- Get all track IDs for a specific playlist
- SQLite-based TTL caching for improved performance
- Environment-based configuration for secure credential management

## Configuration

The application uses a `config.env` file for configuration. This file is ignored by git to keep your credentials secure.

Available configuration options:
- `SPOTIFY_CLIENT_ID`: Your Spotify app client ID
- `SPOTIFY_CLIENT_SECRET`: Your Spotify app client secret
- `SPOTIFY_REDIRECT_URI`: OAuth redirect URI (default: http://localhost:8888/callback)
- `CACHE_TTL_SECONDS`: Cache time-to-live in seconds (default: 3600)
