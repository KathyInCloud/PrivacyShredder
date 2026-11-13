# Privacy Shredder (隐私粉碎机)
这是一个极简的隐私清理工具，一键抹除照片、视频、PDF 中的位置、设备和作者等敏感元数据。

为了满足不同用户的需求，本项目提供三种使用方式：

## 方式一：轻量极速网页版（秒开免休眠，100% 浏览器本地处理图片）
适合需要快速处理 JPG / PNG / WebP 照片的用户。文件仅在您的浏览器内存中本地处理，不经过任何服务器。
👉 **[点击这里访问 Hugging Face 在线版](https://huggingface.co/spaces/KathyInCloud/PrivacyShredder)**

## 方式二：全格式云端网页版（支持视频与 PDF）
适合需要在线处理视频（MP4/MOV）或 PDF 文档的用户。
👉 **[点击这里访问 Streamlit 在线版](https://privacyshredder.streamlit.app/)**
*(注：文件仅在云端内存中临时处理，处理完毕后系统会立即自动销毁，绝不保存。)*

## 方式三：Windows 本地完整版（极致安全，断网可用）
适合追求绝对隐私、处理极大体积文件，或在无网络环境下使用的用户。文件全程不会离开您的硬盘。
👉 **[点击这里前往 Releases 页面下载 .exe 软件](https://github.com/KathyInCloud/PrivacyShredder/releases/latest)**

---
**开发者注：** 本工具桌面版与全格式版底层核心逻辑基于 `exiftool` 构建；轻量极速版基于纯前端 Canvas 重绘构建。
