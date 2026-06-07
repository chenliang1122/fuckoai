from __future__ import annotations

import json
import os
import queue
import random
import re
import string
import subprocess
import sys
import threading
import time
from pathlib import Path
from urllib.error import HTTPError, URLError
from urllib.parse import urlencode, urlparse
from urllib.request import Request, urlopen

try:
    import tkinter as tk
    from tkinter import messagebox, scrolledtext, ttk
except ImportError:
    tk = None
    messagebox = None
    scrolledtext = None
    ttk = None


ROOT = Path(__file__).resolve().parent
DEFAULT_API_BASE = "http://127.0.0.1:3030"
CREATE_NO_WINDOW = getattr(subprocess, "CREATE_NO_WINDOW", 0)
MAX_LOG_MESSAGE_LENGTH = 4000
MAX_LOG_MESSAGES_PER_FLUSH = 80
MAX_LOG_WIDGET_CHARS = 200000
LOG_FLUSH_INTERVAL_MS = 100
LOG_FLUSH_BUSY_INTERVAL_MS = 15
LOCAL_SERVER_HOSTS = {"127.0.0.1", "localhost", "0.0.0.0", "::1"}


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


def load_local_json(path: Path) -> dict:
    if not path.exists():
        return {}
    try:
        raw = path.read_text(encoding="utf-8")
    except OSError:
        return {}
    try:
        payload = json.loads(raw)
    except json.JSONDecodeError:
        return {}
    return payload if isinstance(payload, dict) else {}


def request_json(method: str, url: str, body: dict | None = None, timeout: int = 10) -> dict:
    headers = {"Accept": "application/json"}
    data = None
    if body is not None:
        data = json.dumps(body).encode("utf-8")
        headers["Content-Type"] = "application/json"
    request = Request(url, data=data, method=method, headers=headers)
    with urlopen(request, timeout=timeout) as response:
        raw = response.read().decode("utf-8", errors="replace")
    return json.loads(raw) if raw else {}


def check_api_health(api_base: str) -> dict | None:
    try:
        return request_json("GET", f"{api_base}/api/health")
    except (HTTPError, URLError, OSError, json.JSONDecodeError):
        return None


def parse_positive_int(value: str, default: int = 1) -> int:
    try:
        parsed = int(str(value).strip())
        return parsed if parsed > 0 else default
    except (TypeError, ValueError):
        return default


def generate_random_local_part(length: int = 10) -> str:
    alphabet = string.ascii_lowercase + string.digits
    return "".join(random.choices(alphabet, k=length))


def truncate_log_message(message: str, limit: int = MAX_LOG_MESSAGE_LENGTH) -> str:
    text = str(message).replace("\r\n", "\n").replace("\r", "\n")
    if len(text) <= limit:
        return text
    head = max(0, int(limit * 0.75))
    tail = max(0, limit - head)
    omitted = len(text) - head - tail
    return f"{text[:head]}\n... [日志已截断，省略 {omitted} 个字符] ...\n{text[-tail:]}"


def format_duration(seconds: float) -> str:
    total_seconds = max(0, int(round(seconds)))
    minutes, secs = divmod(total_seconds, 60)
    hours, minutes = divmod(minutes, 60)
    if hours:
        return f"{hours}h {minutes}m {secs}s"
    if minutes:
        return f"{minutes}m {secs}s"
    return f"{secs}s"


