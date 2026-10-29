# AI_OCR_paddlex_with_claude_chat

ローカルOCR（PaddleOCR / PaddleX + OpenVINO高速化）× クラウドLLM（Claude / claude.aiの一時チャット）を組み合わせた、日本語領収書の会計仕訳データ作成パイプラインです。

領収書画像そのものや文字起こし結果を外部サービスの学習データにしないことを重視し、「重い文字起こし処理」はローカルで完結させ、「文脈理解・仕訳」だけをクラウドLLMの一時チャット（学習に使われない設定のチャット）に一時的に投げる構成にしています。

## ワークフロー

```
⓪ 事前準備: 過去仕訳データフィルタ（初回または過去仕訳更新時）
   過去仕訳データ（siwake.txt）をフィルタ・重複排除し、siwake_filtered.txt を作成

① ローカルでOCR実行
   領収書画像フォルダを指定 → PaddleOCR / PaddleX（OpenVINO EP推論）で一括OCR
   → プロンプト＋OCR結果を1つにまとめたJSONを生成し、クリップボードにコピー
   → 既定ブラウザでclaude.aiの新規チャットを開く

② claude.aiに貼り付け
   コピーされたJSONをそのままチャットに貼り付けるだけで、
   会計用CSV（Index,date,description,total_amount,taxrate,
   ship_to_name,invoice_registered,warning_flag）がコードブロックで返ってくる

③ ローカルで突合・マーキング
   ②のCSVをアプリに貼り付け（またはファイル指定） → ①のOCR結果と突合し、
   各フィールドの根拠箇所（座標）を矩形でマークした画像を出力

④ 会計仕訳データの作成
   突合済みCSVとフィルタ済み過去仕訳を突き合わせ、会計ソフトインポート用仕訳データ
   （siwake_matched.txt）を作成
   ※マッチした行はCSVから削除され、未マッチ行のみが元CSVに上書き保存されます

⑤ 人が最後に確認
   マーク画像と未マッチCSVをWindowsフォトアプリ／Excelで見比べて最終チェック・修正
   → siwake_matched.txt をそのまま会計ソフトへインポート
```

途中の画像プレビューやCSV編集用のUIはあえて作り込まず、目視確認・修正は使い慣れたWindowsフォトアプリとExcelで行う想定です。

## セットアップ

### 1. Python本体

Python 3.10〜3.12（3.12推奨）を使用します。tkinter（GUI）は標準ライブラリですが、Linuxでは別途システムパッケージが必要な場合があります。

```bash
# Ubuntu/Debianの場合
sudo apt install python3-tk
```

Windowsの公式インストーラーにはtkinterが同梱されています。

### 2. 依存パッケージ

```bash
pip install -r requirements.txt
```

> [!NOTE]
> - 本プロジェクトは PaddleOCR 3.6.0 / PaddleX 3.6.1 を使用し、OpenVINO Execution Provider (ONNX Runtime) 経由で推論を約4割高速化しています。
> - 新しいPaddlePaddle/PaddleOCRではPIRとoneDNNの非互換エラーが発生する場合があるため、動作確認済みのバージョンで固定されています。
> - PaddleX/PaddleOCRの初回実行時にモデルファイルが自動ダウンロードされます（保存先はプロジェクト直下の `models/`。`.gitignore` 済み）。

### 3. プロンプトテンプレート

`claude_prompt_template.txt` にClaudeへ渡す指示文（プロンプト）が入っています。
このファイルは編集可能です。列構成や判定ルールを変えたい場合はここを直接書き換えてください。

## 使い方

```bash
python receipt_app.py
```

1. **⓪ 過去仕訳フィルタ**: 過去仕訳テキスト（`siwake.txt`）を指定し、フィルタ実行（初回のみ）
2. **① OCR実行**: 画像フォルダとモデルフォルダを指定してOCRを実行。完了時にJSONがクリップボードにコピーされ、ブラウザでclaude.aiが開きます。
3. **② claude.aiでCSV取得**: チャットに `Ctrl+V` で貼り付けて送信し、返ってきたCSVコードブロックをコピーします。
4. **③ CSV突合・マーキング**: アプリの貼り付けダイアログまたはCSVファイルを指定して突合を実行。`画像フォルダ/marked/` に根拠箇所の矩形マーク画像が出力されます。
5. **④ 仕訳作成**: 「③ 仕訳作成」を実行すると、過去仕訳とマッチした行が `siwake_matched.txt` に出力され、未マッチ行だけがCSVに残ります。

## ファイル構成

| ファイル | 役割 |
|---|---|
| `receipt_app.py` | GUI本体。⓪〜④のワークフローを一括制御するデスクトップアプリ |
| `ocr_positions.py` | PaddleOCR / PaddleX（OpenVINO EP）で画像をバッチ処理し、位置情報（座標）付きOCR結果を生成 |
| `match_and_mark.py` | Claudeが返したCSVとOCR結果をRapidFuzz等で突合し、該当箇所を矩形マークした画像を出力 |
| `filter_siwake.py` | 過去仕訳データ（`siwake.txt`）の重複排除・正規化フィルタ処理 |
| `match_receipts.py` | 突合後CSVとフィルタ済み過去仕訳の突き合わせ・会計取込テキスト生成 |
| `claude_prompt_template.txt` | Claudeに渡すプロンプト本文（編集可能） |

## 注意事項

- 領収書画像・OCR結果・生成されたCSV・仕訳テキストには機密情報が含まれるため、`models/`・`ocr_positions.json`・`marked/`・`siwake*.txt`・`*.csv` などはリポジトリにコミットしないでください（`.gitignore` 済み）。
- claude.aiには学習に使われない一時的なチャット（シークレットチャット）として利用することを推奨します。
- `total_amount` や `date` のマッチングは正規化完全一致ベース、`description` や `ship_to_name` はあいまい一致（RapidFuzz、閾値80）です。Claudeの出力があいまい一致の限度を超えて要約されている場合は意図的に「未マッチ」として扱われます（無理に拾いに行くと誤マッチのリスクが増えるため）。

## ライセンス

未設定（必要に応じて追記してください）。
