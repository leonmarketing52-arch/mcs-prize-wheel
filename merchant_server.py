#!/usr/bin/env python3
"""Local merchant dashboard and GitHub publisher. Uses the owner's existing gh login."""
import argparse
import base64
import binascii
import datetime
import hmac
import json
import math
import mimetypes
import os
from pathlib import Path
import re
import secrets
import shutil
import subprocess
import sys
import tempfile
import threading
import urllib.error
import urllib.parse
import urllib.request
import webbrowser
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer

ROOT = Path(__file__).resolve().parent
HOST = "127.0.0.1"
PORT = 8765
ORIGIN = "http://%s:%s" % (HOST, PORT)
REPO = "leonmarketing52-arch/mcs-prize-wheel"
BRANCH = "main"
PUBLIC_URL = "https://leonmarketing52-arch.github.io/mcs-prize-wheel/"
MAX_BODY = 3 * 1024 * 1024
CSRF_TOKEN = secrets.token_urlsafe(32)
PUBLISH_LOCK = threading.Lock()
COLORS = ["#f06a51", "#f4b85f", "#80bd91", "#f7dfba", "#84c5bd", "#f39175", "#bd9bd7", "#f4d377"]
STATIC_EXTENSIONS = {".html", ".css", ".js", ".json", ".png", ".jpg", ".jpeg", ".webp", ".gif", ".svg", ".ico", ".woff", ".woff2"}
IMAGE_PATTERN = re.compile(r"data:image/(webp|png|jpeg);base64,([A-Za-z0-9+/]*={0,2})")
SHA_PATTERN = re.compile(r"[0-9a-f]{40}")


class PublishError(Exception):
    def __init__(self, message, status=502):
        super().__init__(message)
        self.status = status


def canonical_color(value, index):
    if value is None:
        return COLORS[index % len(COLORS)]
    if not isinstance(value, str):
        raise ValueError("奖项颜色格式无效")
    value = value.strip().lower()
    if re.fullmatch(r"#[0-9a-f]{6}", value):
        return value
    if re.fullmatch(r"#[0-9a-f]{3}", value):
        return "#" + "".join(c * 2 for c in value[1:])
    match = re.fullmatch(r"rgb\(\s*(\d{1,3})\s*,\s*(\d{1,3})\s*,\s*(\d{1,3})\s*\)", value)
    if match:
        channels = tuple(int(c) for c in match.groups())
        if all(0 <= c <= 255 for c in channels):
            return "#%02x%02x%02x" % channels
    raise ValueError("奖项颜色需要使用 HEX 或 RGB 格式")


def validate_prizes(values):
    if not isinstance(values, list) or not 2 <= len(values) <= 24:
        raise ValueError("请设置 2 至 24 个奖项")
    prizes = []
    for index, value in enumerate(values):
        if not isinstance(value, dict):
            raise ValueError("奖项格式无效")
        name = value.get("name")
        if not isinstance(name, str) or not name.strip() or len(name.strip()) > 80:
            raise ValueError("奖项名称不能为空，且最多为 80 个字符")
        chance = value.get("chance")
        if isinstance(chance, bool) or not isinstance(chance, (int, float)) or not 0 <= chance <= 100 or not math.isfinite(chance):
            raise ValueError("中奖概率需要为 0 至 100 的数字")
        prize = {"name": name.strip(), "chance": chance, "color": canonical_color(value.get("color"), index)}
        image = value.get("image")
        if image not in (None, ""):
            if not isinstance(image, str):
                raise ValueError("产品图片格式无效")
            match = IMAGE_PATTERN.fullmatch(image)
            if not match:
                raise ValueError("产品图片只支持上传的 PNG、JPEG 或 WebP 图片")
            try:
                raw = base64.b64decode(match.group(2), validate=True)
            except (ValueError, binascii.Error):
                raise ValueError("产品图片数据不完整")
            kind = match.group(1)
            valid = ((kind == "png" and raw.startswith(b"\x89PNG\r\n\x1a\n"))
                     or (kind == "jpeg" and raw.startswith(b"\xff\xd8\xff"))
                     or (kind == "webp" and len(raw) >= 12 and raw[:4] == b"RIFF" and raw[8:12] == b"WEBP"))
            if not valid:
                raise ValueError("产品图片格式与内容不符")
            prize["image"] = image
        prizes.append(prize)
    if abs(sum(p["chance"] for p in prizes) - 100) > 0.001:
        raise ValueError("中奖概率合计需要等于 100%")
    if len(json.dumps(prizes, ensure_ascii=False).encode("utf-8")) > MAX_BODY:
        raise ValueError("奖项和图片数据不能超过 3 MB")
    return prizes


def gh_executable():
    path = shutil.which("gh")
    if not path:
        for candidate in ("/opt/homebrew/bin/gh", "/usr/local/bin/gh"):
            if Path(candidate).is_file():
                path = candidate
                break
    if not path:
        raise PublishError("没有找到 GitHub CLI，请在商家电脑安装 gh 后再发布", 503)
    return path


