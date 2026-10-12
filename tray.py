# -*- coding: utf-8 -*-
"""系统托盘图标（纯 ctypes 调 Shell_NotifyIcon，无需第三方库）

- 图标上直接显示当前亮度数字
- 鼠标滚轮悬停在图标上 -> 调亮度
      （Windows 不会把 WM_MOUSEWHEEL 发给托盘窗口，所以这里用
        WH_MOUSE_LL 低级鼠标钩子 + Shell_NotifyIconGetRect 命中判定）
- 左键单击 -> 显示/隐藏亮度悬浮条
- 右键 -> 菜单（显示/隐藏、恢复默认、开机自启、退出）

所有事件通过 queue 回传给 tk 主线程处理。
"""
import ctypes
import os
import tempfile
import threading
from ctypes import wintypes

user32 = ctypes.WinDLL("user32", use_last_error=True)
shell32 = ctypes.WinDLL("shell32", use_last_error=True)
kernel32 = ctypes.WinDLL("kernel32", use_last_error=True)

LRESULT = ctypes.c_ssize_t

WM_APP = 0x8000
WM_TRAY = WM_APP + 1
WM_REFRESH = WM_APP + 2
WM_CLOSE = 0x0010
WM_DESTROY = 0x0002
WM_NULL = 0x0000
WM_LBUTTONUP = 0x0202
WM_LBUTTONDBLCLK = 0x0203
WM_RBUTTONUP = 0x0205
WM_MOUSEWHEEL = 0x020A
WH_MOUSE_LL = 14
WM_QUIT = 0x0012

NIM_ADD, NIM_MODIFY, NIM_DELETE = 0, 1, 2
NIF_MESSAGE, NIF_ICON, NIF_TIP = 0x01, 0x02, 0x04
IMAGE_ICON = 1
LR_LOADFROMFILE = 0x0010
SM_CXSMICON = 49

MF_STRING = 0x0000
MF_CHECKED = 0x0008
MF_SEPARATOR = 0x0400
TPM_RETURNCMD = 0x0100
TPM_RIGHTBUTTON = 0x0002

IDM_TOGGLE = 1001
IDM_DEFAULT = 1002
IDM_AUTOSTART = 1003
IDM_QUIT = 1004
IDM_SET_DEFAULT = 1005

CLASS_NAME = "SancBrightnessTrayWnd"
WINDOW_TITLE = "SancBrightnessTray"
ICON_UID = 1

WNDPROC = ctypes.WINFUNCTYPE(LRESULT, wintypes.HWND, wintypes.UINT,
                             wintypes.WPARAM, wintypes.LPARAM)
HOOKPROC = ctypes.WINFUNCTYPE(LRESULT, ctypes.c_int, wintypes.WPARAM, wintypes.LPARAM)


class GUID(ctypes.Structure):
    _fields_ = [("Data1", wintypes.DWORD), ("Data2", wintypes.WORD),
                ("Data3", wintypes.WORD), ("Data4", ctypes.c_byte * 8)]


class NOTIFYICONDATAW(ctypes.Structure):
    _fields_ = [
        ("cbSize", wintypes.DWORD),
        ("hWnd", wintypes.HWND),
        ("uID", wintypes.UINT),
        ("uFlags", wintypes.UINT),
        ("uCallbackMessage", wintypes.UINT),
        ("hIcon", wintypes.HICON),
        ("szTip", wintypes.WCHAR * 128),
        ("dwState", wintypes.DWORD),
        ("dwStateMask", wintypes.DWORD),
        ("szInfo", wintypes.WCHAR * 256),
        ("uTimeoutOrVersion", wintypes.UINT),
        ("szInfoTitle", wintypes.WCHAR * 64),
        ("dwInfoFlags", wintypes.DWORD),
        ("guidItem", GUID),
        ("hBalloonIcon", wintypes.HICON),
    ]


class NOTIFYICONIDENTIFIER(ctypes.Structure):
    _fields_ = [("cbSize", wintypes.DWORD), ("hWnd", wintypes.HWND),
                ("uID", wintypes.UINT), ("guidItem", GUID)]


