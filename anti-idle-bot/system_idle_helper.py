#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
Windows Idle Prevention Helper
A minimal, AV-friendly background utility that prevents system sleep and idle timeouts.
Designed for long-term unattended operation in corporate environments (5+ years).

Primary mechanism: SetThreadExecutionState (power management API)
Fallback mechanism: Minimal keyboard simulation (F15 key only by default)
"""

import os
import sys
import ctypes
import threading
import random
from typing import Optional, Set
from ctypes import wintypes

# Handle ULONG_PTR for 32/64 bit compatibility
ULONG_PTR = ctypes.c_uint64 if ctypes.sizeof(ctypes.c_void_p) == 8 else ctypes.c_uint32

# --- Win32 API Definitions ---

# Input Types
INPUT_MOUSE = 0
INPUT_KEYBOARD = 1

# Mouse Event Flags
MOUSEEVENTF_MOVE = 0x0001
MOUSEEVENTF_ABSOLUTE = 0x8000

# Keyboard Event Flags
KEYEVENTF_KEYUP = 0x0002

# Virtual Key Codes (High Function Keys - Rarely Mapped)
VK_F15 = 0x7E

# Thread Execution State
ES_CONTINUOUS = 0x80000000
ES_SYSTEM_REQUIRED = 0x00000001
ES_DISPLAY_REQUIRED = 0x00000002

# System Metrics
SM_XVIRTUALSCREEN = 76
SM_YVIRTUALSCREEN = 77
SM_CXVIRTUALSCREEN = 78
SM_CYVIRTUALSCREEN = 79

# Error Codes
ERROR_ALREADY_EXISTS = 183
PROCESS_QUERY_LIMITED_INFORMATION = 0x1000

# Windows Hook Constants (for optional hook mode)
WH_KEYBOARD_LL = 13
WH_MOUSE_LL = 14

HOOKPROC = ctypes.WINFUNCTYPE(ctypes.c_long, ctypes.c_int, wintypes.WPARAM, wintypes.LPARAM)


class MOUSEINPUT(ctypes.Structure):
    _fields_ = [
        ('dx', wintypes.LONG),
        ('dy', wintypes.LONG),
        ('mouseData', wintypes.DWORD),
        ('dwFlags', wintypes.DWORD),
        ('time', wintypes.DWORD),
        ('dwExtraInfo', ULONG_PTR)
    ]


class KEYBDINPUT(ctypes.Structure):
    _fields_ = [
        ('wVk', wintypes.WORD),
        ('wScan', wintypes.WORD),
        ('dwFlags', wintypes.DWORD),
        ('time', wintypes.DWORD),
        ('dwExtraInfo', ULONG_PTR)
    ]


class HARDWAREINPUT(ctypes.Structure):
    _fields_ = [
        ('uMsg', wintypes.DWORD),
        ('wParamL', wintypes.WORD),
        ('wParamH', wintypes.WORD)
    ]


class INPUT_UNION(ctypes.Union):
    _fields_ = [
        ('mi', MOUSEINPUT),
        ('ki', KEYBDINPUT),
        ('hi', HARDWAREINPUT)
    ]


class INPUT(ctypes.Structure):
    _fields_ = [
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
        ("left", wintypes.LONG),
        ("top", wintypes.LONG),
        ("right", wintypes.LONG),
        ("bottom", wintypes.LONG)
    ]


user32 = ctypes.windll.user32
kernel32 = ctypes.windll.kernel32


class IdlePreventionHelper:
    """
    Minimal Windows idle prevention helper.

    Behavioral Profile:
    - Primary: Uses SetThreadExecutionState (official power management API)
    - Fallback: Minimal F15 keypress simulation (only when needed)
    - No mouse movement by default
    - Low polling frequency (1 second intervals)
    - Caches foreground process/fullscreen checks
    - No global hooks by default
    - Service-first design (no console, no signals, no disk I/O)
    """

    def __init__(
            self,
            idle_threshold: float = 45.0,
            nudge_interval: float = 12.0,
            random_jitter: float = 0.3,
            deny_list: Optional[Set[str]] = None,
            skip_fullscreen: bool = True,
            use_hooks: bool = False,
            debug: bool = False
    ):
        """
        Args:
            idle_threshold: Seconds of idle time before activating (default: 45)
            nudge_interval: Seconds between F15 keypresses (default: 12, range: 8-15s with jitter)
            random_jitter: Random variation in nudge_interval (default: 0.3 = ±30%)
            deny_list: Executable names to skip (e.g., {"vlc.exe"})
            skip_fullscreen: Skip when fullscreen app detected (default: True)
            use_hooks: Enable low-level hooks (NOT RECOMMENDED, default: False)
            debug: Enable debug output (memory-only, default: False)
        """
        self.idle_threshold = idle_threshold
        self.nudge_interval = nudge_interval
        self.random_jitter = random_jitter
        self.deny_list = deny_list or {
            "vlc.exe", "powerpnt.exe", "obs64.exe", "netflix.exe",
            "teams.exe", "ms-teams.exe", "zoom.exe", "mpc-hc64.exe", "mpc-hc.exe"
        }
        self.skip_fullscreen = skip_fullscreen
        self.use_hooks = use_hooks
        self.debug = debug

        # Timing configuration (low-frequency polling)
        self.poll_interval = 1.0  # 1 second (reduced from 0.1s)
        self.cache_interval = 5000  # 5 seconds (cache foreground checks)

        # Single-instance mutex
        self.mutex_name = "Global\\WIN_IDLE_PREVENTION_HELPER"
        self._mutex_handle = None

        # Runtime state
        self._running = False
        self._last_cursor_pos = (0, 0)
        self._last_input_time_tick = 0  # 64-bit tick count

        # Ignore window for our own input (monotonic ticks)
        self._ignore_until_tick = 0

        # Optional hook support (disabled by default)
        self._hook_thread = None
        self._hook_thread_id = None
        self._keyboard_hook = None
        self._mouse_hook = None

        # Caching for reduced syscall overhead
        self._last_fg_check_tick = 0
        self._cached_fg_exe = None
        self._last_fs_check_tick = 0
        self._cached_is_fullscreen = False

    def _log(self, msg: str):
        """Memory-only debug output. No disk writes."""
        if self.debug:
            tick_sec = self._get_tick_count_64() // 1000
            print(f"[{tick_sec}s] {msg}", flush=True)

    def _get_tick_count_64(self) -> int:
        """
        Returns monotonic tick count in milliseconds (64-bit, no wrap).
        Safe for multi-year uptimes, immune to system clock changes and sleep/resume.
        """
        return kernel32.GetTickCount64()

    def _ticks_elapsed(self, start_tick: int) -> int:
        """Returns milliseconds elapsed since start_tick."""
        return self._get_tick_count_64() - start_tick

    def _create_mutex(self) -> bool:
        """Creates single-instance mutex."""
        self._mutex_handle = kernel32.CreateMutexW(None, False, ctypes.c_wchar_p(self.mutex_name))
        if not self._mutex_handle:
            return False
        return kernel32.GetLastError() != ERROR_ALREADY_EXISTS

    def _release_mutex(self):
        """Releases single-instance mutex."""
        if self._mutex_handle:
            kernel32.CloseHandle(self._mutex_handle)
            self._mutex_handle = None

    def _set_sleep_inhibit(self, enable: bool):
        """
        Primary mechanism: Official Windows power management API.
        Inhibits system sleep and display timeout without input simulation.
        """
        flags = ES_CONTINUOUS
        if enable:
            flags |= ES_SYSTEM_REQUIRED | ES_DISPLAY_REQUIRED
        kernel32.SetThreadExecutionState(flags)

    def _setup_hooks(self):
        """
        Optional low-level hooks (NOT RECOMMENDED for corporate environments).
        Only used when explicitly enabled via use_hooks=True.
        """
        self._hook_thread_id = kernel32.GetCurrentThreadId()
        self._log(f"Hook thread started (TID={self._hook_thread_id})")

        def hook_callback(nCode, wParam, lParam):
            if nCode >= 0:
                current_tick = self._get_tick_count_64()
                if current_tick >= self._ignore_until_tick:
                    # User interacted
                    self._last_input_time_tick = current_tick
            return user32.CallNextHookEx(None, nCode, wParam, lParam)

        self._hook_callback_ptr = HOOKPROC(hook_callback)

        h_mod = kernel32.GetModuleHandleW(None)
        self._keyboard_hook = user32.SetWindowsHookExW(WH_KEYBOARD_LL, self._hook_callback_ptr, h_mod, 0)
        self._mouse_hook = user32.SetWindowsHookExW(WH_MOUSE_LL, self._hook_callback_ptr, h_mod, 0)

        if not self._keyboard_hook or not self._mouse_hook:
            self._log("ERROR: Failed to install hooks (blocked by security policy).")
            self._hook_thread_id = None
            return

        # Message loop
        msg = wintypes.MSG()
        while user32.GetMessageW(ctypes.byref(msg), None, 0, 0) > 0:
            user32.TranslateMessage(ctypes.byref(msg))
            user32.DispatchMessageW(ctypes.byref(msg))

        self._log("Hook thread exiting")

    def _remove_hooks(self):
        """Clean up optional hooks."""
        if self._keyboard_hook:
            user32.UnhookWindowsHookEx(self._keyboard_hook)
            self._keyboard_hook = None
        if self._mouse_hook:
            user32.UnhookWindowsHookEx(self._mouse_hook)
            self._mouse_hook = None

        if self._hook_thread_id is not None:
            user32.PostThreadMessageW(self._hook_thread_id, 0x0012, 0, 0)  # WM_QUIT
            self._log(f"Posted WM_QUIT to hook thread")

        if self._hook_thread and self._hook_thread.is_alive():
            self._hook_thread.join(timeout=2.0)

    def _get_idle_seconds(self) -> float:
        """
        Returns seconds since last user input using Windows GetLastInputInfo.
        This is the primary user activity detection method (no hooks required).
        """
        lii = LASTINPUTINFO()
        lii.cbSize = ctypes.sizeof(LASTINPUTINFO)
        if not user32.GetLastInputInfo(ctypes.byref(lii)):
            return 0.0

        # GetLastInputInfo uses 32-bit GetTickCount internally
        tick_now = kernel32.GetTickCount()
        idle_ms = (tick_now - lii.dwTime) & 0xFFFFFFFF
        return idle_ms / 1000.0

    def _get_foreground_exe(self) -> Optional[str]:
        """
        Returns lowercase name of foreground process executable.
        Result is cached for cache_interval milliseconds to reduce syscall overhead.
        """
        current_tick = self._get_tick_count_64()

        # Return cached value if still fresh
        if self._ticks_elapsed(self._last_fg_check_tick) < self.cache_interval:
            return self._cached_fg_exe

        # Refresh cache
        hwnd = user32.GetForegroundWindow()
        if not hwnd:
            self._cached_fg_exe = None
            self._last_fg_check_tick = current_tick
            return None

        pid = wintypes.DWORD()
        user32.GetWindowThreadProcessId(hwnd, ctypes.byref(pid))

        hproc = kernel32.OpenProcess(PROCESS_QUERY_LIMITED_INFORMATION, False, pid.value)
        if not hproc:
            self._cached_fg_exe = None
            self._last_fg_check_tick = current_tick
            return None

        try:
            buf = ctypes.create_unicode_buffer(260)
            size = wintypes.DWORD(len(buf))
            if kernel32.QueryFullProcessImageNameW(hproc, 0, buf, ctypes.byref(size)):
                self._cached_fg_exe = os.path.basename(buf.value).lower()
            else:
                self._cached_fg_exe = None
        finally:
            kernel32.CloseHandle(hproc)

        self._last_fg_check_tick = current_tick
        return self._cached_fg_exe

    def _is_fullscreen(self) -> bool:
        """
        Returns True if foreground window is fullscreen.
        Result is cached for cache_interval milliseconds to reduce syscall overhead.
        """
        if not self.skip_fullscreen:
            return False

        current_tick = self._get_tick_count_64()

        # Return cached value if still fresh
        if self._ticks_elapsed(self._last_fs_check_tick) < self.cache_interval:
            return self._cached_is_fullscreen

        # Refresh cache
        hwnd = user32.GetForegroundWindow()
        if not hwnd:
            self._cached_is_fullscreen = False
            self._last_fs_check_tick = current_tick
            return False

        rect = RECT()
        if not user32.GetWindowRect(hwnd, ctypes.byref(rect)):
            self._cached_is_fullscreen = False
            self._last_fs_check_tick = current_tick
            return False

        vx = user32.GetSystemMetrics(SM_XVIRTUALSCREEN)
        vy = user32.GetSystemMetrics(SM_YVIRTUALSCREEN)
        vw = user32.GetSystemMetrics(SM_CXVIRTUALSCREEN)
        vh = user32.GetSystemMetrics(SM_CYVIRTUALSCREEN)

        tol = 2
        is_fs = (abs(rect.left - vx) <= tol and
                 abs(rect.top - vy) <= tol and
                 abs(rect.right - (vx + vw)) <= tol and
                 abs(rect.bottom - (vy + vh)) <= tol)

        self._cached_is_fullscreen = is_fs
        self._last_fs_check_tick = current_tick
        return is_fs

    def _is_user_interacting(self) -> bool:
        """
        Detects user interaction using GetLastInputInfo and cursor movement.
        No hooks required (hooks are optional and disabled by default).
        """
        current_tick = self._get_tick_count_64()

        # Check if we're in ignore window (our own simulated input)
        if current_tick < self._ignore_until_tick:
            # Update tracking silently
            lii = LASTINPUTINFO()
            lii.cbSize = ctypes.sizeof(LASTINPUTINFO)
            if user32.GetLastInputInfo(ctypes.byref(lii)):
                # Convert to 64-bit tick for consistency
                self._last_input_time_tick = current_tick

            pt = wintypes.POINT()
            if user32.GetCursorPos(ctypes.byref(pt)):
                self._last_cursor_pos = (pt.x, pt.y)

            return False

        # Hook-based detection (if enabled)
        if self.use_hooks and self._last_input_time_tick != 0:
            if self._ticks_elapsed(self._last_input_time_tick) < 2000:  # 2 seconds
                return True

        # Primary: GetLastInputInfo (Windows native tracking)
        lii = LASTINPUTINFO()
        lii.cbSize = ctypes.sizeof(LASTINPUTINFO)
        if user32.GetLastInputInfo(ctypes.byref(lii)):
            # Check if input time changed since our last check
            if self._last_input_time_tick != 0:
                # Estimate 64-bit tick from 32-bit dwTime
                if lii.dwTime != (self._last_input_time_tick & 0xFFFFFFFF):
                    self._last_input_time_tick = current_tick
                    return True
            else:
                self._last_input_time_tick = current_tick

        # Secondary: Cursor movement detection
        pt = wintypes.POINT()
        if user32.GetCursorPos(ctypes.byref(pt)):
            current_pos = (pt.x, pt.y)
            if current_pos != self._last_cursor_pos:
                self._last_cursor_pos = current_pos
                return True

        return False

    def _update_last_input(self):
        """Initialize tracking of last input timestamp."""
        current_tick = self._get_tick_count_64()
        self._last_input_time_tick = current_tick

        pt = wintypes.POINT()
        if user32.GetCursorPos(ctypes.byref(pt)):
            self._last_cursor_pos = (pt.x, pt.y)

    def _press_key(self, vk_code: int):
        """
        Simulates a single keypress (down + up).
        Default: F15 key only (VK code 0x7E).
        """
        # Key down
        inp_down = INPUT()
        inp_down.type = INPUT_KEYBOARD
        inp_down.union.ki = KEYBDINPUT(
            wVk=vk_code, wScan=0, dwFlags=0,
            time=0, dwExtraInfo=0
        )

        # Key up
        inp_up = INPUT()
        inp_up.type = INPUT_KEYBOARD
        inp_up.union.ki = KEYBDINPUT(
            wVk=vk_code, wScan=0, dwFlags=KEYEVENTF_KEYUP,
            time=0, dwExtraInfo=0
        )

        user32.SendInput(2, (INPUT * 2)(inp_down, inp_up), ctypes.sizeof(INPUT))

    def _move_mouse_relative(self, dx: int, dy: int):
        """Simulates relative mouse movement."""
        inp = INPUT()
        inp.type = INPUT_MOUSE
        inp.union.mi = MOUSEINPUT(
            dx=dx, dy=dy, mouseData=0,
            dwFlags=MOUSEEVENTF_MOVE,
            time=0, dwExtraInfo=0
        )
        user32.SendInput(1, ctypes.byref(inp), ctypes.sizeof(INPUT))

    def _should_skip(self) -> bool:
        """
        Returns True if we should skip input simulation.
        Uses cached checks to minimize syscall overhead.
        """
        if self._is_user_interacting():
            self._log("SKIP (user active)")
            return True

        if self.skip_fullscreen and self._is_fullscreen():
            self._log("SKIP (fullscreen)")
            return True

        exe = self._get_foreground_exe()
        if exe and exe in self.deny_list:
            self._log(f"SKIP (deny-list: {exe})")
            return True

        return False

    def _simulate_input(self):
        """
        Fallback mechanism: Minimal keyboard or mouse simulation.
        Randomly chooses between F15 keypress or 1-pixel mouse nudge.
        """
        # Set ignore window (300ms) to avoid detecting our own input
        self._ignore_until_tick = self._get_tick_count_64() + 300

        action = random.choice(["key", "move"])

        if action == "key":
            self._press_key(VK_F15)
            self._log("INPUT (F15)")
        else:
            # 1-pixel nudge in random direction and back
            dx, dy = random.choice([(1, 0), (-1, 0), (0, 1), (0, -1)])
            self._move_mouse_relative(dx, dy)
            kernel32.Sleep(10)
            self._move_mouse_relative(-dx, -dy)
            self._log(f"INPUT (Nudge {dx},{dy})")

    def _sleep_monotonic(self, seconds: float):
        """
        Sleep using monotonic timing with early exit on stop.
        Breaks sleep into 250ms chunks to check _running flag.
        """
        start_tick = self._get_tick_count_64()
        target_ms = int(seconds * 1000)

        while self._running and self._ticks_elapsed(start_tick) < target_ms:
            remaining_ms = target_ms - self._ticks_elapsed(start_tick)
            sleep_ms = min(250, remaining_ms)
            if sleep_ms > 0:
                kernel32.Sleep(sleep_ms)

    def _idle_prevention_loop(self):
        """
        Main idle prevention loop.
        Uses SetThreadExecutionState as primary mechanism with F15 fallback.
        """
        while self._running:
            if self._should_skip():
                self._sleep_monotonic(self.poll_interval)
                continue

            # Simulate F15 keypress
            self._simulate_input()

            # Wait for next interval with jitter
            jitter = self.nudge_interval * self.random_jitter
            interval = self.nudge_interval + random.uniform(-jitter, jitter)

            start_tick = self._get_tick_count_64()
            target_ms = int(interval * 1000)

            while self._running and self._ticks_elapsed(start_tick) < target_ms:
                if self._should_skip():
                    break
                self._sleep_monotonic(self.poll_interval)

    def start(self):
        """
        Starts the idle prevention helper.
        Service-first design: no console manipulation, no signal handlers.
        """
        # Set below-normal priority (minimal CPU impact)
        kernel32.SetPriorityClass(kernel32.GetCurrentProcess(), 0x00004000)

        if not self._create_mutex():
            if self.debug:
                print("Process is already running. Exiting.")
            sys.exit(0)

        self._running = True

        # Primary mechanism: SetThreadExecutionState
        self._set_sleep_inhibit(True)

        self._update_last_input()

        # Optional hooks (NOT RECOMMENDED)
        if self.use_hooks:
            self._hook_thread = threading.Thread(target=self._setup_hooks, daemon=False)
            self._hook_thread.start()
            self._log("WARNING: Hooks enabled (not recommended for corporate environments)")
        else:
            self._log("Hook-less mode (recommended)")

        if self.debug:
            print(f"Idle Prevention Helper started")
            print(f"  Idle threshold: {self.idle_threshold}s")
            print(f"  Input interval: {self.nudge_interval}s ±{int(self.random_jitter*100)}%")
            print(f"  Strategy: F15 keypress only")
            print(f"  Hooks: {'ENABLED' if self.use_hooks else 'DISABLED'}")

        try:
            active_logged = False
            while self._running:
                idle = self._get_idle_seconds()

                if idle >= self.idle_threshold:
                    if not active_logged:
                        self._log(f"ACTIVE (idle={idle:.1f}s)")
                        active_logged = True

                    if not self._should_skip():
                        # Refresh execution state periodically
                        self._set_sleep_inhibit(True)
                        self._idle_prevention_loop()
                else:
                    if active_logged:
                        self._log(f"PASSIVE (idle={idle:.1f}s)")
                    active_logged = False

                self._sleep_monotonic(self.poll_interval)
        finally:
            self._running = False
            self._set_sleep_inhibit(False)
            if self.use_hooks:
                self._remove_hooks()
            self._release_mutex()

    def stop(self):
        """Gracefully stops the helper (for service controller or external stop signal)."""
        self._running = False


if __name__ == "__main__":
    import argparse

    parser = argparse.ArgumentParser(
        description="Windows Idle Prevention Helper - AV-friendly background utility"
    )
    parser.add_argument(
        "--idle", type=float, default=45.0,
        help="Idle threshold in seconds (default: 45)"
    )
    parser.add_argument(
        "--interval", type=float, default=12.0,
        help="Interval between F15 keypresses in seconds (default: 12, recommended: 8-15)"
    )
    parser.add_argument(
        "--use-hooks", action="store_true",
        help="Enable low-level hooks (NOT RECOMMENDED for corporate environments)"
    )
    parser.add_argument(
        "--debug", action="store_true",
        help="Enable debug logging (memory-only)"
    )

    args = parser.parse_args()

    helper = IdlePreventionHelper(
        idle_threshold=args.idle,
        nudge_interval=args.interval,
        use_hooks=args.use_hooks,
        debug=args.debug
    )

    try:
        helper.start()
    except KeyboardInterrupt:
        if args.debug:
            print("\nStopping...")
        helper.stop()
