import urllib.request
import urllib.error
import json
import os
from load_dotenv import load_dotenv

load_dotenv()

LITELLM_API_KEY = os.environ["LITELLM_API_KEY"]
LITELLM_BASE_URL = os.environ["LITELLM_BASE_URL"]
DRY_RUN = os.getenv("DRY_RUN", "false").lower() == "true"

ALIASES = {
    "claude-opus-4-5": "anthropic/gemini-claude-opus-4-5-thinking",
    "claude-haiku-4-5": "anthropic/gemini-claude-sonnet-4-5",
    "claude-sonnet-4-5": "anthropic/gemini-claude-sonnet-4-5-thinking",
    "claude-opus-4-5-20251101": "anthropic/gemini-claude-opus-4-5-thinking",
    "claude-haiku-4-5-20251001": "anthropic/gemini-claude-sonnet-4-5",
    "claude-sonnet-4-5-20250929": "anthropic/gemini-claude-sonnet-4-5-thinking",
}


def update_model_group_aliases():
    payload = {"router_settings": {"model_group_alias": ALIASES}}

    if DRY_RUN:
        print("🔍 [DRY RUN] Would update config with aliases:")
        print(f"   Payload: {json.dumps(payload, indent=2)}")
        return

    data = json.dumps(payload).encode("utf-8")

    req = urllib.request.Request(
        f"{LITELLM_BASE_URL}/config/update", data=data, method="POST"
    )

    req.add_header("Authorization", f"Bearer {LITELLM_API_KEY}")
    req.add_header("Content-Type", "application/json")
    req.add_header("Accept", "*/*")

    try:
        with urllib.request.urlopen(req) as response:
            status = response.getcode()
            body = response.read().decode("utf-8")
            print(f"✅ Success: Updated model group aliases | Status: {status}")
            print(f"Response: {body}")
    except urllib.error.HTTPError as e:
        error_body = e.read().decode("utf-8")
        print(f"❌ Failed to update aliases | Error {e.code}: {error_body}")
    except Exception as e:
        print(f"❌ Network Error: {e}")


if __name__ == "__main__":
    update_model_group_aliases()
