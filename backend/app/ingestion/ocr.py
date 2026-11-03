"""OCR for scanned pages and standalone images.

Preprocessing matters more than the OCR engine for the "poor image quality"
case in the brief. The pipeline below is deliberately conservative: grayscale,
deskew, adaptive threshold, and upscale small images. Aggressive denoising
tends to eat thin digits in invoice totals, which is the worst possible place
to lose a character.
"""
from __future__ import annotations

import logging

import cv2
import numpy as np
import pytesseract
from PIL import Image, ImageSequence

from ..config import settings
from .base import ExtractedPage, IngestionError
from .layout import PositionedWord, render

log = logging.getLogger(__name__)

# Tesseract page segmentation mode 6 = "assume a single uniform block of
# text". Invoices are mostly block-structured; PSM 3 (auto) tends to split
# line-item tables into unusable fragments.
TESS_CONFIG = "--oem 3 --psm 6"

# Images smaller than this on the long edge get upscaled before OCR, because
# Tesseract's accuracy falls off sharply below ~300 DPI equivalent.
MIN_LONG_EDGE = 1600
# Above this we downscale: beyond ~4000px there is no accuracy gain, only time.
MAX_LONG_EDGE = 4000


def _deskew(gray: np.ndarray) -> np.ndarray:
    """Rotate the page so text lines are horizontal.

    Uses the minimum-area rectangle of all dark pixels. Skew beyond +/-15
    degrees is ignored: on a real scan that usually means the detection latched
    onto a logo or a border, and rotating would make things worse.
    """
    inverted = cv2.bitwise_not(gray)
    coords = cv2.findNonZero(inverted)
    if coords is None:
        return gray
    angle = cv2.minAreaRect(coords)[-1]
    if angle < -45:
        angle = 90 + angle
    if abs(angle) < 0.3 or abs(angle) > 15:
        return gray
    h, w = gray.shape[:2]
    matrix = cv2.getRotationMatrix2D((w // 2, h // 2), angle, 1.0)
    return cv2.warpAffine(
        gray, matrix, (w, h), flags=cv2.INTER_CUBIC, borderMode=cv2.BORDER_REPLICATE
    )


def _resize(gray: np.ndarray) -> np.ndarray:
    h, w = gray.shape[:2]
    long_edge = max(h, w)
    if long_edge == 0:
        return gray
    if long_edge < MIN_LONG_EDGE:
        scale = MIN_LONG_EDGE / long_edge
        interp = cv2.INTER_CUBIC
    elif long_edge > MAX_LONG_EDGE:
        scale = MAX_LONG_EDGE / long_edge
        interp = cv2.INTER_AREA
    else:
        return gray
    return cv2.resize(gray, (int(w * scale), int(h * scale)), interpolation=interp)


def preprocess(image: Image.Image) -> np.ndarray:
    """PIL image -> cleaned binary numpy array ready for Tesseract."""
    rgb = np.array(image.convert("RGB"))
    gray = cv2.cvtColor(rgb, cv2.COLOR_RGB2GRAY)
    gray = _resize(gray)
    gray = _deskew(gray)
    # Adaptive threshold handles uneven lighting from phone photos of invoices,
    # which a global Otsu threshold turns into a half-black page.
    binary = cv2.adaptiveThreshold(
        gray, 255, cv2.ADAPTIVE_THRESH_GAUSSIAN_C, cv2.THRESH_BINARY, 31, 15
    )
    # Remove salt-and-pepper speckle without blurring glyph edges.
    return cv2.medianBlur(binary, 3)


def ocr_image(image: Image.Image) -> tuple[str, float | None]:
    """Return (text, mean_word_confidence). Confidence is 0-100 or None."""
    prepared = preprocess(image)
    try:
        data = pytesseract.image_to_data(
            prepared,
            lang=settings.ocr_lang,
            config=TESS_CONFIG,
            output_type=pytesseract.Output.DICT,
        )
    except pytesseract.TesseractNotFoundError as exc:  # pragma: no cover
        raise IngestionError(
            "Tesseract binary not found on PATH", code="ocr_unavailable"
        ) from exc

    # Rebuild layout from word boxes. Joining tokens with spaces would flatten
    # the line-item table into one unparseable line - see ingestion/layout.py.
    positioned: list[PositionedWord] = []
    confidences: list[float] = []
    count = len(data.get("text", []))
    for i in range(count):
        token = (data["text"][i] or "").strip()
        if not token:
            continue
        positioned.append(
            PositionedWord(
                x0=float(data["left"][i]),
                y0=float(data["top"][i]),
                x1=float(data["left"][i]) + float(data["width"][i]),
                y1=float(data["top"][i]) + float(data["height"][i]),
                text=token,
            )
        )
        try:
            value = float(data["conf"][i])
        except (TypeError, ValueError, KeyError):
            continue
        # Tesseract emits -1 for non-text regions; those are not failures.
        if value >= 0:
            confidences.append(value)

    text = render(positioned)
    mean_conf = sum(confidences) / len(confidences) if confidences else None
    return text, mean_conf


def ingest_image_file(path: str) -> list[ExtractedPage]:
    """OCR a standalone image. Multi-frame TIFFs become multiple pages."""
    try:
        image = Image.open(path)
    except Exception as exc:
        raise IngestionError(f"Cannot open image: {exc}", code="image_unreadable") from exc

    pages: list[ExtractedPage] = []
    try:
        frames = list(ImageSequence.Iterator(image))
    except Exception:
        frames = [image]

    for index, frame in enumerate(frames, start=1):
        try:
            text, conf = ocr_image(frame)
        except IngestionError:
            raise
        except Exception as exc:
            # One bad frame should not sink a 40-page fax TIFF.
            log.warning("OCR failed on frame %s of %s: %s", index, path, exc)
            pages.append(
                ExtractedPage(page_number=index, text="", used_ocr=True, ocr_confidence=None)
            )
            continue
        pages.append(
            ExtractedPage(page_number=index, text=text, used_ocr=True, ocr_confidence=conf)
        )

    if not pages:
        raise IngestionError("Image contained no readable frames", code="image_empty")
    return pages
