# -*- coding: utf-8 -*-
"""显示器亮度悬浮条 + 系统托盘图标

【悬浮条】
- 拖滑块 / 滚轮（悬停在条子上滚）调亮度
- 默认 10 秒无操作自动隐藏
- 位置默认锁定；双击条子背景解锁，可拖动；再双击重新锁定
- 双击中间的数字 = 恢复默认亮度

【任务栏托盘图标】
- 图标上显示当前亮度数字
- 鼠标滚轮悬停在图标上 -> 调亮度
- 左键单击 -> 显示 / 隐藏悬浮条
- 右键 -> 菜单（显示/隐藏、恢复默认、设置默认亮度、开机自启、退出）

【全局快捷键】Ctrl + Alt + ↑ / ↓   调亮 / 调暗（步进 5）

【默认亮度】默认亮度存在 brightness.txt，开机自启时会直接应用。
可在托盘或悬浮条的右键菜单里点「设置默认亮度…」手动改，也可手改该文件。

关掉悬浮条只是隐藏，程序仍在托盘运行；托盘菜单里"退出"才真正结束。

【唯一实例】重复启动不会再开一个托盘图标，只会把已在运行那个实例的悬浮条叫出来。

【线程模型】DDC/CI 是毫秒级慢速 I/O，显示器无响应时可能阻塞数秒，
所有调节都丢给 ddc_worker 后台线程执行，Tk 主线程只改界面和投递请求，
拖动停止 100ms 后才真正应用（去抖），保证界面和退出永不卡死。
"""
import ctypes
import json
import math
import os
import queue
import sys
import threading
import time
import tkinter as tk
from ctypes import wintypes
from tkinter import simpledialog

HERE = os.path.dirname(os.path.abspath(__file__))
if HERE not in sys.path:
    sys.path.insert(0, HERE)

import set_brightness as sb  # noqa: E402
import tray as tray_mod  # noqa: E402

STATE_FILE = os.path.join(HERE, "widget_state.json")
DEFAULT_CFG_HEADER = (
    "# 默认亮度（0-100），改完保存即可；也可以在托盘/悬浮条右键菜单里设置。\n"
    "# 每次启动本程序（含开机自启）都会把显示器设成这个值。\n"
    "%d\n"
)
HOTKEY_STEP = 5
IDLE_LIMIT = 10          # 秒：无操作自动隐藏
COMMIT_DELAY_MS = 100    # 去抖：停止调节这么久后才把最终值交给后台线程

MUTEX_NAME = "Local\\SancBrightnessWidget.single"
WAKEUP_NAME = "Local\\SancBrightnessWidget.wakeup"
ERROR_ALREADY_EXISTS = 183
WAIT_OBJECT_0 = 0
EVENT_MODIFY_STATE = 0x0002

MOD_ALT = 0x0001
MOD_CONTROL = 0x0002
MOD_NOREPEAT = 0x4000
WM_HOTKEY = 0x0312
WM_QUIT = 0x0012
VK_UP = 0x26
VK_DOWN = 0x28
HK_UP = 1
HK_DOWN = 2

CARD = "#ffffff"
BORDER = "#e3e6ec"
FG = "#23272e"
MUTED = "#a2a9b3"
ACCENT = "#2f7cf6"
TROUGH = "#eaeef4"
SUN = "#f0a93c"
MAGIC = "#ff00fe"
FONT = "Microsoft YaHei UI"
MONO = "Consolas"

# 单实例守卫用的命名内核对象
k32 = ctypes.WinDLL("kernel32", use_last_error=True)
k32.CreateMutexW.restype = wintypes.HANDLE
k32.CreateMutexW.argtypes = [wintypes.LPVOID, wintypes.BOOL, wintypes.LPCWSTR]
k32.CreateEventW.restype = wintypes.HANDLE
k32.CreateEventW.argtypes = [wintypes.LPVOID, wintypes.BOOL, wintypes.BOOL,
                             wintypes.LPCWSTR]
k32.OpenEventW.restype = wintypes.HANDLE
k32.OpenEventW.argtypes = [wintypes.DWORD, wintypes.BOOL, wintypes.LPCWSTR]
k32.SetEvent.restype = wintypes.BOOL
k32.SetEvent.argtypes = [wintypes.HANDLE]
k32.CloseHandle.restype = wintypes.BOOL
k32.CloseHandle.argtypes = [wintypes.HANDLE]
k32.WaitForSingleObject.restype = wintypes.DWORD
k32.WaitForSingleObject.argtypes = [wintypes.HANDLE, wintypes.DWORD]

