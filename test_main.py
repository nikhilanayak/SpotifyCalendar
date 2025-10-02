from flask import Flask, request, redirect
import requests
import base64
import os

CLIENT_ID = os.getenv('SPOTIFY_CLIENT_ID')
CLIENT_SECRET = os.getenv('SPOTIFY_CLIENT_SECRET')
REDIRECT_URI = "http://localhost:9998/callback"

app = Flask(__name__)

# Step 1: Login
@app.route("/")
def login():
    scope = "user-read-playback-state user-read-currently-playing user-modify-playback-state streaming"
    auth_url = (
        "https://accounts.spotify.com/authorize"
        f"?client_id={CLIENT_ID}"
        "&response_type=code"
        f"&redirect_uri={REDIRECT_URI}"
        f"&scope={scope}"
    )
    return f'<a href="{auth_url}">Log in with Spotify</a>'

# Step 2: Callback
@app.route("/callback")
def callback():
    code = request.args.get("code")

    token_url = "https://accounts.spotify.com/api/token"
    auth_header = base64.b64encode(f"{CLIENT_ID}:{CLIENT_SECRET}".encode()).decode()
    headers = {"Authorization": f"Basic {auth_header}"}
    data = {
        "grant_type": "authorization_code",
        "code": code,
        "redirect_uri": REDIRECT_URI,
    }

    r = requests.post(token_url, data=data, headers=headers)
    tokens = r.json()
    return tokens  # contains access_token + refresh_token

if __name__ == "__main__":
    app.run(port=9998)
