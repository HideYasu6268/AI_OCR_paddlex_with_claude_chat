# -*- coding: utf-8 -*-
"""
match_and_mark.py

Claudeが返したCSV（Index,date,description,total_amount,taxrate,ship_to_name,
invoice_registered,warning_flag）と、ocr_positions.py が生成した位置情報付きJSONを
突き合わせ、CSVの各フィールド値がどのOCRテキスト片に対応するかを判定して、
該当箇所に矩形マークを付けた画像を出力する。

CSVファイル自体は照合対象としてのみ読み込むが、claude.aiのCSVダウンロード仕様回避のために
埋め込まれた全角ハイフン「－」（空欄プレースホルダー）は空文字列へ置換したうえで元のパスへ
上書き保存する（会計ソフトへは「－」を残さずそのまま取り込めるようにするため）。

照合方式（フィールドごと）:
  - total_amount / date        : 数字のみに正規化した文字列の完全一致
  - invoice_registered         : 値が「〇」（登録番号を確認できた）の場合のみ、OCRテキストを
                                   正規表現 T\\d{13} で走査してマーク対象とする。
                                   「△」（未確認だが推定）「×」はOCR上に探す対象が無いため対象外。
  - description / ship_to_name : 完全一致 → 空白除去後の正規化一致 → RapidFuzzでの
                                   あいまい一致（閾値未満は「未マッチ」として扱う）
  - warning_flag                : マーキング対象外

description は Claude が要約・言い換えすることが多く、OCR原文をそのまま抜き出さない
ケースが多いため、無理に類似度検索で拾おうとせず「未マッチ多め」を許容する方針。
意味検索ではなく文字列の逆引きなので embedding は使わない（RapidFuzzのみで完結）。
"""

import sys
sys.stdout.reconfigure(encoding='utf-8', errors='replace')

import os
import re
import csv
import io
import json
from typing import Any, Dict, List, Optional

from rapidfuzz import fuzz, process
from PIL import Image, ImageDraw, ImageFont

FUZZY_THRESHOLD = 80  # RapidFuzz WRatio は0〜100

# claude.aiのCSVダウンロード機能が空文字列セルを含む行を落とす事象への対処として、
# プロンプト側で「空欄」の代わりに出力させている全角ハイフン。読み込み直後に空文字列へ戻し、
# 会計ソフト取込用の実ファイルにも反映されるよう元のCSVパスへ上書き保存する。
_BLANK_PLACEHOLDER = "－"

FIELD_COLORS = {
    "description":    "#2e7d32",  # 緑
    "total_amount":   "#c62828",  # 赤
    "ship_to_name":   "#1565c0",  # 青
    "invoice_registered": "#ef6c00",  # 橙
    "date":           "#6a1b9a",  # 紫
}

FUZZY_FIELDS = ("description", "ship_to_name")

_IMAGE_EXTS = (".jpg", ".jpeg", ".png", ".bmp", ".tif", ".tiff")


# ──────────────────────────────────────────────
#  CSV / JSON 読み込み
# ──────────────────────────────────────────────
def parse_claude_csv_text(text: str) -> List[Dict[str, str]]:
    """claude.aiのチャットからコピペしたCSVテキストをパースする。
    前後のMarkdownコードフェンス（```csv ... ```）が付いていても剥がす。
    """
    text = text.strip()
    if text.startswith("﻿"):
        text = text.lstrip("﻿")
    lines = text.splitlines()
    if lines and lines[0].strip().startswith("```"):
        lines = lines[1:]
    if lines and lines[-1].strip().startswith("```"):
        lines = lines[:-1]
    text = "\n".join(lines).strip()
    if not text:
        return []
    rows = list(csv.DictReader(io.StringIO(text)))
    for row in rows:
        for key, value in row.items():
            if value is not None and value.strip() == _BLANK_PLACEHOLDER:
                row[key] = ""
    return rows


def save_claude_csv(path: str, rows: List[Dict[str, str]]) -> None:
    """空欄プレースホルダーを置換した内容を、元のCSVと同じ形式（cp932）で上書き保存する。"""
    if not rows:
        return
    fieldnames = list(rows[0].keys())
    with open(path, "w", newline="", encoding="cp932") as f:
        writer = csv.DictWriter(f, fieldnames=fieldnames)
        writer.writeheader()
        writer.writerows(rows)


