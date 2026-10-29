#!/usr/bin/env python3
"""
siwake.txt フィルタリング&重複排除スクリプト

1. 列11（貸方補助コード）が "1101" の行のみ残す
2. 取引先名を正規化（match_receipts.py と同一ロジック: 株式会社->㈱、半角全角統一、空白除去等）
3. 取引先・借方科目コード・借方税区分 が同じものは、
   21カラム目の「摘要」が最も抽象的に表現されている行をGroq APIで判定して残す
   （全て空欄の場合は空欄の行を残す）

使い方:
    python filter_siwake.py siwake.txt [出力ファイル名]

siwake.txt 列インデックス（タブ区切り）:
    6  : 借方科目コード（補助コード: 1112 など）
    9  : 借方税区分（810 など）
    11 : 貸方補助コード（フィルタ対象）
    20 : 税率区分
    21 : 摘要（ダブルクォート付きで格納される場合あり）
    22 : 取引先（ダブルクォート付きで格納）
"""

import json
import os
import re
import sys
import time
import unicodedata
import urllib.error
import urllib.request
from collections import OrderedDict
from pathlib import Path

# フィルタ対象の列インデックスと値
FILTER_COL = 11        # 貸方補助コード
FILTER_VAL = "1101"

# 摘要の列インデックス
DESC_COL = 21          # 摘要

# 取引先列インデックス
PARTNER_COL = 22       # 取引先名

# 重複排除キーの列インデックス（pdf_to_csv.py の DEDUP_KEYS に対応）
DEDUP_COLS = {
    "取引先":        PARTNER_COL,  # 取引先名（正規化後）
    "借方科目コード":   6,            # 借方科目コード（補助コード: 1112 など）
    "借方税区分":      9,            # 借方税区分（810 など）
    "税率区分":       20,           # 軽減税率区分（4=軽減8%, 0=標準10%）
}

GROQ_MODELS = ["qwen/qwen3.8-27b", "openai/gpt-oss-120b", "openai/gpt-oss-20b"]


def normalize_partner_name(name: str) -> str:
    """
    取引先名を正規化する（match_receipts.py と共通ロジック）。
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


def get_groq_api_key() -> str:
    """環境変数または GROQ_API_KEY.txt から API キーを取得する"""
    api_key = os.environ.get("GROQ_API_KEY", "").strip()
    if api_key:
        return api_key

    # スクリプトのディレクトリ、またはカレントディレクトリの GROQ_API_KEY.txt を探す
    candidates = [
        Path(__file__).resolve().parent / "GROQ_API_KEY.txt",
        Path.cwd() / "GROQ_API_KEY.txt",
    ]
    for p in candidates:
        if p.is_file():
            key = p.read_text(encoding="utf-8").strip()
            if key:
                return key
    return ""


def clean_desc(raw: str) -> str:
    """摘要テキストから前後の空白や引用符を除去する"""
    return raw.strip().strip('"').strip()


def pick_most_abstract_batch(batch_groups: dict[str, list[str]], api_key: str) -> dict[str, str]:
    """
    Groq API を呼び出して、各グループの候補リストから最も抽象的な摘要を選択する。
    失敗時は文字数が最も短い候補をフォールバックとして返す。
    """
    if not api_key:
        print("【警告】GROQ_API_KEY が見つからないため、最も文字数が短い摘要をフォールバック採用します。")
        return {gid: min(candidates, key=len) for gid, candidates in batch_groups.items()}

    url = "https://api.groq.com/openai/v1/chat/completions"
    headers = {
        "Authorization": f"Bearer {api_key}",
        "Content-Type": "application/json",
        "User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64)",
    }

    prompt = f"""あなたは会計・仕訳データの正規化の専門家です。
以下に、重複排除の各グループの「摘要」候補リストが与えられています。
各グループについて、候補リストの中から、**最も内容が抽象的に表現されているもの（日付・数量・個別具体的な品名・部屋番号などの限定的な情報を含まず、より上位の概念・包括的・一般的な表現）** を1つだけ選んでください。

候補データ:
{json.dumps(batch_groups, ensure_ascii=False, indent=2)}

