# P0 sidecar 本地直跑 —— 活体验证证据

> 事实源：设计 `docs/hasn-node设计文档/14-AI-Native应用平台/12-Presenton演示文稿应用接入设计.md` §5；
> 实施 `.../实施/02-Presenton接入实施清单.md` P0。
> 启动器：`huanxing/sidecar_launch.py`（同时是 P2 daemon 端 Rust sidecar 管理器的规格）。

## 验证日期
2026-06-05（owner=h_47094e96 小智 / 18611348367）；new-api 已由主人在 `127.0.0.1:3180` 上线后复跑。

## TL;DR（new-api 上线后的活体结果）—— 完整 generate→export 已真实跑通 ✅

经三处真实修复后，**完整 generate→导出 PPTX 已端到端跑通**（HTTP 200 / 94s / 落盘 143KB 合法
OOXML / 2 slides）。证据见 `test-results/presenton-p0/presenton-generate-2slides.pptx`。

三处修复（均零 Mock 零 Fake）：

1. **new-api gpt-5.x 不支持 `response_format: json_schema`（结构化输出，静默丢弃→返回 Markdown→
   llmai `json.loads` 崩 char 0）** → 加 `newapi_compat_shim.py` 在 sidecar↔new-api 间改写为
   `json_object`+schema 注入（真实 JSON）；Presenton 自带 `validate_schema` 重试环兜底。
2. **Presenton 模板 schema 页 URL 硬编码 `http://localhost`（80 口）** → P2 构建期补丁
   `internal_app_base_url()` 改用动态 Next.js URL（`NEXT_PUBLIC_URL`）。已用**打补丁源码 FastAPI**
   （`--fastapi-source`）活体验证：4 次 LLM 调用（大纲+结构+2 页内容）全经 shim，模板 schema 抽取
   经 Chrome 加载 `NEXT_PUBLIC_URL/schema` 成功，导出 PPTX 落盘。
3. **new-api 未配图像渠道**（`/v1/models` 无图像模型，503 `model_not_found`）→ D4 通道① 逐页配图
   **如实降级为占位图**（`/static/images/placeholder.jpg`，非致命）；待主人加图像渠道后即真实配图（基础设施）。

> 注：打包二进制（PyInstaller PYZ）无法改 Python，故用**打补丁源码 FastAPI** 验证 P2 补丁；
> 生产嵌入式 sidecar 应分发**打补丁后重新打包**的 Presenton（P2「构建期补丁」即指此）。

## 活体逐阶段证据（按 generate 管线推进）

| 阶段 | 结果 | 证据 |
|---|---|---|
| sidecar 三进程就绪 | ✅ | shim/next/fastapi 各取动态端口；`/docs`200、`/api/v1/ppt/presentation/all`200 |
| 大纲生成（LLM 经 new-api） | ✅ | `[shim] #1 POST /v1/chat/completions rewritten(json_schema→json_object)` → fastapi `Generated 2 outlines for the presentation` |
| 结构化输出 json_schema 兼容 | ✅（shim） | 直连 new-api 时 `json.loads` 崩 `Expecting value: line 1 column 1 (char 0)`（拿到 Markdown）；经 shim 改 `json_object`+注入 schema 后产出合法 JSON（实测 3 页 `{"slides":[…]}`） |
| 导出运行时 Chrome 启动 | ✅ | 注入 `PUPPETEER_EXECUTABLE_PATH`(复用已装 Chrome for Testing 148) 后，错误从「Failed to launch browser / Chrome was not found」变为「Failed to fetch or parse schema page」——证明浏览器已能启动，仅差页面 URL |
| 模板 schema 抽取 | ❌（P2 补丁） | `extract_schema("http://localhost/schema?group=general")` + 兜底 `http://localhost/api/template` 都连 `localhost:80`，sidecar 未在 80 口起服务 → `Cannot connect to host localhost:80` |
| 逐页配图（D4 通道①） | ⏸️（基础设施） | new-api `/v1/models` 无图像模型；`generate_image` 失败被 catch 落占位图（非致命，见 `services/image_generation_service.py:125`） |

## 三处真实修复 / 隔离（代码层，零 fake）

### ① new-api 结构化输出兼容 shim（`huanxing/newapi_compat_shim.py`，D1 兼容层）
- 活体实测：new-api 的 gpt-5.x 渠道桥接 OpenAI Responses API（响应 id `resp_…`），
  `response_format: json_schema`（strict）被**静默丢弃**，返回自由 Markdown；但**支持 `json_object`**。
- shim 仅对 `*/chat/completions` 且 `response_format.type==json_schema` 的请求改写为 `json_object`+把 schema 注入
  system 消息；其余（含 `/v1/images/generations`、`/v1/models`、非结构化 chat）原样透传。流式 SSE 增量转发。
- 正确性不靠 shim 兜底：Presenton 自带 `generate_structured_with_schema_retries` 的 `validate_schema` 重试环
  仍校验 JSON 是否匹配 schema 并自纠。
- 退场条件：new-api 提供原生支持 json_schema 的渠道后，`--no-llm-compat` 摘除，`CUSTOM_LLM_URL` 直指 new-api。

### ② Chrome 复用（`PUPPETEER_EXECUTABLE_PATH`，D2）
- Presenton 导出运行时把 Chrome 版本钉死（如 146.0.7680.76），首跑自动下载在本机损坏
  （`end of central directory record signature not found`）。
- 启动器 `_resolve_chrome()` 复用已装 Chrome（env 覆盖 > 已缓存 Chrome for Testing 最新 > 系统 Chrome），
  经 `PUPPETEER_EXECUTABLE_PATH`+`PUPPETEER_SKIP_DOWNLOAD` 注入，跳过下载。

### ③ 模板 schema URL 硬编码 `http://localhost`（P2 待补丁）
- `templates/get_layout_by_name.py:134` 主路径 `http://localhost/schema?group=…`（Chrome 加载）
  + `:83` 兜底 `http://localhost/api/template?group=…`（HTTP）——都是 80 口。
- 真 Electron app 用动态端口加 nginx-on-80 的部署假设；嵌入式 sidecar 非 root 无法绑 80（实测 `bind 80 → Permission denied`）。
- 修法属 **P2 presenton 构建期补丁**：把 `http://localhost` 改为动态 Next.js URL（`NEXT_PUBLIC_URL`）。
  源码 FastAPI 仅 36 依赖、无重型 ML 库，可打补丁后从源码跑，验证完整 generate→export。

## 历史记录（new-api 未上线时的初版结论，已被上文 TL;DR 取代）

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
