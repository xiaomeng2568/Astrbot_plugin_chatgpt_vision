# AstrBot ChatGPT OAuth 识图

[下载 v0.1.5 测试包](https://github.com/xiaomeng2568/Astrbot_plugin_chatgpt_vision/releases/tag/v0.1.5-fix1) · [历史版本](https://github.com/xiaomeng2568/Astrbot_plugin_chatgpt_vision/releases)

独立的 AstrBot 插件，通过官方 Sign in with ChatGPT 开源应用授权流程调用公开 Responses API，支持图片描述、截图问答与 OCR。没有实现 OAuth 图像生成，也不把订阅授权当作 API Key。

这是待服务器联调的测试版：离线测试通过不代表你的账号必定有权限、你的地区必定可访问，或与未知的第三方转述插件已经兼容。

## 一、安装前

- AstrBot >= 4.25.3，使用内置 Agent 执行流程。当前实现依据官方文档和公开 master 源码；服务器版本仍须实测。
- Python 必须使用 **AstrBot 实际运行的环境**，不是任意系统 Python。代码只用现有 aiohttp、Pillow、cryptography；这些也是当前 AstrBot 公开依赖。本次没有安装新依赖。包内 requirements.txt 供宿主检查，若服务器缺依赖请先反馈，不要盲目升级生产环境。
- 登录脚本与 AstrBot 必须以同一个 Windows 用户运行。Windows 凭据由当前用户 DPAPI 加密；换成另一个服务用户无法解密。
- 这个插件不需要 GT 720 做本地模型推理。

在 WebUI“插件 → 安装插件 → 从文件安装”上传 ZIP，检查加载状态。解压后应该是一个 `astrbot_plugin_chatgpt_vision` 目录；若手工部署，将该目录放到 `AstrBot/data/plugins/`。

默认 `backend=existing`、`auto_reply=false`，不会接管正常图片对话。使用独立的 /OAuth识图 指令，不抢占旧插件可能已使用的 /识图 指令。原有生图插件与 DeepSeek 配置不需删除。

## 二、独立代理链路

在本插件 WebUI 配置：

| 设置 | 作用 | 留空时 |
|---|---|---|
| openai_proxy | 授权代码交换、JWKS、令牌刷新、模型列表、Responses和注销 | 读取系统默认代理路由 |
| image_proxy | QQ/CDN图片下载，与OpenAI代理独立 | 读取系统默认代理路由 |

示例：`http://127.0.0.1:7890`。支持 HTTP/HTTPS 代理；不支持 SOCKS 地址或 PAC 脚本，本包不会安装额外代理依赖。若代理软件同时提供 SOCKS 和 HTTP 端口，请使用 HTTP 端口。

“系统默认”具体指进程环境的 HTTP_PROXY / HTTPS_PROXY / ALL_PROXY / NO_PROXY，以及 Python 能发现的 Windows 静态代理；没有系统代理时直连。透明/TUN路由由操作系统负责。不读取 `.netrc` 中的无关账户登录信息。修改代理后重载插件。

登录网页使用 **服务器浏览器自身的网络设置**，Python 的独立代理无法替浏览器改设置。QQ本体的连接也不由本插件改动。代理若带用户名/密码，配置和截图需保密；推荐使用本机无凭据监听端口。

## 三、在服务器本机授权

建议用远程桌面进入 Windows 服务器，在服务器上打开终端和浏览器。不要把服务器产生的回调链接改为公网地址，也不要复制现有浏览器Cookie、Codex凭据或任何Token。

在插件目录打开终端，以下 `python` 都应替换为 AstrBot 的 Python 完整路径（PowerShell 路径含空格时用 `& '完整路径/python.exe'`）。

```powershell
python manage.py login
python manage.py status
```

登录脚本自动读取 **本插件自己的** `data/config/astrbot_plugin_chatgpt_vision_config.json` 中的 `openai_proxy`。如果该文件在你的部署中位置不同，或想临时覆盖代理：

```powershell
python manage.py --proxy http://127.0.0.1:7890 login
```

授权回调只监听 `127.0.0.1:14558/auth/callback`，最长等待5分钟。脚本在浏览器打开官方授权页。若未自动打开，可执行 `login --no-browser`，只在服务器本机使用输出的授权链接。不要发到QQ、公开日志或反馈消息里。

端口冲突时：`python manage.py login --port 14559`。登录脚本不会把凭据发送到QQ或写入普通插件配置。实际保存到 `data/plugin_data/astrbot_plugin_chatgpt_vision/oauth-state.bin`；自定义部署可用 `--data-dir '实际目录'`，该目录必须和插件使用的目录一致。

首次按官方流程动态注册本插件；后续保留注册映射。浏览器授权完成后，脚本会分别显示“身份验证通过”和“实际推理授权”。**只登录成功而没有推理授权时，插件不会调用视觉模型。**

普通再次执行 login 会复用当前注册；可先用 status 查看账号记录ID，再执行 `python manage.py login --account <记录ID>`。注册另一个账号使用 `login --new-account`；切换已保存账号用 `use-account <记录ID>`，切换后重新验证模型。首次代码交换失败会保留已颁发client_id供重试，但未验证身份不会启用账号。不要把真实令牌粘贴到任何命令参数。

## 四、验证当前账号的视觉模型

```powershell
python manage.py models
python manage.py verify-model <实际返回的slug>
python manage.py select-model <同一个slug>
```

不内置固定模型名，也不把客户端缓存目录当成实际权限。`models` 请求当前账号的官方模型目录；`verify-model` 将一张本机随机生成、不含隐私的校验图发送给该模型，只有收到完整成功事件并正确读出六位校验码才通过。

图片验证可能消耗订阅额度。验证失败不一定说明模型不支持视觉，也可能是账号、网络、流协议或识别结果问题；本包会阻止选择未通过验证的模型。验证有效期24小时；过期后重新 verify-model / select-model。真实请求仍可能因为服务端权限变化而失败。

## 五、先手动测试 QQ，再启用自动接管

1. 本插件配置改成 `backend=chatgpt_oauth`，先保持 `auto_reply=false`。
2. 先在管理员的 QQ 私聊测试。默认只有 AstrBot 管理员可用；普通用户填入 `allowed_user_ids`。
3. 发送一条包含图片和文字的消息，例如 `/OAuth识图 描述一下这张图`、`/OAuth识图 帮我看这个报错` 或 `/OAuth识图 OCR，抄录图中可辨认的文字`。
4. `/OAuth识图状态` 只向管理员返回登录、授权和模型验证是否通过，不返回令牌、账号邮箱、图片数据或本地路径。
5. 默认每次最多3张、每张8MiB、合计16MiB、每张20M像素。自动忽略历史图、头像；动画图片要求改发静态截图。最长边默认2048，长截图OCR可调4096；太长的图建议分段发。
6. 如需处理引用图片，开启 `include_replied_images`；然后引用图片输入 `/OAuth识图 ...`。该开关只用于显式指令，自动模式只看当前消息的直接图片。

在“目标群”中选中群聊后，全体成员可用；未选中的群不可用。若需限制成员，开启“群内单独授权”并配置“群内可用成员”；私聊使用独立名单。

确认手动测试正常、旧转述插件没有提前回应，再启用 `auto_reply=true`。之后 QQ 正常唤醒机器人并带图，就会走 OAuth 识图；普通非唤醒群消息不会因为装了本插件而全部上传。

自动接管使用 `on_waiting_llm_request`，完成后停止内置 Agent 后续请求。它可阻止后面的内置转述和二次回复，但**无法追回另一个第三方插件更早已主动发出的消息**，也无法阻止一个不遵循事件停止状态的插件后台任务。本地没有服务器旧插件，因此尚未验证这一点。若重复回复，先关 auto_reply，保留显式指令并反馈旧插件名称、版本和触发顺序。

OAuth失败时不会自动把原图发给DeepSeek或其他供应商。若希望整个会话仍走原有图片转述，设置 `backend=existing` 即可；本插件不代替或重写服务器旧插件。

## 用量统计

如需统计QQ识图用量，从独立仓库 https://github.com/xiaomeng2568/Astrbot_plugin_token_usage 安装“模型用量统计”插件。安装后发送 `/token` 查看今日统计，`/token 近7天` 或 `/token 累计` 查询其他周期；默认仅机器人管理员可查。

OAuth识图仅上报接口返回的数字用量，不提供API费用或订阅余额。未知用量不按0计算；不安装统计插件也能正常识图，不需重新授权。命令行自检和模型目录查询不计入QQ识图统计。

## 六、人格与隐私

尝试读取当前会话绑定的人格，缺失时使用会话默认人格；无法读取则只用内置输出规则。默认中文、天然呆、简短、纯文本，最多500字；OCR要求忠实原文，长结果标记截断。模型输出不保证OCR逐字准确，重要文字需核对原图。

只把当前问题、当前图片和启用的人格规则发往官方API；不发送完整历史，不启用模型工具。图片在内存解码重编码，移除EXIF等元数据，以data URL上传，不把QQ的签名下载URL交给模型。输入输出不写入本插件的历史记录或图片缓存。

`store=false`不代表QQ、AstrBot本身或服务方政策全部零留存；这些需要单独配置。旧生图插件自己的图片缓存也不受本插件控制。截图中可见的隐私仍会作为图片内容提交，请发送前自行遮挡。

若QQ适配器给的是本地文件路径，默认只允许AstrBot临时目录。读取失败时，可将适配器专用的图片缓存目录加入 `trusted_image_dirs`；不要添加整盘、整个用户目录或存放凭据的目录。

## 七、退出、切回与反馈

```powershell
python manage.py logout
```

会尝试官方撤销刷新会话并清除本地Token，保留账号/client注册映射。若提示远端撤销未确认，请在 ChatGPT 设置中断开本应用。账号用量请看：https://chatgpt.com/settings/usage 。插件不会猜测套餐何时重置，不会绕过用量限制。

随时关闭自动接管，或把 backend 改为 existing，或停用本插件。无需删除原有功能。自动流程需要内置Agent的该钩子；使用Dify/扣子等第三方执行器时先用显式命令测试。

请反馈：AstrBot版本、QQ适配器与旧转述插件名称/版本、失败在哪一步（登录/权限/模型目录/校验图/QQ）、固定错误提示、是否重复回复。不要提供令牌、完整授权URL、oauth-state.bin、私人截图、原始请求响应或代理密码。`test-results.txt` 是假数据离线测试结果，可正常分享。

## 离线测试及官方依据

使用已有依赖，在插件目录运行：`python -m unittest discover -s tests -v`。测试使用假令牌、生成图片、模拟QQ事件和响应；不登录真实账号。

- https://developers.openai.com/siwc/token-sharing-open-source/sign-in
- https://developers.openai.com/siwc/token-sharing-open-source/profiles-and-sessions
- https://developers.openai.com/siwc/token-sharing-open-source/models-and-inference
- https://developers.openai.com/siwc/token-sharing-open-source/preview-limitations
- https://docs.astrbot.app/dev/star/guides/listen-message-event.html
- https://docs.astrbot.app/dev/star/guides/ai.html

## 许可证

本项目采用 [MIT License](LICENSE)。允许使用、修改、分发和商用，须保留版权及许可声明；软件按原样提供，不作担保。第三方依赖遵循各自许可证。

## v0.1.5 数据目录修正

修正版显式使用 StarTools.get_data_dir("astrbot_plugin_chatgpt_vision")，与 manage.py 默认目录一致。授权文件只保存在 data/plugin_data/astrbot_plugin_chatgpt_vision/，不读取或修改共享根目录的文件及权限。旧版若曾将文件写到共享根目录，请先停用插件、备份，再由管理员将确认属于本插件的 oauth-state.bin 移入该专属目录；POSIX目录权限为0700、文件为0600。不要搬动运行中的锁文件，不要递归修改共享根目录权限。如原本已在专属目录授权，无需迁移。
