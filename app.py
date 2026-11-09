# -*- coding: utf-8 -*-
"""
隐私粉碎机 (Privacy Shredder) —— Web 网页版

把图片 / 视频 / PDF 拖进网页，服务器会在临时目录里调用 exiftool，等价于执行：
    exiftool -all= -overwrite_original "临时文件路径"
清除文件里的元数据（作者、GPS 位置、设备型号、拍摄时间等），
然后在网页上提供「下载清理后的文件」按钮（多个文件会打包成 ZIP）。

本地运行：
    pip install -r requirements.txt
    并且 exiftool 已加入系统 PATH（在终端里能运行 `exiftool -ver`）
    streamlit run app.py

部署到 Streamlit Community Cloud：
    requirements.txt 里写 streamlit，packages.txt 里写 libimage-exiftool-perl，
    云端会自动安装好 exiftool。

隐私说明：上传的文件只在服务器的临时目录里处理，处理完立即删除，不会写入永久存储；
清理结果只保存在本次会话的内存里，点「清空」或关闭页面后即被释放。
"""

import io
import mimetypes
import os
import re
import shutil
import subprocess
import tempfile
import zipfile
from dataclasses import dataclass

import streamlit as st

# ----------------------------------------------------------------------------
# 配置
# ----------------------------------------------------------------------------
APP_TITLE = "隐私粉碎机"
APP_ICON = "🛡️"

# 网页上允许上传的格式（都是 exiftool 能写入的格式）
IMAGE_TYPES = [
    "jpg", "jpeg", "png", "gif", "webp", "heic", "heif", "avif", "tif", "tiff",
    "dng", "cr2", "cr3", "nef", "arw", "orf", "rw2", "raf",
]
VIDEO_TYPES = ["mp4", "mov", "m4v", "3gp", "3g2"]
DOCUMENT_TYPES = ["pdf"]
SUPPORTED_TYPES = IMAGE_TYPES + VIDEO_TYPES + DOCUMENT_TYPES

MAX_TOTAL_MB = 200         # 单次上传的总大小上限，避免云端内存被撑爆（与 .streamlit/config.toml 的 maxUploadSize 保持一致）
EXIFTOOL_TIMEOUT_S = 300   # 处理单个文件的最长时间（大视频可能比较慢）
CLEAN_SUFFIX = "_clean"    # 清理后的文件名后缀：photo.jpg -> photo_clean.jpg

PDF_NOTE = "提示：PDF 的清理结果理论上可被还原（exiftool 的限制）"

# Windows 本地断网运行版的下载地址（Releases 页面永远指向最新版本）
DESKTOP_DOWNLOAD_URL = "https://github.com/KathyInCloud/PrivacyShredder/releases/latest"

# 本地 Windows 运行时不弹出黑色控制台窗口（Linux 上这个值是 0）
CREATE_NO_WINDOW = getattr(subprocess, "CREATE_NO_WINDOW", 0)


# ----------------------------------------------------------------------------
# 后厨逻辑：用 tempfile 接住上传的文件 -> 调用 exiftool -> 打包
# ----------------------------------------------------------------------------
@dataclass
class FileReport:
    """单个文件的处理结果。"""
    name: str
    ok: bool
    message: str = ""  # 成功时是备注（可为空），失败时是错误原因


@dataclass
class ShredResult:
    """一次「开始粉碎」的整体结果。"""
    signature: tuple          # 本次处理的上传文件标识，用来判断结果是否过期
    reports: list             # list[FileReport]
    download_data: bytes      # 要下载的内容（单个文件 或 ZIP），没有成功的文件时为空
    download_name: str
    download_mime: str

    @property
    def ok_count(self):
        return sum(1 for r in self.reports if r.ok)

    @property
    def failed_count(self):
        return len(self.reports) - self.ok_count


def find_exiftool():
    """返回 exiftool 的完整路径；找不到返回 None。
    用完整路径调用，Windows 下即使 exiftool 是 .bat 包装也能正常运行。"""
    return shutil.which("exiftool")


