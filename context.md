# Receipt App コンテキスト（2026-07-22 更新）

## プロジェクト概要
Tkinter デスクトップアプリ。領収書画像をOCRし、位置情報付きJSONを生成してclaude.ai
（シークレットチャット、手動コピペ）にフィールド抽出を依頼し、Claudeが返したCSVと
JSONを「完全一致→正規化一致→RapidFuzzのあいまい一致」の段階照合で突き合わせて
画像上に該当箇所をマークする。CSVはそのまま会計ソフトに取り込む。

- メインファイル: `receipt_app.py`（Tkinter GUI、①②の2ステップ）
- OCR位置情報抽出: `ocr_positions.py`
- CSV突合・画像マーク: `match_and_mark.py`
- Claude貼り付け用定型プロンプト: `claude_prompt_template.txt`

**方向転換の経緯**: 従来はローカルNER（xlm-roberta-ner-japanese）＋正規表現で
フィールド抽出していたが、日本語領収書の文脈理解精度がClaudeの方が高いため、
フィールド抽出自体をClaudeに委譲する方式に変更した。API連携ではなく手動チャット
コピペを想定しているため、ワークフローが2段階（JSON作成→人手でclaude.ai→CSV取込）
に分かれている。

`subject_engine.py`（ローカルRAGでの勘定科目推定）は現状未使用。将来的に実装検討
の可能性はあるが今は触らない。

---

## 環境

- Python 3.12（`venv/`）
- paddleocr==3.6.0 / paddlex==3.6.1
  - **注意**: より新しいバージョンでは `NotImplementedError: ConvertPirAttribute2RuntimeAttribute not support [pir::ArrayAttribute<pir::DoubleAttribute>]` が発生してOCR推論が動かない。PIR + oneDNN (MKL-DNN) の非互換が原因。このバージョンで固定。
- rapidfuzz（`match_and_mark.py` の description/ship_to_name のあいまい一致判定に使用）
- `subject_engine.py`・`download_models.py`・`simple_OCR.py`・`vendors.db`は未使用のため削除済み
  （NER廃止・embedding方式からRapidFuzz方式への変更に伴い、torch/sentence-transformers系の依存も不要になった）

### PaddleXモデルの保存先（重要・過去にハマったポイント）
`ocr_positions.py`は `PADDLE_PDX_CACHE_HOME` 環境変数でモデルキャッシュ先を
`<プロジェクト>/models/` に固定している。**注意**: 以前のコードは
`PADDLE_PDX_HOME` という変数名を使っていたが、これはインストール済み
paddlex（`paddlex/utils/cache.py`）が実際には参照しない誤った変数名で、
効いていなかった。そのため過去のダウンロードは全て `C:\Users\<user>\.paddlex`
（ユーザーのホームディレクトリ、既定値 `DEFAULT_CACHE_DIR`）に保存されてしまっていた。
2026-07-22に変数名を修正し、既存のモデル一式（約210MB）を`models/`配下に移動済み。
モデルが見当たらない/再ダウンロードが走る場合は、まずこの環境変数名を疑うこと。

**embeddingを使わない理由**: Step2.5は「意味検索」ではなく「OCR結果から元文字列を探す」
検索処理であるため、multilingual-e5-smallによるコサイン類似度は不要と判断し廃止した。
実際、Claudeが読み取り困難な手書き文字を誤転記したケース（例:「(株)金失舟合」を
「株式会社鉄鋳合」と誤読）で、embeddingは無関係な行（"レヅNo"等）に高スコアで
誤マッチしていたが、RapidFuzzは同じ入力に対して閾値未満と正しく判定し「未マッチ」
として処理した。

---

## ワークフロー

### ① 画像を選択してJSON作成（`receipt_app.py: on_step1` → `ocr_positions.py`）
1. 複数の領収書画像を選択
2. `OcrPositionExtractor` が画像ごとにPaddleOCRを実行し、`rec_texts` と
   `rec_boxes`（`[x1,y1,x2,y2]`矩形、`paddlex.inference.pipelines.ocr.result`
   で確認済みの属性名）をインデックス揃いで取得
3. 位置情報付きJSONを保存（保存先はユーザー選択）:
   ```json
   {
     "images": [
       {
         "index": 1,
         "file_name": "1.jpg",
         "width": 1200,
         "height": 1600,
         "items": [
           {"id": 0, "text": "〇〇株式会社", "box": [120, 45, 480, 90]},
           ...
         ]
       }
     ]
   }
   ```
   `index` はCSVの `Index` 列と対応させる番号（画像選択順の連番）。
