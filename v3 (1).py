#!/usr/bin/env python3
import os
import sys
import json
import time
import threading
import socket
import logging
import shutil
from datetime import datetime
from logging.handlers import RotatingFileHandler

from PyQt6.QtWidgets import (
    QApplication, QMainWindow, QWidget, QVBoxLayout, QHBoxLayout,
    QGridLayout, QLabel, QPushButton, QLineEdit, QSpinBox, QCheckBox,
    QComboBox, QGroupBox, QTabWidget, QTimeEdit, QFrame,
    QMessageBox, QFileDialog, QSlider, QFormLayout
)
from PyQt6.QtCore import Qt, QTimer, QThread, pyqtSignal, QTime, pyqtSlot
from PyQt6.QtGui import QFont

import win32gui
import win32process
import psutil

try:
    import mysql.connector as mysql
except ImportError:
    mysql = None

try:
    import mss
    from PIL import Image
except ImportError:
    mss = None
    Image = None

CONFIG_PATH = "config.json"
CONFIG_BACKUP_PATH = "config.json.bak"
EVENT_QUEUE_PATH = "event_queue.json"
SCREENSHOT_DIR = "monitor_screenshots"
os.makedirs(SCREENSHOT_DIR, exist_ok=True)

logger = logging.getLogger()
logger.setLevel(logging.DEBUG)  # Enable debug mode by default for diagnostics
handler = RotatingFileHandler('monitor.log', maxBytes=5 * 1024 * 1024, backupCount=3)
formatter = logging.Formatter('%(asctime)s - %(levelname)s - %(message)s')
handler.setFormatter(formatter)
logger.addHandler(handler)
logger.addHandler(logging.StreamHandler())

def now_str_file():
    return datetime.now().strftime("%Y-%m-%d_%H-%M-%S")

def now_str_db():
    return datetime.now().strftime("%Y-%m-%d %H:%M:%S")

class EnhancedConfig:
    def __init__(self, path=CONFIG_PATH):
        self.path = path
        self.data = {
            "mysql": {
                "enabled": True,
                "host": "127.0.0.1",
                "user": "monitor_user",
                "password": "password123",
                "database": "monitor_db",
                "table": "events",
                "port": 3306
            },
            "settings": {
                "hotkey": "<ctrl>+<shift>+m",
                "shutdown_timer_minutes": 30,
                "auto_resume_minutes": 30,
                "always_on_top": False,
                "mode": "title",
                "poll_interval": 2.0,
                "debug_mode": True,  # Default to True for diagnostics
                "auto_sync_minutes": 5
            },
            "scheduling": {
                "enabled": False,
                "active_days": [1, 2, 3, 4, 5],
                "start_time": "09:00:00",
                "end_time": "17:00:00",
            },
            "screenshots": {
                "enabled": True,
                "interval_seconds": 300,
                "on_events_only": False,
                "quality": 60,
                "max_files": 500,
                "auto_cleanup_days": 15,
                "format": "auto"
            },
            "ui": {
                "theme": "dark",
                "font_size": 10,
                "window_size": [900, 700],
                "window_position": [100, 100]
            }
        }
        self.load()

    def load(self):
        if not os.access(self.path, os.R_OK):
            logging.warning(f"Config file {self.path} not readable, using defaults")
            return
        if os.path.exists(self.path):
            try:
                with open(self.path, "r", encoding="utf-8") as f:
                    data = json.load(f)
                    self._deep_merge(self.data, data)
                    logging.debug(f"Config loaded: {json.dumps(self.data, indent=2)}")
            except Exception as e:
                logging.error(f"Failed to load config: {e}")
                logging.warning("Using default config due to load failure")

    def _deep_merge(self, base, update):
        for key, value in update.items():
            if isinstance(value, dict) and key in base and isinstance(base[key], dict):
                self._deep_merge(base[key], value)
            else:
                base[key] = value

    def save(self):
        try:
            if not os.access(os.path.dirname(self.path) or '.', os.W_OK):
                logging.error(f"Cannot write to config directory: {os.path.dirname(self.path)}")
                return False
            # Create backup before saving
            if os.path.exists(self.path):
                shutil.copy2(self.path, CONFIG_BACKUP_PATH)
                logging.debug(f"Created config backup: {CONFIG_BACKUP_PATH}")
            with open(self.path, "w", encoding="utf-8") as f:
                json.dump(self.data, f, indent=2, ensure_ascii=False)
                logging.debug(f"Config saved: {json.dumps(self.data, indent=2)}")
            return True
        except Exception as e:
            logging.error(f"Failed to save config: {e}")
            return False

    def get(self, section, key, default=None):
        value = self.data.get(section, {}).get(key, default)
        logging.debug(f"Config get: {section}.{key} = {value}")
        return value

    def set(self, section, key, value):
        if section not in self.data:
            self.data[section] = {}
        self.data[section][key] = value
        logging.debug(f"Config set: {section}.{key} = {value}")

class DBClient:
    """Database client with queue, hostname logging, and auto-reconnect"""

    def __init__(self, config, status_callback=None):
        self.config = config
        self.conn = None
        self.status_callback = status_callback or (lambda x, c: None)
        self.lock = threading.Lock()
        self.queue = []
        self.hostname = socket.gethostname()

        self._load_queue()
        self._try_connect()

        # Keepalive / reconnect thread
        self.keepalive_thread = threading.Thread(target=self._keepalive_loop, daemon=True)
        self.keepalive_thread.start()

    def _set_status(self, text, color="blue"):
        try:
            self.status_callback(text, color)
        except Exception:
            pass

    @property
    def table(self):
        return self.config.get("mysql", "table", "events")

    def _load_queue(self):
        if os.path.exists(EVENT_QUEUE_PATH):
            try:
                with open(EVENT_QUEUE_PATH, "r", encoding="utf-8") as f:
                    self.queue = json.load(f)
            except Exception:
                self.queue = []

    def _save_queue(self):
        try:
            with open(EVENT_QUEUE_PATH, "w", encoding="utf-8") as f:
                json.dump(self.queue, f, indent=2)
        except Exception:
            pass

    def _try_connect(self):
        """Try to connect to MySQL, create table if needed, and flush queue"""
        if not self.config.get("mysql", "enabled", True):
            self.conn = None
            self._set_status("MySQL: Disabled", "blue")
            return

        if mysql is None:
            self._set_status("MySQL: Driver missing ❌", "red")
            return

        try:
            mysql_config = self.config.data["mysql"]
            if self.conn:
                try:
                    self.conn.close()
                except:
                    pass

            self.conn = mysql.connect(
                host=mysql_config["host"],
                user=mysql_config["user"],
                password=mysql_config["password"],
                database=mysql_config["database"],
                port=mysql_config.get("port", 3306),
                autocommit=True
            )
            self._set_status("MySQL: Connected ✅", "green")

            # Ensure table exists
            cursor = self.conn.cursor()
            cursor.execute(f"""
                CREATE TABLE IF NOT EXISTS {self.table} (
                    id INT AUTO_INCREMENT PRIMARY KEY,
                    event VARCHAR(255) NOT NULL,
                    timestamp DATETIME NOT NULL,
                    window_title TEXT,
                    extra JSON,
                    hostname VARCHAR(255)
                )
            """)
            cursor.close()

            self.flush_queue()

        except Exception as e:
            self.conn = None
            self._set_status(f"MySQL: Disconnected ❌ ({str(e)[:50]})", "red")

    def _keepalive_loop(self):
        """Background thread to keep DB connection alive and reconnect if lost"""
        while True:
            if self.conn is None:
                self._try_connect()
            else:
                try:
                    cursor = self.conn.cursor()
                    cursor.execute("SELECT 1")
                    cursor.close()
                except Exception:
                    self.conn = None
                    self._set_status("MySQL: Connection lost ❌", "red")
            time.sleep(15)

    def enqueue(self, row):
        """Save events locally when DB is not connected"""
        with self.lock:
            row["hostname"] = self.hostname
            self.queue.append(row)
            self._save_queue()

    def flush_queue(self):
        """Flush queued events to DB if connected"""
        if self.conn is None or not self.queue:
            return
        with self.lock:
            try:
                cursor = self.conn.cursor()
                for event in self.queue:
                    cursor.execute(
                        f"INSERT INTO {self.table} (event, timestamp, window_title, extra, hostname) VALUES (%s,%s,%s,%s,%s)",
                        (
                            event["event"],
                            event["timestamp"],
                            event.get("window_title"),
                            json.dumps(event.get("extra", {})),
                            event.get("hostname", self.hostname),
                        ),
                    )
                self.queue.clear()
                cursor.close()
                if os.path.exists(EVENT_QUEUE_PATH):
                    os.remove(EVENT_QUEUE_PATH)
                logging.info("Queue flushed successfully.")
            except Exception as e:
                logging.error(f"Failed to flush queue: {e}")

    def write_event(self, event, window_title=None, extra=None):
        """Write event directly to DB or queue if disconnected"""
        row = {
            "event": event,
            "timestamp": now_str_db(),
            "window_title": window_title,
            "extra": extra or {},
            "hostname": self.hostname,
        }
        if self.conn is None:
            self.enqueue(row)
            return

        try:
            cursor = self.conn.cursor()
            cursor.execute(
                f"INSERT INTO {self.table} (event, timestamp, window_title, extra, hostname) VALUES (%s,%s,%s,%s,%s)",
                (row["event"], row["timestamp"], row["window_title"], json.dumps(row["extra"]), row["hostname"]),
            )
            cursor.close()
        except Exception:
            self.enqueue(row)