def build_unicode_job(exiftool, file_paths):
    """
    让 exiftool 从标准输入读取一份 UTF-8 编码的参数列表（-@ -），路径不再经过命令行。
    这样即使临时目录路径含中文（例如 Windows 用户名是中文），也不会报 "No matching files"。
    参数内容等价于：exiftool -all= -overwrite_original 文件...
    返回 (命令, 要写入 stdin 的字节)。
    """
    cmd = [
        exiftool,
        "-charset", "exiftool=utf8",
        "-charset", "filename=utf8",
        "-@", "-",
    ]
    arguments = "\n".join(["-all=", "-overwrite_original", *file_paths]) + "\n"
    return cmd, arguments.encode("utf-8")


def safe_basename(name):
    """去掉路径部分，只保留文件名（防止 ../ 之类的奇怪名字）。"""
    base = os.path.basename((name or "").replace("\\", "/")).strip()
    return base or "file"


def safe_temp_name(index, original_name):
    """临时文件名只由序号 + 纯字母数字扩展名组成，不会以 - 开头，也没有任何特殊字符。"""
    ext = re.sub(r"[^a-z0-9]", "", os.path.splitext(original_name)[1].lower())
    return f"{index:04d}.{ext}" if ext else f"{index:04d}"


def clean_output_name(original_name, used):
    """生成清理后的文件名（photo.jpg -> photo_clean.jpg），重名时自动加 (2)、(3)……"""
    stem, ext = os.path.splitext(original_name)
    candidate = f"{stem}{CLEAN_SUFFIX}{ext}"
    counter = 2
    while candidate.lower() in used:
        candidate = f"{stem}{CLEAN_SUFFIX} ({counter}){ext}"
        counter += 1
    used.add(candidate.lower())
    return candidate


def shred_file(exiftool, file_path, display_name):
    """
    对服务器临时目录里的单个文件执行元数据清除。
    返回 (是否成功, 说明文字)：失败时是错误原因，成功时是备注（可能为空字符串）。
    """
    cmd, stdin_data = build_unicode_job(exiftool, [file_path])
    try:
        raw = subprocess.run(
            cmd,
            input=stdin_data,
            capture_output=True,
            timeout=EXIFTOOL_TIMEOUT_S,
            creationflags=CREATE_NO_WINDOW,
        )
    except subprocess.TimeoutExpired:
        return False, "处理超时"
    except OSError as exc:
        return False, f"无法启动 exiftool：{exc}"

    stdout = raw.stdout.decode("utf-8", "replace")
    stderr = raw.stderr.decode("utf-8", "replace")

    if raw.returncode == 0:
        note = PDF_NOTE if "reversible" in stderr.lower() else ""
        return True, note

    # 失败：优先显示 stderr，其次 stdout
    detail = (stderr or stdout).strip()
    first_line = detail.splitlines()[0] if detail else f"exiftool 返回错误码 {raw.returncode}"
    # exiftool 的报错末尾会附带临时文件的完整路径，换成用户看得懂的原始文件名
    for variant in {file_path, file_path.replace("\\", "/")}:
        first_line = first_line.replace(variant, display_name)
    return False, first_line.removeprefix("Error: ").strip()


def shred_uploads(uploads, exiftool, on_progress=None):
    """
    处理用户上传的所有文件：
      1. 用 tempfile 在服务器上建一个临时目录，把每个上传的文件写进去；
      2. 逐个调用 exiftool 清理；
      3. 只有 1 个成功 -> 直接给文件；多个成功 -> 打成 ZIP；
      4. 退出 with 时，临时目录和里面的文件会被自动删除。
    """
    reports = []
    cleaned = []   # [(下载时的文件名, 临时目录里的路径)]
    used_names = set()
    total = len(uploads)

    with tempfile.TemporaryDirectory(prefix="privacy_shredder_") as workdir:
        for index, upload in enumerate(uploads, start=1):
            original = safe_basename(upload.name)
            if on_progress:
                on_progress(index - 1, total, original)

            path = os.path.join(workdir, safe_temp_name(index, original))
            with open(path, "wb") as fh:
                fh.write(upload.getbuffer())

            ok, message = shred_file(exiftool, path, original)
            reports.append(FileReport(original, ok, message))
            if ok:
                cleaned.append((clean_output_name(original, used_names), path))

        if on_progress:
            on_progress(total, total, "")

        data, name, mime = b"", "", "application/octet-stream"
        if len(cleaned) == 1:
            name, path = cleaned[0]
            with open(path, "rb") as fh:
                data = fh.read()
            mime = mimetypes.guess_type(name)[0] or "application/octet-stream"
        elif len(cleaned) > 1:
            buffer = io.BytesIO()
            with zipfile.ZipFile(buffer, "w", zipfile.ZIP_DEFLATED, compresslevel=1) as archive:
                for arcname, path in cleaned:
                    archive.write(path, arcname)
            data, name, mime = buffer.getvalue(), "privacy_shredder_cleaned.zip", "application/zip"

    signature = tuple(upload.file_id for upload in uploads)
    return ShredResult(signature, reports, data, name, mime)



