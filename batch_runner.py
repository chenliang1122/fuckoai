from __future__ import annotations

import argparse
import json
import os
import signal
import sys
import time
from pathlib import Path
from typing import Any
from urllib.error import HTTPError, URLError
from urllib.parse import urlparse
from urllib.request import Request, urlopen

ROOT = Path(__file__).resolve().parent
DEFAULT_API_BASE = "http://127.0.0.1:3030"

PHASE_LABELS: dict[str, str] = {
    "idle": "空闲",
    "running": "运行中",
    "stopping": "停止中",
    "stopped": "已停止",
    "done": "已完成",
}
STEP_LABELS: dict[str, str] = {
    "preparing_email": "准备邮箱",
    "buying_phone": "购买号码",
    "waiting_sms": "等待短信验证码",
    "filling_phone": "请在浏览器中填写手机号",
    "filling_sms_code": "请在浏览器中填写短信验证码",
    "filling_password": "请在浏览器中填写密码",
    "filling_account_details": "请在浏览器中填写姓名年龄",
    "filling_email": "请在浏览器中填写邮箱",
    "waiting_email_code": "等待邮箱验证码",
    "filling_email_code": "请在浏览器中填写邮箱验证码",
    "completing": "完成当前注册",
    "canceling": "取消当前号码",
    "stopping": "正在停止",
    "waiting_next": "等待下一个",
}


def load_env_file(path: Path) -> None:
    if not path.exists():
        return
    for raw_line in path.read_text(encoding="utf-8").splitlines():
        line = raw_line.strip()
        if not line or line.startswith("#") or "=" not in line:
            continue
        key, value = line.split("=", 1)
        key = key.strip()
        value = value.strip()
        if key and key not in os.environ:
            os.environ[key] = value


load_env_file(ROOT / ".env")


def request_json(method: str, url: str, body: dict[str, Any] | None = None, timeout: int = 10) -> Any:
    headers = {"Accept": "application/json"}
    data = None
    if body is not None:
        data = json.dumps(body).encode("utf-8")
        headers["Content-Type"] = "application/json"
    request = Request(url, data=data, method=method, headers=headers)
    with urlopen(request, timeout=timeout) as response:
        raw = response.read().decode("utf-8", errors="replace")
    return json.loads(raw) if raw else {}


def check_api_health(api_base: str) -> dict[str, Any] | None:
    try:
        return request_json("GET", f"{api_base}/api/health")
    except (HTTPError, URLError, OSError, json.JSONDecodeError):
        return None


def format_state_line(state: dict[str, Any]) -> str:
    phase = PHASE_LABELS.get(state.get("phase", ""), state.get("phase", "idle"))
    progress = f"{state.get('completed', 0)}/{state.get('total', 0)}"
    step = STEP_LABELS.get(state.get("currentStep", ""), state.get("currentStep", "--"))
    email = state.get("currentEmail", "") or "--"
    phone = state.get("currentPhone", "") or "--"
    results = f"OK:{state.get('success', 0)} SKIP:{state.get('skipped', 0)} FAIL:{state.get('failed', 0)}"
    return f"[{phase}] 进度 {progress} | 邮箱 {email} | 手机 {phone} | 步骤 {step} | {results}"


def print_logs(logs: list[dict[str, str]], last_printed_index: int) -> int:
    for i in range(last_printed_index, len(logs)):
        entry = logs[i]
        level = entry.get("level", "info")
        prefix = {"error": "❌", "warn": "⚠️ ", "info": "  "}.get(level, "  ")
        timestamp = entry.get("time", "")[-8:] or "--:--:--"
        print(f"{prefix} [{timestamp}] {entry.get('message', '')}")
    return len(logs)


