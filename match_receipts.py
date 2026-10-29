#!/usr/bin/env python3
"""
receipts_accounting.csv と siwake_filtered.txt を突合し、
マッチしたレコードを会計ソフト取込用仕訳データ（siwake_matched.txt）に出力するスクリプト。
マッチしたレコードは元CSVから削除され、未マッチ行のみが元CSVに上書き保存される。

【マッチキー】:
  1. 税率区分:
     - CSV.taxrate=8   <-> siwake col20="4" (軽減8%)
     - CSV.taxrate=10  <-> siwake col20="0" (標準10%)
     - CSV.taxrate=""  <-> siwake col20 は不問 (ワイルドカード)
  2. 取引先名:
     - siwake_filtered.txt 側は filter_siwake.py により正規化済み
     - CSV 側を同一ルール（株式会社->㈱, 半角全角統一, 空白除去, 大文字化等）で正規化して突合

【マッチング判定】:
  - まず「完全一致」を試行（1件に一意に定まる場合のみ採用）
  - 無ければ「部分一致/前方一致」を試行（1件に一意に定まる場合のみ採用）
  - 複数該当する場合は特定不能のため「未マッチ」扱い

使い方:
    python match_receipts.py [receipts_csv] [siwake_filtered_txt] [output_siwake_txt]
"""

import csv
from datetime import datetime
import re
import sys
import unicodedata
from pathlib import Path

# --- siwake.txt 列インデックス ---
SIWAKE_COL_DATE     = 0    # 取引日
SIWAKE_COL_D_SUB    = 6    # 借方補助コード
SIWAKE_COL_TAXCODE  = 9    # 借方税区分
SIWAKE_COL_C_CODE   = 10   # 貸方科目コード
SIWAKE_COL_C_SUB    = 11   # 貸方補助コード
SIWAKE_COL_AMOUNT   = 15   # 金額
SIWAKE_COL_TAX_AMT  = 16   # 消費税額
SIWAKE_COL_TAXRATE  = 20   # 税率区分 (4=軽減8%, 0=標準10%)
SIWAKE_COL_DESC     = 21   # 摘要
SIWAKE_COL_PARTNER  = 22   # 取引先
SIWAKE_COL_REG_DATE = 29   # 登録日 / 作成日

# --- taxrate 変換テーブル（CSV -> siwake col20） ---
TAXRATE_MAP = {
    "8":  "4",   # 軽減8%
    "10": "0",   # 標準10%
    "":   None,  # 空欄 = ワイルドカード（税率問わず）
}


def format_date(d_str: str) -> str:
    """CSVの日付（YYYY/M/D等）を YYYY/MM/DD に正規化する"""
    if not d_str:
        return ""
    d_str = d_str.strip()
    m = re.match(r"^(\d{4})[/-](\d{1,2})[/-](\d{1,2})$", d_str)
    if m:
        return f"{int(m.group(1)):04d}/{int(m.group(2)):02d}/{int(m.group(3)):02d}"
    return d_str


def parse_amount(amt_str: str) -> int:
    """金額文字列（カンマや空白を含む場合あり）を整数に変換する"""
    if not amt_str:
        return 0
    cleaned = re.sub(r"[^\d\-]", "", str(amt_str))
    try:
        return int(cleaned)
    except ValueError:
        return 0


def calculate_tax(amount: int, taxrate: str, taxcode: str, orig_tax: int) -> int:
    """
    金額・税率区分・税区分から消費税額を内税計算する。
    元レコードの消費税額が0（対象外取引等）の場合は0とする。
    """
    if orig_tax == 0 or not amount:
        return 0

    if taxrate == "4":    # 軽減8%
        base_tax = round(amount * 8 / 108)
    elif taxrate == "0":  # 標準10%
        base_tax = round(amount * 10 / 110)
    else:
        return 0

    # インボイス経過措置（80%控除）
    if taxcode in ("514", "515"):
        return round(base_tax * 0.8)

    return base_tax


def build_siwake_row(row: dict, rec: dict) -> list[str]:
    """
    マッチしたCSV行の情報を該当仕訳レコード形式に当てはめ、
    会計ソフト取込用の列リスト（30列）を生成する。
    """
    cols = list(rec["cols"])

    # 1. 取引日 (col 0): CSVの日付（YYYY/MM/DD形式）
    csv_date = format_date(row.get("date", ""))
    if csv_date:
        cols[SIWAKE_COL_DATE] = csv_date

    # 2. 金額 (col 15) & 消費税額 (col 16)
    amount = parse_amount(row.get("total_amount", ""))
    cols[SIWAKE_COL_AMOUNT] = str(amount)

    orig_tax = int(cols[SIWAKE_COL_TAX_AMT]) if cols[SIWAKE_COL_TAX_AMT].isdigit() else 0
    tax_amt = calculate_tax(amount, cols[SIWAKE_COL_TAXRATE], cols[SIWAKE_COL_TAXCODE], orig_tax)
    cols[SIWAKE_COL_TAX_AMT] = str(tax_amt)

    # 3. 摘要 (col 21): CSVの description を優先し、空欄なら仕訳元の摘要を残す
    csv_desc = row.get("description", "").strip().strip('"\'')
    if csv_desc:
        cols[SIWAKE_COL_DESC] = f'"{csv_desc}"'
    # 空欄の場合は cols[SIWAKE_COL_DESC] をそのまま保持

    # 4. 取引先 (col 22): 仕訳元の取引先名（マスタ登録名）をそのまま保持（cols[22]そのまま）

    # 5. 登録日 / 作成日 (col 29): 本日の日付
    if len(cols) > SIWAKE_COL_REG_DATE:
        cols[SIWAKE_COL_REG_DATE] = datetime.now().strftime("%Y/%m/%d")

    return cols


