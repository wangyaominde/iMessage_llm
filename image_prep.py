"""图片预处理：把 iMessage 附件转成 LLM 能吃的格式。

iMessage 里 iPhone 照片是 HEIC，LLM 只认 jpeg/png/gif/webp；全分辨率照片还会超 5MB 上限。
load_image_for_llm() 统一处理：HEIC/不支持格式 → 转 jpeg，过大 → 缩放，返回 (bytes, mime)。
优先用 macOS 自带 sips，失败退回 Pillow + pillow-heif。全程用临时文件且用完即删，不留垃圾。
"""
from __future__ import annotations

import mimetypes
import os
import subprocess
import tempfile
from typing import Optional

SUPPORTED = {'image/jpeg', 'image/png', 'image/gif', 'image/webp'}
MAX_DIM = 1568          # 长边像素上限（Anthropic 建议值，也够 OpenAI vision）
MAX_BYTES = 4_500_000   # 原始字节上限；base64 后 ~1.33x，留在 Anthropic 5MB 之下


def _guess_mime(path: str) -> Optional[str]:
    mime = mimetypes.guess_type(path)[0]
    if mime:
        return mime
    ext = os.path.splitext(path)[1].lower()
    return {
        '.heic': 'image/heic', '.heif': 'image/heif',
        '.jpg': 'image/jpeg', '.jpeg': 'image/jpeg',
        '.png': 'image/png', '.gif': 'image/gif', '.webp': 'image/webp',
    }.get(ext)


def _to_jpeg_bytes(src: str, max_dim: int = MAX_DIM) -> Optional[bytes]:
    """转 jpeg 并缩放到长边 max_dim；返回 jpeg 字节，失败 None。用完删临时文件。"""
    tmp = None
    try:
        with tempfile.NamedTemporaryFile(suffix='.jpg', delete=False) as t:
            tmp = t.name
        cmd = ['sips', '-s', 'format', 'jpeg', '-Z', str(max_dim), src, '--out', tmp]
        r = subprocess.run(cmd, capture_output=True, text=True, timeout=20)
        if r.returncode == 0 and os.path.exists(tmp) and os.path.getsize(tmp) > 0:
            with open(tmp, 'rb') as f:
                return f.read()
    except Exception as e:
        print(f"sips 转码失败: {e}")
    finally:
        if tmp and os.path.exists(tmp):
            try:
                os.remove(tmp)
            except Exception:
                pass
    # 退回 Pillow + pillow-heif
    try:
        from PIL import Image
        try:
            import pillow_heif
            pillow_heif.register_heif_opener()
        except Exception:
            pass
        import io
        img = Image.open(src)
        img.thumbnail((max_dim, max_dim))
        buf = io.BytesIO()
        img.convert('RGB').save(buf, 'JPEG', quality=85)
        return buf.getvalue()
    except Exception as e:
        print(f"Pillow 转码失败: {e}")
        return None


def load_image_for_llm(path: str) -> Optional[tuple[bytes, str]]:
    """返回 (bytes, mime)，mime 保证是 SUPPORTED 之一；无法处理返回 None。"""
    if not path or not os.path.exists(path):
        return None
    try:
        size = os.path.getsize(path)
    except Exception:
        return None

    mime = _guess_mime(path)
    is_heic = mime in ('image/heic', 'image/heif') or path.lower().endswith(('.heic', '.heif'))

    if is_heic or mime not in SUPPORTED:
        data = _to_jpeg_bytes(path)
        return (data, 'image/jpeg') if data else None

    # 已是支持格式：太大就转 jpeg 缩小，否则原样
    if size > MAX_BYTES:
        data = _to_jpeg_bytes(path)
        if data:
            return (data, 'image/jpeg')
        # 缩放失败也别硬发超大图（大概率 400），放弃这张
        return None
    try:
        with open(path, 'rb') as f:
            return (f.read(), mime)
    except Exception:
        return None