# ----------------------------------------------------------------------------
# 前端界面
# ----------------------------------------------------------------------------
CUSTOM_CSS = """
<style>
/* 去掉 Streamlit 默认的菜单和页脚，让页面更干净 */
#MainMenu, footer { visibility: hidden; }
[data-testid="stHeader"] { background: transparent; }
.block-container { max-width: 720px; padding-top: 3.5rem; padding-bottom: 3rem; }

/* 标题区 */
.ps-hero { text-align: center; margin-bottom: 1.8rem; }
.ps-hero .ps-icon { font-size: 3.2rem; line-height: 1; }
.ps-hero h1 {
    font-size: 2.4rem; font-weight: 800; letter-spacing: .04em;
    margin: .5rem 0 .4rem; padding: 0;
    background: linear-gradient(90deg, #0a84ff, #5e5ce6 55%, #bf5af2);
    -webkit-background-clip: text; background-clip: text;
    -webkit-text-fill-color: transparent;
}
.ps-hero p { color: #8e8e93; font-size: 1rem; margin: 0; }

/* 拖拽上传区 */
[data-testid="stFileUploaderDropzone"] {
    border: 2px dashed #3a3a3c; border-radius: 24px;
    background: #1c1c1e; padding: 2.4rem 1.2rem;
    transition: border-color .2s ease, background .2s ease;
}
[data-testid="stFileUploaderDropzone"]:hover {
    border-color: #0a84ff; background: #1f2630;
}

/* 主按钮 */
.stButton > button[kind="primary"], .stDownloadButton > button[kind="primary"] {
    border: 0; border-radius: 16px; padding: .8rem 1rem;
    font-size: 1.1rem; font-weight: 700; color: #fff;
    background: linear-gradient(90deg, #0a84ff, #5e5ce6);
    transition: transform .1s ease, filter .2s ease;
}
.stButton > button[kind="primary"]:hover:not(:disabled),
.stDownloadButton > button[kind="primary"]:hover:not(:disabled) {
    filter: brightness(1.12); transform: translateY(-1px);
}
.stButton > button[kind="primary"]:disabled { opacity: .35; }

/* 本地版推荐卡片 */
.ps-promo-title { font-weight: 700; font-size: 1rem; margin: 0 0 .3rem; }
.ps-promo-text { color: #aeaeb2; font-size: .92rem; line-height: 1.6; margin: 0; }
[data-testid="stLinkButton"] a { border-radius: 14px; font-weight: 600; }

/* 底部说明 */
.ps-foot { text-align: center; color: #636366; font-size: .85rem; margin-top: 2.2rem; }
</style>
"""


def format_size(num_bytes):
    return f"{num_bytes / (1024 * 1024):.1f} MB"


def render_header():
    st.markdown(CUSTOM_CSS, unsafe_allow_html=True)
    st.markdown(
        f"""
        <div class="ps-hero">
            <div class="ps-icon">{APP_ICON}</div>
            <h1>{APP_TITLE}</h1>
            <p>一键抹掉照片、视频、PDF 里的位置、设备和作者信息</p>
        </div>
        """,
        unsafe_allow_html=True,
    )


