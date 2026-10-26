"""Run with AstrBot's Python, on the server, as the same operating-system user."""
from __future__ import annotations

import argparse
import asyncio
import json
import sys
import webbrowser
from pathlib import Path

import aiohttp
from aiohttp import web

from vision.client import VisionClient
from vision.errors import VisionError
from vision.oauth import OAuth, new_transaction
from vision.network import RoutedSession
from vision.storage import Store


async def login(oauth: OAuth, port: int, account_id: str | None, no_browser=False, new_account=False):
    state = oauth.store.load()
    account = state["accounts"].get(account_id) if account_id else (
        None if new_account else state["accounts"].get(state.get("active")))
    if account is None and not new_account and state.get("pending_registration"):
        account = {"client_id": state["pending_registration"]}
    if account_id and not account:
        raise VisionError("auth_invalid")
    finished = asyncio.get_running_loop().create_future()
    transaction = None

    async def callback(request):
        if transaction is None or finished.done():
            return web.Response(status=400, text="This sign-in attempt is no longer active.")
        if request.host != f"127.0.0.1:{port}" or request.remote != "127.0.0.1":
            return web.Response(status=400, text="Invalid callback.")
        if any(len(request.query.getall(k)) != 1 for k in request.query):
            return web.Response(status=400, text="Invalid callback.")
        try:
            result = await oauth.finish(transaction, dict(request.query))
            finished.set_result(result)
            text = "Sign-in verified. You can close this page and return to the server terminal."
            status = 200
        except VisionError as exc:
            if not transaction["used"]:
                # An unsolicited callback must not cancel the user's valid attempt.
                return web.Response(status=400, text="Invalid callback.")
            finished.set_exception(exc)
            text, status = "Sign-in could not be verified. Return to the server terminal.", 400
        except Exception:
            finished.set_exception(VisionError("auth_invalid"))
            text, status = "Sign-in could not be verified.", 400
        return web.Response(text=text, status=status, headers={"Cache-Control": "no-store",
                            "Referrer-Policy": "no-referrer", "Content-Security-Policy": "default-src 'none'"})

    app = web.Application(client_max_size=16384)
    app.router.add_get("/auth/callback", callback)
    runner = web.AppRunner(app, access_log=None)
    await runner.setup()
    try:
        await web.TCPSite(runner, "127.0.0.1", port).start()
        transaction = new_transaction(await oauth.store.host_id(), f"http://127.0.0.1:{port}/auth/callback", account)
        print("Continue with ChatGPT：请在服务器本机浏览器完成官方授权。")
        if no_browser:
            # Fresh authorization URL contains no access/refresh/ID token.
            print(transaction["authorization_url"])
        else:
            webbrowser.open(transaction["authorization_url"])
        identity = await asyncio.wait_for(finished, timeout=300)
        print("身份验证通过。账号记录：" + identity)
        current = oauth.store.active(oauth.store.load())
        print("实际推理授权：" + ("已授予" if {"resource.invoke", "chatgpt.tokens.use.direct"}.issubset(set(current["scopes"])) else "未授予；不会发起识图请求"))
    except (asyncio.TimeoutError, OSError):
        raise VisionError("auth_invalid") from None
    finally:
        await runner.cleanup()