class MSLLHOOKSTRUCT(ctypes.Structure):
    _fields_ = [("pt", wintypes.POINT), ("mouseData", wintypes.DWORD),
                ("flags", wintypes.DWORD), ("time", wintypes.DWORD),
                ("dwExtraInfo", ctypes.c_void_p)]


class WNDCLASSW(ctypes.Structure):
    _fields_ = [
        ("style", wintypes.UINT),
        ("lpfnWndProc", WNDPROC),
        ("cbClsExtra", ctypes.c_int),
        ("cbWndExtra", ctypes.c_int),
        ("hInstance", wintypes.HINSTANCE),
        ("hIcon", wintypes.HICON),
        ("hCursor", wintypes.HANDLE),
        ("hbrBackground", wintypes.HBRUSH),
        ("lpszMenuName", wintypes.LPCWSTR),
        ("lpszClassName", wintypes.LPCWSTR),
    ]


user32.CreateWindowExW.restype = wintypes.HWND
user32.CreateWindowExW.argtypes = [wintypes.DWORD, wintypes.LPCWSTR, wintypes.LPCWSTR,
                                   wintypes.DWORD, ctypes.c_int, ctypes.c_int,
                                   ctypes.c_int, ctypes.c_int, wintypes.HWND,
                                   wintypes.HMENU, wintypes.HINSTANCE, wintypes.LPVOID]
user32.DefWindowProcW.restype = LRESULT
user32.DefWindowProcW.argtypes = [wintypes.HWND, wintypes.UINT,
                                  wintypes.WPARAM, wintypes.LPARAM]
user32.CallNextHookEx.restype = LRESULT
user32.CallNextHookEx.argtypes = [wintypes.HHOOK, ctypes.c_int,
                                  wintypes.WPARAM, wintypes.LPARAM]
user32.SetWindowsHookExW.restype = wintypes.HHOOK
user32.SetWindowsHookExW.argtypes = [ctypes.c_int, HOOKPROC,
                                     wintypes.HINSTANCE, wintypes.DWORD]
user32.UnhookWindowsHookEx.restype = wintypes.BOOL
user32.UnhookWindowsHookEx.argtypes = [wintypes.HHOOK]
user32.GetMessageW.restype = ctypes.c_int
user32.GetMessageW.argtypes = [ctypes.POINTER(wintypes.MSG), wintypes.HWND,
                               ctypes.c_uint, ctypes.c_uint]
user32.TranslateMessage.argtypes = [ctypes.POINTER(wintypes.MSG)]
user32.DispatchMessageW.argtypes = [ctypes.POINTER(wintypes.MSG)]
user32.LoadImageW.restype = wintypes.HANDLE
user32.LoadImageW.argtypes = [wintypes.HINSTANCE, wintypes.LPCWSTR, wintypes.UINT,
                              ctypes.c_int, ctypes.c_int, wintypes.UINT]
# 这两个不声明 restype 在 64 位下会被截断成 32 位，句柄一截断就出错。
user32.GetSystemMetrics.restype = ctypes.c_int
user32.GetSystemMetrics.argtypes = [ctypes.c_int]
user32.DestroyIcon.restype = wintypes.BOOL
user32.DestroyIcon.argtypes = [wintypes.HICON]
user32.CreatePopupMenu.restype = wintypes.HMENU
user32.CreatePopupMenu.argtypes = []
user32.TrackPopupMenu.restype = wintypes.BOOL
user32.TrackPopupMenu.argtypes = [wintypes.HMENU, wintypes.UINT, ctypes.c_int,
                                  ctypes.c_int, ctypes.c_int, wintypes.HWND,
                                  ctypes.c_void_p]
user32.DestroyMenu.restype = wintypes.BOOL
user32.DestroyMenu.argtypes = [wintypes.HMENU]
user32.AppendMenuW.restype = wintypes.BOOL
user32.AppendMenuW.argtypes = [wintypes.HMENU, wintypes.UINT, ctypes.c_size_t,
                               wintypes.LPCWSTR]
