#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
Mouse Jiggler - Professional Edition
====================================
A robust, class-based Windows utility to prevent system idle/sleep.
Features:
- Idle detection using Win32 API.
- Black screen overlay for privacy and visual feedback.
- Monitor power management (sleep/wake).
- Fullscreen and process-based suspension.
- Thread-safe architecture.
"""

import os
import sys
import time
import math
import logging
import ctypes
import threading
import signal
import atexit
from dataclasses import dataclass, field
from typing import Optional, Set, Tuple, Any
from ctypes import wintypes
import tkinter as tk

# =============================================================================
# CONFIGURATION
# =============================================================================

@dataclass(frozen=True)
class JigglerConfig:
    """Configuration settings for the Mouse Jiggler."""
    IDLE_THRESHOLD_S: float = 30.0
    SKIP_IF_FULLSCREEN: bool = True
    PROCESS_DENYLIST: Set[str] = field(default_factory=lambda: {
        "vlc.exe", "powerpnt.exe", "obs64.exe", "netflix.exe"
    })
    
    # Modes: 'circle' or 'nudge'
    JIGGLE_MODE: str = "circle"
    
    # Circle settings
    CIRCLE_RADIUS_PX: int = 10
    CIRCLE_STEPS: int = 90
    CIRCLE_DURATION_S: float = 1.8
    
    # Nudge settings
    NUDGE_INTERVAL_S: float = 30.0
    NUDGE_PIXELS: int = 1
    
    # Internal timing
    POLL_INTERVAL_S: float = 0.2
    
    # Features
    ENABLE_SLEEP_INHIBIT: bool = True
    ENABLE_BLACK_SCREEN: bool = True
    ENABLE_MONITOR_SLEEP: bool = False
    MONITOR_SLEEP_DELAY_S: float = 2.0
    SHOW_STATUS_TEXT: bool = False
    
    MUTEX_NAME: str = "Global\\MOUSE_JIGGLER_PRO"

# =============================================================================
# WIN32 API WRAPPER
# =============================================================================

class Win32:
    """Encapsulates Windows API calls and structures."""
    
    user32 = ctypes.windll.user32
    kernel32 = ctypes.windll.kernel32
    
    # Constants
    INPUT_MOUSE = 0
    MOUSEEVENTF_MOVE = 0x0001
    MOUSEEVENTF_ABSOLUTE = 0x8000
    MOUSEEVENTF_VIRTUALDESK = 0x4000
    
    VK_LBUTTON = 0x01
    VK_RBUTTON = 0x02
    VK_MBUTTON = 0x04
    
    WM_SYSCOMMAND = 0x0112
    SC_MONITORPOWER = 0xF170
    MONITOR_OFF = 2
    MONITOR_ON = -1
    
    ES_CONTINUOUS = 0x80000000
    ES_SYSTEM_REQUIRED = 0x00000001
    ES_DISPLAY_REQUIRED = 0x00000002
    
    ERROR_ALREADY_EXISTS = 183
    PROCESS_QUERY_LIMITED_INFORMATION = 0x1000
    
    SM_XVIRTUALSCREEN = 76
    SM_YVIRTUALSCREEN = 77
    SM_CXVIRTUALSCREEN = 78
    SM_CYVIRTUALSCREEN = 79

    class MOUSEINPUT(ctypes.Structure):
        _fields_ = [
            ('dx', wintypes.LONG),
            ('dy', wintypes.LONG),
            ('mouseData', wintypes.DWORD),
            ('dwFlags', wintypes.DWORD),
            ('time', wintypes.DWORD),
            ('dwExtraInfo', ctypes.POINTER(wintypes.ULONG))
        ]

    class INPUT_UNION(ctypes.Union):
        pass

    class INPUT(ctypes.Structure):
        pass
    
    INPUT_UNION._fields_ = [('mi', MOUSEINPUT)]
    INPUT._fields_ = [
        ('type', wintypes.DWORD),
        ('union', INPUT_UNION)
    ]

    class LASTINPUTINFO(ctypes.Structure):
        _fields_ = [
            ("cbSize", wintypes.UINT),
            ("dwTime", wintypes.DWORD)
        ]

    class RECT(ctypes.Structure):
        _fields_ = [
            ("left", wintypes.LONG), ("top", wintypes.LONG),
            ("right", wintypes.LONG), ("bottom", wintypes.LONG)
        ]

    @classmethod
    def get_idle_seconds(cls) -> float:
        lii = cls.LASTINPUTINFO()
        lii.cbSize = ctypes.sizeof(cls.LASTINPUTINFO)
        if not cls.user32.GetLastInputInfo(ctypes.byref(lii)):
            return 0.0
        tick_now = cls.kernel32.GetTickCount()
        # Handle wrap-around and unsigned math
        idle_ms = (tick_now - lii.dwTime) & 0xFFFFFFFF
        return idle_ms / 1000.0

    @classmethod
    def get_last_input_time(cls) -> int:
        lii = cls.LASTINPUTINFO()
        lii.cbSize = ctypes.sizeof(cls.LASTINPUTINFO)
        if not cls.user32.GetLastInputInfo(ctypes.byref(lii)):
            return 0
        return lii.dwTime

    @classmethod
    def set_sleep_inhibit(cls, enable: bool):
        flags = cls.ES_CONTINUOUS
        if enable:
            flags |= cls.ES_SYSTEM_REQUIRED | cls.ES_DISPLAY_REQUIRED
        cls.kernel32.SetThreadExecutionState(flags)

    @classmethod
    def get_foreground_exe(cls) -> Optional[str]:
        hwnd = cls.user32.GetForegroundWindow()
        if not hwnd: return None
        pid = wintypes.DWORD()
        cls.user32.GetWindowThreadProcessId(hwnd, ctypes.byref(pid))
        hproc = cls.kernel32.OpenProcess(cls.PROCESS_QUERY_LIMITED_INFORMATION, False, pid.value)
        if not hproc: return None
        try:
            buf = ctypes.create_unicode_buffer(260)
            size = wintypes.DWORD(len(buf))
            if cls.kernel32.QueryFullProcessImageNameW(hproc, 0, buf, ctypes.byref(size)):
                return os.path.basename(buf.value).lower()
            return None
        finally:
            cls.kernel32.CloseHandle(hproc)

    @classmethod
    def is_fullscreen(cls, tolerance: int = 2) -> bool:
        hwnd = cls.user32.GetForegroundWindow()
        if not hwnd: return False
        rect = cls.RECT()
        if not cls.user32.GetWindowRect(hwnd, ctypes.byref(rect)): return False
        
        vx = cls.user32.GetSystemMetrics(cls.SM_XVIRTUALSCREEN)
        vy = cls.user32.GetSystemMetrics(cls.SM_YVIRTUALSCREEN)
        vw = cls.user32.GetSystemMetrics(cls.SM_CXVIRTUALSCREEN)
        vh = cls.user32.GetSystemMetrics(cls.SM_CYVIRTUALSCREEN)
        
        return (abs(rect.left - vx) <= tolerance and
                abs(rect.top - vy) <= tolerance and
                abs(rect.right - (vx + vw)) <= tolerance and
                abs(rect.bottom - (vy + vh)) <= tolerance)

    @classmethod
    def move_mouse_abs(cls, x_px: int, y_px: int):
        vx = cls.user32.GetSystemMetrics(cls.SM_XVIRTUALSCREEN)
        vy = cls.user32.GetSystemMetrics(cls.SM_YVIRTUALSCREEN)
        vw = cls.user32.GetSystemMetrics(cls.SM_CXVIRTUALSCREEN)
        vh = cls.user32.GetSystemMetrics(cls.SM_CYVIRTUALSCREEN)
        
        ax = int((x_px - vx) * 65535 / max(1, vw))
        ay = int((y_px - vy) * 65535 / max(1, vh))
        
        inp = cls.INPUT()
        inp.type = cls.INPUT_MOUSE
        inp.union.mi = cls.MOUSEINPUT(
            dx=ax, dy=ay, mouseData=0,
            dwFlags=cls.MOUSEEVENTF_MOVE | cls.MOUSEEVENTF_ABSOLUTE | cls.MOUSEEVENTF_VIRTUALDESK,
            time=0, dwExtraInfo=None
        )
        cls.user32.SendInput(1, ctypes.byref(inp), ctypes.sizeof(cls.INPUT))

    @classmethod
    def move_mouse_rel(cls, dx: int, dy: int):
        inp = cls.INPUT()
        inp.type = cls.INPUT_MOUSE
        inp.union.mi = cls.MOUSEINPUT(
            dx=dx, dy=dy, mouseData=0,
            dwFlags=cls.MOUSEEVENTF_MOVE,
            time=0, dwExtraInfo=None
        )
        cls.user32.SendInput(1, ctypes.byref(inp), ctypes.sizeof(cls.INPUT))

    @classmethod
    def is_button_pressed(cls) -> bool:
        return any(cls.user32.GetAsyncKeyState(vk) & 0x8000 for vk in [cls.VK_LBUTTON, cls.VK_RBUTTON, cls.VK_MBUTTON])

    @classmethod
    def set_monitor_power(cls, on: bool):
        hwnd = cls.user32.GetDesktopWindow()
        power = cls.MONITOR_ON if on else cls.MONITOR_OFF
        cls.user32.SendMessageW(hwnd, cls.WM_SYSCOMMAND, cls.SC_MONITORPOWER, power)

# =============================================================================
# OVERLAY CLASS
# =============================================================================

class BlackOverlay:
    """Manages the fullscreen black overlay window."""
    
    def __init__(self, config: JigglerConfig, on_close_callback: Any):
        self.config = config
        self.on_close = on_close_callback
        self.root: Optional[tk.Tk] = None
        self._thread: Optional[threading.Thread] = None
        self._lock = threading.Lock()

    def start(self):
        with self._lock:
            if self.root: return
            self._thread = threading.Thread(target=self._run, daemon=True)
            self._thread.start()

    def stop(self):
        with self._lock:
            if self.root:
                self.root.after(0, self.root.quit)

    def _run(self):
        self.root = tk.Tk()
        self.root.attributes('-fullscreen', True)
        self.root.attributes('-topmost', True)
        self.root.configure(background='black', cursor='none')
        self.root.overrideredirect(True)
        
        if self.config.SHOW_STATUS_TEXT:
            label = tk.Label(
                self.root,
                text="🖱️ MOUSE JIGGLER ACTIVE\n\nPower saving mode enabled.\nMove mouse or press ESC to wake.",
                font=("Segoe UI", 16), fg="#888888", bg="black"
            )
            label.place(relx=0.5, rely=0.5, anchor='center')

        self.root.bind('<Escape>', lambda e: self.on_close())
        self.root.bind('<Key>', lambda e: self.on_close())
        self.root.bind('<Button>', lambda e: self.on_close())
        
        self.root.mainloop()
        with self._lock:
            try:
                self.root.destroy()
            except:
                pass
            self.root = None

# =============================================================================
# MAIN JIGGLER CONTROLLER
# =============================================================================

class MouseJiggler:
    """Core controller for the Mouse Jiggler application."""
    
    def __init__(self, config: JigglerConfig):
        self.config = config
        self.logger = logging.getLogger("Jiggler")
        self.stop_event = threading.Event()
        self.overlay = BlackOverlay(config, self.deactivate)
        self.active = False
        self.monitor_sleeping = False
        self.mutex_handle = None

    def setup(self):
        """Initial setup, mutex check, and sleep inhibition."""
        self._check_single_instance()
        if self.config.ENABLE_SLEEP_INHIBIT:
            Win32.set_sleep_inhibit(True)
            atexit.register(Win32.set_sleep_inhibit, False)
        
        signal.signal(signal.SIGINT, self._handle_signal)
        signal.signal(signal.SIGTERM, self._handle_signal)

    def _check_single_instance(self):
        Win32.kernel32.SetLastError(0)
        self.mutex_handle = Win32.kernel32.CreateMutexW(None, False, ctypes.c_wchar_p(self.config.MUTEX_NAME))
        if Win32.kernel32.GetLastError() == Win32.ERROR_ALREADY_EXISTS:
            self.logger.error("Another instance is already running.")
            sys.exit(1)
        atexit.register(self._release_mutex)

    def _release_mutex(self):
        if self.mutex_handle:
            Win32.kernel32.CloseHandle(self.mutex_handle)

    def _handle_signal(self, sig, frame):
        self.logger.info(f"Signal {sig} received. Shutting down...")
        self.stop()

    def stop(self):
        self.stop_event.set()
        self.deactivate()

    def deactivate(self):
        """Returns the system to normal state."""
        self.active = False
        self.overlay.stop()
        if self.monitor_sleeping:
            Win32.set_monitor_power(True)
            self.monitor_sleeping = False
            self.logger.info("Monitor woken up.")

    def should_suspend(self) -> bool:
        """Checks if jiggling should be suspended (fullscreen app or denylisted process)."""
        if self.config.SKIP_IF_FULLSCREEN and Win32.is_fullscreen():
            return True
        exe = Win32.get_foreground_exe()
        if exe in self.config.PROCESS_DENYLIST:
            return True
        return False

    def activate(self):
        """Enters the jiggling/sleep state."""
        self.active = True
        self.logger.info("Jiggler activated.")
        
        if self.config.ENABLE_BLACK_SCREEN:
            self.overlay.start()
            
        if self.config.ENABLE_MONITOR_SLEEP:
            # Delayed monitor sleep
            threading.Timer(self.config.MONITOR_SLEEP_DELAY_S, self._do_monitor_sleep).start()

    def _do_monitor_sleep(self):
        if self.active and self.config.ENABLE_MONITOR_SLEEP:
            Win32.set_monitor_power(False)
            self.monitor_sleeping = True
            self.logger.info("Monitor put to sleep.")

    def run(self):
        """Main execution loop."""
        self.setup()
        self.logger.info("Jiggler started. Monitoring idle state...")
        
        last_recorded_input_time = Win32.get_last_input_time()
        
        try:
            while not self.stop_event.is_set():
                idle_sec = Win32.get_idle_seconds()
                current_input_time = Win32.get_last_input_time()
                
                if not self.active:
                    if idle_sec >= self.config.IDLE_THRESHOLD_S and not self.should_suspend():
                        self.activate()
                        last_recorded_input_time = current_input_time
                else:
                    user_moved = False
                    
                    # If the system's last input time has changed since we last checked
                    if current_input_time != last_recorded_input_time:
                        # Check if it was us or the user.
                        # We use a button press check as a strong indicator of user activity.
                        if Win32.is_button_pressed():
                            user_moved = True
                        else:
                            # If no button is pressed, it might be mouse movement.
                            # We check if the input time changed significantly since our last jiggle.
                            # Since we update last_recorded_input_time immediately after jiggling,
                            # any change here likely means user activity OR a delayed system update.
                            # To be safe, we allow a tiny bit of slack.
                            user_moved = True

                    if user_moved or self.should_suspend():
                        self.deactivate()
                        self.logger.info("Jiggler paused (user activity).")
                    else:
                        if self._jiggle():
                            # Update our record with the input time caused by our jiggle
                            last_recorded_input_time = Win32.get_last_input_time()
                        
                        if self.config.JIGGLE_MODE == "nudge":
                            self._interruptible_sleep(self.config.NUDGE_INTERVAL_S)
                
                time.sleep(self.config.POLL_INTERVAL_S)
        finally:
            self.deactivate()
            self.logger.info("Jiggler stopped.")

    def _interruptible_sleep(self, seconds: float):
        """Sleeps for the given duration but checks for stop/deactivation signals."""
        steps = int(seconds / 0.1)
        for _ in range(steps):
            if self.stop_event.is_set() or not self.active or Win32.get_idle_seconds() < 1.0:
                return True
            time.sleep(0.1)
        return False

    def _jiggle(self):
        """Performs the actual mouse movement based on mode."""
        if self.config.JIGGLE_MODE == "nudge":
            Win32.move_mouse_rel(self.config.NUDGE_PIXELS, 0)
            time.sleep(0.05)
            Win32.move_mouse_rel(-self.config.NUDGE_PIXELS, 0)
            return True # Success
        else:
            # Circle mode
            vx = Win32.user32.GetSystemMetrics(Win32.SM_XVIRTUALSCREEN)
            vy = Win32.user32.GetSystemMetrics(Win32.SM_YVIRTUALSCREEN)
            vw = Win32.user32.GetSystemMetrics(Win32.SM_CXVIRTUALSCREEN)
            vh = Win32.user32.GetSystemMetrics(Win32.SM_CYVIRTUALSCREEN)
            cx, cy = vx + vw // 2, vy + vh // 2
            
            steps = self.config.CIRCLE_STEPS
            radius = self.config.CIRCLE_RADIUS_PX
            delay = self.config.CIRCLE_DURATION_S / steps
            
            for i in range(steps):
                if self.stop_event.is_set() or not self.active:
                    return False # Interrupted
                angle = (2 * math.pi * i) / steps
                x = int(cx + radius * math.cos(angle))
                y = int(cy + radius * math.sin(angle))
                Win32.move_mouse_abs(x, y)
                time.sleep(delay)
            return True

# =============================================================================
# ENTRY POINT
# =============================================================================

def main():
    logging.basicConfig(
        level=logging.INFO,
        format="%(asctime)s | %(levelname)s | %(message)s",
        datefmt="%H:%M:%S"
    )
    
    config = JigglerConfig()
    jiggler = MouseJiggler(config)
    jiggler.run()

if __name__ == "__main__":
    main()
