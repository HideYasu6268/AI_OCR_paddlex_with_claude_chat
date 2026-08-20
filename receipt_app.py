# -*- coding: utf-8 -*-
"""
receipt_app.py

最小限のUI:
  上部  : 画像フォルダ（①②共通。毎回このフォルダの中身を入れ替えて使う運用）
  ①    : モデルフォルダ指定 → OCR実行 → JSON作成
          （プロンプト本文=instructions ＋ OCR結果=images を1つにまとめたJSON。
           本スクリプトと同じディレクトリに ocr_positions.json として固定名で保存）
          → そのJSON全体をクリップボードにコピー
          → 既定ブラウザで claude.ai の新規シークレットチャットを開く
          → チャットに貼り付けるだけで完結（別途プロンプトを貼る必要なし）
  ②    : Claudeが返したCSVを貼り付け → ①のJSONと突合 → 画像フォルダ/marked にマーク画像出力

目視確認・CSV修正はアプリ内では行わない（Windowsフォトアプリ＋Excelで直接行う運用）。
"""

import sys
sys.stdout.reconfigure(encoding='utf-8', errors='replace')

import os
import json
import traceback
import webbrowser
from typing import Any, Dict, List, Optional

import tkinter as tk
from tkinter import ttk, filedialog, messagebox

import match_and_mark

_BASE_DIR = os.path.dirname(os.path.abspath(__file__))
_PROMPT_TEMPLATE_PATH = os.path.join(_BASE_DIR, "claude_prompt_template.txt")
_JSON_PATH = os.path.join(_BASE_DIR, "ocr_positions.json")
_DEFAULT_MODELS_DIR = os.path.join(_BASE_DIR, "models")

_IMAGE_EXTS = {".jpg", ".jpeg", ".png", ".bmp", ".tif", ".tiff"}