class LauncherApp:
    def __init__(self) -> None:
        if tk is None or ttk is None:
            raise RuntimeError("launcher.py 是 Windows/Tkinter 桌面入口；Linux 下请运行 server.py 并访问 /ui")

        self.api_base = os.getenv("LAUNCHER_API_BASE", DEFAULT_API_BASE).rstrip("/")
        self.server_path = ROOT / "server.py"
        self.purchase_config_path = (ROOT / os.getenv("PURCHASE_CONFIG_FILE", "purchase_config.json")).resolve()
        self.signup_path = ROOT / "chatgpt_signup_to_code.py"
        self.server_process: subprocess.Popen | None = None
        self.signup_process: subprocess.Popen | None = None
        self.batch_thread: threading.Thread | None = None
        self.server_thread: threading.Thread | None = None
        self.stop_requested = threading.Event()
        self.log_queue: queue.Queue[tuple[str, str]] = queue.Queue()
        self.server_ready = False
        self.closed = False

        self.root = tk.Tk()
        self.root.title("ChatGPT 注册启动器")
        self.root.attributes("-topmost", True)
        self.root.minsize(460, 720)
        self.root.configure(bg="#f3efe5")
        self.root.protocol("WM_DELETE_WINDOW", self.on_close)

        self.server_status_var = tk.StringVar(value="Server: 启动中...")
        self.batch_status_var = tk.StringVar(value="批次: 空闲")
        self.progress_status_var = tk.StringVar(value="进度: 0/0")
        self.current_status_var = tk.StringVar(value="当前: 无")
        self.result_status_var = tk.StringVar(value="结果: 成功 0 / 跳过 0 / 失败 0")
        self.duration_status_var = tk.StringVar(value="耗时: 总计 0s / 平均单号 0s")
        self.settings_status_var = tk.StringVar(value="设置: 等待服务就绪")

        self.mode_var = tk.StringVar(value="sequential")
        self.first_email_var = tk.StringVar(value="user001@example.com")
        self.count_var = tk.StringVar(value="1")
        self.random_domain_var = tk.StringVar(value="example.com")
        self.group_label_var = tk.StringVar()
        self.group_country_code_var = tk.StringVar()
        self.group_country_name_var = tk.StringVar()
        self.group_operator_var = tk.StringVar(value="any")
        self.group_exact_price_var = tk.StringVar()
        self.group_max_price_var = tk.StringVar()
        self.group_fixed_price_var = tk.BooleanVar(value=True)
        self.group_enabled_var = tk.BooleanVar(value=True)
        self.country_lookup_result_var = tk.StringVar(value="国家查询: 输入国家名后可自动查询代码和运营商")
        self.purchase_groups: list[dict] = []
        self.selected_group_index: int | None = None
        self.settings_loading = False
        self.settings_window: tk.Toplevel | None = None
        self.settings_ui_ready = False
        self.batch_started_at: float | None = None
        self.batch_task_seconds_total = 0.0

        self.build_ui()
        self.place_window()
        self.update_generator_state()
        self.root.after(100, self.flush_logs)
        self.server_thread = threading.Thread(target=self.bootstrap_server, daemon=True)
        self.server_thread.start()

    def build_ui(self) -> None:
        style = ttk.Style()
        if "vista" in style.theme_names():
            style.theme_use("vista")

        container = ttk.Frame(self.root)
        container.pack(fill="both", expand=True)
        container.columnconfigure(0, weight=1)
        container.rowconfigure(0, weight=1)

        canvas = tk.Canvas(container, highlightthickness=0, bg="#f3efe5")
        canvas.grid(row=0, column=0, sticky="nsew")
        scrollbar = ttk.Scrollbar(container, orient="vertical", command=canvas.yview)
        scrollbar.grid(row=0, column=1, sticky="ns")
        canvas.configure(yscrollcommand=scrollbar.set)

        outer = ttk.Frame(canvas, padding=14)
        outer_id = canvas.create_window((0, 0), window=outer, anchor="nw")

        def sync_outer_width(event=None) -> None:
            canvas.itemconfigure(outer_id, width=canvas.winfo_width())

        def sync_scrollregion(event=None) -> None:
            canvas.configure(scrollregion=canvas.bbox("all"))

        def bind_wheel(event=None) -> None:
            canvas.bind_all("<MouseWheel>", on_mousewheel)

        def unbind_wheel(event=None) -> None:
            canvas.unbind_all("<MouseWheel>")

        def on_mousewheel(event) -> None:
            canvas.yview_scroll(-1 * int(event.delta / 120), "units")

        outer.bind("<Configure>", sync_scrollregion)
        canvas.bind("<Configure>", sync_outer_width)
        canvas.bind("<Enter>", bind_wheel)
        canvas.bind("<Leave>", unbind_wheel)
        outer.bind("<Enter>", bind_wheel)
        outer.bind("<Leave>", unbind_wheel)
        self.main_canvas = canvas

        outer.columnconfigure(0, weight=1)

        title = ttk.Label(outer, text="注册启动器", font=("Microsoft YaHei UI", 15, "bold"))
        title.grid(row=0, column=0, sticky="w")

        hint = ttk.Label(
            outer,
            text="窗口固定在屏幕右侧并保持置顶，尽量不挡住浏览器中间区域。支持顺序前缀和随机前缀两种邮箱队列生成方式。",
            foreground="#555555",
            wraplength=410,
            justify="left",
        )
        hint.grid(row=1, column=0, sticky="we", pady=(6, 10))

        status_frame = ttk.Frame(outer)
        status_frame.grid(row=2, column=0, sticky="we", pady=(0, 10))
        status_frame.columnconfigure(0, weight=1)
        ttk.Label(status_frame, textvariable=self.server_status_var).grid(row=0, column=0, sticky="w")
        ttk.Label(status_frame, textvariable=self.batch_status_var).grid(row=1, column=0, sticky="w", pady=(4, 0))
        ttk.Label(status_frame, textvariable=self.progress_status_var).grid(row=2, column=0, sticky="w", pady=(4, 0))
        ttk.Label(status_frame, textvariable=self.current_status_var).grid(row=3, column=0, sticky="w", pady=(4, 0))
        ttk.Label(status_frame, textvariable=self.result_status_var).grid(row=4, column=0, sticky="w", pady=(4, 0))
        ttk.Label(status_frame, textvariable=self.duration_status_var).grid(row=5, column=0, sticky="w", pady=(4, 0))
        action_frame = ttk.Frame(status_frame)
        action_frame.grid(row=6, column=0, sticky="w", pady=(8, 0))
        self.restart_server_button = ttk.Button(action_frame, text="重启本地 API", command=self.request_server_restart)
        self.restart_server_button.grid(row=0, column=0, sticky="w")
        self.settings_button = ttk.Button(action_frame, text="购买设置", command=self.open_settings_window)
        self.settings_button.grid(row=0, column=1, sticky="w", padx=(8, 0))

        generator_frame = ttk.LabelFrame(outer, text="邮箱列表生成", padding=12)
        generator_frame.grid(row=3, column=0, sticky="we")
        generator_frame.columnconfigure(0, weight=1)

        mode_frame = ttk.Frame(generator_frame)
        mode_frame.grid(row=0, column=0, sticky="w")
        ttk.Radiobutton(mode_frame, text="顺序前缀", variable=self.mode_var, value="sequential", command=self.update_generator_state).grid(row=0, column=0, sticky="w")
        ttk.Radiobutton(mode_frame, text="随机前缀", variable=self.mode_var, value="random", command=self.update_generator_state).grid(row=0, column=1, sticky="w", padx=(12, 0))

        seq_frame = ttk.Frame(generator_frame)
        seq_frame.grid(row=1, column=0, sticky="we", pady=(10, 0))
        seq_frame.columnconfigure(1, weight=1)
        ttk.Label(seq_frame, text="第一个邮箱").grid(row=0, column=0, sticky="w")
        self.first_email_entry = ttk.Entry(seq_frame, textvariable=self.first_email_var)
        self.first_email_entry.grid(row=0, column=1, sticky="we", padx=(8, 0))

        random_frame = ttk.Frame(generator_frame)
        random_frame.grid(row=2, column=0, sticky="we", pady=(10, 0))
        random_frame.columnconfigure(1, weight=1)
        ttk.Label(random_frame, text="随机域名").grid(row=0, column=0, sticky="w")
        self.random_domain_entry = ttk.Entry(random_frame, textvariable=self.random_domain_var)
        self.random_domain_entry.grid(row=0, column=1, sticky="we", padx=(8, 0))

        common_frame = ttk.Frame(generator_frame)
        common_frame.grid(row=3, column=0, sticky="we", pady=(10, 0))
        common_frame.columnconfigure(1, weight=1)
        ttk.Label(common_frame, text="总注册数").grid(row=0, column=0, sticky="w")
        self.count_entry = ttk.Entry(common_frame, textvariable=self.count_var, width=12)
        self.count_entry.grid(row=0, column=1, sticky="w", padx=(8, 0))

        generate_button = ttk.Button(generator_frame, text="生成待注册邮箱列表", command=self.generate_queue)
        generate_button.grid(row=4, column=0, sticky="we", pady=(10, 0))

        queue_frame = ttk.LabelFrame(outer, text="待注册邮箱列表", padding=12)
        queue_frame.grid(row=4, column=0, sticky="nsew", pady=(12, 0))
        queue_frame.columnconfigure(0, weight=1)
        queue_frame.rowconfigure(1, weight=1)

        ttk.Label(queue_frame, text="可手动编辑，一行一个邮箱。全部留空则自动创建临时邮箱跑 1 次。").grid(row=0, column=0, sticky="w")
        self.email_widget = scrolledtext.ScrolledText(
            queue_frame,
            wrap="word",
            height=6,
            font=("Consolas", 10),
            bg="#fffdf7",
            fg="#1f1f1f",
            insertbackground="#1f1f1f",
        )
        self.email_widget.grid(row=1, column=0, sticky="nsew", pady=(6, 10))

        button_frame = ttk.Frame(queue_frame)
        button_frame.grid(row=2, column=0, sticky="we")
        button_frame.columnconfigure(0, weight=1)
        button_frame.columnconfigure(1, weight=1)
        self.start_button = ttk.Button(button_frame, text="开始批量注册", command=self.start_batch)
        self.start_button.grid(row=0, column=0, sticky="we", padx=(0, 6))
        self.stop_button = ttk.Button(button_frame, text="停止当前批次", command=self.stop_batch, state="disabled")
        self.stop_button.grid(row=0, column=1, sticky="we")

        log_frame = ttk.LabelFrame(outer, text="实时日志", padding=8)
        log_frame.grid(row=5, column=0, sticky="nsew", pady=(12, 0))
        log_frame.columnconfigure(0, weight=1)
        log_frame.rowconfigure(0, weight=1)
        self.log_widget = scrolledtext.ScrolledText(
            log_frame,
            wrap="word",
            height=12,
            font=("Consolas", 10),
            bg="#fffdf7",
            fg="#1f1f1f",
            insertbackground="#1f1f1f",
        )
        self.log_widget.grid(row=0, column=0, sticky="nsew")
        self.log_widget.configure(state="disabled")

    def build_settings_ui(self, parent: ttk.Frame) -> None:
        parent.columnconfigure(0, weight=1)
        parent.rowconfigure(0, weight=1)

        container = ttk.Frame(parent)
        container.grid(row=0, column=0, sticky="nsew")
        container.columnconfigure(0, weight=1)
        container.rowconfigure(0, weight=1)

        canvas = tk.Canvas(container, highlightthickness=0, bg="#f3efe5")
        canvas.grid(row=0, column=0, sticky="nsew")
        scrollbar = ttk.Scrollbar(container, orient="vertical", command=canvas.yview)
        scrollbar.grid(row=0, column=1, sticky="ns")
        canvas.configure(yscrollcommand=scrollbar.set)
        inner = ttk.Frame(canvas, padding=14)
        window_id = canvas.create_window((0, 0), window=inner, anchor="nw")

        def sync_inner_width(event=None) -> None:
            canvas.itemconfigure(window_id, width=canvas.winfo_width())

        def sync_scrollregion(event=None) -> None:
            canvas.configure(scrollregion=canvas.bbox("all"))

        def bind_wheel(event=None) -> None:
            canvas.bind_all("<MouseWheel>", on_mousewheel)

        def unbind_wheel(event=None) -> None:
            canvas.unbind_all("<MouseWheel>")

        def on_mousewheel(event) -> None:
            canvas.yview_scroll(-1 * int(event.delta / 120), "units")

        inner.bind("<Configure>", sync_scrollregion)
        canvas.bind("<Configure>", sync_inner_width)
        canvas.bind("<Enter>", bind_wheel)
        canvas.bind("<Leave>", unbind_wheel)
        inner.bind("<Enter>", bind_wheel)
        inner.bind("<Leave>", unbind_wheel)
        self.settings_canvas = canvas

        inner.columnconfigure(0, weight=1)

        title = ttk.Label(inner, text="购买配置", font=("Microsoft YaHei UI", 14, "bold"))
        title.grid(row=0, column=0, sticky="w")

        hint = ttk.Label(
            inner,
            text="服务固定为 OpenAI / dr。这里只维护购买组顺序；购买时会按列表顺序依次尝试，某组没号或失败就自动试下一组。",
            foreground="#555555",
            wraplength=620,
            justify="left",
        )
        hint.grid(row=1, column=0, sticky="we", pady=(6, 10))

        groups_frame = ttk.LabelFrame(inner, text="购买组顺序", padding=12)
        groups_frame.grid(row=2, column=0, sticky="nsew")
        groups_frame.columnconfigure(0, weight=1)
        groups_frame.rowconfigure(1, weight=1)

        ttk.Label(groups_frame, textvariable=self.settings_status_var).grid(row=0, column=0, sticky="w")

        list_frame = ttk.Frame(groups_frame)
        list_frame.grid(row=1, column=0, sticky="nsew", pady=(8, 10))
        list_frame.columnconfigure(0, weight=1)
        list_frame.rowconfigure(0, weight=1)
        self.group_listbox = tk.Listbox(list_frame, height=8, activestyle="dotbox", exportselection=False)
        self.group_listbox.grid(row=0, column=0, sticky="nsew")
        self.group_listbox.bind("<<ListboxSelect>>", self.on_group_select)
        scrollbar = ttk.Scrollbar(list_frame, orient="vertical", command=self.group_listbox.yview)
        scrollbar.grid(row=0, column=1, sticky="ns")
        self.group_listbox.configure(yscrollcommand=scrollbar.set)

        list_buttons = ttk.Frame(groups_frame)
        list_buttons.grid(row=2, column=0, sticky="we")
        for index in range(3):
            list_buttons.columnconfigure(index, weight=1)
        ttk.Button(list_buttons, text="新增组", command=self.add_group).grid(row=0, column=0, sticky="we", padx=(0, 6))
        ttk.Button(list_buttons, text="删除组", command=self.delete_group).grid(row=0, column=1, sticky="we", padx=(0, 6))
        ttk.Button(list_buttons, text="上移", command=self.move_group_up).grid(row=0, column=2, sticky="we")
        ttk.Button(list_buttons, text="下移", command=self.move_group_down).grid(row=1, column=0, sticky="we", padx=(0, 6), pady=(6, 0))
        ttk.Button(list_buttons, text="重新加载", command=self.refresh_purchase_settings_async).grid(row=1, column=1, sticky="we", padx=(0, 6), pady=(6, 0))
        ttk.Button(list_buttons, text="保存设置", command=self.save_purchase_settings_async).grid(row=1, column=2, sticky="we", pady=(6, 0))

        editor_frame = ttk.LabelFrame(inner, text="当前组编辑", padding=12)
        editor_frame.grid(row=3, column=0, sticky="we", pady=(12, 0))
        editor_frame.columnconfigure(1, weight=1)

        ttk.Label(editor_frame, text="组名称").grid(row=0, column=0, sticky="w")
        self.group_label_entry = ttk.Entry(editor_frame, textvariable=self.group_label_var)
        self.group_label_entry.grid(row=0, column=1, sticky="we", padx=(8, 0))

        ttk.Label(editor_frame, text="国家名称").grid(row=1, column=0, sticky="w", pady=(10, 0))
        self.group_country_name_entry = ttk.Entry(editor_frame, textvariable=self.group_country_name_var)
        self.group_country_name_entry.grid(row=1, column=1, sticky="we", padx=(8, 0), pady=(10, 0))

        lookup_row = ttk.Frame(editor_frame)
        lookup_row.grid(row=2, column=0, columnspan=2, sticky="we", pady=(10, 0))
        lookup_row.columnconfigure(1, weight=1)
        ttk.Label(lookup_row, text="国家代码").grid(row=0, column=0, sticky="w")
        self.group_country_code_entry = ttk.Entry(editor_frame, textvariable=self.group_country_code_var)
        self.group_country_code_entry.grid_forget()
        self.group_country_code_entry = ttk.Entry(lookup_row, textvariable=self.group_country_code_var, width=12)
        self.group_country_code_entry.grid(row=0, column=1, sticky="w", padx=(8, 8))
        self.lookup_country_button = ttk.Button(lookup_row, text="查询代码/运营商", command=self.lookup_country_async)
        self.lookup_country_button.grid(row=0, column=2, sticky="e")

        ttk.Label(editor_frame, text="运营商").grid(row=3, column=0, sticky="w", pady=(10, 0))
        self.group_operator_entry = ttk.Entry(editor_frame, textvariable=self.group_operator_var)
        self.group_operator_entry.grid(row=3, column=1, sticky="we", padx=(8, 0), pady=(10, 0))

        ttk.Label(editor_frame, textvariable=self.country_lookup_result_var, foreground="#555555", wraplength=620, justify="left").grid(
            row=4, column=0, columnspan=2, sticky="we", pady=(10, 0)
        )

        price_row = ttk.Frame(editor_frame)
        price_row.grid(row=5, column=0, columnspan=2, sticky="we", pady=(10, 0))
        price_row.columnconfigure(1, weight=1)
        price_row.columnconfigure(3, weight=1)
        ttk.Label(price_row, text="精确价格").grid(row=0, column=0, sticky="w")
        self.group_exact_price_entry = ttk.Entry(editor_frame, textvariable=self.group_exact_price_var)
        self.group_exact_price_entry.grid_forget()
        self.group_exact_price_entry = ttk.Entry(price_row, textvariable=self.group_exact_price_var)
        self.group_exact_price_entry.grid(row=0, column=1, sticky="we", padx=(8, 12))
        ttk.Label(price_row, text="最高价格").grid(row=0, column=2, sticky="w")
        self.group_max_price_entry = ttk.Entry(editor_frame, textvariable=self.group_max_price_var)
        self.group_max_price_entry.grid_forget()
        self.group_max_price_entry = ttk.Entry(price_row, textvariable=self.group_max_price_var)
        self.group_max_price_entry.grid(row=0, column=3, sticky="we", padx=(8, 0))

        check_row = ttk.Frame(editor_frame)
        check_row.grid(row=6, column=0, columnspan=2, sticky="w", pady=(10, 0))
        self.group_fixed_price_check = ttk.Checkbutton(check_row, text="固定精确价格", variable=self.group_fixed_price_var)
        self.group_fixed_price_check.grid(row=0, column=0, sticky="w")
        self.group_enabled_check = ttk.Checkbutton(check_row, text="启用当前组", variable=self.group_enabled_var)
        self.group_enabled_check.grid(row=0, column=1, sticky="w", padx=(16, 0))

        self.settings_editor_widgets = [
            self.group_label_entry,
            self.group_country_code_entry,
            self.group_country_name_entry,
            self.group_operator_entry,
            self.group_exact_price_entry,
            self.group_max_price_entry,
            self.group_fixed_price_check,
            self.group_enabled_check,
            self.lookup_country_button,
        ]
        self.settings_ui_ready = True
        self.set_group_editor_enabled(False)

    def open_settings_window(self) -> None:
        if self.settings_window is not None:
            try:
                if self.settings_window.winfo_exists():
                    self.settings_window.deiconify()
                    self.settings_window.lift()
                    self.settings_window.focus_force()
                    return
            except tk.TclError:
                pass

        self.settings_window = tk.Toplevel(self.root)
        self.settings_window.title("购买设置")
        self.settings_window.configure(bg="#f3efe5")
        self.settings_window.minsize(620, 720)
        self.settings_window.protocol("WM_DELETE_WINDOW", self.close_settings_window)
        self.build_settings_ui(self.settings_window)
        self.place_settings_window()
        self.load_purchase_settings_from_disk()
        self.refresh_purchase_settings_async()

    def close_settings_window(self) -> None:
        window = self.settings_window
        self.settings_window = None
        self.settings_ui_ready = False
        if window is not None:
            try:
                window.destroy()
            except tk.TclError:
                pass

    def normalize_purchase_settings(self, payload: dict | None) -> dict:
        payload = payload if isinstance(payload, dict) else {}
        settings = payload.get("purchaseSettings") if "purchaseSettings" in payload else payload
        settings = settings if isinstance(settings, dict) else {}
        groups = []
        for item in settings.get("purchaseGroups") or []:
            if not isinstance(item, dict):
                continue
            groups.append(
                {
                    "label": str(item.get("label") or "").strip(),
                    "enabled": bool(item.get("enabled", True)),
                    "countryName": str(item.get("countryName") or "").strip(),
                    "countryCode": str(item.get("countryCode") or "").strip(),
                    "operator": str(item.get("operator") or "any").strip() or "any",
                    "fixedPrice": bool(item.get("fixedPrice", True)),
                    "exactPrice": str(item.get("exactPrice") or "").strip(),
                    "maxPrice": str(item.get("maxPrice") or "").strip(),
                }
            )
        return {
            "purchaseGroups": groups,
        }

    def load_purchase_settings_from_disk(self) -> None:
        payload = load_local_json(self.purchase_config_path)
        self.apply_purchase_settings(payload)
        if self.purchase_groups:
            self.settings_status_var.set(f"设置: 已从本地文件加载 {len(self.purchase_groups)} 组")
        else:
            self.settings_status_var.set("设置: 本地文件为空，等待 API 刷新")

    def new_group_template(self) -> dict:
        return {
            "label": "",
            "enabled": True,
            "countryName": "",
            "countryCode": "",
            "operator": "any",
            "fixedPrice": True,
            "exactPrice": "",
            "maxPrice": "",
        }

    def set_group_editor_enabled(self, enabled: bool) -> None:
        state = "normal" if enabled else "disabled"
        for widget in self.settings_editor_widgets:
            try:
                widget.configure(state=state)
            except tk.TclError:
                pass

    def clear_group_form(self) -> None:
        self.group_label_var.set("")
        self.group_country_code_var.set("")
        self.group_country_name_var.set("")
        self.group_operator_var.set("any")
        self.group_exact_price_var.set("")
        self.group_max_price_var.set("")
        self.group_fixed_price_var.set(True)
        self.group_enabled_var.set(True)
        self.country_lookup_result_var.set("国家查询: 输入国家名后可自动查询代码和运营商")

    def build_group_summary(self, group: dict, index: int) -> str:
        status = "ON" if group.get("enabled", True) else "OFF"
        country = str(group.get("countryCode") or group.get("countryName") or "--")
        operator = str(group.get("operator") or "any")
        if group.get("fixedPrice") and str(group.get("exactPrice") or "").strip():
            price = f"exact {group.get('exactPrice')}"
        elif str(group.get("maxPrice") or "").strip():
            price = f"max {group.get('maxPrice')}"
        else:
            price = "market"
        label = str(group.get("label") or "").strip()
        prefix = f"{index + 1}. [{status}]"
        return f"{prefix} {label}" if label else f"{prefix} {country} / {operator} / {price}"

    def refresh_group_list(self) -> None:
        self.group_listbox.delete(0, "end")
        for index, group in enumerate(self.purchase_groups):
            self.group_listbox.insert("end", self.build_group_summary(group, index))
        if not self.purchase_groups:
            self.selected_group_index = None
            self.clear_group_form()
            self.set_group_editor_enabled(False)

    def load_group_into_form(self, index: int) -> None:
        if index < 0 or index >= len(self.purchase_groups):
            self.selected_group_index = None
            self.clear_group_form()
            self.set_group_editor_enabled(False)
            return
        group = self.purchase_groups[index]
        self.selected_group_index = index
        self.group_label_var.set(str(group.get("label") or ""))
        self.group_country_code_var.set(str(group.get("countryCode") or ""))
        self.group_country_name_var.set(str(group.get("countryName") or ""))
        self.group_operator_var.set(str(group.get("operator") or "any"))
        self.group_exact_price_var.set(str(group.get("exactPrice") or ""))
        self.group_max_price_var.set(str(group.get("maxPrice") or ""))
        self.group_fixed_price_var.set(bool(group.get("fixedPrice", True)))
        self.group_enabled_var.set(bool(group.get("enabled", True)))
        self.country_lookup_result_var.set("国家查询: 输入国家名后可自动查询代码和运营商")
        self.set_group_editor_enabled(True)

    def sync_current_group_from_form(self) -> None:
        if self.selected_group_index is None or self.selected_group_index >= len(self.purchase_groups):
            return
        self.purchase_groups[self.selected_group_index] = {
            "label": self.group_label_var.get().strip(),
            "enabled": bool(self.group_enabled_var.get()),
            "countryName": self.group_country_name_var.get().strip(),
            "countryCode": self.group_country_code_var.get().strip(),
            "operator": self.group_operator_var.get().strip() or "any",
            "fixedPrice": bool(self.group_fixed_price_var.get()),
            "exactPrice": self.group_exact_price_var.get().strip(),
            "maxPrice": self.group_max_price_var.get().strip(),
        }
        self.refresh_group_list()
        self.group_listbox.selection_clear(0, "end")
        self.group_listbox.selection_set(self.selected_group_index)

    def on_group_select(self, event=None) -> None:
        selection = self.group_listbox.curselection()
        if not selection:
            return
        next_index = int(selection[0])
        if self.selected_group_index is not None and self.selected_group_index != next_index:
            self.sync_current_group_from_form()
        self.load_group_into_form(next_index)

    def apply_purchase_settings(self, payload: dict | None) -> None:
        settings = self.normalize_purchase_settings(payload)
        self.purchase_groups = settings["purchaseGroups"]
        if not self.settings_ui_ready or self.settings_window is None:
            return
        self.selected_group_index = None
        self.refresh_group_list()
        if self.purchase_groups:
            self.group_listbox.selection_set(0)
            self.load_group_into_form(0)
        self.settings_status_var.set(f"设置: 已加载 {len(self.purchase_groups)} 组")

    def refresh_purchase_settings_async(self) -> None:
        if self.settings_loading or self.closed or not self.settings_ui_ready:
            return
        self.settings_loading = True
        self.settings_status_var.set("设置: 加载中...")
        threading.Thread(target=self.refresh_purchase_settings, daemon=True).start()

    def refresh_purchase_settings(self) -> None:
        try:
            settings = request_json("GET", f"{self.api_base}/api/purchase-settings", timeout=3)
        except Exception as error:
            if self.purchase_groups:
                error_text = f"设置: 已显示本地配置，API 刷新失败 {error}"
            else:
                error_text = f"设置: 加载失败 {error}"
            if not self.closed:
                self.root.after(0, lambda text=error_text: self.settings_status_var.set(text))
            self.settings_loading = False
            return

        def apply() -> None:
            self.apply_purchase_settings(settings)
            self.settings_loading = False

        if not self.closed:
            self.root.after(0, apply)

    def collect_purchase_settings_payload(self) -> dict:
        self.sync_current_group_from_form()
        groups = []
        for group in self.purchase_groups:
            groups.append(
                {
                    "label": str(group.get("label") or "").strip(),
                    "enabled": bool(group.get("enabled", True)),
                    "countryName": str(group.get("countryName") or "").strip(),
                    "countryCode": str(group.get("countryCode") or "").strip(),
                    "operator": str(group.get("operator") or "any").strip() or "any",
                    "fixedPrice": bool(group.get("fixedPrice", True)),
                    "exactPrice": str(group.get("exactPrice") or "").strip(),
                    "maxPrice": str(group.get("maxPrice") or "").strip(),
                }
            )
        return {
            "serviceName": "OpenAI",
            "serviceCode": "dr",
            "purchaseGroups": groups,
        }

    def add_group(self) -> None:
        self.sync_current_group_from_form()
        self.purchase_groups.append(self.new_group_template())
        self.refresh_group_list()
        index = len(self.purchase_groups) - 1
        self.group_listbox.selection_clear(0, "end")
        self.group_listbox.selection_set(index)
        self.load_group_into_form(index)
        self.settings_status_var.set(f"设置: 已新增第 {index + 1} 组，记得保存")

    def delete_group(self) -> None:
        if self.selected_group_index is None or self.selected_group_index >= len(self.purchase_groups):
            return
        del self.purchase_groups[self.selected_group_index]
        self.refresh_group_list()
        if self.purchase_groups:
            index = min(self.selected_group_index, len(self.purchase_groups) - 1)
            self.group_listbox.selection_set(index)
            self.load_group_into_form(index)
        else:
            self.clear_group_form()
            self.set_group_editor_enabled(False)
        self.settings_status_var.set("设置: 当前组已删除，记得保存")

    def move_group_up(self) -> None:
        if self.selected_group_index is None or self.selected_group_index <= 0:
            return
        self.sync_current_group_from_form()
        index = self.selected_group_index
        self.purchase_groups[index - 1], self.purchase_groups[index] = self.purchase_groups[index], self.purchase_groups[index - 1]
        self.refresh_group_list()
        self.group_listbox.selection_set(index - 1)
        self.load_group_into_form(index - 1)
        self.settings_status_var.set("设置: 已上移当前组，记得保存")

    def move_group_down(self) -> None:
        if self.selected_group_index is None or self.selected_group_index >= len(self.purchase_groups) - 1:
            return
        self.sync_current_group_from_form()
        index = self.selected_group_index
        self.purchase_groups[index + 1], self.purchase_groups[index] = self.purchase_groups[index], self.purchase_groups[index + 1]
        self.refresh_group_list()
        self.group_listbox.selection_set(index + 1)
        self.load_group_into_form(index + 1)
        self.settings_status_var.set("设置: 已下移当前组，记得保存")

    def save_purchase_settings_async(self) -> None:
        if self.settings_loading or self.closed:
            return
        payload = self.collect_purchase_settings_payload()
        self.settings_loading = True
        self.settings_status_var.set("设置: 保存中...")
        threading.Thread(target=self.save_purchase_settings, args=(payload,), daemon=True).start()

    def save_purchase_settings(self, payload: dict) -> None:
        try:
            settings = request_json("POST", f"{self.api_base}/api/purchase-settings", payload, timeout=5)
        except Exception as error:
            error_text = f"设置: 保存失败 {error}"
            if not self.closed:
                self.root.after(0, lambda text=error_text: self.settings_status_var.set(text))
            self.settings_loading = False
            return

        def apply() -> None:
            self.apply_purchase_settings(settings)
            self.append_log(f"购买设置已保存到 {self.purchase_config_path}")
            self.settings_loading = False

        if not self.closed:
            self.root.after(0, apply)

    def lookup_country_async(self) -> None:
        country_name = self.group_country_name_var.get().strip()
        if not country_name:
            self.country_lookup_result_var.set("国家查询: 请先填写国家名称")
            return
        self.country_lookup_result_var.set("国家查询: 查询中...")
        threading.Thread(target=self.lookup_country, args=(country_name, "dr"), daemon=True).start()

    def lookup_country(self, country_name: str, service_code: str) -> None:
        query = urlencode({"name": country_name, "serviceCode": service_code})
        try:
            payload = request_json("GET", f"{self.api_base}/api/country-lookup?{query}")
        except Exception as error:
            error_text = f"国家查询: 失败 {error}"
            if not self.closed:
                self.root.after(0, lambda text=error_text: self.country_lookup_result_var.set(text))
            return

        country = payload.get("country") or {}
        operators = [str(item) for item in payload.get("operators") or [] if str(item)]
        matches = payload.get("matches") or []

        def apply() -> None:
            code = str(country.get("code") or "").strip()
            name = str(country.get("name") or country.get("localName") or country_name).strip()
            if code:
                self.group_country_code_var.set(code)
            if name:
                self.group_country_name_var.set(name)
            non_any_operators = [item for item in operators if item.lower() != "any"]
            current_operator = self.group_operator_var.get().strip().lower()
            if len(non_any_operators) == 1 and current_operator in {"", "any"}:
                self.group_operator_var.set(non_any_operators[0])
            match_names = []
            for item in matches[:5]:
                if not isinstance(item, dict):
                    continue
                label = str(item.get("name") or item.get("localName") or item.get("code") or "").strip()
                code_value = str(item.get("code") or "").strip()
                match_names.append(f"{label}({code_value})" if code_value else label)
            operator_text = ", ".join(operators) if operators else "无"
            matches_text = "；候选: " + ", ".join(match_names) if match_names else ""
            self.country_lookup_result_var.set(f"国家查询: {name} -> {code}；运营商: {operator_text}{matches_text}")
            self.sync_current_group_from_form()

        if not self.closed:
            self.root.after(0, apply)

    def place_window(self) -> None:
        self.root.update_idletasks()
        screen_w = self.root.winfo_screenwidth()
        screen_h = self.root.winfo_screenheight()
        width = min(560, max(460, screen_w // 4))
        height = min(max(780, screen_h - 80), 940)
        x = max(12, screen_w - width - 18)
        y = 20
        self.root.geometry(f"{width}x{height}+{x}+{y}")

    def place_settings_window(self) -> None:
        try:
            if self.settings_window is None or not self.settings_window.winfo_exists():
                return
            self.settings_window.update_idletasks()
            screen_w = self.settings_window.winfo_screenwidth()
            screen_h = self.settings_window.winfo_screenheight()
            width = min(860, max(620, screen_w - 180))
            height = min(max(820, screen_h - 120), 980)
            x = max(24, (screen_w - width) // 2)
            y = max(24, (screen_h - height) // 2)
            self.settings_window.geometry(f"{width}x{height}+{x}+{y}")
        except tk.TclError:
            pass

    def update_generator_state(self) -> None:
        sequential = self.mode_var.get() == "sequential"
        self.first_email_entry.configure(state="normal" if sequential else "disabled")
        self.random_domain_entry.configure(state="disabled" if sequential else "normal")

    def append_log(self, message: str, source: str = "ui") -> None:
        self.log_queue.put((source, truncate_log_message(message)))

    def set_server_status(self, value: str) -> None:
        if self.closed:
            return
        try:
            self.root.after(0, lambda: self.server_status_var.set(value))
        except tk.TclError:
            pass

    def set_batch_status(self, value: str) -> None:
        if self.closed:
            return
        try:
            self.root.after(0, lambda: self.batch_status_var.set(value))
        except tk.TclError:
            pass

    def set_progress_status(self, value: str) -> None:
        if self.closed:
            return
        try:
            self.root.after(0, lambda: self.progress_status_var.set(value))
        except tk.TclError:
            pass

    def set_current_status(self, value: str) -> None:
        if self.closed:
            return
        try:
            self.root.after(0, lambda: self.current_status_var.set(value))
        except tk.TclError:
            pass

    def set_result_status(self, value: str) -> None:
        if self.closed:
            return
        try:
            self.root.after(0, lambda: self.result_status_var.set(value))
        except tk.TclError:
            pass

    def set_duration_status(self, value: str) -> None:
        if self.closed:
            return
        try:
            self.root.after(0, lambda: self.duration_status_var.set(value))
        except tk.TclError:
            pass

    def update_duration_metrics(self, completed_count: int) -> None:
        total_elapsed = time.perf_counter() - self.batch_started_at if self.batch_started_at is not None else 0.0
        average = (self.batch_task_seconds_total / completed_count) if completed_count > 0 else 0.0
        self.set_duration_status(f"耗时: 总计 {format_duration(total_elapsed)} / 平均单号 {format_duration(average)}")

    def set_controls_running(self, running: bool) -> None:
        def apply() -> None:
            state = "disabled" if running else "normal"
            self.start_button.configure(state="disabled" if running else "normal")
            self.stop_button.configure(state="normal" if running else "disabled")
            self.email_widget.configure(state=state)
            self.first_email_entry.configure(state="disabled" if running or self.mode_var.get() != "sequential" else "normal")
            self.random_domain_entry.configure(state="disabled" if running or self.mode_var.get() != "random" else "normal")
            self.count_entry.configure(state=state)
        if self.closed:
            return
        try:
            self.root.after(0, apply)
        except tk.TclError:
            pass

    def flush_logs(self) -> None:
        if self.closed:
            return
        chunks: list[str] = []
        processed = 0
        while True:
            if processed >= MAX_LOG_MESSAGES_PER_FLUSH:
                break
            try:
                source, message = self.log_queue.get_nowait()
            except queue.Empty:
                break
            prefix = {"server": "[server] ", "signup": "[signup] ", "ui": ""}.get(source, "")
            chunks.append(f"{prefix}{message}\n")
            processed += 1

        if chunks:
            self.log_widget.configure(state="normal")
            self.log_widget.insert("end", "".join(chunks))
            self.trim_log_widget()
            self.log_widget.see("end")
            self.log_widget.configure(state="disabled")

        next_delay = LOG_FLUSH_BUSY_INTERVAL_MS if not self.log_queue.empty() else LOG_FLUSH_INTERVAL_MS
        try:
            self.root.after(next_delay, self.flush_logs)
        except tk.TclError:
            pass

    def trim_log_widget(self) -> None:
        current_chars = int(self.log_widget.count("1.0", "end-1c", "chars")[0])
        if current_chars <= MAX_LOG_WIDGET_CHARS:
            return
        trim_chars = current_chars - MAX_LOG_WIDGET_CHARS
        # Keep a bounded log buffer so long runs do not keep slowing the UI down.
        self.log_widget.delete("1.0", f"1.0 + {trim_chars} chars")

    def read_process_output(self, process: subprocess.Popen, source: str) -> None:
        if process.stdout is None:
            return
        try:
            for line in process.stdout:
                text = line.rstrip()
                if text:
                    self.append_log(text, source)
        except Exception as error:
            self.append_log(f"读取 {source} 输出失败: {error}")

    def set_restart_button_enabled(self, enabled: bool) -> None:
        if self.closed:
            return
        try:
            self.root.after(0, lambda: self.restart_server_button.configure(state="normal" if enabled else "disabled"))
        except tk.TclError:
            pass

    def normalize_path_string(self, value: str) -> str:
        return os.path.normcase(os.path.normpath(str(value or "").strip()))

    def current_server_matches_workspace(self, health: dict | None) -> bool:
        if not isinstance(health, dict):
            return False
        purchase_config_file = str(health.get("purchaseConfigFile") or "").strip()
        if not purchase_config_file:
            return False
        return self.normalize_path_string(purchase_config_file) == self.normalize_path_string(self.purchase_config_path)

    def get_local_api_port(self) -> int | None:
        parsed = urlparse(self.api_base)
        host = (parsed.hostname or "").strip().lower()
        if host not in LOCAL_SERVER_HOSTS:
            return None
        if parsed.port is not None:
            return parsed.port
        return 443 if parsed.scheme == "https" else 80

    def list_local_listener_processes(self) -> list[dict]:
        port = self.get_local_api_port()
        if port is None:
            return []
        script = f"""
$conns = Get-NetTCPConnection -LocalPort {port} -State Listen -ErrorAction SilentlyContinue
if (-not $conns) {{
    '[]'
    exit 0
}}
$items = foreach ($conn in ($conns | Sort-Object -Property OwningProcess -Unique)) {{
    $proc = Get-CimInstance Win32_Process -Filter "ProcessId = $($conn.OwningProcess)" -ErrorAction SilentlyContinue
    if ($proc) {{
        [pscustomobject]@{{
            pid = [int]$conn.OwningProcess
            commandLine = [string]$proc.CommandLine
            executablePath = [string]$proc.ExecutablePath
            localAddress = [string]$conn.LocalAddress
        }}
    }}
}}
$items | ConvertTo-Json -Compress
"""
        result = subprocess.run(
            ["powershell", "-NoProfile", "-Command", script],
            capture_output=True,
            text=True,
            encoding="utf-8",
            errors="replace",
            creationflags=CREATE_NO_WINDOW,
            timeout=10,
            check=False,
        )
        payload = result.stdout.strip() or "[]"
        if result.returncode != 0:
            raise RuntimeError(result.stderr.strip() or "PowerShell 查询监听进程失败")
        parsed = json.loads(payload)
        if isinstance(parsed, dict):
            return [parsed]
        return parsed if isinstance(parsed, list) else []

    def is_safe_server_process(self, process_info: dict) -> bool:
        command_line = str(process_info.get("commandLine") or "").lower()
        executable_path = str(process_info.get("executablePath") or "").lower()
        return "server.py" in command_line and ("python" in command_line or executable_path.endswith("python.exe"))

    def terminate_process_by_pid(self, pid: int) -> None:
        subprocess.run(
            ["taskkill", "/PID", str(pid), "/T", "/F"],
            capture_output=True,
            text=True,
            encoding="utf-8",
            errors="replace",
            creationflags=CREATE_NO_WINDOW,
            timeout=15,
            check=False,
        )

    def stop_existing_server(self, reason: str) -> bool:
        port = self.get_local_api_port()
        if port is None:
            self.append_log(f"API 地址 {self.api_base} 不是本地地址，无法自动关闭旧进程。")
            return False

        try:
            listeners = self.list_local_listener_processes()
        except Exception as error:
            self.append_log(f"查询占用 {port} 端口的进程失败: {error}")
            return False

        if not listeners:
            self.append_log(f"未发现占用 {port} 端口的监听进程。")
            return True

        unsafe = [item for item in listeners if not self.is_safe_server_process(item)]
        if unsafe:
            for item in unsafe:
                self.append_log(
                    f"检测到非 server.py 进程占用 {port} 端口，未自动关闭: PID {item.get('pid')} {item.get('commandLine') or item.get('executablePath') or ''}"
                )
            return False

        self.append_log(f"{reason}，准备关闭 {port} 端口上的旧 server.py 进程...")
        for item in listeners:
            pid = int(item.get("pid") or 0)
            if pid <= 0:
                continue
            self.append_log(f"关闭旧进程 PID {pid} ...")
            self.terminate_process_by_pid(pid)
            if self.server_process and self.server_process.pid == pid:
                self.server_process = None

        deadline = time.time() + 15
        while time.time() < deadline:
            if not check_api_health(self.api_base):
                self.server_ready = False
                return True
            time.sleep(0.5)

        self.append_log("旧进程关闭后端口仍未释放。")
        return False

    def start_server_process(self) -> bool:
        if not self.server_path.exists():
            self.set_server_status("Server: server.py 不存在")
            self.append_log(f"未找到 {self.server_path}")
            return False

        self.append_log("正在启动当前目录的 server.py ...")
        self.server_process = subprocess.Popen(
            [sys.executable, "-u", str(self.server_path)],
            cwd=str(ROOT),
            stdin=subprocess.DEVNULL,
            stdout=subprocess.PIPE,
            stderr=subprocess.STDOUT,
            text=True,
            encoding="utf-8",
            errors="replace",
            bufsize=1,
            creationflags=CREATE_NO_WINDOW,
        )
        threading.Thread(target=self.read_process_output, args=(self.server_process, "server"), daemon=True).start()

        deadline = time.time() + 20
        while time.time() < deadline:
            health = check_api_health(self.api_base)
            if health:
                if self.current_server_matches_workspace(health):
                    self.server_ready = True
                    self.set_server_status("Server: 已启动")
                    self.refresh_purchase_settings_async()
                    self.append_log("当前目录的 server.py 启动成功。")
                    return True
                self.append_log("新启动的 API 已响应，但配置文件路径仍不匹配当前目录。")
                break
            if self.server_process.poll() is not None:
                self.set_server_status("Server: 启动失败")
                self.append_log(f"server.py 已退出，退出码 {self.server_process.returncode}")
                return False
            time.sleep(1)

        self.set_server_status("Server: 启动超时")
        self.append_log("等待当前目录 server.py 就绪超时。")
        return False

    def ensure_server_running(self, force_restart: bool = False) -> None:
        self.set_restart_button_enabled(False)
        self.server_ready = False
        try:
            self.append_log("检查本地 API 状态...")
            health = check_api_health(self.api_base)
            if health and self.current_server_matches_workspace(health) and not force_restart:
                self.server_ready = True
                self.set_server_status("Server: 已运行")
                self.refresh_purchase_settings_async()
                self.append_log("本地 API 已在运行，且配置属于当前目录。")
                return

            if health:
                purchase_config_file = str(health.get("purchaseConfigFile") or "").strip() or "<unknown>"
                reason = "检测到旧的本地 API"
                if force_restart:
                    reason = "手动请求重启本地 API"
                self.append_log(
                    f"{reason}，当前进程使用的配置文件是 {purchase_config_file}，期望为 {self.purchase_config_path}"
                )
                self.set_server_status("Server: 重启中...")
                if not self.stop_existing_server(reason):
                    self.set_server_status("Server: 无法关闭旧进程")
                    return
            else:
                try:
                    listeners = self.list_local_listener_processes()
                except Exception as error:
                    self.append_log(f"检查本地监听进程失败: {error}")
                    listeners = []
                if listeners:
                    reason = "检测到占用 API 端口但未响应的旧进程"
                    self.append_log(f"{reason}，准备先释放端口后再启动。")
                    self.set_server_status("Server: 重启中...")
                    if not self.stop_existing_server(reason):
                        self.set_server_status("Server: 无法关闭旧进程")
                        return
                elif force_restart:
                    self.append_log("当前没有检测到运行中的本地 API，直接启动新的 server.py。")

            self.start_server_process()
        finally:
            self.set_restart_button_enabled(True)

    def request_server_restart(self) -> None:
        if self.server_thread and self.server_thread.is_alive():
            self.append_log("本地 API 启动或重启仍在进行中，请稍后。")
            return
        self.server_thread = threading.Thread(target=self.ensure_server_running, kwargs={"force_restart": True}, daemon=True)
        self.server_thread.start()

    def bootstrap_server(self) -> None:
        self.ensure_server_running(force_restart=False)

    def wait_for_server_ready(self, timeout_seconds: int = 20) -> bool:
        deadline = time.time() + timeout_seconds
        while time.time() < deadline:
            if self.server_ready:
                return True
            health = check_api_health(self.api_base)
            if health and self.current_server_matches_workspace(health):
                self.server_ready = True
                self.set_server_status("Server: 已运行")
                self.refresh_purchase_settings_async()
                return True
            time.sleep(1)
        return False

    def generate_sequential_emails(self, first_email: str, total: int) -> list[str]:
        normalized = first_email.strip()
        if "@" not in normalized:
            raise ValueError("顺序前缀模式需要输入完整邮箱，例如 user001@example.com")
        local_part, domain = normalized.split("@", 1)
        match = re.match(r"^(.*?)(\d+)$", local_part)
        if not match:
            raise ValueError("顺序前缀模式要求邮箱前缀以数字结尾，例如 user001@example.com 或 useruser001@example.com")
        prefix, number_text = match.groups()
        width = len(number_text)
        start = int(number_text)
        return [f"{prefix}{start + offset:0{width}d}@{domain}" for offset in range(total)]

    def generate_random_emails(self, domain: str, total: int) -> list[str]:
        normalized_domain = domain.strip()
        if not normalized_domain:
            raise ValueError("随机前缀模式需要填写域名，例如 example.com")
        results: list[str] = []
        seen: set[str] = set()
        while len(results) < total:
            address = f"{generate_random_local_part()}@{normalized_domain}"
            if address in seen:
                continue
            seen.add(address)
            results.append(address)
        return results

    def generate_queue(self) -> None:
        total = parse_positive_int(self.count_var.get(), default=1)
        try:
            if self.mode_var.get() == "sequential":
                emails = self.generate_sequential_emails(self.first_email_var.get(), total)
            else:
                emails = self.generate_random_emails(self.random_domain_var.get(), total)
        except ValueError as error:
            messagebox.showerror("生成失败", str(error), parent=self.root)
            return

        self.email_widget.configure(state="normal")
        self.email_widget.delete("1.0", "end")
        self.email_widget.insert("1.0", "\n".join(emails))
        self.append_log(f"已生成 {len(emails)} 个待注册邮箱。")
        self.set_progress_status(f"进度: 0/{len(emails)}")
        self.set_current_status("当前: 未开始")

    def get_email_queue(self) -> list[str | None]:
        raw = self.email_widget.get("1.0", "end").splitlines()
        emails = [line.strip() for line in raw if line.strip()]
        if not emails:
            return [None]
        return emails

    def launch_signup_process(self, email: str | None) -> subprocess.Popen:
        command = [sys.executable, "-u", str(self.signup_path), "--close-on-success"]
        if email:
            command.extend(["--email", email])
        process = subprocess.Popen(
            command,
            cwd=str(ROOT),
            stdin=subprocess.DEVNULL,
            stdout=subprocess.PIPE,
            stderr=subprocess.STDOUT,
            text=True,
            encoding="utf-8",
            errors="replace",
            bufsize=1,
            creationflags=CREATE_NO_WINDOW,
        )
        threading.Thread(target=self.read_process_output, args=(process, "signup"), daemon=True).start()
        return process

    def cleanup_signup_artifacts(self) -> None:
        try:
            from chatgpt_signup_to_code import cleanup_automation_artifacts

            removed = cleanup_automation_artifacts(stale_max_age_seconds=0)
            if removed:
                self.append_log(f"已清理 {removed} 个注册临时目录。")
        except Exception as error:
            self.append_log(f"清理注册残留失败: {error}")

    def start_batch(self) -> None:
        if self.batch_thread and self.batch_thread.is_alive():
            self.append_log("已有批次在运行。")
            return
        if not self.signup_path.exists():
            self.append_log(f"未找到 {self.signup_path}")
            return

        emails = self.get_email_queue()
        total = len(emails)
        self.stop_requested.clear()
        self.batch_started_at = time.perf_counter()
        self.batch_task_seconds_total = 0.0
        self.set_controls_running(True)
        self.set_batch_status("批次: 准备启动...")
        self.set_progress_status(f"进度: 0/{total}")
        self.set_current_status("当前: 准备中")
        self.set_result_status("结果: 成功 0 / 跳过 0 / 失败 0")
        self.update_duration_metrics(0)
        self.append_log("开始批量注册...")
        self.batch_thread = threading.Thread(target=self.run_batch, args=(emails,), daemon=True)
        self.batch_thread.start()

    def run_batch(self, emails: list[str | None]) -> None:
        total = len(emails)
        success_count = 0
        skipped_count = 0
        failed_count = 0
        completed_count = 0

        if not self.wait_for_server_ready():
            self.append_log("本地 API 未在超时内就绪，批次终止。")
            self.finish_batch("批次: 启动失败", total, completed_count, success_count, skipped_count, failed_count, "当前: 无")
            return

        for index, email in enumerate(emails, start=1):
            if self.stop_requested.is_set():
                break

            label = email or "<自动创建邮箱>"
            task_started_at = time.perf_counter()
            self.set_batch_status(f"批次: 运行中 {index}/{total}")
            self.set_current_status(f"当前: {label}")
            self.append_log("")
            self.append_log(f"开始第 {index}/{total} 个注册: {label}")
            self.cleanup_signup_artifacts()

            try:
                self.signup_process = self.launch_signup_process(email)
            except Exception as error:
                failed_count += 1
                completed_count += 1
                self.append_log(f"注册脚本启动失败: {error}")
                self.set_progress_status(f"进度: {completed_count}/{total}")
                self.set_result_status(f"结果: 成功 {success_count} / 跳过 {skipped_count} / 失败 {failed_count}")
                continue

            return_code = self.signup_process.wait()
            self.signup_process = None
            self.cleanup_signup_artifacts()

            if self.stop_requested.is_set():
                self.append_log("当前批次已收到停止请求。")
                break

            completed_count += 1
            self.batch_task_seconds_total += time.perf_counter() - task_started_at
            if return_code == 0:
                success_count += 1
                self.append_log(f"第 {index}/{total} 个注册完成。")
            elif return_code == 2:
                skipped_count += 1
                self.append_log(f"第 {index}/{total} 个邮箱未收到验证码，已跳过。")
            else:
                failed_count += 1
                self.append_log(f"第 {index}/{total} 个注册失败，退出码 {return_code}。")

            self.set_progress_status(f"进度: {completed_count}/{total}")
            self.set_result_status(f"结果: 成功 {success_count} / 跳过 {skipped_count} / 失败 {failed_count}")
            self.update_duration_metrics(completed_count)

            if index < total:
                self.append_log("等待 2 秒后开始下一个邮箱...")
                time.sleep(2)

        if self.stop_requested.is_set():
            batch_status = f"批次: 已停止"
        else:
            batch_status = f"批次: 已完成"
        self.cleanup_signup_artifacts()
        self.append_log(f"{batch_status} 成功 {success_count} 跳过 {skipped_count} 失败 {failed_count}")
        self.finish_batch(batch_status, total, completed_count, success_count, skipped_count, failed_count, "当前: 无")

    def finish_batch(
        self,
        batch_status: str,
        total: int,
        completed_count: int,
        success_count: int,
        skipped_count: int,
        failed_count: int,
        current_status: str,
    ) -> None:
        self.set_batch_status(batch_status)
        self.set_progress_status(f"进度: {completed_count}/{total}")
        self.set_current_status(current_status)
        self.set_result_status(f"结果: 成功 {success_count} / 跳过 {skipped_count} / 失败 {failed_count}")
        self.update_duration_metrics(completed_count)
        self.set_controls_running(False)
        self.signup_process = None

    def stop_batch(self) -> None:
        if not (self.batch_thread and self.batch_thread.is_alive()):
            self.append_log("当前没有运行中的批次。")
            self.set_controls_running(False)
            return

        self.append_log("正在停止当前批次...")
        self.stop_requested.set()
        self.set_batch_status("批次: 停止中...")
        if self.signup_process and self.signup_process.poll() is None:
            try:
                self.signup_process.terminate()
            except OSError as error:
                self.append_log(f"停止注册脚本失败: {error}")

    def on_close(self) -> None:
        self.closed = True
        self.stop_requested.set()
        if self.signup_process and self.signup_process.poll() is None:
            try:
                self.signup_process.terminate()
            except OSError:
                pass
        self.root.destroy()

    def run(self) -> None:
        self.root.mainloop()


def main() -> int:
    app = LauncherApp()
    app.run()
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
