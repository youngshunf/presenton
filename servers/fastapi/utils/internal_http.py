import os

from utils.simple_auth import (
    SESSION_COOKIE_NAME,
    create_session_token,
    get_configured_auth_username,
    get_internal_auth_headers,
)


def internal_app_base_url() -> str:
    """Origin for trusted loopback calls to the Next.js app (schema / template pages).

    唤星 embedded_desktop sidecar 补丁：嵌入式 sidecar 在动态端口跑 Next.js（非 root、无 nginx、
    不占 80 口），因此优先用 spawn 注入的 NEXT_PUBLIC_URL；未设置时回退到历史默认
    http://localhost（Docker / Electron-with-nginx 部署）。去尾斜杠。
    """
    base = os.getenv("NEXT_PUBLIC_URL") or "http://localhost"
    return base.rstrip("/")


def internal_request_headers() -> dict[str, str]:
    """Headers for trusted loopback calls between FastAPI and Next.js."""
    headers = dict(get_internal_auth_headers())
    username = get_configured_auth_username()
    if username and "Cookie" not in headers:
        token = create_session_token(username)
        headers["Cookie"] = f"{SESSION_COOKIE_NAME}={token}"
    return headers
