from __future__ import annotations

import os
import queue
import threading
import tkinter as tk
import tkinter.font as tkfont
from datetime import datetime
from pathlib import Path
from tkinter import filedialog, messagebox, ttk

from . import __version__
from . import ds_tokens as ds
from .errors import AnonymizerError, Cancelled
from .help_text import show_help
from .mapping import INDUSTRIES
from .pipeline import Options, PgOptions, run
from .runlog import RunLog


class Application:
    def __init__(self, root):
        self.root, self.events, self.cancel = root, queue.Queue(maxsize=20), threading.Event()
        self.running, self.pending_close = False, False
        root.title("SNP Anonymizer")
        root.geometry("1030x820")
        root.minsize(870, 640)
        root.configure(bg=ds.BG_CANVAS)
        families = set(tkfont.families(root))
        family = ds.FONT_FAMILY if ds.FONT_FAMILY in families else ds.FONT_FALLBACK
        self.font_caption = (family, ds.CAPTION_SIZE, "bold")
        self.font_heading = (family, -15, "bold")
        self.font_body = (family, -14)
        self.font_secondary = (family, -12)
        self.font_button = (family, -14, "bold")
        style = ttk.Style(root)
        style.theme_use("clam")
        # In.Plan Design System (бандл фронта): Open Sans, бренд #9000ff,
        # поверхности #ffffff/#f5f6f8, границы #eaeaea, фокус полей брендовый.
        style.configure("TFrame", background=ds.BG_CANVAS)
        style.configure("Card.TFrame", background=ds.BG_SURFACE)
        style.configure("TLabel", background=ds.BG_CANVAS, foreground=ds.TEXT_PRIMARY, font=self.font_body)
        style.configure("Card.TLabel", background=ds.BG_SURFACE, foreground=ds.TEXT_PRIMARY)
        style.configure("Caption.TLabel", background=ds.RAIL, foreground=ds.TEXT_ON_BRAND, font=self.font_caption)
        style.configure("Sub.TLabel", background=ds.BG_CANVAS, foreground=ds.TEXT_SECONDARY, font=self.font_secondary)
        style.configure("Hint.TLabel", background=ds.BG_SURFACE, foreground=ds.TEXT_SUB, font=self.font_secondary)
        style.configure("Heading.TLabel", background=ds.BG_SURFACE, foreground=ds.TEXT_PRIMARY, font=self.font_heading)
        # Кнопки — stroked по scp-button фронта: белый фон, обводка #949598,
        # текст чёрный; hover #f5f6f8, pressed #eaeaea. Видимы на любом фоне.
        style.configure("TButton", background=ds.BG_SURFACE, foreground=ds.TEXT_HEADING,
                        font=self.font_body, padding=(ds.SPACE_L, ds.SPACE_S),
                        bordercolor=ds.BORDER_NEUTRAL, lightcolor=ds.BORDER_NEUTRAL,
                        darkcolor=ds.BORDER_NEUTRAL, borderwidth=1)
        style.map("TButton",
                  background=[("active", ds.BG_CANVAS), ("pressed", ds.BG_HOVER), ("disabled", ds.BG_SURFACE)],
                  foreground=[("disabled", ds.TEXT_DISABLED)],
                  bordercolor=[("disabled", ds.BORDER_TERTIARY)],
                  lightcolor=[("disabled", ds.BORDER_TERTIARY)],
                  darkcolor=[("disabled", ds.BORDER_TERTIARY)])
        style.configure("Secondary.TButton", background=ds.BG_SURFACE, foreground=ds.TEXT_HEADING,
                        font=self.font_body, padding=(ds.SPACE_L, ds.SPACE_S),
                        bordercolor=ds.BORDER_NEUTRAL, lightcolor=ds.BORDER_NEUTRAL,
                        darkcolor=ds.BORDER_NEUTRAL, borderwidth=1)
        style.map("Secondary.TButton",
                  background=[("active", ds.BG_CANVAS), ("pressed", ds.BG_HOVER), ("disabled", ds.BG_SURFACE)],
                  foreground=[("disabled", ds.TEXT_DISABLED)],
                  bordercolor=[("disabled", ds.BORDER_TERTIARY)],
                  lightcolor=[("disabled", ds.BORDER_TERTIARY)],
                  darkcolor=[("disabled", ds.BORDER_TERTIARY)])
        # Primary — .scp-btn продукта: заливка #9000ff, белый текст, вес bold, высота 40px.
        style.configure("Primary.TButton", background=ds.BRAND, foreground=ds.TEXT_ON_BRAND,
                        font=self.font_button, padding=(ds.SPACE_L, 10), borderwidth=0,
                        bordercolor=ds.BRAND, lightcolor=ds.BRAND, darkcolor=ds.BRAND)
        style.map("Primary.TButton",
                  background=[("active", ds.BRAND_HOVER), ("pressed", ds.BRAND_ACTIVE), ("disabled", ds.BG_DISABLED)],
                  foreground=[("disabled", ds.TEXT_DISABLED)],
                  bordercolor=[("active", ds.BRAND_HOVER), ("pressed", ds.BRAND_ACTIVE), ("disabled", ds.BORDER_TERTIARY)],
                  lightcolor=[("active", ds.BRAND_HOVER), ("pressed", ds.BRAND_ACTIVE), ("disabled", ds.BORDER_TERTIARY)],
                  darkcolor=[("active", ds.BRAND_HOVER), ("pressed", ds.BRAND_ACTIVE), ("disabled", ds.BORDER_TERTIARY)])
        style.configure("TEntry", padding=7, font=self.font_body, fieldbackground=ds.BG_SURFACE,
                        foreground=ds.TEXT_PRIMARY, bordercolor=ds.BORDER_TERTIARY,
                        lightcolor=ds.BORDER_TERTIARY, darkcolor=ds.BORDER_TERTIARY, insertcolor=ds.TEXT_PRIMARY)
        style.map("TEntry", bordercolor=[("focus", ds.BORDER_BRAND)],
                  lightcolor=[("focus", ds.BORDER_BRAND)], darkcolor=[("focus", ds.BORDER_BRAND)])
        style.configure("TCombobox", padding=5, font=self.font_body, fieldbackground=ds.BG_SURFACE,
                        foreground=ds.TEXT_PRIMARY, bordercolor=ds.BORDER_TERTIARY,
                        lightcolor=ds.BORDER_TERTIARY, darkcolor=ds.BORDER_TERTIARY,
                        arrowcolor=ds.TEXT_SECONDARY, selectbackground=ds.BRAND_600, selectforeground=ds.TEXT_ON_BRAND)
        style.map("TCombobox", fieldbackground=[("readonly", ds.BG_SURFACE)])
        style.configure("Horizontal.TProgressbar", background=ds.BRAND_600, troughcolor=ds.BG_CANVAS,
                        bordercolor=ds.BORDER_TERTIARY, lightcolor=ds.BRAND_600, darkcolor=ds.BRAND_600)
        style.configure("Vertical.TScrollbar", background=ds.BG_CANVAS, troughcolor=ds.BG_SURFACE,
                        bordercolor=ds.BG_SURFACE, arrowcolor=ds.TEXT_SECONDARY)
        style.configure("TCheckbutton", background=ds.BG_SURFACE, foreground=ds.TEXT_PRIMARY, font=self.font_body)
        style.map("TCheckbutton", background=[("active", ds.BG_SURFACE)])
        try:
            icon = tk.PhotoImage(file=Path(__file__).parent / "resources" / "ds" / "app-icon.png")
            self._logo = icon  # держим ссылку: Tk-изображения удаляются сборщиком
            root.iconphoto(True, icon)
        except tk.TclError:
            self._logo = None
        # Чёрная полоса сверху: надпись перенесена сюда, шрифт вдвое мельче титула.
        # Справка — заметная белая кнопка справа на чёрном; доступна и во время обработки.
        bar = tk.Frame(root, height=44, bg=ds.RAIL)
        bar.pack(side="top", fill="x")
        bar.pack_propagate(False)
        self.caption = tk.Label(bar, text="SNP Anonymizer", bg=ds.RAIL, fg=ds.TEXT_ON_BRAND,
                                font=self.font_caption)
        self.caption.pack(side="left", padx=ds.SPACE_XL)
        self.help_button = tk.Button(bar, text="?  Справка   F1", bg=ds.RAIL, fg=ds.TEXT_ON_BRAND,
                                     activebackground=ds.HOVER_ON_DARK, activeforeground=ds.TEXT_ON_BRAND,
                                     relief="flat", bd=0, font=self.font_secondary, cursor="hand2",
                                     command=lambda: show_help(root, __version__))
        self.help_button.pack(side="right", padx=ds.SPACE_L)
        # Тёмный рельс канонического scp-shell (sidebar--closed: 69px) с белой иконкой In.
        rail = tk.Frame(root, width=69, bg=ds.RAIL)
        rail.pack(side="left", fill="y")
        rail.pack_propagate(False)
        try:
            in_logo = tk.PhotoImage(file=Path(__file__).parent / "resources" / "ds" / "header-logo-in.png")
            self._rail_logo = in_logo
            self.rail_logo = tk.Label(rail, image=in_logo, bg=ds.RAIL)
            self.rail_logo.pack(anchor="nw", padx=15, pady=15)
        except tk.TclError:
            self.rail_logo = None
        self.rail = rail
        outer = ttk.Frame(root, padding=ds.SPACE_XL)
        outer.pack(side="left", fill="both", expand=True)
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
        self.skip_error_tables = tk.BooleanVar(value=False)
        self.runlog = RunLog()
        self.status = tk.StringVar(value="Выберите SQL-файл или папку CSV")
        self.result = tk.StringVar(value="Будут созданы dump.sql, inserts.sql, csv/ и report.json")
        self.controls = []

        # Нижняя зона резервируется первой: кнопки запуска видны при любой высоте окна.
        ttk.Label(outer, textvariable=self.result, style="Sub.TLabel", wraplength=920).pack(side="bottom", anchor="w", pady=5)
        ttk.Label(outer, textvariable=self.status).pack(side="bottom", anchor="w")
        self.bar = ttk.Progressbar(outer, maximum=100)
        self.bar.pack(side="bottom", fill="x", pady=(14, 8))
        footer = ttk.Frame(outer)
        footer.pack(side="bottom", fill="x", pady=(4, 0))
        self.start_button = ttk.Button(footer, text="Обезличить и экспортировать", style="Primary.TButton", command=self.start)
        self.start_button.pack(side="left")
        self.cancel_button = ttk.Button(footer, text="Отмена", style="Secondary.TButton", command=self.cancel.set, state="disabled")
        self.cancel_button.pack(side="left", padx=10)
        self.open_button = ttk.Button(footer, text="Открыть результат", style="Secondary.TButton", command=self.open_result, state="disabled")
        self.open_button.pack(side="right")
        self.export_button = ttk.Button(footer, text="Выгрузить журнал (CSV)", style="Secondary.TButton", command=self.export_log, state="disabled")
        self.export_button.pack(side="right", padx=(0, 10))

        # Карточки — в прокручиваемой зоне: при нехватке высоты окна контент
        # доступен колесом/скроллбаром, а кнопки остаются на месте.
        canvas = tk.Canvas(outer, highlightthickness=0, background=ds.BG_CANVAS)
        vscroll = ttk.Scrollbar(outer, orient="vertical", command=canvas.yview)
        canvas.configure(yscrollcommand=vscroll.set)
        vscroll.pack(side="right", fill="y")
        canvas.pack(side="left", fill="both", expand=True)
        cards_holder = ttk.Frame(canvas, style="TFrame")
        canvas_window = canvas.create_window((0, 0), window=cards_holder, anchor="nw")
        cards_holder.bind("<Configure>", lambda event: canvas.configure(scrollregion=canvas.bbox("all")))
        canvas.bind("<Configure>", lambda event: canvas.itemconfigure(canvas_window, width=event.width))

        card = self.card(cards_holder, "1. Источник и результат")
        self.path_row(card, 1, "Исходные данные", self.source, self.pick_source, "SQL…", secondary=("CSV…", self.pick_csv))
        self.path_row(card, 2, "Папка результата", self.output, self.pick_output, "Выбрать…")
        self.path_row(card, 3, "Локальный проект", self.project, self.pick_project, "Открыть…", secondary=("Новый…", self.new_project))
        ttk.Label(card, text="Проект содержит исходные значения и карту замен. Храните его отдельно от выдаваемого пакета.", style="Hint.TLabel").grid(row=4, column=0, columnspan=4, sticky="w", pady=(8, 0))
        ttk.Label(card, text="Карта замен в Excel (необязательно)", style="Card.TLabel").grid(row=5, column=0, sticky="w", pady=4)
        entry = ttk.Entry(card, textvariable=self.mapping_xlsx)
        entry.grid(row=5, column=1, sticky="ew", padx=16, pady=4)
        control = ttk.Button(card, text="Файл…", command=self.pick_mapping)
        control.grid(row=5, column=2, columnspan=2, sticky="ew", padx=(6, 0), pady=4)
        self.controls.extend([entry, control])
        ttk.Label(card, text="Файл содержит исходные значения: храните его отдельно, получателям данных не передавайте.", style="Hint.TLabel").grid(row=6, column=0, columnspan=4, sticky="w", pady=(8, 0))
        skip_check = ttk.Checkbutton(card, text="Пропускать таблицы с ошибками (продолжить обработку остальных; причины — в журнале)",
                                     variable=self.skip_error_tables)
        skip_check.grid(row=7, column=0, columnspan=4, sticky="w", pady=(8, 0))
        self.controls.append(skip_check)

        card = self.card(cards_holder, "2. Настройки обезличивания")
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
        ttk.Label(card, text="Пусто — все схемы. Несколько схем — через запятую.", style="Hint.TLabel").grid(row=5, column=1, sticky="w", padx=(16, 0))
        ttk.Label(card, text="Количества, цены и даты сохраняются, включая JSON. Тексты и бизнес-коды заменяются.\nsys_id не выгружается.", style="Hint.TLabel").grid(row=6, column=0, columnspan=2, sticky="w", pady=(12, 0))

        card = self.card(cards_holder, "3. Загрузка в PostgreSQL (необязательно)")
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
        ttk.Label(self.pg_frame, text="Таблицы датасета должны существовать в целевой схеме. Сбой — полный откат, база не меняется.\nПароль не сохраняется и не попадает в отчёт.", style="Hint.TLabel").grid(row=3, column=0, columnspan=4, sticky="w", pady=(8, 0))
        self.pg_toggle()

        # Колесо мыши прокручивает зону карточек.
        def _wheel(event):
            canvas.yview_scroll(-1 * (event.delta // 120), "units")

        def bind_wheel(widget):
            widget.bind("<MouseWheel>", _wheel)
            for child in widget.winfo_children():
                bind_wheel(child)

        bind_wheel(cards_holder)
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
        self.select_source(filedialog.askopenfilename(filetypes=[
            ("Выгрузка данных", "*.sql *.zip"), ("ZIP-архив", "*.zip"), ("PostgreSQL SQL", "*.sql")]))

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

    def export_log(self):
        path = filedialog.asksaveasfilename(defaultextension=".csv", filetypes=[("CSV", "*.csv")])
        if not path:
            return
        try:
            written = self.runlog.write_csv(path)
            messagebox.showinfo("Журнал", f"Записано записей: {written}.")
        except (OSError, ValueError) as error:
            messagebox.showerror("Журнал", f"Не удалось записать файл ({type(error).__name__}).")

    def pg_toggle(self):
        state = "normal" if self.pg_enabled.get() else "disabled"
        for entry in self.pg_entries:
            entry.configure(state=state)
        # Поля подключения занимают ~10 строк; до включения флажка они скрыты.
        if self.pg_enabled.get():
            self.pg_frame.grid(row=2, column=0, columnspan=4, sticky="ew")
        else:
            self.pg_frame.grid_remove()

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
        self.runlog = RunLog()
        options = Options(Path(self.source.get()), Path(self.output.get()), Path(self.project.get()), industry,
                          "cp1251" if self.encoding.get() == "Windows-1251" else "utf-8-sig",
                          {"Авто": None, "Табуляция": "\t"}.get(self.delimiter.get(), self.delimiter.get()),
                          tuple(s.strip() for s in self.schemas.get().split(",") if s.strip()), "public", pg,
                          Path(self.mapping_xlsx.get()) if self.mapping_xlsx.get().strip() else None, True,
                          self.runlog, self.skip_error_tables.get())
        self.running = True
        self.cancel.clear()
        self.start_button.configure(state="disabled")
        self.cancel_button.configure(state="normal")
        self.open_button.configure(state="disabled")
        self.export_button.configure(state="disabled")
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
        try:
            self._poll_once()
        except tk.TclError:
            return  # окно уже уничтожено; отложенный callback догонять нечего

    def _poll_once(self):
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
                self.export_button.configure(state="normal")
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
                    self.result.set(data + " Журнал прогона доступен кнопкой «Выгрузить журнал (CSV)».")
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