4. `claude_prompt_template.txt` の内容＋JSON全文をクリップボードにコピー
   （Tkinter標準の `clipboard_clear`/`clipboard_append`、追加依存なし）
5. ユーザーが手動でclaude.aiのシークレットチャットに貼り付け、CSVを取得する

### ② CSV貼り付け→マーク画像生成（`receipt_app.py: on_step2` → `match_and_mark.py`）
1. Claudeはファイルではなくテキスト形式でCSVを返すため、`_ask_paste_csv()` が開く
   貼り付け専用ダイアログ（Text欄）にCSVテキストをそのまま貼り付ける
   （列: `Index,date,description,total_amount,taxrate,ship_to_name,
   invoice_registered,warning_flag`）。Markdownのコードフェンス（\`\`\`csv ... \`\`\`）が
   付いていても `parse_claude_csv_text()` が自動的に剥がす
2. ①のJSON（直前のセッションのものか、ファイル選択）と元画像フォルダを指定
3. `match_and_mark.run()` がCSV各行と対応する画像のOCR items を突き合わせ:

   | CSVフィールド | 照合方法 |
   |---|---|
   | `total_amount` | 数字のみに正規化した文字列の完全一致（`_match_amount_field`） |
   | `date` | 年/月/日の構造を持つパターンを正規化して比較（`_match_date_field`）。単純な数字の部分一致は使わない（短い無関係な数字が誤って金額・日付にヒットするため） |
   | `invoice_registered` | OCRテキストを正規表現 `T\d{13}` で走査（trueの場合のみ） |
   | `description` / `ship_to_name` | 完全一致 → 空白除去後の正規化一致 → RapidFuzz（`fuzz.WRatio`、閾値80、`FUZZY_THRESHOLD`）のあいまい一致。閾値未満は「未マッチ」（`_match_text_field`）。descriptionはClaudeが要約・言い換えすることが多く未マッチになりやすいが、誤った枠を描くよりは安全という方針で許容している |
   | `warning_flag` | マーキング対象外 |

4. Pillowでフィールドごとに色分けした矩形を元画像のコピーに描画し、
   `<元ファイル名>_marked.<ext>` として出力フォルダに保存
5. マッチ件数・未マッチ一覧をサマリダイアログで表示、マーク済み画像をプレビュー表示
6. CSVファイル自体は一切書き換えない（そのまま会計ソフトに取り込む）

---

## 主要API

### `ocr_positions.py`
```python
extractor = OcrPositionExtractor()
record = extractor.process_one(index=1, image_path="1.jpg")
# → {"index":1, "file_name":"1.jpg", "width":.., "height":.., "items":[{"id":0,"text":..,"box":[x1,y1,x2,y2]}, ...]}

data = extractor.run_ocr_batch(["1.jpg", "2.jpg"])  # → {"images": [...]}
save_json(data, "ocr_positions.json")
```

### `match_and_mark.py`
```python
# GUIから: claude.aiのチャットからコピペしたテキストをそのまま渡す
result = match_and_mark.run_from_text(
    csv_text=pasted_text,           # Text欄に貼り付けられた生テキスト
    json_path="ocr_positions.json",
    images_dir="test/",
    out_dir="test/marked/",
)
# → {"total_rows":.., "processed":.., "field_match_counts":{...}, "unmatched":[...], "output_files":[...]}

# ファイルとして保存済みのCSVを使う場合（CLI/テスト用）
result = match_and_mark.run("claude_result.csv", "ocr_positions.json", "test/", "test/marked/")
```
CLI: `python match_and_mark.py <csv> <json> <images_dir> <out_dir>`

---

## 削除済みファイル（2026-07-22）

未使用だった以下は削除済み。将来、勘定科目推定などを再検討する場合はgit履歴
（`subject_engine.py`にvendors/raw_aliases/correctionsのスキーマとAPIの記録あり）
を参照。

- `subject_engine.py`（ローカルRAGでの勘定科目推定、vendors.db使用）
- `vendors.db`
- `download_models.py`（NER/e5-smallのDL用。PaddleOCRモデルは初回実行時に自動DLされる）
- `simple_OCR.py`（`ocr_positions.py`に置き換えられた初期の実験スクリプト）
