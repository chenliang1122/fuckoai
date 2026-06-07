from __future__ import annotations

import argparse
import json
import os
import random
import re
import shutil
import string
import subprocess
import sys
import tempfile
import threading
import time
from pathlib import Path
from typing import Any
from urllib.error import HTTPError, URLError
from urllib.parse import quote
from urllib.request import Request, urlopen

try:
    import uiautomation as auto
except ImportError:
    auto = None


ROOT = Path(__file__).resolve().parent
SIGNUP_URL = "https://chatgpt.com/auth/login?intent=signup"
CALLBACK_PREFIX = "localhost:1455/auth/callback"
PASSWORD_URL = "https://auth.openai.com/create-account/password"
DEFAULT_EDGE = r"C:\Program Files (x86)\Microsoft\Edge\Application\msedge.exe"
DEFAULT_API_BASE = "http://127.0.0.1:3030"
UI_POLL_INTERVAL_SECONDS = 0.25
MAX_STUCK_PAGE_HITS = 6
MAX_FATAL_ERROR_REFRESH_ATTEMPTS = 5
SESSION_DIR_PREFIX = "codex-edge-automation-"


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


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(
        description=(
            "Drive a real Edge InPrivate window through ChatGPT phone signup and Codex OAuth. "
            "Phone numbers and verification codes can be pulled from the local API automatically."
        )
    )
    parser.add_argument("--phone", default=None, help="Phone number override. If omitted, the script buys a new one via the local API.")
    parser.add_argument(
        "--password",
        default=os.getenv("SIGNUP_PASSWORD"),
        help="Password to set on the OpenAI password page. Defaults to SIGNUP_PASSWORD from .env when set.",
    )
    parser.add_argument(
        "--name",
        default=os.getenv("SIGNUP_NAME"),
        help="Full name for the account details page. Defaults to SIGNUP_NAME from .env when set.",
    )
    parser.add_argument(
        "--age",
        default=os.getenv("SIGNUP_AGE"),
        help="Age for the account details page. Defaults to SIGNUP_AGE from .env when set.",
    )
    parser.add_argument("--email", default=None, help="Email override. If omitted, the script tries to create one via the local temp-mail API.")
    parser.add_argument(
        "--oauth-url",
        default=None,
        help="Optional Codex authorize URL override. If omitted, the script fetches one from the local Codex OAuth API.",
    )
    parser.add_argument("--edge-path", default=DEFAULT_EDGE, help=f"Path to Edge executable. Default: {DEFAULT_EDGE}")
    parser.add_argument("--api-base", default=DEFAULT_API_BASE, help=f"Base URL for the local API. Default: {DEFAULT_API_BASE}")
    parser.add_argument("--timeout", type=int, default=45, help="Max seconds to wait for each browser state.")
    parser.add_argument("--code-timeout", type=int, default=180, help="Legacy timeout option kept for compatibility.")
    parser.add_argument("--poll-interval", type=int, default=15, help="Legacy SMS polling interval option kept for compatibility.")
    parser.add_argument("--max-code-attempts", type=int, default=6, help="Max SMS polling attempts before canceling the phone and restarting with a new phone.")
    parser.add_argument("--phone-poll-interval", type=int, default=15, help="Polling interval in seconds for SMS verification codes.")
    parser.add_argument("--email-poll-interval", type=int, default=10, help="Polling interval in seconds for email verification codes.")
    parser.add_argument("--email-max-attempts", type=int, default=3, help="Max email polling attempts before skipping the current email.")
    parser.add_argument(
        "--close-on-success",
        action="store_true",
        help="Close the dedicated Edge InPrivate window before exiting after a successful run.",
    )
    return parser.parse_args()


def log(message: str) -> None:
    print(message, flush=True)


def summarize_json_payload(payload: Any, max_length: int = 1200) -> str:
    text = json.dumps(payload, ensure_ascii=False)
    if len(text) <= max_length:
        return text
    if isinstance(payload, dict):
        summary: dict[str, Any] = {"keys": sorted(payload.keys())}
        files = payload.get("files")
        if isinstance(files, list):
            summary["filesCount"] = len(files)
            names = []
            for item in files[:10]:
                if isinstance(item, dict):
                    names.append(str(item.get("name") or item.get("filename") or item.get("path") or "<unnamed>"))
                else:
                    names.append(str(item))
            summary["sampleFiles"] = names
        text = json.dumps(summary, ensure_ascii=False)
        if len(text) <= max_length:
            return text
    head = max(0, int(max_length * 0.75))
    tail = max(0, max_length - head)
    omitted = len(text) - head - tail
    return f"{text[:head]} ... [truncated {omitted} chars] ... {text[-tail:]}"


def prompt_value(label: str, current_value: str | None) -> str:
    if current_value:
        return current_value
    value = input(f"{label}: ").strip()
    while not value:
        value = input(f"{label}: ").strip()
    return value


def ensure_edge(edge_path: str) -> None:
    if not Path(edge_path).exists():
        raise FileNotFoundError(f"Edge not found: {edge_path}")


def launch_edge(edge_path: str, url: str, session_dir: Path) -> None:
    subprocess.Popen(
        [
            edge_path,
            f"--user-data-dir={session_dir}",
            "--inprivate",
            "--new-window",
            "--no-first-run",
            "--no-default-browser-check",
            url,
        ]
    )


