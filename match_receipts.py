#!/usr/bin/env python3
"""
receipts_accounting.csv と siwake_filtered.txt を突合するスクリプト

【マッチキー】:
  1. 税率区分:
     - CSV.taxrate=8   <-> siwake col20="4" (軽減8%)
     - CSV.taxrate=10  <-> siwake col20="0" (標準10%)
     - CSV.taxrate=""  <-> siwake col20 は不問 (ワイルドカード)
  2. 取引先名 (TXTもCSVも同一の正規化を実行して突合):
     - 株式会社, (株), （株） -> ㈱
     - 有限会社, (有), （有） -> ㈲
     - 既存の ㈱ や ㈲ などの略称はそのまま保持
     - 半角カナ <-> 全角カナを統一 (NFKC)
     - 全角英数 <-> 半角英数を統一 (NFKC)
     - スペース（全角・半角）を除去
     - 英字を大文字に統一

【マッチング判定】:
  - まず「完全一致」を試行
  - 完全一致しない場合は、店舗名付きや法人格有無に対応する「部分一致/前方一致」を試行

使い方:
    python match_receipts.py [receipts_csv] [siwake_filtered_txt] [output_csv]
"""

import csv
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
SIWAKE_COL_PARTNER  = 22   # 取引先

# --- taxrate 変換テーブル（CSV -> siwake col20） ---
TAXRATE_MAP = {
    "8":  "4",   # 軽減8%
    "10": "0",   # 標準10%
    "":   None,  # 空欄 = ワイルドカード（税率問わず）
}


def normalize_name(name: str) -> str:
    """
    取引先名を正規化する（TXT / CSV 共通適用）。
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
    siwake_filtered.txt を読み込み、行データを正規化取引先名付きのリストで返す。
    """
    enc = "utf-8-sig"
    records = []

    with open(path, encoding=enc, errors="replace", newline="") as f:
        for line in f:
            line = line.rstrip("\r\n")
            if not line:
                continue
            cols = line.split("\t")
            if len(cols) <= max(SIWAKE_COL_PARTNER, SIWAKE_COL_TAXRATE):
                continue

            partner_raw  = cols[SIWAKE_COL_PARTNER].strip('"')
            partner_norm = normalize_name(partner_raw)   # TXT側も正規化
            taxrate      = cols[SIWAKE_COL_TAXRATE]

            records.append({
                "partner_raw":  partner_raw,
                "partner_norm": partner_norm,
                "taxrate":      taxrate,
                "cols":         cols,
            })
    return records


def find_matches(ship_to_norm: str, target_tax: str | None, siwake_records: list):
    """
    正規化後取引先名と税率でマッチする仕訳レコードを検索する。
    完全一致を優先し、無ければ部分一致/前方一致を試行する。
    """
    if not ship_to_norm:
        return [], ""

    # 税率条件で候補を絞り込み
    candidates = [
        r for r in siwake_records
        if target_tax is None or r["taxrate"] == target_tax
    ]

    # 1. 完全一致
    exact = [r for r in candidates if r["partner_norm"] == ship_to_norm]
    if exact:
        return exact, "完全一致"

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

    if fuzzy:
        return fuzzy, "部分一致"

    return [], "未マッチ"


def main():
    args = sys.argv[1:]
    csv_path    = Path(args[0]) if len(args) > 0 else Path("receipts_accounting.csv")
    siwake_path = Path(args[1]) if len(args) > 1 else Path("siwake_filtered.txt")
    out_path    = Path(args[2]) if len(args) > 2 else Path("matched_output.csv")

    siwake_records = load_siwake(siwake_path)

    results = []

    with open(csv_path, encoding="cp932", errors="replace", newline="") as f:
        reader = csv.DictReader(f)
        for row in reader:
            taxrate_csv  = row.get("taxrate", "").strip()
            ship_to_name = row.get("ship_to_name", "").strip()

            # CSV側も正規化を実行
            ship_to_norm = normalize_name(ship_to_name)

            # taxrate を siwake col20 値に変換
            target_tax = TAXRATE_MAP.get(taxrate_csv, taxrate_csv)

            # マッチング検索
            matched_records, match_type = find_matches(ship_to_norm, target_tax, siwake_records)

            if matched_records:
                for rec in matched_records:
                    scols = rec["cols"]
                    results.append({
                        # receipts (CSV) 側
                        "r_Index":        row.get("Index", ""),
                        "r_date":         row.get("date", ""),
                        "r_description":  row.get("description", ""),
                        "r_total_amount": row.get("total_amount", ""),
                        "r_taxrate":      taxrate_csv,
                        "r_ship_to_name": ship_to_name,
                        "r_ship_to_norm": ship_to_norm,
                        # siwake (TXT) 側
                        "s_date":         scols[SIWAKE_COL_DATE],
                        "s_d_sub_code":   scols[SIWAKE_COL_D_SUB],
                        "s_taxcode":      scols[SIWAKE_COL_TAXCODE],
                        "s_c_code":       scols[SIWAKE_COL_C_CODE],
                        "s_c_sub":        scols[SIWAKE_COL_C_SUB],
                        "s_amount":       scols[SIWAKE_COL_AMOUNT],
                        "s_tax_amount":   scols[SIWAKE_COL_TAX_AMT],
                        "s_taxrate":      scols[SIWAKE_COL_TAXRATE],
                        "s_partner":      rec["partner_raw"],
                        "s_partner_norm": rec["partner_norm"],
                        "match_type":     match_type,
                    })
            else:
                results.append({
                    "r_Index":        row.get("Index", ""),
                    "r_date":         row.get("date", ""),
                    "r_description":  row.get("description", ""),
                    "r_total_amount": row.get("total_amount", ""),
                    "r_taxrate":      taxrate_csv,
                    "r_ship_to_name": ship_to_name,
                    "r_ship_to_norm": ship_to_norm,
                    "s_date": "", "s_d_sub_code": "", "s_taxcode": "",
                    "s_c_code": "", "s_c_sub": "", "s_amount": "",
                    "s_tax_amount": "", "s_taxrate": "",
                    "s_partner": "", "s_partner_norm": "",
                    "match_type":     "未マッチ",
                })

    fieldnames = [
        "r_Index", "r_date", "r_description", "r_total_amount",
        "r_taxrate", "r_ship_to_name", "r_ship_to_norm",
        "s_date", "s_d_sub_code", "s_taxcode", "s_c_code", "s_c_sub",
        "s_amount", "s_tax_amount", "s_taxrate", "s_partner", "s_partner_norm",
        "match_type",
    ]

    with open(out_path, "w", encoding="utf-8-sig", newline="") as f:
        writer = csv.DictWriter(f, fieldnames=fieldnames, extrasaction="ignore")
        writer.writeheader()
        writer.writerows(results)

    exact_count   = sum(1 for r in results if r["match_type"] == "完全一致")
    fuzzy_count   = sum(1 for r in results if r["match_type"] == "部分一致")
    unmatch_count = sum(1 for r in results if r["match_type"] == "未マッチ")
    total_count   = len(results)

    print(
        f"合計: {total_count} 行 (完全一致: {exact_count} 件, 部分一致: {fuzzy_count} 件, 未マッチ: {unmatch_count} 件) -> {out_path}"
    )


if __name__ == "__main__":
    main()