def normalize_name(name: str) -> str:
    """
    取引先名を正規化する（CSV 適用用）。
    1. 前後の引用符・空白を除去
    2. NFKC正規化（半角カナ->全角カナ、全角英数->半角英数）
    3. 空白文字（全角・半角・タブ等）を除去
    4. 法人格の表記統一:
       - 株式会社, (株), （株） -> ㈱
       - 有限会社, (有), （有） -> ㈲
       - 合同会社, (同), （同） -> ㈾
       - 一般社団法人, (社), （社） -> ㈳
    5. 英字を大文字に統一
    """
    if not name:
        return ""
    name = name.strip('"\' \t\r\n')

    # 1. NFKC正規化 (半角カナ->全角カナ, ㈱や㈲は一旦(株)(有)に展開される)
    name = unicodedata.normalize("NFKC", name)

    # 2. 全角・半角スペースの除去
    name = re.sub(r"[\s\u3000]+", "", name)

    # 3. 法人格表記を統一 (株式会社->㈱, 有限会社->㈲)
    name = re.sub(r"株式会社|\(株\)|（株）", "㈱", name)
    name = re.sub(r"有限会社|\(有\)|（有）", "㈲", name)
    name = re.sub(r"合同会社|\(同\)|（同）", "㈾", name)
    name = re.sub(r"一般社団法人|\(社\)|（社）", "㈳", name)

    # 4. 英字を大文字に統一
    name = name.upper()
    return name


def load_siwake(path: Path):
    """
    siwake_filtered.txt を読み込み、(records, header_line) で返す。
    （※取引先名は filter_siwake.py 側で正規化済みのため、ここでの正規化は行わない）
    """
    enc = "utf-8-sig"
    records = []
    header_line = ""

    with open(path, encoding=enc, errors="replace", newline="") as f:
        for line in f:
            line = line.rstrip("\r\n")
            if not line:
                continue
            cols = line.split("\t")
            if len(cols) <= max(SIWAKE_COL_PARTNER, SIWAKE_COL_TAXRATE):
                if not header_line and len(records) == 0:
                    header_line = line
                continue

            partner_raw  = cols[SIWAKE_COL_PARTNER].strip('"')
            partner_norm = partner_raw   # filter_siwake.py で既に正規化済み
            taxrate      = cols[SIWAKE_COL_TAXRATE]

            records.append({
                "partner_raw":  partner_raw,
                "partner_norm": partner_norm,
                "taxrate":      taxrate,
                "cols":         cols,
            })
    return records, header_line


def find_match(ship_to_norm: str, target_tax: str | None, siwake_records: list):
    """
    正規化後取引先名と税率でマッチする仕訳レコードを検索する。
    完全一致を優先し、無ければ部分一致/前方一致を試行する。
    ※ 候補が2件以上（複数該当）ある場合は科目を特定できないため、該当なし（未マッチ）として扱う。
    """
    if not ship_to_norm:
        return None, "未マッチ"

    # 税率条件で候補を絞り込み
    candidates = [
        r for r in siwake_records
        if target_tax is None or r["taxrate"] == target_tax
    ]

    # 1. 完全一致
    exact = [r for r in candidates if r["partner_norm"] == ship_to_norm]
    if len(exact) == 1:
        return exact[0], "完全一致"
    elif len(exact) > 1:
        # 複数該当は科目を特定できないため該当なし
        return None, "未マッチ"

    # 2. 前方一致 / 包含一致（店舗名付きや法人格省略に対応）
    # 例: CSV "鶴見石油㈱浜松町SS" <-> TXT "鶴見石油㈱"
    # 例: CSV "富士シティオ㈱FUJI伊勢町店" <-> TXT "富士シティオ㈱"
    # 例: CSV "KDDI㈱" <-> TXT "KDDI"
    s_norm_no_corp = re.sub(r"[㈱㈲㈾㈳]", "", ship_to_norm)
    fuzzy = []
    for r in candidates:
        p_norm = r["partner_norm"]
        p_norm_no_corp = re.sub(r"[㈱㈲㈾㈳]", "", p_norm)

        # 3文字以上で包含関係があるか
        is_match = False
        if len(p_norm) >= 3 and p_norm in ship_to_norm:
            is_match = True
        elif len(ship_to_norm) >= 3 and ship_to_norm in p_norm:
            is_match = True
        elif s_norm_no_corp and p_norm_no_corp:
            if len(p_norm_no_corp) >= 3 and p_norm_no_corp in s_norm_no_corp:
                is_match = True
            elif len(s_norm_no_corp) >= 3 and s_norm_no_corp in p_norm_no_corp:
                is_match = True

        if is_match:
            fuzzy.append(r)

    if len(fuzzy) == 1:
        return fuzzy[0], "部分一致"
    elif len(fuzzy) > 1:
        # 複数該当は特定できないため該当なし
        return None, "未マッチ"

    return None, "未マッチ"


