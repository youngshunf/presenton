# P0 sidecar 本地直跑 —— 活体验证证据

> 事实源：设计 `docs/hasn-node设计文档/14-AI-Native应用平台/12-Presenton演示文稿应用接入设计.md` §5；
> 实施 `.../实施/02-Presenton接入实施清单.md` P0。
> 启动器：`huanxing/sidecar_launch.py`（同时是 P2 daemon 端 Rust sidecar 管理器的规格）。

## 验证日期
2026-06-05（owner=h_47094e96 小智 / 18611348367）

## 已证实（零 Mock 零 Fake，真实进程）

| 项 | 结果 | 证据 |
|---|---|---|
| 复用已安装 app 打包产物 | ✅ | FastAPI 二进制 `…/resources/fastapi/fastapi`(Mach-O arm64) + Next standalone `…/resources/nextjs/servers/nextjs/server.js` + 导出运行时 `…/resources/export` 均解析到 |
| 打包 FastAPI 二进制可独立 spawn（非 Electron/Docker） | ✅ | 进程启动、进入 `api/lifespan.py` 启动序列 |
| Next.js standalone 可独立 spawn 并完整就绪 | ✅ | `▲ Next.js 16.2.6 … ✓ Ready in 0ms`，`GET / -> 200` 1.0s 内就绪 |
| 端口预分配（每实例动态端口） | ✅ | fastapi/next 各取一个 `127.0.0.1:0` 空闲端口 |
| env 接线正确（D1 LLM custom + D4 图像 openai_compatible 指向 new-api） | ✅ | FastAPI 启动期 `check_llm_and_image_provider_api_or_model_availability` → `list_available_openai_compatible_models` 真的向 `CUSTOM_LLM_URL`(new-api) 发起连接（说明 `CUSTOM_LLM_URL`/`OPENAI_COMPAT_IMAGE_*` 已正确流入） |
| 导出运行时四方（Chromium 包/soffice/ImageMagick/NEXT_PUBLIC_URL）就位 | ✅ | `EXPORT_PACKAGE_ROOT`/`SOFFICE_PATH`/`IMAGEMAGICK_BINARY`/`NEXT_PUBLIC_URL` 全部解析注入；导出运行时随已安装 app 在位（D2 复用，无需重新打包） |
| 零 Mock 零 Fake：前置缺失即如实报错 | ✅ | new-api 不可达时 precheck 报不可达 + FastAPI 启动期连接失败 `Application startup failed. Exiting.`(code=3)，启动器立即附堆栈报错，不伪造 |

## 关键发现：Presenton 启动期强制校验 LLM/图像 provider 可达

`servers/fastapi/api/lifespan.py:97` → `utils/model_availability.py:235`
`check_llm_and_image_provider_api_or_model_availability`：**FastAPI 在 lifespan 启动阶段就会向配置的
LLM/图像 base_url 拉取模型列表**，连不上则启动失败退出。这意味着 sidecar **真实依赖 new-api**，
无法用空配置“假启动”——与零 Mock 零 Fake 一致。

## 唯一阻塞（环境前提，非代码缺陷）

**new-api 网关需在 `127.0.0.1:3180` 上线**，且 channels 已配置可服务：
- 聊天模型 `gpt-5.5`（owner_llm_credentials 现值）
- 一个图像模型（启动器 `--image-model`，默认 `gpt-image-1`，活体须为 new-api 上真实存在的图像模型 id）

本机现状：new-api 源码在 `/Users/mac/saas/ai-creator/services/new-api`（Go），但**无 `.env`/无 channels DB/未运行**，
故无法由本仓自动拉起一个可服务的 new-api（需 owner 的上游 provider key）。

## new-api 上线后如何复跑（一条命令）

```bash
# 1) 查 new-api 上的真实图像模型 id
curl -s http://127.0.0.1:3180/v1/models -H "Authorization: Bearer <owner_token>" | jq '.data[].id'

# 2) 起 sidecar 并保持运行（供 UI/Agent 联调）
python3 huanxing/sidecar_launch.py --image-model <真实图像模型id> --hold

# 3) 另开终端，对 sidecar 真实生成 + 导出（验收：导出 PPTX/PDF 落盘 + 逐页配图）
#    FastAPI 端口见启动日志；POST /api/v1/ppt/presentation/generate（export_as=pptx/pdf）
```

启动器就绪后会打印 FastAPI/Next 端口与 new-api 可达性；活体 generate/图像/导出在 new-api 上线后即可一次跑通。