def open_url_in_edge(edge_path: str, url: str, session_dir: Path) -> None:
    subprocess.Popen(
        [
            edge_path,
            f"--user-data-dir={session_dir}",
            "--inprivate",
            "--no-first-run",
            "--no-default-browser-check",
            url,
        ]
    )


def list_matching_windows() -> list[auto.Control]:
    matches: list[auto.Control] = []
    for window in auto.GetRootControl().GetChildren():
        try:
            name = window.Name
        except Exception:
            continue
        if "Edge" not in name or "InPrivate" not in name:
            continue
        if any(token in name for token in ("ChatGPT", "OpenAI", "localhost")):
            matches.append(window)
    return matches


def first_matching_window() -> auto.Control | None:
    matches = list_matching_windows()
    return matches[-1] if matches else None


def get_window_handle(window: auto.Control) -> int | None:
    try:
        handle = int(getattr(window, "NativeWindowHandle", 0) or 0)
    except Exception:
        return None
    return handle or None


def has_matching_window_handle(handle: int | None) -> bool:
    if handle is None:
        return bool(list_matching_windows())
    for candidate in list_matching_windows():
        if get_window_handle(candidate) == handle:
            return True
    return False


def wait_for_window(timeout_seconds: int) -> auto.Control:
    deadline = time.time() + timeout_seconds
    while time.time() < deadline:
        window = first_matching_window()
        if window is not None:
            return window
        time.sleep(UI_POLL_INTERVAL_SECONDS)
    raise TimeoutError("Timed out waiting for the dedicated Edge automation window.")


def activate(window: auto.Control) -> None:
    window.SetActive()
    time.sleep(0.3)


def read_title(window: auto.Control) -> str:
    try:
        return window.Name
    except Exception:
        return ""


def control_exists(control: auto.Control, timeout_seconds: int = 1) -> bool:
    try:
        return bool(control.Exists(timeout_seconds))
    except Exception:
        return False


def window_contains_text(window: auto.Control, needle: str) -> bool:
    stack = [window]
    while stack:
        control = stack.pop()
        try:
            name = control.Name
            if needle in str(name or ""):
                return True
        except Exception:
            pass
        try:
            stack.extend(control.GetChildren())
        except Exception:
            pass
    return False


def get_address_value(window: auto.Control) -> str:
    address = window.EditControl(Name="地址和搜索栏", searchDepth=20)
    if not control_exists(address, 2):
        return ""
    try:
        return address.GetValuePattern().Value
    except Exception:
        return ""


def wait_for_control(factory, timeout_seconds: int, description: str):
    deadline = time.time() + timeout_seconds
    while time.time() < deadline:
        try:
            control = factory()
            if control.Exists(1):
                return control
        except Exception:
            pass
        time.sleep(UI_POLL_INTERVAL_SECONDS)
    raise TimeoutError(f"Timed out waiting for {description}.")


def click_button_if_exists(window: auto.Control, name: str, delay_seconds: int = 2) -> bool:
    button = window.ButtonControl(Name=name, searchDepth=60)
    if not control_exists(button, 2):
        return False
    button.Click()
    time.sleep(delay_seconds)
    return True


def set_edit_value(edit: auto.Control, value: str) -> str:
    edit.Click()
    time.sleep(0.2)
    edit.GetValuePattern().SetValue(value)
    time.sleep(0.4)
    return edit.GetValuePattern().Value


def wait_for_page_progress(window: auto.Control, max_wait_seconds: float = 2.5) -> None:
    start_title = read_title(window)
    start_address = get_address_value(window)
    start_state = page_state(window)
    deadline = time.time() + max_wait_seconds
    while time.time() < deadline:
        time.sleep(UI_POLL_INTERVAL_SECONDS)
        if read_title(window) != start_title:
            return
        if get_address_value(window) != start_address:
            return
        if page_state(window) != start_state:
            return


def normalize_phone_digits(phone: str) -> str:
    return re.sub(r"\D", "", phone or "")


def as_signup_phone(phone: str) -> str:
    digits = normalize_phone_digits(phone)
    return f"+{digits}" if digits else phone


def request_json(method: str, url: str, body: dict[str, Any] | None = None) -> Any:
    headers = {"Accept": "application/json"}
    data = None
    if body is not None:
        data = json.dumps(body).encode("utf-8")
        headers["Content-Type"] = "application/json"
    request = Request(url, data=data, method=method, headers=headers)
    try:
        with urlopen(request, timeout=60) as response:
            raw = response.read().decode("utf-8", errors="replace")
    except HTTPError as error:
        text = error.read().decode("utf-8", errors="replace").strip()
        raise RuntimeError(f"HTTP {error.code} {text}")
    except URLError as error:
        raise RuntimeError(f"Connection failed: {error.reason}")
    return json.loads(raw) if raw else {}


def start_local_api_if_needed(api_base: str) -> None:
    try:
        request_json("GET", f"{api_base}/api/health")
        return
    except Exception:
        pass

    server_file = Path(__file__).with_name("server.py")
    if not server_file.exists():
        return

    creationflags = getattr(subprocess, "CREATE_NO_WINDOW", 0)
    subprocess.Popen(
        [sys.executable, str(server_file)],
        cwd=str(server_file.parent),
        creationflags=creationflags,
    )

    deadline = time.time() + 20
    while time.time() < deadline:
        try:
            request_json("GET", f"{api_base}/api/health")
            return
        except Exception:
            time.sleep(1)
    raise RuntimeError("Local API did not become ready after starting server.py")


