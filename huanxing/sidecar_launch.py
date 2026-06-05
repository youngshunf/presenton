#!/usr/bin/env python3
"""唤星 Presenton sidecar 启动器（P0 活体验证工具 + P2 sidecar 管理器规格）.

本脚本以 **embedded_desktop** 形态在本地直跑 Presenton（非 Docker、非 Electron），
把 LLM/图像统一指向唤星 new-api 网关（D1/D4），并复用 Presenton 打包产物里的
导出运行时（D2）。它有两个用途：

1. P0 活体验证：手动起 sidecar，跑通 generate→逐页配图→导出 PPTX/PDF（零 Mock 零 Fake）。
2. P2 规格：daemon 端 Rust sidecar 管理器（hasn-shell-boundary EmbeddedAppSidecar）
   将复刻此处的「端口预分配 / 完整 env 注入 / 先备好 NEXT_PUBLIC_URL / HTTP 探活 / tree-kill」逻辑。

env / 路径事实源：external/presenton/electron/app/main.ts:299-394 + utils/servers.ts。
配置读取事实源：servers/fastapi/utils/get_env.py（全部 os.getenv）；
  CAN_CHANGE_KEYS=false 关掉 per-request 从 userConfig 重载（middlewares.py），spawn env 即权威。

零 Mock 零 Fake：任一前置缺失（new-api 不可达 / 二进制缺失 / 导出失败）直接报错退出，不伪造。
"""

from __future__ import annotations

import argparse
import contextlib
import json
import os
import signal
import socket
import sqlite3
import subprocess
import sys
import time
import urllib.error
import urllib.request
from dataclasses import dataclass, field
from pathlib import Path

HASN_HOME = Path(os.path.expanduser("~/.hasn"))
DEFAULT_APP = (
    "/Applications/Presenton Open Source.app/Contents/Resources/app/resources"
)
READY_TIMEOUT_S = 120


# ---------------------------------------------------------------------------
# owner LLM 凭据（new-api 网关）解析：D1 走 owner_llm_credentials + owner 配额
# ---------------------------------------------------------------------------
@dataclass(frozen=True)
class LlmCredential:
    owner_id: str
    base_url: str   # 已归一到 .../v1
    token: str
    chat_model: str
    image_model: str


def _active_owner_dir() -> Path:
    accounts = HASN_HOME / "accounts.json"
    if not accounts.exists():
        raise SystemExit(f"[FATAL] 找不到 {accounts}（daemon 未初始化？）")
    data = json.loads(accounts.read_text())
    phone = data.get("last_active_phone")
    if not phone:
        raise SystemExit("[FATAL] accounts.json 无 last_active_phone")
    owner_dir = HASN_HOME / phone
    if not (owner_dir / "hasn_db.sqlite").exists():
        raise SystemExit(f"[FATAL] 活跃 owner {phone} 无 hasn_db.sqlite")
    return owner_dir


def _normalize_v1(base: str) -> str:
    base = base.rstrip("/")
    return base if base.endswith("/v1") else base + "/v1"


def resolve_credential(image_model: str) -> LlmCredential:
    owner_dir = _active_owner_dir()
    db = owner_dir / "hasn_db.sqlite"
    conn = sqlite3.connect(f"file:{db}?mode=ro", uri=True)
    try:
        row = conn.execute(
            "SELECT owner_id, llm_token, llm_base_url, llm_model "
            "FROM owner_llm_credentials LIMIT 1"
        ).fetchone()
    finally:
        conn.close()
    if not row:
        raise SystemExit(
            f"[FATAL] {db} owner_llm_credentials 为空（owner 未配置 new-api 网关？）"
        )
    owner_id, token, base_url, chat_model = row
    base_url = os.environ.get("HX_NEWAPI_BASE_URL", base_url)
    token = os.environ.get("HX_NEWAPI_TOKEN", token)
    chat_model = os.environ.get("HX_CHAT_MODEL", chat_model or "gpt-5.5")
    return LlmCredential(
        owner_id=owner_id,
        base_url=_normalize_v1(base_url),
        token=token,
        chat_model=chat_model,
        image_model=image_model,
    )


# ---------------------------------------------------------------------------
# 打包产物 + 导出运行时路径解析（D2：复用已安装 app 的产物，不重新打包）
# ---------------------------------------------------------------------------
@dataclass(frozen=True)
class Artifacts:
    fastapi_bin: Path
    fastapi_dir: Path
    nextjs_server: Path
    nextjs_cwd: Path
    export_root: Path
    soffice: str | None
    imagemagick: str | None
    convert_module: Path | None
    node_bin: str
    chrome_bin: str | None  # 导出运行时 Puppeteer 用（D2：复用已装 Chrome，跳过钉版下载）