def load_claude_csv(path: str) -> List[Dict[str, str]]:
    # claude.aiにはCSVをShift-JIS(cp932)で出力するよう指示しているため、それを前提に読む。
    with open(path, "r", newline="", encoding="cp932") as f:
        text = f.read()
    rows = parse_claude_csv_text(text)
    if _BLANK_PLACEHOLDER in text:
        save_claude_csv(path, rows)
    return rows


def load_positions_json(path: str) -> Dict[str, Any]:
    with open(path, "r", encoding="utf-8") as f:
        return json.load(f)


def _find_image_by_index(images_dir: str, index: int) -> Optional[str]:
    """OCR実行時に画像は "{index}.拡張子" にリネーム済みのはずなので、
    JSON内のfile_nameを鵜呑みにせず、まずインデックス番号から直接ファイルを特定する。
    """
    for ext in _IMAGE_EXTS:
        candidate = os.path.join(images_dir, f"{index}{ext}")
        if os.path.exists(candidate):
            return candidate
    return None


def _index_images(data: Dict[str, Any]) -> Dict[int, Dict[str, Any]]:
    result = {}
    for rec in data.get("images", []):
        try:
            result[int(rec["index"])] = rec
        except (KeyError, ValueError, TypeError):
            continue
    return result


def _parse_row_index(raw: str) -> Optional[tuple]:
    """CSVのIndex値をパースする。
    軽減税率混在で分割された行は「3.5」のように小数で返ってくるが、
    対応する画像は整数部（3）のものなので、画像を引くためのキーと
    表示用のラベル（元の文字列）を別に返す。
    """
    raw = (raw or "").strip()
    if not raw:
        return None
    try:
        value = float(raw)
    except ValueError:
        return None
    return int(value), raw


# ──────────────────────────────────────────────
#  正規化・照合ヘルパー
# ──────────────────────────────────────────────
def _digits_only(s: str) -> str:
    if not s:
        return ""
    fw = "０１２３４５６７８９"
    hw = "0123456789"
    s = str(s).translate(str.maketrans(fw, hw))
    return re.sub(r"[^0-9]", "", s)


def _match_amount_field(value: str, items: List[Dict[str, Any]]) -> Optional[Dict[str, Any]]:
    # 完全一致のみ。containment判定だと「2号機」の"2"が"1200"の一部として
    # 誤ヒットするなど、桁数の短い無関係な数字を拾ってしまうため使わない。
    target = _digits_only(value)
    if not target:
        return None
    for item in items:
        if item.get("box") is None:
            continue
        item_digits = _digits_only(item.get("text", ""))
        if item_digits and item_digits == target:
            return {"box": item["box"], "method": "digits", "score": 1.0, "text": item["text"]}
    return None


_DATE_RE = re.compile(r"(\d{4})\s*[年/\-.]\s*(\d{1,2})\s*[月/\-.]\s*(\d{1,2})")


def _normalize_date(value: str) -> Optional[str]:
    if not value:
        return None
    s = str(value).translate(str.maketrans("０１２３４５６７８９", "0123456789"))
    m = _DATE_RE.search(s)
    if not m:
        return None
    y, mo, d = m.groups()
    return f"{int(y):04d}{int(mo):02d}{int(d):02d}"


def _match_date_field(value: str, items: List[Dict[str, Any]]) -> Optional[Dict[str, Any]]:
    # 数字のcontainment判定だと単発の数字（"2"等）がどんな日付にもヒットして
    # しまうため、年/月/日の構造を持つパターンにマッチした場合のみ比較する。
    target = _normalize_date(value)
    if not target:
        return None
    for item in items:
        if item.get("box") is None:
            continue
        item_date = _normalize_date(item.get("text", ""))
        if item_date and item_date == target:
            return {"box": item["box"], "method": "date", "score": 1.0, "text": item["text"]}
    return None


def _match_registration(items: List[Dict[str, Any]]) -> Optional[Dict[str, Any]]:
    for item in items:
        if item.get("box") is None:
            continue
        if re.search(r"T\d{13}", item.get("text", "")):
            return {"box": item["box"], "method": "regex", "score": 1.0, "text": item["text"]}
    return None


