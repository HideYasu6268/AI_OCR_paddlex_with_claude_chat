#!/usr/bin/env python3
"""
siwake.txt フィルタリング&重複排除スクリプト

1. 列11（貸方補助コード）が "1101" の行のみ残す
2. 取引先・借方科目コード・借方税区分 が同じものは先頭1件のみ残す
   （pdf_to_csv.py の DEDUP_KEYS と同じロジック）

使い方:
    python filter_siwake.py siwake.txt [出力ファイル名]

siwake.txt 列インデックス（タブ区切り）:
    6  : 借方科目コード（補助コード: 1112 など）
    9  : 借方税区分（810 など）
    11 : 貸方補助コード（フィルタ対象）
    22 : 取引先（ダブルクォート付きで格納）
"""

import sys
from pathlib import Path

# フィルタ対象の列インデックスと値
FILTER_COL = 11        # 貸方補助コード
FILTER_VAL = "1101"

# 重複排除キーの列インデックス（pdf_to_csv.py の DEDUP_KEYS に対応）
DEDUP_COLS = {
    "取引先":        22,   # 取引先名
    "借方科目コード":   6,   # 借方科目コード（補助コード: 1112 など）
    "借方税区分":      9,   # 借方税区分（810 など）
    "税率区分":       20,  # 軽減税率区分（4=軽減8%, 0=標準10%）
}


def main():
    if len(sys.argv) < 2:
        sys.exit(f"usage: {sys.argv[0]} siwake.txt [output.txt]")

    src = Path(sys.argv[1])
    dst = Path(sys.argv[2]) if len(sys.argv) > 2 else src.with_stem(src.stem + "_filtered")

    # BOM付きUTF-8（Excelで文字化けしない）
    enc = "utf-8-sig"

    total = 0
    filtered = []

    # ---- Step1: 貸方補助コード == "1101" でフィルタ ----
    with open(src, encoding=enc, errors="replace", newline="") as f:
        for line in f:
            line_stripped = line.rstrip("\r\n")
            if not line_stripped:
                continue  # 空行スキップ
            total += 1
            cols = line_stripped.split("\t")
            if len(cols) <= FILTER_COL:
                continue
            if cols[FILTER_COL] == FILTER_VAL:
                filtered.append(cols)

    # ---- Step2: 取引先・借方科目コード・借方税区分 で重複排除 ----
    max_col = max(DEDUP_COLS.values())
    seen = {}
    deduped = []
    for cols in filtered:
        if len(cols) <= max_col:
            continue  # 列数が足りない行はスキップ
        key = tuple(cols[i] for i in DEDUP_COLS.values())
        if key not in seen:
            seen[key] = True
            deduped.append(cols)

    # ---- 出力 ----
    with open(dst, "w", encoding=enc, newline="\r\n") as f:
        for cols in deduped:
            f.write("\t".join(cols) + "\r\n")

    print(
        f"{total} 件 -> フィルタ後 {len(filtered)} 件 -> 重複排除後 {len(deduped)} 件 -> {dst}"
    )


if __name__ == "__main__":
    main()
