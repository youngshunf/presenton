"""唤星 Presenton sidecar 安全加固（设计 doc12 §11.1 / §11.7，构建期可重放补丁）。

`embedded_desktop` 形态下，Presenton sidecar 只应经唤星 daemon 反代访问，绝不直接暴露给
浏览器或其他本机进程。上游默认姿态（已核实 `api/main.py:76-83` + `api/middlewares.py`）是
最宽松的：``allow_origins=["*"] + allow_credentials=True`` 叠加 ``DISABLE_AUTH=true`` 整体放行，
意味着任意恶意网页/进程都能对 ``http://127.0.0.1:<sidecar 动态端口>`` 发起带凭据的跨源读写
（列举/删除/生成/导出所有演示文稿），loopback 绑定也挡不住 DNS rebinding。

本模块在唯一装配点 `api/main.py` 接线三道收敛（不散改上游业务源码）：

1. CORS 收敛（§11.1.1，`apply_huanxing_cors`）：去掉 ``通配 origin + allow_credentials`` 组合；
   embedded 形态浏览器侧永远经 daemon **同源**反代，无需跨源 CORS，默认空白名单。
   ``HX_SIDECAR_ALLOW_ORIGINS``（逗号分隔）可在特殊形态显式给 daemon 反代 origin。
2. Host 头校验（§11.1.2，**无条件常开**）：只接受 loopback Host（``127.0.0.1`` / ``localhost`` /
   ``::1``，任意端口），拒任意域名 → 防 DNS rebinding（恶意域名 A 记录重绑 127.0.0.1 绕过同源）。
3. sidecar 随机 token（§11.1.3，env 存在时强制）：daemon spawn 注入 ``HX_SIDECAR_TOKEN``、反代
   附加 ``X-HX-Sidecar-Token``；缺/错 token → 403 → 绕过 daemon 直连 sidecar 端口被拒。env 缺省
   （P0 standalone）只剩 Host 闸，仍只绑回环。

两闸合为 `HuanxingSidecarGuardMiddleware`，由 `apply_huanxing_sidecar_guard` 在所有上游
middleware 之后装配，使其位于**最外层最先执行**（Starlette 后加先跑），坏请求在进入任何业务
逻辑前即被拒。

零 Mock 零 Fake：闸门命中即返回真实 403 JSON，不放行、不伪造。
"""

from __future__ import annotations

import hmac
import os
from urllib.parse import urlsplit

from fastapi import FastAPI, Request
from starlette.middleware.base import BaseHTTPMiddleware
from starlette.responses import JSONResponse

#: daemon spawn 时注入的 sidecar 随机 token 环境变量（§11.1.3）。
SIDECAR_TOKEN_ENV = "HX_SIDECAR_TOKEN"
#: daemon 反代每次转发附加的 token 请求头（小写，HTTP 头大小写不敏感）。
SIDECAR_TOKEN_HEADER = "x-hx-sidecar-token"
#: 可选的显式跨源白名单（逗号分隔 origin）；缺省=空（embedded 同源无需 CORS）。
ALLOW_ORIGINS_ENV = "HX_SIDECAR_ALLOW_ORIGINS"

#: 允许的 loopback 主机名（`urlsplit` 解析后小写比对）。
_LOOPBACK_HOSTS = frozenset({"127.0.0.1", "localhost", "::1"})


def _host_is_loopback(host_header: str | None) -> bool:
    """判定 Host 头是否指向本机回环（防 DNS rebinding / 远端直连）。

    Host 形如 ``127.0.0.1:41001`` / ``[::1]:41001`` / ``localhost``。借 ``urlsplit`` 统一解析
    出 hostname（IPv6 方括号、端口都剥掉，且 hostname 已小写）后白名单比对。空/缺 Host 视为
    不可信 → 拒（daemon 反代经 `reqwest` 总会带回环 Host）。
    """
    if not host_header:
        return False
    parsed = urlsplit(f"//{host_header.strip()}")
    return parsed.hostname in _LOOPBACK_HOSTS


class HuanxingSidecarGuardMiddleware(BaseHTTPMiddleware):
    """请求级闸门：Host loopback 校验（无条件）+ sidecar token 校验（token 存在时）。

    `expected_token` 在装配时一次性从 env 读定（``CAN_CHANGE_KEYS=false`` 下 spawn env 即权威，
    无需 per-request 重读）。``None`` 表示未注入 token（P0 standalone）→ 只跑 Host 闸。
    """

    def __init__(self, app, *, expected_token: str | None) -> None:
        super().__init__(app)
        self._expected_token = expected_token or None

    async def dispatch(self, request: Request, call_next):
        # ① Host 头校验（无条件常开）：非回环 Host 一律拒，防 DNS rebinding / 远端直连。
        if not _host_is_loopback(request.headers.get("host")):
            return JSONResponse(
                status_code=403,
                content={"detail": "Forbidden: non-loopback Host rejected"},
            )
        # ② sidecar token 校验（仅当 daemon 注入了 HX_SIDECAR_TOKEN）：恒定时比较防时序侧信道；
        #    缺头/错头 → 绕过 daemon 直连 sidecar 端口被拒。
        if self._expected_token is not None:
            presented = request.headers.get(SIDECAR_TOKEN_HEADER, "")
            if not hmac.compare_digest(presented, self._expected_token):
                return JSONResponse(
                    status_code=403,
                    content={"detail": "Forbidden: missing or invalid sidecar token"},
                )
        return await call_next(request)


def apply_huanxing_cors(app: FastAPI) -> None:
    """收敛 CORS（§11.1.1），替代上游 ``allow_origins=["*"] + allow_credentials=True``。

    embedded 同源默认空白名单（浏览器侧经 daemon 同源反代，无跨源）；仅当显式配置
    ``HX_SIDECAR_ALLOW_ORIGINS`` 时才放行对应 origin，且**永不**与 ``allow_credentials=True``
    组合通配。
    """
    from fastapi.middleware.cors import CORSMiddleware

    raw = (os.getenv(ALLOW_ORIGINS_ENV) or "").strip()
    allow_origins = [item.strip() for item in raw.split(",") if item.strip()]
    app.add_middleware(
        CORSMiddleware,
        allow_origins=allow_origins,
        allow_credentials=False,
        allow_methods=["*"] if allow_origins else [],
        allow_headers=["*"] if allow_origins else [],
    )


def apply_huanxing_sidecar_guard(app: FastAPI) -> None:
    """装配 Host + token 闸（§11.1.2/§11.1.3）。

    须在所有上游 middleware **之后**调用，使本闸位于最外层、最先执行（Starlette 后加先跑），
    坏请求在进入任何业务逻辑前即被拒。token 一次性从 env 读定。
    """
    app.add_middleware(
        HuanxingSidecarGuardMiddleware,
        expected_token=os.getenv(SIDECAR_TOKEN_ENV),
    )
