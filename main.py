# -*- coding: utf-8 -*-
"""
隐私粉碎机 (Privacy Shredder)

把文件或文件夹拖进窗口，程序会在后台调用本机的 exiftool，等价于执行：
    exiftool -all= -overwrite_original "文件路径"
从而清除文件里的元数据（作者、GPS 位置、设备型号、拍摄时间等）。

拖入文件夹时：程序会遍历里面的所有子文件夹，只挑出 exiftool 能写入的格式
（用 `exiftool -listwf` 获取），每 20 个文件分一批交给 exiftool，并实时显示进度。
不支持的文件（如 .txt）会被跳过，不会修改。

依赖：
    pip install customtkinter tkinterdnd2
    并且 exiftool 已加入系统 PATH（在 PowerShell 里能运行 `exiftool -ver`）。

注意：-overwrite_original 会直接修改原文件，且不会保留备份。
"""

import os
import queue
import re
import shutil
import subprocess
import threading

import customtkinter as ctk
from tkinterdnd2 import DND_FILES, TkinterDnD

# ----------------------------------------------------------------------------
# 外观配置
# ----------------------------------------------------------------------------
APP_TITLE = "隐私粉碎机"
FONT_FAMILY = "Microsoft YaHei UI"

COLOR_BG = "#121212"            # 窗口背景
COLOR_ZONE = "#1c1c1e"          # 拖拽区背景
COLOR_BORDER_IDLE = "#3a3a3c"   # 空闲时边框
COLOR_BORDER_HOVER = "#0a84ff"  # 文件悬停在上方时边框
COLOR_BORDER_BUSY = "#ff9f0a"   # 处理中边框
COLOR_BORDER_OK = "#30d158"     # 成功边框
COLOR_BORDER_WARN = "#ff9f0a"   # 提示边框（例如文件夹里没有可处理的文件）
COLOR_BORDER_FAIL = "#ff453a"   # 失败边框
COLOR_TEXT = "#f2f2f7"
COLOR_SUBTEXT = "#8e8e93"

RESET_DELAY_MS = 4000   # 成功/失败提示显示多久后恢复到初始状态
POLL_INTERVAL_MS = 100  # 界面线程多久检查一次后台线程的消息
BATCH_SIZE = 20         # 处理文件夹时，每批交给 exiftool 的文件数量

# Windows 下不弹出黑色控制台窗口
CREATE_NO_WINDOW = getattr(subprocess, "CREATE_NO_WINDOW", 0)


# ----------------------------------------------------------------------------
# 后端逻辑：调用 exiftool
# ----------------------------------------------------------------------------
PDF_NOTE = "提示：PDF 的清理结果理论上可被还原（exiftool 的限制）"

# 取不到 `exiftool -listwf` 时使用的备用清单（常见的可写入格式）
FALLBACK_EXTENSIONS = frozenset(
    "3g2 3gp 3gpp ai arw avif cr2 cr3 crw dng eps gif heic heif hif icc jp2 jpe "
    "jpeg jpg jxl m4a m4v mov mp4 mpo nef orf pdf png psd qt raf rw2 tif tiff "
    "webp xmp".split()
)
_writable_extensions = None  # 缓存：exiftool 支持写入的扩展名（小写、不带点）


def build_command(file_path):
    """
    构造命令：exiftool -all= -overwrite_original "文件路径"

    这里使用「参数列表」而不是手动拼接字符串。Windows 下 subprocess 会自动
    给含空格的参数加上双引号，所以带空格的路径会被正确地写成 "C:\\My Files\\a.pdf"，
    同时也不会因为路径里的特殊字符（& ^ % 等）引发命令注入问题。
    """
    return ["exiftool", "-all=", "-overwrite_original", file_path]


