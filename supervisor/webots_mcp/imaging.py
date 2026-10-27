"""Screenshot helpers: PNG recompression (stdlib only), optional downscale, hashing."""
import hashlib
import struct
import zlib

PNG_SIG = b"\x89PNG\r\n\x1a\n"


def md5_file(path, chunk=1 << 20):
    h = hashlib.md5()
    with open(path, "rb") as f:
        while True:
            block = f.read(chunk)
            if not block:
                return h.hexdigest()
            h.update(block)


def _chunk(kind, data):
    body = kind + data
    return struct.pack(">I", len(data)) + body + struct.pack(">I", zlib.crc32(body) & 0xFFFFFFFF)


def recompress_png(path, level=9):
    """Rewrite the PNG's IDAT stream with zlib `level`; pixels are untouched.

    Webots writes screenshots with (almost) no deflate compression (~7.9 MB for
    1955x1344).  Returns the new size, or None if the file is not a PNG this
    function understands (left unchanged).
    """
    with open(path, "rb") as f:
        data = f.read()
    if not data.startswith(PNG_SIG):
        return None
    pos, chunks, idat = len(PNG_SIG), [], []
    while pos + 8 <= len(data):
        length, kind = struct.unpack(">I4s", data[pos:pos + 8])
        payload = data[pos + 8:pos + 8 + length]
        pos += 12 + length
        if kind == b"IDAT":
            idat.append(payload)
            if not any(c[0] == b"IDAT" for c in chunks):
                chunks.append((b"IDAT", None))
        else:
            chunks.append((kind, payload))
        if kind == b"IEND":
            break
    if not idat or not chunks or chunks[-1][0] != b"IEND":
        return None
    try:
        raw = zlib.decompress(b"".join(idat))
    except zlib.error:
        return None
    packed = zlib.compress(raw, max(0, min(9, int(level))))
    out = bytearray(PNG_SIG)
    for kind, payload in chunks:
        out += _chunk(kind, packed if kind == b"IDAT" else payload)
    if len(out) >= len(data):
        return len(data)  # already compact: keep the original
    with open(path, "wb") as f:
        f.write(out)
    return len(out)


def downscale(path, scale, jpeg_quality=90):
    """Resize in place with Pillow when installed. Returns (done, size_or_reason)."""
    try:
        from PIL import Image  # noqa: WPS433 (optional dependency)
    except ImportError:
        return False, "Pillow is not installed in the Webots Python environment"
    with Image.open(path) as im:
        w, h = im.size
        new = (max(1, int(round(w * scale))), max(1, int(round(h * scale))))
        resized = im.convert("RGB").resize(new, Image.LANCZOS)
        if str(path).lower().endswith(".png"):
            resized.save(path, "PNG", optimize=True)
        else:
            resized.save(path, "JPEG", quality=jpeg_quality)
    return True, new
