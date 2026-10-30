# Профиль связности SNP Anonymizer

Профиль объявляет общие идентификаторы и точный охват проверки. Он применяется к исходному каталогу до обезличивания схем. В обходе эталона профиль не включает модель обратно и не отбрасывает дополнительные колонки.

## Выбор и режим выдачи

В GUI выберите «Optimizer v1» или «JSON-файл…». Выбор включает «Требовать проверенную связность»; флажок можно снять явно. «Без профиля» остаётся исходным выбором, требование выключено.

```powershell
.\distrib\SNP-Anonymizer.exe --input C:\data\input --output C:\data\anonymous_new --project C:\private\connected.snpanon --bypass-model --connectivity-profile builtin:optimizer-v1 --require-integrity
```

Внешний профиль задаётся тем же аргументом:

```powershell
.\distrib\SNP-Anonymizer.exe --input C:\data\input --output C:\data\anonymous_new --project C:\private\connected.snpanon --bypass-model --connectivity-profile C:\profiles\connectivity.json --require-integrity
```

В стандартном режиме проверяются объявленные моделью/DDL отношения и применённый профиль. В обходе эталона используется входная структура и явный профиль; `model_applied` остаётся `false`. Без профиля сохраняется прежняя независимая замена по колонкам и непроверенная связность. `--require-integrity` без применимого полного контракта блокирует выдачу.

## Встроенный Optimizer v1

Namespace по умолчанию — исходная схема. Коды разных схем не смешиваются. Внутри схемы общий объект получает одну замену между датасетами; проверки строк и связей разделяются по `datasetid`.

| Домен | Члены `optimizer_*` |
|---|---|
| LOCATION | demand/storage/production/procurement/bom.`location`; transportation.`loc_from`/`loc_to`; procurement.`supplier` |
| PRODUCT | `product` во всех шести бизнес-таблицах |
| RESOURCE | production/storage/transportation.`resource` |
| BOM | production/bom.`bomnum` |
| TRANSPORT_TYPE | transportation.`transport_type` |
| CLIENT | demand.`client` |

`supplier` намеренно объявлен локацией. Клиент и тип транспорта — отдельные домены. BOM-домен согласует коды `bomnum`; полная операция проверяется с объявленными location/product и ключами, а не выводится из одного имени кода.

Профиль проверяет совпадения ролей доменов в namespace/`datasetid`, составные LOCATION/PRODUCT-проекции и ссылки `(datasetid, period)` шести бизнес-таблиц на `optimizer_periods`. Совпадения ролей не объявляют FK на demand: отсутствие спроса у транзитного узла допустимо.

Ключи ниже — BK-контроль сохранения состава групп с `NULLS NOT DISTINCT`, а не добавляемые в БД ограничения UNIQUE:

| Таблица `optimizer_*` | Колонки |
|---|---|
| periods | datasetid, period |
| demand | datasetid, period, location, product, client |
| procurement | datasetid, period, location, product, supplier |
| production | datasetid, period, location, product, bomnum, resource |
| storage | datasetid, period, location, product, resource |
| transportation | datasetid, period, loc_from, loc_to, product, transport_type, resource |
| bom | datasetid, period, location, product, bomnum, input_output |

Исходные группы дублей сохраняются и указываются отдельно. PK/UNIQUE/FK входа проверяются независимо. Пропущенная колонка не подставляется NULL для подтверждения применимости; отсутствующие и системные группы отражаются в coverage с причиной.

## JSON v1

Формат — UTF-8 JSON размером не более 1 МиБ. Совокупные метаданные компиляции ограничены 16 МиБ. Неизвестные атрибуты, повторные идентификаторы и некорректные технические имена отклоняются. Профиль не содержит исходных кодов, приватных соответствий, секретов или путей к клиентским данным. Произвольный SQL/Python не исполняется.

| Поле | Содержание |
|---|---|
| `format` | `snp-anonymizer-connectivity-v1` |
| `profile_id`, `version` | Технический идентификатор и строковая версия |
| `namespace_rule` | `source_schema` (по умолчанию) или явно общий `shared` |
| `domains` | Домен: `id`, `kind`, `equality`, `prefix`, `members` |
| `relations` | Связь: `id`, `kind`, `source`, `target`, `nulls`, `predicate` |
| `keys` | Ключ: `id`, `table`, упорядоченные `columns`, `kind`, `nulls` |
| `required_checks` | Идентификаторы обязательных проверок; домен записывается как `domain:ID` |
| `missing_target_policy` | `not_checked` или `block` |

Член домена задаётся объектом `{"table":"имя","column":"имя"}`. Доступны типы `auto`, `TEXT`, `NUMBER`, `UUID`; `auto` согласует совместимые входные типы. Правила сравнения: `auto`, `exact`, `numeric`, `uuid`, `pg_char`. Префикс — технический ASCII-идентификатор. Числовые идентификаторы получают числовые коды с Decimal, UUID — UUID, текст — латинские коды общей допустимой длины. Несовместимые типы, невместимая сохранённая замена и исчерпанное пространство кодов блокируют преобразование.

В v1 профильные домены поддерживают TEXT/VARCHAR/CHAR, конечные точные NUMERIC/DECIMAL и целые числа, UUID. Для чисел учитываются общие precision/scale и диапазоны всех ролей. Все формы FLOAT/REAL (`float4`, `float8`, `double precision` и другие псевдонимы), JSON/JSONB, TIME и TIMESTAMP/DATE_TIME как профильные идентификаторы не поддерживаются. FLOAT/REAL-идентификатор или явный COLLATE в домене блокирует преобразование с `CHECK_UNSUPPORTED`. Это ограничение не меняет сохранение обычных показателей FLOAT/REAL. CHAR/CHARACTER без указанной длины имеет длину 1; сравнение `pg_char` игнорирует завершающие ASCII-пробелы.