def main() -> None:
    parser = argparse.ArgumentParser(description="GPT Reg 批量注册 CLI 启动器")
    parser.add_argument("--api-base", default=DEFAULT_API_BASE, help=f"API 地址, 默认 {DEFAULT_API_BASE}")
    parser.add_argument("--stop", action="store_true", help="停止正在运行的批次")
    parser.add_argument("--status", action="store_true", help="仅查看当前批次状态后退出")
    parser.add_argument("--max-sms-attempts", type=int, default=6, help="短信验证码最大轮询次数 (默认 6)")
    parser.add_argument("--sms-poll-interval", type=int, default=15, help="短信验证码轮询间隔秒 (默认 15)")
    parser.add_argument("--max-email-attempts", type=int, default=3, help="邮箱验证码最大轮询次数 (默认 3)")
    parser.add_argument("--email-poll-interval", type=int, default=10, help="邮箱验证码轮询间隔秒 (默认 10)")
    parser.add_argument("--password", default=os.getenv("SIGNUP_PASSWORD", ""), help="注册密码")
    parser.add_argument("--name", default=os.getenv("SIGNUP_NAME", ""), help="注册姓名")
    parser.add_argument("--age", default=os.getenv("SIGNUP_AGE", ""), help="注册年龄")
    args = parser.parse_args()

    api_base = args.api_base.rstrip("/")

    # Check server health
    health = check_api_health(api_base)
    if not health:
        print(f"❌ 无法连接 API 服务 {api_base}, 请先启动 server.py")
        sys.exit(1)
    print(f"✅ API 服务已连接: {api_base}")

    # --stop
    if args.stop:
        print("正在发送停止请求...")
        try:
            result = request_json("POST", f"{api_base}/api/batch/stop", timeout=5)
            state = result.get("batchState", result)
            print(f"停止结果: {state.get('phase', '?')}")
            print(format_state_line(state))
        except Exception as exc:
            print(f"❌ 停止请求失败: {exc}")
            sys.exit(1)
        return

    # --status (one-shot)
    if args.status:
        try:
            payload = request_json("GET", f"{api_base}/api/batch/status")
            state = payload.get("batchState", payload)
            print(format_state_line(state))
            if state.get("running"):
                print("批次正在运行中, 日志:")
                logs = request_json("GET", f"{api_base}/api/batch/logs").get("logs", [])
                print_logs(logs, 0)
        except Exception as exc:
            print(f"❌ 查询状态失败: {exc}")
            sys.exit(1)
        return

    # Start batch
    # First check if already running
    try:
        status_payload = request_json("GET", f"{api_base}/api/batch/status")
        current_state = status_payload.get("batchState", status_payload)
        if current_state.get("running"):
            print("⚠️  批次已在运行中")
            print(format_state_line(current_state))
            print("使用 --stop 停止当前批次, 或使用 --status 查看状态")
            sys.exit(1)
    except Exception:
        pass

    print("正在启动批量注册...")
    body: dict[str, Any] = {
        "maxSmsAttempts": args.max_sms_attempts,
        "smsPollInterval": args.sms_poll_interval,
        "maxEmailAttempts": args.max_email_attempts,
        "emailPollInterval": args.email_poll_interval,
    }
    if args.password:
        body["password"] = args.password
    if args.name:
        body["name"] = args.name
    if args.age:
        body["age"] = args.age

    try:
        start_payload = request_json("POST", f"{api_base}/api/batch/start", body, timeout=5)
    except Exception as exc:
        print(f"❌ 启动批量注册失败: {exc}")
        sys.exit(1)

    if "error" in start_payload:
        print(f"❌ {start_payload['error']}")
        sys.exit(1)

    state = start_payload.get("batchState", start_payload)
    email_queue = start_payload.get("emailQueue", {})
    total = len(email_queue.get("emails", []))
    print(f"✅ 批量注册已启动 ({total} 个邮箱)")
    print(format_state_line(state))
    print()
    print("实时日志 (Ctrl+C 停止):")
    print("-" * 60)

    last_log_index = 0
    running = True

    def handle_stop(signum, frame):
        nonlocal running
        print("\n正在发送停止请求...")
        try:
            request_json("POST", f"{api_base}/api/batch/stop", timeout=5)
        except Exception:
            pass
        running = False

    signal.signal(signal.SIGINT, handle_stop)
    signal.signal(signal.SIGTERM, handle_stop)

    while running:
        try:
            status_payload = request_json("GET", f"{api_base}/api/batch/status", timeout=5)
            state = status_payload.get("batchState", status_payload)
        except Exception:
            time.sleep(3)
            continue

        # Print new log entries
        try:
            logs_payload = request_json("GET", f"{api_base}/api/batch/logs", timeout=5)
            new_logs = logs_payload.get("logs", [])
            last_log_index = print_logs(new_logs, last_log_index)
        except Exception:
            pass

        if not state.get("running"):
            print("-" * 60)
            print(format_state_line(state))
            print(f"批次已结束: {PHASE_LABELS.get(state.get('phase', ''), state.get('phase', ''))}")
            break

        time.sleep(3)


if __name__ == "__main__":
    main()