def api_health(api_base: str) -> dict[str, Any]:
    return request_json("GET", f"{api_base}/api/health")


def api_purchase_phone(api_base: str) -> str:
    payload = request_json("POST", f"{api_base}/api/purchase", {})
    item = payload.get("item") or {}
    phone = str(item.get("phoneNumber") or "").strip()
    if not phone:
        raise RuntimeError("Local API purchase response did not include phoneNumber")
    return phone


def api_get_phone_code(api_base: str, phone: str) -> str | None:
    digits = normalize_phone_digits(phone)
    payload = request_json("GET", f"{api_base}/api/phones/{digits}/code")
    status = payload.get("status") or {}
    record = payload.get("record") or {}
    code = status.get("code") or record.get("lastCode")
    return str(code).strip() if code else None


def api_wait_for_phone_code(api_base: str, phone: str, timeout_seconds: int, poll_interval: int) -> str:
    deadline = time.time() + timeout_seconds
    while time.time() < deadline:
        code = api_get_phone_code(api_base, phone)
        if code:
            return code
        time.sleep(poll_interval)
    raise TimeoutError("Timed out waiting for the SMS verification code from the local API.")


def api_wait_for_phone_code_attempts(api_base: str, phone: str, max_attempts: int, poll_interval: int) -> str | None:
    for attempt in range(1, max_attempts + 1):
        code = api_get_phone_code(api_base, phone)
        if code:
            return code
        log(f"SMS code not received yet. Attempt {attempt}/{max_attempts}.")
        if attempt < max_attempts:
            time.sleep(poll_interval)
    return None


def api_cancel_phone(api_base: str, phone: str) -> dict[str, Any]:
    digits = normalize_phone_digits(phone)
    return request_json("POST", f"{api_base}/api/phones/{digits}/cancel", {})


def api_create_temp_mail(api_base: str) -> str:
    settings_payload = request_json("GET", f"{api_base}/api/temp-mail/settings")
    settings = settings_payload.get("settings") or {}
    domains = settings.get("defaultDomains") or settings.get("domains") or []
    if not domains:
        raise RuntimeError("Temp-mail settings did not expose any domains")
    domain = str(domains[0])
    name = "mail" + "".join(random.choices(string.ascii_lowercase + string.digits, k=10))
    request_json(
        "POST",
        f"{api_base}/api/temp-mail/address",
        {"name": name, "domain": domain, "enablePrefix": True},
    )
    return f"{name}@{domain}"


def api_ensure_temp_mail_address(api_base: str, address: str) -> str:
    normalized = str(address or "").strip()
    if "@" not in normalized:
        raise RuntimeError(f"Invalid email address: {normalized}")
    name, domain = normalized.split("@", 1)
    if not name or not domain:
        raise RuntimeError(f"Invalid email address: {normalized}")
    try:
        payload = request_json(
            "POST",
            f"{api_base}/api/temp-mail/address",
            {"name": name, "domain": domain, "enablePrefix": False},
        )
    except RuntimeError as error:
        message = str(error)
        if "Address already exists" in message:
            return normalized
        raise
    item = payload.get("item") or {}
    created_address = str(item.get("address") or normalized).strip()
    return created_address or normalized


def flatten_strings(value: Any) -> list[str]:
    items: list[str] = []
    if isinstance(value, dict):
        for item in value.values():
            items.extend(flatten_strings(item))
    elif isinstance(value, list):
        for item in value:
            items.extend(flatten_strings(item))
    elif value is not None:
        items.append(str(value))
    return items


def extract_six_digit_code(value: Any) -> str | None:
    text = "\n".join(flatten_strings(value))
    match = re.search(r"(?<!\d)(\d{6})(?!\d)", text)
    return match.group(1) if match else None


def api_latest_mail(api_base: str, address: str) -> dict[str, Any] | None:
    encoded = quote(address, safe="")
    payload = request_json("GET", f"{api_base}/api/temp-mail/address/{encoded}/mails/latest")
    return payload.get("item")


def api_wait_for_email_code(api_base: str, address: str, timeout_seconds: int, poll_interval: int) -> str:
    deadline = time.time() + timeout_seconds
    while time.time() < deadline:
        mail = api_latest_mail(api_base, address)
        code = extract_six_digit_code(mail)
        if code:
            return code
        time.sleep(poll_interval)
    raise TimeoutError("Timed out waiting for the email verification code from the temp-mail API.")


def api_wait_for_email_code_attempts(api_base: str, address: str, max_attempts: int, poll_interval: int) -> str | None:
    for attempt in range(1, max_attempts + 1):
        mail = api_latest_mail(api_base, address)
        code = extract_six_digit_code(mail)
        if code:
            return code
        log(f"Email code not received yet. Attempt {attempt}/{max_attempts}.")
        if attempt < max_attempts:
            time.sleep(poll_interval)
    return None


def api_get_codex_oauth_url(api_base: str) -> dict[str, Any]:
    payload = request_json("GET", f"{api_base}/api/codex-oauth/url")
    if not isinstance(payload, dict):
        raise RuntimeError("Codex OAuth URL API returned an unexpected payload")
    return payload


