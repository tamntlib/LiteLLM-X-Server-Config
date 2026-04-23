import json
import urllib.error
import urllib.request


HTTP_USER_AGENT = "LiteLLM-X-Server-Config/1.0"


def build_request(url, *, data=None, headers=None, method=None):
    request_headers = {"User-Agent": HTTP_USER_AGENT}
    if headers:
        request_headers.update(headers)
    return urllib.request.Request(
        url,
        data=data,
        headers=request_headers,
        method=method,
    )


def read_http_error_body(error, limit=200):
    try:
        return error.read(limit).decode("utf-8", "replace").strip()
    except Exception:
        return ""


def format_http_error(error, *, body_limit=200):
    details = f"HTTP Error {error.code}: {error.reason}"
    body_preview = read_http_error_body(error, limit=body_limit)
    if body_preview:
        details = f"{details} | Body: {body_preview}"
    return details


def request_json(url, *, data=None, headers=None, method=None, timeout=None):
    req = build_request(
        url,
        data=data,
        headers=headers,
        method=method,
    )
    with urllib.request.urlopen(req, timeout=timeout) as response:
        return json.loads(response.read().decode("utf-8"))
