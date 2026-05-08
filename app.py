
import json
import os
import sys
import queue
import threading
import time
import webbrowser
from pathlib import Path
from datetime import datetime
import tkinter as tk
from tkinter import ttk, filedialog, messagebox

import requests


APP_TITLE = "Replay Sync"

ROOT = Path(getattr(sys, "_MEIPASS", Path(__file__).parent))
ICON_FILE = ROOT / "app.ico"

APP_DIR = Path(os.getenv("APPDATA", Path.home())) / "ReplaySync"
CONFIG_FILE = APP_DIR / "config.json"
STATE_FILE = APP_DIR / "state.json"
HISTORY_FILE = APP_DIR / "history.json"

DEFAULT_DEMO_DIR = Path.home() / "Documents" / "My Games" / "Rocket League" / "TAGame" / "Demos"
UPLOAD_URL = "https://ballchasing.com/api/v2/upload"
API_ROOT = "https://ballchasing.com/api/"


def clock():
    return datetime.now().strftime("%H:%M:%S")


def full_time():
    return datetime.now().strftime("%Y-%m-%d %H:%M:%S")


def read_json(path, fallback):
    try:
        return json.loads(path.read_text(encoding="utf-8"))
    except Exception:
        return fallback


def write_json(path, data):
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(data, indent=2), encoding="utf-8")


def clean_token(value):
    return value.strip().replace("Bearer ", "").strip()


def stable(path, delay=2):
    try:
        first = path.stat().st_size
        time.sleep(delay)
        second = path.stat().st_size
        return first == second and second > 0
    except OSError:
        return False


def default_config():
    return {
        "demo_dir": str(DEFAULT_DEMO_DIR),
        "token": "",
        "visibility": "private",
        "group": "",
        "interval": 5,
        "file_age": 8,
    }


class SyncWorker:
    def __init__(self, events):
        self.events = events
        self.stop_event = threading.Event()
        self.baseline = set()
        self.synced = set(read_json(STATE_FILE, {"synced": []}).get("synced", []))
        self.cooldown_until = 0

    def emit(self, kind, data):
        self.events.put((kind, data))

    def log(self, text):
        self.emit("log", f"[{clock()}] {text}")

    def save_state(self):
        write_json(STATE_FILE, {"synced": sorted(self.synced)})

    def add_history(self, item):
        history = read_json(HISTORY_FILE, [])
        history.insert(0, item)
        history = history[:300]
        write_json(HISTORY_FILE, history)
        self.emit("history", item)

    def mark_current_files_as_baseline(self, folder):
        path = Path(folder)
        if not path.exists():
            self.baseline = set()
            return
        self.baseline = {str(item.resolve()) for item in path.glob("*.replay")}

    def watch_new(self, get_config):
        cfg = get_config()
        self.mark_current_files_as_baseline(cfg["demo_dir"])
        self.log("Watching started. Existing files are being ignored.")

        while not self.stop_event.is_set():
            cfg = get_config()
            folder = Path(cfg["demo_dir"])

            if time.time() < self.cooldown_until:
                time.sleep(3)
                continue

            if not folder.exists():
                self.log("Replay folder not found.")
                time.sleep(8)
                continue

            try:
                files = sorted(folder.glob("*.replay"), key=lambda item: item.stat().st_mtime)
            except OSError:
                time.sleep(8)
                continue

            for item in files:
                if self.stop_event.is_set():
                    break

                key = str(item.resolve())

                if key in self.baseline or key in self.synced:
                    continue

                try:
                    age = time.time() - item.stat().st_mtime
                except OSError:
                    continue

                if age < int(cfg["file_age"]):
                    continue

                if stable(item):
                    self.upload(item, cfg)
                    self.baseline.add(key)

            time.sleep(int(cfg["interval"]))

        self.log("Watching stopped.")

    def sync_existing(self, cfg):
        folder = Path(cfg["demo_dir"])
        if not folder.exists():
            self.log("Replay folder not found.")
            return

        try:
            files = sorted(folder.glob("*.replay"), key=lambda item: item.stat().st_mtime)
        except OSError:
            self.log("Could not read replay folder.")
            return

        self.log(f"Sync existing started. {len(files)} replay files found.")

        for item in files:
            if self.stop_event.is_set():
                break

            key = str(item.resolve())
            if key in self.synced:
                continue

            if stable(item):
                self.upload(item, cfg)
                self.baseline.add(key)

        self.log("Sync existing finished.")

    def upload(self, path, cfg):
        token = clean_token(cfg.get("token", ""))

        if not token:
            self.log("Missing API token.")
            return

        params = {"visibility": cfg.get("visibility", "private")}
        group = cfg.get("group", "").strip()

        if group:
            params["group"] = group

        self.log(f"Uploading {path.name}")

        try:
            with path.open("rb") as handle:
                response = requests.post(
                    UPLOAD_URL,
                    params=params,
                    headers={"Authorization": token},
                    files={"file": (path.name, handle, "application/octet-stream")},
                    timeout=90,
                )

            if response.status_code in (201, 409):
                try:
                    payload = response.json()
                except Exception:
                    payload = {}

                link = payload.get("location") or payload.get("link") or ""
                result = "uploaded" if response.status_code == 201 else "duplicate"

                self.synced.add(str(path.resolve()))
                self.save_state()

                self.add_history({
                    "time": full_time(),
                    "file": path.name,
                    "status": result,
                    "visibility": cfg.get("visibility", "private"),
                    "group": group,
                    "url": link,
                })

                self.log(f"{result.title()}: {link or path.name}")
                return

            if response.status_code == 401:
                self.add_history({
                    "time": full_time(),
                    "file": path.name,
                    "status": "unauthorized",
                    "visibility": cfg.get("visibility", "private"),
                    "group": group,
                    "url": "",
                })
                self.log("Token rejected.")
                return

            if response.status_code == 429:
                retry = response.headers.get("Retry-After")
                try:
                    wait = int(retry) if retry else 120
                except ValueError:
                    wait = 120
                self.cooldown_until = time.time() + max(30, wait)
                self.log(f"Rate limited. Cooling down for {wait}s.")
                return

            self.add_history({
                "time": full_time(),
                "file": path.name,
                "status": f"failed {response.status_code}",
                "visibility": cfg.get("visibility", "private"),
                "group": group,
                "url": "",
            })
            self.log(f"Upload failed: {response.status_code}")

        except Exception as exc:
            self.add_history({
                "time": full_time(),
                "file": path.name,
                "status": "local error",
                "visibility": cfg.get("visibility", "private"),
                "group": group,
                "url": "",
            })
            self.log(f"Upload error: {exc}")