def render_desktop_promo():
    """在上传区下方推荐 Windows 本地断网运行版（适合极致隐私 / 超大文件）。"""
    with st.container(border=True):
        st.markdown(
            '<p class="ps-promo-title">💻 想要更彻底的隐私保护？</p>'
            '<p class="ps-promo-text">'
            "如果您追求极致的隐私安全，或者需要处理超过 200MB 的超大文件，"
            "推荐下载我们的 Windows 本地断网运行版——"
            "全程在您自己的电脑上离线处理，文件不会经过任何服务器。"
            "</p>",
            unsafe_allow_html=True,
        )
        st.link_button(
            "⬇️  下载 Windows 本地版",
            DESKTOP_DOWNLOAD_URL,
            width="stretch",
        )


def render_result(result):
    """显示处理结果和下载按钮。"""
    ok, failed = result.ok_count, result.failed_count

    if ok and not failed:
        st.success(f"清理成功！共处理了 {ok} 个文件。", icon="✅")
    elif ok:
        st.warning(f"成功 {ok} 个，失败 {failed} 个。", icon="⚠️")
    else:
        st.error("清理失败，没有文件被成功处理。", icon="❌")

    if ok:
        if ok == 1:
            label = "⬇️  下载清理后的文件"
        else:
            label = f"⬇️  下载清理后的文件（ZIP，共 {ok} 个）"
        st.download_button(
            label,
            data=result.download_data,
            file_name=result.download_name,
            mime=result.download_mime,
            type="primary",
            width="stretch",
            key="download_result",
        )

    for note in sorted({r.message for r in result.reports if r.ok and r.message}):
        st.info(note, icon="ℹ️")

    with st.expander("处理详情", expanded=bool(failed)):
        for report in result.reports:
            if report.ok:
                st.markdown(f"✅ `{report.name}`")
            else:
                st.markdown(f"❌ `{report.name}` —— {report.message}")


def main():
    st.set_page_config(page_title=APP_TITLE, page_icon=APP_ICON, layout="centered")
    render_header()

    exiftool = find_exiftool()
    if exiftool is None:
        st.error(
            "服务器上没有找到 exiftool，无法清理文件。\n\n"
            "云端部署：请确认仓库根目录有 packages.txt，内容为 `libimage-exiftool-perl`。\n\n"
            "本地运行：请先安装 exiftool 并加入系统 PATH。",
            icon="🚫",
        )
        st.stop()

    st.session_state.setdefault("uploader_round", 0)

    uploads = st.file_uploader(
        "上传要清理的文件",
        type=SUPPORTED_TYPES,
        accept_multiple_files=True,
        label_visibility="collapsed",
        key=f"uploader_{st.session_state.uploader_round}",
    )
    st.caption(
        "支持拖拽多个文件：图片（JPG / PNG / HEIC / WebP / RAW 等）、"
        "视频（MP4 / MOV 等）、PDF"
    )

    render_desktop_promo()

    total_bytes = sum(upload.size for upload in uploads)
    too_big = total_bytes > MAX_TOTAL_MB * 1024 * 1024
    if too_big:
        st.error(
            f"本次上传共 {format_size(total_bytes)}，超过了 {MAX_TOTAL_MB} MB 的上限，"
            "请减少文件后再试。",
            icon="📦",
        )

    clicked = st.button(
        "开始粉碎",
        type="primary",
        width="stretch",
        disabled=not uploads or too_big,
    )

    if clicked:
        progress = st.progress(0.0, text="准备中……")

        def on_progress(done, total, name):
            text = f"正在粉碎 {done + 1} / {total}：{name}" if name else "正在打包……"
            progress.progress(done / total, text=text)

        try:
            st.session_state.result = shred_uploads(uploads, exiftool, on_progress)
        finally:
            progress.empty()

    result = st.session_state.get("result")
    if result is not None and result.signature != tuple(u.file_id for u in uploads):
        # 用户换了一批文件，旧结果作废，并释放内存
        st.session_state.pop("result", None)
        result = None

    if result is not None:
        st.divider()
        render_result(result)
        if st.button("清空，重新开始", width="stretch"):
            st.session_state.pop("result", None)
            st.session_state.uploader_round += 1  # 换一个 key，上传区就被清空了
            st.rerun()

    st.markdown(
        '<div class="ps-foot">🔒 文件只在服务器临时目录里处理，处理完立即删除，不会被永久保存</div>',
        unsafe_allow_html=True,
    )


if __name__ == "__main__":
    main()
