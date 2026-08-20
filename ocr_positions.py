# -*- coding: utf-8 -*-
"""
ocr_positions.py

複数の領収書画像をPaddleOCRでバッチ処理し、各テキスト片の位置情報（矩形box）を
保持したJSONを生成する。生成したJSONはclaude.aiのチャットに貼り付けて
フィールド抽出（date/description/total_amount/...）を依頼する用途に使う。
"""

import sys
sys.stdout.reconfigure(encoding='utf-8', errors='replace')

import os
import json
import traceback
from typing import Any, Dict, List

_MODELS_DIR = os.path.join(os.path.dirname(os.path.abspath(__file__)), "models")
# 注意: paddlexが実際に読む変数名は PADDLE_PDX_CACHE_HOME（PADDLE_PDX_HOME ではない）。
os.environ.setdefault("PADDLE_PDX_CACHE_HOME", _MODELS_DIR)

from paddleocr import PaddleOCR
from PIL import Image


class OcrPositionExtractor:

    def __init__(self):
        self.ocr = PaddleOCR(lang="japan")

    def run_ocr_batch(self, image_paths: List[str]) -> Dict[str, Any]:
        images = []
        for i, path in enumerate(image_paths, start=1):
            images.append(self.process_one(i, path))
        return {"images": images}

    def process_one(self, index: int, image_path: str) -> Dict[str, Any]:
        file_name = os.path.basename(image_path)
        width, height = self._image_size(image_path)
        items: List[Dict[str, Any]] = []
        try:
            result = self.ocr.predict(image_path)
            items = self._extract_items(result)
        except Exception as e:
            traceback.print_exc()
            return {
                "index": index,
                "file_name": file_name,
                "width": width,
                "height": height,
                "items": items,
                "error": str(e),
            }
        return {
            "index": index,
            "file_name": file_name,
            "width": width,
            "height": height,
            "items": items,
        }

    def _image_size(self, image_path: str):
        try:
            with Image.open(image_path) as img:
                return img.size  # (width, height)
        except Exception:
            return (None, None)

    def _extract_items(self, ocr_result: Any) -> List[Dict[str, Any]]:
        items: List[Dict[str, Any]] = []
        if not (isinstance(ocr_result, list) and len(ocr_result) > 0):
            return items
        page = ocr_result[0]
        try:
            texts = page["rec_texts"]
            boxes = page["rec_boxes"]
        except Exception:
            try:
                texts = page.rec_texts
                boxes = page.rec_boxes
            except Exception:
                return items
        for i, text in enumerate(texts):
            text = str(text).strip()
            if not text:
                continue
            box = boxes[i] if i < len(boxes) else None
            box_list = [int(v) for v in box] if box is not None else None
            items.append({"id": i, "text": text, "box": box_list})
        return items


def run_ocr_batch(image_paths: List[str]) -> Dict[str, Any]:
    extractor = OcrPositionExtractor()
    return extractor.run_ocr_batch(image_paths)


def save_json(data: Dict[str, Any], path: str) -> None:
    with open(path, "w", encoding="utf-8") as f:
        json.dump(data, f, ensure_ascii=False, indent=2)


if __name__ == "__main__":
    import sys
    target_dir = sys.argv[1] if len(sys.argv) > 1 else "test"
    exts = {".jpg", ".jpeg", ".png", ".bmp", ".tif", ".tiff"}
    paths = sorted(
        os.path.join(target_dir, n) for n in os.listdir(target_dir)
        if os.path.splitext(n)[1].lower() in exts
    )
    print(f"{len(paths)} 枚の画像をOCR処理します...")
    data = run_ocr_batch(paths)
    out_path = os.path.join(target_dir, "ocr_positions.json")
    save_json(data, out_path)
    print(f"✓ 保存完了: {out_path}")