def detect_csv_encoding(path: Path) -> str:
    """CSVファイルのエンコーディング（UTF-8 BOM付き / CP932 / UTF-8）を判定する"""
    try:
        raw = path.read_bytes()[:4]
        if raw.startswith(b"\xef\xbb\xbf"):
            return "utf-8-sig"
        path.read_text(encoding="utf-8")
        return "utf-8"
    except (UnicodeDecodeError, Exception):
        return "cp932"


def run_match_receipts(
    csv_path: Path | str,
    siwake_path: Path | str,
    out_txt_path: Path | str,
    progress_cb=None,
) -> dict:
    """
    receipts_accounting.csv と siwake_filtered.txt を突合し、
    siwake_matched.txt を作成して元CSVからマッチ行を削除（上書き）する。
    GUI や他スクリプトから呼び出し可能。
    """
    csv_path     = Path(csv_path)
    siwake_path  = Path(siwake_path)
    out_txt_path = Path(out_txt_path)

    if progress_cb:
        progress_cb("フィルタ済み仕訳データ読み込み中…")

    siwake_records, header_line = load_siwake(siwake_path)
    csv_enc = detect_csv_encoding(csv_path)

    with open(csv_path, encoding=csv_enc, errors="replace", newline="") as f:
        reader = csv.DictReader(f)
        fieldnames = reader.fieldnames or []
        rows = list(reader)

    siwake_rows = []
    unmatched_rows = []
    exact_count = 0
    fuzzy_count = 0

    if progress_cb:
        progress_cb(f"突合判定中…（{len(rows)}行）")

    for row in rows:
        taxrate_csv  = row.get("taxrate", "").strip()
        ship_to_name = row.get("ship_to_name", "").strip()

        # CSV側も正規化を実行
        ship_to_norm = normalize_name(ship_to_name)

        # taxrate を siwake col20 値に変換
        target_tax = TAXRATE_MAP.get(taxrate_csv, taxrate_csv)

        # マッチング検索（1件のみ特定できる場合のみ取得、複数該当は未マッチ）
        rec, match_type = find_match(ship_to_norm, target_tax, siwake_records)

        if rec is not None:
            if match_type == "完全一致":
                exact_count += 1
            elif match_type == "部分一致":
                fuzzy_count += 1

            # 会計ソフト取込用仕訳行を生成（CSV側からは除外）
            siwake_cols = build_siwake_row(row, rec)
            siwake_rows.append(siwake_cols)
        else:
            # 未マッチ行は元CSVに残す
            unmatched_rows.append(row)

    # 1. 会計ソフト取込用 siwake_matched.txt 出力（TSV形式、CRLF）
    with open(out_txt_path, "w", encoding="utf-8-sig", newline="") as f:
        if header_line:
            f.write(header_line + "\r\n")
        for s_cols in siwake_rows:
            f.write("\t".join(s_cols) + "\r\n")

    # 2. 元のCSVを未マッチ行のみで上書き保存
    with open(csv_path, "w", encoding=csv_enc, newline="") as f:
        writer = csv.DictWriter(f, fieldnames=fieldnames, extrasaction="ignore")
        writer.writeheader()
        writer.writerows(unmatched_rows)

    matched_count = len(siwake_rows)
    unmatched_count = len(unmatched_rows)
    total_count = len(rows)

    result = {
        "total": total_count,
        "matched": matched_count,
        "exact": exact_count,
        "fuzzy": fuzzy_count,
        "unmatched": unmatched_count,
        "out_txt_path": str(out_txt_path),
        "csv_path": str(csv_path),
    }

    print(
        f"突合結果: 合計 {total_count} 件中 {matched_count} 件マッチ（完全一致: {exact_count} 件, 部分一致: {fuzzy_count} 件, 未マッチ: {unmatched_count} 件）"
    )
    print(f"  -> 会計取込用仕訳データ: {matched_count} 行を出力 -> {out_txt_path}")
    print(f"  -> 未マッチCSVデータ: {unmatched_count} 行を上書き保存 -> {csv_path}（マッチ分 {matched_count} 件を削除）")

    return result


def main():
    args = sys.argv[1:]
    csv_path     = Path(args[0]) if len(args) > 0 else Path("receipts_accounting.csv")
    siwake_path  = Path(args[1]) if len(args) > 1 else Path("siwake_filtered.txt")
    out_txt_path = Path(args[2]) if len(args) > 2 else Path("siwake_matched.txt")
    run_match_receipts(csv_path, siwake_path, out_txt_path)


if __name__ == "__main__":
    main()