class ScheduleChecker:
    """Check if monitoring should be active based on schedule settings"""
    def __init__(self, config):
        self.config = config

    def is_monitoring_time(self):
        now = datetime.now()
        schedule = self.config.data["scheduling"]

        if not schedule.get("enabled", False):
            return True

        current_day = now.isoweekday()  # 1=Mon..7=Sun
        active_days = schedule.get("active_days", [])
        if active_days and current_day not in active_days:
            return False

        start_time_str = schedule.get("start_time", "00:00:00")
        end_time_str = schedule.get("end_time", "23:59:59")

        try:
            start_time = datetime.strptime(start_time_str, "%H:%M:%S").time()
            end_time = datetime.strptime(end_time_str, "%H:%M:%S").time()
            current_time = now.time()
            if start_time <= end_time:
                return start_time <= current_time <= end_time
            else:
                # overnight window
                return current_time >= start_time or current_time <= end_time
        except Exception as e:
            logging.error(f"Error parsing time range: {e}")
            return True

class MonitorThread(QThread):
    """Background monitor for window presence"""
    status_update = pyqtSignal(str, str)          # (message, color)
    window_state_changed = pyqtSignal(bool)       # exists
    schedule_status_changed = pyqtSignal(bool)    # in_schedule

    def __init__(self, parent=None):
        super().__init__(parent)
        self.parent_app = parent
        self.running = True
        self.monitoring_enabled = True
        self.last_exists = None
        self.last_in_schedule = None

    def run(self):
        poll_interval = float(self.parent_app.config.get("settings", "poll_interval", 2.0))
        while self.running:
            try:
                if self.parent_app.has_target():
                    in_schedule = self.parent_app.schedule_checker.is_monitoring_time()

                    if self.last_in_schedule is None:
                        self.last_in_schedule = in_schedule
                    elif in_schedule != self.last_in_schedule:
                        self.schedule_status_changed.emit(in_schedule)
                        self.last_in_schedule = in_schedule

                    if in_schedule and self.monitoring_enabled:
                        exists = self.parent_app.check_window_exists()

                        if self.last_exists is None:
                            self.last_exists = exists
                        elif exists != self.last_exists:
                            self.window_state_changed.emit(exists)
                            self.last_exists = exists

                self.msleep(int(poll_interval * 1000))
            except Exception as e:
                logging.error(f"Monitor thread error: {e}")
                self.msleep(5000)

    def stop(self):
        self.running = False
        self.wait()

# --- Hotkey setup: prefer pynput, fallback to keyboard ---
try:
    from pynput import keyboard as pynput_keyboard
    from pynput.keyboard import GlobalHotKeys
    PYNPUT_AVAILABLE = True
except Exception:
    PYNPUT_AVAILABLE = False

try:
    import keyboard
    KEYBOARD_AVAILABLE = True
except Exception:
    KEYBOARD_AVAILABLE = False

class HotkeyManager:
    """Global hotkey manager with pynput/keyboard fallback"""
    def __init__(self, hotkey_str, callback, status_cb=None):
        self.hotkey = hotkey_str
        self.callback = callback
        self.status_cb = status_cb or (lambda ok, msg: None)
        self._listener = None
        self._registered = False
        self._setup()

    def _setup(self):
        # Try pynput first
        if PYNPUT_AVAILABLE:
            try:
                def handler():
                    try:
                        self.callback()
                    except Exception as e:
                        logging.error(f"Hotkey callback error: {e}")

                mapping = {self.hotkey: handler}
                self._listener = GlobalHotKeys(mapping)
                self._listener.start()
                self._registered = True
                self.status_cb(True, "pynput")
                return
            except Exception as e:
                logging.debug(f"pynput GlobalHotKeys failed: {e}")

        # Fallback to keyboard module
        if KEYBOARD_AVAILABLE:
            try:
                keyboard.add_hotkey(self.hotkey, self.callback)
                self._registered = True
                self.status_cb(True, "keyboard")
                return
            except Exception as e:
                logging.debug(f"keyboard.add_hotkey failed: {e}")

        self._registered = False
        self.status_cb(False, "none")

    def unregister(self):
        try:
            if PYNPUT_AVAILABLE and getattr(self._listener, 'stop', None):
                try:
                    self._listener.stop()
                except Exception:
                    pass
            if KEYBOARD_AVAILABLE:
                try:
                    keyboard.unhook_all_hotkeys()
                except Exception:
                    pass
        except Exception:
            pass

