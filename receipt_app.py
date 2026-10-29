# -*- coding: utf-8 -*-
"""
receipt_app.py

領収書OCR → Claude → CSV突合マーク → 会計ソフト仕訳インポートtxt作成 デスクトップアプリ

ワークフロー:
  ⓪ 事前準備: 過去仕訳データ（siwake.txt）をフィルタ・重複排除し、siwake_filtered.txt を作成
  画像フォルダ: ①②共通の領収書画像フォルダを指定
  ① OCR実行 : 画像群をPaddleOCRで読み取り、プロンプト込みJSONを作成 → クリップボードにコピー & claude.ai を開く
  ② CSV突合 : Claudeが出力したCSVとOCR結果JSONを突合し、画像上にマーキング
  ③ 仕訳作成 : 突合後CSVとフィルタ済み仕訳を突き合わせ、会計取込用仕訳データ（siwake_matched.txt）を作成
              マッチした行はCSVから削除され、未マッチ行のみが元CSVに上書き保存される
"""

import sys
sys.stdout.reconfigure(encoding='utf-8', errors='replace')

import os
import json
import queue
import threading
import traceback
import webbrowser
from typing import Any, Dict, List, Optional

import tkinter as tk
from tkinter import ttk, filedialog, messagebox

import match_and_mark

_BASE_DIR = os.path.dirname(os.path.abspath(__file__))
_PROMPT_TEMPLATE_PATH = os.path.join(_BASE_DIR, "claude_prompt_template.txt")
_DEFAULT_JSON_PATH = os.path.join(_BASE_DIR, "ocr_positions.json")
_JSON_PATH = _DEFAULT_JSON_PATH
_DEFAULT_MODELS_DIR = os.path.join(_BASE_DIR, "models")
_DEFAULT_SIWAKE_PATH = os.path.join(_BASE_DIR, "siwake.txt")
_DEFAULT_FILTERED_PATH = os.path.join(_BASE_DIR, "siwake_filtered.txt")
_DEFAULT_MATCHED_PATH = os.path.join(_BASE_DIR, "siwake_matched.txt")
_DEFAULT_CSV_PATH = os.path.join(_BASE_DIR, "receipts_accounting.csv")

_IMAGE_EXTS = {".jpg", ".jpeg", ".png", ".bmp", ".tif", ".tiff"}


