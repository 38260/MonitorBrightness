# -*- coding: utf-8 -*-
"""显示器亮度小工具（通过 DDC/CI 协议，VCP 代码 0x10）

用法:
    python set_brightness.py              # 列出所有显示器及当前亮度（只看不改）
    python set_brightness.py 40           # 把所有支持 DDC 的显示器亮度设为 40
    python set_brightness.py 40 0         # 只把第 0 台显示器设为 40
    python set_brightness.py apply        # 读取同目录 brightness.txt 里的数值并应用
"""
import ctypes
import os
import sys
from ctypes import wintypes

user32 = ctypes.WinDLL("user32", use_last_error=True)
dxva2 = ctypes.WinDLL("dxva2", use_last_error=True)

VCP_BRIGHTNESS = 0x10


class PHYSICAL_MONITOR(ctypes.Structure):
    _fields_ = [
        ("hPhysicalMonitor", wintypes.HANDLE),
        ("szPhysicalMonitorDescription", wintypes.WCHAR * 128),
    ]


MONITORENUMPROC = ctypes.WINFUNCTYPE(
    wintypes.BOOL,
    wintypes.HANDLE,
    wintypes.HDC,
    ctypes.POINTER(wintypes.RECT),
    wintypes.LPARAM,
)

user32.EnumDisplayMonitors.argtypes = [
    wintypes.HDC, ctypes.POINTER(wintypes.RECT), MONITORENUMPROC, wintypes.LPARAM
]
user32.EnumDisplayMonitors.restype = wintypes.BOOL

dxva2.GetNumberOfPhysicalMonitorsFromHMONITOR.argtypes = [
    wintypes.HANDLE, ctypes.POINTER(wintypes.DWORD)
]
dxva2.GetNumberOfPhysicalMonitorsFromHMONITOR.restype = wintypes.BOOL

dxva2.GetPhysicalMonitorsFromHMONITOR.argtypes = [
    wintypes.HANDLE, wintypes.DWORD, ctypes.POINTER(PHYSICAL_MONITOR)
]
dxva2.GetPhysicalMonitorsFromHMONITOR.restype = wintypes.BOOL

dxva2.GetVCPFeatureAndVCPFeatureReply.argtypes = [
    wintypes.HANDLE, ctypes.c_ubyte,
    ctypes.POINTER(wintypes.DWORD),
    ctypes.POINTER(wintypes.DWORD),
    ctypes.POINTER(wintypes.DWORD),
]
dxva2.GetVCPFeatureAndVCPFeatureReply.restype = wintypes.BOOL

dxva2.SetVCPFeature.argtypes = [wintypes.HANDLE, ctypes.c_ubyte, wintypes.DWORD]
dxva2.SetVCPFeature.restype = wintypes.BOOL

dxva2.DestroyPhysicalMonitors.argtypes = [
    wintypes.DWORD, ctypes.POINTER(PHYSICAL_MONITOR)
]
dxva2.DestroyPhysicalMonitors.restype = wintypes.BOOL


def list_monitors():
    result = []

    def _cb(hmon, hdc, rect, data):
        count = wintypes.DWORD()
        if dxva2.GetNumberOfPhysicalMonitorsFromHMONITOR(
            hmon, ctypes.byref(count)
        ) and count.value:
            arr = (PHYSICAL_MONITOR * count.value)()
            if dxva2.GetPhysicalMonitorsFromHMONITOR(hmon, count.value, arr):
                result.append((hmon, arr, count.value))
        return True

    user32.EnumDisplayMonitors(None, None, MONITORENUMPROC(_cb), 0)
    return result


def get_brightness(hmon):
    cur = wintypes.DWORD()
    mx = wintypes.DWORD()
    vct = wintypes.DWORD()
    ok = dxva2.GetVCPFeatureAndVCPFeatureReply(
        hmon, VCP_BRIGHTNESS, ctypes.byref(vct), ctypes.byref(cur), ctypes.byref(mx)
    )
    if not ok:
        return None, None
    return cur.value, mx.value


def set_brightness(hmon, value):
    return bool(dxva2.SetVCPFeature(hmon, VCP_BRIGHTNESS, int(value)))


def read_config():
    here = os.path.dirname(os.path.abspath(__file__))
    cfg = os.path.join(here, "brightness.txt")
    if not os.path.exists(cfg):
        return None
    for line in open(cfg, encoding="utf-8-sig"):
        line = line.strip()
        if line and not line.startswith("#"):
            try:
                return int(line)
            except ValueError:
                continue
    return None


def main():
    args = sys.argv[1:]
    target = None
    only_index = None

    if args and args[0].lower() == "apply":
        target = read_config()
        if target is None:
            print("brightness.txt 里没有有效的亮度数值")
            return 1
    elif args:
        try:
            target = int(args[0])
        except ValueError:
            print("亮度必须是数字，例如: python set_brightness.py 40")
            return 1
        if len(args) > 1:
            try:
                only_index = int(args[1])
            except ValueError:
                pass

    mons = list_monitors()
    if not mons:
        print("没有检测到任何显示器")
        return 1

    idx = 0
    changed = 0
    for hmon, arr, cnt in mons:
        for i in range(cnt):
            pm = arr[i]
            desc = pm.szPhysicalMonitorDescription
            cur, mx = get_brightness(pm.hPhysicalMonitor)
            tag = "[%d] %s" % (idx, desc)
            if target is None:
                print("%s  当前亮度: %s  (最大 %s)" % (tag, cur, mx))
            elif cur is None:
                print("%s  不支持调节（跳过）" % tag)
            elif only_index is None or only_index == idx:
                ok = set_brightness(pm.hPhysicalMonitor, target)
                now, _ = get_brightness(pm.hPhysicalMonitor)
                print("%s  设置%s -> 现在亮度: %s" % (tag, "成功" if ok else "失败", now))
                if ok:
                    changed += 1
            else:
                print("%s  跳过 (当前亮度 %s)" % (tag, cur))
            idx += 1

    for hmon, arr, cnt in mons:
        dxva2.DestroyPhysicalMonitors(cnt, arr)

    if target is not None:
        print("完成，共修改 %d 台显示器" % changed)
    return 0


if __name__ == "__main__":
    sys.exit(main())