user32.GetCursorPos.restype = wintypes.BOOL
user32.GetCursorPos.argtypes = [ctypes.POINTER(wintypes.POINT)]
user32.SetForegroundWindow.restype = wintypes.BOOL
user32.SetForegroundWindow.argtypes = [wintypes.HWND]
user32.PostMessageW.restype = wintypes.BOOL
user32.PostMessageW.argtypes = [wintypes.HWND, wintypes.UINT,
                                wintypes.WPARAM, wintypes.LPARAM]
user32.PostQuitMessage.restype = None
user32.PostQuitMessage.argtypes = [ctypes.c_int]
user32.PostThreadMessageW.restype = wintypes.BOOL
user32.PostThreadMessageW.argtypes = [wintypes.DWORD, wintypes.UINT,
                                      wintypes.WPARAM, wintypes.LPARAM]
user32.DestroyWindow.restype = wintypes.BOOL
user32.DestroyWindow.argtypes = [wintypes.HWND]
user32.RegisterClassW.restype = wintypes.WORD
user32.RegisterClassW.argtypes = [ctypes.c_void_p]
# 关键：64 位下不声明 restype 会被截断成 32 位，导致 SetWindowsHookEx 失败
kernel32.GetModuleHandleW.restype = wintypes.HANDLE
kernel32.GetModuleHandleW.argtypes = [wintypes.LPCWSTR]
kernel32.GetCurrentThreadId.restype = wintypes.DWORD
shell32.Shell_NotifyIconW.restype = wintypes.BOOL
shell32.Shell_NotifyIconW.argtypes = [wintypes.DWORD, ctypes.POINTER(NOTIFYICONDATAW)]
shell32.Shell_NotifyIconGetRect.restype = ctypes.c_long
shell32.Shell_NotifyIconGetRect.argtypes = [ctypes.POINTER(NOTIFYICONIDENTIFIER),
                                            ctypes.POINTER(wintypes.RECT)]


def make_icon_ico(value, path):
    from PIL import Image, ImageDraw, ImageFont
    size = 64
    img = Image.new("RGBA", (size, size), (0, 0, 0, 0))
    d = ImageDraw.Draw(img)
    d.rounded_rectangle([3, 3, size - 3, size - 3], radius=15,
                        fill=(47, 124, 246, 255))
    txt = str(int(value))
    font = None
    for name in ("segoeuib.ttf", "arialbd.ttf", "arial.ttf"):
        try:
            font = ImageFont.truetype("C:/Windows/Fonts/" + name, 38)
            break
        except Exception:
            continue
    if font is None:
        font = ImageFont.load_default()
    bb = d.textbbox((0, 0), txt, font=font)
    d.text(((size - (bb[2] - bb[0])) / 2 - bb[0],
            (size - (bb[3] - bb[1])) / 2 - bb[1]),
           txt, font=font, fill=(255, 255, 255, 255))
    img.save(path, format="ICO", sizes=[(16, 16), (24, 24), (32, 32), (48, 48), (64, 64)])


def is_autostart_enabled():
    import winreg
    try:
        k = winreg.OpenKey(winreg.HKEY_CURRENT_USER,
                           r"Software\Microsoft\Windows\CurrentVersion\Run")
        try:
            winreg.QueryValueEx(k, "SancBrightnessWidget")
            return True
        except FileNotFoundError:
            return False
        finally:
            winreg.CloseKey(k)
    except Exception:
        return False


def set_autostart(enable, script_path, pythonw):
    import winreg
    try:
        k = winreg.OpenKey(winreg.HKEY_CURRENT_USER,
                           r"Software\Microsoft\Windows\CurrentVersion\Run",
                           0, winreg.KEY_SET_VALUE)
        try:
            if enable:
                exe = os.path.normpath(pythonw)
                scr = os.path.normpath(script_path)
                winreg.SetValueEx(k, "SancBrightnessWidget", 0, winreg.REG_SZ,
                                  '"%s" "%s"' % (exe, scr))
            else:
                try:
                    winreg.DeleteValue(k, "SancBrightnessWidget")
                except FileNotFoundError:
                    pass
        finally:
            winreg.CloseKey(k)
        return True
    except Exception:
        return False