# 热键线程需要的最小 User32 面：模块句柄用于 RegisterClass/CreateWindow。
k32.GetModuleHandleW.restype = wintypes.HANDLE
k32.GetModuleHandleW.argtypes = [wintypes.LPCWSTR]

# 设计稿尺寸（逻辑像素，运行时按 DPI 缩放）
D_W, D_H = 384, 54
D_SL_X1, D_SL_X2 = 58, 250
D_X_VALUE = 278
D_X_MINUS, D_X_PLUS, D_X_CLOSE = 306, 334, 362


def load_state():
    try:
        with open(STATE_FILE, encoding="utf-8") as f:
            return json.load(f)
    except Exception:
        return {}


def save_state(state):
    try:
        with open(STATE_FILE, "w", encoding="utf-8") as f:
            json.dump(state, f, ensure_ascii=False)
    except Exception:
        pass


def read_default_value():
    v = sb.read_config()
    return 45 if v is None else v


def write_default_value(value):
    """把默认亮度写回 brightness.txt（开机自启时读的就是它）。"""
    cfg = os.path.join(HERE, "brightness.txt")
    with open(cfg, "w", encoding="utf-8") as f:
        f.write(DEFAULT_CFG_HEADER % int(value))


def apply_all(value):
    value = max(0, min(100, int(value)))
    mons = sb.list_monitors()
    n = 0
    try:
        for _hmon, arr, cnt in mons:
            for i in range(cnt):
                pm = arr[i]
                cur, _mx = sb.get_brightness(pm.hPhysicalMonitor)
                if cur is None:
                    continue
                if sb.set_brightness(pm.hPhysicalMonitor, value):
                    n += 1
    finally:
        for _hmon, arr, cnt in mons:
            sb.dxva2.DestroyPhysicalMonitors(cnt, arr)
    return n


def read_current():
    mons = sb.list_monitors()
    val = None
    try:
        for _hmon, arr, cnt in mons:
            for i in range(cnt):
                pm = arr[i]
                cur, _mx = sb.get_brightness(pm.hPhysicalMonitor)
                if cur is not None and val is None:
                    val = cur
    finally:
        for _hmon, arr, cnt in mons:
            sb.dxva2.DestroyPhysicalMonitors(cnt, arr)
    return val


def ddc_worker(cmd_q, out_q, stop_event):
    """后台线程：所有 DDC/CI 慢速 I/O 都在这里做，绝不碰 Tk 主线程。

    拖动滑块会连续投递请求，这里只执行最新的一个值（旧值直接丢弃），
    避免显示器被排队请求"回放"一遍。应用完回读一次实际亮度发回 UI 校正。
    """
    while not stop_event.is_set():
        try:
            item = cmd_q.get(timeout=0.2)
        except queue.Empty:
            continue
        latest = item
        while True:
            try:
                nxt = cmd_q.get_nowait()
            except queue.Empty:
                break
            latest = nxt
        if latest[0] != "apply":
            continue
        requested = latest[1]
        try:
            apply_all(requested)
            actual = read_current()
        except Exception:
            actual = None
        out_q.put(("value", requested, actual))


def pythonw_path():
    exe = sys.executable
    if exe.lower().endswith("python.exe"):
        cand = exe[: -len("python.exe")] + "pythonw.exe"
        if os.path.exists(cand):
            return cand
    return exe


def acquire_single_instance():
    """占住全局唯一的命名互斥体。

    已经有一个实例在跑就返回 None，并顺手把它的悬浮条叫出来（避免重复启动时
    "双击没反应"）。互斥体随进程存在，进程退出即释放。
    """
    mutex = k32.CreateMutexW(None, False, MUTEX_NAME)
    if ctypes.get_last_error() == ERROR_ALREADY_EXISTS:
        if mutex:
            k32.CloseHandle(mutex)
        other = k32.OpenEventW(EVENT_MODIFY_STATE, False, WAKEUP_NAME)
        if other:
            k32.SetEvent(other)
            k32.CloseHandle(other)
        return None
    return (mutex, k32.CreateEventW(None, False, False, WAKEUP_NAME))


def enable_dpi_awareness():
    """必须在创建 Tk 窗口之前调用：否则高分屏下会被系统拉伸放大（发虚）。"""
    try:
        ctypes.WinDLL("shcore").SetProcessDpiAwareness(2)
    except Exception:
        try:
            ctypes.WinDLL("user32").SetProcessDPIAware()
        except Exception:
            pass


