# GLM-002 — независимые группы PK/UNIQUE и бизнес-ключей

Состояние: **запланирована, не реализована**. Следует за согласованием группового metadata-контракта GLM-001.

Основание: `Validator.row` в `validation.py:98` собирает все поля, имеющие BK/PK/UK, в один общий tuple. Два независимых UNIQUE(a) и UNIQUE(b) не равны UNIQUE(a,b); дубликат a при разных b может быть скрыт. Дефект независимо воспроизведён при подготовке передачи. Флаги YAML без grouping не доказывают конкретный физический constraint.

Цель: хранить и проверять **каждую** исходную PK/UNIQUE группу отдельно на диске, а логический BK модели показывать с явно выбранным grain. Не выводить группы из похожих имён и не перенумеровывать quantities/flags из-за ошибочной PK-метки модели.

Приёмка: независимые UNIQUE(a)/UNIQUE(b); composite UNIQUE(a,b); PK с NULL; PostgreSQL NULL semantics, включая явное решение для NULLS NOT DISTINCT; канонические numeric/UUID; before/after duplicates по группе; byte-budget; manifest roundtrip; все старые тесты/EXE smoke. Unsupported semantics отклонять явно, а не выдавать PASS.

Ownership и разрешённые файлы определяет planner следующего этапа. Не считать эту карточку готовой реализацией или закрытым issue.