def build_unicode_job(file_paths):
    """
    给「路径含中文/日文/emoji 等非 ASCII 字符」的文件，以及文件夹的批量处理使用。
    file_paths 是一个路径列表（一次可以放多个文件）。

    Windows 版 exiftool 按系统 ANSI 代码页解析命令行参数，遇到无法表示的字符时
    会报 "No matching files"。所以这种情况改成：让 exiftool 从标准输入读取一份
    UTF-8 编码的参数列表（-@ -）。参数内容与上面的命令完全一致，只是路径不再
    经过命令行，因此中文路径也能正确处理，批量处理时也不会超出命令行长度上限。
    返回 (命令, 要写入 stdin 的字节)。
    """
    cmd = [
        "exiftool",
        "-charset", "exiftool=utf8",
        "-charset", "filename=utf8",
        "-@", "-",
    ]
    arguments = "\n".join(["-all=", "-overwrite_original", *file_paths]) + "\n"
    return cmd, arguments.encode("utf-8")


def _run_exiftool(file_path):
    """根据路径是否纯 ASCII，选择合适的调用方式并执行。"""
    options = {"capture_output": True, "creationflags": CREATE_NO_WINDOW}
    if file_path.isascii():
        return subprocess.run(
            build_command(file_path), text=True, errors="replace", **options
        )

    cmd, stdin_data = build_unicode_job([file_path])
    raw = subprocess.run(cmd, input=stdin_data, **options)
    return subprocess.CompletedProcess(
        raw.args,
        raw.returncode,
        raw.stdout.decode("utf-8", "replace"),
        raw.stderr.decode("utf-8", "replace"),
    )


def shred_file(file_path):
    """
    对单个文件执行元数据清除。
    返回 (是否成功, 说明文字)：
      - 失败时，说明文字是错误原因；
      - 成功时，说明文字是需要提醒用户的备注（没有则为空字符串）。
    """
    if not os.path.isfile(file_path):
        if os.path.isdir(file_path):
            return False, "这是一个文件夹，不是文件"
        return False, "找不到该文件"

    try:
        result = _run_exiftool(file_path)
    except FileNotFoundError:
        return False, "未找到 exiftool，请确认它已加入系统 PATH"
    except OSError as exc:
        return False, f"无法启动 exiftool：{exc}"

    stderr = (result.stderr or "").strip()
    if result.returncode == 0:
        note = PDF_NOTE if "reversible" in stderr.lower() else ""
        return True, note

    # 失败：优先显示 stderr，其次 stdout
    detail = stderr or (result.stdout or "").strip()
    first_line = detail.splitlines()[0] if detail else f"exiftool 返回错误码 {result.returncode}"
    # exiftool 的报错末尾会附带完整文件路径（" - C:/xxx/a.txt"），界面上不需要
    path_suffix = " - " + file_path.replace("\\", "/")
    if first_line.endswith(path_suffix):
        first_line = first_line[: -len(path_suffix)]
    return False, first_line


# ----------------------------------------------------------------------------
# 后端逻辑：文件夹批量处理
# ----------------------------------------------------------------------------
_STAT_FAILED = re.compile(r"(\d+) files weren't updated due to errors")


class ExiftoolError(Exception):
    """exiftool 无法启动（没安装、不在 PATH 等），继续处理下去也没有意义。"""


def get_writable_extensions():
    """向 exiftool 询问它能写入哪些扩展名（`exiftool -listwf`），结果会缓存。"""
    global _writable_extensions
    if _writable_extensions is not None:
        return _writable_extensions

    extensions = set()
    try:
        result = subprocess.run(
            ["exiftool", "-listwf"],
            capture_output=True,
            text=True,
            errors="replace",
            creationflags=CREATE_NO_WINDOW,
            timeout=30,
        )
        if result.returncode == 0:
            lines = result.stdout.splitlines()[1:]  # 第一行是标题
            extensions = {t.lower() for line in lines for t in line.split() if t.isalnum()}
    except (OSError, subprocess.SubprocessError):
        pass

    _writable_extensions = frozenset(extensions) or FALLBACK_EXTENSIONS
    return _writable_extensions


def _is_link(path):
    """符号链接 / 目录联接点：不钻进去，避免死循环，也避免误改文件夹之外的文件。"""
    return os.path.islink(path) or getattr(os.path, "isjunction", lambda p: False)(path)


