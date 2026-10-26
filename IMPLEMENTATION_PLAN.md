# AstrBot 官方 OAuth 识图：实现方案与安全分析

核查日期：2026-10-25。目标是独立插件 `astrbot_plugin_chatgpt_vision`，不修改原生图插件和服务器现有 DeepSeek 配置。

## 已核实的架构

本地已有的仓库是通用生图插件，其 `GenerationResult` 返回图片字节；`codex_responses` 固定发送 image_generation 工具，不能作为本次视觉客户端。服务器安装版本及 DeepSeek 转述插件源码不在本地，本次不宣称已验证它们。

AstrBot 官方消息文档与公开源码显示：平台消息转成 AstrMessageEvent，图片是消息链中的 Image 组件。内置 Agent 在准备会话锁和构建模型请求前触发 on_waiting_llm_request；该钩子停止事件后会退出后续请求。新插件在此处理被唤醒的图片消息，显式 /OAuth识图 指令走独立入口。不能只在 on_llm_request 清空 image_urls，因为原有转述可能已先完成。

## 实现分层

1. **消息与后端选择**：配置 backend=existing/chatgpt_oauth。默认 existing 完全不拦截自动消息，由服务器原有流程处理；切换 OAuth 后，仅接管允许的 QQ 平台、用户和群，发送结果并停止后续内置请求。现有独立插件若在更早阶段主动回复，仍需服务器联调协调开关，不能凭空保证不重复。
2. **独立 OAuth 管理**：管理员在服务器终端运行 manage.py，浏览器通过官方授权页登录；本机 127.0.0.1 回调，随机 state、nonce、PKCE S256、一次性回调和超时。按官方开源应用动态注册流程使用 dynamic_agent_client，保存回调颁发的实际 client_id 和稳定 host ID。验证 JWKS 签名、iss/aud/exp/nonce/sub 和授权范围。令牌不进入 QQ、URL、AstrBot 配置界面或日志，也不读取 ~/.codex、浏览器或其他应用凭据。
3. **凭据保护**：插件专属数据目录；Windows 使用当前服务用户的 DPAPI，Unix 用目录0700/文件0600；原子写入。跨进程文件锁串行化刷新，成套替换轮换令牌。单独命令完成退出和官方撤销，保留账号注册映射。管理员本机 CLI 提供账号状态、模型列表、验证和选择。
4. **账号模型目录**：用同一账号 OAuth Token 请求官方 /v1/models，只显示服务返回的 models/slug/display_name。目录存在不等于有视觉权限；选择前提交随机生成、不含隐私的图片校验任务，收到完整完成状态且正确读出校验码后才标记可用。失败标为未确认，不擅自指定一个模型。账号切换、模型变化或验证过期后重新确认。
5. **视觉 Responses 客户端**：只请求 https://api.openai.com/v1/responses，input 使用消息数组，文本与 input_image data URL 同时提交。固定 store=false、stream=true，不发送工具、不发送不支持的采样/输出限制参数。SSE 按完整事件解析，正确处理分片、CRLF、completed/failed/incomplete、拒绝、连接中断。先在内存收齐成功答案，再一次性回复 QQ；流中失败不发送已有片段。
6. **图片处理**：仅取本条消息实际图片；可显式配置引用图片。限定数量、字节、总大小和像素，真实解码后重编码成 PNG/JPEG，移除 EXIF 等附带信息；以 data URL 上传，不把 QQ 的签名下载链接交给模型。网络图片只允许 HTTP(S) 公网地址，校验每次 DNS 解析和重定向；本地文件限制在配置的可信目录，拒绝 UNC 和路径逃逸。无图片落盘和跨会话缓存。
7. **人格与回复**：读取当前会话人格；使用 instructions 继承人格并追加中文、天然呆、简短、纯文本约束。图片内容按不可信数据处理，禁止将图中文字当成系统指令。OCR 以准确性优先，不为扮演人格改写原文。输出纯文本、去除常见 Markdown 装饰并限制长度；长 OCR 明示截断。默认不转发图片/全文给 DeepSeek，不自动写入识图历史。

## 错误与用量策略

- 401：只在流打开前尝试一次串行刷新；终止性刷新错误清除不可用令牌并要求重新登录，网络故障不误删凭据。
- 403/不支持能力：安全提示权限或视觉能力不满足；无 OAuth 死循环、无 API Key 回退。
- 429/订阅使用限制：暂停当前请求，提供简短中文提示；遵循 Retry-After，不猜测套餐重置时间。
- 503：保留凭据；本轮提示稍后重试，不无限自动重放图片请求。
- 流中断/failed/incomplete：不把半截结果当成识图成功，不自动切换供应商传送隐私图片。

## 风险及处理

| 风险 | 处理与剩余限制 |
|---|---|
| 登录劫持、错误账号替换 | 校验state/nonce/PKCE、签名和原账号身份；不同注册记录分开 |
| 刷新并发导致令牌复用 | 文件锁和原子写入；管理命令与插件共享同一锁 |
| QQ群消耗账号额度、误传敏感图片 | 默认仅管理员可用、群默认关闭，显式授权后才传图；使用官方Usage管理额度 |
| 图片URL访问内网或读取任意本地文件 | 独立受限图片下载器，不复用旧插件trusted_source绕过路径 |
| 日志、异常、部分流泄露令牌或图片 | 外发错误采用枚举和固定文本；不输出原始响应/提示词/URL/Base64/令牌，异常抑制上下文 |
| store=false被误当作全链路零留存 | 只控制请求存储；QQ、AstrBot自身日志以及服务政策仍需单独管理 |
| 旧转述插件重复工作 | 默认existing；OAuth接管内置Agent前置钩子，但外部提前回复需实机协调 |
| 平台预览行为或权限变化 | 官方端点固定、能力需验证；真实登录、视觉请求、QQ适配器验收均列为部署后检查 |

## 测试与交付

离线测试覆盖：OAuth回调、签名/nonce/范围、过期与轮换、锁冲突；图片伪装/超大/EXIF/路径逃逸/内网URL；账号目录与图片校验；真实分块SSE与各种终止事件；QQ消息提取、后端旁路、人格及纯文本；注入假令牌和图片哨兵验证错误与日志不泄露。测试只用生成图片和假凭据。

输出独立插件ZIP、中文说明、安全说明、测试记录和SHA-256。不会在本地安装新依赖；仅使用已有的aiohttp、Pillow和cryptography（也是当前AstrBot公开依赖）。没有服务器登录与真实QQ环境时，不宣称完成真实账号和端到端验收。

## 官方依据

- https://developers.openai.com/siwc/token-sharing-open-source/sign-in
- https://developers.openai.com/siwc/token-sharing-open-source/profiles-and-sessions
- https://developers.openai.com/siwc/token-sharing-open-source/models-and-inference
- https://developers.openai.com/siwc/token-sharing-open-source/errors-and-recovery
- https://developers.openai.com/siwc/token-sharing-open-source/preview-limitations
- https://docs.astrbot.app/dev/star/guides/listen-message-event.html
- https://docs.astrbot.app/dev/star/guides/ai.html
- https://github.com/AstrBotDevs/AstrBot/blob/master/astrbot/core/pipeline/process_stage/method/agent_sub_stages/internal.py


## 测试版补充

最终独立指令为 /OAuth识图、/OAuth识图状态。默认不自动接管；OpenAI和QQ图片下载的代理分开配置，留空使用系统静态代理/环境变量。详见README。