def _first_existing(paths: list[str]) -> str | None:
    for p in paths:
        if p and Path(p).exists():
            return p
    return None


def resolve_artifacts(app_resources: Path) -> Artifacts:
    fastapi_bin = app_resources / "fastapi" / "fastapi"
    nextjs_server = app_resources / "nextjs" / "servers" / "nextjs" / "server.js"
    export_root = app_resources / "export"
    missing = [p for p in (fastapi_bin, nextjs_server, export_root) if not p.exists()]
    if missing:
        raise SystemExit(
            "[FATAL] 打包产物缺失（请确认已安装 Presenton Open Source.app）：\n  "
            + "\n  ".join(str(m) for m in missing)
        )

    soffice = _first_existing(
        [
            os.environ.get("SOFFICE_PATH", ""),
            "/Applications/LibreOffice.app/Contents/MacOS/soffice",
            os.path.expanduser(
                "~/.cache/codex-runtimes/codex-primary-runtime/dependencies/bin/soffice"
            ),
            "/usr/local/bin/soffice",
            "/opt/homebrew/bin/soffice",
        ]
    )
    imagemagick = _first_existing(
        [os.environ.get("IMAGEMAGICK_BINARY", ""), "/usr/local/bin/magick",
         "/opt/homebrew/bin/magick", "/usr/local/bin/convert"]
    )
    convert_module = export_root / "py" / "convert-darwin-arm64"
    node_bin = _first_existing(
        [os.environ.get("HX_NODE_BIN", ""), "node",
         os.path.expanduser("~/.nvm/versions/node/v22.22.0/bin/node")]
    ) or "node"
    chrome_bin = _resolve_chrome()
    return Artifacts(
        fastapi_bin=fastapi_bin,
        fastapi_dir=fastapi_bin.parent,
        nextjs_server=nextjs_server,
        nextjs_cwd=nextjs_server.parent,
        export_root=export_root,
        soffice=soffice,
        imagemagick=imagemagick,
        convert_module=convert_module if convert_module.exists() else None,
        node_bin=node_bin,
        chrome_bin=chrome_bin,
    )


def _resolve_chrome() -> str | None:
    """找一个可用 Chrome 给 Puppeteer（D2）：env 覆盖 > 已缓存 Chrome for Testing（取最新）> 系统 Chrome。

    Presenton 导出运行时把 Chrome 版本钉死（如 146.0.7680.76），首跑自动下载常因网络/代理
    损坏（end of central directory record signature not found）。复用已装 Chrome 经
    PUPPETEER_EXECUTABLE_PATH 注入即可跳过下载（零 fake：找不到就返回 None，导出会如实报错）。
    """
    env_override = os.environ.get("HX_CHROME_BIN", "")
    if env_override and Path(env_override).exists():
        return env_override
    cft_root = Path(os.path.expanduser("~/.cache/puppeteer/chrome"))
    if cft_root.is_dir():
        # 形如 mac_arm-148.0.7778.97/chrome-mac-arm64/Google Chrome for Testing.app/Contents/MacOS/...
        cands: list[tuple[str, str]] = []
        for d in cft_root.iterdir():
            inner = d / "chrome-mac-arm64" / "Google Chrome for Testing.app" / "Contents" / "MacOS" / "Google Chrome for Testing"
            if inner.exists():
                cands.append((d.name, str(inner)))
        if cands:
            cands.sort(reverse=True)  # 版本号字典序近似取最新
            return cands[0][1]
    return _first_existing([
        "/Applications/Google Chrome.app/Contents/MacOS/Google Chrome",
        "/Applications/Chromium.app/Contents/MacOS/Chromium",
    ])


# ---------------------------------------------------------------------------
# 端口预分配 + env 构建 + spawn + 探活 + tree-kill
# ---------------------------------------------------------------------------
def free_port() -> int:
    s = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
    try:
        s.bind(("127.0.0.1", 0))
        return s.getsockname()[1]
    finally:
        s.close()