class ReceiptApp(tk.Tk):

    def __init__(self):
        super().__init__()
        self.title("領収書OCR → Claude → CSV突合マーク")
        self.resizable(False, False)

        self._extractor = None  # OcrPositionExtractor（遅延import・遅延初期化）
        self._extractor_model_dir = None  # 初期化に使ったモデルフォルダ（切替検知用）

        self.images_dir_var = tk.StringVar()
        self.model_dir_var = tk.StringVar(value=_DEFAULT_MODELS_DIR)

        self._build_ui()

    # ──────────────────────────────
    # UI構築
    # ──────────────────────────────
    def _build_ui(self):
        pad = {"padx": 8, "pady": 4}

        common = ttk.LabelFrame(self, text="画像フォルダ（①②共通）")
        common.pack(fill="x", **pad)
        row = ttk.Frame(common)
        row.pack(fill="x", padx=6, pady=6)
        ttk.Entry(row, textvariable=self.images_dir_var, width=60).pack(side="left", fill="x", expand=True)
        ttk.Button(row, text="参照…", command=self._browse_images_dir).pack(side="left", padx=(6, 0))

        step1 = ttk.LabelFrame(self, text="① OCR実行 → JSON作成 → Claudeへ")
        step1.pack(fill="x", **pad)

        row1 = ttk.Frame(step1)
        row1.pack(fill="x", padx=6, pady=(6, 2))
        ttk.Label(row1, text="モデルフォルダ:").pack(side="left")
        ttk.Entry(row1, textvariable=self.model_dir_var, width=48).pack(side="left", fill="x", expand=True, padx=(6, 0))
        ttk.Button(row1, text="参照…", command=self._browse_model_dir).pack(side="left", padx=(6, 0))

        row2 = ttk.Frame(step1)
        row2.pack(fill="x", padx=6, pady=4)
        ttk.Button(row2, text="① OCR実行", command=self.on_step1).pack(side="left")
        self.step1_progress = ttk.Progressbar(row2, mode="determinate")
        self.step1_progress.pack(side="left", fill="x", expand=True, padx=(8, 0))

        self.step1_status = tk.StringVar(value="未実行")
        ttk.Label(step1, textvariable=self.step1_status, foreground="#555555").pack(
            anchor="w", padx=6, pady=(0, 6))

        step2 = ttk.LabelFrame(self, text="② CSV突合 → マーク画像生成")
        step2.pack(fill="x", **pad)

        row3 = ttk.Frame(step2)
        row3.pack(fill="x", padx=6, pady=6)
        ttk.Button(row3, text="② CSV貼り付け→実行", command=self.on_step2).pack(side="left")

        self.step2_status = tk.StringVar(value="未実行")
        ttk.Label(step2, textvariable=self.step2_status, foreground="#555555").pack(
            anchor="w", padx=6, pady=(0, 6))

    def _browse_images_dir(self):
        d = filedialog.askdirectory(title="画像フォルダを選択")
        if d:
            self.images_dir_var.set(d)

    def _browse_model_dir(self):
        d = filedialog.askdirectory(title="モデルフォルダを選択")
        if d:
            self.model_dir_var.set(d)

    # ──────────────────────────────
    # ① OCR実行
    # ──────────────────────────────
    def _get_extractor(self, model_dir: str):
        """PaddleOCRはモデルパスを import 時の環境変数でしか読まないため、
        ユーザーがモデルフォルダを指定・変更できるよう ocr_positions の import 自体を遅延させる。
        一度初期化した後にモデルフォルダを変更した場合は、アプリの再起動が必要。
        """
        if self._extractor is not None:
            if self._extractor_model_dir != model_dir:
                messagebox.showwarning(
                    "モデルフォルダ",
                    "モデルフォルダの変更を反映するにはアプリの再起動が必要です。\n"
                    f"現在使用中: {self._extractor_model_dir}"
                )
            return self._extractor

        os.environ["PADDLE_PDX_CACHE_HOME"] = model_dir
        global OcrPositionExtractor, save_json
        from ocr_positions import OcrPositionExtractor, save_json  # noqa: F401

        self.step1_status.set("PaddleOCR 初期化中…（初回のみ時間がかかります）")
        self.update_idletasks()
        self._extractor = OcrPositionExtractor()
        self._extractor_model_dir = model_dir
        return self._extractor

    def on_step1(self):
        images_dir = self.images_dir_var.get().strip()
        model_dir = self.model_dir_var.get().strip()

        if not images_dir or not os.path.isdir(images_dir):
            messagebox.showerror("エラー", "画像フォルダを正しく指定してください。")
            return
        if not model_dir:
            messagebox.showerror("エラー", "モデルフォルダを指定してください。")
            return

        image_paths = sorted(
            os.path.join(images_dir, n) for n in os.listdir(images_dir)
            if os.path.splitext(n)[1].lower() in _IMAGE_EXTS
        )
        if not image_paths:
            messagebox.showerror("エラー", "画像フォルダ内に画像ファイルが見つかりません。")
            return

        try:
            extractor = self._get_extractor(model_dir)
        except Exception as e:
            traceback.print_exc()
            messagebox.showerror("初期化エラー", f"PaddleOCRの初期化に失敗しました。\n{e}")
            return

        self.step1_progress["maximum"] = len(image_paths)
        self.step1_progress["value"] = 0

        images_result = []
        for i, path in enumerate(image_paths, start=1):
            self.step1_status.set(f"OCR処理中 {i}/{len(image_paths)}: {os.path.basename(path)}")
            self.update_idletasks()
            try:
                rec = extractor.process_one(i, path)
            except Exception as e:
                traceback.print_exc()
                rec = {"index": i, "file_name": os.path.basename(path),
                       "width": None, "height": None, "items": [], "error": str(e)}
            images_result.append(rec)
            self.step1_progress["value"] = i
            self.update_idletasks()

        try:
            with open(_PROMPT_TEMPLATE_PATH, "r", encoding="utf-8") as f:
                instructions = f.read()
        except Exception:
            messagebox.showerror(
                "エラー",
                f"プロンプトテンプレートが見つかりません。\n{_PROMPT_TEMPLATE_PATH}\n"
                "先にこのファイルを作成してください。"
            )
            return

        data = {"instructions": instructions, "images": images_result}
        try:
            save_json(data, _JSON_PATH)
        except Exception as e:
            traceback.print_exc()
            messagebox.showerror("保存エラー", f"JSONの保存に失敗しました。\n{e}")
            return

        self._copy_json_to_clipboard(data)
        try:
            webbrowser.open("https://claude.ai/new?incognito=")
        except Exception:
            pass

        self.step1_status.set(
            f"完了（{len(image_paths)}枚）: {_JSON_PATH}（プロンプト込みJSONをクリップボードにコピー済み）")

    def _copy_json_to_clipboard(self, data: Dict[str, Any]):
        """プロンプト（instructions）とOCR結果（images）を1つに含んだJSONを
        そのままクリップボードにコピーする。claude.aiには貼り付けるだけでよい。"""
        json_text = json.dumps(data, ensure_ascii=False, indent=2)
        self.clipboard_clear()
        self.clipboard_append(json_text)
        self.update()

    # ──────────────────────────────
    # ② CSV貼り付け → 突合 → マーク画像生成
    # ──────────────────────────────
    def _ask_paste_csv(self) -> Optional[str]:
        dlg = tk.Toplevel(self)
        dlg.title("CSV貼り付け")
        dlg.transient(self)
        dlg.grab_set()
        dlg.geometry("700x480")

        frm = ttk.Frame(dlg, padding=12)
        frm.pack(fill="both", expand=True)

        ttk.Label(frm, text="claude.aiが返したCSVテキストをこの欄に貼り付けてください（Ctrl+V）。",
                  font=("", 9)).pack(anchor="w")
        ttk.Label(
            frm,
            text=("ヘッダー行: Index,date,description,total_amount,taxrate,"
                  "ship_to_name,invoice_registered,warning_flag"),
            foreground="#555555", font=("", 8),
        ).pack(anchor="w", pady=(0, 6))

        txt = tk.Text(frm, height=20, wrap="none", font=("Consolas", 9))
        sb_y = ttk.Scrollbar(frm, orient="vertical", command=txt.yview)
        sb_x = ttk.Scrollbar(frm, orient="horizontal", command=txt.xview)
        txt.configure(yscrollcommand=sb_y.set, xscrollcommand=sb_x.set)
        sb_y.pack(side="right", fill="y")
        txt.pack(side="top", fill="both", expand=True)
        sb_x.pack(side="top", fill="x")
        txt.focus_set()

        result: List[Optional[str]] = [None]

        def on_paste_from_clipboard():
            try:
                clip = self.clipboard_get()
            except tk.TclError:
                messagebox.showwarning("クリップボード", "クリップボードにテキストがありません。", parent=dlg)
                return
            txt.delete("1.0", "end")
            txt.insert("1.0", clip)

        def on_ok():
            content = txt.get("1.0", "end")
            if not content.strip():
                messagebox.showwarning("入力エラー", "CSVテキストを貼り付けてください。", parent=dlg)
                return
            result[0] = content
            dlg.destroy()

        def on_cancel():
            dlg.destroy()

        btn_row = ttk.Frame(frm)
        btn_row.pack(pady=(8, 0))
        ttk.Button(btn_row, text="クリップボードから貼り付け",
                   command=on_paste_from_clipboard).pack(side="left", padx=4)
        ttk.Button(btn_row, text="OK", command=on_ok).pack(side="left", padx=4)
        ttk.Button(btn_row, text="キャンセル", command=on_cancel).pack(side="left", padx=4)

        dlg.wait_window()
        return result[0]

    def on_step2(self):
        images_dir = self.images_dir_var.get().strip()
        if not images_dir or not os.path.isdir(images_dir):
            messagebox.showerror("エラー", "画像フォルダを正しく指定してください。")
            return
        if not os.path.exists(_JSON_PATH):
            messagebox.showerror(
                "エラー", f"位置情報JSONが見つかりません。先に①を実行してください。\n{_JSON_PATH}")
            return

        csv_text = self._ask_paste_csv()
        if not csv_text:
            return

        out_dir = os.path.join(images_dir, "marked")

        self.step2_status.set("突合・マーク画像生成中…")
        self.update_idletasks()
        try:
            result = match_and_mark.run_from_text(csv_text, _JSON_PATH, images_dir, out_dir)
        except Exception as e:
            traceback.print_exc()
            messagebox.showerror("処理エラー", f"突合処理に失敗しました。\n{e}")
            self.step2_status.set("エラーで中断しました。")
            return

        self._show_summary(result)
        self.step2_status.set(
            f"完了（{result.get('processed', 0)}/{result.get('total_rows', 0)}件）: {out_dir}")

    def _show_summary(self, result: Dict[str, Any]):
        lines = [
            f"処理件数: {result.get('processed', 0)} / {result.get('total_rows', 0)} 行",
        ]
        counts = result.get("field_match_counts", {})
        if counts:
            lines.append("フィールド別マッチ件数:")
            for field, n in counts.items():
                lines.append(f"  {field}: {n}")
        unmatched = result.get("unmatched", [])
        if unmatched:
            lines.append(f"未マッチあり: {len(unmatched)}行")
            for u in unmatched[:5]:
                lines.append(f"  Index {u['index']}: {', '.join(u['fields'])}")
            if len(unmatched) > 5:
                lines.append(f"  …他 {len(unmatched) - 5} 行")
        skipped = result.get("skipped_no_image", [])
        if skipped:
            lines.append(f"画像が見つからずスキップ: {skipped}")
        messagebox.showinfo("突合結果", "\n".join(lines))


if __name__ == "__main__":
    app = ReceiptApp()
    app.mainloop()