class ReceiptApp(tk.Tk):

    def __init__(self):
        super().__init__()
        self.title("領収書OCR → Claude → 会計仕訳作成")
        self.resizable(False, False)

        self._extractor = None  # OcrPositionExtractor（遅延import・遅延初期化）
        self._extractor_model_dir = None  # 初期化に使ったモデルフォルダ（切替検知用）

        self.siwake_path_var = tk.StringVar(
            value=_DEFAULT_SIWAKE_PATH if os.path.exists(_DEFAULT_SIWAKE_PATH) else ""
        )
        self.filtered_path_var = tk.StringVar(
            value=_DEFAULT_FILTERED_PATH if os.path.exists(_DEFAULT_FILTERED_PATH) else ""
        )
        self.matched_path_var = tk.StringVar(value=_DEFAULT_MATCHED_PATH)

        self.images_dir_var = tk.StringVar()
        self.model_dir_var = tk.StringVar(value=_DEFAULT_MODELS_DIR)
        self.csv_path_var = tk.StringVar(
            value=_DEFAULT_CSV_PATH if os.path.exists(_DEFAULT_CSV_PATH) else ""
        )
        self.json_path_var = tk.StringVar(value=_DEFAULT_JSON_PATH)

        self._build_ui()

    # ──────────────────────────────
    # UI構築
    # ──────────────────────────────
    def _build_ui(self):
        pad = {"padx": 8, "pady": 4}

        # ── ⓪ 事前準備: 過去仕訳フィルタ ──
        step0 = ttk.LabelFrame(self, text="⓪ 事前準備: 過去仕訳データフィルタ（重複排除）")
        step0.pack(fill="x", **pad)

        row0_1 = ttk.Frame(step0)
        row0_1.pack(fill="x", padx=6, pady=(6, 2))
        ttk.Label(row0_1, text="過去仕訳TXT:", width=13).pack(side="left")
        ttk.Entry(row0_1, textvariable=self.siwake_path_var, width=48).pack(side="left", fill="x", expand=True, padx=(6, 0))
        ttk.Button(row0_1, text="参照…", command=self._browse_siwake_path).pack(side="left", padx=(6, 0))

        row0_2 = ttk.Frame(step0)
        row0_2.pack(fill="x", padx=6, pady=(2, 4))
        self.step0_btn = ttk.Button(row0_2, text="⓪ 過去仕訳フィルタ実行", command=self.on_step0)
        self.step0_btn.pack(side="left")

        self.step0_status = tk.StringVar(value="未実行（初回または過去仕訳更新時に実行）")
        ttk.Label(step0, textvariable=self.step0_status, foreground="#555555").pack(
            anchor="w", padx=6, pady=(0, 6))

        # ── 画像フォルダ（①②共通） ──
        common = ttk.LabelFrame(self, text="画像フォルダ（①②共通）")
        common.pack(fill="x", **pad)
        row = ttk.Frame(common)
        row.pack(fill="x", padx=6, pady=6)
        ttk.Entry(row, textvariable=self.images_dir_var, width=60).pack(side="left", fill="x", expand=True)
        ttk.Button(row, text="参照…", command=self._browse_images_dir).pack(side="left", padx=(6, 0))

        # ── ① OCR実行 ──
        step1 = ttk.LabelFrame(self, text="① OCR実行 → JSON作成 → Claudeへ")
        step1.pack(fill="x", **pad)

        row1 = ttk.Frame(step1)
        row1.pack(fill="x", padx=6, pady=(6, 2))
        ttk.Label(row1, text="モデルフォルダ:", width=13).pack(side="left")
        ttk.Entry(row1, textvariable=self.model_dir_var, width=48).pack(side="left", fill="x", expand=True, padx=(6, 0))
        ttk.Button(row1, text="参照…", command=self._browse_model_dir).pack(side="left", padx=(6, 0))

        row2 = ttk.Frame(step1)
        row2.pack(fill="x", padx=6, pady=4)
        self.step1_btn = ttk.Button(row2, text="① OCR実行", command=self.on_step1)
        self.step1_btn.pack(side="left")
        self.step1_progress = ttk.Progressbar(row2, mode="determinate")
        self.step1_progress.pack(side="left", fill="x", expand=True, padx=(8, 0))

        self.step1_status = tk.StringVar(value="未実行")
        ttk.Label(step1, textvariable=self.step1_status, foreground="#555555").pack(
            anchor="w", padx=6, pady=(0, 6))

        # ── ② CSV突合・マーク画像生成 ──
        step2 = ttk.LabelFrame(self, text="② CSV突合 → マーク画像生成")
        step2.pack(fill="x", **pad)

        row3 = ttk.Frame(step2)
        row3.pack(fill="x", padx=6, pady=(6, 2))
        ttk.Label(row3, text="CSVファイル:", width=13).pack(side="left")
        ttk.Entry(row3, textvariable=self.csv_path_var, width=48).pack(side="left", fill="x", expand=True, padx=(6, 0))
        ttk.Button(row3, text="参照…", command=self._browse_csv_path).pack(side="left", padx=(6, 0))

        row_json = ttk.Frame(step2)
        row_json.pack(fill="x", padx=6, pady=(2, 6))
        ttk.Label(row_json, text="OCR結果JSON:", width=13).pack(side="left")
        ttk.Entry(row_json, textvariable=self.json_path_var, width=48).pack(side="left", fill="x", expand=True, padx=(6, 0))
        ttk.Button(row_json, text="参照…", command=self._browse_json_path).pack(side="left", padx=(6, 0))

        row4 = ttk.Frame(step2)
        row4.pack(fill="x", padx=6, pady=(0, 6))
        ttk.Button(row4, text="② 突合・マーク実行", command=self.on_step2).pack(side="left")

        self.step2_status = tk.StringVar(value="未実行")
        ttk.Label(step2, textvariable=self.step2_status, foreground="#555555").pack(
            anchor="w", padx=6, pady=(0, 6))

        # ── ③ 突合後CSV → インポートtxt作成 ──
        step3 = ttk.LabelFrame(self, text="③ 突合後CSV → インポートtxtファイル作成")
        step3.pack(fill="x", **pad)

        row3_1 = ttk.Frame(step3)
        row3_1.pack(fill="x", padx=6, pady=(6, 2))
        ttk.Label(row3_1, text="フィルタ済仕訳:", width=13).pack(side="left")
        ttk.Entry(row3_1, textvariable=self.filtered_path_var, width=48).pack(side="left", fill="x", expand=True, padx=(6, 0))
        ttk.Button(row3_1, text="参照…", command=self._browse_filtered_path).pack(side="left", padx=(6, 0))

        row3_2 = ttk.Frame(step3)
        row3_2.pack(fill="x", padx=6, pady=(2, 6))
        self.step3_btn = ttk.Button(row3_2, text="③ インポートtxt作成", command=self.on_step3)
        self.step3_btn.pack(side="left")

        self.step3_status = tk.StringVar(value="未実行（マッチ行を仕訳txtに出力し、元CSVから削除）")
        ttk.Label(step3, textvariable=self.step3_status, foreground="#555555").pack(
            anchor="w", padx=6, pady=(0, 6))

    def _browse_siwake_path(self):
        f = filedialog.askopenfilename(
            title="過去仕訳TXTファイル（siwake.txt）を選択",
            filetypes=[("テキストファイル", "*.txt"), ("すべてのファイル", "*.*")],
        )
        if f:
            self.siwake_path_var.set(f)

    def _browse_filtered_path(self):
        f = filedialog.askopenfilename(
            title="フィルタ済み仕訳ファイルを選択",
            filetypes=[("テキストファイル", "*.txt"), ("すべてのファイル", "*.*")],
        )
        if f:
            self.filtered_path_var.set(f)

    def _browse_images_dir(self):
        d = filedialog.askdirectory(title="画像フォルダを選択")
        if d:
            self.images_dir_var.set(d)

    def _browse_model_dir(self):
        d = filedialog.askdirectory(title="モデルフォルダを選択")
        if d:
            self.model_dir_var.set(d)

    def _browse_csv_path(self):
        f = filedialog.askopenfilename(
            title="Claudeが出力したCSVファイルを選択",
            filetypes=[("CSVファイル", "*.csv"), ("すべてのファイル", "*.*")],
        )
        if f:
            self.csv_path_var.set(f)

    def _browse_json_path(self):
        f = filedialog.askopenfilename(
            title="OCR結果のJSONファイルを選択",
            filetypes=[("JSONファイル", "*.json"), ("すべてのファイル", "*.*")],
        )
        if f:
            self.json_path_var.set(f)

    # ──────────────────────────────
    # ① OCR実行
    # ──────────────────────────────
    def _get_extractor(self, model_dir: str, status_queue: Optional["queue.Queue"] = None):
        """PaddleOCRはモデルパスを import 時の環境変数でしか読まないため、
        ユーザーがモデルフォルダを指定・変更できるよう ocr_positions の import 自体を遅延させる。
        一度初期化した後にモデルフォルダを変更した場合は、アプリの再起動が必要。

        バックグラウンドスレッドから呼ばれる場合があるため、ここではUIに直接触れず、
        状態通知は status_queue 経由（未指定時のみメインスレッド直接更新）で行う。
        """
        if self._extractor is not None:
            return self._extractor

        os.environ["PADDLE_PDX_CACHE_HOME"] = model_dir
        global OcrPositionExtractor, save_json
        from ocr_positions import OcrPositionExtractor, save_json  # noqa: F401

        if status_queue is not None:
            status_queue.put(("status", "PaddleOCR 初期化中…（初回のみ時間がかかります）"))
        else:
            self.step1_status.set("PaddleOCR 初期化中…（初回のみ時間がかかります）")
            self.update_idletasks()
        self._extractor = OcrPositionExtractor()
        self._extractor_model_dir = model_dir
        return self._extractor

    def _rename_images_sequentially(self, images_dir: str, image_paths: List[str]) -> List[str]:
        """OCR処理前に画像ファイルを 1,2,3... の連番にリネームする（拡張子は維持）。
        一時名を経由してからリネームすることで、既存のファイル名との衝突を避ける。
        """
        temp_entries = []
        for i, path in enumerate(image_paths, start=1):
            ext = os.path.splitext(path)[1]
            temp_path = os.path.join(images_dir, f"__renaming_{i}{ext}")
            os.rename(path, temp_path)
            temp_entries.append((temp_path, ext))

        final_paths = []
        for i, (temp_path, ext) in enumerate(temp_entries, start=1):
            final_path = os.path.join(images_dir, f"{i}{ext}")
            os.rename(temp_path, final_path)
            final_paths.append(final_path)
        return final_paths

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

        if self._extractor is not None and self._extractor_model_dir != model_dir:
            messagebox.showwarning(
                "モデルフォルダ",
                "モデルフォルダの変更を反映するにはアプリの再起動が必要です。\n"
                f"現在使用中: {self._extractor_model_dir}"
            )

        try:
            image_paths = self._rename_images_sequentially(images_dir, image_paths)
        except Exception as e:
            traceback.print_exc()
            messagebox.showerror("リネームエラー", f"画像ファイルのリネームに失敗しました。\n{e}")
            return

        self.step1_btn.config(state="disabled")
        self.step1_progress["maximum"] = len(image_paths)
        self.step1_progress["value"] = 0
        self.step1_status.set(f"OCR準備中…（{len(image_paths)}枚）")

        self._step1_queue: "queue.Queue" = queue.Queue()
        threading.Thread(
            target=self._step1_worker,
            args=(model_dir, image_paths),
            daemon=True,
        ).start()
        self.after(5000, self._poll_step1_queue)

    def _step1_worker(self, model_dir: str, image_paths: List[str]):
        """①のOCRループ本体。PaddleOCRの処理はCPU/IOで数秒〜数十秒かかるため、
        メインスレッド（UI）をブロックしないよう別スレッドで実行する。
        UI操作は一切行わず、進捗はキュー経由でメインスレッドに通知する。
        """
        q = self._step1_queue
        try:
            extractor = self._get_extractor(model_dir, status_queue=q)
        except Exception as e:
            traceback.print_exc()
            q.put(("error", f"PaddleOCRの初期化に失敗しました。\n{e}"))
            return

        images_result = []
        for i, path in enumerate(image_paths, start=1):
            q.put(("progress", i, os.path.basename(path)))
            try:
                rec = extractor.process_one(i, path)
            except Exception as e:
                traceback.print_exc()
                rec = {"index": i, "file_name": os.path.basename(path),
                       "width": None, "height": None, "items": [], "error": str(e)}
            images_result.append(rec)

        q.put(("done", images_result))

    def _poll_step1_queue(self):
        q = self._step1_queue
        try:
            while True:
                msg = q.get_nowait()
                kind = msg[0]
                if kind == "status":
                    self.step1_status.set(msg[1])
                elif kind == "progress":
                    i, name = msg[1], msg[2]
                    self.step1_progress["value"] = i
                    total = int(self.step1_progress["maximum"])
                    self.step1_status.set(f"OCR処理中 {i}/{total}: {name}")
                elif kind == "error":
                    self.step1_btn.config(state="normal")
                    messagebox.showerror("初期化エラー", msg[1])
                    self.step1_status.set("エラーで中断しました。")
                    return
                elif kind == "done":
                    self.step1_btn.config(state="normal")
                    self._finish_step1(msg[1])
                    return
        except queue.Empty:
            pass
        self.after(5000, self._poll_step1_queue)

    def _finish_step1(self, images_result: List[Dict[str, Any]]):
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
        json_path = self.json_path_var.get().strip() or _DEFAULT_JSON_PATH
        try:
            save_json(data, json_path)
            self.json_path_var.set(json_path)
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
            f"完了（{len(images_result)}枚）: {json_path}（プロンプト込みJSONをクリップボードにコピー済み）")

    def _copy_json_to_clipboard(self, data: Dict[str, Any]):
        """プロンプト（instructions）とOCR結果（images）を1つに含んだJSONを
        クリップボードにコピーする。claude.aiには貼り付けるだけでよい。

        JSON内に埋め込まれた指示（instructions）だけだと、チャット本文が空のまま
        貼り付けた場合にClaude側が「実行してよいか」確認を挟むことがあるため、
        貼り付けた瞬間にチャット本文として見える明示的な一行を先頭に添えて、
        確認なしで一発でCSVが出力されるようにする。
        """
        json_text = json.dumps(data, ensure_ascii=False, indent=2)
        lead_in = (
            "以下のJSONの instructions フィールドに従って、"
            "CSVファイルとして出力してください。"
            "確認や質問は不要です。\n\n"
        )
        full_text = lead_in + json_text
        self.clipboard_clear()
        self.clipboard_append(full_text)
        self.update()

    # ──────────────────────────────
    # ② CSV参照 → 突合 → マーク画像生成
    # ──────────────────────────────
    def on_step2(self):
        images_dir = self.images_dir_var.get().strip()
        if not images_dir or not os.path.isdir(images_dir):
            messagebox.showerror("エラー", "画像フォルダを正しく指定してください。")
            return

        json_path = self.json_path_var.get().strip()
        if not json_path or not os.path.isfile(json_path):
            messagebox.showerror(
                "エラー",
                f"OCR結果JSONファイルが見つかりません。先に①を実行するか、正しいファイルを指定してください。\n{json_path}"
            )
            return

        csv_path = self.csv_path_var.get().strip()
        if not csv_path or not os.path.isfile(csv_path):
            messagebox.showerror("エラー", "Claudeが出力したCSVファイルを正しく指定してください。")
            return

        out_dir = os.path.join(images_dir, "marked")

        self.step2_status.set("突合・マーク画像生成中…")
        self.update_idletasks()
        try:
            result = match_and_mark.run(csv_path, json_path, images_dir, out_dir)
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
        mismatches = result.get("index_filename_mismatch", [])
        if mismatches:
            lines.append(f"ファイル名とIndexの不一致: {len(mismatches)}件（画像フォルダが①実行時と異なる可能性）")
            for m in mismatches[:5]:
                lines.append(f"  Index {m['index']}: JSON記載={m['expected']} / 実ファイル={m['found']}")
            if len(mismatches) > 5:
                lines.append(f"  …他 {len(mismatches) - 5} 件")

        skipped = result.get("skipped_no_image", [])
        if skipped:
            lines.append(f"画像が見つからずスキップ: {skipped}")
        messagebox.showinfo("突合結果", "\n".join(lines))

    # ──────────────────────────────
    # ⓪ 過去仕訳データフィルタ（重複排除）
    # ──────────────────────────────
    def on_step0(self):
        siwake_path = self.siwake_path_var.get().strip()
        if not siwake_path or not os.path.isfile(siwake_path):
            messagebox.showerror("エラー", "過去仕訳TXTファイル（siwake.txt）を正しく指定してください。")
            return

        out_path = self.filtered_path_var.get().strip() or _DEFAULT_FILTERED_PATH

        self.step0_btn.config(state="disabled")
        self.step0_status.set("フィルタ・重複排除処理中…")

        def _worker():
            try:
                import filter_siwake
                res = filter_siwake.run_filter_siwake(
                    src=siwake_path,
                    dst=out_path,
                    progress_cb=lambda msg: self.step0_status.set(msg),
                )
                self.after(0, lambda: self._finish_step0(res))
            except Exception as e:
                traceback.print_exc()
                self.after(0, lambda: self._error_step0(str(e)))

        threading.Thread(target=_worker, daemon=True).start()

    def _finish_step0(self, res: dict):
        self.step0_btn.config(state="normal")
        self.filtered_path_var.set(res["dst"])
        self.siwake_path_var.set("")
        self.step0_status.set(f"完了（{res['deduped']}件）: {os.path.basename(res['dst'])}")
        msg = (
            f"過去仕訳フィルタ完了:\n"
            f"  元データ: {res['total']} 件\n"
            f"  貸方1101フィルタ後: {res['filtered']} 件\n"
            f"  重複排除後: {res['deduped']} 件\n\n"
            f"出力先: {res['dst']}"
        )
        messagebox.showinfo("事前準備完了", msg)

    def _error_step0(self, err: str):
        self.step0_btn.config(state="normal")
        self.step0_status.set("エラーで中断しました。")
        messagebox.showerror("エラー", f"過去仕訳フィルタ処理に失敗しました。\n{err}")

    # ──────────────────────────────
    # ③ 突合後CSV → インポートtxt作成
    # ──────────────────────────────
    def on_step3(self):
        csv_path = self.csv_path_var.get().strip()
        if not csv_path or not os.path.isfile(csv_path):
            if os.path.isfile(_DEFAULT_CSV_PATH):
                csv_path = _DEFAULT_CSV_PATH
                self.csv_path_var.set(csv_path)
            else:
                messagebox.showerror("エラー", "対象のCSVファイル（②で突合したCSV等）を指定してください。")
                return

        filtered_path = self.filtered_path_var.get().strip()
        if not filtered_path or not os.path.isfile(filtered_path):
            messagebox.showerror("エラー", "フィルタ済み仕訳ファイルが見つかりません。先に⓪を実行してください。")
            return

        out_txt_path = self.matched_path_var.get().strip() or _DEFAULT_MATCHED_PATH

        self.step3_btn.config(state="disabled")
        self.step3_status.set("インポートtxt作成処理中…")

        def _worker():
            try:
                import match_receipts
                res = match_receipts.run_match_receipts(
                    csv_path=csv_path,
                    siwake_path=filtered_path,
                    out_txt_path=out_txt_path,
                    progress_cb=lambda msg: self.step3_status.set(msg),
                )
                self.after(0, lambda: self._finish_step3(res))
            except Exception as e:
                traceback.print_exc()
                self.after(0, lambda: self._error_step3(str(e)))

        threading.Thread(target=_worker, daemon=True).start()

    def _finish_step3(self, res: dict):
        self.step3_btn.config(state="normal")
        msg = (
            f"仕訳データ作成完了:\n\n"
            f"【突合結果】\n"
            f"  対象CSV件数: {res['total']} 件\n"
            f"  マッチ件数: {res['matched']} 件（完全一致: {res['exact']} 件, 部分一致: {res['fuzzy']} 件）\n"
            f"  未マッチ件数: {res['unmatched']} 件\n\n"
            f"【出力内容】\n"
            f"  -> 会計取込用仕訳: {res['out_txt_path']}\n"
            f"  -> 未マッチCSV（上書き）: {res['csv_path']}\n"
            f"     （マッチした {res['matched']} 件はCSVから削除されました）"
        )
        self.step3_status.set(
            f"完了: マッチ {res['matched']} 件（仕訳出力） / 残り {res['unmatched']} 件（CSV上書き）"
        )
        messagebox.showinfo("インポートtxt作成完了", msg)

    def _error_step3(self, err: str):
        self.step3_btn.config(state="normal")
        self.step3_status.set("エラーで中断しました。")
        messagebox.showerror("エラー", f"インポートtxt作成処理に失敗しました。\n{err}")


if __name__ == "__main__":
    app = ReceiptApp()
    app.mainloop()