def dpi_scale():
    try:
        return max(1.0, ctypes.WinDLL("shcore").GetScaleFactorForDevice(0) / 100.0)
    except Exception:
        pass
    try:
        u = ctypes.WinDLL("user32")
        g = ctypes.WinDLL("gdi32")
        dc = u.GetDC(None)
        dpi = g.GetDeviceCaps(dc, 88)      # LOGPIXELSX
        u.ReleaseDC(None, dc)
        return max(1.0, dpi / 96.0)
    except Exception:
        return 1.0


HOTKEY_CLASS = "SancBrightnessHotkeyWnd"
HOTKEY_WNDPROC = ctypes.WINFUNCTYPE(
    ctypes.c_ssize_t, wintypes.HWND, wintypes.UINT, wintypes.WPARAM, wintypes.LPARAM)


def hotkey_worker(q, stop_event):
    """全局热键线程。

    【为什么必须有自己的隐藏窗口】
    之前这里用 `GetMessageW(..., None, 0, 0)` 抓整个线程的消息。Win32 规定：
    窗口消息由「最早调用 GetMessage 的线程」处理，而窗口过程会在那个线程上执行。
    本进程内 tray 图标窗口与 Tk 窗口同属一个进程，热键线程一旦抢先取走它们的
    窗口消息，tray 的 WndProc（内部含 TrackPopupMenu 模态循环）就会跑在热键线程上，
    与 Tk 主循环争抢消息、互相阻塞 —— 表现就是界面整窗卡死、点 × 也关不掉。

    现在改成：本线程注册专属窗口类并创建一个隐藏窗口，RegisterHotKey 把热键
    投递到该窗口，GetMessageW 也只取本窗口的消息。窗口消息永不跨线程，
    从结构上杜绝了上面那条死锁路径。
    """
    u = ctypes.WinDLL("user32", use_last_error=True)
    k = ctypes.WinDLL("kernel32", use_last_error=True)
    u.RegisterHotKey.argtypes = [wintypes.HWND, ctypes.c_int, ctypes.c_uint, ctypes.c_uint]
    u.RegisterHotKey.restype = wintypes.BOOL
    u.UnregisterHotKey.argtypes = [wintypes.HWND, ctypes.c_int]
    u.UnregisterHotKey.restype = wintypes.BOOL
    u.GetMessageW.argtypes = [ctypes.POINTER(wintypes.MSG), wintypes.HWND,
                              ctypes.c_uint, ctypes.c_uint]
    u.GetMessageW.restype = ctypes.c_int
    u.PostThreadMessageW.argtypes = [wintypes.DWORD, ctypes.c_uint,
                                     wintypes.WPARAM, wintypes.LPARAM]
    u.RegisterClassW.argtypes = [ctypes.c_void_p]
    u.RegisterClassW.restype = wintypes.WORD
    u.CreateWindowExW.argtypes = [wintypes.DWORD, wintypes.LPCWSTR, wintypes.LPCWSTR,
                                  wintypes.DWORD, ctypes.c_int, ctypes.c_int,
                                  ctypes.c_int, ctypes.c_int, wintypes.HWND,
                                  wintypes.HMENU, wintypes.HINSTANCE, wintypes.LPVOID]
    u.CreateWindowExW.restype = wintypes.HWND
    u.DefWindowProcW.argtypes = [wintypes.HWND, wintypes.UINT,
                                 wintypes.WPARAM, wintypes.LPARAM]
    u.DefWindowProcW.restype = ctypes.c_ssize_t
    u.TranslateMessage.argtypes = [ctypes.POINTER(wintypes.MSG)]
    u.TranslateMessage.restype = wintypes.BOOL
    u.DispatchMessageW.argtypes = [ctypes.POINTER(wintypes.MSG)]
    u.DispatchMessageW.restype = ctypes.c_ssize_t

    class WNDCLASSW(ctypes.Structure):
        _fields_ = [
            ("style", wintypes.UINT),
            ("lpfnWndProc", HOTKEY_WNDPROC),
            ("cbClsExtra", ctypes.c_int),
            ("cbWndExtra", ctypes.c_int),
            ("hInstance", wintypes.HINSTANCE),
            ("hIcon", wintypes.HICON),
            ("hCursor", wintypes.HANDLE),
            ("hbrBackground", wintypes.HBRUSH),
            ("lpszMenuName", wintypes.LPCWSTR),
            ("lpszClassName", wintypes.LPCWSTR),
        ]

    def _wndproc(hwnd, msg, wparam, lparam):
        # 热键通知由系统投递，这里只做分发；其余消息交回默认处理。
        if msg == WM_HOTKEY:
            if wparam == HK_UP:
                q.put(("step", HOTKEY_STEP))
            elif wparam == HK_DOWN:
                q.put(("step", -HOTKEY_STEP))
            return 0
        return u.DefWindowProcW(hwnd, msg, wparam, lparam)

    wndproc_ref = HOTKEY_WNDPROC(_wndproc)
    hinst = k.GetModuleHandleW(None)

    # 先把这个线程要用的窗口建起来（只有建了窗口，本线程的消息队列才存在，
    # PostThreadMessage 才不会因为 "queue 尚未创建" 而丢消息）。
    wc = WNDCLASSW()
    wc.lpfnWndProc = wndproc_ref
    wc.hInstance = hinst
    wc.lpszClassName = HOTKEY_CLASS
    if not u.RegisterClassW(ctypes.byref(wc)):
        # 返回值 0 = 注册失败；唯一正常情形是"类已存在"（ERROR_CLASS_ALREADY_EXISTS
        # = 1410）。此时窗口仍可正常创建，继续即可；其它错误记下但不致命。
        if ctypes.get_last_error() != 1410:
            q.put(("hotkeys", False, False))
    hwnd = u.CreateWindowExW(0, HOTKEY_CLASS, HOTKEY_CLASS, 0,
                             0, 0, 0, 0, None, None, hinst, None)

    # 先把线程 id 交出去：即使窗口创建失败，退出时也能用 PostThreadMessage 唤醒。
    q.put(("tid", k.GetCurrentThreadId()))

    if not hwnd:
        q.put(("hotkeys", False, False))
        return

    ok_up = bool(u.RegisterHotKey(hwnd, HK_UP, MOD_CONTROL | MOD_ALT | MOD_NOREPEAT, VK_UP))
    ok_dn = bool(u.RegisterHotKey(hwnd, HK_DOWN, MOD_CONTROL | MOD_ALT | MOD_NOREPEAT, VK_DOWN))
    q.put(("hotkeys", ok_up, ok_dn))

    # 只取本窗口的消息：窗口过程永远在热键线程上执行，不碰 Tk / tray 的窗口消息。
    # 用 GetMessageW(hwnd) 而非 (None)：前者只取投递到本窗口的消息，
    # 后者会取走整个线程队列里的窗口消息。
    msg = wintypes.MSG()
    while True:
        r = u.GetMessageW(ctypes.byref(msg), hwnd, 0, 0)
        if r in (0, -1):
            break
        try:
            u.TranslateMessage(ctypes.byref(msg))
            u.DispatchMessageW(ctypes.byref(msg))
        except Exception:
            # 单条消息处理失败绝不能终止消息循环，否则热键会永久失效。
            pass

    # 退出清理：热键与窗口都要销毁，避免反复启动/退出累积内核对象
    # （窗口类本身注册在进程内，进程退出即释放，无需且不应注销）。
    u.UnregisterHotKey(hwnd, HK_UP)
    u.UnregisterHotKey(hwnd, HK_DOWN)
    try:
        u.DestroyWindow(hwnd)
    except Exception:
        pass