def collect_files(folder):
    """
    遍历文件夹（含所有子文件夹）。
    返回 (exiftool 能处理的文件列表, 被跳过的文件数)。
    """
    extensions = get_writable_extensions()
    found, skipped = [], 0
    for root, dirs, files in os.walk(folder):
        dirs[:] = sorted(d for d in dirs if not _is_link(os.path.join(root, d)))
        for name in sorted(files):
            full = os.path.join(root, name)
            ext = os.path.splitext(name)[1][1:].lower()
            if ext in extensions and not os.path.islink(full):
                found.append(full)
            else:
                skipped += 1
    return found, skipped


def shred_batch(file_paths):
    """
    一次 exiftool 调用处理多个文件（路径通过 stdin 传入，中文路径、超长列表都没问题）。
    返回 (成功数, 失败说明列表, 是否出现「PDF 可还原」警告)。
    """
    cmd, stdin_data = build_unicode_job(file_paths)
    try:
        raw = subprocess.run(
            cmd, input=stdin_data, capture_output=True, creationflags=CREATE_NO_WINDOW
        )
    except FileNotFoundError:
        raise ExiftoolError("未找到 exiftool，请确认它已加入系统 PATH") from None
    except OSError as exc:
        raise ExiftoolError(f"无法启动 exiftool：{exc}") from None

    stdout = raw.stdout.decode("utf-8", "replace")
    stderr = raw.stderr.decode("utf-8", "replace")
    reversible = "reversible" in stderr.lower()

    if raw.returncode == 0:
        return len(file_paths), [], reversible

    # exiftool 对每个失败的文件输出一行：Error: 原因 - 文件路径
    reasons = {}
    for line in stderr.splitlines():
        if not line.startswith("Error:"):
            continue
        for path in file_paths:
            suffix = " - " + path.replace("\\", "/")
            if line.endswith(suffix) and path not in reasons:
                reasons[path] = line[len("Error:"):-len(suffix)].strip()
                break

    stat = _STAT_FAILED.search(stdout)
    if not stat and not reasons:
        # 没有任何统计信息：exiftool 整体出错，当作整批失败
        detail = (stderr or stdout).strip()
        reason = detail.splitlines()[0] if detail else f"exiftool 返回错误码 {raw.returncode}"
        return 0, [f"{os.path.basename(p)}：{reason}" for p in file_paths], reversible

    failed_total = max(int(stat.group(1)) if stat else 0, len(reasons))
    failed_total = min(failed_total, len(file_paths))
    errors = [f"{os.path.basename(p)}：{reasons[p]}" for p in file_paths if p in reasons]
    if failed_total > len(errors):
        errors.append(f"另有 {failed_total - len(errors)} 个文件处理失败")
    return len(file_paths) - failed_total, errors, reversible


def shorten(text, limit=40):
    """文件名太长时截断，避免撑坏界面。"""
    return text if len(text) <= limit else text[: limit - 1] + "…"


