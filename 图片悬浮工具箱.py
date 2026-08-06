import tkinter as tk
from tkinter import filedialog, ttk, messagebox
from PIL import Image, ImageChops
import ctypes
from ctypes import wintypes
import os
import sys

# ============ Windows API 类型与常量 ============
user32 = ctypes.windll.user32
gdi32 = ctypes.windll.gdi32

WS_EX_LAYERED = 0x00080000
WS_EX_TRANSPARENT = 0x00000020
WS_EX_NOACTIVATE = 0x08000000
GA_ROOT = 2
ULW_ALPHA = 0x00000002
AC_SRC_OVER = 0x00
AC_SRC_ALPHA = 0x01
DIB_RGB_COLORS = 0
BI_RGB = 0

class POINT(ctypes.Structure):
    _fields_ = [("x", wintypes.LONG), ("y", wintypes.LONG)]

class SIZE(ctypes.Structure):
    _fields_ = [("cx", wintypes.LONG), ("cy", wintypes.LONG)]

class BLENDFUNCTION(ctypes.Structure):
    _fields_ = [
        ("BlendOp", ctypes.c_byte),
        ("BlendFlags", ctypes.c_byte),
        ("SourceConstantAlpha", ctypes.c_byte),
        ("AlphaFormat", ctypes.c_byte),
    ]

class BITMAPINFOHEADER(ctypes.Structure):
    _fields_ = [
        ("biSize", wintypes.DWORD),
        ("biWidth", wintypes.LONG),
        ("biHeight", wintypes.LONG),
        ("biPlanes", wintypes.WORD),
        ("biBitCount", wintypes.WORD),
        ("biCompression", wintypes.DWORD),
        ("biSizeImage", wintypes.DWORD),
        ("biXPelsPerMeter", wintypes.LONG),
        ("biYPelsPerMeter", wintypes.LONG),
        ("biClrUsed", wintypes.DWORD),
        ("biClrImportant", wintypes.DWORD),
    ]

class BITMAPINFO(ctypes.Structure):
    _fields_ = [("bmiHeader", BITMAPINFOHEADER)]

if sys.platform != "win32":
    raise RuntimeError("此方案仅支持 Windows")