def round_rect(cv, x1, y1, x2, y2, r, **kw):
    pts = [
        x1 + r, y1, x2 - r, y1, x2, y1, x2, y1 + r,
        x2, y2 - r, x2, y2, x2 - r, y2, x1 + r, y2,
        x1, y2, x1, y2 - r, x1, y1 + r, x1, y1,
    ]
    return cv.create_polygon(pts, smooth=True, **kw)


class BrightnessBar:
    def __init__(self, root, wakeup=None):
        self.root = root
        self.S = dpi_scale()

        self.state = load_state()
        self.wakeup = wakeup
        self.q = queue.Queue()
        self.stop_event = threading.Event()
        # _running: 轮询/计时器是否还应自续期。quit() 置 False，
        # 保证 Tk 销毁后不再有任何 after 回调触发（否则抛 TclError 噪音）。
        self._running = True
        self.tid = None
        self._drag = (0, 0)
        self._mode = None          # None / "slider" / "window"：按下时确定，整个拖动过程不变
        self._toast_job = None
        self._lock_hint = False
        self._hover = False
        self._menu_open = False
        self._last_active = time.time()
        self.tray = None

        self.default = read_default_value()
        # 启动即把亮度设成 brightness.txt 里的默认值。这个应用动作由
        # ddc_worker 在后台做（显示器无响应时同步调用曾把整个程序卡死），
        # 主线程先显示默认值，worker 应用完回读实际值再校正。
        # 配合开机自启，这就是"开机亮度"。
        self.value = self.default
        self.locked = bool(self.state.get("locked", True))

        self._commit_job = None
        self.ddc_q = queue.Queue()
        self._build_ui()
        self._start_ddc_worker()
        self._start_hotkeys()
        self._start_tray()
        self._poll()
        self._tick()

    # ---------------- 尺寸工具 ----------------
    def P(self, v):
        return int(round(v * self.S))

    def FP(self, px):
        """字体像素尺寸（Tk 用负数表示像素）。"""
        return -int(round(px * self.S))

    # ---------------- 界面 ----------------
    def _build_ui(self):
        P = self.P
        self.W, self.H = P(D_W), P(D_H)
        self.CY = self.H // 2
        self.SL_X1, self.SL_X2 = P(D_SL_X1), P(D_SL_X2)
        self.X_VALUE = P(D_X_VALUE)
        self.X_MINUS, self.X_PLUS, self.X_CLOSE = P(D_X_MINUS), P(D_X_PLUS), P(D_X_CLOSE)

        r = self.root
        r.overrideredirect(True)
        r.attributes("-topmost", True)
        r.configure(bg=MAGIC)
        try:
            r.wm_attributes("-transparentcolor", MAGIC)
        except Exception:
            pass

        x = self.state.get("x", r.winfo_screenwidth() - self.W - P(28))
        y = self.state.get("y", P(26))
        r.geometry("%dx%d+%d+%d" % (self.W, self.H, x, y))

        self.cv = tk.Canvas(r, width=self.W, height=self.H, bg=MAGIC,
                            highlightthickness=0, bd=0)
        self.cv.pack()

        self._card = round_rect(self.cv, 1, 1, self.W - 1, self.H - 1, P(15),
                                fill=CARD, outline=BORDER, width=1)

        self._draw_sun()
        self._draw_slider()
        self._draw_value()
        self._draw_buttons()
        self._apply_lock_style()
        self._bind_events()

        if not self.state.get("visible", True):
            self.root.withdraw()

    def _draw_sun(self):
        P = self.P
        cx, cy = P(27), self.CY
        for i in range(8):
            a = math.radians(i * 45)
            self.cv.create_line(cx + math.cos(a) * P(8.2), cy + math.sin(a) * P(8.2),
                                cx + math.cos(a) * P(11.2), cy + math.sin(a) * P(11.2),
                                fill=SUN, width=max(1, P(2)), capstyle="round",
                                tags=("sun",))
        self.cv.create_oval(cx - P(5.4), cy - P(5.4), cx + P(5.4), cy + P(5.4),
                            fill=SUN, outline="", tags=("sun",))

    def _draw_slider(self):
        y = self.CY
        round_rect(self.cv, self.SL_X1, y - self.P(3), self.SL_X2, y + self.P(3),
                   self.P(3), fill=TROUGH, outline="", tags=("slider",))
        hx = self._val_to_x(self.value)
        if hx > self.SL_X1 + 1:
            round_rect(self.cv, self.SL_X1, y - self.P(3), hx, y + self.P(3),
                       self.P(3), fill=ACCENT, outline="", tags=("slider", "fill"))
        r = self.P(8)
        self._handle = self.cv.create_oval(
            hx - r, y - r, hx + r, y + r,
            fill="#ffffff", outline=ACCENT, width=max(1, self.P(2.5)),
            tags=("slider", "knob"))

    def _draw_value(self):
        self._value_item = self.cv.create_text(
            self.X_VALUE, self.CY, text=str(self.value), fill=FG,
            font=(MONO, self.FP(21), "bold"), tags=("value",))

    def _draw_buttons(self):
        self._btn_minus = self.cv.create_text(
            self.X_MINUS, self.CY, text="\u2212", fill=MUTED,
            font=("Segoe UI", self.FP(20)), tags=("btn", "b_minus"))
        self._btn_plus = self.cv.create_text(
            self.X_PLUS, self.CY, text="+", fill=MUTED,
            font=("Segoe UI", self.FP(20)), tags=("btn", "b_plus"))
        self._btn_close = self.cv.create_text(
            self.X_CLOSE, self.CY, text="\u00d7", fill=MUTED,
            font=("Segoe UI", self.FP(19)), tags=("btn", "b_close"))

    def _apply_lock_style(self):
        if self.locked:
            self.cv.itemconfigure(self._card, outline=BORDER, width=1)
        else:
            self.cv.itemconfigure(self._card, outline=ACCENT,
                                  width=max(1, self.P(2)))

    # ---------------- 事件 ----------------
    def _bind_events(self):
        cv = self.cv
        for item, act in ((self._btn_minus, "minus"),
                          (self._btn_plus, "plus"),
                          (self._btn_close, "close")):
            cv.tag_bind(item, "<Button-1>", lambda e, a=act: self._click_btn(a))
            cv.tag_bind(item, "<Enter>", lambda e, i=item: self._hover_btn(i, True))
            cv.tag_bind(item, "<Leave>", lambda e, i=item: self._hover_btn(i, False))

        cv.tag_bind("value", "<Double-Button-1>", self._reset_default)

        cv.bind("<Button-1>", self._press)
        cv.bind("<B1-Motion>", self._drag_move)
        cv.bind("<ButtonRelease-1>", self._release)
        cv.bind("<Double-Button-1>", self._dblclick)
        cv.bind("<Button-3>", self._popup)
        cv.bind("<MouseWheel>", self._wheel)
        cv.bind("<Enter>", self._enter)
        cv.bind("<Leave>", self._leave)

        self.menu = tk.Menu(self.root, tearoff=0, font=(FONT, self.FP(12)))
        self.menu.add_command(label="快捷键  Ctrl+Alt+\u2191 / \u2193   调亮 / 调暗")
        self.menu.add_separator()
        self._mi_reset = self.menu.add_command(
            label="恢复默认亮度 (%d)" % self.default,
            command=self._reset_default)
        self.menu.add_command(label="设置默认亮度…",
                              command=self.ask_default)
        self.menu.add_command(label="隐藏到托盘", command=self.hide)
        self.menu.add_separator()
        self.menu.add_command(label="退出", command=self.quit)

    def _hover_btn(self, item, on):
        self.cv.itemconfigure(item, fill=ACCENT if on else MUTED)
        self.cv.configure(cursor="hand2" if on else "")
        self.poke()

    def _click_btn(self, act):
        if act == "minus":
            self.nudge(-HOTKEY_STEP)
        elif act == "plus":
            self.nudge(HOTKEY_STEP)
        else:
            self.hide()
        return "break"

    def _val_to_x(self, v):
        return self.SL_X1 + (self.SL_X2 - self.SL_X1) * (v / 100.0)

    def _x_to_val(self, x):
        span = self.SL_X2 - self.SL_X1
        r = (x - self.SL_X1) / float(span)
        return int(round(max(0.0, min(1.0, r)) * 100))

    def _wheel(self, e):
        self.poke()
        self.set_value(self.value + (HOTKEY_STEP if e.delta > 0 else -HOTKEY_STEP))
        return "break"

    def _enter(self, e):
        self._hover = True
        self.poke()

    def _leave(self, e):
        self._hover = False
        self.poke()

    def _in_slider_zone(self, x, y):
        """滑块的可点区域放宽一些，避免点偏一点就变成拖窗口。"""
        return (self.SL_X1 - self.P(10) <= x <= self.SL_X2 + self.P(10)
                and abs(y - self.CY) <= self.P(15))

    def _press(self, e):
        self.poke()
        self._mode = None
        items = self.cv.find_withtag("current")
        tags = self.cv.gettags(items[0]) if items else ()
        if "btn" in tags or "value" in tags:
            self._mode = "button"                     # 标记一下，释放时不刷新空闲计时
            return                                   # 按钮/数字各自处理，不参与拖动
        if "slider" in tags or self._in_slider_zone(e.x, e.y):
            self._mode = "slider"                    # 按下就定死；之后指针滑出去也仍是在调亮度
            self.set_value(self._x_to_val(e.x))
            return
        self._mode = "window"
        self._drag = (e.x_root - self.root.winfo_x(), e.y_root - self.root.winfo_y())

    def _drag_move(self, e):
        if self._mode == "slider":
            self.set_value(self._x_to_val(e.x))
            return
        if self._mode != "window":
            return
        if self.locked:
            if not self._lock_hint:
                self._lock_hint = True
                self.toast("位置已锁定 · 双击解锁")
            return
        self.root.geometry("+%d+%d" % (e.x_root - self._drag[0],
                                       e.y_root - self._drag[1]))

    def _release(self, e):
        mode, self._mode = self._mode, None
        self._lock_hint = False
        if mode == "button":
            # 按钮/数字上的释放只是点击收尾，不应该刷新空闲计时，
            # 否则每次点完 × 之外的按钮都会把自动隐藏再往后推 10 秒。
            return
        self.poke()

    def _dblclick(self, e):
        items = self.cv.find_withtag("current")
        if items:
            tags = self.cv.gettags(items[0])
            if "slider" in tags or "btn" in tags or "value" in tags:
                return
        self.toggle_lock()

    def _popup(self, e):
        self.poke()
        self._menu_open = True
        try:
            self.menu.tk_popup(e.x_root, e.y_root)
        finally:
            self.menu.grab_release()
            self._menu_open = False
            self.poke()

    def toggle_lock(self):
        self.locked = not self.locked
        self._apply_lock_style()
        self._persist()
        self.toast("位置已锁定" if self.locked else "已解锁 · 可拖动")
        self.poke()

    # ---------------- 亮度 ----------------
    def set_value(self, v):
        v = max(0, min(100, int(v)))
        if v != self.value:
            self.value = v
            self._redraw_slider()
            self.cv.itemconfigure(self._value_item, text=str(v))
            self._schedule_commit()
        if self.tray:
            self.tray.set_value(v)
        self.poke()

    def _schedule_commit(self):
        """去抖：连续拖动/滚轮只在停止 100ms 后把最终值交给 ddc_worker。

        这里必须容忍 after_cancel 失败：回调可能刚好已经执行（此时 _commit
        已把 _commit_job 置 None），再去 cancel 会抛 TclError 并让最后一次
        取值被漏掉。所以用 try/except 包住，无论取消成功与否都重新排一次。
        """
        if self._commit_job is not None:
            try:
                self.root.after_cancel(self._commit_job)
            except Exception:
                pass
        self._commit_job = self.root.after(COMMIT_DELAY_MS, self._commit)

    def _commit(self):
        self._commit_job = None
        self.ddc_q.put(("apply", self.value))

    def _accept_actual(self, requested, actual):
        """ddc_worker 应用完回读的实际亮度。仅当用户此后没再改动时才校正显示。"""
        if actual is None or actual == self.value or self.value != requested:
            return
        self.value = actual
        self._redraw_slider()
        self.cv.itemconfigure(self._value_item, text=str(actual))
        if self.tray:
            self.tray.set_value(actual)

    def nudge(self, d):
        if self.root.state() == "withdrawn":
            self.show()
        self.set_value(self.value + d)

    def _reset_default(self, _e=None):
        self.set_value(self.default)
        self.poke()

    def ask_default(self):
        if self.root.state() == "withdrawn":
            self.show()
        self._menu_open = True
        try:
            v = simpledialog.askinteger(
                "设置默认亮度",
                "默认亮度（0-100）\n开机自启和「恢复默认亮度」都用这个值：",
                parent=self.root, initialvalue=self.value,
                minvalue=0, maxvalue=100)
        finally:
            self._menu_open = False
            self.poke()
        if v is not None:
            self.set_default(v)

    def set_default(self, v):
        self.default = max(0, min(100, int(v)))
        write_default_value(self.default)
        self.menu.entryconfig(self._mi_reset,
                              label="恢复默认亮度 (%d)" % self.default)
        if self.tray:
            self.tray.default_value = self.default
        self.toast("默认亮度已设为 %d" % self.default)
        self.poke()

    def _redraw_slider(self):
        y = self.CY
        hx = self._val_to_x(self.value)
        r = self.P(8)
        self.cv.coords(self._handle, hx - r, y - r, hx + r, y + r)
        self.cv.delete("fill")
        if hx > self.SL_X1 + 1:
            round_rect(self.cv, self.SL_X1, y - self.P(3), hx, y + self.P(3),
                       self.P(3), fill=ACCENT, outline="", tags=("slider", "fill"))
        self.cv.tag_raise("knob")

    # ---------------- 托盘 / 热键 ----------------
    def _start_tray(self):
        self.tray = tray_mod.TrayIcon(
            self.q, default_value=self.default,
            script_path=os.path.join(HERE, "brightness_widget.py"),
            pythonw=pythonw_path())
        self.tray.set_value(self.value)
        if not self.tray.start():
            self.tray = None

    def _start_ddc_worker(self):
        threading.Thread(target=ddc_worker,
                         args=(self.ddc_q, self.q, self.stop_event),
                         daemon=True).start()
        self.ddc_q.put(("apply", self.default))

    def _start_hotkeys(self):
        threading.Thread(target=hotkey_worker,
                         args=(self.q, self.stop_event), daemon=True).start()

    def _poll(self):
        """事件轮询：把后台线程投递到 q 里的请求搬到 Tk 主线程执行。

        【为什么整体包 try/finally】
        这个函数靠 `after` 自续期维持轮询链。只要有一次在重排之前抛了异常
        （队列处理分支里的任何 bug、Tk 对象已销毁、Windows API 临时失败…），
        `self.root.after(...)` 就不会被执行，轮询链当场断裂 —— 界面还显示着，
        但再也不处理任何队列消息，托盘/热键全部失灵，用户看到的就是"卡死"。
        所以这里必须 finally 兜底：无论发生什么，都要把下一次轮询排上。
        单个请求的处理异常也不能连带整批丢弃，每个请求各自隔离。
        """
        if not self._running:
            return
        try:
            if self.wakeup and k32.WaitForSingleObject(self.wakeup, 0) == WAIT_OBJECT_0:
                self.show()
            while True:
                try:
                    item = self.q.get_nowait()
                except queue.Empty:
                    break
                self._dispatch(item)
        except Exception:
            pass
        finally:
            # 退出时（_running=False）不再重排，避免 destroy 后触发 TclError。
            if self._running:
                try:
                    self.root.after(60, self._poll)
                except Exception:
                    pass

    def _dispatch(self, item):
        """单个队列请求的分发；异常在此隔离，不影响轮询链。"""
        try:
            k = item[0]
            if k == "tid":
                self.tid = item[1]
            elif k == "hotkeys":
                if not (item[1] and item[2]):
                    self.toast("全局热键被占用")
            elif k == "step":
                self.nudge(item[1])
            elif k == "value":
                self._accept_actual(item[1], item[2])
            elif k == "toggle_bar":
                self.toggle_bar()
            elif k == "set_default":
                self._reset_default()
            elif k == "ask_default":
                self.ask_default()
            elif k == "toast":
                self.toast(item[1])
            elif k == "quit":
                self.quit()
        except Exception:
            pass

    # ---------------- 空闲自动隐藏 ----------------
    def poke(self):
        self._last_active = time.time()

    def _tick(self):
        if not self._running:
            return
        try:
            if (self.root.state() != "withdrawn" and not self._hover
                    and not self._menu_open
                    and time.time() - self._last_active > IDLE_LIMIT):
                self.hide()
        except Exception:
            pass
        finally:
            if self._running:
                try:
                    self.root.after(1000, self._tick)
                except Exception:
                    pass

    # ---------------- 显隐 ----------------
    def toggle_bar(self):
        if self.root.state() == "withdrawn":
            self.show()
        else:
            self.hide()

    def show(self):
        self.root.deiconify()
        self.root.attributes("-topmost", True)
        self.poke()
        self._persist(visible=True)

    def hide(self):
        self.root.withdraw()
        self._persist(visible=False)

    def toast(self, text):
        if self._toast_job:
            try:
                self.root.after_cancel(self._toast_job)
            except Exception:
                pass
        self.cv.delete("toast")
        if self.root.state() == "withdrawn":
            self.show()
        P = self.P
        w = P(78)
        round_rect(self.cv, self.W / 2 - w, self.CY - P(13),
                   self.W / 2 + w, self.CY + P(13), P(13),
                   fill="#f4f6fa", outline=BORDER, tags=("toast",))
        t = self.cv.create_text(self.W / 2, self.CY, text=text, fill=FG,
                                font=(FONT, self.FP(12), "bold"), tags=("toast",))
        self.cv.tag_raise(t)
        self._toast_job = self.root.after(1600, self._clear_toast)

    def _clear_toast(self):
        self.cv.delete("toast")
        self._toast_job = None

    def _persist(self, visible=None):
        st = {"x": self.root.winfo_x(), "y": self.root.winfo_y(),
              "value": self.value, "locked": self.locked}
        st["visible"] = (visible if visible is not None
                         else self.state.get("visible", True))
        save_state(st)

    # ---------------- 退出 ----------------
    def quit(self):
        # 先停掉自续期，再拆资源：这样 destroy 之后不会有任何 after 回调
        # 触发（也不会因为回调里访问已销毁的控件而抛 TclError）。
        self._running = False
        for job in (self._toast_job, self._commit_job):
            if job is not None:
                try:
                    self.root.after_cancel(job)
                except Exception:
                    pass
        self._toast_job = None
        self._commit_job = None
        try:
            self._persist()
        except Exception:
            pass
        self.stop_event.set()
        if self.tid:
            ctypes.WinDLL("user32", use_last_error=True).PostThreadMessageW(
                self.tid, WM_QUIT, 0, 0)
        if self.tray:
            try:
                self.tray.stop()
            except Exception:
                pass
        try:
            self.root.destroy()
        except Exception:
            pass


def main():
    handles = acquire_single_instance()
    if handles is None:
        return 0
    enable_dpi_awareness()
    root = tk.Tk()
    root.title("亮度")
    BrightnessBar(root, handles[1])
    root.mainloop()
    return 0


if __name__ == "__main__":
    main()