def _is_truthy(value: str) -> bool:
    # invoice_registered は 〇/△/× の三値で返ってくる想定。
    # 実際にT番号を探してマークすべきなのは「〇（登録番号を確認できた）」の場合のみで、
    # △（未確認だが推定）や×はOCR上に探す対象が無いため対象外とする。
    v = str(value).strip().lower()
    return v in ("true", "1", "yes", "はい", "〇", "○", "◯")


def _normalize_text(s: str) -> str:
    if not s:
        return ""
    return re.sub(r"[\s　]+", "", str(s).strip())


def _match_text_field(value: str, items: List[Dict[str, Any]],
                       threshold: float = FUZZY_THRESHOLD) -> Optional[Dict[str, Any]]:
    value = (value or "").strip()
    if not value:
        return None
    candidates = [it for it in items if it.get("box") is not None and it.get("text", "").strip()]
    if not candidates:
        return None

    # 第一段階: 完全一致
    for item in candidates:
        if item["text"].strip() == value:
            return {"box": item["box"], "method": "exact", "score": 100.0, "text": item["text"]}

    # 第二段階: 空白を除いた正規化一致
    norm_value = _normalize_text(value)
    for item in candidates:
        if _normalize_text(item["text"]) == norm_value:
            return {"box": item["box"], "method": "normalized", "score": 100.0, "text": item["text"]}

    # 第三段階: RapidFuzzによるあいまい一致（閾値未満は未マッチとして扱う）
    choices = [it["text"] for it in candidates]
    result = process.extractOne(value, choices, scorer=fuzz.WRatio, score_cutoff=threshold)
    if result is None:
        return None
    _, score, idx = result
    best_item = candidates[idx]
    return {"box": best_item["box"], "method": "fuzzy", "score": score, "text": best_item["text"]}


# ──────────────────────────────────────────────
#  1件分のフィールド照合
# ──────────────────────────────────────────────
def match_fields(image_record: Dict[str, Any], csv_row: Dict[str, str]) -> Dict[str, Dict[str, Any]]:
    items = image_record.get("items", [])
    matches: Dict[str, Dict[str, Any]] = {}

    m = _match_amount_field(csv_row.get("total_amount", ""), items)
    if m:
        matches["total_amount"] = m

    m = _match_date_field(csv_row.get("date", ""), items)
    if m:
        matches["date"] = m

    if _is_truthy(csv_row.get("invoice_registered", "")):
        m = _match_registration(items)
        if m:
            matches["invoice_registered"] = m

    for field in FUZZY_FIELDS:
        m = _match_text_field(csv_row.get(field, ""), items)
        if m:
            matches[field] = m

    return matches


# ──────────────────────────────────────────────
#  画像への矩形マーキング
# ──────────────────────────────────────────────
def mark_image(image_path: str, entries: List[Dict[str, Any]], out_path: str) -> None:
    """entries: [{"field":.., "box":.., "label":..}, ...]
    1枚の画像に複数CSV行分のマークをまとめて描画できるよう、
    フィールド名固定のdictではなくリスト形式で受け取る
    （軽減税率混在で1画像に2行が対応するケースをマージするため）。
    """
    img = Image.open(image_path).convert("RGB")
    draw = ImageDraw.Draw(img)
    try:
        font = ImageFont.load_default()
    except Exception:
        font = None

    for entry in entries:
        box = entry.get("box")
        if not box:
            continue
        x1, y1, x2, y2 = box
        color = FIELD_COLORS.get(entry.get("field"), "#000000")
        draw.rectangle([x1, y1, x2, y2], outline=color, width=3)
        label = entry.get("label", entry.get("field", ""))
        label_y = max(0, y1 - 14)
        draw.text((x1, label_y), label, fill=color, font=font)

    os.makedirs(os.path.dirname(out_path), exist_ok=True)
    img.save(out_path)


# ──────────────────────────────────────────────
#  全体オーケストレーション
# ──────────────────────────────────────────────
def run(csv_path: str, json_path: str, images_dir: str, out_dir: str) -> Dict[str, Any]:
    csv_rows = load_claude_csv(csv_path)
    data = load_positions_json(json_path)
    return run_from_rows(csv_rows, data, images_dir, out_dir)