# ==================== 悬浮图片窗口（完美透明） ====================
class FloatingImage:
    def __init__(self, master=None, image_path=None, title="悬浮图片"):
        self.master = master
        self.image_path = image_path
        self.title = title
        self.pil_image = Image.open(image_path).convert("RGBA")
        self.image_ratio = self.pil_image.width / self.pil_image.height

        self.scale = 100
        self.alpha = 1.0
        self.locked = False
        self.no_capture = False
        self.is_hidden = False
        self.topmost = False        # 新增：默认不置顶

        # DIB 资源
        self._mem_dc = None
        self._hbitmap = None
        self._old_bmp = None
        self._w = 1
        self._h = 1

        # 创建无边框窗口（默认不置顶）
        self.toplevel = tk.Toplevel(master)
        self.toplevel.title(title)
        self.toplevel.overrideredirect(True)
        self.toplevel.attributes("-topmost", False)   # 默认不置顶

        # 获取原生 HWND
        self.toplevel.update_idletasks()
        self.hwnd = user32.GetAncestor(self.toplevel.winfo_id(), GA_ROOT)

        # 设置 WS_EX_LAYERED
        style = user32.GetWindowLongW(self.hwnd, -20)
        user32.SetWindowLongW(self.hwnd, -20, style | WS_EX_LAYERED)

        # 自适应屏幕大小，避免默认过大
        screen_w = self.toplevel.winfo_screenwidth()
        screen_h = self.toplevel.winfo_screenheight()
        max_w, max_h = int(screen_w * 0.9), int(screen_h * 0.9)
        if self.pil_image.width > max_w or self.pil_image.height > max_h:
            scale_x = max_w / self.pil_image.width * 100
            scale_y = max_h / self.pil_image.height * 100
            self.scale = max(1, int(min(scale_x, scale_y)))
        else:
            self.scale = 100

        # 创建预览小图，加速缩放
        max_preview = max(screen_w, screen_h) * 2
        if max(self.pil_image.size) > max_preview:
            ratio = max_preview / max(self.pil_image.size)
            new_size = (int(self.pil_image.width * ratio), int(self.pil_image.height * ratio))
            self.preview_base = self.pil_image.resize(new_size, Image.Resampling.LANCZOS)
        else:
            self.preview_base = self.pil_image

        # 窗口初始位置
        self.x = (screen_w - int(self.pil_image.width * self.scale / 100)) // 2
        self.y = (screen_h - int(self.pil_image.height * self.scale / 100)) // 2

        # 绑定拖动事件
        self.toplevel.bind("<ButtonPress-1>", self.on_press)
        self.toplevel.bind("<B1-Motion>", self.on_move)

        # 首次显示
        self.update_layered(redraw=True)

    # ---------- 置顶控制 ----------
    def set_topmost(self, enabled):
        """设置是否置顶"""
        self.topmost = enabled
        self.toplevel.attributes("-topmost", enabled)

    # ---------- DIB 创建与更新 ----------
    def _create_dib(self, w, h, data_bgra):
        """创建 DIB 并选入内存 DC，data_bgra 为预乘 alpha 的 BGRA 字节串"""
        bmi = BITMAPINFO()
        bmi.bmiHeader.biSize = ctypes.sizeof(BITMAPINFOHEADER)
        bmi.bmiHeader.biWidth = w
        bmi.bmiHeader.biHeight = -h  # top-down，避免垂直翻转
        bmi.bmiHeader.biPlanes = 1
        bmi.bmiHeader.biBitCount = 32
        bmi.bmiHeader.biCompression = BI_RGB
        bmi.bmiHeader.biSizeImage = 0

        bits = ctypes.c_void_p()
        hbitmap = gdi32.CreateDIBSection(None, ctypes.byref(bmi), DIB_RGB_COLORS, ctypes.byref(bits), None, 0)
        if not hbitmap:
            raise ctypes.WinError()

        # 复制图像数据到 DIB 内存
        data_len = w * h * 4
        ctypes.memmove(bits.value, data_bgra, data_len)

        # 创建兼容 DC，并选入 DIB
        if self._mem_dc:
            gdi32.DeleteDC(self._mem_dc)
        self._mem_dc = gdi32.CreateCompatibleDC(None)
        if not self._mem_dc:
            raise ctypes.WinError()
        if self._hbitmap:
            gdi32.DeleteObject(self._hbitmap)
        self._old_bmp = gdi32.SelectObject(self._mem_dc, hbitmap)
        self._hbitmap = hbitmap
        self._w, self._h = w, h

    def _prepare_data(self):
        """生成预乘 alpha 的 BGRA 字节数据"""
        w = max(1, int(self.pil_image.width * self.scale / 100))
        h = max(1, int(self.pil_image.height * self.scale / 100))

        # 使用预览图加速（如果预览图不够大则用原图）
        base = self.preview_base
        if base.width < w or base.height < h:
            base = self.pil_image

        img = base.resize((w, h), Image.Resampling.LANCZOS).convert("RGBA")

        # 预乘 alpha
        r, g, b, a = img.split()
        r = ImageChops.multiply(r, a)
        g = ImageChops.multiply(g, a)
        b = ImageChops.multiply(b, a)
        premul = Image.merge("RGBA", (r, g, b, a))

        # 转为 BGRA 字节序列
        return w, h, premul.tobytes("raw", "BGRA")

    def _apply_layered(self):
        """将当前 DIB 绘制到窗口"""
        if self._mem_dc is None:
            return

        hdc_dst = user32.GetDC(0)
        try:
            p_pt_dst = POINT(self.x, self.y)
            p_size = SIZE(self._w, self._h)
            p_pt_src = POINT(0, 0)
            blend = BLENDFUNCTION(AC_SRC_OVER, 0, int(self.alpha * 255), AC_SRC_ALPHA)

            result = user32.UpdateLayeredWindow(
                self.hwnd,
                hdc_dst,
                ctypes.byref(p_pt_dst),
                ctypes.byref(p_size),
                self._mem_dc,
                ctypes.byref(p_pt_src),
                0,
                ctypes.byref(blend),
                ULW_ALPHA
            )
            if not result:
                raise ctypes.WinError()
        finally:
            user32.ReleaseDC(0, hdc_dst)

    def update_layered(self, redraw=True):
        """刷新窗口。redraw=True 时重新生成图像；False 时仅更新位置/透明度"""
        if redraw:
            w, h, data = self._prepare_data()
            self._create_dib(w, h, data)
        self._apply_layered()

    # ---------- 拖动事件 ----------
    def on_press(self, event):
        if self.locked:
            return
        self._drag_offset_x = event.x_root - self.x
        self._drag_offset_y = event.y_root - self.y

    def on_move(self, event):
        if self.locked:
            return
        self.x = event.x_root - self._drag_offset_x
        self.y = event.y_root - self._drag_offset_y
        self.update_layered(redraw=False)

    # ---------- 对外设置 ----------
    def set_scale(self, scale, fast=False):
        self.scale = scale
        self.update_layered(redraw=True)

    def set_alpha(self, alpha):
        self.alpha = alpha
        self.update_layered(redraw=False)

    # ---------- 隐藏 / 显示 ----------
    def hide(self):
        self.toplevel.withdraw()
        self.is_hidden = True

    def show(self):
        self.toplevel.deiconify()
        self.is_hidden = False
        self.update_layered(redraw=False)

    # ---------- 锁定 / 防录制（不改变置顶状态） ----------
    def lock(self):
        self.locked = True
        style = user32.GetWindowLongW(self.hwnd, -20)
        style |= WS_EX_TRANSPARENT | WS_EX_NOACTIVATE
        user32.SetWindowLongW(self.hwnd, -20, style)

    def unlock(self):
        self.locked = False
        style = user32.GetWindowLongW(self.hwnd, -20)
        style &= ~WS_EX_TRANSPARENT
        style &= ~WS_EX_NOACTIVATE
        user32.SetWindowLongW(self.hwnd, -20, style)

    def set_no_capture(self, enabled):
        self.no_capture = enabled
        WDA_NONE = 0
        WDA_EXCLUDEFROMCAPTURE = 0x11
        value = WDA_EXCLUDEFROMCAPTURE if enabled else WDA_NONE
        if not user32.SetWindowDisplayAffinity(self.hwnd, value):
            raise ctypes.WinError()

    def destroy(self):
        # 释放 GDI 资源
        if self._mem_dc:
            if self._old_bmp:
                gdi32.SelectObject(self._mem_dc, self._old_bmp)
            gdi32.DeleteDC(self._mem_dc)
        if self._hbitmap:
            gdi32.DeleteObject(self._hbitmap)
        self._mem_dc = None
        self._hbitmap = None
        self.toplevel.destroy()


