import os

from fastapi import HTTPException

from litellm.caching.caching import DualCache
from litellm.integrations.custom_logger import CustomLogger
from litellm.proxy._types import UserAPIKeyAuth
from litellm.types.utils import CallTypesLiteral


class ClaudeCodeHook(CustomLogger):
    @staticmethod
    def _parse_version(version_str: str):
        parts = version_str.split(".")
        if len(parts) != 3 or not all(part.isdigit() for part in parts):
            return None
        return tuple(int(part) for part in parts)

    @staticmethod
    def _get_configured_models() -> set[str]:
        models_env = os.getenv("CLAUDE_CODE_MODELS", "")
        if not models_env.strip():
            return set()
        return {model.strip() for model in models_env.split(",") if model.strip()}

    async def async_pre_call_hook(
        self,
        user_api_key_dict: UserAPIKeyAuth,
        cache: DualCache,
        data: dict,
        call_type: CallTypesLiteral,
    ):
        model_name = data.get("model")
        configured_models = self._get_configured_models()
        min_version_str = os.getenv("CLAUDE_CODE_MIN_VERSION")

        if not model_name or model_name not in configured_models or not min_version_str:
            return data

        metadata = data.get("metadata") or {}
        litellm_metadata = data.get("litellm_metadata") or {}
        proxy_server_request = data.get("proxy_server_request") or {}
        headers = proxy_server_request.get("headers") or {}

        user_agent = (
            metadata.get("user_agent")
            or litellm_metadata.get("user_agent")
            or headers.get("user-agent")
            or headers.get("User-Agent")
            or ""
        ).strip()

        if not user_agent:
            raise HTTPException(status_code=403, detail="Missing User-Agent header")

        ua_lower = user_agent.lower()
        marker = "claude-cli/"
        idx = ua_lower.find(marker)

        if idx == -1:
            raise HTTPException(status_code=403, detail="Only Claude Code is allowed")

        version_token = user_agent[idx + len(marker) :].split()[0].strip()
        numeric_version = version_token.split("-")[0].split("+")[0].strip()

        current = self._parse_version(numeric_version)
        minimum = self._parse_version(min_version_str)

        if current is None:
            raise HTTPException(
                status_code=400,
                detail="Invalid Claude Code version",
            )

        if minimum is None:
            raise HTTPException(
                status_code=500,
                detail=f"Invalid CLAUDE_CODE_MIN_VERSION env var: {min_version_str}",
            )

        if current < minimum:
            raise HTTPException(
                status_code=400,
                detail=f"Claude Code version must be >= {min_version_str}",
            )

        return data


proxy_handler_instance = ClaudeCodeHook()