class ReplaySync(tk.Tk):
    def __init__(self):
        super().__init__()

        self.title(APP_TITLE)

        try:
            self.iconbitmap(default=str(ICON_FILE))
            self.wm_iconbitmap(str(ICON_FILE))
        except Exception as exc:
            print("Icon load failed:", exc)

        self.geometry("860x600")
        self.minsize(800, 540)

        self.config_data = read_json(CONFIG_FILE, default_config())
        self.events = queue.Queue()
        self.worker = SyncWorker(self.events)
        self.watch_thread = None

        self.demo_var = tk.StringVar()
        self.token_var = tk.StringVar()
        self.visibility_var = tk.StringVar()
        self.group_var = tk.StringVar()
        self.interval_var = tk.StringVar()
        self.age_var = tk.StringVar()

        self.build_ui()
        self.load_form()
        self.load_history()
        self.after(200, self.drain_events)

    def build_ui(self):
        shell = ttk.Frame(self, padding=14)
        shell.pack(fill="both", expand=True)

        header = ttk.Frame(shell)
        header.pack(fill="x")

        ttk.Label(header, text="Replay Sync", font=("Segoe UI", 18, "bold")).pack(side="left")
        ttk.Label(header, text="automatic replay uploads", foreground="#666666").pack(side="left", padx=(12, 0))

        self.tabs = ttk.Notebook(shell)
        self.tabs.pack(fill="both", expand=True, pady=(12, 0))

        self.sync_tab = ttk.Frame(self.tabs, padding=12)
        self.history_tab = ttk.Frame(self.tabs, padding=12)
        self.log_tab = ttk.Frame(self.tabs, padding=12)

        self.tabs.add(self.sync_tab, text="Sync")
        self.tabs.add(self.history_tab, text="History")
        self.tabs.add(self.log_tab, text="Log")

        self.build_sync_tab()
        self.build_history_tab()
        self.build_log_tab()

    def build_sync_tab(self):
        box = ttk.LabelFrame(self.sync_tab, text="Settings", padding=12)
        box.pack(fill="x")

        ttk.Label(box, text="Replay folder").grid(row=0, column=0, sticky="w")
        ttk.Entry(box, textvariable=self.demo_var).grid(row=0, column=1, sticky="ew", padx=8)
        ttk.Button(box, text="Browse", command=self.pick_folder).grid(row=0, column=2)

        ttk.Label(box, text="API token").grid(row=1, column=0, sticky="w", pady=(8, 0))
        ttk.Entry(box, textvariable=self.token_var, show="•").grid(row=1, column=1, sticky="ew", padx=8, pady=(8, 0))
        ttk.Button(box, text="Test", command=self.test_token).grid(row=1, column=2, pady=(8, 0))

        ttk.Label(box, text="Visibility").grid(row=2, column=0, sticky="w", pady=(8, 0))
        ttk.Combobox(
            box,
            textvariable=self.visibility_var,
            values=["private", "unlisted", "public"],
            state="readonly",
            width=12,
        ).grid(row=2, column=1, sticky="w", padx=8, pady=(8, 0))

        ttk.Label(box, text="Group").grid(row=3, column=0, sticky="w", pady=(8, 0))
        ttk.Entry(box, textvariable=self.group_var).grid(row=3, column=1, sticky="ew", padx=8, pady=(8, 0))
        ttk.Button(box, text="Open site", command=lambda: webbrowser.open("https://ballchasing.com")).grid(row=3, column=2, pady=(8, 0))

        timing = ttk.Frame(box)
        timing.grid(row=4, column=0, columnspan=3, sticky="ew", pady=(12, 0))

        ttk.Label(timing, text="Scan every").pack(side="left")
        ttk.Entry(timing, textvariable=self.interval_var, width=5).pack(side="left", padx=4)
        ttk.Label(timing, text="sec   wait").pack(side="left")
        ttk.Entry(timing, textvariable=self.age_var, width=5).pack(side="left", padx=4)
        ttk.Label(timing, text="sec after new file").pack(side="left")

        box.columnconfigure(1, weight=1)

        actions = ttk.Frame(self.sync_tab)
        actions.pack(fill="x", pady=(14, 0))

        self.start_button = ttk.Button(actions, text="Start watching", command=self.start_watching)
        self.start_button.pack(side="left")

        self.stop_button = ttk.Button(actions, text="Stop watching", command=self.stop_watching, state="disabled")
        self.stop_button.pack(side="left", padx=8)

        ttk.Button(actions, text="Sync existing", command=self.sync_existing).pack(side="left", padx=(12, 0))
        ttk.Button(actions, text="Save settings", command=self.save_form).pack(side="right")

        note = (
            "Start watching only picks up replays created after you press Start. "
            "Use Sync existing when you intentionally want old replay files uploaded too."
        )
        ttk.Label(self.sync_tab, text=note, foreground="#666666", wraplength=740).pack(anchor="w", pady=(14, 0))

    def build_history_tab(self):
        columns = ("time", "file", "status", "visibility", "group", "url")
        self.history_tree = ttk.Treeview(self.history_tab, columns=columns, show="headings")

        headings = {
            "time": "Time",
            "file": "File",
            "status": "Status",
            "visibility": "Visibility",
            "group": "Group",
            "url": "Link",
        }

        widths = {
            "time": 145,
            "file": 230,
            "status": 105,
            "visibility": 75,
            "group": 90,
            "url": 260,
        }

        for column in columns:
            self.history_tree.heading(column, text=headings[column])
            self.history_tree.column(column, width=widths[column], anchor="w")

        self.history_tree.pack(fill="both", expand=True)
        self.history_tree.bind("<Double-1>", self.open_selected_link)

        actions = ttk.Frame(self.history_tab)
        actions.pack(fill="x", pady=(8, 0))

        ttk.Button(actions, text="Open link", command=self.open_selected_link).pack(side="left")
        ttk.Button(actions, text="Clear history", command=self.clear_history).pack(side="right")

    def build_log_tab(self):
        self.log_box = tk.Text(self.log_tab, wrap="word")
        self.log_box.pack(fill="both", expand=True)

    def load_form(self):
        cfg = self.config_data
        self.demo_var.set(cfg.get("demo_dir", str(DEFAULT_DEMO_DIR)))
        self.token_var.set(cfg.get("token", ""))
        self.visibility_var.set(cfg.get("visibility", "private"))
        self.group_var.set(cfg.get("group", ""))
        self.interval_var.set(str(cfg.get("interval", 5)))
        self.age_var.set(str(cfg.get("file_age", 8)))

    def save_form(self):
        try:
            self.config_data = {
                "demo_dir": self.demo_var.get().strip(),
                "token": clean_token(self.token_var.get()),
                "visibility": self.visibility_var.get() or "private",
                "group": self.group_var.get().strip(),
                "interval": max(2, int(self.interval_var.get())),
                "file_age": max(2, int(self.age_var.get())),
            }
        except ValueError:
            messagebox.showerror(APP_TITLE, "Scan and wait values must be numbers.")
            return False

        write_json(CONFIG_FILE, self.config_data)
        self.write_log("Settings saved.")
        return True

    def get_config(self):
        return self.config_data

    def pick_folder(self):
        folder = filedialog.askdirectory(initialdir=self.demo_var.get() or str(DEFAULT_DEMO_DIR))
        if folder:
            self.demo_var.set(folder)

    def test_token(self):
        if not self.save_form():
            return

        token = clean_token(self.config_data.get("token", ""))
        if not token:
            messagebox.showwarning(APP_TITLE, "Paste an API token first.")
            return

        try:
            response = requests.get(API_ROOT, headers={"Authorization": token}, timeout=20)
            if response.status_code == 200:
                self.write_log("Token OK.")
                messagebox.showinfo(APP_TITLE, "Token works.")
            else:
                self.write_log(f"Token failed: {response.status_code}")
                messagebox.showerror(APP_TITLE, f"Token failed: {response.status_code}")
        except Exception as exc:
            self.write_log(f"Token test error: {exc}")
            messagebox.showerror(APP_TITLE, str(exc))

    def start_watching(self):
        if not self.save_form():
            return

        self.worker.stop_event.clear()

        self.watch_thread = threading.Thread(
            target=self.worker.watch_new,
            args=(self.get_config,),
            daemon=True,
        )
        self.watch_thread.start()

        self.start_button.config(state="disabled")
        self.stop_button.config(state="normal")

    def stop_watching(self):
        self.worker.stop_event.set()
        self.start_button.config(state="normal")
        self.stop_button.config(state="disabled")

    def sync_existing(self):
        if not self.save_form():
            return

        approved = messagebox.askyesno(
            APP_TITLE,
            "Upload existing replay files that have not been synced yet?"
        )
        if not approved:
            return

        threading.Thread(
            target=self.worker.sync_existing,
            args=(self.config_data,),
            daemon=True,
        ).start()

    def load_history(self):
        for item in reversed(read_json(HISTORY_FILE, [])):
            self.add_history(item)

    def add_history(self, item):
        self.history_tree.insert(
            "",
            0,
            values=(
                item.get("time", ""),
                item.get("file", ""),
                item.get("status", ""),
                item.get("visibility", ""),
                item.get("group", ""),
                item.get("url", ""),
            ),
        )

    def clear_history(self):
        if not messagebox.askyesno(APP_TITLE, "Clear local upload history?"):
            return

        write_json(HISTORY_FILE, [])

        for item in self.history_tree.get_children():
            self.history_tree.delete(item)

        self.write_log("History cleared.")

    def open_selected_link(self, event=None):
        selected = self.history_tree.selection()
        if not selected:
            return

        values = self.history_tree.item(selected[0], "values")
        if len(values) >= 6 and values[5]:
            webbrowser.open(values[5])

    def write_log(self, text):
        self.log_box.insert("end", f"[{clock()}] {text}\n")
        self.log_box.see("end")

    def drain_events(self):
        while True:
            try:
                kind, data = self.events.get_nowait()
            except queue.Empty:
                break

            if kind == "log":
                self.write_log(data)
            elif kind == "history":
                self.add_history(data)

        self.after(200, self.drain_events)


if __name__ == "__main__":
    ReplaySync().mainloop()