def build_env(
    cred: LlmCredential,
    art: Artifacts,
    app_data: Path,
    fast_port: int,
    next_port: int,
) -> dict[str, str]:
    """复刻 main.ts:299-394 的 env，并叠加 new-api LLM/图像（D1/D4）。"""
    fast_url = f"http://127.0.0.1:{fast_port}"
    next_url = f"http://127.0.0.1:{next_port}"
    user_config = str(app_data / "userConfig.json")
    env: dict[str, str] = {
        # --- 基础 ---
        "DEBUG": "False",
        "CAN_CHANGE_KEYS": "false",  # 关 per-request 重载，spawn env 权威
        "DISABLE_AUTH": "true",
        "MIGRATE_DATABASE_ON_STARTUP": "True",
        "DISABLE_ANONYMOUS_TRACKING": "true",
        "APP_DATA_DIRECTORY": str(app_data),
        "TEMP_DIRECTORY": str(app_data / "tmp"),
        "USER_CONFIG_PATH": user_config,
        # --- LLM（D1：custom → new-api，owner 配额）---
        "LLM": "custom",
        "CUSTOM_LLM_URL": cred.base_url,
        "CUSTOM_LLM_API_KEY": cred.token,
        "CUSTOM_MODEL": cred.chat_model,
        # --- 图像（D4 通道①：Presenton 内部自动配图 → new-api，openai_compatible）---
        "IMAGE_PROVIDER": "openai_compatible",
        "OPENAI_COMPAT_IMAGE_BASE_URL": cred.base_url,
        "OPENAI_COMPAT_IMAGE_API_KEY": cred.token,
        "OPENAI_COMPAT_IMAGE_MODEL": cred.image_model,
        # --- 跨进程 URL（导出强依赖 NEXT_PUBLIC_URL，须先备好）---
        "FAST_API_INTERNAL_URL": fast_url,
        "NEXT_PUBLIC_FAST_API": fast_url,
        "NEXT_PUBLIC_URL": next_url,
        # --- 导出运行时（D2：复用已安装 app 产物）---
        "EXPORT_PACKAGE_ROOT": str(art.export_root),
        "EXPORT_RUNTIME_DIR": str(art.export_root),
        "LITEPARSE_NODE_BINARY": art.node_bin,
    }
    if art.soffice:
        env["SOFFICE_PATH"] = art.soffice
    if art.imagemagick:
        env["IMAGEMAGICK_BINARY"] = art.imagemagick
    if art.convert_module:
        env["BUILT_PYTHON_MODULE_PATH"] = str(art.convert_module)
    if art.chrome_bin:
        # 导出运行时 Puppeteer：用已装 Chrome，禁止首跑去下钉版（常损坏）。
        env["PUPPETEER_EXECUTABLE_PATH"] = art.chrome_bin
        env["PUPPETEER_SKIP_DOWNLOAD"] = "true"
        env["PUPPETEER_CACHE_DIR"] = os.path.expanduser("~/.cache/puppeteer")
    return env


def write_user_config(env: dict[str, str], app_data: Path) -> None:
    """与 spawn env 一致地写一份 userConfig.json（双保险；UI 设置页也读它）。"""
    for sub in ("", "tmp", "exports", "images", "uploads", "fonts", "pptx-to-html"):
        (app_data / sub).mkdir(parents=True, exist_ok=True)
    cfg = {
        "LLM": "custom",
        "CUSTOM_LLM_URL": env["CUSTOM_LLM_URL"],
        "CUSTOM_LLM_API_KEY": env["CUSTOM_LLM_API_KEY"],
        "CUSTOM_MODEL": env["CUSTOM_MODEL"],
        "IMAGE_PROVIDER": "openai_compatible",
        "OPENAI_COMPAT_IMAGE_BASE_URL": env["OPENAI_COMPAT_IMAGE_BASE_URL"],
        "OPENAI_COMPAT_IMAGE_API_KEY": env["OPENAI_COMPAT_IMAGE_API_KEY"],
        "OPENAI_COMPAT_IMAGE_MODEL": env["OPENAI_COMPAT_IMAGE_MODEL"],
    }
    (app_data / "userConfig.json").write_text(json.dumps(cfg))


def http_ok(url: str, timeout: float = 4.0) -> int | None:
    try:
        with urllib.request.urlopen(url, timeout=timeout) as r:
            return r.status
    except urllib.error.HTTPError as e:
        return e.code
    except Exception:
        return None