def github_request(endpoint, method="GET", body=None, allow_missing=False):
    args = [gh_executable(), "api", "--hostname", "github.com", "--method", method, endpoint]
    data = None
    if body is not None:
        args += ["--input", "-"]
        data = json.dumps(body, ensure_ascii=False)
    try:
        result = subprocess.run(args, input=data, text=True, capture_output=True, timeout=35, cwd=str(ROOT))
    except (OSError, subprocess.TimeoutExpired):
        raise PublishError("GitHub 连接超时或不可用，请稍后重新尝试")
    if result.returncode:
        if allow_missing and "HTTP 404" in result.stderr:
            return None
        if "HTTP 409" in result.stderr or "HTTP 422" in result.stderr:
            raise PublishError("GitHub 文件已被其他操作更新，请重新发布；本机草稿已保留", 409)
        raise PublishError("无法完成 GitHub 操作，请确认网络、gh 登录状态和仓库写入权限")
    try:
        return json.loads(result.stdout)
    except ValueError:
        raise PublishError("GitHub 返回的结果无法读取，请稍后检查发布状态")


def write_local_campaign(encoded):
    temporary = None
    try:
        with tempfile.NamedTemporaryFile(mode="wb", prefix=".campaign-", suffix=".tmp", dir=str(ROOT), delete=False) as stream:
            temporary = stream.name
            stream.write(encoded)
            stream.flush()
            os.fsync(stream.fileno())
        os.replace(temporary, str(ROOT / "campaign.json"))
    finally:
        if temporary and os.path.exists(temporary):
            os.unlink(temporary)


def publish_campaign(prizes):
    campaign = {"version": 1, "updatedAt": datetime.datetime.now(datetime.timezone.utc).isoformat().replace("+00:00", "Z"), "prizes": prizes}
    encoded = (json.dumps(campaign, ensure_ascii=False, indent=2) + "\n").encode("utf-8")
    endpoint = "repos/%s/contents/campaign.json" % REPO
    existing = github_request(endpoint + "?ref=" + BRANCH, allow_missing=True)
    body = {"message": "Publish prize wheel campaign", "branch": BRANCH, "content": base64.b64encode(encoded).decode("ascii")}
    if existing:
        if existing.get("type") != "file" or not existing.get("sha"):
            raise PublishError("GitHub 的活动配置路径不是可更新的文件", 409)
        body["sha"] = existing["sha"]
    result = github_request(endpoint, method="PUT", body=body)
    commit = result.get("commit", {}).get("sha")
    if not isinstance(commit, str) or not SHA_PATTERN.fullmatch(commit):
        raise PublishError("GitHub 已接受请求，但没有返回发布编号，请先检查网站状态")
    reply = {"ok": True, "commit": commit, "url": PUBLIC_URL, "state": "building", "updatedAt": campaign["updatedAt"]}
    try:
        write_local_campaign(encoded)
    except OSError:
        reply["warning"] = "GitHub 已接收更新，但无法保存本机 campaign.json；请保留浏览器中的草稿"
    return reply


