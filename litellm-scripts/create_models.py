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
LITELLM_MODEL_ACCESS_GROUPS = [
    x.strip()
    for x in os.getenv("LITELLM_MODEL_ACCESS_GROUPS", "").split(",")
    if x.strip()
]
CLI_PROXY_API_CREDENTIAL_SERVICE_NAME = os.environ[
    "CLI_PROXY_API_CREDENTIAL_SERVICE_NAME"
]
DRY_RUN = os.getenv("DRY_RUN", "false").lower() == "true"

interfaces = ["openai", "gemini", "anthropic"]

# Your list of models
models_to_add = [
    "gpt-oss-120b-medium",
    "gemini-claude-sonnet-4-5",
    "gemini-claude-sonnet-4-5-thinking",
    "gemini-claude-opus-4-5-thinking",
    "gemini-3-pro-image-preview",
    "gemini-2.5-flash",
    "gemini-2.5-flash-lite",
    "gemini-2.5-computer-use-preview-10-2025",
    "gemini-3-pro-preview",
    "gemini-3-flash-preview",
]

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


# Fetch prices at startup
MODEL_PRICES = fetch_model_prices()


def add_model(interface, model_id):
    # Get price data for the model
    price_data = find_model_price(model_id, MODEL_PRICES)
    price_fields = extract_price_fields(price_data)

    if price_fields:
        print(
            f"💰 Found pricing for {model_id}: {price_data['model_id']} {price_fields}"
        )
    else:
        print(f"⚠️ No pricing found for {model_id}")

    now_iso_string = (
        datetime.now(timezone.utc)
        .isoformat(timespec="milliseconds")
        .replace("+00:00", "Z")
    )

    # Prepare the JSON payload
    model_info = (
        {"access_groups": LITELLM_MODEL_ACCESS_GROUPS}
        if LITELLM_MODEL_ACCESS_GROUPS
        else {}
    )
    model_info.update(price_fields)
    model_info.update(
        {
            "updated_at": now_iso_string,
            "updated_by": LITELLM_ACTOR,
            "created_at": now_iso_string,
            "created_by": LITELLM_ACTOR,
        }
    )

    payload = {
        "model_name": f"{interface}/{model_id}",
        "litellm_params": {
            "model": f"{interface}/{model_id}",
            "custom_llm_provider": f"{interface}",
            "litellm_credential_name": f"{CLI_PROXY_API_CREDENTIAL_SERVICE_NAME}-{interface}",
        },
        "model_info": model_info,
    }

    if DRY_RUN:
        print(f"🔍 [DRY RUN] Would create model: {model_id}")
        print(f"   Payload: {json.dumps(payload, indent=2)}")
        return

    # Convert dict to JSON string and then to bytes
    data = json.dumps(payload).encode("utf-8")

    # Setup the Request object
    req = urllib.request.Request(
        f"{LITELLM_BASE_URL}/model/new", data=data, method="POST"
    )

    # Add Headers
    req.add_header("Authorization", f"Bearer {LITELLM_API_KEY}")
    req.add_header("Content-Type", "application/json")
    req.add_header("Accept", "*/*")

    try:
        with urllib.request.urlopen(req) as response:
            status = response.getcode()
            body = response.read().decode("utf-8")
            print(f"✅ Success: {model_id} | Status: {status}")
    except urllib.error.HTTPError as e:
        error_body = e.read().decode("utf-8")
        print(f"❌ Failed: {model_id} | Error {e.code}: {error_body}")
    except Exception as e:
        print(f"❌ Network Error: {e}")


if __name__ == "__main__":
    # Execute loop
    for interface in interfaces:
        for model in models_to_add:
            add_model(interface, model)
