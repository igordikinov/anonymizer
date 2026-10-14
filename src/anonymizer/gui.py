from __future__ import annotations

import os
import queue
import threading
import tkinter as tk
from datetime import datetime
from pathlib import Path
from tkinter import filedialog, messagebox, ttk

from . import __version__
from .errors import AnonymizerError, Cancelled
from .help_text import show_help
from .mapping import INDUSTRIES
from .pipeline import Options, PgOptions, run


class Application:
    def __init__(self, root):
        self.root, self.events, self.cancel = root, queue.Queue(maxsize=20), threading.Event()
        self.running, self.pending_close = False, False
        root.title("SNP Anonymizer")
        root.geometry("1030x790")
        root.minsize(870, 720)
        root.configure(bg="#f4f6fa")
        style = ttk.Style(root)
        style.theme_use("clam")
        style.configure("TFrame", background="#f4f6fa")
        style.configure("Card.TFrame", background="white")
        style.configure("TLabel", background="#f4f6fa", foreground="#20304b", font=("Segoe UI", 10))
        style.configure("Card.TLabel", background="white")
        style.configure("Title.TLabel", font=("Segoe UI", 23, "bold"))
        style.configure("Sub.TLabel", foreground="#69758a")
        style.configure("Heading.TLabel", background="white", font=("Segoe UI", 12, "bold"))
        style.configure("TButton", font=("Segoe UI", 10), padding=(14, 8))
        style.configure("Primary.TButton", background="#315ed0", foreground="white", font=("Segoe UI", 11, "bold"))
        style.map("Primary.TButton", background=[("active", "#254bb0"), ("disabled", "#a8b4cf")])
        style.configure("TEntry", padding=7, font=("Segoe UI", 10))
        style.configure("TCombobox", padding=5)
        style.configure("Horizontal.TProgressbar", background="#315ed0", troughcolor="#e5eaf3")
        outer = ttk.Frame(root, padding=26)
        outer.pack(fill="both", expand=True)
        ttk.Label(outer, text="SNP Anonymizer", style="Title.TLabel").pack(side="left")
        # Справка доступна и во время обработки, поэтому вне self.controls.
        ttk.Button(outer, text="?", width=3, command=lambda: show_help(root, __version__)).pack(side="right")
        root.bind("<F1>", lambda event: show_help(root, __version__))
        ttk.Label(outer, text="Клиентские данные → обезличенный набор для демо и анализа", style="Sub.TLabel").pack(anchor="w", pady=(4, 18))
        self.source = tk.StringVar()
        self.output = tk.StringVar()
        self.project = tk.StringVar()
        self.industry = tk.StringVar(value=INDUSTRIES["food"]["title"])
        self.encoding = tk.StringVar(value="UTF-8")
        self.delimiter = tk.StringVar(value="Авто")
        self.schemas = tk.StringVar()
        self.pg_enabled = tk.BooleanVar(value=False)
        self.pg_host, self.pg_port = tk.StringVar(), tk.StringVar(value="5432")
        self.pg_db, self.pg_user = tk.StringVar(), tk.StringVar()
        self.pg_password, self.pg_schema = tk.StringVar(), tk.StringVar(value="public")
        self.mapping_xlsx = tk.StringVar()
        self.status = tk.StringVar(value="Выберите SQL-файл или папку CSV")
        self.result = tk.StringVar(value="Будут созданы dump.sql, inserts.sql, csv/ и report.json")
        self.controls = []

        card = self.card(outer, "1. Источник и результат")
        self.path_row(card, 1, "Исходные данные", self.source, self.pick_source, "SQL…", secondary=("CSV…", self.pick_csv))
        self.path_row(card, 2, "Папка результата", self.output, self.pick_output, "Выбрать…")
        self.path_row(card, 3, "Локальный проект", self.project, self.pick_project, "Открыть…", secondary=("Новый…", self.new_project))
        ttk.Label(card, text="Проект содержит исходные значения и карту замен. Храните его отдельно от выдаваемого пакета.", style="Card.TLabel").grid(row=4, column=0, columnspan=4, sticky="w", pady=(8, 0))
        ttk.Label(card, text="Карта замен в Excel (необязательно)", style="Card.TLabel").grid(row=5, column=0, sticky="w", pady=4)
        entry = ttk.Entry(card, textvariable=self.mapping_xlsx)
        entry.grid(row=5, column=1, sticky="ew", padx=16, pady=4)
        control = ttk.Button(card, text="Файл…", command=self.pick_mapping)
        control.grid(row=5, column=2, columnspan=2, sticky="ew", padx=(6, 0), pady=4)
        self.controls.extend([entry, control])
        ttk.Label(card, text="Файл содержит исходные значения: храните его отдельно, получателям данных не передавайте.", style="Card.TLabel").grid(row=6, column=0, columnspan=4, sticky="w", pady=(8, 0))

        card = self.card(outer, "2. Настройки обезличивания")
        for row, (caption, variable, values) in enumerate([
            ("Отрасль", self.industry, [v["title"] for v in INDUSTRIES.values()]),
            ("Кодировка", self.encoding, ["UTF-8", "Windows-1251"]),
            ("Разделитель CSV", self.delimiter, ["Авто", ";", ",", "Табуляция", "|"]),
        ], start=1):
            ttk.Label(card, text=caption, style="Card.TLabel").grid(row=row, column=0, sticky="w", pady=4)
            box = ttk.Combobox(card, textvariable=variable, values=values, state="readonly", width=35)
            box.grid(row=row, column=1, sticky="ew", padx=(16, 0), pady=4)
            self.controls.append(box)
        ttk.Label(card, text="Схемы SQL", style="Card.TLabel").grid(row=4, column=0, sticky="w", pady=4)
        entry = ttk.Entry(card, textvariable=self.schemas)
        entry.grid(row=4, column=1, sticky="ew", padx=(16, 0), pady=4)
        self.controls.append(entry)
        ttk.Label(card, text="Пусто — все схемы. Несколько схем — через запятую.", style="Card.TLabel").grid(row=5, column=1, sticky="w", padx=(16, 0))
        ttk.Label(card, text="Количества, цены и даты сохраняются, включая JSON. Тексты и бизнес-коды заменяются.\nsys_id не выгружается.", style="Card.TLabel").grid(row=6, column=0, columnspan=2, sticky="w", pady=(12, 0))

        card = self.card(outer, "3. Загрузка в PostgreSQL (необязательно)")
        check = ttk.Checkbutton(card, text="После проверок загрузить датасет в базу (TRUNCATE таблиц датасета + COPY)", variable=self.pg_enabled, command=self.pg_toggle)
        check.grid(row=1, column=0, columnspan=4, sticky="w", pady=(0, 6))
        self.controls.append(check)
        self.pg_frame = ttk.Frame(card, style="Card.TFrame")
        self.pg_frame.grid(row=2, column=0, columnspan=4, sticky="ew")
        self.pg_frame.columnconfigure(1, weight=1)
        self.pg_frame.columnconfigure(3, weight=1)
        pg_rows = [("Хост", self.pg_host, 0, 0), ("Порт", self.pg_port, 0, 2), ("База", self.pg_db, 1, 0),
                   ("Пользователь", self.pg_user, 1, 2), ("Пароль", self.pg_password, 2, 0), ("Целевая схема", self.pg_schema, 2, 2)]
        self.pg_entries = []
        for row, (caption, variable, grid_row, grid_col) in enumerate(pg_rows):
            ttk.Label(self.pg_frame, text=caption, style="Card.TLabel").grid(row=grid_row, column=grid_col, sticky="w", pady=3, padx=(0, 16 if grid_col == 0 else 0))
            entry = ttk.Entry(self.pg_frame, textvariable=variable, show="*" if caption == "Пароль" else "")
            entry.grid(row=grid_row, column=grid_col + 1, sticky="ew", pady=3, padx=(0, 18))
            self.pg_entries.append(entry)
            self.controls.append(entry)
        ttk.Label(self.pg_frame, text="Таблицы датасета должны существовать в целевой схеме. Сбой — полный откат, база не меняется.\nПароль не сохраняется и не попадает в отчёт.", style="Card.TLabel").grid(row=3, column=0, columnspan=4, sticky="w", pady=(8, 0))
        self.pg_toggle()

        footer = ttk.Frame(outer)
        footer.pack(fill="x", pady=(4, 0))
        self.start_button = ttk.Button(footer, text="Обезличить и экспортировать", style="Primary.TButton", command=self.start)
        self.start_button.pack(side="left")
        self.cancel_button = ttk.Button(footer, text="Отмена", command=self.cancel.set, state="disabled")
        self.cancel_button.pack(side="left", padx=10)
        self.open_button = ttk.Button(footer, text="Открыть результат", command=self.open_result, state="disabled")
        self.open_button.pack(side="right")
        self.bar = ttk.Progressbar(outer, maximum=100)
        self.bar.pack(fill="x", pady=(14, 8))
        ttk.Label(outer, textvariable=self.status).pack(anchor="w")
        ttk.Label(outer, textvariable=self.result, style="Sub.TLabel", wraplength=920).pack(anchor="w", pady=5)
        root.protocol("WM_DELETE_WINDOW", self.on_close)
        root.after(100, self.poll)

    def card(self, parent, title):
        frame = ttk.Frame(parent, style="Card.TFrame", padding=18)
        frame.pack(fill="x", pady=(0, 14))
        frame.columnconfigure(1, weight=1)
        ttk.Label(frame, text=title, style="Heading.TLabel").grid(row=0, column=0, columnspan=4, sticky="w", pady=(0, 9))
        return frame

    def path_row(self, parent, row, caption, variable, callback, button, secondary=None):
        ttk.Label(parent, text=caption, style="Card.TLabel").grid(row=row, column=0, sticky="w", pady=4)
        entry = ttk.Entry(parent, textvariable=variable)
        entry.grid(row=row, column=1, sticky="ew", padx=16, pady=4)
        control = ttk.Button(parent, text=button, command=callback)
        control.grid(row=row, column=2, sticky="ew", pady=4)
        self.controls.extend([entry, control])
        if secondary:
            control = ttk.Button(parent, text=secondary[0], command=secondary[1])
            control.grid(row=row, column=3, sticky="ew", padx=(6, 0), pady=4)
            self.controls.append(control)

    def select_source(self, path):
        if path:
            self.source.set(path)
            p = Path(path)
            parent = p.parent
            stamp = datetime.now().strftime("%Y%m%d_%H%M%S")
            self.output.set(str(parent / f"snp_anon_{stamp}"))
            if not self.project.get():
                self.project.set(str(parent / "client.snpanon"))

    def pick_source(self):
        self.select_source(filedialog.askopenfilename(filetypes=[("PostgreSQL SQL", "*.sql")]))

    def pick_csv(self):
        self.select_source(filedialog.askdirectory(title="Папка с CSV"))

    def pick_output(self):
        parent = filedialog.askdirectory(title="Родительская папка результата")
        if parent:
            self.output.set(str(Path(parent) / ("snp_anon_" + datetime.now().strftime("%Y%m%d_%H%M%S"))))

    def pick_project(self):
        path = filedialog.askopenfilename(filetypes=[("Проект SNP Anonymizer", "*.snpanon")])
        if path:
            self.project.set(path)
            try:
                import sqlite3
                with sqlite3.connect(f"file:{Path(path).as_posix()}?mode=ro", uri=True) as db:
                    industry = db.execute("SELECT value FROM settings WHERE name='industry'").fetchone()[0]
                    self.industry.set(INDUSTRIES[industry]["title"])
            except Exception:
                messagebox.showerror("Проект", "Не удалось прочитать настройки проекта.")

    def new_project(self):
        path = filedialog.asksaveasfilename(defaultextension=".snpanon", filetypes=[("Проект SNP Anonymizer", "*.snpanon")])
        if path:
            if Path(path).exists():
                messagebox.showerror("Новый проект", "Выберите новое имя файла; существующий проект не перезаписывается.")
            else:
                self.project.set(path)

    def pick_mapping(self):
        path = filedialog.asksaveasfilename(defaultextension=".xlsx", filetypes=[("Книга Excel", "*.xlsx")])
        if path:
            self.mapping_xlsx.set(path)

    def pg_toggle(self):
        state = "normal" if self.pg_enabled.get() else "disabled"
        for entry in self.pg_entries:
            entry.configure(state=state)

    def pg_options(self):
        if not self.pg_enabled.get():
            return None
        if not all(v.get().strip() for v in [self.pg_host, self.pg_db, self.pg_user]):
            messagebox.showerror("PostgreSQL", "Укажите хост, базу и пользователя для загрузки.")
            raise ValueError("pg")
        if not self.pg_port.get().strip().isdigit():
            messagebox.showerror("PostgreSQL", "Порт должен быть числом.")
            raise ValueError("pg")
        return PgOptions(self.pg_host.get().strip(), int(self.pg_port.get()), self.pg_db.get().strip(),
                         self.pg_user.get().strip(), self.pg_password.get(), self.pg_schema.get().strip() or "public")

    def start(self):
        if not all(v.get().strip() for v in [self.source, self.output, self.project]):
            messagebox.showerror("Параметры", "Выберите исходные данные, новую папку результата и файл проекта.")
            return
        industry = next(k for k, v in INDUSTRIES.items() if v["title"] == self.industry.get())
        try:
            pg = self.pg_options()
        except ValueError:
            return
        options = Options(Path(self.source.get()), Path(self.output.get()), Path(self.project.get()), industry,
                          "cp1251" if self.encoding.get() == "Windows-1251" else "utf-8-sig",
                          {"Авто": None, "Табуляция": "\t"}.get(self.delimiter.get(), self.delimiter.get()),
                          tuple(s.strip() for s in self.schemas.get().split(",") if s.strip()), "public", pg,
                          Path(self.mapping_xlsx.get()) if self.mapping_xlsx.get().strip() else None, True)
        self.running = True
        self.cancel.clear()
        self.start_button.configure(state="disabled")
        self.cancel_button.configure(state="normal")
        self.open_button.configure(state="disabled")
        for control in self.controls:
            control.configure(state="disabled")
        self.result.set("Обработка идёт локально." + (" После проверок датасет будет загружен в PostgreSQL." if pg else " Исходные файлы сохраняются."))

        def progress(stage, amount, message):
            try:
                self.events.put_nowait(("progress", (stage, amount, message)))
            except queue.Full:
                pass

        def worker():
            try:
                report = run(options, progress, self.cancel)
                self.events.put(("done", report))
            except AnonymizerError as error:
                self.events.put(("error", str(error)))
            except Exception:
                self.events.put(("error", "Внутренняя ошибка обработки. Клиентские значения не записаны в журнал."))
        threading.Thread(target=worker, daemon=False).start()

    def poll(self):
        while not self.events.empty():
            kind, data = self.events.get_nowait()
            if kind == "progress":
                stage, amount, message = data
                base, scale = {"import": (0, 35), "transform": (35, 55), "validate": (90, 9), "pg": (95, 4), "mapping": (99, 1), "done": (100, 0)}[stage]
                self.bar["value"] = base + amount * scale
                self.status.set(message)
            else:
                self.running = False
                self.start_button.configure(state="normal")
                self.cancel_button.configure(state="disabled")
                for control in self.controls:
                    control.configure(state="readonly" if isinstance(control, ttk.Combobox) else "normal")
                self.pg_toggle()
                if kind == "done":
                    self.status.set(f"Готово: {data['tables']} таблиц, {data['rows']:,} строк")
                    missing = sum(f.get("status") == "target_not_in_input" or f.get("missing_after", 0) > 0 for f in data["validation"]["foreign_keys"])
                    loaded = f" В PostgreSQL загружено строк: {data['pg_load']['rows']:,} (схема {data['pg_load']['schema']})." if "pg_load" in data else ""
                    self.result.set(f"Проверены доступные связи, бизнес-ключи и сохраняемые поля. Предупреждений по ссылкам: {missing}.{loaded} Отчёт — report.json.")
                    self.open_button.configure(state="normal")
                else:
                    self.status.set("Итоговый пакет не создан")
                    self.result.set(data)
                    if not self.pending_close:
                        messagebox.showerror("Обработка", data)
                if self.pending_close:
                    self.root.destroy()
                    return
        self.root.after(100, self.poll)

    def open_result(self):
        if Path(self.output.get()).is_dir():
            os.startfile(self.output.get())

    def on_close(self):
        if self.running:
            self.pending_close = True
            self.cancel.set()
            self.status.set("Отмена и удаление временных данных…")
        else:
            self.root.destroy()


def launch(smoke=False):
    root = tk.Tk()
    if smoke:
        root.withdraw()
    app = Application(root)
    if smoke:
        root.update_idletasks()
        root.update()
        root.destroy()
    else:
        root.mainloop()