def wait_ready(
    url: str,
    label: str,
    timeout: int = READY_TIMEOUT_S,
    proc: subprocess.Popen | None = None,
    log: Path | None = None,
) -> None:
    """探活直到 2xx-4xx；若进程在就绪前退出，立即报错并附日志尾部（零 fake，不空等）。"""
    start = time.time()
    while time.time() - start < timeout:
        if proc is not None and proc.poll() is not None:
            tail = ""
            if log and log.exists():
                tail = "\n".join(log.read_text("utf-8", "replace").splitlines()[-8:])
            raise SystemExit(
                f"[FATAL] {label} 进程已退出(code={proc.returncode})未就绪 —— "
                f"通常是启动期 LLM/图像 provider 校验连不上 new-api。日志尾部:\n{tail}"
            )
        code = http_ok(url)
        if code is not None and 200 <= code < 500:
            print(f"[ready] {label} <- {url} ({code}) in {time.time()-start:.1f}s")
            return
        time.sleep(1)
    raise SystemExit(f"[FATAL] {label} 在 {timeout}s 内未就绪：{url}")


def precheck_newapi(cred: LlmCredential) -> bool:
    """活体核实 new-api 可达 + 列出模型（零 fake：不可达即如实返回 False）。"""
    url = f"{cred.base_url}/models"
    req = urllib.request.Request(url, headers={"Authorization": f"Bearer {cred.token}"})
    try:
        with urllib.request.urlopen(req, timeout=8) as r:
            body = r.read().decode("utf-8", "replace")
        ids = [m.get("id") for m in json.loads(body).get("data", [])]
        print(f"[new-api] 可达，{len(ids)} 个模型；含图像候选："
              f"{[i for i in ids if any(k in (i or '').lower() for k in ('image','dall','flux','seedream','sd','gpt-image'))][:8]}")
        return True
    except Exception as e:
        print(f"[new-api] 不可达：{cred.base_url} -> {e}")
        return False


def start_compat_shim(sc: "Sidecar", cred: LlmCredential, logs: Path) -> str:
    """起 new-api 结构化输出兼容 shim（json_schema→json_object+inject），返回供 sidecar 用的 .../v1.

    为什么：活体实测 new-api gpt-5.x 渠道不认 `response_format: json_schema`（静默丢弃→返回
    Markdown→Presenton `json.loads` 崩 500），但认 `json_object`。shim 在中间做最小改写，
    真实模型真实 JSON（详见 newapi_compat_shim.py）。fastapi 启动期会校验 LLM 连通性，
    故 shim 必须**先就绪**再起 fastapi。
    """
    shim_path = Path(__file__).resolve().with_name("newapi_compat_shim.py")
    if not shim_path.exists():
        raise SystemExit(f"[FATAL] 缺少兼容 shim：{shim_path}")
    upstream_root = (
        cred.base_url[:-3].rstrip("/") if cred.base_url.endswith("/v1") else cred.base_url
    )
    shim_port = free_port()
    proc = sc.spawn(
        "llm-compat-shim",
        [sys.executable, str(shim_path), "--port", str(shim_port), "--upstream", upstream_root],
        shim_path.parent, {}, logs / "shim.log",
    )
    wait_ready(f"http://127.0.0.1:{shim_port}/healthz", "compat-shim",
               timeout=20, proc=proc, log=logs / "shim.log")
    return f"http://127.0.0.1:{shim_port}/v1"


@dataclass
class Sidecar:
    procs: list[subprocess.Popen] = field(default_factory=list)

    def spawn(self, name: str, argv: list[str], cwd: Path, env: dict[str, str],
              log: Path) -> subprocess.Popen:
        full_env = {**os.environ, **env}
        fh = open(log, "wb")
        p = subprocess.Popen(
            argv, cwd=str(cwd), env=full_env, stdout=fh, stderr=subprocess.STDOUT,
            stdin=subprocess.DEVNULL, start_new_session=True,
        )
        print(f"[spawn] {name} pid={p.pid} -> {log}")
        self.procs.append(p)
        return p

    def shutdown(self) -> None:
        for p in self.procs:
            with contextlib.suppress(Exception):
                os.killpg(os.getpgid(p.pid), signal.SIGTERM)
        time.sleep(2)
        for p in self.procs:
            with contextlib.suppress(Exception):
                if p.poll() is None:
                    os.killpg(os.getpgid(p.pid), signal.SIGKILL)