class Handler(BaseHTTPRequestHandler):
    server_version = "MCSLocalPublisher/1.0"

    def log_message(self, format, *args):
        # Request bodies and GitHub credentials are deliberately never logged.
        sys.stderr.write("%s %s\n" % (self.command, urllib.parse.urlsplit(self.path).path))

    def allowed_request(self):
        if self.headers.get("Host") != "%s:%s" % (HOST, PORT):
            self.respond(403, {"error": "仅允许从本机商家后台访问"})
            return False
        origin = self.headers.get("Origin")
        if origin and origin != ORIGIN:
            self.respond(403, {"error": "不允许外部网页访问本机发布服务"})
            return False
        if self.path.startswith("/api/") and self.headers.get("Sec-Fetch-Site") == "cross-site":
            self.respond(403, {"error": "请从本机商家后台执行此操作"})
            return False
        return True

    def respond(self, status, value):
        raw = json.dumps(value, ensure_ascii=False).encode("utf-8")
        self.send_response(status)
        self.send_header("Content-Type", "application/json; charset=utf-8")
        self.send_header("Content-Length", str(len(raw)))
        self.send_header("Cache-Control", "no-store")
        self.send_header("X-Content-Type-Options", "nosniff")
        self.send_header("X-Frame-Options", "DENY")
        self.end_headers()
        self.wfile.write(raw)

    def do_OPTIONS(self):
        self.respond(403, {"error": "跨域访问未启用"})

    def do_GET(self):
        if not self.allowed_request():
            return
        parsed = urllib.parse.urlsplit(self.path)
        if parsed.path == "/api/status":
            self.respond(200, {"publisher": True, "csrfToken": CSRF_TOKEN, "repo": REPO, "url": PUBLIC_URL})
            return
        if parsed.path == "/api/deployment":
            commit = urllib.parse.parse_qs(parsed.query).get("commit", [""])[0]
            if not SHA_PATTERN.fullmatch(commit):
                self.respond(400, {"error": "发布编号无效"})
                return
            try:
                build = github_request("repos/%s/pages/builds/latest" % REPO, allow_missing=True)
                state = "building"
                if build and build.get("commit") == commit:
                    status = build.get("status")
                    state = "built" if status == "built" else "failed" if status == "errored" else "building"
                self.respond(200, {"ok": True, "state": state, "commit": commit, "url": PUBLIC_URL})
            except PublishError as error:
                self.respond(error.status, {"error": str(error)})
            return
        if parsed.path.startswith("/api/"):
            self.respond(404, {"error": "接口不存在"})
            return
        relative = urllib.parse.unquote(parsed.path).lstrip("/") or "index.html"
        parts = Path(relative).parts
        if not parts or any(part.startswith(".") for part in parts):
            self.respond(404, {"error": "文件不存在"})
            return
        target = (ROOT / relative).resolve()
        allowed_name = relative in {"index.html", "campaign.json", "favicon.ico"} or (parts[0] in {"assets", "images"} and target.suffix.lower() in STATIC_EXTENSIONS)
        if not allowed_name or ROOT not in target.parents or not target.is_file():
            self.respond(404, {"error": "文件不存在"})
            return
        try:
            raw = target.read_bytes()
        except OSError:
            self.respond(404, {"error": "文件无法读取"})
            return
        self.send_response(200)
        content_type = mimetypes.guess_type(str(target))[0] or "application/octet-stream"
        self.send_header("Content-Type", content_type + ("; charset=utf-8" if content_type.startswith("text/") or content_type == "application/json" else ""))
        self.send_header("Content-Length", str(len(raw)))
        self.send_header("Cache-Control", "no-store")
        self.send_header("X-Content-Type-Options", "nosniff")
        self.send_header("X-Frame-Options", "DENY")
        self.end_headers()
        self.wfile.write(raw)

    def do_POST(self):
        if not self.allowed_request():
            return
        if urllib.parse.urlsplit(self.path).path != "/api/publish":
            self.respond(404, {"error": "接口不存在"})
            return
        if self.headers.get_content_type() != "application/json" or self.headers.get("Transfer-Encoding"):
            self.respond(415, {"error": "发布内容必须为 JSON"})
            return
        try:
            length = int(self.headers.get("Content-Length", "0"))
        except ValueError:
            length = 0
        if length <= 0 or length > MAX_BODY:
            self.respond(413, {"error": "发布内容不能为空，且不能超过 3 MB"})
            return
        try:
            self.connection.settimeout(15)
            payload = json.loads(self.rfile.read(length).decode("utf-8"))
            if not isinstance(payload, dict):
                raise ValueError("发布内容格式无效")
            token = payload.get("csrfToken", "")
            if not isinstance(token, str) or not hmac.compare_digest(token, CSRF_TOKEN):
                self.respond(403, {"error": "发布验证已过期，请刷新本机商家后台"})
                return
            prizes = validate_prizes(payload.get("prizes"))
        except (ValueError, UnicodeError, OSError) as error:
            self.respond(400, {"error": str(error) if isinstance(error, ValueError) and not isinstance(error, json.JSONDecodeError) else "发布内容无法读取"})
            return
        if not PUBLISH_LOCK.acquire(blocking=False):
            self.respond(409, {"error": "正在发布上一份设置，请等待完成"})
            return
        try:
            self.respond(200, publish_campaign(prizes))
        except PublishError as error:
            self.respond(error.status, {"error": str(error)})
        except (OSError, ValueError, TypeError):
            self.respond(500, {"error": "发布服务暂时出错，本机草稿仍然保留"})
        finally:
            PUBLISH_LOCK.release()


def main():
    parser = argparse.ArgumentParser(description="MCS 本机商家后台")
    parser.add_argument("--open", action="store_true", help="打开浏览器中的商家后台")
    options = parser.parse_args()
    dashboard = ORIGIN + "/?view=dashboard"
    try:
        server = ThreadingHTTPServer((HOST, PORT), Handler)
        server.daemon_threads = True
    except OSError:
        # Reuse only this exact helper; never stop or replace an unknown process.
        try:
            with urllib.request.urlopen(ORIGIN + "/api/status", timeout=2) as response:
                status = json.load(response)
            if status.get("publisher") is True and status.get("repo") == REPO:
                print("商家后台已经运行：" + dashboard)
                if options.open:
                    webbrowser.open(dashboard)
                return 0
        except (OSError, ValueError):
            pass
        print("端口 8765 正在被其他程序使用。请关闭占用该端口的程序后重试。", file=sys.stderr)
        return 1
    print("商家后台：" + dashboard, flush=True)
    print("请保留此终端窗口。按 Control+C 可停止本机服务。", flush=True)
    if options.open:
        threading.Timer(0.3, lambda: webbrowser.open(dashboard)).start()
    try:
        server.serve_forever()
    except KeyboardInterrupt:
        print("\n本机商家后台已停止。")
    finally:
        server.server_close()
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
