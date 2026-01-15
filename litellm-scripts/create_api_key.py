#!/usr/bin/env python3
import argparse
import urllib.request
import urllib.parse
import urllib.error
import json
import os
import sys
from load_dotenv import load_dotenv


load_dotenv()


LITELLM_API_KEY = os.environ["LITELLM_API_KEY"]
LITELLM_BASE_URL = os.environ["LITELLM_BASE_URL"]


def get_user_by_email(email):
    """Get user by email. Returns user data or None if not found."""
    # user_email is a partial match filter, so we need to check exact match
    url = f"{LITELLM_BASE_URL}/user/list?user_email={urllib.parse.quote(email)}&page=1&page_size=100"
    req = urllib.request.Request(url, method="GET")
    req.add_header("Authorization", f"Bearer {LITELLM_API_KEY}")
    req.add_header("Accept", "application/json")

    try:
        with urllib.request.urlopen(req) as response:
            data = json.loads(response.read().decode("utf-8"))
            users = data.get("users", [])
            for user in users:
                if user.get("user_email") == email:
                    return user
            return None
    except urllib.error.HTTPError as e:
        if e.code == 404:
            return None
        error_body = e.read().decode("utf-8")
        print(f"⚠️ Error checking user: {e.code}: {error_body}", file=sys.stderr)
        return None
    except Exception as e:
        print(f"⚠️ Error checking user: {e}", file=sys.stderr)
        return None


def create_user(email):
    """Create a new user with the given email."""
    payload = {
        "user_id": None,
        "user_email": email,
        "user_role": "internal_user_viewer",
        "models": ["General"],
        "auto_create_key": False,
    }

    data = json.dumps(payload).encode("utf-8")

    req = urllib.request.Request(
        f"{LITELLM_BASE_URL}/user/new", data=data, method="POST"
    )
    req.add_header("Authorization", f"Bearer {LITELLM_API_KEY}")
    req.add_header("Content-Type", "application/json")
    req.add_header("Accept", "*/*")

    try:
        with urllib.request.urlopen(req) as response:
            return json.loads(response.read().decode("utf-8"))
    except urllib.error.HTTPError as e:
        error_body = e.read().decode("utf-8")
        print(f"❌ Failed to create user: {e.code}: {error_body}", file=sys.stderr)
        sys.exit(1)


def create_api_key(user_id, key_alias):
    """Create an API key for the given user."""
    payload = {
        "user_id": user_id,
        "team_id": None,
        "key_alias": key_alias,
        "models": ["all-team-models"],
        "key_type": "llm_api",
        "metadata": {},
    }

    data = json.dumps(payload).encode("utf-8")

    req = urllib.request.Request(
        f"{LITELLM_BASE_URL}/key/generate", data=data, method="POST"
    )
    req.add_header("Authorization", f"Bearer {LITELLM_API_KEY}")
    req.add_header("Content-Type", "application/json")
    req.add_header("Accept", "*/*")

    try:
        with urllib.request.urlopen(req) as response:
            return json.loads(response.read().decode("utf-8"))
    except urllib.error.HTTPError as e:
        error_body = e.read().decode("utf-8")
        print(f"❌ Failed to create API key: {e.code}: {error_body}", file=sys.stderr)
        sys.exit(1)


def main():
    parser = argparse.ArgumentParser(description="Create LiteLLM user and API key")
    parser.add_argument("email", help="User email address")
    parser.add_argument("--alias", "-a", help="API key alias (default: email prefix)")
    args = parser.parse_args()

    email = args.email
    key_alias = args.alias or email.split("@")[0]

    print(f"📧 Processing: {email}", file=sys.stderr)

    # Check if user exists
    user = get_user_by_email(email)

    if user:
        user_id = user.get("user_id")
        print(f"👤 User already exists: {user_id}", file=sys.stderr)
    else:
        print(f"👤 Creating new user...", file=sys.stderr)
        result = create_user(email)
        user_id = result.get("user_id")
        print(f"✅ User created: {user_id}", file=sys.stderr)

    # Create API key
    print(f"🔑 Creating API key...", file=sys.stderr)
    key_result = create_api_key(user_id, key_alias)
    api_key = key_result.get("key")

    print(f"✅ API key created", file=sys.stderr)
    print(api_key)


if __name__ == "__main__":
    main()
