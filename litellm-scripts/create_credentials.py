import urllib.request
import json
import os
from load_dotenv import load_dotenv


load_dotenv()


LITELLM_API_KEY = os.environ["LITELLM_API_KEY"]
LITELLM_BASE_URL = os.environ["LITELLM_BASE_URL"]
CLI_PROXY_API_CREDENTIAL_SERVICE_NAME = os.environ["CLI_PROXY_API_CREDENTIAL_SERVICE_NAME"]
CLI_PROXY_API_KEY = os.environ["CLI_PROXY_API_KEY"]


def post_credential(data):
    url = f"{LITELLM_BASE_URL}/credentials"

    headers = {
        "Authorization": "Bearer " + LITELLM_API_KEY,
        "Content-Type": "application/json",
    }

    req = urllib.request.Request(url, data=json.dumps(data).encode(), headers=headers)

    try:
        with urllib.request.urlopen(req) as res:
            return res.read().decode()
    except Exception as e:
        return str(e)


print(
    post_credential(
        {
            "credential_name": f"{CLI_PROXY_API_CREDENTIAL_SERVICE_NAME}-gemini",
            "credential_values": {
                "api_key": CLI_PROXY_API_KEY,
                "api_base": f"http://{CLI_PROXY_API_CREDENTIAL_SERVICE_NAME}:8317/v1beta",
            },
            "credential_info": {"custom_llm_provider": "Google_AI_Studio"},
        }
    )
)

print(
    post_credential(
        {
            "credential_name": f"{CLI_PROXY_API_CREDENTIAL_SERVICE_NAME}-anthropic",
            "credential_values": {
                "api_key": CLI_PROXY_API_KEY,
                "api_base": f"http://{CLI_PROXY_API_CREDENTIAL_SERVICE_NAME}:8317",
            },
            "credential_info": {"custom_llm_provider": "Anthropic"},
        }
    )
)
