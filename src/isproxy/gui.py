"""Tkinter control panel for isproxy."""

from __future__ import annotations

import queue
import sys
import threading
import time
import webbrowser
from urllib.parse import urlsplit

from .js_rules import DEFAULT_CONFIG_PATH, JsRewriteConfig, JsRule, load_config, save_config
from .proxy import ProxyServer, RequestRecord, Target, is_address_in_use

try:
    import tkinter as tk
    from tkinter import filedialog, messagebox, ttk
except ImportError:  # pragma: no cover - depends on the Python build
    tk = None  # type: ignore[assignment]
    filedialog = None  # type: ignore[assignment]
    messagebox = None  # type: ignore[assignment]
    ttk = None  # type: ignore[assignment]

DEFAULT_TARGET = ""
MAX_ROWS = 500
POLL_MS = 100

_COLUMNS = (
    ("time", "时间", 80, "center"),
    ("client", "客户端", 110, "center"),
    ("method", "方法", 70, "center"),
    ("path", "路径", 400, "w"),
    ("status", "状态", 60, "center"),
    ("edits", "改写", 60, "center"),
    ("ms", "耗时(ms)", 80, "e"),
)


def parse_target_url(url: str) -> tuple[Target, str]:
    """Split a page URL into the proxy origin and the resource path to open."""
    text = url.strip()
    if not text:
        raise ValueError("目标站点不能为空")
    target = Target.parse(text)
    origin = Target.parse(f"{target.scheme}://{target.netloc}")
    parts = urlsplit(text if "://" in text else "https://" + text)
    resource_path = parts.path or "/"
    if parts.query:
        resource_path += "?" + parts.query
    return origin, resource_path