def main() -> int:
    ap = argparse.ArgumentParser(description="唤星 Presenton sidecar 启动器（P0）")
    ap.add_argument("--app-resources", default=DEFAULT_APP)
    ap.add_argument("--image-model", default=os.environ.get("HX_IMAGE_MODEL", "gpt-image-1"),
                    help="new-api 上的图像模型 id（活体须真实存在）")
    ap.add_argument("--app-data", default=None, help="sidecar 专用数据目录（默认 ~/.hasn/<owner>/presenton-sidecar）")
    ap.add_argument("--hold", action="store_true", help="启动后保持运行（供手动 UI/Agent 联调）")
    ap.add_argument("--smoke", action="store_true", help="探活 + 列路由后退出")
    ap.add_argument(
        "--llm-compat", action=argparse.BooleanOptionalAction, default=True,
        help="在 sidecar↔new-api 间挂结构化输出兼容 shim（默认开；"
             "new-api 渠道原生支持 json_schema 后可 --no-llm-compat 摘除）",
    )
    args = ap.parse_args()

    cred = resolve_credential(args.image_model)
    art = resolve_artifacts(Path(args.app_resources))
    owner_dir = _active_owner_dir()
    app_data = Path(args.app_data) if args.app_data else owner_dir / "presenton-sidecar"

    print("=== 唤星 Presenton sidecar（P0） ===")
    print(f"owner       : {cred.owner_id}")
    print(f"new-api     : {cred.base_url}  chat={cred.chat_model}  image={cred.image_model}")
    print(f"fastapi bin : {art.fastapi_bin}")
    print(f"soffice     : {art.soffice}")
    print(f"imagemagick : {art.imagemagick}")
    print(f"chrome      : {art.chrome_bin}")
    print(f"app_data    : {app_data}")

    newapi_up = precheck_newapi(cred)

    fast_port, next_port = free_port(), free_port()
    env = build_env(cred, art, app_data, fast_port, next_port)
    logs = app_data / "logs"
    logs.mkdir(parents=True, exist_ok=True)

    sc = Sidecar()
    shim_url: str | None = None
    try:
        # LLM 兼容 shim 须先于 fastapi 就绪：new-api gpt-5.x 不认 json_schema，fastapi 启动期
        # 会校验 LLM 连通性（api/lifespan.py）。shim 改写 json_schema→json_object（零 fake 真 JSON）。
        if args.llm_compat:
            shim_url = start_compat_shim(sc, cred, logs)
            env["CUSTOM_LLM_URL"] = shim_url
            print(f"[llm-compat] CUSTOM_LLM_URL -> {shim_url}（json_schema→json_object）")
        write_user_config(env, app_data)

        # 先备好 NEXT_PUBLIC_URL（env 已含），再起两进程；导出在请求期才用 Next，故顺序不破。
        next_proc = sc.spawn(
            "nextjs", [art.node_bin, str(art.nextjs_server)], art.nextjs_cwd,
            {**env, "HOSTNAME": "127.0.0.1", "PORT": str(next_port)},
            logs / "nextjs.log")
        fast_proc = sc.spawn(
            "fastapi", [str(art.fastapi_bin), "--port", str(fast_port)],
            art.fastapi_dir, env, logs / "fastapi.log")

        wait_ready(f"http://127.0.0.1:{next_port}/", "Next.js",
                   proc=next_proc, log=logs / "nextjs.log")
        wait_ready(f"http://127.0.0.1:{fast_port}/docs", "FastAPI",
                   proc=fast_proc, log=logs / "fastapi.log")

        # 路由探活（证明 /api/v1/ppt/* 在）
        for path in ("/api/v1/ppt/presentation/all", "/docs"):
            code = http_ok(f"http://127.0.0.1:{fast_port}{path}")
            print(f"[route] {path} -> {code}")

        # 把解析出的端口写入 app_data/sidecar.json，供后台启动后读取（P2 管理器同款句柄）。
        (app_data / "sidecar.json").write_text(json.dumps({
            "owner_id": cred.owner_id,
            "fastapi_port": fast_port,
            "nextjs_port": next_port,
            "fastapi_url": f"http://127.0.0.1:{fast_port}",
            "nextjs_url": f"http://127.0.0.1:{next_port}",
            "app_data": str(app_data),
            "newapi_up": newapi_up,
            "llm_compat_url": shim_url,  # None=直连 new-api；否则 sidecar 经此 shim 出网
        }))

        print("\n=== P0 sidecar 就绪 ===")
        print(f"  FastAPI : http://127.0.0.1:{fast_port}  (/docs, /api/v1/ppt/*)")
        print(f"  Next UI : http://127.0.0.1:{next_port}")
        print(f"  new-api : {'可达' if newapi_up else '不可达（活体 generate/图像将失败——零 fake，不伪造）'}")
        if not newapi_up:
            print("  [注] 活体 LLM/图像生成需 new-api 在 127.0.0.1:3180 上线并配置好 channels。")

        if args.smoke:
            return 0
        if args.hold:
            print("\n保持运行中（Ctrl-C 退出）……")
            signal.pause()
        return 0
    finally:
        if not args.hold:
            sc.shutdown()


if __name__ == "__main__":
    sys.exit(main())
