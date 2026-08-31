# -*- coding: utf-8 -*-
"""
ocr_positions_profiled.py

ocr_positions.py に処理時間の計測を追加したバージョン。
- PaddleOCR の初期化時間
- 画像1枚ごとの処理時間（OCR推論＋抽出）
- 各画像の解像度（width x height）
- 全体の合計時間と1枚あたりの平均
を標準エラー出力(stderr)に表示する。

計測ログは stderr、通常の進捗は stdout に出るので、
ログだけファイルに残したい場合は次のように実行:
    python ocr_positions_profiled.py test 2> profile.log

JSON出力の内容は元の ocr_positions.py と同一。
"""

import sys
sys.stdout.reconfigure(encoding='utf-8', errors='replace')
sys.stderr.reconfigure(encoding='utf-8', errors='replace')

import os
import json
import time
import traceback
from typing import Any, Dict, List

_MODELS_DIR = os.path.join(os.path.dirname(os.path.abspath(__file__)), "models")
# 注意: paddlexが実際に読む変数名は PADDLE_PDX_CACHE_HOME（PADDLE_PDX_HOME ではない）。
os.environ.setdefault("PADDLE_PDX_CACHE_HOME", _MODELS_DIR)

# onnxruntime-openvino が openvino.dll 等を見つけられるよう、
# openvino パッケージ同梱のDLLディレクトリを検索パスに追加しておく。
try:
    import openvino as _openvino

    _OPENVINO_LIBS_DIR = os.path.join(os.path.dirname(_openvino.__file__), "libs")
    if os.path.isdir(_OPENVINO_LIBS_DIR):
        if hasattr(os, "add_dll_directory"):
            os.add_dll_directory(_OPENVINO_LIBS_DIR)
        os.environ["PATH"] = _OPENVINO_LIBS_DIR + os.pathsep + os.environ.get("PATH", "")
except ImportError:
    pass

from paddleocr import PaddleOCR
from paddlex import create_pipeline
from PIL import Image


def _log(msg: str) -> None:
    """計測ログを stderr に出す。"""
    print(msg, file=sys.stderr, flush=True)


class OcrPositionExtractor:

    def __init__(self):
        # --- 初期化時間の計測 ---
        t0 = time.perf_counter()
        # PaddleOCR() でモデル名・前処理設定を解決させ、その設定を使って
        # onnxruntime(OpenVINO Execution Provider)版のパイプラインを組み直す。
        # det/rec は同一モデル・同一前後処理のままバックエンドだけ変更するため、
        # 認識結果はpaddle+mkldnn版と同一（実測でdiffゼロを確認済み）で、
        # 推論速度のみ約4割短縮される。
        _resolved = PaddleOCR(
            lang="japan",
            use_doc_orientation_classify=True,   # 向き分類オン（ONNX版があるためOpenVINO EPと共存可）
            use_doc_unwarping=False,             # 歪み補正オフ（UVDocはONNX版が無くOpenVINO EPと非対応）
            use_textline_orientation=True,       # 行向き分類オン（ONNX版があるためOpenVINO EPと共存可）
            text_recognition_batch_size=8,       # 文字行をまとめて推論しオーバーヘッド削減
        )
        try:
            self.ocr = create_pipeline(
                config=_resolved._merged_paddlex_config,
                engine="onnxruntime",
                engine_config={
                    "providers": ["OpenVINOExecutionProvider"],
                    "provider_options": {"device_type": "CPU"},
                },
                device="cpu",
            )
        except Exception:
            # OpenVINO EP が使えない環境では、解決済みのpaddle版にフォールバック。
            _log("[警告] OpenVINO EPパイプラインの構築に失敗。paddle+mkldnn版にフォールバックします。")
            _log(traceback.format_exc())
            self.ocr = _resolved
        self.init_seconds = time.perf_counter() - t0
        _log(f"[計測] 初期化(PaddleOCRロード): {self.init_seconds:.2f} 秒")

    def run_ocr_batch(self, image_paths: List[str]) -> Dict[str, Any]:
        images = []
        per_image_seconds: List[float] = []

        batch_t0 = time.perf_counter()
        for i, path in enumerate(image_paths, start=1):
            img_t0 = time.perf_counter()
            result = self.process_one(i, path)
            elapsed = time.perf_counter() - img_t0
            per_image_seconds.append(elapsed)

            w = result.get("width")
            h = result.get("height")
            n_items = len(result.get("items", []))
            size_str = f"{w}x{h}" if w and h else "サイズ不明"
            _log(
                f"[計測] {i:>3}/{len(image_paths)}  "
                f"{result.get('file_name','')}  "
                f"{size_str}  "
                f"items={n_items}  "
                f"{elapsed:.2f} 秒"
            )
            images.append(result)

        total = time.perf_counter() - batch_t0

        # --- サマリ ---
        _log("")
        _log("========== 計測サマリ ==========")
        _log(f"画像枚数            : {len(image_paths)} 枚")
        _log(f"初期化時間          : {self.init_seconds:.2f} 秒（1回のみ）")
        _log(f"OCR処理合計         : {total:.2f} 秒")
        _log(f"初期化＋OCR合計     : {self.init_seconds + total:.2f} 秒")
        if per_image_seconds:
            avg = sum(per_image_seconds) / len(per_image_seconds)
            first = per_image_seconds[0]
            rest = per_image_seconds[1:]
            _log(f"1枚あたり平均       : {avg:.2f} 秒")
            _log(f"1枚目               : {first:.2f} 秒")
            if rest:
                avg_rest = sum(rest) / len(rest)
                _log(f"2枚目以降の平均     : {avg_rest:.2f} 秒（実力値の目安）")
            _log(f"最速 / 最遅         : {min(per_image_seconds):.2f} / {max(per_image_seconds):.2f} 秒")
        _log("================================")
        _log("")

        return {"images": images}

    def process_one(self, index: int, image_path: str) -> Dict[str, Any]:
        file_name = os.path.basename(image_path)
        width, height = self._image_size(image_path)
        items: List[Dict[str, Any]] = []
        try:
            result = list(self.ocr.predict(image_path))
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