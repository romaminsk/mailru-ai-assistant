"""Синтетические PNG и PDF для тестов. Реальных чеков здесь нет."""

from __future__ import annotations

import io

from PIL import Image


def make_png(width: int = 40, height: int = 30, color: str = "white") -> bytes:
    buffer = io.BytesIO()
    Image.new("RGB", (width, height), color).save(buffer, "PNG")
    return buffer.getvalue()


def _escape(text: str) -> str:
    return text.replace("\\", "\\\\").replace("(", "\\(").replace(")", "\\)")


def _stream(data: bytes) -> bytes:
    return f"<< /Length {len(data)} >>\nstream\n".encode() + data + b"\nendstream"


def _assemble(objects: list[bytes]) -> bytes:
    out = bytearray(b"%PDF-1.4\n")
    offsets = []
    for number, body in enumerate(objects, start=1):
        offsets.append(len(out))
        out += f"{number} 0 obj\n".encode() + body + b"\nendobj\n"
    xref_at = len(out)
    out += f"xref\n0 {len(objects) + 1}\n0000000000 65535 f \n".encode()
    for offset in offsets:
        out += f"{offset:010d} 00000 n \n".encode()
    out += f"trailer\n<< /Size {len(objects) + 1} /Root 1 0 R >>\nstartxref\n{xref_at}\n%%EOF\n".encode()
    return bytes(out)


def make_pdf(pages: list[dict]) -> bytes:
    """pages: {'lines': [...]} — встроенный текстовый слой; {'image': (w, h)} — скан (только изображение)."""
    count = len(pages)
    kids = " ".join(f"{4 + index * 3} 0 R" for index in range(count))
    objects: list[bytes] = [
        b"<< /Type /Catalog /Pages 2 0 R >>",
        f"<< /Type /Pages /Kids [{kids}] /Count {count} >>".encode(),
        b"<< /Type /Font /Subtype /Type1 /BaseFont /Helvetica >>",
    ]
    for index, page in enumerate(pages):
        page_no = 4 + index * 3
        content_no, image_no = page_no + 1, page_no + 2
        if "image" in page:
            width, height = page["image"]
            pixels = bytes([240]) * (width * height)
            xobjects = f"/XObject << /Im1 {image_no} 0 R >>"
            content = b"q 400 0 0 500 60 200 cm /Im1 Do Q"
            image_obj = (
                f"<< /Type /XObject /Subtype /Image /Width {width} /Height {height} "
                f"/ColorSpace /DeviceGray /BitsPerComponent 8 /Length {len(pixels)} >>\nstream\n"
            ).encode() + pixels + b"\nendstream"
        else:
            lines = page["lines"]
            xobjects = ""
            body = "BT /F1 14 Tf 50 780 Td 18 TL\n" + "".join(f"({_escape(line)}) Tj T*\n" for line in lines) + "ET"
            content = body.encode("latin-1")
            image_obj = None
        objects.append(
            (
                f"<< /Type /Page /Parent 2 0 R /MediaBox [0 0 595 842] "
                f"/Resources << /Font << /F1 3 0 R >> {xobjects} >> /Contents {content_no} 0 R >>"
            ).encode()
        )
        objects.append(_stream(content))
        objects.append(image_obj if image_obj is not None else b"<< >>")
    return _assemble(objects)


def make_encrypted_pdf() -> bytes:
    from pypdf import PdfWriter

    writer = PdfWriter()
    writer.add_blank_page(width=200, height=200)
    writer.encrypt("secret-test-password")
    buffer = io.BytesIO()
    writer.write(buffer)
    return buffer.getvalue()


def make_rgb_scan_pdf(image) -> bytes:
    """PDF-скан: одна страница с RGB-изображением без текстового слоя."""
    rgb = image.convert("RGB")
    width, height = rgb.size
    pixels = rgb.tobytes()
    content = f"q {width} 0 0 {height} 0 0 cm /Im1 Do Q".encode()
    objects: list[bytes] = [
        b"<< /Type /Catalog /Pages 2 0 R >>",
        b"<< /Type /Pages /Kids [4 0 R] /Count 1 >>",
        b"<< /Type /Font /Subtype /Type1 /BaseFont /Helvetica >>",
        (
            f"<< /Type /Page /Parent 2 0 R /MediaBox [0 0 {width} {height}] "
            "/Resources << /XObject << /Im1 6 0 R >> >> /Contents 5 0 R >>"
        ).encode(),
        _stream(content),
        (
            f"<< /Type /XObject /Subtype /Image /Width {width} /Height {height} "
            f"/ColorSpace /DeviceRGB /BitsPerComponent 8 /Length {len(pixels)} >>\nstream\n"
        ).encode() + pixels + b"\nendstream",
    ]
    return _assemble(objects)