def api_submit_codex_callback(api_base: str, redirect_url: str) -> dict[str, Any]:
    payload = request_json(
        "POST",
        f"{api_base}/api/codex-oauth/callback",
        {"provider": "codex", "redirect_url": redirect_url},
    )
    if not isinstance(payload, dict):
        raise RuntimeError("Codex OAuth callback API returned an unexpected payload")
    return payload


def api_get_codex_status(api_base: str, state: str) -> dict[str, Any]:
    encoded_state = quote(state, safe="")
    payload = request_json("GET", f"{api_base}/api/codex-oauth/status?state={encoded_state}")
    if not isinstance(payload, dict):
        raise RuntimeError("Codex OAuth status API returned an unexpected payload")
    return payload


def api_get_codex_files(api_base: str) -> dict[str, Any]:
    payload = request_json("GET", f"{api_base}/api/codex-oauth/files")
    if not isinstance(payload, dict):
        raise RuntimeError("Codex OAuth files API returned an unexpected payload")
    return payload


def extract_state_from_url(url: str) -> str:
    match = re.search(r"[?&]state=([^&#]+)", url)
    return match.group(1) if match else ""


def get_phone_button(window: auto.Control):
    return window.ButtonControl(Name="使用电话号码继续", searchDepth=60)


def get_phone_input(window: auto.Control):
    return window.EditControl(Name="手机号码", searchDepth=60)


def get_continue_button(window: auto.Control):
    return window.ButtonControl(Name="继续", searchDepth=60)


def get_password_input(window: auto.Control):
    return window.EditControl(Name="密码", searchDepth=60)


def get_code_input(window: auto.Control):
    return window.EditControl(Name="验证码", searchDepth=60)


def get_name_input(window: auto.Control):
    return window.EditControl(Name="全名", searchDepth=60)


def get_age_input(window: auto.Control):
    return window.SpinnerControl(Name="年龄", searchDepth=60)


def get_email_input(window: auto.Control):
    return window.EditControl(Name="电子邮件地址", searchDepth=60)


def reject_cookie_banner(window: auto.Control) -> bool:
    for label in ("拒绝非必需", "全部接受"):
        if click_button_if_exists(window, label, delay_seconds=3):
            log(f"Handled cookie banner via: {label}")
            return True
    return False


def open_phone_flow(window: auto.Control, timeout_seconds: int) -> None:
    if control_exists(get_phone_input(window), 2):
        return
    wait_for_control(lambda: get_phone_button(window), timeout_seconds, "the phone signup button").Click()
    wait_for_control(lambda: get_phone_input(window), timeout_seconds, "the phone input")


def submit_phone(window: auto.Control, phone: str, timeout_seconds: int) -> None:
    phone_input = wait_for_control(lambda: get_phone_input(window), timeout_seconds, "the phone input")
    observed = set_edit_value(phone_input, as_signup_phone(phone))
    log(f"Filled phone field as: {observed}")
    wait_for_control(lambda: get_continue_button(window), timeout_seconds, "the continue button").Click()
    wait_for_page_progress(window, max_wait_seconds=2.5)


def submit_password(window: auto.Control, password: str, timeout_seconds: int) -> None:
    password_input = wait_for_control(lambda: get_password_input(window), timeout_seconds, "the password input")
    observed = set_edit_value(password_input, password)
    log(f"Filled password field length: {len(observed)}")
    wait_for_control(lambda: get_continue_button(window), timeout_seconds, "the password continue button").Click()
    wait_for_page_progress(window, max_wait_seconds=2.5)


def submit_code(window: auto.Control, code: str, timeout_seconds: int) -> None:
    code_input = wait_for_control(lambda: get_code_input(window), timeout_seconds, "the verification code input")
    set_edit_value(code_input, code)
    wait_for_control(lambda: get_continue_button(window), timeout_seconds, "the code continue button").Click()
    wait_for_page_progress(window, max_wait_seconds=2.5)


def submit_account_details(window: auto.Control, name: str, age: str, timeout_seconds: int) -> None:
    name_input = wait_for_control(lambda: get_name_input(window), timeout_seconds, "the name input")
    age_input = wait_for_control(lambda: get_age_input(window), timeout_seconds, "the age input")
    submit = wait_for_control(
        lambda: window.ButtonControl(Name="完成帐户创建", searchDepth=60),
        timeout_seconds,
        "the account creation button",
    )
    set_edit_value(name_input, name)
    set_edit_value(age_input, age)
    submit.Click()
    wait_for_page_progress(window, max_wait_seconds=3.0)


def submit_email(window: auto.Control, email: str, timeout_seconds: int) -> None:
    email_input = wait_for_control(lambda: get_email_input(window), timeout_seconds, "the email input")
    observed = set_edit_value(email_input, email)
    log(f"Filled email field as: {observed}")
    wait_for_control(lambda: get_continue_button(window), timeout_seconds, "the email continue button").Click()
    wait_for_page_progress(window, max_wait_seconds=2.5)


def choose_existing_account(window: auto.Control) -> bool:
    def walk(ctrl):
        try:
            if ctrl.ControlTypeName == "ButtonControl" and ctrl.Name.startswith("选择帐户 "):
                return ctrl
        except Exception:
            pass
        try:
            for child in ctrl.GetChildren():
                found = walk(child)
                if found is not None:
                    return found
        except Exception:
            return None
        return None

    found = walk(window)
    if found is None:
        return False
    found.Click()
    wait_for_page_progress(window, max_wait_seconds=2.5)
    return True


