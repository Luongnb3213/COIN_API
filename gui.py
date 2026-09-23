from __future__ import annotations

import json
import logging
import queue
import threading
import tkinter as tk
from pathlib import Path
from tkinter import scrolledtext
from tkinter import filedialog, messagebox, ttk

import api_main
from src import config
from src.connections.xlsx_connection import create_template


CONFIG_FILE = Path(__file__).resolve().parent / "config.json"


class TkLogHandler(logging.Handler):
    def __init__(self, log_queue: queue.Queue[str]) -> None:
        super().__init__(logging.INFO)
        self.log_queue = log_queue
        self.setFormatter(logging.Formatter("%(asctime)s | %(levelname)-7s | %(name)s | %(message)s"))

    def emit(self, record: logging.LogRecord) -> None:
        try:
            self.log_queue.put(self.format(record))
        except Exception:
            pass


class CoinApiApp(tk.Tk):
    def __init__(self) -> None:
        super().__init__()
        self.title("COIN_API")
        self.geometry("1120x760")
        self.minsize(940, 620)
        self.running = False
        self.log_queue: queue.Queue[str] = queue.Queue()
        self.stats_text = tk.StringVar(value="PENDING: 0 | PROCESSING: 0 | SUCCESS: 0 | FAILED: 0 | RETRY: 0")

        cfg = self._load_config()
        self.xlsx_path = tk.StringVar(value=str(cfg.get("xlsx_path") or config.XLSX_PATH))
        self.worker_count = tk.IntVar(value=int(cfg.get("worker_count") or 1))
        self.run_limit = tk.IntVar(value=int(cfg.get("run_limit") or 0))
        self.otpbase_api_key = tk.StringVar(value=str(cfg.get("otpbase_api_key") or config.OTPBASE_API_KEY))
        self.use_proxy = tk.BooleanVar(value=bool(cfg.get("use_proxy", config.USE_PROXY)))

        self._install_log_handler()
        self._build_ui()
        self.after(150, self._poll_logs)
        self.after(1000, self._refresh_stats)
        self.protocol("WM_DELETE_WINDOW", self._on_close)

    @staticmethod
    def _load_config() -> dict:
        if CONFIG_FILE.exists():
            with open(CONFIG_FILE, "r", encoding="utf-8") as f:
                return json.load(f)
        return {}

    def _save_config(self) -> None:
        cfg = self._load_config()
        cfg.update(
            {
                "xlsx_path": self.xlsx_path.get().strip(),
                "worker_count": max(1, int(self.worker_count.get() or 1)),
                "run_limit": max(0, int(self.run_limit.get() or 0)),
                "otpbase_api_key": self.otpbase_api_key.get().strip(),
                "use_proxy": bool(self.use_proxy.get()),
            }
        )
        with open(CONFIG_FILE, "w", encoding="utf-8") as f:
            json.dump(cfg, f, ensure_ascii=False, indent=2)

        config.XLSX_PATH = Path(cfg["xlsx_path"]).expanduser()
        config.WORKER_COUNT = cfg["worker_count"]
        config.RUN_LIMIT = cfg["run_limit"]
        config.OTPBASE_API_KEY = cfg["otpbase_api_key"]
        config.USE_PROXY = cfg["use_proxy"]

    def _build_ui(self) -> None:
        root = ttk.Frame(self, padding=16)
        root.pack(fill="both", expand=True)
        root.columnconfigure(0, weight=1)
        root.rowconfigure(2, weight=1)

        settings = ttk.LabelFrame(root, text="Cấu hình", padding=12)
        settings.grid(row=0, column=0, sticky="ew")
        settings.columnconfigure(1, weight=1)

        ttk.Label(settings, text="File Excel").grid(row=0, column=0, sticky="w", pady=4)
        ttk.Entry(settings, textvariable=self.xlsx_path).grid(row=0, column=1, sticky="ew", padx=8)
        ttk.Button(settings, text="Chọn", command=self.choose_xlsx).grid(row=0, column=2, sticky="ew")

        controls = ttk.Frame(settings)
        controls.grid(row=1, column=1, sticky="w", padx=8, pady=8)
        ttk.Label(settings, text="Worker").grid(row=1, column=0, sticky="w", pady=4)
        ttk.Spinbox(controls, from_=1, to=50, textvariable=self.worker_count, width=10).pack(side="left")
        ttk.Label(controls, text="Limit").pack(side="left", padx=(24, 6))
        ttk.Spinbox(controls, from_=0, to=100000, textvariable=self.run_limit, width=10).pack(side="left")

        ttk.Label(settings, text="OTPBase API key").grid(row=2, column=0, sticky="w", pady=4)
        ttk.Entry(settings, textvariable=self.otpbase_api_key).grid(row=2, column=1, columnspan=2, sticky="ew", padx=8)

        ttk.Checkbutton(settings, text="Dùng proxy trong sheet Proxies", variable=self.use_proxy).grid(row=3, column=1, sticky="w", padx=8, pady=4)

        actions = ttk.Frame(root)
        actions.grid(row=1, column=0, sticky="ew", pady=10)
        self.template_button = ttk.Button(actions, text="Tạo template", command=self.create_template)
        self.template_button.pack(side="left")
        self.run_button = ttk.Button(actions, text="Chạy", command=self.run_batch)
        self.run_button.pack(side="left", padx=8)
        self.stop_button = ttk.Button(actions, text="Dừng", command=self.stop_batch)
        self.stop_button.pack(side="left")
        ttk.Button(actions, text="Xoá log", command=self.clear_log).pack(side="left", padx=(18, 8))
        ttk.Button(actions, text="Copy log", command=self.copy_log).pack(side="left")
        ttk.Label(actions, textvariable=self.stats_text).pack(side="right")

        log_frame = ttk.LabelFrame(root, text="Log", padding=8)
        log_frame.grid(row=2, column=0, sticky="nsew")
        log_frame.columnconfigure(0, weight=1)
        log_frame.rowconfigure(0, weight=1)

        self.status_text = scrolledtext.ScrolledText(log_frame, height=26, wrap="word", font=("Menlo", 12))
        self.status_text.grid(row=0, column=0, sticky="nsew")
        self.status_text.configure(state="disabled")
        self._log("Sẵn sàng.")
        self._log(f"Log file: {api_main.LOG_FILE}")

    def _log(self, message: str) -> None:
        self.status_text.configure(state="normal")
        self.status_text.insert("end", message + "\n")
        self.status_text.see("end")
        self.status_text.configure(state="disabled")

    def clear_log(self) -> None:
        self.status_text.configure(state="normal")
        self.status_text.delete("1.0", "end")
        self.status_text.configure(state="disabled")

    def copy_log(self) -> None:
        text = self.status_text.get("1.0", "end-1c")
        self.clipboard_clear()
        self.clipboard_append(text)
        self._log("Đã copy log vào clipboard.")

    def _install_log_handler(self) -> None:
        self._log_handler = TkLogHandler(self.log_queue)
        self._log_handler._coin_api_gui = True
        root = logging.getLogger()
        for handler in list(root.handlers):
            if getattr(handler, "_coin_api_gui", False):
                root.removeHandler(handler)
        logging.getLogger().addHandler(self._log_handler)

    def _poll_logs(self) -> None:
        while True:
            try:
                message = self.log_queue.get_nowait()
            except queue.Empty:
                break
            self._log(message)
        self.after(150, self._poll_logs)

    def _refresh_stats(self) -> None:
        stats = dict(config.SESSION_STATS)
        self.stats_text.set(
            " | ".join(
                f"{key}: {stats.get(key, 0)}"
                for key in ("PENDING", "PROCESSING", "SUCCESS", "FAILED", "RETRY", "FAIL_NO_RETRY")
            )
        )
        self.after(1000, self._refresh_stats)

    def _set_running_state(self, running: bool) -> None:
        self.running = running
        self.run_button.configure(state="disabled" if running else "normal")
        self.template_button.configure(state="disabled" if running else "normal")

    def choose_xlsx(self) -> None:
        path = filedialog.askopenfilename(filetypes=[("Excel", "*.xlsx")])
        if path:
            self.xlsx_path.set(path)

    def create_template(self) -> None:
        self._save_config()
        create_template(Path(self.xlsx_path.get()).expanduser())
        self._log(f"Đã tạo template: {self.xlsx_path.get()}")

    def run_batch(self) -> None:
        if self.running:
            return
        self._save_config()
        self._set_running_state(True)
        self._log("Đang chạy...")
        threading.Thread(target=self._run_worker, daemon=True).start()

    def _run_worker(self) -> None:
        try:
            result = api_main.main(int(self.run_limit.get() or 0))
            self.after(0, lambda: self._log(f"Xong: {result}"))
        except Exception as exc:
            self.after(0, lambda: messagebox.showerror("COIN_API", str(exc)))
            self.after(0, lambda: self._log(f"Lỗi: {exc}"))
        finally:
            self.after(0, lambda: self._set_running_state(False))

    def stop_batch(self) -> None:
        config.STOP_FLAG = True
        self._log("Đã gửi yêu cầu dừng.")

    def _on_close(self) -> None:
        try:
            logging.getLogger().removeHandler(self._log_handler)
        except Exception:
            pass
        self.destroy()


if __name__ == "__main__":
    CoinApiApp().mainloop()
