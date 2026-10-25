"""Only fixed, non-sensitive diagnostics may cross plugin boundaries."""

MESSAGES = {
    "login_required": "唔，还没登录识图账号，请让管理员在服务器完成授权。",
    "permission": "这个账号暂时没有所需权限，请管理员检查授权范围。",
    "quota": "唔，识图额度或频率到上限了，稍后再试吧。",
    "unavailable": "识图服务暂时忙不过来，稍后再试吧。",
    "network": "图片还没看完，连接就断了，稍后再试吧。",
    "image": "唔，这张图暂时读不了，请换成普通 PNG 或 JPG 再试。",
    "image_limit": "图片有点太大或太多了，请分开发小一些的图。",
    "image_source": "这张图片的来源暂时不能安全读取，请重新发送图片。",
    "model": "请管理员先读取账号模型列表，再验证并选择视觉模型。",
    "vision_unconfirmed": "这个模型的视觉能力还没有验证通过。",
    "incomplete": "这次图片没有完整看完，请稍后再试。",
    "refused": "这张图我暂时不能分析，换个问题试试吧。",
    "protocol": "识图接口的回复格式暂时不兼容，请管理员检查。",
    "auth_invalid": "登录验证没有通过，请在服务器重新发起授权。",
    "storage": "识图凭据暂时不能读取，请检查服务用户和文件权限。",
    "busy": "唔，上一张图还没看完，稍等一下再发。",
    "internal": "识图遇到一点问题，请管理员检查插件状态。",
}


class VisionError(Exception):
    def __init__(self, code: str):
        self.code = code if code in MESSAGES else "internal"
        super().__init__(MESSAGES[self.code])


def http_error(status: int, code: str = "") -> VisionError:
    if status == 401:
        return VisionError("login_required")
    if status == 429 or code == "subscription_sharing_usage_limit_exceeded":
        return VisionError("quota")
    if status == 403:
        return VisionError("permission")
    if status >= 500:
        return VisionError("unavailable")
    if code == "subscription_sharing_unsupported_capability":
        return VisionError("vision_unconfirmed")
    return VisionError("protocol")