def click_codex_consent(window: auto.Control, timeout_seconds: int) -> None:
    wait_for_control(lambda: get_continue_button(window), timeout_seconds, "the Codex consent continue button").Click()
    wait_for_page_progress(window, max_wait_seconds=4.0)


def skip_chatgpt_onboarding(window: auto.Control) -> bool:
    return click_button_if_exists(window, "跳过", delay_seconds=4)


def close_automation_window(window: auto.Control) -> None:
    handle = get_window_handle(window)
    try:
        window.SetActive()
    except Exception:
        pass
    for _ in range(3):
        try:
            button = window.ButtonControl(Name="关闭", searchDepth=8)
            if control_exists(button, 1):
                button.Click()
        except Exception:
            pass
        time.sleep(0.5)
        if not has_matching_window_handle(handle):
            return
        try:
            window.SendKeys("%{F4}")
        except Exception:
            pass
        time.sleep(0.8)
        if not has_matching_window_handle(handle):
            return
        try:
            window.GetWindowPattern().Close()
        except Exception:
            pass
        time.sleep(0.8)
        if not has_matching_window_handle(handle):
            return


def close_all_automation_windows(preferred_window: auto.Control | None = None) -> None:
    preferred_handle = get_window_handle(preferred_window) if preferred_window is not None else None
    for _ in range(3):
        candidates = list_matching_windows()
        if not candidates:
            return
        ordered: list[auto.Control] = []
        if preferred_handle is not None:
            ordered.extend(candidate for candidate in candidates if get_window_handle(candidate) == preferred_handle)
            ordered.extend(candidate for candidate in candidates if get_window_handle(candidate) != preferred_handle)
        else:
            ordered = list(reversed(candidates))
        seen: set[int] = set()
        for candidate in ordered:
            handle = get_window_handle(candidate)
            if handle is None:
                continue
            if handle in seen:
                continue
            seen.add(handle)
            close_automation_window(candidate)
        time.sleep(0.5)


def refresh_page(window: auto.Control) -> None:
    try:
        window.SetActive()
    except Exception:
        pass
    try:
        window.SendKeys("{F5}")
    except Exception:
        try:
            window.SendKeys("^r")
        except Exception:
            pass
    wait_for_page_progress(window, max_wait_seconds=5.0)


def create_session_dir() -> Path:
    return Path(tempfile.mkdtemp(prefix=SESSION_DIR_PREFIX))


def cleanup_session_dir(path: Path | None) -> bool:
    if path is None:
        return False
    try:
        target = Path(path)
    except TypeError:
        return False
    if not target.exists():
        return False
    for _ in range(3):
        try:
            shutil.rmtree(target)
            return True
        except FileNotFoundError:
            return False
        except OSError:
            time.sleep(0.5)
    return False


def cleanup_stale_session_dirs(max_age_seconds: float = 0) -> int:
    removed = 0
    now = time.time()
    temp_root = Path(tempfile.gettempdir())
    for candidate in temp_root.glob(f"{SESSION_DIR_PREFIX}*"):
        if not candidate.is_dir():
            continue
        try:
            age_seconds = now - candidate.stat().st_mtime
        except OSError:
            continue
        if age_seconds < max_age_seconds:
            continue
        if cleanup_session_dir(candidate):
            removed += 1
    return removed


def cleanup_automation_artifacts(
    preferred_window: auto.Control | None = None,
    tracked_session_dirs: list[Path] | None = None,
    *,
    stale_max_age_seconds: float = 0,
) -> int:
    close_all_automation_windows(preferred_window)
    removed = 0
    seen: set[Path] = set()
    for path in tracked_session_dirs or []:
        try:
            normalized = Path(path)
        except TypeError:
            continue
        if normalized in seen:
            continue
        seen.add(normalized)
        if cleanup_session_dir(normalized):
            removed += 1
    removed += cleanup_stale_session_dirs(max_age_seconds=stale_max_age_seconds)
    return removed


def restart_signup_window(edge_path: str) -> Path:
    next_session_dir = create_session_dir()
    launch_edge(edge_path, SIGNUP_URL, next_session_dir)
    return next_session_dir


def restart_with_new_phone(window: auto.Control, edge_path: str) -> Path:
    close_all_automation_windows(window)
    time.sleep(0.4)
    return restart_signup_window(edge_path)


def buy_phone_for_signup(api_base: str) -> str:
    log("Buying phone number from local API...")
    phone = api_purchase_phone(api_base)
    log(f"Purchased phone: {phone}")
    return phone


def page_state(window: auto.Control) -> str:
    title = read_title(window)
    address = get_address_value(window)
    if CALLBACK_PREFIX in address:
        return "callback"
    if "糟糕，出错了" in title:
        return "fatal_error"
    if "欢迎回来" in title:
        return "account_picker"
    if "要求提供电子邮件地址" in title or "auth.openai.com/add-email" in address:
        return "add_email"
    if "检查你的收件箱" in title and control_exists(get_code_input(window), 1):
        return "email_code"
    if "创建密码" in title or control_exists(get_password_input(window), 1):
        return "password"
    if "查看你的手机" in title and control_exists(get_code_input(window), 1):
        return "sms_code"
    if ("你的年龄是多少" in title or control_exists(get_name_input(window), 1)) and control_exists(get_age_input(window), 1):
        return "account_details"
    if "使用 ChatGPT 登录到 Codex" in title or "/sign-in-with-chatgpt/codex/consent" in address:
        return "codex_consent"
    if control_exists(get_phone_input(window), 1) or control_exists(get_phone_button(window), 1):
        return "signup_phone"
    if "ChatGPT" in title:
        return "chatgpt"
    return "unknown"