`source` и `target` содержат `members`: список объектов `{"table":"имя","columns":["колонка1","колонка2"]}`. Ширина кортежей обеих сторон совпадает. `projection:"bag"` сохраняет каждое вхождение строки/роли, `projection:"set"` задаёт DISTINCT-проекцию; до проверки отношений проверяется сохранение точного членства её узлов.

Связь имеет `kind:"join"` для сохранения совпадений или `kind:"fk"` для ссылочной проверки. Предикат первой версии — `predicate:"equality"`, по всем упорядоченным компонентам кортежа. `nulls:"sql"` исключает из совпадения NULL-компоненты, `null_safe` допускает NULL-safe равенство, `match_simple` задаёт FK-семантику. Пустая TEXT-строка остаётся значением. Вычисляемые даты и другие предикаты не вычисляются и получают `UNSUPPORTED`; обязательная неподдерживаемая проверка не даёт проверенную выдачу.

Отношения и ключи с FLOAT/REAL, JSON/JSONB, TIME/TIMESTAMP/DATE_TIME или явно объявленным COLLATE получают `UNSUPPORTED`: точная семантика PostgreSQL для них в v1 не воспроизводится. При `--require-integrity` неподдерживаемая обязательная проверка даёт только диагностический отчёт с кодом `CHECK_UNSUPPORTED`, без данных. DATE-равенство нормализует календарную ISO-дату, включая компактную запись, только для сравнения; исходные лексемы дат в экспортах сохраняются.

У ключей `kind` — `pk`, `unique` или `bk`; `nulls` — `distinct` либо `not_distinct`. Группы проверяются независимо, включая составные. Для независимых UNIQUE(a) и UNIQUE(b) нужны две записи; UNIQUE(a,b) их не заменяет.

Пример для двух технических таблиц `nodes(datasetid, location)` и `edges(datasetid, loc_from)`:

```json
{
  "format": "snp-anonymizer-connectivity-v1",
  "profile_id": "custom-v1",
  "version": "1",
  "namespace_rule": "source_schema",
  "domains": [
    {
      "id": "LOCATION",
      "kind": "auto",
      "equality": "auto",
      "prefix": "LOC",
      "members": [
        {"table": "nodes", "column": "location"},
        {"table": "edges", "column": "loc_from"}
      ]
    }
  ],
  "relations": [
    {
      "id": "locations",
      "kind": "join",
      "source": {
        "members": [{"table": "nodes", "columns": ["datasetid", "location"]}],
        "projection": "bag"
      },
      "target": {
        "members": [{"table": "edges", "columns": ["datasetid", "loc_from"]}],
        "projection": "bag"
      },
      "nulls": "sql",
      "predicate": "equality"
    }
  ],
  "keys": [
    {
      "id": "nodes-grain",
      "table": "nodes",
      "columns": ["datasetid", "location"],
      "kind": "bk",
      "nulls": "not_distinct"
    }
  ],
  "required_checks": ["domain:LOCATION", "locations", "nodes-grain"],
  "missing_target_policy": "not_checked"
}
```

## Проект и отчёт

Для профильного режима создайте новый `.snpanon`. Проект v2 хранит отпечаток эффективной семантики профиля, namespace, доменов, сравнения, кодирования и версии компилятора. Перестановка файлов и размер порции не меняют сохранённые назначения. Изменившийся несовместимый контракт не смешивается с картой. Непустой проект v1 не мигрируется; без профиля он продолжает работать.

Результат включает `connectivity-profile.json` и `report.json` v2. Профиль передаётся без приватных привязок исходных схем; клиентские значения и карта замен в отчёт не попадают. Временные номера строк используются только для безопасной диагностики.

Валидатор заново читает фактические CSV, dump и INSERT. Для отношений вычисляются точные `lost` и `added`, для ключей сравнивается состав групп дублей. Общие counts могут совпасть при неверных парах; это всё равно FAIL. Исходные дефекты регистрируются отдельно, инструмент их не исправляет.

| Поле отчёта | Значение |
|---|---|
| `model_applied` | Факт применения эталона; профиль обхода не меняет его на true |
| `connectivity_profile_id/version/hash` | Применённый контракт |
| `relationships_checked`, `keys_checked` | Непустой набор соответствующих проверок целиком исполнен с PASS/FAIL; пропуск или UNSUPPORTED даёт false |
| `integrity_status` | `PASSED`, `FAILED`, `NOT_CHECKED` относительно обязательного охвата |
| `source_integrity_status` | `PASSED`, `WARNINGS`, `NOT_CHECKED` для исходных объявленных отношений |
| `coverage`, `checks`, `limitations` | Охват, отдельные статусы и причины ограничений |
| `publication_mode` | `VERIFIED_INTEGRITY`, `UNVERIFIED_INTEGRITY`; null при diagnostic-only результате |
| `dataset_published` | false, если создан только диагностический отчёт |

Проверенный результат возможен только при определённом непустом обязательном наборе и PASS каждой его проверки. Любой FAIL преобразования блокирует данные независимо от `required_checks` и выбранного режима. Отмена, лимит или ошибка чтения дают `CHECK_INCOMPLETE`, а не PASS. При блокировке создаётся только безопасный `report.json`; частичные CSV/SQL и приватная карта не публикуются. Схемы пропущенных таблиц в отчёте представлены псевдонимами, а не исходными именами.

Все проверки завершаются до опционального PostgreSQL. Файловая публикация и COMMIT PostgreSQL не являются общей транзакцией; обработка неподтверждённого COMMIT сохраняется. Успех связности относится к объявленным отношениям и не доказывает совместимость с произвольной схемой БД, расчёт дат прибытия или выполнимость оптимизатора.