# ==================== 控制面板 ====================
class ControlPanel:
    def __init__(self, root):
        self.root = root
        self.root.title("图片悬浮工具箱——鱿鱼丝")
        self.root.geometry("340x420")  # 增高以容纳复选框
        self.root.resizable(False, False)

        self.image_windows = []

        # 打开/关闭
        frame_btns = tk.Frame(root)
        frame_btns.pack(pady=5)
        tk.Button(frame_btns, text="打开图片", command=self.open_image).pack(side=tk.LEFT, padx=5)
        tk.Button(frame_btns, text="关闭当前", command=self.close_current).pack(side=tk.LEFT, padx=5)

        # 图片选择
        frame_select = tk.Frame(root)
        frame_select.pack(pady=5, fill=tk.X, padx=10)
        tk.Label(frame_select, text="当前图片:").pack(side=tk.LEFT)
        self.combo = ttk.Combobox(frame_select, state="readonly", width=25)
        self.combo.pack(side=tk.LEFT, padx=5)
        self.combo.bind("<<ComboboxSelected>>", self.on_select)

        # 大小调节 (1% - 150%)
        frame_scale = tk.Frame(root)
        frame_scale.pack(pady=5, fill=tk.X, padx=10)
        tk.Label(frame_scale, text="大小 (%)").pack(side=tk.LEFT)
        self.scale_var = tk.DoubleVar(value=100)
        self.scale_slider = tk.Scale(
            frame_scale, from_=1, to=150, orient=tk.HORIZONTAL,
            variable=self.scale_var, command=self.on_scale_change
        )
        self.scale_slider.pack(side=tk.LEFT, fill=tk.X, expand=True, padx=5)

        # 透明度调节 (0.0 - 1.0)
        frame_alpha = tk.Frame(root)
        frame_alpha.pack(pady=5, fill=tk.X, padx=10)
        tk.Label(frame_alpha, text="透明度").pack(side=tk.LEFT)
        self.alpha_var = tk.DoubleVar(value=1.0)
        self.alpha_slider = tk.Scale(
            frame_alpha, from_=0.0, to=1.0, resolution=0.05, orient=tk.HORIZONTAL,
            variable=self.alpha_var, command=self.on_alpha_change
        )
        self.alpha_slider.pack(side=tk.LEFT, fill=tk.X, expand=True, padx=5)

        # 置顶复选框（独立于锁定）
        self.topmost_var = tk.BooleanVar(value=False)
        self.topmost_check = tk.Checkbutton(
            root, text="置顶", variable=self.topmost_var,
            command=self.toggle_topmost, state=tk.DISABLED
        )
        self.topmost_check.pack(pady=2)

        # 锁定 / 解锁 / 防录制 / 隐藏
        frame_lock = tk.Frame(root)
        frame_lock.pack(pady=5)
        self.lock_btn = tk.Button(frame_lock, text="锁定", command=self.lock_current, state=tk.DISABLED)
        self.lock_btn.pack(side=tk.LEFT, padx=5)
        self.unlock_btn = tk.Button(frame_lock, text="解锁", command=self.unlock_current, state=tk.DISABLED)
        self.unlock_btn.pack(side=tk.LEFT, padx=5)
        self.no_capture_btn = tk.Button(frame_lock, text="防录制", command=self.toggle_no_capture, state=tk.DISABLED)
        self.no_capture_btn.pack(side=tk.LEFT, padx=5)
        self.hide_btn = tk.Button(frame_lock, text="隐藏", command=self.toggle_hide, state=tk.DISABLED)
        self.hide_btn.pack(side=tk.LEFT, padx=5)

        root.protocol("WM_DELETE_WINDOW", self.on_close)
        self.refresh_combo()

    def open_image(self):
        file_path = filedialog.askopenfilename(
            title="选择图片",
            filetypes=[("图片文件", "*.png *.jpg *.jpeg *.gif *.bmp"), ("所有文件", "*.*")]
        )
        if not file_path:
            return

        title = os.path.basename(file_path)
        img = FloatingImage(self.root, file_path, title)
        self.image_windows.append(img)

        self.refresh_combo()
        self.combo.current(len(self.image_windows) - 1)
        self.on_select()

    def close_current(self):
        if not self.image_windows or self.combo.current() < 0:
            return
        idx = self.combo.current()
        img = self.image_windows.pop(idx)
        img.destroy()
        self.refresh_combo()
        self.on_select()

    def refresh_combo(self):
        names = [w.title for w in self.image_windows]
        self.combo['values'] = names
        if not names:
            self.combo.set('')
        self._set_controls_state(False)

    def on_select(self, event=None):
        if not self.image_windows or self.combo.current() < 0:
            self._set_controls_state(False)
            return

        img = self.image_windows[self.combo.current()]
        self.scale_var.set(img.scale)
        self.alpha_var.set(img.alpha)
        self.topmost_var.set(img.topmost)   # 同步复选框

        self._set_controls_state(True)

        if img.locked:
            self.lock_btn.config(state=tk.DISABLED)
            self.unlock_btn.config(state=tk.NORMAL)
        else:
            self.lock_btn.config(state=tk.NORMAL)
            self.unlock_btn.config(state=tk.DISABLED)

        if img.no_capture:
            self.no_capture_btn.config(text="防录制(开)", state=tk.NORMAL)
        else:
            self.no_capture_btn.config(text="防录制(关)", state=tk.NORMAL)

        if img.is_hidden:
            self.hide_btn.config(text="显示", state=tk.NORMAL)
        else:
            self.hide_btn.config(text="隐藏", state=tk.NORMAL)

    def _set_controls_state(self, enabled):
        state = tk.NORMAL if enabled else tk.DISABLED
        self.scale_slider.config(state=state)
        self.alpha_slider.config(state=state)
        self.topmost_check.config(state=state)
        self.hide_btn.config(state=state)
        if not enabled:
            self.lock_btn.config(state=tk.DISABLED)
            self.unlock_btn.config(state=tk.DISABLED)
            self.no_capture_btn.config(state=tk.DISABLED)

    # ---------- 置顶切换 ----------
    def toggle_topmost(self):
        if self.combo.current() < 0:
            return
        img = self.image_windows[self.combo.current()]
        img.set_topmost(self.topmost_var.get())

    # ---------- 滑块回调 ----------
    def on_scale_change(self, value):
        if self.combo.current() < 0:
            return
        self.image_windows[self.combo.current()].set_scale(int(float(value)))

    def on_alpha_change(self, value):
        if self.combo.current() < 0:
            return
        self.image_windows[self.combo.current()].set_alpha(float(value))

    def lock_current(self):
        if self.combo.current() < 0:
            return
        self.image_windows[self.combo.current()].lock()
        self.lock_btn.config(state=tk.DISABLED)
        self.unlock_btn.config(state=tk.NORMAL)

    def unlock_current(self):
        if self.combo.current() < 0:
            return
        self.image_windows[self.combo.current()].unlock()
        self.lock_btn.config(state=tk.NORMAL)
        self.unlock_btn.config(state=tk.DISABLED)

    def toggle_no_capture(self):
        if self.combo.current() < 0:
            return
        img = self.image_windows[self.combo.current()]
        try:
            img.set_no_capture(not img.no_capture)
            if img.no_capture:
                self.no_capture_btn.config(text="防录制(开)")
            else:
                self.no_capture_btn.config(text="防录制(关)")
        except Exception as e:
            messagebox.showerror("防录制失败", str(e))

    def toggle_hide(self):
        if self.combo.current() < 0:
            return
        img = self.image_windows[self.combo.current()]
        if img.is_hidden:
            img.show()
            self.hide_btn.config(text="隐藏")
        else:
            img.hide()
            self.hide_btn.config(text="显示")

    def on_close(self):
        for img in self.image_windows:
            img.destroy()
        self.root.destroy()


if __name__ == "__main__":
    root = tk.Tk()
    app = ControlPanel(root)
    root.mainloop()