# ----------------------------------------------------------------------------
# 前端界面
# ----------------------------------------------------------------------------
class PrivacyShredderApp(ctk.CTk, TkinterDnD.DnDWrapper):
    """
    customtkinter 的窗口 + tkinterdnd2 的拖拽能力。
    让窗口同时继承 DnDWrapper，并在初始化后加载 tkdnd 库即可。
    """

    def __init__(self):
        super().__init__()
        self.TkdndVersion = TkinterDnD._require(self)

        ctk.set_appearance_mode("dark")

        self.title(APP_TITLE)
        self.geometry("560x440")
        self.minsize(420, 340)
        self.configure(fg_color=COLOR_BG)

        self._busy = False
        self._border_color = COLOR_BORDER_IDLE
        self._reset_job = None
        self._events = queue.Queue()

        self._build_ui()
        self._register_drop_targets(self)
        self._show_idle()

    # ---------------- 界面搭建 ----------------
    def _build_ui(self):
        self.drop_zone = ctk.CTkFrame(
            self,
            fg_color=COLOR_ZONE,
            corner_radius=24,
            border_width=3,
            border_color=COLOR_BORDER_IDLE,
        )
        self.drop_zone.pack(fill="both", expand=True, padx=24, pady=24)

        inner = ctk.CTkFrame(self.drop_zone, fg_color="transparent")
        inner.place(relx=0.5, rely=0.5, anchor="center")

        self.main_label = ctk.CTkLabel(
            inner,
            text="",
            font=ctk.CTkFont(family=FONT_FAMILY, size=34, weight="bold"),
            text_color=COLOR_TEXT,
            wraplength=470,
        )
        self.main_label.pack(padx=20, pady=(0, 10))

        self.sub_label = ctk.CTkLabel(
            inner,
            text="",
            font=ctk.CTkFont(family=FONT_FAMILY, size=14),
            text_color=COLOR_SUBTEXT,
            wraplength=420,
            justify="center",
        )
        self.sub_label.pack(padx=20)

    def _register_drop_targets(self, widget):
        """把窗口以及所有子控件都注册为拖拽目标，保证拖到任何位置都能接收。"""
        widget.drop_target_register(DND_FILES)
        widget.dnd_bind("<<DropEnter>>", self._on_drop_enter)
        widget.dnd_bind("<<DropLeave>>", self._on_drop_leave)
        widget.dnd_bind("<<Drop>>", self._on_drop)
        for child in widget.winfo_children():
            self._register_drop_targets(child)

    # ---------------- 状态显示 ----------------
    def _set_state(self, main_text, sub_text, border_color, main_color=COLOR_TEXT):
        self._border_color = border_color
        self.drop_zone.configure(border_color=border_color)
        self.main_label.configure(text=main_text, text_color=main_color)
        self.sub_label.configure(text=sub_text)

    def _show_idle(self):
        self._reset_job = None
        self._set_state(
            "将文件或文件夹拖到这里",
            "支持 PDF、MP4、图片等格式，可一次拖入多个\n拖入文件夹会处理里面所有的文件（含子文件夹）\n注意：会直接覆盖原文件，不保留备份",
            COLOR_BORDER_IDLE,
        )

    def _cancel_reset(self):
        if self._reset_job is not None:
            self.after_cancel(self._reset_job)
            self._reset_job = None

    # ---------------- 拖拽事件 ----------------
    def _on_drop_enter(self, event):
        if not self._busy:
            self.drop_zone.configure(border_color=COLOR_BORDER_HOVER)
        return event.action

    def _on_drop_leave(self, event):
        self.drop_zone.configure(border_color=self._border_color)
        return event.action

    def _on_drop(self, event):
        self.drop_zone.configure(border_color=self._border_color)
        if self._busy:
            return event.action

        # event.data 里，带空格的路径会被 Tcl 用 {} 包起来，必须用 splitlist 解析
        raw_paths = self.tk.splitlist(event.data)
        paths = [os.path.abspath(os.path.normpath(p)) for p in raw_paths if p]
        paths = list({os.path.normcase(p): p for p in paths}.values())  # 去掉重复项
        if not paths:
            return event.action

        self._start_shredding(paths)
        return event.action

    # ---------------- 后台处理 ----------------
    def _start_shredding(self, paths):
        self._busy = True
        self._cancel_reset()
        self._set_state(
            "正在粉碎...",
            shorten(os.path.basename(paths[0]) or paths[0]),
            COLOR_BORDER_BUSY,
            main_color=COLOR_BORDER_BUSY,
        )

        thread = threading.Thread(target=self._worker, args=(paths,), daemon=True)
        thread.start()
        self.after(POLL_INTERVAL_MS, self._poll_events)

    def _worker(self, paths):
        """
        后台线程：逐个处理拖入的文件 / 文件夹，把进度和结果放进队列，不直接操作界面。
        无论中途发生什么，最后一定会发出 "done" 消息，保证界面不会一直卡在「正在粉碎」。
        """
        summary = {"ok": 0, "failed": 0, "skipped": 0, "errors": [], "notes": []}
        try:
            total = len(paths)
            for index, path in enumerate(paths, start=1):
                if os.path.isdir(path):
                    self._process_folder(path, summary)
                else:
                    suffix = f"（{index}/{total}）" if total > 1 else ""
                    self._events.put(("progress", shorten(os.path.basename(path)) + suffix))
                    self._process_file(path, summary)
        except ExiftoolError as exc:
            summary["failed"] += 1
            summary["errors"].append(str(exc))
        except Exception as exc:  # 兜底：任何意外都如实告诉用户，而不是让界面卡住
            summary["failed"] += 1
            summary["errors"].append(f"发生意外错误：{exc}")
        finally:
            self._events.put(("done", summary))

    @staticmethod
    def _add_note(summary, note):
        if note and note not in summary["notes"]:
            summary["notes"].append(note)

    def _process_file(self, path, summary):
        """处理单个文件（沿用严格的 exiftool -all= -overwrite_original "路径" 命令）。"""
        ok, message = shred_file(path)
        if ok:
            summary["ok"] += 1
            self._add_note(summary, message)
        else:
            summary["failed"] += 1
            summary["errors"].append(f"{os.path.basename(path)}：{message}")

    def _process_folder(self, folder, summary):
        """遍历文件夹，把可处理的文件按 BATCH_SIZE 分批交给 exiftool，并汇报进度。"""
        name = shorten(os.path.basename(folder) or folder)
        self._events.put(("progress", f"正在扫描文件夹：{name}"))
        files, skipped = collect_files(folder)
        summary["skipped"] += skipped

        total = len(files)
        done = 0
        for start in range(0, total, BATCH_SIZE):
            batch = files[start:start + BATCH_SIZE]
            self._events.put(("progress", f"{name}\n已处理 {done} / {total} 个文件"))
            ok, errors, reversible = shred_batch(batch)
            done += len(batch)
            summary["ok"] += ok
            summary["failed"] += len(batch) - ok
            summary["errors"].extend(errors)
            if reversible:
                self._add_note(summary, PDF_NOTE)

    def _poll_events(self):
        """界面线程：定时取出后台线程发来的消息并更新界面。"""
        finished = False
        try:
            while True:
                event = self._events.get_nowait()
                if event[0] == "progress":
                    self.main_label.configure(text="正在粉碎...")
                    self.sub_label.configure(text=event[1])
                elif event[0] == "done":
                    self._show_result(event[1])
                    finished = True
        except queue.Empty:
            pass

        if not finished:
            self.after(POLL_INTERVAL_MS, self._poll_events)

    def _show_result(self, summary):
        """根据处理结果（成功 / 失败 / 跳过的数量）显示最终提示。"""
        self._busy = False
        ok, failed, skipped = summary["ok"], summary["failed"], summary["skipped"]
        errors, notes = summary["errors"], summary["notes"]
        delay = RESET_DELAY_MS

        extra = []
        if skipped:
            extra.append(f"另有 {skipped} 个不支持的文件已跳过（未改动）")
        extra.extend(notes)

        if failed == 0 and ok > 0:
            lines = [f"共处理了 {ok} 个文件"] + extra
            self._set_state(
                "清理成功！",
                "\n".join(lines),
                COLOR_BORDER_OK,
                main_color=COLOR_BORDER_OK,
            )
            if extra:
                delay = RESET_DELAY_MS * 2  # 有额外提示时多停留一会儿
        elif ok == 0 and failed == 0:
            lines = ["文件夹里没有 exiftool 能处理的文件"] + extra
            self._set_state(
                "没有可处理的文件",
                "\n".join(lines),
                COLOR_BORDER_WARN,
                main_color=COLOR_BORDER_WARN,
            )
            delay = RESET_DELAY_MS * 2
        else:
            title = "部分文件失败" if ok else "清理失败"
            lines = [f"成功 {ok} 个，失败 {failed} 个"] if ok else []
            lines.extend(shorten(e, 60) for e in errors[:3])
            if len(errors) > 3:
                lines.append(f"……还有 {len(errors) - 3} 条错误未显示")
            lines.extend(extra)
            self._set_state(title, "\n".join(lines), COLOR_BORDER_FAIL, main_color=COLOR_BORDER_FAIL)
            delay = RESET_DELAY_MS * 2

        self._reset_job = self.after(delay, self._show_idle)


def main():
    if shutil.which("exiftool") is None:
        print("提示：没有在系统 PATH 中找到 exiftool，处理文件时会失败。")
    app = PrivacyShredderApp()
    app.mainloop()


if __name__ == "__main__":
    main()