def run_from_rows(csv_rows: List[Dict[str, str]], data: Dict[str, Any],
                   images_dir: str, out_dir: str) -> Dict[str, Any]:
    if not csv_rows:
        raise ValueError(
            "CSVを解析できませんでした。ヘッダー行（Index,date,description,...）を含む"
            "正しいCSV形式か確認してください。")
    if "Index" not in csv_rows[0]:
        raise ValueError(
            f"CSVに 'Index' 列が見つかりません。列名: {list(csv_rows[0].keys())}")

    images_by_index = _index_images(data)

    field_names = ("date", "description", "total_amount", "ship_to_name", "invoice_registered")
    summary = {
        "total_rows": len(csv_rows),
        "processed": 0,
        "skipped_no_image": [],
        "field_match_counts": {f: 0 for f in field_names},
        "unmatched": [],  # [{"index":.., "fields":[...]}]
        "index_filename_mismatch": [],  # [{"index":.., "expected":.., "found":..}]
        "output_files": [],
    }

    os.makedirs(out_dir, exist_ok=True)

    # 軽減税率混在で分割された行（例: 3, 3.5）は同じ画像を指すため、
    # 画像インデックスごとに行をグルーピングしてから1回だけマーク画像を出力する。
    groups: Dict[int, List[Any]] = {}
    for row in csv_rows:
        parsed = _parse_row_index(row.get("Index", ""))
        if parsed is None:
            summary["skipped_no_image"].append(row.get("Index", ""))
            continue
        image_idx, raw_label = parsed
        groups.setdefault(image_idx, []).append((raw_label, row))

    for image_idx, rows_for_image in groups.items():
        image_record = images_by_index.get(image_idx)
        if image_record is None:
            summary["skipped_no_image"].extend(raw_label for raw_label, _ in rows_for_image)
            continue

        # ファイル名とインデックスの突合: リネーム後は "{index}.拡張子" のはずなので、
        # まずインデックスから実ファイルを特定し、JSON記載のfile_nameと一致するか確認する。
        # 食い違う場合は画像フォルダがJSON生成後に入れ替わっている等の可能性があるため、
        # 誤った画像へのマーキングを避けて未処理として報告する。
        image_path = _find_image_by_index(images_dir, image_idx)
        if image_path is None:
            summary["skipped_no_image"].extend(raw_label for raw_label, _ in rows_for_image)
            continue
        if os.path.basename(image_path) != image_record["file_name"]:
            summary["index_filename_mismatch"].append({
                "index": image_idx,
                "expected": image_record["file_name"],
                "found": os.path.basename(image_path),
            })
            summary["skipped_no_image"].extend(raw_label for raw_label, _ in rows_for_image)
            continue

        multi = len(rows_for_image) > 1
        mark_entries: List[Dict[str, Any]] = []

        for raw_label, row in rows_for_image:
            matches = match_fields(image_record, row)

            unmatched_fields = []
            for f in field_names:
                if f == "invoice_registered" and not _is_truthy(row.get(f, "")):
                    continue
                if not (row.get(f, "") or "").strip():
                    continue
                if f in matches:
                    summary["field_match_counts"][f] += 1
                else:
                    unmatched_fields.append(f)
            if unmatched_fields:
                summary["unmatched"].append({"index": raw_label, "fields": unmatched_fields})

            for field, m in matches.items():
                label = f"{field}({raw_label})" if multi else field
                mark_entries.append({"field": field, "box": m["box"], "label": label})

            summary["processed"] += 1

        base, ext = os.path.splitext(image_record["file_name"])
        out_path = os.path.join(out_dir, f"{base}_marked{ext}")
        mark_image(image_path, mark_entries, out_path)
        summary["output_files"].append(out_path)

    return summary


if __name__ == "__main__":
    import sys
    if len(sys.argv) < 5:
        print("使い方: python match_and_mark.py <csv> <json> <images_dir> <out_dir>")
        raise SystemExit(1)
    result = run(sys.argv[1], sys.argv[2], sys.argv[3], sys.argv[4])
    print(json.dumps(result, ensure_ascii=False, indent=2))
