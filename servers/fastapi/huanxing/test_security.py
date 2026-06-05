"""唤星 sidecar 安全闸单测（设计 doc12 §11.1）。

构建最小 FastAPI app 装配 `apply_huanxing_cors` + `apply_huanxing_sidecar_guard`，用
`TestClient` 验证 Host loopback 闸、token 闸、CORS 收敛。token 在装配时一次性从 env 读定，
故每个用例先设/清 env 再建 app。
"""

from __future__ import annotations

import os

import pytest
from fastapi import FastAPI
from fastapi.testclient import TestClient

from huanxing.security import (
    SIDECAR_TOKEN_ENV,
    SIDECAR_TOKEN_HEADER,
    apply_huanxing_cors,
    apply_huanxing_sidecar_guard,
)


def _build_client(token: str | None) -> TestClient:
    if token is None:
        os.environ.pop(SIDECAR_TOKEN_ENV, None)
    else:
        os.environ[SIDECAR_TOKEN_ENV] = token

    app = FastAPI()

    @app.get("/ping")
    def ping() -> dict[str, bool]:
        return {"ok": True}

    apply_huanxing_cors(app)
    apply_huanxing_sidecar_guard(app)
    # base_url 决定默认 Host 头；用回环以默认通过 Host 闸。
    return TestClient(app, base_url="http://127.0.0.1:41001")


def test_loopback_host_with_correct_token_passes() -> None:
    client = _build_client("secret-token")
    resp = client.get("/ping", headers={SIDECAR_TOKEN_HEADER: "secret-token"})
    assert resp.status_code == 200
    assert resp.json() == {"ok": True}


def test_missing_token_rejected_when_token_required() -> None:
    client = _build_client("secret-token")
    resp = client.get("/ping")  # 无 token 头
    assert resp.status_code == 403
    assert "sidecar token" in resp.json()["detail"]


def test_wrong_token_rejected() -> None:
    client = _build_client("secret-token")
    resp = client.get("/ping", headers={SIDECAR_TOKEN_HEADER: "WRONG"})
    assert resp.status_code == 403


@pytest.mark.parametrize("evil_host", ["evil.example", "attacker.com:41001", "169.254.1.1"])
def test_non_loopback_host_rejected_even_with_token(evil_host: str) -> None:
    client = _build_client("secret-token")
    # 即便带对的 token，非回环 Host 也先被 Host 闸拒（防 DNS rebinding）。
    resp = client.get(
        "/ping",
        headers={"host": evil_host, SIDECAR_TOKEN_HEADER: "secret-token"},
    )
    assert resp.status_code == 403
    assert "Host" in resp.json()["detail"]


def test_localhost_and_ipv6_loopback_allowed() -> None:
    client = _build_client(None)  # 无 token（P0 standalone）→ 只剩 Host 闸
    for host in ("localhost", "localhost:41001", "[::1]:41001", "127.0.0.1"):
        resp = client.get("/ping", headers={"host": host})
        assert resp.status_code == 200, f"loopback host {host} 应放行"


def test_no_token_env_runs_host_gate_only() -> None:
    client = _build_client(None)  # P0 standalone：HX_SIDECAR_TOKEN 缺省
    # 回环 Host 无需 token 即放行。
    assert client.get("/ping").status_code == 200


def test_cors_does_not_echo_wildcard_with_credentials() -> None:
    client = _build_client(None)
    resp = client.get(
        "/ping",
        headers={"host": "127.0.0.1", "origin": "http://evil.example"},
    )
    # 收敛后不再回 `access-control-allow-origin: *` + 凭据组合（embedded 空白名单不放行跨源）。
    acao = resp.headers.get("access-control-allow-origin")
    assert acao != "*"
    assert resp.headers.get("access-control-allow-credentials") != "true"