def page_signature(state: str, title: str, address: str) -> tuple[str, str, str]:
    return (state, title.strip(), address.strip())


def should_retry_submitted_step(state: str, previous_state: str | None, already_submitted: bool) -> bool:
    if not already_submitted or previous_state != "fatal_error":
        return False
    return state in {"signup_phone", "password", "account_details", "add_email"}


def phone_submit_reached_password_step(state: str, address: str) -> bool:
    normalized_address = address.strip()
    return state == "password" and normalized_address.startswith(PASSWORD_URL)


def main() -> int:
    sys.stdout.reconfigure(encoding="utf-8")
    if auto is None:
        raise RuntimeError(
            "uiautomation is not installed. The legacy browser automation script is Windows-only; "
            "on Linux, start server.py and use http://127.0.0.1:3030/ui with VNC/noVNC."
        )
    args = parse_args()
    session_dir: Path | None = None
    tracked_session_dirs: list[Path] = []
    current_window: auto.Control | None = None
    cleanup_on_exit = True

    ensure_edge(args.edge_path)
    start_local_api_if_needed(args.api_base)
    health = api_health(args.api_base)
    temp_mail_configured = bool(health.get("tempMailConfigured"))

    signup_launched = False
    oauth_launched = False
    oauth_callback_uploaded = False
    phone_submitted = False
    password_submitted = False
    account_details_submitted = False
    email_submitted = False
    active_phone = args.phone
    active_email = args.email
    oauth_state = ""
    provided_oauth_url = str(args.oauth_url or "").strip()
    oauth_prefetch_started = False
    oauth_prefetch_done = False
    oauth_prefetch_error: str | None = None
    last_page_key: tuple[str, str, str] | None = None
    same_page_hits = 0
    fatal_error_refresh_attempts = 0

    def allocate_session_dir() -> Path:
        nonlocal session_dir
        session_dir = create_session_dir()
        tracked_session_dirs.append(session_dir)
        return session_dir

    def launch_signup_window() -> None:
        nonlocal signup_launched
        target_session_dir = session_dir or allocate_session_dir()
        launch_edge(args.edge_path, SIGNUP_URL, target_session_dir)
        signup_launched = True

    def start_oauth_prefetch() -> None:
        nonlocal oauth_prefetch_started, oauth_prefetch_done, oauth_prefetch_error, oauth_state
        if oauth_launched or provided_oauth_url or args.oauth_url or oauth_prefetch_started:
            return

        def worker() -> None:
            nonlocal oauth_prefetch_done, oauth_prefetch_error, oauth_state
            try:
                log("Prefetching Codex OAuth URL in background...")
                oauth_payload = api_get_codex_oauth_url(args.api_base)
                next_url = str(oauth_payload.get("url") or "").strip()
                next_state = str(oauth_payload.get("state") or "").strip()
                if not next_url:
                    raise RuntimeError(f"Codex OAuth URL API did not return a usable url: {oauth_payload}")
                args.oauth_url = next_url
                oauth_state = next_state
                log(f"Prefetched Codex OAuth URL. State: {oauth_state or '<none>'}")
            except Exception as exc:
                oauth_prefetch_error = str(exc)
                log(f"Prefetch Codex OAuth URL failed: {exc}")
            finally:
                oauth_prefetch_done = True

        oauth_prefetch_started = True
        threading.Thread(target=worker, daemon=True).start()

    def restart_current_email_with_new_phone(window: auto.Control, reason: str, *, cancel_current_phone: bool = False) -> Path:
        nonlocal oauth_launched, oauth_callback_uploaded, phone_submitted, password_submitted
        nonlocal account_details_submitted, email_submitted, active_phone, oauth_state
        nonlocal oauth_prefetch_started, oauth_prefetch_done, oauth_prefetch_error
        nonlocal last_page_key, same_page_hits, fatal_error_refresh_attempts, session_dir, signup_launched
        log(reason)
        if cancel_current_phone and active_phone:
            try:
                cancel_result = api_cancel_phone(args.api_base, active_phone)
                log(f"Phone cancel result: {json.dumps(cancel_result, ensure_ascii=False)}")
            except Exception as exc:
                log(f"Phone cancel failed: {exc}")
        close_all_automation_windows(window)
        cleanup_session_dir(session_dir)
        session_dir = None
        signup_launched = False
        next_phone = buy_phone_for_signup(args.api_base)
        next_session_dir = allocate_session_dir()
        launch_edge(args.edge_path, SIGNUP_URL, next_session_dir)
        signup_launched = True
        oauth_launched = False
        oauth_callback_uploaded = False
        phone_submitted = False
        password_submitted = False
        account_details_submitted = False
        email_submitted = False
        active_phone = next_phone
        oauth_state = ""
        oauth_prefetch_started = False
        oauth_prefetch_done = False
        oauth_prefetch_error = None
        last_page_key = None
        same_page_hits = 0
        fatal_error_refresh_attempts = 0
        if not provided_oauth_url:
            args.oauth_url = None
        time.sleep(0.8)
        return next_session_dir

    try:
        if not signup_launched:
            if not active_phone:
                active_phone = buy_phone_for_signup(args.api_base)
            if not active_email and temp_mail_configured:
                log("Pre-creating temp-mail address from local API...")
                active_email = api_create_temp_mail(args.api_base)
                log(f"Prepared email: {active_email}")
            allocate_session_dir()
            launch_signup_window()

        while True:
            window = wait_for_window(args.timeout)
            current_window = window
            activate(window)
            reject_cookie_banner(window)

            title = read_title(window)
            address = get_address_value(window)
            state = page_state(window)
            previous_page_key = last_page_key
            previous_state = previous_page_key[0] if previous_page_key else None
            current_page_key = page_signature(state, title, address)
            if current_page_key == previous_page_key:
                same_page_hits += 1
            else:
                last_page_key = current_page_key
                same_page_hits = 1

            log(f"Page: {state} | Title: {title}")
            if address:
                log(f"Address: {address}")
            log(f"Page stability: {same_page_hits}/{MAX_STUCK_PAGE_HITS}")

            if state != "fatal_error":
                fatal_error_refresh_attempts = 0

            if same_page_hits >= MAX_STUCK_PAGE_HITS and state not in {"callback", "fatal_error"}:
                session_dir = restart_current_email_with_new_phone(
                    window,
                    f"Page '{state}' repeated {same_page_hits} times with no progress. Closing window, getting a new phone, and restarting current email.",
                    cancel_current_phone=True,
                )
                continue

            if state == "fatal_error":
                fatal_error_refresh_attempts += 1
                if fatal_error_refresh_attempts <= MAX_FATAL_ERROR_REFRESH_ATTEMPTS:
                    log(
                        f"OpenAI error page detected. Refreshing page "
                        f"({fatal_error_refresh_attempts}/{MAX_FATAL_ERROR_REFRESH_ATTEMPTS}) before restart."
                    )
                    refresh_page(window)
                    continue
                session_dir = restart_current_email_with_new_phone(
                    window,
                    f"OpenAI error page persisted after {MAX_FATAL_ERROR_REFRESH_ATTEMPTS} refresh attempts. "
                    "Closing window and restarting current email with a new phone number.",
                    cancel_current_phone=True,
                )
                continue

            if window_contains_text(window, "与此电话号码相关联的帐户已存在"):
                session_dir = restart_current_email_with_new_phone(
                    window,
                    "Phone number is already associated with an existing account. Closing window and restarting current email with a new phone number.",
                    cancel_current_phone=True,
                )
                continue

            if state == "password" and window_contains_text(window, "Incorrect phone number or password"):
                session_dir = restart_current_email_with_new_phone(
                    window,
                    "OpenAI reported 'Incorrect phone number or password' on the password page. Treating the phone as used and restarting current email with a new phone number.",
                    cancel_current_phone=True,
                )
                continue

            if phone_submitted and not password_submitted:
                if not phone_submit_reached_password_step(state, address):
                    session_dir = restart_current_email_with_new_phone(
                        window,
                        "Phone submit did not land on the password page URL. Treating phone as used, keeping the current email, and restarting with a new number.",
                        cancel_current_phone=True,
                    )
                    continue

            if state == "callback":
                callback_url = address if address.startswith("http") else f"http://{address}"
                if not oauth_callback_uploaded:
                    log("Submitting callback URL to local Codex OAuth API...")
                    callback_result = api_submit_codex_callback(args.api_base, callback_url)
                    log(f"Callback API result: {json.dumps(callback_result, ensure_ascii=False)}")
                    oauth_callback_uploaded = True

                    state_value = extract_state_from_url(callback_url) or oauth_state
                    if state_value:
                        status_result = api_get_codex_status(args.api_base, state_value)
                        log(f"OAuth status: {json.dumps(status_result, ensure_ascii=False)}")
                    files_result = api_get_codex_files(args.api_base)
                    log(f"OAuth files: {summarize_json_payload(files_result)}")

                log("")
                log("Final callback URL:")
                log(callback_url)
                if args.close_on_success:
                    log("Closing automation window...")
                    close_automation_window(window)
                cleanup_on_exit = bool(args.close_on_success)
                return 0

            if state == "signup_phone":
                open_phone_flow(window, args.timeout)
                if not phone_submitted:
                    if not active_phone:
                        active_phone = buy_phone_for_signup(args.api_base)
                    submit_phone(window, active_phone, args.timeout)
                    phone_submitted = True
                elif should_retry_submitted_step(state, previous_state, phone_submitted):
                    log("Recovered from the OpenAI error page back to phone signup. Re-submitting phone to continue the flow.")
                    submit_phone(window, active_phone, args.timeout)
                else:
                    time.sleep(0.5)
                continue

            if state == "password":
                if not password_submitted:
                    args.password = prompt_value("Password", args.password)
                    submit_password(window, args.password, args.timeout)
                    password_submitted = True
                elif should_retry_submitted_step(state, previous_state, password_submitted):
                    args.password = prompt_value("Password", args.password)
                    log("Recovered from the OpenAI error page back to the password step. Re-submitting password to continue.")
                    submit_password(window, args.password, args.timeout)
                else:
                    time.sleep(0.5)
                continue

            if state == "sms_code":
                if active_phone:
                    log("Waiting for SMS verification code from local API...")
                    sms_code = api_wait_for_phone_code_attempts(
                        args.api_base,
                        active_phone,
                        args.max_code_attempts,
                        args.phone_poll_interval,
                    )
                    if not sms_code:
                        session_dir = restart_current_email_with_new_phone(
                            window,
                            f"No SMS code after {args.max_code_attempts} attempts spaced {args.phone_poll_interval}s apart. Closing window, getting a new phone, and restarting current email.",
                            cancel_current_phone=True,
                        )
                        continue
                    log(f"Received SMS code: {sms_code}")
                    start_oauth_prefetch()
                else:
                    sms_code = prompt_value("SMS verification code", None)
                submit_code(window, sms_code, args.timeout)
                continue

            if state == "account_details":
                if not account_details_submitted:
                    args.name = prompt_value("Full name", args.name)
                    args.age = prompt_value("Age", args.age)
                    submit_account_details(window, args.name, args.age, args.timeout)
                    account_details_submitted = True
                elif should_retry_submitted_step(state, previous_state, account_details_submitted):
                    args.name = prompt_value("Full name", args.name)
                    args.age = prompt_value("Age", args.age)
                    log("Recovered from the OpenAI error page back to account details. Re-submitting details to continue.")
                    submit_account_details(window, args.name, args.age, args.timeout)
                else:
                    time.sleep(0.5)
                continue

            if state == "add_email":
                if not email_submitted:
                    if active_email and temp_mail_configured:
                        log(f"Ensuring temp-mail address exists: {active_email}")
                        active_email = api_ensure_temp_mail_address(args.api_base, active_email)
                    active_email = prompt_value("Email", active_email)
                    submit_email(window, active_email, args.timeout)
                    email_submitted = True
                elif should_retry_submitted_step(state, previous_state, email_submitted):
                    if active_email and temp_mail_configured:
                        log(f"Ensuring temp-mail address exists: {active_email}")
                        active_email = api_ensure_temp_mail_address(args.api_base, active_email)
                    active_email = prompt_value("Email", active_email)
                    log("Recovered from the OpenAI error page back to add-email. Re-submitting email to continue.")
                    submit_email(window, active_email, args.timeout)
                else:
                    time.sleep(0.5)
                continue

            if state == "email_code":
                if active_email and temp_mail_configured:
                    log("Waiting for email verification code from local temp-mail API...")
                    email_code = api_wait_for_email_code_attempts(
                        args.api_base,
                        active_email,
                        args.email_max_attempts,
                        args.email_poll_interval,
                    )
                    if not email_code:
                        log("No email code after max attempts. Canceling phone if possible, closing window, and skipping this email.")
                        if active_phone:
                            try:
                                cancel_result = api_cancel_phone(args.api_base, active_phone)
                                log(f"Phone cancel result: {json.dumps(cancel_result, ensure_ascii=False)}")
                            except Exception as exc:
                                log(f"Phone cancel failed: {exc}")
                        close_automation_window(window)
                        cleanup_on_exit = True
                        return 2
                    log(f"Received email code: {email_code}")
                else:
                    email_code = prompt_value("Email verification code", None)
                submit_code(window, email_code, args.timeout)
                continue

            if state == "account_picker":
                if choose_existing_account(window):
                    continue
                raise RuntimeError("Could not find the existing-account selection button.")

            if state == "codex_consent":
                click_codex_consent(window, args.timeout)
                continue

            if state == "chatgpt":
                if not oauth_launched:
                    if not args.oauth_url:
                        if oauth_prefetch_started and not oauth_prefetch_done:
                            log("Waiting briefly for prefetched Codex OAuth URL...")
                            deadline = time.time() + 5
                            while time.time() < deadline and not args.oauth_url and not oauth_prefetch_done:
                                time.sleep(UI_POLL_INTERVAL_SECONDS)
                        if not args.oauth_url and oauth_prefetch_error:
                            log(f"Falling back to direct Codex OAuth URL fetch after prefetch failure: {oauth_prefetch_error}")
                        oauth_payload = api_get_codex_oauth_url(args.api_base)
                        args.oauth_url = str(oauth_payload.get("url") or "").strip()
                        oauth_state = str(oauth_payload.get("state") or "").strip()
                        if not args.oauth_url:
                            raise RuntimeError(f"Codex OAuth URL API did not return a usable url: {oauth_payload}")
                        log(f"Fetched Codex OAuth URL from local API. State: {oauth_state or '<none>'}")
                    else:
                        oauth_state = extract_state_from_url(args.oauth_url)
                    log("Launching Codex OAuth URL...")
                    if not session_dir:
                        raise RuntimeError("Automation session directory is missing before launching OAuth.")
                    open_url_in_edge(args.edge_path, args.oauth_url, session_dir)
                    oauth_launched = True
                    time.sleep(2.5)
                    continue
                time.sleep(0.5)
                continue

            time.sleep(0.5)
    finally:
        if cleanup_on_exit:
            cleanup_automation_artifacts(
                current_window,
                tracked_session_dirs,
                stale_max_age_seconds=0,
            )


if __name__ == "__main__":
    try:
        raise SystemExit(main())
    except Exception as exc:
        log(f"Error: {exc}")
        raise
