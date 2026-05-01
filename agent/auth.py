"""
agent/auth.py

Multi-user Google Sign-In for Hearth.

Flow:
  1. App loads → check st.session_state for logged-in user
  2. If not logged in → show login screen
  3. User clicks "Sign in with Google" → InstalledAppFlow opens browser
  4. After consent, get user info (email, name, user_id) from Google
  5. Store in st.session_state + persist to data/sessions/{user_id}.json
  6. All DB queries scoped to user_id

User ID = md5(email) — stable, short, filesystem-safe.
Token stored at: data/tokens/{user_id}/google_token.json
"""

import hashlib
import json
import os

from google.auth.transport.requests import Request
from google.oauth2.credentials import Credentials
from google_auth_oauthlib.flow import InstalledAppFlow
from googleapiclient.discovery import build

import hearth_config as cfg

SCOPES = [
    "https://www.googleapis.com/auth/gmail.readonly",
    "https://www.googleapis.com/auth/gmail.send",
    "https://www.googleapis.com/auth/calendar",
    "https://www.googleapis.com/auth/userinfo.email",
    "https://www.googleapis.com/auth/userinfo.profile",
    "openid",
]


# ── User ID helpers ───────────────────────────────────────────────────────────

def user_id_from_email(email: str) -> str:
    """Stable short ID from email — used as folder name for tokens/data."""
    return hashlib.md5(email.lower().encode()).hexdigest()[:12]


def token_path(user_id: str) -> str:
    path = os.path.join(cfg.DATA_DIR, "tokens", user_id)
    os.makedirs(path, exist_ok=True)
    return os.path.join(path, "google_token.json")


def sessions_path() -> str:
    path = os.path.join(cfg.DATA_DIR, "sessions")
    os.makedirs(path, exist_ok=True)
    return path


# ── Session persistence ────────────────────────────────────────────────────────

def save_session(user: dict):
    """Persist user info so returning users skip the OAuth flow."""
    path = os.path.join(sessions_path(), f"{user['user_id']}.json")
    with open(path, "w") as f:
        json.dump(user, f)


def load_session(user_id: str) -> dict | None:
    path = os.path.join(sessions_path(), f"{user_id}.json")
    if not os.path.exists(path):
        return None
    with open(path) as f:
        return json.load(f)


def list_sessions() -> list[dict]:
    """Return all saved user sessions — for the 'switch account' UI."""
    sdir = sessions_path()
    users = []
    for fname in os.listdir(sdir):
        if fname.endswith(".json"):
            try:
                with open(os.path.join(sdir, fname)) as f:
                    users.append(json.load(f))
            except Exception:
                continue
    return sorted(users, key=lambda u: u.get("name",""))


# ── Google OAuth ──────────────────────────────────────────────────────────────

def _get_user_info(creds: Credentials) -> dict:
    """Fetch name + email from Google userinfo endpoint."""
    service = build("oauth2", "v2", credentials=creds)
    info    = service.userinfo().get().execute()
    email   = info.get("email","")
    return {
        "email":   email,
        "name":    info.get("name", email),
        "picture": info.get("picture",""),
        "user_id": user_id_from_email(email),
    }


def sign_in_with_google() -> dict | None:
    """
    Run the OAuth flow. Opens a browser window once.
    Returns user dict or None on failure.
    """
    if not os.path.exists(cfg.GOOGLE_CREDS):
        raise FileNotFoundError(
            f"Missing {cfg.GOOGLE_CREDS}\n"
            "Download from Google Cloud Console → Credentials → Hearth → Download JSON"
        )

    flow  = InstalledAppFlow.from_client_secrets_file(cfg.GOOGLE_CREDS, SCOPES)
    creds = flow.run_local_server(port=0)

    user  = _get_user_info(creds)
    tpath = token_path(user["user_id"])

    # Save token scoped to this user
    with open(tpath, "w") as f:
        f.write(creds.to_json())

    save_session(user)
    return user


def get_credentials(user_id: str) -> Credentials | None:
    """
    Return valid credentials for a user, refreshing if needed.
    Returns None if not authenticated.
    """
    tpath = token_path(user_id)
    if not os.path.exists(tpath):
        return None
    try:
        creds = Credentials.from_authorized_user_file(tpath, SCOPES)
        if creds and creds.expired and creds.refresh_token:
            creds.refresh(Request())
            with open(tpath, "w") as f:
                f.write(creds.to_json())
        return creds if creds and creds.valid else None
    except Exception:
        return None


def is_user_authenticated(user_id: str) -> bool:
    return get_credentials(user_id) is not None


def sign_out(user_id: str):
    """Remove token + session for this user."""
    try:
        os.remove(token_path(user_id))
    except FileNotFoundError:
        pass
    try:
        os.remove(os.path.join(sessions_path(), f"{user_id}.json"))
    except FileNotFoundError:
        pass

def is_credentials_configured() -> bool:
    """Check if the Google OAuth credentials file exists."""
    return os.path.exists(cfg.GOOGLE_CREDS)