必ず以下のJSON形式のみで回答してください。キーはグループ名、値は選んだ摘要文字列（候補リスト内に存在する文字列）です。
{{
  "g_0": "...",
  "g_1": "..."
}}
"""

    for model in GROQ_MODELS:
        payload = {
            "model": model,
            "messages": [
                {"role": "system", "content": "あなたは会計仕訳データの正規化アシスタントです。必ず指定されたJSON形式のみを出力してください。"},
                {"role": "user", "content": prompt},
            ],
            "response_format": {"type": "json_object"},
            "temperature": 0.0,
        }
        for attempt in range(2):  # 429対策で最大2回試行
            try:
                req = urllib.request.Request(url, data=json.dumps(payload).encode("utf-8"), headers=headers)
                with urllib.request.urlopen(req, timeout=30) as resp:
                    res_data = json.loads(resp.read().decode("utf-8"))
                    content_str = res_data["choices"][0]["message"]["content"]
                    result = json.loads(content_str)
                    # 結果の検証と不足分の補完
                    validated = {}
                    for gid, candidates in batch_groups.items():
                        val = result.get(gid)
                        if val in candidates:
                            validated[gid] = val
                        else:
                            # 候補リストの中に完全一致しない場合は最短文字列を採用
                            validated[gid] = min(candidates, key=len)
                    return validated
            except urllib.error.HTTPError as e:
                if e.code == 429 and attempt == 0:
                    time.sleep(2.0)  # レートリミット時は少し待機して再試行
                    continue
                print(f"【注意】Groq API ({model}) HTTPエラー: {e}")
                break
            except Exception as e:
                print(f"【注意】Groq API ({model}) 呼び出しエラー: {e}")
                break

    # すべてのモデルで失敗した場合は文字数が最も短い候補をフォールバック
    return {gid: min(candidates, key=len) for gid, candidates in batch_groups.items()}


def run_filter_siwake(src: Path | str, dst: Path | str | None = None, progress_cb=None) -> dict:
    """
    siwake.txt のフィルタリング & 重複排除を実行する。
    GUI や他スクリプトから呼び出し可能。
    """
    src = Path(src)
    dst = Path(dst) if dst is not None else src.with_stem(src.stem + "_filtered")

    enc = "utf-8-sig"
    header_line = ""
    total = 0
    filtered = []

    if progress_cb:
        progress_cb("過去仕訳データ読み込み・フィルタ中…")

    # ---- Step1: 貸方補助コード == "1101" でフィルタ ----
    with open(src, encoding=enc, errors="replace", newline="") as f:
        for line in f:
            line_stripped = line.rstrip("\r\n")
            if not line_stripped:
                continue
            cols = line_stripped.split("\t")
            if len(cols) <= FILTER_COL:
                if not header_line and total == 0:
                    header_line = line_stripped
                continue
            total += 1
            if cols[FILTER_COL] == FILTER_VAL:
                filtered.append(cols)

    # ---- Step2: 取引先名の正規化 -> 重複排除 ----
    max_col = max(max(DEDUP_COLS.values()), DESC_COL)
    groups = OrderedDict()
    for cols in filtered:
        if len(cols) <= max_col:
            continue

        norm_partner = normalize_partner_name(cols[PARTNER_COL])
        cols[PARTNER_COL] = f'"{norm_partner}"' if norm_partner else '""'

        key = tuple(cols[i] for i in DEDUP_COLS.values())
        if key not in groups:
            groups[key] = []
        groups[key].append(cols)

    to_judge = {}
    gid_to_key = {}

    for idx, (key, rows) in enumerate(groups.items()):
        candidates = []
        for r in rows:
            cd = clean_desc(r[DESC_COL])
            if cd and cd not in candidates:
                candidates.append(cd)

        if len(candidates) > 1:
            gid = f"g_{idx}"
            to_judge[gid] = candidates
            gid_to_key[gid] = key

    judged_results = {}
    if to_judge:
        api_key = get_groq_api_key()
        msg = f"重複グループのうち、摘要候補が複数ある {len(to_judge)} 件を Groq API で判定中..."
        print(msg)
        if progress_cb:
            progress_cb(msg)

        batch_size = 50
        judge_items = list(to_judge.items())
        for i in range(0, len(judge_items), batch_size):
            chunk = dict(judge_items[i : i + batch_size])
            chunk_results = pick_most_abstract_batch(chunk, api_key)
            judged_results.update(chunk_results)
            if i + batch_size < len(judge_items):
                time.sleep(1.0)

    deduped = []
    for idx, (key, rows) in enumerate(groups.items()):
        if len(rows) == 1:
            deduped.append(rows[0])
            continue

        gid = f"g_{idx}"
        if gid in judged_results:
            target_desc = judged_results[gid]
            chosen_row = None
            for r in rows:
                if clean_desc(r[DESC_COL]) == target_desc:
                    chosen_row = r
                    break
            if chosen_row is None:
                chosen_row = rows[0]
            deduped.append(chosen_row)
        else:
            first_non_empty = None
            for r in rows:
                if clean_desc(r[DESC_COL]):
                    first_non_empty = r
                    break
            deduped.append(first_non_empty if first_non_empty is not None else rows[0])

    # ---- 出力 ----
    with open(dst, "w", encoding=enc, newline="") as f:
        if header_line:
            f.write(header_line + "\r\n")
        for cols in deduped:
            f.write("\t".join(cols) + "\r\n")

    # 元txtファイルの削除（出力ファイルが正常に作成された後）
    if src.resolve() != dst.resolve() and src.exists():
        try:
            src.unlink()
        except Exception as e:
            print(f"【警告】元ファイルの削除に失敗しました ({src}): {e}")

    result = {
        "src": str(src),
        "dst": str(dst),
        "total": total,
        "filtered": len(filtered),
        "deduped": len(deduped),
        "header": header_line,
    }

    print(
        f"{total} 件 -> フィルタ後 {len(filtered)} 件 -> 重複排除後 {len(deduped)} 件 -> {dst}"
    )
    return result


def main():
    if len(sys.argv) < 2:
        sys.exit(f"usage: {sys.argv[0]} siwake.txt [output.txt]")

    src = Path(sys.argv[1])
    dst = Path(sys.argv[2]) if len(sys.argv) > 2 else src.with_stem(src.stem + "_filtered")
    run_filter_siwake(src, dst)


if __name__ == "__main__":
    main()