class ProxyApp:
    def __init__(self) -> None:
        if tk is None:  # pragma: no cover - guarded by main()
            raise RuntimeError("tkinter is not available")

        self.root = tk.Tk()
        self.root.title("isproxy 反向代理")
        self.root.geometry("960x620")
        self.root.minsize(800, 480)

        self.events: queue.Queue[RequestRecord] = queue.Queue()
        self.server: ProxyServer | None = None
        self.server_thread: threading.Thread | None = None
        self.local_url = ""
        self.page_url = ""
        self.resource_path = "/"
        self._closing = False

        self.target_var = tk.StringVar(value=DEFAULT_TARGET)
        self.host_var = tk.StringVar(value="127.0.0.1")
        self.port_var = tk.StringVar(value="8080")
        self.insecure_var = tk.BooleanVar(value=False)
        self.status_var = tk.StringVar(value="未启动")
        self.parsed_var = tk.StringVar(value="")
        self.js_status_var = tk.StringVar(value="")
        self.js_config = JsRewriteConfig()
        self._js_config_error = ""
        self._load_js_config()

        self._build_ui()
        self.target_var.trace_add("write", self._update_parsed)
        self._update_parsed()
        self._refresh_js_status()
        self.root.protocol("WM_DELETE_WINDOW", self._on_close)
        self.root.after(POLL_MS, self._drain_events)

    # -- layout -----------------------------------------------------------
    def _build_ui(self) -> None:
        self.root.columnconfigure(0, weight=1)
        self.root.rowconfigure(2, weight=1)

        settings = ttk.LabelFrame(self.root, text="代理设置", padding=10)
        settings.grid(row=0, column=0, sticky="ew", padx=12, pady=(12, 6))
        settings.columnconfigure(1, weight=1)

        ttk.Label(settings, text="目标页面").grid(row=0, column=0, sticky="w", pady=2)
        self.target_entry = ttk.Entry(settings, textvariable=self.target_var)
        self.target_entry.grid(row=0, column=1, columnspan=5, sticky="ew", padx=(8, 0), pady=2)

        ttk.Label(settings, textvariable=self.parsed_var, foreground="#6b7280").grid(
            row=1, column=1, columnspan=5, sticky="w", padx=(8, 0), pady=(0, 4)
        )

        ttk.Label(settings, text="监听地址").grid(row=2, column=0, sticky="w", pady=2)
        self.host_entry = ttk.Entry(settings, textvariable=self.host_var, width=16)
        self.host_entry.grid(row=2, column=1, sticky="w", padx=(8, 0), pady=2)

        ttk.Label(settings, text="端口").grid(row=2, column=2, sticky="e", padx=(16, 0), pady=2)
        self.port_entry = ttk.Entry(settings, textvariable=self.port_var, width=8)
        self.port_entry.grid(row=2, column=3, sticky="w", padx=(8, 0), pady=2)

        self.insecure_check = ttk.Checkbutton(
            settings,
            text="跳过上游证书校验",
            variable=self.insecure_var,
        )
        self.insecure_check.grid(row=2, column=4, columnspan=2, sticky="w", padx=(16, 0), pady=2)

        ttk.Label(settings, textvariable=self.js_status_var, foreground="#6b7280").grid(
            row=3, column=1, columnspan=6, sticky="w", padx=(8, 0), pady=(4, 0)
        )

        actions = ttk.Frame(self.root)
        actions.grid(row=1, column=0, sticky="ew", padx=12, pady=(6, 6))
        actions.columnconfigure(6, weight=1)

        self.start_button = ttk.Button(actions, text="启动", command=self.start)
        self.start_button.grid(row=0, column=0, padx=(0, 6))
        self.stop_button = ttk.Button(actions, text="停止", command=self.stop, state="disabled")
        self.stop_button.grid(row=0, column=1, padx=6)
        self.open_button = ttk.Button(actions, text="打开页面", command=self.open_page, state="disabled")
        self.open_button.grid(row=0, column=2, padx=6)
        self.copy_button = ttk.Button(
            actions,
            text="复制代理链接",
            command=self.copy_link,
            state="disabled",
        )
        self.copy_button.grid(row=0, column=3, padx=6)
        self.js_button = ttk.Button(actions, text="JS 修改规则…", command=self.edit_js_rules)
        self.js_button.grid(row=0, column=4, padx=6)
        self.clear_button = ttk.Button(actions, text="清空日志", command=self.clear_log)
        self.clear_button.grid(row=0, column=5, padx=6)
        ttk.Label(actions, textvariable=self.status_var, anchor="e").grid(
            row=0, column=6, sticky="e", padx=(12, 0)
        )

        log_frame = ttk.LabelFrame(self.root, text="请求日志", padding=6)
        log_frame.grid(row=2, column=0, sticky="nsew", padx=12, pady=(0, 12))
        log_frame.columnconfigure(0, weight=1)
        log_frame.rowconfigure(0, weight=1)

        self.tree = ttk.Treeview(
            log_frame,
            columns=[column[0] for column in _COLUMNS],
            show="headings",
            selectmode="browse",
        )
        for name, heading, width, anchor in _COLUMNS:
            self.tree.heading(name, text=heading)
            self.tree.column(
                name,
                width=width,
                minwidth=50,
                anchor=anchor,
                stretch=name == "path",
            )

        y_scroll = ttk.Scrollbar(log_frame, orient="vertical", command=self.tree.yview)
        x_scroll = ttk.Scrollbar(log_frame, orient="horizontal", command=self.tree.xview)
        self.tree.configure(yscrollcommand=y_scroll.set, xscrollcommand=x_scroll.set)
        self.tree.grid(row=0, column=0, sticky="nsew")
        y_scroll.grid(row=0, column=1, sticky="ns")
        x_scroll.grid(row=1, column=0, sticky="ew")

        self.tree.tag_configure("ok", foreground="#1a7f37")
        self.tree.tag_configure("redirect", foreground="#9a6700")
        self.tree.tag_configure("warn", foreground="#bc4c00")
        self.tree.tag_configure("error", foreground="#cf222e")

    # -- lifecycle --------------------------------------------------------
    def run(self) -> None:
        self.root.mainloop()

    def start(self) -> None:
        if self.server is not None:
            return

        host = self.host_var.get().strip() or "127.0.0.1"
        try:
            port_text = self.port_var.get().strip()
            if not port_text:
                raise ValueError("端口不能为空")
            port = int(port_text)
            if not 0 <= port <= 65535:
                raise ValueError("端口必须在 0-65535 之间")
            target, resource_path = parse_target_url(self.target_var.get())
        except ValueError as exc:
            messagebox.showerror("参数错误", str(exc), parent=self.root)
            return

        try:
            server = ProxyServer(
                (host, port),
                target,
                verify_tls=not self.insecure_var.get(),
                on_request=self.events.put,
                js_config=self.js_config,
            )
        except OSError as exc:
            message = f"无法监听 {host}:{port}\n{exc}"
            if is_address_in_use(exc):
                message += (
                    "\n\n该端口可能仍被上一个代理进程占用（Windows 下旧进程不会自动退出）。\n"
                    f"可以执行：netstat -ano | findstr :{port}\n"
                    "找到对应 PID 后执行：taskkill /PID <PID> /F\n"
                    "或者换一个端口，并清理浏览器对该本地地址的缓存。"
                )
            messagebox.showerror("启动失败", message, parent=self.root)
            return

        self.server = server
        self.server_thread = threading.Thread(
            target=server.serve_forever,
            name="isproxy-server",
            daemon=True,
        )
        self.server_thread.start()

        actual_host, actual_port = server.server_address[:2]
        display_host = "127.0.0.1" if actual_host in ("0.0.0.0", "::") else actual_host
        if ":" in display_host and not display_host.startswith("["):
            display_host = f"[{display_host}]"
        self.local_url = f"http://{display_host}:{actual_port}"
        self.resource_path = resource_path
        self.page_url = self.local_url + resource_path
        self.status_var.set(f"运行中：{self.page_url}  →  {target.origin}")

        self._set_editable(False)
        self.stop_button.configure(state="normal")
        self.open_button.configure(state="normal")
        self.copy_button.configure(state="normal")

    def stop(self) -> None:
        server = self.server
        if server is None:
            return

        self.server = None
        self.status_var.set("正在停止…")
        self.stop_button.configure(state="disabled")
        self.open_button.configure(state="disabled")
        self.copy_button.configure(state="disabled")
        try:
            server.shutdown()
            server.server_close()
            if self.server_thread is not None:
                self.server_thread.join(timeout=2)
        finally:
            self.server_thread = None
            self.local_url = ""
            self.page_url = ""
            self.resource_path = "/"
            self.status_var.set("已停止")
            self._set_editable(True)
            self.stop_button.configure(state="disabled")
            self.open_button.configure(state="disabled")
            self.copy_button.configure(state="disabled")

    def _load_js_config(self) -> None:
        if not DEFAULT_CONFIG_PATH.exists():
            self.js_config = JsRewriteConfig()
            return
        try:
            self.js_config = load_config(DEFAULT_CONFIG_PATH)
        except (OSError, ValueError) as exc:
            self.js_config = JsRewriteConfig(enabled=False, rules=())
            self._js_config_error = str(exc)

    def _refresh_js_status(self) -> None:
        config = self.js_config
        if self._js_config_error:
            self.js_status_var.set(f"JS 修改：配置读取失败（{self._js_config_error}）")
            return
        if not config.rules:
            self.js_status_var.set("JS 修改：未配置规则")
            return
        enabled_count = sum(1 for rule in config.rules if rule.enabled)
        if not config.enabled:
            self.js_status_var.set(f"JS 修改：已关闭（{enabled_count}/{len(config.rules)} 条启用）")
            return
        if enabled_count == 0:
            self.js_status_var.set(f"JS 修改：无启用规则（共 {len(config.rules)} 条）")
            return
        scopes = []
        if config.inline_scripts:
            scopes.append("内联")
        if config.external_scripts:
            scopes.append("外部 JS")
        scope_text = "/".join(scopes) if scopes else "无范围"
        self.js_status_var.set(
            f"JS 修改：开（{enabled_count}/{len(config.rules)} 条启用；{scope_text}）"
        )

    def _update_parsed(self, *_: object) -> None:
        text = self.target_var.get().strip()
        if not text:
            self.parsed_var.set("解析：请输入目标页面 URL")
            return
        try:
            target, resource_path = parse_target_url(text)
        except ValueError:
            self.parsed_var.set("解析：无法识别该 URL")
            return
        self.parsed_var.set(f"解析：域名 {target.origin}    资源路径 {resource_path}")

    def _set_editable(self, editable: bool) -> None:
        state = "normal" if editable else "disabled"
        for widget in (
            self.target_entry,
            self.host_entry,
            self.port_entry,
            self.insecure_check,
            self.start_button,
        ):
            widget.configure(state=state)

    def _on_close(self) -> None:
        self._closing = True
        self.stop()
        self.root.destroy()

    # -- actions ----------------------------------------------------------
    def open_page(self) -> None:
        if self.page_url:
            webbrowser.open(self.page_url)

    def copy_link(self) -> None:
        if not self.page_url:
            return
        self.root.clipboard_clear()
        self.root.clipboard_append(self.page_url)
        self.root.update_idletasks()

        previous = self.status_var.get()
        message = f"已复制：{self.page_url}"
        self.status_var.set(message)
        self.root.after(1600, lambda: self._restore_status(message, previous))

    def _restore_status(self, message: str, previous: str) -> None:
        if not self._closing and self.status_var.get() == message:
            self.status_var.set(previous)

    def clear_log(self) -> None:
        for item in self.tree.get_children():
            self.tree.delete(item)

    def edit_js_rules(self) -> None:
        window = tk.Toplevel(self.root)
        window.title("JS 修改规则")
        window.transient(self.root)
        window.geometry("900x600")
        window.minsize(720, 460)
        window.columnconfigure(0, weight=1)
        window.rowconfigure(2, weight=1)

        rules: list[JsRule] = list(self.js_config.rules)
        enabled_var = tk.BooleanVar(value=self.js_config.enabled)
        inline_var = tk.BooleanVar(value=self.js_config.inline_scripts)
        external_var = tk.BooleanVar(value=self.js_config.external_scripts)

        options = ttk.Frame(window)
        options.grid(row=0, column=0, sticky="ew", padx=12, pady=(12, 6))
        ttk.Checkbutton(options, text="启用 JS 修改", variable=enabled_var).pack(side="left")
        ttk.Checkbutton(options, text="HTML 内联 <script>", variable=inline_var).pack(
            side="left", padx=(18, 0)
        )
        ttk.Checkbutton(options, text="同源外部 JS 响应", variable=external_var).pack(
            side="left", padx=(18, 0)
        )

        ttk.Label(
            window,
            text=f"规则列表（双击编辑；保存到 {DEFAULT_CONFIG_PATH}）",
        ).grid(row=1, column=0, sticky="w", padx=12, pady=(4, 2))

        list_frame = ttk.Frame(window)
        list_frame.grid(row=2, column=0, sticky="nsew", padx=12)
        list_frame.columnconfigure(0, weight=1)
        list_frame.rowconfigure(0, weight=1)

        columns = ("state", "type", "find", "replace", "scope", "count")
        headings = {
            "state": "状态",
            "type": "类型",
            "find": "查找",
            "replace": "替换",
            "scope": "URL 范围",
            "count": "次数",
        }
        widths = {"state": 60, "type": 80, "find": 230, "replace": 230, "scope": 150, "count": 50}
        tree = ttk.Treeview(list_frame, columns=columns, show="headings", selectmode="browse")
        for name in columns:
            tree.heading(name, text=headings[name])
            tree.column(
                name,
                width=widths[name],
                minwidth=50,
                anchor="w",
                stretch=name in ("find", "replace"),
            )
        tree.tag_configure("disabled", foreground="#9ca3af")
        scroll = ttk.Scrollbar(list_frame, orient="vertical", command=tree.yview)
        tree.configure(yscrollcommand=scroll.set)
        tree.grid(row=0, column=0, sticky="nsew")
        scroll.grid(row=0, column=1, sticky="ns")

        toolbar = ttk.Frame(window)
        toolbar.grid(row=3, column=0, sticky="ew", padx=12, pady=(8, 0))

        def preview(value: str, limit: int = 70) -> str:
            text = value.replace("\r", "\\r").replace("\n", "\\n").replace("\t", "\\t")
            return text if len(text) <= limit else text[: limit - 1] + "…"

        def selected_index() -> int | None:
            selection = tree.selection()
            return int(selection[0]) if selection else None

        def update_buttons(*_: object) -> None:
            index = selected_index()
            state = "normal" if index is not None else "disabled"
            edit_button.configure(state=state)
            delete_button.configure(state=state)
            toggle_button.configure(state=state)
            up_button.configure(state="normal" if index is not None and index > 0 else "disabled")
            down_button.configure(
                state="normal" if index is not None and index < len(rules) - 1 else "disabled"
            )

        def refresh_tree(select: int | None = None) -> None:
            children = tree.get_children()
            if children:
                tree.delete(*children)
            for index, rule in enumerate(rules):
                flag_text = f"（{rule.flags}）" if rule.regex and rule.flags else ""
                tree.insert(
                    "",
                    "end",
                    iid=str(index),
                    values=(
                        "启用" if rule.enabled else "禁用",
                        ("正则" if rule.regex else "文本") + flag_text,
                        preview(rule.find),
                        preview(rule.replace),
                        rule.url or "全部",
                        rule.count if rule.count else "全部",
                    ),
                    tags=() if rule.enabled else ("disabled",),
                )
            if select is not None and 0 <= select < len(rules):
                iid = str(select)
                tree.selection_set(iid)
                tree.focus(iid)
                tree.see(iid)
            update_buttons()

        def edit_rule(initial: JsRule | None) -> JsRule | None:
            dialog = tk.Toplevel(window)
            dialog.title("编辑规则" if initial else "新增规则")
            dialog.transient(window)
            dialog.geometry("650x460")
            dialog.minsize(520, 380)
            dialog.columnconfigure(1, weight=1)
            dialog.rowconfigure(1, weight=1)

            ttk.Label(dialog, text="查找内容").grid(
                row=0, column=0, sticky="nw", padx=12, pady=(12, 4)
            )
            find_text = tk.Text(dialog, height=4, wrap="word", undo=True)
            find_text.grid(row=0, column=1, sticky="nsew", padx=(8, 12), pady=(12, 4))

            ttk.Label(dialog, text="替换内容").grid(
                row=1, column=0, sticky="nw", padx=12, pady=4
            )
            replace_text = tk.Text(dialog, height=6, wrap="word", undo=True)
            replace_text.grid(row=1, column=1, sticky="nsew", padx=(8, 12), pady=4)

            enabled_var = tk.BooleanVar(value=initial.enabled if initial else True)
            regex_var = tk.BooleanVar(value=initial.regex if initial else False)
            options_line = ttk.Frame(dialog)
            options_line.grid(row=2, column=1, sticky="w", padx=(8, 12), pady=4)
            ttk.Checkbutton(options_line, text="启用此规则", variable=enabled_var).pack(side="left")
            regex_check = ttk.Checkbutton(options_line, text="使用正则表达式", variable=regex_var)
            regex_check.pack(side="left", padx=(18, 0))

            flag_vars = {
                "i": tk.BooleanVar(value=initial is not None and "i" in initial.flags),
                "m": tk.BooleanVar(value=initial is not None and "m" in initial.flags),
                "s": tk.BooleanVar(value=initial is not None and "s" in initial.flags),
            }
            flags_frame = ttk.Frame(dialog)
            flags_frame.grid(row=3, column=1, sticky="w", padx=(8, 12), pady=4)
            ttk.Label(flags_frame, text="正则标志").pack(side="left")
            flag_checks = []
            for flag, label in (("i", "i 忽略大小写"), ("m", "m 多行"), ("s", "s 点匹配换行")):
                check = ttk.Checkbutton(flags_frame, text=label, variable=flag_vars[flag])
                check.pack(side="left", padx=(10, 0))
                flag_checks.append(check)

            def sync_flag_state(*_: object) -> None:
                state = "normal" if regex_var.get() else "disabled"
                for check in flag_checks:
                    check.configure(state=state)

            regex_var.trace_add("write", sync_flag_state)
            sync_flag_state()

            ttk.Label(dialog, text="URL 范围").grid(
                row=4, column=0, sticky="w", padx=12, pady=4
            )
            url_var = tk.StringVar(value=(initial.url or "") if initial else "")
            ttk.Entry(dialog, textvariable=url_var).grid(
                row=4, column=1, sticky="ew", padx=(8, 12), pady=4
            )
            ttk.Label(
                dialog,
                text='可选，例如 /python3/*；留空表示全部',
                foreground="#6b7280",
            ).grid(row=5, column=1, sticky="w", padx=(8, 12))

            ttk.Label(dialog, text="最大次数").grid(
                row=6, column=0, sticky="w", padx=12, pady=4
            )
            count_var = tk.StringVar(value=str(initial.count) if initial else "0")
            ttk.Spinbox(dialog, from_=0, to=1_000_000, textvariable=count_var, width=10).grid(
                row=6, column=1, sticky="w", padx=(8, 12), pady=4
            )
            ttk.Label(
                dialog,
                text="0 表示全部替换",
                foreground="#6b7280",
            ).grid(row=7, column=1, sticky="w", padx=(8, 12))

            error_var = tk.StringVar()
            ttk.Label(dialog, textvariable=error_var, foreground="#cf222e").grid(
                row=8, column=0, columnspan=2, sticky="w", padx=12, pady=(4, 0)
            )

            result: JsRule | None = None

            def confirm() -> None:
                nonlocal result
                flags = (
                    "".join(flag for flag, var in flag_vars.items() if var.get())
                    if regex_var.get()
                    else ""
                )
                try:
                    count = int(count_var.get().strip() or "0")
                    result = JsRule(
                        find=find_text.get("1.0", "end-1c"),
                        replace=replace_text.get("1.0", "end-1c"),
                        regex=regex_var.get(),
                        flags=flags,
                        url=url_var.get().strip() or None,
                        count=count,
                        enabled=enabled_var.get(),
                    )
                except ValueError as exc:
                    error_var.set(str(exc))
                    return
                dialog.destroy()

            ttk.Button(dialog, text="确定", command=confirm).grid(
                row=9, column=0, columnspan=2, sticky="e", padx=12, pady=12
            )
            dialog.bind("<Escape>", lambda _event: dialog.destroy())
            dialog.protocol("WM_DELETE_WINDOW", dialog.destroy)
            if initial is not None:
                find_text.insert("1.0", initial.find)
                replace_text.insert("1.0", initial.replace)
            find_text.focus_set()
            dialog.grab_set()
            dialog.wait_window()
            window.grab_set()
            return result

        def add_rule() -> None:
            rule = edit_rule(None)
            if rule is not None:
                rules.append(rule)
                refresh_tree(len(rules) - 1)

        def edit_selected() -> None:
            index = selected_index()
            if index is None:
                return
            rule = edit_rule(rules[index])
            if rule is not None:
                rules[index] = rule
                refresh_tree(index)

        def delete_selected() -> None:
            index = selected_index()
            if index is None:
                return
            if not messagebox.askyesno("删除规则", "确定删除选中的规则吗？", parent=window):
                return
            del rules[index]
            refresh_tree(min(index, len(rules) - 1) if rules else None)

        def move(delta: int) -> None:
            index = selected_index()
            if index is None:
                return
            target = index + delta
            if not 0 <= target < len(rules):
                return
            rules[index], rules[target] = rules[target], rules[index]
            refresh_tree(target)

        def toggle_selected() -> None:
            index = selected_index()
            if index is None:
                return
            rules[index] = rules[index].with_enabled(not rules[index].enabled)
            refresh_tree(index)

        def current_config() -> JsRewriteConfig:
            return JsRewriteConfig(
                enabled=enabled_var.get(),
                inline_scripts=inline_var.get(),
                external_scripts=external_var.get(),
                rules=tuple(rules),
            )

        def import_rules() -> None:
            path = filedialog.askopenfilename(
                parent=window,
                title="导入规则",
                filetypes=[("JSON 文件", "*.json"), ("所有文件", "*.*")],
            )
            if not path:
                return
            if rules and not messagebox.askyesno(
                "导入规则", "导入会替换当前规则列表，继续吗？", parent=window
            ):
                return
            try:
                imported = load_config(path)
            except (OSError, ValueError) as exc:
                messagebox.showerror("导入失败", str(exc), parent=window)
                return
            rules[:] = list(imported.rules)
            enabled_var.set(imported.enabled)
            inline_var.set(imported.inline_scripts)
            external_var.set(imported.external_scripts)
            refresh_tree(0 if rules else None)

        def export_rules() -> None:
            path = filedialog.asksaveasfilename(
                parent=window,
                title="导出规则",
                defaultextension=".json",
                initialfile="js_rules.json",
                filetypes=[("JSON 文件", "*.json")],
            )
            if not path:
                return
            try:
                save_config(path, current_config())
            except OSError as exc:
                messagebox.showerror("导出失败", str(exc), parent=window)
                return
            messagebox.showinfo("导出成功", f"已导出到 {path}", parent=window)

        add_button = ttk.Button(toolbar, text="新增", command=add_rule)
        add_button.grid(row=0, column=0, padx=(0, 6))
        edit_button = ttk.Button(toolbar, text="编辑", command=edit_selected)
        edit_button.grid(row=0, column=1, padx=6)
        delete_button = ttk.Button(toolbar, text="删除", command=delete_selected)
        delete_button.grid(row=0, column=2, padx=6)
        toggle_button = ttk.Button(toolbar, text="启用/禁用", command=toggle_selected)
        toggle_button.grid(row=0, column=3, padx=6)
        up_button = ttk.Button(toolbar, text="上移", command=lambda: move(-1))
        up_button.grid(row=0, column=4, padx=6)
        down_button = ttk.Button(toolbar, text="下移", command=lambda: move(1))
        down_button.grid(row=0, column=5, padx=6)
        ttk.Button(toolbar, text="导入 JSON…", command=import_rules).grid(
            row=0, column=6, padx=(18, 6)
        )
        ttk.Button(toolbar, text="导出 JSON…", command=export_rules).grid(
            row=0, column=7, padx=6
        )

        def save_config_and_close() -> None:
            config = current_config()
            try:
                save_config(DEFAULT_CONFIG_PATH, config)
            except OSError as exc:
                messagebox.showerror(
                    "保存失败",
                    f"无法写入 {DEFAULT_CONFIG_PATH}\n{exc}",
                    parent=window,
                )
                return
            self.js_config = config
            self._js_config_error = ""
            if self.server is not None:
                self.server.js_config = config
            self._refresh_js_status()
            window.destroy()

        bottom = ttk.Frame(window)
        bottom.grid(row=4, column=0, sticky="e", padx=12, pady=12)
        ttk.Button(bottom, text="保存并应用", command=save_config_and_close).pack(
            side="left", padx=(0, 6)
        )
        ttk.Button(bottom, text="取消", command=window.destroy).pack(side="left")

        tree.bind("<<TreeviewSelect>>", update_buttons)
        tree.bind("<Double-1>", lambda _event: edit_selected())
        refresh_tree(0 if rules else None)
        window.grab_set()
        tree.focus_set()

    # -- request log ------------------------------------------------------
    def _drain_events(self) -> None:
        if self._closing:
            return
        try:
            for _ in range(200):
                self._insert_record(self.events.get_nowait())
        except queue.Empty:
            pass
        finally:
            self.root.after(POLL_MS, self._drain_events)

    def _insert_record(self, record: RequestRecord) -> None:
        if record.status >= 500:
            tag = "error"
        elif record.status >= 400:
            tag = "warn"
        elif record.status >= 300:
            tag = "redirect"
        else:
            tag = "ok"

        self.tree.insert(
            "",
            "end",
            values=(
                time.strftime("%H:%M:%S", time.localtime(record.time)),
                record.client,
                record.method,
                record.path,
                record.status,
                record.js_edits,
                f"{record.duration * 1000:.0f}",
            ),
            tags=(tag,),
        )
        items = self.tree.get_children()
        if len(items) > MAX_ROWS:
            for item in items[: len(items) - MAX_ROWS]:
                self.tree.delete(item)
        self.tree.yview_moveto(1.0)


def main() -> int:
    if tk is None:
        print("错误：当前 Python 未包含 tkinter。", file=sys.stderr)
        print("Windows 官方安装包默认包含；Linux 可安装 python3-tk。", file=sys.stderr)
        return 1
    ProxyApp().run()
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