async def run(args):
    store = Store(Path(args.data_dir))
    proxy = args.proxy
    if proxy is None:
        # Read only this new plugin's own public network setting; never other app credentials.
        config_path = store.directory.parents[1] / "config" / "astrbot_plugin_chatgpt_vision_config.json"
        try:
            proxy = json.loads(config_path.read_text(encoding="utf-8-sig")).get("openai_proxy", "")
        except FileNotFoundError:
            proxy = ""
        except Exception:
            raise VisionError("proxy_config") from None
    async with RoutedSession(proxy) as session:
        oauth = OAuth(store, session)
        client = VisionClient(oauth, args.timeout)
        if args.command == "login":
            await login(oauth, args.port, args.account, args.no_browser, args.new_account)
        elif args.command == "status":
            state = store.load()
            records = []
            for identity, a in state["accounts"].items():
                records.append({"account": identity, "active": state.get("active") == identity,
                                "logged_in": bool(a.get("access_token")),
                                "plan_authorized": {"resource.invoke", "chatgpt.tokens.use.direct"}.issubset(set(a.get("scopes", []))),
                                "selected_model": a.get("selected_model"),
                                "verification_required": True if not a.get("selected_model") else
                                __import__('time').time() - a.get("verified_models", {}).get(a["selected_model"], 0) > 86400})
            print(json.dumps({"data_directory": str(store.directory), "accounts": records}, ensure_ascii=False, indent=2))
        elif args.command == "use-account":
            async with store.transaction() as state:
                if args.value not in state["accounts"]:
                    raise VisionError("auth_invalid")
                state["active"] = args.value
                # Reconfirm visual access when switching accounts.
                state["accounts"][args.value].pop("selected_model", None)
                state["accounts"][args.value].pop("verified_models", None)
                store.save(state)
            print("已切换账号，请重新验证并选择视觉模型。")
        elif args.command == "models":
            models, _ = await diagnostic_call(client, args, client.models())
            print(json.dumps(models, ensure_ascii=False, indent=2))
        elif args.command == "verify-model":
            await diagnostic_call(client, args, client.verify_model(args.value))
            print("视觉校验已完整成功；仅使用本机随机生成的校验图。")
        elif args.command == "check-text":
            await diagnostic_call(client, args, client.check_text(args.value))
            print("纯文本Responses请求已完整成功；这一步不代表视觉能力已验证。")
        elif args.command == "self-test":
            labels = {"text": "1/3 检查账号目录与纯文本Responses请求……",
                      "vision": "2/3 检查随机生成图片的实际视觉请求……",
                      "select": "3/3 保存已通过视觉验证的模型……"}
            await diagnostic_call(client, args, client.self_test(args.value, lambda phase: print(labels[phase], flush=True)))
            print("完整自检通过，视觉模型已选好。接下来可用QQ的 /OAuth识图 测试。")
        elif args.command == "select-model":
            await diagnostic_call(client, args, client.select(args.value))
            print("已选择已验证的账号模型。")
        elif args.command == "logout":
            print("本地令牌已清除。" + ("远端会话撤销已确认。" if await oauth.logout() else
                  "远端撤销未确认，请在 ChatGPT 设置中断开本应用。"))


async def diagnostic_call(client, args, operation):
    try:
        return await operation
    finally:
        if args.diagnostic:
            print("安全诊断（不含Token、图片、原始响应或代理地址）：")
            print(json.dumps(client.diagnostic, ensure_ascii=False, indent=2), flush=True)


def default_data_dir(plugin_dir):
    return Path(plugin_dir).resolve().parents[1] / 'plugin_data' / 'astrbot_plugin_chatgpt_vision'


def main():
    parser = argparse.ArgumentParser(description="AstrBot official ChatGPT OAuth vision management")
    plugin_dir = Path(__file__).resolve().parent
    parser.add_argument("--data-dir", default=str(default_data_dir(plugin_dir)))
    parser.add_argument("--proxy", default=None, help="Override this command's HTTP(S) proxy. Empty string uses system proxy discovery.")
    parser.add_argument("--timeout", type=int, default=180, help="Total request timeout in seconds, bounded to 10..300")
    parser.add_argument("--diagnostic", action="store_true", help="Print allowlisted diagnostic metadata, never raw server content")
    sub = parser.add_subparsers(dest="command", required=True)
    auth = sub.add_parser("login")
    auth.add_argument("--port", type=int, default=14558)
    auth.add_argument("--account", help="Saved account record ID for reauthorization")
    auth.add_argument("--new-account", action="store_true", help="Explicitly register a different ChatGPT account")
    auth.add_argument("--no-browser", action="store_true")
    for name in ("status", "models", "logout"):
        sub.add_parser(name)
    for name in ("verify-model", "select-model", "use-account", "check-text", "self-test"):
        sub.add_parser(name).add_argument("value")
    args = parser.parse_args()
    try:
        asyncio.run(run(args))
    except KeyboardInterrupt:
        print("操作已取消。", file=sys.stderr)
        return 1
    except VisionError as exc:
        print(str(exc) + "（" + exc.code + "）", file=sys.stderr)
        return 1
    except Exception:
        print("操作未完成；为保护凭据，不输出原始异常。请检查端口、网络及服务账号。", file=sys.stderr)
        return 1
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
