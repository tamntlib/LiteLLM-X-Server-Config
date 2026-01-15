import urllib.request
import urllib.error
import json
import os
from load_dotenv import load_dotenv


load_dotenv()


LITELLM_API_KEY = os.environ["LITELLM_API_KEY"]
LITELLM_BASE_URL = os.environ["LITELLM_BASE_URL"]
DRY_RUN = os.getenv("DRY_RUN", "false").lower() == "true"


def fetch_existing_models():
    """Fetch existing models from LiteLLM API."""
    url = f"{LITELLM_BASE_URL}/v2/model/info?include_team_models=true"
    req = urllib.request.Request(url, method="GET")
    req.add_header("Authorization", f"Bearer {LITELLM_API_KEY}")
    req.add_header("Accept", "application/json")

    try:
        with urllib.request.urlopen(req) as response:
            data = json.loads(response.read().decode("utf-8"))
            return data.get("data", [])
    except Exception as e:
        print(f"⚠️ Failed to fetch existing models: {e}")
        return []


def delete_model(model_data):
    """Delete a model by ID."""
    model_name = model_data.get("model_name")
    model_info = model_data.get("model_info", {})
    model_id = model_info.get("id")

    if not model_id:
        print(f"⚠️ No model ID found for {model_name}")
        return

    if DRY_RUN:
        print(f"🔍 [DRY RUN] Would delete model: {model_name} (id: {model_id})")
        return

    # Build payload
    payload = {"id": model_id}

    # Convert dict to JSON string and then to bytes
    data = json.dumps(payload).encode("utf-8")

    # Setup the Request object
    req = urllib.request.Request(
        f"{LITELLM_BASE_URL}/model/delete", data=data, method="POST"
    )

    # Add Headers
    req.add_header("Authorization", f"Bearer {LITELLM_API_KEY}")
    req.add_header("Content-Type", "application/json")
    req.add_header("Accept", "*/*")

    try:
        with urllib.request.urlopen(req) as response:
            status = response.getcode()
            print(f"✅ Deleted: {model_name} (id: {model_id}) | Status: {status}")
    except urllib.error.HTTPError as e:
        error_body = e.read().decode("utf-8")
        print(f"❌ Failed: {model_name} | Error {e.code}: {error_body}")
    except Exception as e:
        print(f"❌ Network Error: {e}")


# Fetch existing models
print("📥 Fetching existing models...")
EXISTING_MODELS = fetch_existing_models()
print(f"   Found {len(EXISTING_MODELS)} existing models")

if not DRY_RUN:
    print("⚠️ WARNING: This will DELETE ALL models!")
    confirm = input("Type 'y' to confirm: ")
    if confirm.lower() != "y":
        print("❌ Aborted.")
        exit(1)

# Delete each model
for model_data in EXISTING_MODELS:
    model_name = model_data.get("model_name", "")
    print(f"🗑️ Deleting: {model_name}")
    delete_model(model_data)

print("✅ Done.")
