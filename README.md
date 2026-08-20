# AI_OCR_paddlex_with_claude_chat

ローカルOCR（PaddleOCR）× クラウドLLM（Claude / claude.aiの一時チャット）を組み合わせた、
日本語領収書の会計CSV変換パイプラインです。

領収書画像そのものや文字起こし結果を外部サービスの学習データにしないことを重視し、
「重い文字起こし処理」はローカルで完結させ、「文脈理解・仕訳」だけをクラウドLLMの
一時チャット（学習に使われない設定のチャット）に一時的に投げる構成にしています。

## ワークフロー

```
① ローカルでOCR実行
   領収書画像フォルダを指定 → PaddleOCRで一括OCR
   → プロンプト＋OCR結果を1つにまとめたJSONを生成し、クリップボードにコピー
   → 既定ブラウザでclaude.aiの新規チャットを開く

② claude.aiに貼り付け
   コピーされたJSONをそのままチャットに貼り付けるだけで、
   会計用CSV（Index,date,description,total_amount,taxrate,
   ship_to_name,invoice_registered,warning_flag）がコードブロックで返ってくる

③ ローカルで突合・マーキング
   ②のCSVをアプリに貼り付け → ①のOCR結果と突合し、
   各フィールドの根拠箇所（座標）を矩形でマークした画像を出力

④ 人が最後に確認
   マーク画像とCSVをWindowsフォトアプリ／Excelで見比べて最終チェック・修正
   → そのまま会計ソフトへインポート
```

途中の画像プレビューやCSV編集用のUIはあえて作り込まず、目視確認・修正は
使い慣れたWindowsフォトアプリとExcelで行う想定です。

## セットアップ

### 1. Python本体

Python 3.10以降を推奨します。tkinter（GUI）は標準ライブラリですが、
Linuxでは別途システムパッケージが必要な場合があります。

```bash
# Ubuntu/Debianの場合
sudo apt install python3-tk
```

Windowsの公式インストーラーにはtkinterが同梱されています。

### 2. 依存パッケージ

```bash
pip install -r requirements.txt
```

PaddleOCRの初回実行時に、モデルファイルが自動でダウンロードされます
（保存先は`models/`。`.gitignore`済みなので、初回起動時に少し時間がかかります）。

### 3. プロンプトテンプレート

`claude_prompt_template.txt`にClaudeへ渡す指示文（プロンプト）が入っています。
このファイルは編集可能です。列構成や判定ルールを変えたい場合はここを直接書き換えてください。

## 使い方

```bash
python receipt_app.py
```

1. 画像フォルダとモデルフォルダを指定し、「①OCR実行」を押す
2. claude.aiのチャットに `Ctrl+V` で貼り付けて送信し、返ってきたCSVをコピーする
3. 「②CSV貼り付け→実行」を押し、コピーしたCSVを貼り付けて実行する
4. `画像フォルダ/marked/` に出力されたマーク済み画像とCSVを見比べて確認・修正する

画像フォルダは処理のたびに中身を入れ替えて使う運用を想定しています
（過去のOCR結果を使い回す機能は持たせていません）。

## ファイル構成

| ファイル | 役割 |
|---|---|
| `receipt_app.py` | GUI本体。①②の実行とプロンプト付きJSONの生成・クリップボードコピーを担当 |
| `ocr_positions.py` | PaddleOCRで画像をバッチ処理し、位置情報（座標）付きのOCR結果を生成 |
| `match_and_mark.py` | Claudeが返したCSVとOCR結果を突合し、該当箇所を矩形マークした画像を出力 |
| `claude_prompt_template.txt` | Claudeに渡すプロンプト本文（編集可能） |

## 注意事項

- 領収書画像・OCR結果・生成されたCSVには機密情報が含まれるため、`models/`・`ocr_positions.json`・
  `marked/`はリポジトリにコミットしないでください（`.gitignore`済み）。
- claude.aiには学習に使われない一時的なチャットとして利用することを推奨します。
- `total_amount`や`date`のマッチングは完全一致ベース、`description`や`ship_to_name`は
  あいまい一致（RapidFuzz、閾値80）です。Claudeの出力があいまい一致の限度を超えて要約されている場合、
  意図的に「未マッチ」として扱われます（無理に拾いに行くと誤マッチのリスクが増えるため）。

## ライセンス

未設定（必要に応じて追記してください）。