class TrayIcon:
    def __init__(self, q, default_value=45, script_path="", pythonw=""):
        self.q = q
        self.value = default_value
        self.default_value = default_value
        self.script_path = script_path
        self.pythonw = pythonw
        self.hwnd = None
        self.hook = None
        self.hook_tid = None
        self._thread = None
        self._hook_thread = None
        self._ready = threading.Event()
        self._ico_path = os.path.join(tempfile.gettempdir(), "sanc_brightness_tray.ico")
        self._hicon = None
        self._drawn_value = None       # 当前图标已绘制的值，用于去重
        self._wndproc_ref = None
        self._hookproc_ref = None
        self._nid = None

    # ---------- 对外 ----------
    def start(self):
        self._thread = threading.Thread(target=self._run, daemon=True)
        self._thread.start()
        self._ready.wait(timeout=5)
        return self.hwnd is not None

    def set_value(self, value):
        self.value = int(value)
        if self.hwnd:
            user32.PostMessageW(self.hwnd, WM_REFRESH, 0, 0)

    def stop(self):
        if self.hwnd:
            user32.PostMessageW(self.hwnd, WM_CLOSE, 0, 0)
        if self.hook_tid:
            user32.PostThreadMessageW(self.hook_tid, WM_QUIT, 0, 0)

    # ---------- 图标 ----------
    def _update_icon(self, first=False):
        """重绘托盘图标。

        WM_REFRESH 由窗口线程同步处理，而这里要做 Pillow 绘图 + 落盘 ICO
        + LoadImage——都是毫秒级开销。快速滚轮调亮度时会连续投递 WM_REFRESH，
        如果每次都真画一遍，窗口线程会被绘图拖住，托盘右键菜单（模态的
        TrackPopupMenu）响应就会明显迟滞。
        这里用 _drawn_value 记忆「当前图标已经画的是哪个值」，值没变就直接返回，
        把重复绘制完全消掉；同时整段包异常保护，绘图失败也绝不阻断窗口线程。
        """
        if not first and self._drawn_value == self.value and self._nid.hIcon:
            return
        try:
            make_icon_ico(self.value, self._ico_path)
        except Exception:
            return
        try:
            sz = user32.GetSystemMetrics(SM_CXSMICON) or 16
            hicon = user32.LoadImageW(None, self._ico_path, IMAGE_ICON, sz, sz,
                                      LR_LOADFROMFILE)
            if not hicon:
                return
            old = self._hicon
            self._hicon = hicon
            self._nid.hIcon = hicon
            self._nid.szTip = "亮度 %d  ·  滚轮调节  ·  左键显示/隐藏" % self.value
            shell32.Shell_NotifyIconW(NIM_ADD if first else NIM_MODIFY,
                                      ctypes.byref(self._nid))
            self._drawn_value = self.value
            if old:
                user32.DestroyIcon(old)
        except Exception:
            # 图标绘制/更新失败不该影响托盘窗口继续服务消息。
            pass

    # ---------- 菜单 ----------
    def _show_menu(self):
        """弹出托盘菜单。

        TrackPopupMenu 是模态阻塞：它自己跑一个消息循环，直到用户选中或点开
        菜单外才返回。必须保证无论中途抛什么异常，菜单句柄都被 DestroyMenu，
        否则会泄漏 GDI 对象；且异常绝不能逃逸到窗口过程（那会让窗口线程失去
        消息循环，托盘从此无响应）。整个函数以 try/finally 收口。
        """
        hmenu = None
        try:
            hmenu = user32.CreatePopupMenu()
            if not hmenu:
                return
            user32.AppendMenuW(hmenu, MF_STRING, IDM_TOGGLE, "显示 / 隐藏亮度条")
            user32.AppendMenuW(hmenu, MF_STRING, IDM_DEFAULT,
                               "恢复默认亮度 (%d)" % self.default_value)
            user32.AppendMenuW(hmenu, MF_STRING, IDM_SET_DEFAULT,
                               "设置默认亮度…")
            flags = MF_STRING | (MF_CHECKED if is_autostart_enabled() else 0)
            user32.AppendMenuW(hmenu, flags, IDM_AUTOSTART, "开机自动启动")
            user32.AppendMenuW(hmenu, MF_SEPARATOR, 0, None)
            user32.AppendMenuW(hmenu, MF_STRING, IDM_QUIT, "退出")
            pt = wintypes.POINT()
            user32.GetCursorPos(ctypes.byref(pt))
            user32.SetForegroundWindow(self.hwnd)
            cmd = user32.TrackPopupMenu(hmenu, TPM_RETURNCMD | TPM_RIGHTBUTTON,
                                        pt.x, pt.y, 0, self.hwnd, None)
            user32.PostMessageW(self.hwnd, WM_NULL, 0, 0)
            self._handle_menu_cmd(cmd)
        except Exception:
            pass
        finally:
            if hmenu:
                try:
                    user32.DestroyMenu(hmenu)
                except Exception:
                    pass

    def _handle_menu_cmd(self, cmd):
        if cmd == IDM_TOGGLE:
            self.q.put(("toggle_bar",))
        elif cmd == IDM_DEFAULT:
            self.q.put(("set_default",))
        elif cmd == IDM_SET_DEFAULT:
            self.q.put(("ask_default",))
        elif cmd == IDM_AUTOSTART:
            want = not is_autostart_enabled()
            ok = set_autostart(want, self.script_path, self.pythonw)
            self.q.put(("toast", "已开启开机自启" if want and ok
                        else ("已关闭开机自启" if ok else "设置失败")))
        elif cmd == IDM_QUIT:
            self.q.put(("quit",))

    # ---------- 窗口过程 ----------
    def _wndproc(self, hwnd, msg, wparam, lparam):
        """窗口过程。

        【绝不能抛异常】窗口过程一旦把异常抛回给系统，消息循环会失去这个窗口
        的服务能力（托盘图标右键/滚轮/左键全部失灵）。所以整段兜底，
        异常只在内部吞掉，最后始终返回一个合法 LRESULT。
        """
        try:
            if msg == WM_TRAY:
                if lparam == WM_MOUSEWHEEL:
                    delta = ctypes.c_short((wparam >> 16) & 0xFFFF).value
                    self.q.put(("step", 5 if delta > 0 else -5))
                elif lparam in (WM_LBUTTONUP, WM_LBUTTONDBLCLK):
                    self.q.put(("toggle_bar",))
                elif lparam == WM_RBUTTONUP:
                    self._show_menu()
                return 0
            if msg == WM_REFRESH:
                self._update_icon()
                return 0
            if msg == WM_CLOSE:
                user32.DestroyWindow(hwnd)
                return 0
            if msg == WM_DESTROY:
                if self._nid:
                    shell32.Shell_NotifyIconW(NIM_DELETE, ctypes.byref(self._nid))
                user32.PostQuitMessage(0)
                return 0
        except Exception:
            # 吞掉：任何一处失败都不该让托盘窗口停止服务消息。
            pass
        return user32.DefWindowProcW(hwnd, msg, wparam, lparam)

    def _run(self):
        try:
            hinst = kernel32.GetModuleHandleW(None)
            self._wndproc_ref = WNDPROC(self._wndproc)
            wc = WNDCLASSW()
            wc.lpfnWndProc = self._wndproc_ref
            wc.hInstance = hinst
            wc.lpszClassName = CLASS_NAME
            # 0 = 失败；"类已存在"（1410）是重复启动时的正常情形，继续创建窗口。
            if not user32.RegisterClassW(ctypes.byref(wc)):
                if ctypes.get_last_error() != 1410:
                    self._ready.set()
                    return

            self.hwnd = user32.CreateWindowExW(
                0, CLASS_NAME, WINDOW_TITLE, 0, 0, 0, 0, 0,
                None, None, hinst, None)
            if not self.hwnd:
                self._ready.set()
                return

            nid = NOTIFYICONDATAW()
            nid.cbSize = ctypes.sizeof(NOTIFYICONDATAW)
            nid.hWnd = self.hwnd
            nid.uID = ICON_UID
            nid.uFlags = NIF_MESSAGE | NIF_ICON | NIF_TIP
            nid.uCallbackMessage = WM_TRAY
            self._nid = nid
            self._update_icon(first=True)
            self._ready.set()

            # 滚轮钩子另开线程：TrackPopupMenu 会阻塞本线程，钩子会被系统摘掉
            self._hook_thread = threading.Thread(target=self._hook_loop, daemon=True)
            self._hook_thread.start()

            msg = wintypes.MSG()
            while True:
                r = user32.GetMessageW(ctypes.byref(msg), None, 0, 0)
                if r in (0, -1):
                    break
                try:
                    user32.TranslateMessage(ctypes.byref(msg))
                    user32.DispatchMessageW(ctypes.byref(msg))
                except Exception:
                    # 单条消息派发失败不能中断消息循环，否则托盘永久失效。
                    pass
        except Exception:
            self._ready.set()

    # ---------- 滚轮：低级鼠标钩子 ----------
    def _icon_rect(self):
        ident = NOTIFYICONIDENTIFIER()
        ident.cbSize = ctypes.sizeof(NOTIFYICONIDENTIFIER)
        ident.hWnd = self.hwnd
        ident.uID = ICON_UID
        r = wintypes.RECT()
        if shell32.Shell_NotifyIconGetRect(ctypes.byref(ident), ctypes.byref(r)) != 0:
            return None
        if r.right <= r.left or r.bottom <= r.top:
            return None
        return r

    def _mouse_hook(self, ncode, wparam, lparam):
        if ncode >= 0 and wparam == WM_MOUSEWHEEL:
            try:
                ms = ctypes.cast(ctypes.c_void_p(lparam),
                                 ctypes.POINTER(MSLLHOOKSTRUCT)).contents
                r = self._icon_rect()
                if r and (r.left - 2 <= ms.pt.x <= r.right + 2
                          and r.top - 2 <= ms.pt.y <= r.bottom + 2):
                    delta = ctypes.c_short((ms.mouseData >> 16) & 0xFFFF).value
                    self.q.put(("step", 5 if delta > 0 else -5))
                    return 1          # 吞掉，避免任务栏也跟着滚
            except Exception:
                pass
        return user32.CallNextHookEx(None, ncode, wparam, lparam)

    def _hook_loop(self):
        """低级鼠标钩子线程。

        低级钩子回调是在系统投递到本线程的，本线程必须持续跑消息循环，
        否则系统会把钩子判为超时并摘掉。所以这里：
        - 整个循环兜底，单条消息异常不中断；
        - finally 里一定 UnhookWindowsHookEx，避免钩子泄漏（钩子泄漏会拖慢
          全系统的鼠标响应，是很隐蔽的"越用越卡"来源）。
        """
        self.hook_tid = kernel32.GetCurrentThreadId()
        self._hookproc_ref = HOOKPROC(self._mouse_hook)
        hmod = kernel32.GetModuleHandleW(None)
        self.hook = user32.SetWindowsHookExW(WH_MOUSE_LL, self._hookproc_ref,
                                             hmod, 0)
        if not self.hook:
            # 某些环境下传 hMod 会失败，回退到以本线程为模块基址再试一次。
            self.hook = user32.SetWindowsHookExW(WH_MOUSE_LL, self._hookproc_ref,
                                                 hmod, self.hook_tid)
        if not self.hook:
            self.q.put(("toast", "滚轮监听启动失败 (%d)" % ctypes.get_last_error()))
            return
        msg = wintypes.MSG()
        try:
            while True:
                r = user32.GetMessageW(ctypes.byref(msg), None, 0, 0)
                if r in (0, -1):
                    break
                try:
                    user32.TranslateMessage(ctypes.byref(msg))
                    user32.DispatchMessageW(ctypes.byref(msg))
                except Exception:
                    pass
        except Exception:
            pass
        finally:
            if self.hook:
                try:
                    user32.UnhookWindowsHookEx(self.hook)
                except Exception:
                    pass
                self.hook = None
