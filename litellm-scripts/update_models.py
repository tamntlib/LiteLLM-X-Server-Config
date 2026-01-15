import urllib.request
import urllib.error
import json
import os
from datetime import datetime, timezone
from load_dotenv import load_dotenv


load_dotenv()


LITELLM_API_KEY = os.environ["LITELLM_API_KEY"]
LITELLM_BASE_URL = os.environ["LITELLM_BASE_URL"]
LITELLM_ACTOR = os.environ["LITELLM_ACTOR"]
DRY_RUN = os.getenv("DRY_RUN", "false").lower() == "true"


# Fetch model prices from LiteLLM
LITELLM_PRICES_URL = "https://raw.githubusercontent.com/BerriAI/litellm/refs/heads/main/model_prices_and_context_window.json"


def fetch_model_prices():
    """Fetch model prices from LiteLLM GitHub."""
    try:
        with urllib.request.urlopen(LITELLM_PRICES_URL) as response:
            return json.loads(response.read().decode("utf-8"))
    except Exception as e:
        print(f"⚠️ Failed to fetch model prices: {e}")
        return {}


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


def find_model_price(model_id, prices):
    """Find model price data, trying variations if not found."""
    candidates = [model_id]

    # Try removing "gemini-" prefix (for models like gemini-claude-sonnet-4-5)
    if model_id.startswith("gemini-"):
        candidates.append(model_id[7:])

    # Try removing "-thinking" suffix
    if model_id.endswith("-thinking"):
        stripped = model_id[:-9]
        candidates.append(stripped)
        if stripped.startswith("gemini-"):
            candidates.append(stripped[7:])

    # Try removing "-medium" suffix
    if model_id.endswith("-medium"):
        stripped = model_id[:-7]
        candidates.append(stripped)
        if stripped.startswith("gemini-"):
            candidates.append(stripped[7:])

    # Try exact match first for all candidates
    for candidate in candidates:
        if candidate in prices:
            prices[candidate]["model_id"] = candidate
            return prices[candidate]

    # Try partial match - find keys that contain the candidate
    for candidate in candidates:
        for key in prices:
            if candidate in key:
                prices[key]["model_id"] = candidate
                return prices[key]

    return None


def extract_price_fields(price_data):
    """Extract only price-related fields from model data."""
    if not price_data:
        return {}

    price_keys = [
        "input_cost_per_token",
        "output_cost_per_token",
        "input_cost_per_audio_token",
        "output_cost_per_audio_token",
        "input_cost_per_image",
        "output_cost_per_image",
        "input_cost_per_video_per_second",
        "output_cost_per_video_per_second",
        "cache_creation_input_token_cost",
        "cache_read_input_token_cost",
        "output_cost_per_reasoning_token",
        "input_cost_per_token_above_200k_tokens",
        "output_cost_per_token_above_200k_tokens",
    ]

    return {k: v for k, v in price_data.items() if k in price_keys}


def update_model(model_data, price_fields):
    """Update a model with new price fields."""
    model_name = model_data.get("model_name")
    model_info = model_data.get("model_info", {})
    model_id = model_info.get("id")

    if not model_id:
        print(f"⚠️ No model ID found for {model_name}")
        return

    now_iso_string = (
        datetime.now(timezone.utc)
        .isoformat(timespec="milliseconds")
        .replace("+00:00", "Z")
    )

    # Update model_info with new price fields
    updated_model_info = model_info.copy()
    updated_model_info.update(price_fields)
    updated_model_info.update(
        {
            "updated_at": now_iso_string,
            "updated_by": LITELLM_ACTOR,
        }
    )

    # Build payload
    payload = {
        "model_name": model_name,
        "litellm_params": model_data.get("litellm_params", {}),
        "model_info": updated_model_info,
    }

    if DRY_RUN:
        print(f"🔍 [DRY RUN] Would update model: {model_name} (id: {model_id})")
        print(f"   Price fields: {json.dumps(price_fields, indent=2)}")
        return

    # Convert dict to JSON string and then to bytes
    data = json.dumps(payload).encode("utf-8")

    # Setup the Request object
    req = urllib.request.Request(
        f"{LITELLM_BASE_URL}/model/{model_id}/update", data=data, method="PATCH"
    )

    # Add Headers
    req.add_header("Authorization", f"Bearer {LITELLM_API_KEY}")
    req.add_header("Content-Type", "application/json")
    req.add_header("Accept", "*/*")

    try:
        with urllib.request.urlopen(req) as response:
            status = response.getcode()
            print(f"✅ Updated: {model_name} | Status: {status}")
    except urllib.error.HTTPError as e:
        error_body = e.read().decode("utf-8")
        print(f"❌ Failed: {model_name} | Error {e.code}: {error_body}")
    except Exception as e:
        print(f"❌ Network Error: {e}")


# Fetch prices and existing models at startup
print("📥 Fetching model prices from LiteLLM...")
MODEL_PRICES = fetch_model_prices()
print(f"   Found {len(MODEL_PRICES)} model prices")

print("📥 Fetching existing models...")
EXISTING_MODELS = fetch_existing_models()
print(f"   Found {len(EXISTING_MODELS)} existing models")

# Update each model
for model_data in EXISTING_MODELS:
    model_name = model_data.get("model_name", "")

    # Get price data for the model
    price_data = find_model_price(model_name, MODEL_PRICES)
    price_fields = extract_price_fields(price_data)

    if price_fields:
        print(
            f"💰 Found pricing for {model_name}: {price_data.get('model_id', 'N/A')}"
        )
        update_model(model_data, price_fields)
    else:
        print(f"⚠️ No pricing found for {model_name}, skipping")