class WindowMonitorApp(QMainWindow):
    """Main PyQt6 window with monitoring, screenshots, DB, and settings"""

    def __init__(self):
        super().__init__()
        self.config = EnhancedConfig()
        self.schedule_checker = ScheduleChecker(self.config)

        # State
        self.target_value = None
        self.target_pid = None
        self.mode = self.config.get("settings", "mode", "title")
        self.monitoring_active = True
        self.shutdown_scheduled = False
        self.shutdown_timer = None
        self.remaining_seconds = 0

        # Ensure screenshot directory exists
        os.makedirs(SCREENSHOT_DIR, exist_ok=True)

        # Database client
        self.db = DBClient(self.config, self.update_db_status)

        # UI + Timers
        self.setup_ui()
        self.setup_timers()

        # Hotkeys
        self.hotkey_manager = None
        self.setup_hotkeys()

        # Monitor thread
        self.monitor_thread = MonitorThread(self)
        self.monitor_thread.status_update.connect(self.update_status)
        self.monitor_thread.window_state_changed.connect(self.handle_window_state_change)
        self.monitor_thread.schedule_status_changed.connect(self.handle_schedule_change)
        self.monitor_thread.start()

        # Apply saved settings to widgets
        self.apply_saved_settings()

        # Log any discrepancies between widget values and config
        self.check_config_discrepancies()

    # ---------------- UI -----------------
    def apply_theme(self, theme="dark"):
        logging.debug(f"Applying theme: {theme}")
        if theme.lower() == "dark":
            self.setStyleSheet("""
                QMainWindow { background-color: #2b2b2b; color: #ffffff; }
                QTabWidget::pane { border: 1px solid #555555; background-color: #353535; }
                QTabBar::tab { background-color: #404040; color: #ffffff; padding: 8px 16px; margin: 2px; border-radius: 4px; }
                QTabBar::tab:selected { background-color: #0078d4; }
                QGroupBox { font-weight: bold; border: 2px solid #555555; border-radius: 5px; margin: 10px 0px; padding-top: 15px; }
                QPushButton { background-color: #0078d4; border: none; color: white; padding: 8px 16px; border-radius: 4px; font-weight: bold; }
                QLineEdit, QSpinBox, QComboBox, QTimeEdit { background-color: #404040; border: 1px solid #555555; border-radius: 4px; padding: 6px; color: #ffffff; }
                QLabel { color: #ffffff; }
                QTextEdit { background-color: #404040; border: 1px solid #555555; border-radius: 4px; color: #ffffff; }
            """)
        else:
            self.setStyleSheet("""
                QMainWindow { background-color: #f0f0f0; color: #000000; }
                QTabWidget::pane { border: 1px solid #cccccc; background-color: #ffffff; }
                QTabBar::tab { background-color: #e0e0e0; color: #000000; padding: 8px 16px; margin: 2px; border-radius: 4px; }
                QTabBar::tab:selected { background-color: #0078d4; color: white; }
                QGroupBox { font-weight: bold; border: 2px solid #cccccc; border-radius: 5px; margin: 10px 0px; padding-top: 15px; }
                QPushButton { background-color: #0078d4; border: none; color: white; padding: 8px 16px; border-radius: 4px; font-weight: bold; }
                QLineEdit, QSpinBox, QComboBox, QTimeEdit { background-color: #ffffff; border: 1px solid #cccccc; border-radius: 4px; padding: 6px; color: #000000; }
                QLabel { color: #000000; }
                QTextEdit { background-color: #ffffff; border: 1px solid #cccccc; border-radius: 4px; color: #000000; }
            """)

    def setup_ui(self):
        self.setWindowTitle("Advanced Window Monitor")
        self.setMinimumSize(900, 700)

        central = QWidget()
        self.setCentralWidget(central)
        root = QVBoxLayout(central)

        self.tab_widget = QTabWidget()
        root.addWidget(self.tab_widget)

        self.setup_main_tab()
        self.setup_schedule_tab()
        self.setup_screenshot_tab()
        self.setup_database_tab()
        self.setup_settings_tab()

        self.statusBar().showMessage("Ready")

    def setup_main_tab(self):
        tab = QWidget()
        self.tab_widget.addTab(tab, "Monitor")
        layout = QVBoxLayout(tab)

        lbl = QLabel("Click the window you want to monitor and press the hotkey")
        lbl.setFont(QFont("Segoe UI", 11))
        layout.addWidget(lbl)

        self.target_label = QLabel("Monitoring: (not set)")
        self.target_label.setFont(QFont("Segoe UI", 11))
        layout.addWidget(self.target_label)

        status_frame = QFrame()
        grid = QGridLayout(status_frame)
        self.status_label = QLabel("Status: Idle")
        self.schedule_status_label = QLabel("Schedule: Always Active")
        self.db_status_label = QLabel("Database: Checking...")
        self.countdown_label = QLabel("Countdown: --:--:--")
        grid.addWidget(self.status_label, 0, 0)
        grid.addWidget(self.schedule_status_label, 0, 1)
        grid.addWidget(self.db_status_label, 1, 0)
        grid.addWidget(self.countdown_label, 1, 1)
        layout.addWidget(status_frame)

        controls = QGroupBox("Controls")
        ch = QHBoxLayout(controls)
        self.pause_btn = QPushButton("Pause Monitoring")
        self.pause_btn.clicked.connect(self.toggle_monitoring)
        self.pause_btn.setEnabled(False)
        ch.addWidget(self.pause_btn)

        self.always_on_top_cb = QCheckBox("Always on Top")
        self.always_on_top_cb.toggled.connect(self.toggle_always_on_top)
        ch.addWidget(self.always_on_top_cb)
        layout.addWidget(controls)

        mode_group = QGroupBox("Monitor Mode")
        form = QFormLayout(mode_group)
        self.mode_combo = QComboBox()
        self.mode_combo.addItems(["title", "class", "pid"])
        self.mode_combo.currentTextChanged.connect(self.mode_changed)
        form.addRow("Mode:", self.mode_combo)
        layout.addWidget(mode_group)

        hotkey_group = QGroupBox("Hotkey Settings")
        f2 = QFormLayout(hotkey_group)
        self.hotkey_edit = QLineEdit()
        apply_hotkey_btn = QPushButton("Apply")
        apply_hotkey_btn.clicked.connect(self.apply_hotkey)
        row = QHBoxLayout()
        row.addWidget(self.hotkey_edit, 3)
        row.addWidget(apply_hotkey_btn, 1)
        f2.addRow("Hotkey:", row)
        self.current_hotkey_label = QLabel("")
        f2.addRow("Current:", self.current_hotkey_label)
        layout.addWidget(hotkey_group)

        layout.addStretch()

    def setup_schedule_tab(self):
        tab = QWidget()
        self.tab_widget.addTab(tab, "Schedule")
        layout = QVBoxLayout(tab)

        self.schedule_enabled_cb = QCheckBox("Enable Scheduling")
        self.schedule_enabled_cb.toggled.connect(self.schedule_enabled_changed)
        layout.addWidget(self.schedule_enabled_cb)

        days_group = QGroupBox("Active Days")
        days_layout = QHBoxLayout(days_group)
        self.day_checkboxes = {}
        days = ["Monday", "Tuesday", "Wednesday", "Thursday", "Friday", "Saturday", "Sunday"]
        for i, day in enumerate(days, 1):
            cb = QCheckBox(day)
            cb.toggled.connect(self.days_changed)
            self.day_checkboxes[i] = cb
            days_layout.addWidget(cb)
        layout.addWidget(days_group)

        time_group = QGroupBox("Time Range")
        time_form = QFormLayout(time_group)
        self.start_time_edit = QTimeEdit()
        self.start_time_edit.setTime(QTime(9, 0))
        self.start_time_edit.timeChanged.connect(self.time_changed)
        time_form.addRow("Start Time:", self.start_time_edit)

        self.end_time_edit = QTimeEdit()
        self.end_time_edit.setTime(QTime(17, 0))
        self.end_time_edit.timeChanged.connect(self.time_changed)
        time_form.addRow("End Time:", self.end_time_edit)
        layout.addWidget(time_group)

        layout.addStretch()

    def setup_screenshot_tab(self):
        tab = QWidget()
        self.tab_widget.addTab(tab, "Screenshots")
        layout = QVBoxLayout(tab)

        self.screenshots_enabled_cb = QCheckBox("Enable Screenshots")
        self.screenshots_enabled_cb.toggled.connect(self.screenshots_enabled_changed)
        layout.addWidget(self.screenshots_enabled_cb)

        settings_group = QGroupBox("Settings")
        form = QFormLayout(settings_group)

        self.screenshot_interval_spin = QSpinBox()
        self.screenshot_interval_spin.setRange(1, 3600)
        self.screenshot_interval_spin.setValue(300)
        self.screenshot_interval_spin.setSuffix(" seconds")
        self.screenshot_interval_spin.valueChanged.connect(self.screenshot_settings_changed)
        form.addRow("Interval:", self.screenshot_interval_spin)

        self.events_only_cb = QCheckBox("Only on Events")
        self.events_only_cb.toggled.connect(self.screenshot_settings_changed)
        form.addRow(self.events_only_cb)

        qrow = QHBoxLayout()
        self.quality_slider = QSlider(Qt.Orientation.Horizontal)
        self.quality_slider.setRange(10, 100)
        self.quality_slider.setValue(85)
        self.quality_slider.valueChanged.connect(self.quality_changed)
        self.quality_label = QLabel("85%")
        qrow.addWidget(self.quality_slider, 3)
        qrow.addWidget(self.quality_label, 1)
        form.addRow("Quality:", qrow)

        self.max_files_spin = QSpinBox()
        self.max_files_spin.setRange(10, 10000)
        self.max_files_spin.setValue(1000)
        self.max_files_spin.valueChanged.connect(self.screenshot_settings_changed)
        form.addRow("Max Files:", self.max_files_spin)

        self.cleanup_days_spin = QSpinBox()
        self.cleanup_days_spin.setRange(1, 365)
        self.cleanup_days_spin.setValue(30)
        self.cleanup_days_spin.setSuffix(" days")
        self.cleanup_days_spin.valueChanged.connect(self.screenshot_settings_changed)
        form.addRow("Auto Cleanup:", self.cleanup_days_spin)

        layout.addWidget(settings_group)

        folder_group = QGroupBox("Folder")
        row = QHBoxLayout(folder_group)
        self.folder_label = QLabel(os.path.abspath(SCREENSHOT_DIR))
        open_folder_btn = QPushButton("Open Folder")
        open_folder_btn.clicked.connect(self.open_screenshot_folder)
        row.addWidget(self.folder_label, 3)
        row.addWidget(open_folder_btn, 1)

        layout.addWidget(folder_group)
        layout.addStretch()

    def setup_database_tab(self):
        tab = QWidget()
        self.tab_widget.addTab(tab, "Database")
        layout = QVBoxLayout(tab)

        # Enable/Disable DB
        self.db_enabled_cb = QCheckBox("Enable Database Connection")
        self.db_enabled_cb.setChecked(self.config.get("mysql", "enabled", True))
        self.db_enabled_cb.toggled.connect(self.toggle_db_enabled)
        layout.addWidget(self.db_enabled_cb)

        conn_group = QGroupBox("Connection Settings")
        form = QFormLayout(conn_group)

        self.db_host_edit = QLineEdit()
        form.addRow("Host:", self.db_host_edit)

        self.db_port_spin = QSpinBox()
        self.db_port_spin.setRange(1, 65535)
        self.db_port_spin.setValue(3306)
        form.addRow("Port:", self.db_port_spin)

        self.db_user_edit = QLineEdit()
        form.addRow("Username:", self.db_user_edit)

        self.db_password_edit = QLineEdit()
        self.db_password_edit.setEchoMode(QLineEdit.EchoMode.Password)
        form.addRow("Password:", self.db_password_edit)

        self.db_database_edit = QLineEdit()
        form.addRow("Database:", self.db_database_edit)

        self.db_table_edit = QLineEdit()
        form.addRow("Table:", self.db_table_edit)

        # Auto-sync interval control
        self.auto_sync_spin = QSpinBox()
        self.auto_sync_spin.setRange(1, 1440)  # 1 min to 24h
        self.auto_sync_spin.setValue(self.config.get("settings", "auto_sync_minutes", 5))
        self.auto_sync_spin.valueChanged.connect(self.auto_sync_interval_changed)
        form.addRow("Auto Sync Interval:", self.auto_sync_spin)

        # Buttons
        btn_row = QHBoxLayout()
        test_btn = QPushButton("Test Connection")
        test_btn.clicked.connect(self.test_db_connection)
        apply_db_btn = QPushButton("Apply Settings")
        apply_db_btn.clicked.connect(self.apply_db_settings)
        manual_sync_btn = QPushButton("Sync Now")
        manual_sync_btn.clicked.connect(self.manual_sync_to_db)
        btn_row.addWidget(test_btn)
        btn_row.addWidget(apply_db_btn)
        btn_row.addWidget(manual_sync_btn)
        form.addRow(btn_row)

        layout.addWidget(conn_group)

        status_group = QGroupBox("Status")
        sform = QFormLayout(status_group)
        self.db_connection_status = QLabel("Checking...")
        sform.addRow("Connection:", self.db_connection_status)

        self.hostname_label = QLabel(socket.gethostname())
        sform.addRow("This machine:", self.hostname_label)

        self.queue_status = QLabel("0 events")
        sform.addRow("Queue:", self.queue_status)
        layout.addWidget(status_group)

        layout.addStretch()

    def setup_settings_tab(self):
        tab = QWidget()
        self.tab_widget.addTab(tab, "Settings")
        layout = QVBoxLayout(tab)

        gen = QGroupBox("General")
        form = QFormLayout(gen)

        self.shutdown_timer_spin = QSpinBox()
        self.shutdown_timer_spin.setRange(1, 1440)
        self.shutdown_timer_spin.setValue(self.config.get("settings", "shutdown_timer_minutes", 30))
        self.shutdown_timer_spin.setSuffix(" minutes")
        self.shutdown_timer_spin.valueChanged.connect(self.shutdown_timer_changed)
        form.addRow("Shutdown Timer:", self.shutdown_timer_spin)

        self.auto_resume_spin = QSpinBox()
        self.auto_resume_spin.setRange(0, 1440)
        self.auto_resume_spin.setValue(self.config.get("settings", "auto_resume_minutes", 30))
        self.auto_resume_spin.setSuffix(" minutes (0 = disabled)")
        self.auto_resume_spin.valueChanged.connect(self.auto_resume_changed)
        form.addRow("Auto Resume:", self.auto_resume_spin)

        self.poll_interval_spin = QSpinBox()
        self.poll_interval_spin.setRange(1, 60)
        self.poll_interval_spin.setValue(int(self.config.get("settings", "poll_interval", 2)))
        self.poll_interval_spin.setSuffix(" seconds")
        self.poll_interval_spin.valueChanged.connect(self.poll_interval_changed)
        form.addRow("Poll Interval:", self.poll_interval_spin)

        self.debug_mode_cb = QCheckBox("Debug Mode")
        self.debug_mode_cb.setChecked(self.config.get("settings", "debug_mode", True))
        self.debug_mode_cb.toggled.connect(self.toggle_debug_mode)
        form.addRow(self.debug_mode_cb)

        layout.addWidget(gen)

        # Appearance
        theme_group = QGroupBox("Appearance")
        tform = QFormLayout(theme_group)
        self.theme_combo = QComboBox()
        self.theme_combo.addItems(["Dark", "Light"])
        self.theme_combo.currentTextChanged.connect(self.change_theme)
        tform.addRow("Theme:", self.theme_combo)

        self.font_size_spin = QSpinBox()
        self.font_size_spin.setRange(8, 16)
        self.font_size_spin.setValue(self.config.get("ui", "font_size", 10))
        self.font_size_spin.valueChanged.connect(self.change_font_size)
        tform.addRow("Font Size:", self.font_size_spin)

        layout.addWidget(theme_group)

        # Config management
        cfg_group = QGroupBox("Configuration")
        row = QHBoxLayout(cfg_group)
        save_btn = QPushButton("Save Configuration")
        save_btn.clicked.connect(self.save_configuration)
        load_btn = QPushButton("Load Configuration")
        load_btn.clicked.connect(self.load_configuration)
        export_btn = QPushButton("Export Configuration")
        export_btn.clicked.connect(self.export_configuration)
        row.addWidget(save_btn)
        row.addWidget(load_btn)
        row.addWidget(export_btn)
        layout.addWidget(cfg_group)

        layout.addStretch()

    # -------------- Timers --------------
    def setup_timers(self):
        self.countdown_timer = QTimer()
        self.countdown_timer.timeout.connect(self.update_countdown_display)
        self.countdown_timer.start(1000)

        self.queue_timer = QTimer()
        self.queue_timer.timeout.connect(self.update_queue_status)
        self.queue_timer.start(5000)

        self.screenshot_timer = QTimer()
        self.screenshot_timer.timeout.connect(self._screenshot_timer_tick)
        interval = int(self.config.get("screenshots", "interval_seconds", 300) * 1000)
        self.screenshot_timer.setInterval(interval)
        if self.config.get("screenshots", "enabled", True) and not self.config.get("screenshots", "on_events_only", False):
            self.screenshot_timer.start()

        # Auto-sync timer
        self.auto_sync_timer = QTimer()
        interval_minutes = int(self.config.get("settings", "auto_sync_minutes", 5))
        self.auto_sync_timer.setInterval(interval_minutes * 60 * 1000)
        self.auto_sync_timer.timeout.connect(self.auto_sync_to_db)
        self.auto_sync_timer.start()

    def _screenshot_timer_tick(self):
        try:
            if not self.config.get("screenshots", "enabled", True): return
            if self.config.get("screenshots", "on_events_only", False): return
            if not self.has_target(): return
            if not self.schedule_checker.is_monitoring_time(): return
            if not self.monitoring_active: return
            self.take_screenshot("Scheduled", "Timer")
        except Exception as e:
            logging.error(f"Periodic screenshot error: {e}")

    # -------------- Hotkeys --------------
    def setup_hotkeys(self):
        self.hotkey = self.config.get("settings", "hotkey", "<ctrl>+<shift>+m")

        def status_cb(ok, method):
            if ok:
                self.current_hotkey_label.setText(f"{self.hotkey} ({method})")
                self.current_hotkey_label.setStyleSheet("color: #00ff00;")
            else:
                self.current_hotkey_label.setText(f"{self.hotkey} (not registered)")
                self.current_hotkey_label.setStyleSheet("color: #ff0000;")

        if getattr(self, "hotkey_manager", None):
            self.hotkey_manager.unregister()

        try:
            self.hotkey_manager = HotkeyManager(self.hotkey, self.set_active_window, status_cb)
            if not self.hotkey_manager._registered:
                QMessageBox.warning(self, "Hotkey", "Failed to register hotkey. Please check dependencies (pynput or keyboard).")
        except Exception as e:
            logging.error(f"Failed to initialize hotkey manager: {e}")
            status_cb(False, "error")

    # --------- Apply saved settings ----------
    def apply_saved_settings(self):
        logging.debug("Applying saved settings...")
        # Main
        mode = self.config.get("settings", "mode", "title")
        if mode not in ["title", "class", "pid"]:
            logging.warning(f"Invalid mode {mode}, defaulting to 'title'")
            mode = "title"
        self.mode_combo.setCurrentText(mode)
        hotkey = self.config.get("settings", "hotkey", "<ctrl>+<shift>+m")
        self.hotkey_edit.setText(hotkey)
        always_on_top = self.config.get("settings", "always_on_top", False)
        if not isinstance(always_on_top, bool):
            logging.warning(f"Invalid always_on_top {always_on_top}, defaulting to False")
            always_on_top = False
        self.always_on_top_cb.setChecked(always_on_top)

        # Schedule
        sc = self.config.data.get("scheduling", {})
        schedule_enabled = sc.get("enabled", False)
        if not isinstance(schedule_enabled, bool):
            logging.warning(f"Invalid schedule.enabled {schedule_enabled}, defaulting to False")
            schedule_enabled = False
        self.schedule_enabled_cb.setChecked(schedule_enabled)
        active_days = sc.get("active_days", [1, 2, 3, 4, 5])
        if not isinstance(active_days, list):
            logging.warning(f"Invalid active_days {active_days}, defaulting to [1,2,3,4,5]")
            active_days = [1, 2, 3, 4, 5]
        for day, cb in self.day_checkboxes.items():
            cb.setChecked(day in active_days)
        start_time = sc.get("start_time", "09:00:00")
        end_time = sc.get("end_time", "17:00:00")
        try:
            self.start_time_edit.setTime(QTime.fromString(start_time, "HH:mm:ss"))
            self.end_time_edit.setTime(QTime.fromString(end_time, "HH:mm:ss"))
        except Exception as e:
            logging.warning(f"Invalid time format in config: start_time={start_time}, end_time={end_time}, error={e}")
            self.start_time_edit.setTime(QTime(9, 0))
            self.end_time_edit.setTime(QTime(17, 0))

        if schedule_enabled:
            QTimer.singleShot(500, self.check_schedule_status)
        else:
            self.schedule_status_label.setText("Schedule: Always Active")
            self.schedule_status_label.setStyleSheet("color: #00ff00;")

        # Screenshots
        scc = self.config.data.get("screenshots", {})
        screenshots_enabled = scc.get("enabled", True)
        if not isinstance(screenshots_enabled, bool):
            logging.warning(f"Invalid screenshots.enabled {screenshots_enabled}, defaulting to True")
            screenshots_enabled = True
        self.screenshots_enabled_cb.setChecked(screenshots_enabled)
        interval = scc.get("interval_seconds", 300)
        if not isinstance(interval, (int, float)) or interval < 1:
            logging.warning(f"Invalid screenshot interval {interval}, defaulting to 300")
            interval = 300
        self.screenshot_interval_spin.setValue(interval)
        events_only = scc.get("on_events_only", False)
        if not isinstance(events_only, bool):
            logging.warning(f"Invalid screenshots.on_events_only {events_only}, defaulting to False")
            events_only = False
        self.events_only_cb.setChecked(events_only)
        quality = scc.get("quality", 60)
        if not isinstance(quality, int) or quality < 10 or quality > 100:
            logging.warning(f"Invalid screenshot quality {quality}, defaulting to 60")
            quality = 60
        self.quality_slider.setValue(quality)
        self.quality_label.setText(f"{quality}%")
        max_files = scc.get("max_files", 500)
        if not isinstance(max_files, int) or max_files < 10:
            logging.warning(f"Invalid max_files {max_files}, defaulting to 500")
            max_files = 500
        self.max_files_spin.setValue(max_files)
        cleanup_days = scc.get("auto_cleanup_days", 15)
        if not isinstance(cleanup_days, int) or cleanup_days < 1:
            logging.warning(f"Invalid cleanup_days {cleanup_days}, defaulting to 15")
            cleanup_days = 15
        self.cleanup_days_spin.setValue(cleanup_days)

        # Database tab
        my = self.config.data.get("mysql", {})
        db_enabled = my.get("enabled", True)
        if not isinstance(db_enabled, bool):
            logging.warning(f"Invalid mysql.enabled {db_enabled}, defaulting to True")
            db_enabled = True
        self.db_enabled_cb.setChecked(db_enabled)
        self.db_host_edit.setText(my.get("host", "127.0.0.1"))
        port = my.get("port", 3306)
        if not isinstance(port, int) or port < 1 or port > 65535:
            logging.warning(f"Invalid port {port}, defaulting to 3306")
            port = 3306
        self.db_port_spin.setValue(port)
        self.db_user_edit.setText(my.get("user", "monitor_user"))
        self.db_password_edit.setText(my.get("password", ""))
        self.db_database_edit.setText(my.get("database", "monitor_db"))
        self.db_table_edit.setText(my.get("table", "events"))

        # Auto-sync GUI
        auto_sync = self.config.get("settings", "auto_sync_minutes", 5)
        if not isinstance(auto_sync, int) or auto_sync < 1:
            logging.warning(f"Invalid auto_sync_minutes {auto_sync}, defaulting to 5")
            auto_sync = 5
        self.auto_sync_spin.setValue(auto_sync)

        # Settings tab
        st = self.config.data.get("settings", {})
        shutdown_timer = st.get("shutdown_timer_minutes", 30)
        if not isinstance(shutdown_timer, int) or shutdown_timer < 1:
            logging.warning(f"Invalid shutdown_timer_minutes {shutdown_timer}, defaulting to 30")
            shutdown_timer = 30
        self.shutdown_timer_spin.setValue(shutdown_timer)
        auto_resume = st.get("auto_resume_minutes", 30)
        if not isinstance(auto_resume, int) or auto_resume < 0:
            logging.warning(f"Invalid auto_resume_minutes {auto_resume}, defaulting to 30")
            auto_resume = 30
        self.auto_resume_spin.setValue(auto_resume)
        poll_interval = st.get("poll_interval", 2)
        if not isinstance(poll_interval, (int, float)) or poll_interval < 1:
            logging.warning(f"Invalid poll_interval {poll_interval}, defaulting to 2")
            poll_interval = 2
        self.poll_interval_spin.setValue(int(poll_interval))
        debug_mode = st.get("debug_mode", True)
        if not isinstance(debug_mode, bool):
            logging.warning(f"Invalid debug_mode {debug_mode}, defaulting to True")
            debug_mode = True
        self.debug_mode_cb.setChecked(debug_mode)

        # UI
        ui = self.config.data.get("ui", {})
        theme = ui.get("theme", "dark").capitalize()
        if theme not in ["Dark", "Light"]:
            logging.warning(f"Invalid theme {theme}, defaulting to Dark")
            theme = "Dark"
        self.theme_combo.setCurrentText(theme)
        font_size = ui.get("font_size", 10)
        if not isinstance(font_size, int) or font_size < 8 or font_size > 16:
            logging.warning(f"Invalid font_size {font_size}, defaulting to 10")
            font_size = 10
        self.font_size_spin.setValue(font_size)
        self.change_theme(theme)
        self.change_font_size(font_size)
        size = ui.get("window_size", [900, 700])
        if not isinstance(size, list) or len(size) != 2 or not all(isinstance(x, int) for x in size):
            logging.warning(f"Invalid window_size {size}, defaulting to [900, 700]")
            size = [900, 700]
        pos = ui.get("window_position", [100, 100])
        if not isinstance(pos, list) or len(pos) != 2 or not all(isinstance(x, int) for x in pos):
            logging.warning(f"Invalid window_position {pos}, defaulting to [100, 100]")
            pos = [100, 100]
        self.resize(size[0], size[1])
        self.move(pos[0], pos[1])
        logging.debug("Settings applied successfully")

    def check_config_discrepancies(self):
        """Check if widget values match config to detect resets"""
        logging.debug("Checking for config discrepancies...")
        if self.mode_combo.currentText() != self.config.get("settings", "mode", "title"):
            logging.warning(f"Mode mismatch: GUI={self.mode_combo.currentText()}, Config={self.config.get('settings', 'mode')}")
        if self.hotkey_edit.text() != self.config.get("settings", "hotkey", "<ctrl>+<shift>+m"):
            logging.warning(f"Hotkey mismatch: GUI={self.hotkey_edit.text()}, Config={self.config.get('settings', 'hotkey')}")
        if self.always_on_top_cb.isChecked() != self.config.get("settings", "always_on_top", False):
            logging.warning(f"Always on top mismatch: GUI={self.always_on_top_cb.isChecked()}, Config={self.config.get('settings', 'always_on_top')}")
        if self.shutdown_timer_spin.value() != self.config.get("settings", "shutdown_timer_minutes", 30):
            logging.warning(f"Shutdown timer mismatch: GUI={self.shutdown_timer_spin.value()}, Config={self.config.get('settings', 'shutdown_timer_minutes')}")
        if self.auto_resume_spin.value() != self.config.get("settings", "auto_resume_minutes", 30):
            logging.warning(f"Auto resume mismatch: GUI={self.auto_resume_spin.value()}, Config={self.config.get('settings', 'auto_resume_minutes')}")
        if self.poll_interval_spin.value() != int(self.config.get("settings", "poll_interval", 2)):
            logging.warning(f"Poll interval mismatch: GUI={self.poll_interval_spin.value()}, Config={self.config.get('settings', 'poll_interval')}")
        if self.debug_mode_cb.isChecked() != self.config.get("settings", "debug_mode", True):
            logging.warning(f"Debug mode mismatch: GUI={self.debug_mode_cb.isChecked()}, Config={self.config.get('settings', 'debug_mode')}")
        if self.theme_combo.currentText().lower() != self.config.get("ui", "theme", "dark"):
            logging.warning(f"Theme mismatch: GUI={self.theme_combo.currentText()}, Config={self.config.get('ui', 'theme')}")
        if self.font_size_spin.value() != self.config.get("ui", "font_size", 10):
            logging.warning(f"Font size mismatch: GUI={self.font_size_spin.value()}, Config={self.config.get('ui', 'font_size')}")
        # Add similar checks for other tabs if needed
        logging.debug("Discrepancy check complete")

    def check_schedule_status(self):
        try:
            in_schedule = self.schedule_checker.is_monitoring_time()
            self.handle_schedule_change(in_schedule)
        except Exception as e:
            logging.error(f"check_schedule_status error: {e}")

    # -------------- Monitoring actions --------------
    def set_active_window(self):
        try:
            hwnd = win32gui.GetForegroundWindow()
            if not hwnd:
                QMessageBox.warning(self, "Selection", "Could not detect active window.")
                return

            mode = self.mode_combo.currentText()
            if mode == "title":
                title = win32gui.GetWindowText(hwnd) or ""
                if not title:
                    QMessageBox.warning(self, "Title", "Active window has no title.")
                    return
                self.target_value = title
                self.target_pid = None
            elif mode == "class":
                try:
                    cls = win32gui.GetClassName(hwnd) or ""
                except Exception:
                    cls = ""
                if not cls:
                    QMessageBox.warning(self, "Class", "Could not get class name for active window.")
                    return
                self.target_value = cls
                self.target_pid = None
            elif mode == "pid":
                try:
                    _, pid = win32process.GetWindowThreadProcessId(hwnd)
                    pid = int(pid)
                    if not psutil.pid_exists(pid):
                        QMessageBox.critical(self, "PID", f"Process {pid} not found.")
                        return
                    self.target_pid = pid
                    self.target_value = None
                except Exception as ex:
                    QMessageBox.critical(self, "PID", f"Could not get PID: {ex}")
                    return

            self.update_target_display()
            self.pause_btn.setEnabled(True)
            self.update_status("Monitoring", "green")

            self.take_screenshot("Monitoring", "Started")
            self.db.write_event("Monitoring_Started", window_title=self.get_identifier_repr(), extra={"mode": mode, "target": self.get_identifier_repr()})
        except Exception as e:
            QMessageBox.critical(self, "Error", str(e))

    def has_target(self):
        return (self.target_value is not None) or (self.target_pid is not None)

    def get_identifier_repr(self):
        if self.target_pid:
            return f"PID {self.target_pid}"
        return self.target_value or "(not set)"

    def check_window_exists(self):
        mode = self.mode_combo.currentText()
        found = []

        def enum_callback(hwnd, extra):
            try:
                if not win32gui.IsWindowVisible(hwnd):
                    return

                if mode == "title" and self.target_value:
                    try:
                        text = win32gui.GetWindowText(hwnd) or ""
                        if text.lower() == self.target_value.lower():
                            found.append(hwnd)
                    except Exception as e:
                        logging.debug(f"Error reading window text: {e}")

                elif mode == "class" and self.target_value:
                    try:
                        cls = win32gui.GetClassName(hwnd) or ""
                        if cls.lower() == self.target_value.lower():
                            found.append(hwnd)
                    except Exception as e:
                        logging.debug(f"Error reading class name: {e}")

                elif mode == "pid" and self.target_pid:
                    try:
                        _, pid = win32process.GetWindowThreadProcessId(hwnd)
                        if int(pid) == int(self.target_pid) and psutil.pid_exists(int(pid)):
                            found.append(hwnd)
                    except Exception as e:
                        logging.debug(f"Error checking pid for hwnd {hwnd}: {e}")
            except Exception as e:
                logging.debug(f"Enum callback exception: {e}")

        try:
            win32gui.EnumWindows(enum_callback, None)
        except Exception as e:
            logging.debug(f"EnumWindows failed: {e}")
            if mode == "pid" and self.target_pid:
                return psutil.pid_exists(self.target_pid)
            return False

        return len(found) > 0

    # -------------- Screenshots (MSS, multi-monitor, date subfolders) --------------
    def take_screenshot(self, prefix, suffix):
        try:
            sc = self.config.data["screenshots"]
            if not sc.get("enabled", True): return
            if mss is None or Image is None:
                logging.error("mss/PIL not available - cannot take screenshots")
                return

            date_folder = datetime.now().strftime("%Y-%m-%d")
            folder_path = os.path.join(SCREENSHOT_DIR, date_folder)
            os.makedirs(folder_path, exist_ok=True)

            timestamp = now_str_file()
            quality = int(sc.get("quality", 85))
            fmt = sc.get("format", "auto")

            if fmt == "auto":
                if quality < 100:
                    img_format = "JPEG"
                    ext = "jpg"
                else:
                    img_format = "PNG"
                    ext = "png"
            else:
                img_format = fmt.upper()
                ext = img_format.lower()

            filename = f"{prefix}_{timestamp}_{suffix}.{ext}"
            filepath = os.path.join(folder_path, filename)

            with mss.mss() as sct:
                monitors = sct.monitors[1:]  # skip virtual full area (index 0)
                if not monitors:
                    logging.warning("MSS: No monitors found for screenshot.")
                    return

                images = []
                for m in monitors:
                    shot = sct.grab(m)
                    img = Image.frombytes("RGB", (shot.width, shot.height), shot.rgb)
                    images.append(img)

                total_width = sum(img.width for img in images)
                max_height = max(img.height for img in images)
                combined = Image.new("RGB", (total_width, max_height))
                x_offset = 0
                for img in images:
                    combined.paste(img, (x_offset, 0))
                    x_offset += img.width

                if img_format == "JPEG":
                    combined.save(filepath, img_format, quality=quality, optimize=True)
                else:
                    combined.save(filepath, img_format, optimize=True)

            self.db.write_event("Screenshot_Taken", window_title=self.get_identifier_repr(), extra={"filepath": filepath})
            self.cleanup_old_screenshots()
            try:
                self.statusBar().showMessage(f"Screenshot saved: {filepath}", 3000)
            except Exception:
                pass
        except Exception as e:
            logging.error(f"Failed to take screenshot: {e}")

    def cleanup_old_screenshots(self):
        try:
            max_files = self.config.get("screenshots", "max_files", 1000)
            cleanup_days = self.config.get("screenshots", "auto_cleanup_days", 30)

            files = []
            for root, _, filenames in os.walk(SCREENSHOT_DIR):
                for filename in filenames:
                    if filename.lower().endswith(('.png', '.jpg', '.jpeg')):
                        filepath = os.path.join(root, filename)
                        try:
                            ctime = os.path.getctime(filepath)
                        except Exception:
                            ctime = time.time()
                        files.append((filepath, ctime))

            files.sort(key=lambda x: x[1])

            while len(files) > max_files:
                try:
                    os.remove(files[0][0])
                except Exception:
                    pass
                files.pop(0)

            cutoff_time = time.time() - (cleanup_days * 24 * 3600)
            for filepath, ctime in files:
                if ctime < cutoff_time:
                    try:
                        os.remove(filepath)
                    except Exception:
                        pass
        except Exception as e:
            logging.error(f"Failed to cleanup screenshots: {e}")

    # -------- Status/DB/UI Helpers --------
    @pyqtSlot(str, str)
    def update_status(self, message, color):
        self.status_label.setText(f"Status: {message}")
        color_map = {"green": "#00ff00", "red": "#ff0000", "orange": "#ffa500", "blue": "#0078d4"}
        self.status_label.setStyleSheet(f"color: {color_map.get(color, '#ffffff')};")

    @pyqtSlot(str, str)
    def update_db_status(self, message, color):
        self.db_connection_status.setText(message.replace("MySQL: ", ""))
        color_map = {"green": "#00ff00", "red": "#ff0000", "blue": "#0078d4"}
        self.db_connection_status.setStyleSheet(f"color: {color_map.get(color, '#ffffff')};")

    def update_target_display(self):
        self.target_label.setText(f"Monitoring: {self.get_identifier_repr()}")

    def update_hotkey_display(self):
        self.current_hotkey_label.setText(self.hotkey)

    def update_countdown_display(self):
        if not self.shutdown_scheduled or self.remaining_seconds <= 0:
            self.countdown_label.setText("Countdown: --:--:--")
            return
        hours, remainder = divmod(self.remaining_seconds, 3600)
        minutes, seconds = divmod(remainder, 60)
        self.countdown_label.setText(f"Countdown: {hours:02d}:{minutes:02d}:{seconds:02d}")

    def update_queue_status(self):
        queue_size = len(self.db.queue) if hasattr(self.db, 'queue') else 0
        self.queue_status.setText(f"{queue_size} events")

    # --------- Settings Handlers ----------
    def toggle_monitoring(self):
        self.monitoring_active = not self.monitoring_active
        if self.monitoring_active:
            self.pause_btn.setText("Pause Monitoring")
            self.update_status("Monitoring", "green")
            self.db.write_event("Monitoring_Resumed", window_title=self.get_identifier_repr())
        else:
            self.pause_btn.setText("Resume Monitoring")
            self.update_status("Paused", "red")
            self.db.write_event("Monitoring_Paused", window_title=self.get_identifier_repr())
            auto_minutes = self.auto_resume_spin.value()
            if auto_minutes > 0:
                QTimer.singleShot(auto_minutes * 60 * 1000, self.auto_resume_monitoring)

    def auto_resume_monitoring(self):
        if not self.monitoring_active:
            self.monitoring_active = True
            self.pause_btn.setText("Pause Monitoring")
            self.update_status("Monitoring (Auto-Resumed)", "green")
            self.db.write_event("Monitoring_Resumed_Auto", window_title=self.get_identifier_repr())

    def toggle_always_on_top(self, checked):
        self.config.set("settings", "always_on_top", checked)
        self.config.save()
        if checked:
            self.setWindowFlags(self.windowFlags() | Qt.WindowType.WindowStaysOnTopHint)
        else:
            self.setWindowFlags(self.windowFlags() & ~Qt.WindowType.WindowStaysOnTopHint)
        self.show()

    def mode_changed(self, mode):
        self.mode = mode
        self.config.set("settings", "mode", mode)
        self.config.save()

    def apply_hotkey(self):
        new_hotkey = self.hotkey_edit.text().strip()
        if not new_hotkey:
            QMessageBox.warning(self, "Hotkey", "Please enter a valid hotkey")
            return
        self.config.set("settings", "hotkey", new_hotkey)
        self.hotkey = new_hotkey
        self.setup_hotkeys()
        self.config.save()
        QMessageBox.information(self, "Hotkey", "Hotkey updated (attempted registration).")

    def shutdown_timer_changed(self, value):
        logging.debug(f"Shutdown timer changed to {value}")
        self.config.set("settings", "shutdown_timer_minutes", value)
        self.config.save()

    def auto_resume_changed(self, value):
        logging.debug(f"Auto resume changed to {value}")
        self.config.set("settings", "auto_resume_minutes", value)
        self.config.save()

    def poll_interval_changed(self, value):
        logging.debug(f"Poll interval changed to {value}")
        self.config.set("settings", "poll_interval", value)
        self.config.save()

    def toggle_debug_mode(self, checked):
        logging.debug(f"Debug mode changed to {checked}")
        self.config.set("settings", "debug_mode", checked)
        logger.setLevel(logging.DEBUG if checked else logging.INFO)
        self.config.save()

    # ---- Schedule handlers ----
    def schedule_enabled_changed(self, enabled):
        logging.debug(f"Schedule enabled changed to {enabled}")
        self.config.set("scheduling", "enabled", enabled)
        self.config.save()
        if enabled:
            self.schedule_status_label.setText("Schedule: Active")
        else:
            self.schedule_status_label.setText("Schedule: Always Active")

    def days_changed(self):
        active_days = [day for day, cb in self.day_checkboxes.items() if cb.isChecked()]
        logging.debug(f"Active days changed to {active_days}")
        self.config.set("scheduling", "active_days", active_days)
        self.config.save()

    def time_changed(self):
        start_time = self.start_time_edit.time().toString("HH:mm:ss")
        end_time = self.end_time_edit.time().toString("HH:mm:ss")
        logging.debug(f"Schedule times changed: start={start_time}, end={end_time}")
        self.config.set("scheduling", "start_time", start_time)
        self.config.set("scheduling", "end_time", end_time)
        self.config.save()

    # ---- Screenshot settings ----
    def screenshots_enabled_changed(self, enabled):
        logging.debug(f"Screenshots enabled changed to {enabled}")
        self.config.set("screenshots", "enabled", enabled)
        self.config.save()
        if enabled:
            if not self.screenshot_timer.isActive() and not self.config.get("screenshots", "on_events_only", False):
                self.screenshot_timer.start()
        else:
            if self.screenshot_timer.isActive():
                self.screenshot_timer.stop()

    def screenshot_settings_changed(self):
        interval = self.screenshot_interval_spin.value()
        events_only = self.events_only_cb.isChecked()
        max_files = self.max_files_spin.value()
        cleanup_days = self.cleanup_days_spin.value()
        logging.debug(f"Screenshot settings changed: interval={interval}, events_only={events_only}, max_files={max_files}, cleanup_days={cleanup_days}")
        self.config.set("screenshots", "interval_seconds", interval)
        self.config.set("screenshots", "on_events_only", events_only)
        self.config.set("screenshots", "max_files", max_files)
        self.config.set("screenshots", "auto_cleanup_days", cleanup_days)
        self.config.save()

        interval_ms = int(interval * 1000)
        self.screenshot_timer.setInterval(interval_ms)
        if not self.config.get("screenshots", "on_events_only", False) and self.config.get("screenshots", "enabled", True):
            if not self.screenshot_timer.isActive():
                self.screenshot_timer.start()
        else:
            if self.screenshot_timer.isActive():
                self.screenshot_timer.stop()

    def quality_changed(self, value):
        self.quality_label.setText(f"{value}%")
        logging.debug(f"Screenshot quality changed to {value}")
        self.config.set("screenshots", "quality", value)
        self.config.save()

    def open_screenshot_folder(self):
        try:
            path = os.path.abspath(SCREENSHOT_DIR)
            if sys.platform == "win32":
                os.startfile(path)
            elif sys.platform == "darwin":
                import subprocess; subprocess.Popen(["open", path])
            else:
                import subprocess; subprocess.Popen(["xdg-open", path])
        except Exception as e:
            QMessageBox.warning(self, "Error", f"Could not open folder: {e}")

    # ---- DB operations ----
    def test_db_connection(self):
        try:
            if mysql is None:
                QMessageBox.critical(self, "Database", "MySQL connector not installed")
                return
            conn = mysql.connect(
                host=self.db_host_edit.text(),
                port=self.db_port_spin.value(),
                user=self.db_user_edit.text(),
                password=self.db_password_edit.text(),
                database=self.db_database_edit.text()
            )
            conn.close()
            QMessageBox.information(self, "Database", "Connection successful!")
        except Exception as e:
            QMessageBox.critical(self, "Database", f"Connection failed: {e}")

    def apply_db_settings(self):
        mysql_config = {
            "enabled": self.db_enabled_cb.isChecked(),
            "host": self.db_host_edit.text(),
            "port": self.db_port_spin.value(),
            "user": self.db_user_edit.text(),
            "password": self.db_password_edit.text(),
            "database": self.db_database_edit.text(),
            "table": self.db_table_edit.text()
        }
        logging.debug(f"Applying DB settings: {mysql_config}")
        for key, value in mysql_config.items():
            self.config.set("mysql", key, value)
        self.config.save()

        try:
            self.db._try_connect()
        except Exception as e:
            logging.error(f"Reconnection after apply failed: {e}")

        QMessageBox.information(self, "Database", "Settings applied. Attempted reconnect.")

    def toggle_db_enabled(self, enabled):
        logging.debug(f"DB enabled changed to {enabled}")
        self.config.set("mysql", "enabled", enabled)
        self.config.save()
        if enabled:
            logging.info("Database connection enabled, attempting reconnect...")
            try:
                self.db._try_connect()
            except Exception as e:
                logging.error(f"Failed to reconnect DB: {e}")
        else:
            logging.info("Database connection disabled, closing connection...")
            try:
                if self.db.conn:
                    self.db.conn.close()
            except Exception:
                pass
            self.db.conn = None
            self.db_connection_status.setText("Disabled")
            self.db_connection_status.setStyleSheet("color: #aaaaaa;")

    def auto_sync_interval_changed(self, value):
        logging.debug(f"Auto sync interval changed to {value}")
        self.config.set("settings", "auto_sync_minutes", value)
        self.auto_sync_timer.setInterval(value * 60 * 1000)
        self.config.save()
        logging.info(f"Auto-sync interval updated to {value} minutes")

    def manual_sync_to_db(self):
        try:
            self.db.flush_queue()
            self.update_queue_status()
            QMessageBox.information(self, "Database", "Manual sync completed successfully.")
        except Exception as e:
            QMessageBox.critical(self, "Database", f"Manual sync failed: {e}")

    def auto_sync_to_db(self):
        try:
            self.db.flush_queue()
            self.update_queue_status()
            logging.info("Auto-sync to DB completed successfully.")
        except Exception as e:
            logging.error(f"Auto-sync failed: {e}")

    # ---- Schedule/Window events ----
    @pyqtSlot(bool)
    def handle_window_state_change(self, exists):
        if exists:
            self.db.write_event("Monitoring_Reappeared", window_title=self.get_identifier_repr())
            self.take_screenshot("Monitoring", "Reappeared")
            if self.shutdown_scheduled:
                self.cancel_shutdown()
            self.update_status("Window detected", "green")
        else:
            self.db.write_event("Monitoring_Disappeared", window_title=self.get_identifier_repr())
            self.take_screenshot("Monitoring", "Disappeared")
            if self.monitoring_active and not self.shutdown_scheduled and self.schedule_checker.is_monitoring_time():
                self.schedule_shutdown()
            self.update_status("Window disappeared", "orange")

    @pyqtSlot(bool)
    def handle_schedule_change(self, in_schedule):
        if in_schedule:
            self.schedule_status_label.setText("Schedule: Active")
            self.schedule_status_label.setStyleSheet("color: #00ff00;")
        else:
            self.schedule_status_label.setText("Schedule: Inactive")
            self.schedule_status_label.setStyleSheet("color: #ff0000;")

    # ---- Shutdown management ----
    def schedule_shutdown(self):
        if self.shutdown_scheduled:
            return
        minutes = self.shutdown_timer_spin.value()
        self.remaining_seconds = minutes * 60
        self.shutdown_scheduled = True

        self.update_status(f"Window closed — shutdown in {minutes} min", "orange")
        self.shutdown_timer = QTimer()
        self.shutdown_timer.timeout.connect(self.countdown_tick)
        self.shutdown_timer.start(1000)

        self.db.write_event("Shutdown_Scheduled", window_title=self.get_identifier_repr())

    def cancel_shutdown(self):
        if not self.shutdown_scheduled:
            return
        self.shutdown_scheduled = False
        self.remaining_seconds = 0

        if self.shutdown_timer:
            self.shutdown_timer.stop()
            self.shutdown_timer = None

        try:
            if sys.platform.startswith("win"):
                os.system("shutdown /a")
        except Exception:
            pass

        self.update_status("Shutdown cancelled", "green")
        self.db.write_event("Shutdown_Cancelled", window_title=self.get_identifier_repr())

    def countdown_tick(self):
        if not self.shutdown_scheduled:
            return
        if self.monitoring_active and self.schedule_checker.is_monitoring_time():
            self.remaining_seconds -= 1
            if self.remaining_seconds == 60:
                self.take_screenshot("Monitoring", "OneMinuteBeforeShutdown")
            if self.remaining_seconds <= 0:
                self.execute_shutdown()

    def execute_shutdown(self):
        self.take_screenshot("Monitoring", "BeforeShutdown")
        self.db.write_event("Shutdown_Executed", window_title=self.get_identifier_repr())
        try:
            if sys.platform.startswith("win"):
                os.system("shutdown /s /t 5")
            elif sys.platform.startswith("linux"):
                os.system("shutdown -h now")
            elif sys.platform.startswith("darwin"):
                osascript = 'osascript -e \'tell app "System Events" to shut down\''
                os.system(osascript)
        except Exception as e:
            logging.error(f"Failed to execute shutdown: {e}")

        self.shutdown_scheduled = False
        if self.shutdown_timer:
            self.shutdown_timer.stop()

    # ---- UI changes ----
    def change_theme(self, theme):
        logging.debug(f"Changing theme to {theme}")
        self.config.set("ui", "theme", theme.lower())
        self.apply_theme(theme)
        self.config.save()

    def change_font_size(self, size):
        logging.debug(f"Changing font size to {size}")
        self.config.set("ui", "font_size", size)
        app = QApplication.instance()
        app.setFont(QFont("Segoe UI", size))
        self.config.save()

    # ---- Window close/save ----
    def save_configuration(self):
        if self.config.save():
            QMessageBox.information(self, "Configuration", "Configuration saved successfully")
        else:
            QMessageBox.critical(self, "Configuration", "Failed to save configuration")
        self.check_config_discrepancies()

    def load_configuration(self):
        self.config.load()
        self.apply_saved_settings()
        QMessageBox.information(self, "Configuration", "Configuration reloaded")
        self.check_config_discrepancies()

    def export_configuration(self):
        filename, _ = QFileDialog.getSaveFileName(
            self, "Export Configuration", "monitor_config.json", "JSON Files (*.json)"
        )
        if filename:
            try:
                with open(filename, 'w', encoding='utf-8') as f:
                    json.dump(self.config.data, f, indent=2, ensure_ascii=False)
                QMessageBox.information(self, "Export", "Configuration exported successfully")
            except Exception as e:
                QMessageBox.critical(self, "Export", f"Failed to export configuration: {e}")

    def closeEvent(self, event):
        self.config.set("ui", "window_size", [self.width(), self.height()])
        self.config.set("ui", "window_position", [self.x(), self.y()])
        self.config.save()

        if hasattr(self, 'monitor_thread'):
            try:
                self.monitor_thread.stop()
            except Exception:
                pass

        try:
            if hasattr(self, 'screenshot_timer') and self.screenshot_timer.isActive():
                self.screenshot_timer.stop()
            if hasattr(self, 'countdown_timer') and self.countdown_timer.isActive():
                self.countdown_timer.stop()
            if hasattr(self, 'queue_timer') and self.queue_timer.isActive():
                self.queue_timer.stop()
            if hasattr(self, 'auto_sync_timer') and self.auto_sync_timer.isActive():
                self.auto_sync_timer.stop()
        except Exception:
            pass

        try:
            if self.shutdown_scheduled:
                self.cancel_shutdown()
        except Exception:
            pass

        try:
            if self.hotkey_manager:
                self.hotkey_manager.unregister()
        except Exception:
            pass

        event.accept()

def main():
    app = QApplication(sys.argv)
    window = WindowMonitorApp()
    window.show()
    sys.exit(app.exec())

if __name__ == "__main__":
    main()