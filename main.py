"""
CRM системного аналитика — версия 2.0
На основе микро-CRM с занятия 07.09 («Система учёта проекта»).

Что добавлено (ДЗ 09.09 + требования итоговой аттестации):
  • Интервью со стейкхолдером: текстовый протокол (основной режим) и голосовой ввод (бета);
  • Генератор задач: тезисы интервью -> задачи для Frontend / Backend / Architecture / Security;
  • Сотрудники как отдельная сущность, статусы «больничный / отпуск» на период;
  • Автоназначение задач на наименее загруженного доступного сотрудника отдела;
  • Таймер учёта времени и дедлайны у каждой задачи;
  • Контроль качества: проверка задачи, возврат на доработку (баг) с комментарием;
  • KPI сотрудников и отделов: выполнение в срок, качество с первого раза, план/факт по времени;
  • Хранение в базе SQLite (crm.db) вместо JSON-файла.

Запуск: python main.py  ->  http://127.0.0.1:5000
"""
import json
import os
import re
import sqlite3
import sys
import webbrowser
from datetime import datetime, date, timedelta
from threading import Timer

from flask import Flask, render_template, request, jsonify, g, abort
from jinja2 import DictLoader

app = Flask(__name__)
BASE_DIR = os.path.dirname(os.path.abspath(__file__))
DB_FILE = os.environ.get("CRM_DB") or os.path.join(BASE_DIR, "crm.db")
LEGACY_JSON = os.path.join(BASE_DIR, "project_data.json")
PORT = 5000

DEPARTMENTS = ["Frontend", "Backend", "Architecture", "Security"]
DEPT_ICONS = {
    "Frontend": "bi-window",
    "Backend": "bi-hdd-stack",
    "Architecture": "bi-diagram-3",
    "Security": "bi-shield-lock",
}
PRIORITIES = ["высокий", "средний", "низкий"]
PRIORITY_DAYS = {"высокий": 3, "средний": 7, "низкий": 14}  # срок по умолчанию
DEPT_HOURS = {"Frontend": 6, "Backend": 8, "Architecture": 4, "Security": 4}  # оценка по умолчанию, ч
EMP_STATUSES = ["работает", "больничный", "отпуск"]
# Демо-режим для публичной версии (портфолио): база заполняется примером, сбрасывается раз в сутки
# и защищена лимитами от ботов и случайного «засорения»
DEMO_MODE = os.environ.get("CRM_DEMO") == "1"
DEMO_LIMITS = {"text": 8000, "tasks_per_interview": 30, "tasks": 300, "employees": 40, "interviews": 60}
app.config["MAX_CONTENT_LENGTH"] = 256 * 1024

EXAMPLE_INTERVIEW = """Встреча с отделом продаж, стейкхолдер — руководитель направления.
Нужно сделать для дилера личный кабинет с удобным интерфейсом, где на экране видна его персональная скидка.
Менеджер загружает прайс-лист из Excel, а система должна сама пересчитывать оптовую скидку от суммы заказа.
Срочно нужна форма корзины для оформления заказа с мобильного телефона.
Заказ должен автоматически уходить на почту регионального менеджера и выгружаться в 1С.
Доступ в кабинет только по логину и паролю, у каждого дилера должны быть свои права.
Персональные данные дилеров обязательно храним по 152-ФЗ.
Желательно в перспективе выдержать нагрузку до 100 дилеров одновременно.
Нужно предусмотреть резервное копирование базы каждую ночь, сделать до 15.10."""

DEMO_VOICE_INTERVIEW = """Созвон со службой безопасности заказчика.
Нужно добавить двухфакторную авторизацию для администраторов.
Также надо вести журнал аудита всех действий пользователей в системе."""


# ==================== БАЗА ДАННЫХ ====================
SCHEMA = """
CREATE TABLE IF NOT EXISTS employees (
    id          INTEGER PRIMARY KEY AUTOINCREMENT,
    name        TEXT NOT NULL,
    department  TEXT NOT NULL,
    position    TEXT NOT NULL DEFAULT '',
    status      TEXT NOT NULL DEFAULT 'работает',
    absent_from TEXT,
    absent_to   TEXT,
    created_at  TEXT NOT NULL
);
CREATE TABLE IF NOT EXISTS interviews (
    id           INTEGER PRIMARY KEY AUTOINCREMENT,
    title        TEXT NOT NULL,
    stakeholder  TEXT NOT NULL DEFAULT '',
    meeting_date TEXT NOT NULL,
    text         TEXT NOT NULL,
    source       TEXT NOT NULL DEFAULT 'текст',
    created_at   TEXT NOT NULL
);
CREATE TABLE IF NOT EXISTS tasks (
    id               INTEGER PRIMARY KEY AUTOINCREMENT,
    title            TEXT NOT NULL,
    description      TEXT NOT NULL DEFAULT '',
    department       TEXT NOT NULL,
    employee_id      INTEGER REFERENCES employees(id) ON DELETE SET NULL,
    interview_id     INTEGER REFERENCES interviews(id) ON DELETE SET NULL,
    priority         TEXT NOT NULL DEFAULT 'средний',
    deadline         TEXT NOT NULL,
    status           TEXT NOT NULL DEFAULT 'новая',
    estimate_hours   REAL NOT NULL DEFAULT 4,
    spent_seconds    INTEGER NOT NULL DEFAULT 0,
    timer_started_at TEXT,
    returns          INTEGER NOT NULL DEFAULT 0,
    created_at       TEXT NOT NULL,
    completed_at     TEXT
);
CREATE TABLE IF NOT EXISTS meta (
    key   TEXT PRIMARY KEY,
    value TEXT NOT NULL
);
CREATE TABLE IF NOT EXISTS reviews (
    id         INTEGER PRIMARY KEY AUTOINCREMENT,
    task_id    INTEGER NOT NULL REFERENCES tasks(id) ON DELETE CASCADE,
    result     TEXT NOT NULL,
    comment    TEXT NOT NULL DEFAULT '',
    created_at TEXT NOT NULL
);
"""


def get_db():
    if "db" not in g:
        g.db = sqlite3.connect(DB_FILE)
        g.db.row_factory = sqlite3.Row
        g.db.execute("PRAGMA foreign_keys = ON")
    return g.db


@app.before_request
def demo_daily_reset():
    """В демо-версии данные возвращаются к исходным раз в сутки — что бы ни натворили посетители."""
    if not DEMO_MODE:
        return
    db = get_db()
    row = db.execute("SELECT value FROM meta WHERE key = 'seeded_on'").fetchone()
    if not row or row["value"] != today().isoformat():
        seed_demo(db)


def over_demo_limit(db, table, extra=1):
    """True, если в демо-версии таблица переполнится после добавления extra записей."""
    if not DEMO_MODE:
        return False
    count = db.execute(f"SELECT COUNT(*) FROM {table}").fetchone()[0]
    return count + extra > DEMO_LIMITS[table]


DEMO_LIMIT_MESSAGE = "В демо-версии достигнут лимит записей. Нажмите «Сбросить демо-данные» вверху страницы."


@app.teardown_appcontext
def close_db(exc):
    db = g.pop("db", None)
    if db is not None:
        db.close()


def init_db():
    with sqlite3.connect(DB_FILE) as db:
        db.row_factory = sqlite3.Row
        db.executescript(SCHEMA)
        empty = db.execute("SELECT COUNT(*) FROM tasks").fetchone()[0] == 0
        if DEMO_MODE:
            if empty and db.execute("SELECT COUNT(*) FROM employees").fetchone()[0] == 0:
                seed_demo(db)
        elif empty and os.path.exists(LEGACY_JSON):
            import_legacy(db)


def import_legacy(db):
    """Перенос задач из старой версии (project_data.json) в базу — один раз."""
    try:
        with open(LEGACY_JSON, "r", encoding="utf-8") as f:
            data = json.load(f)
    except Exception as e:
        print(f"Не удалось прочитать {LEGACY_JSON}: {e}")
        return
    now = now_iso()
    for t in data.get("tasks", []):
        name = (t.get("employee") or "").strip() or "Без исполнителя"
        row = db.execute("SELECT id FROM employees WHERE name = ?", (name,)).fetchone()
        if row:
            emp_id = row[0]
        else:
            emp_id = db.execute(
                "INSERT INTO employees (name, department, created_at) VALUES (?, ?, ?)",
                (name, "Backend", now),
            ).lastrowid
        done = t.get("status") == "выполнено"
        db.execute(
            "INSERT INTO tasks (title, department, employee_id, deadline, status, created_at, completed_at)"
            " VALUES (?, ?, ?, ?, ?, ?, ?)",
            (t.get("task_name", "Задача"), "Backend", emp_id,
             t.get("deadline", date.today().isoformat()),
             "выполнено" if done else "в работе", now, now if done else None),
        )
    print(f"📥 Перенесено задач из project_data.json: {len(data.get('tasks', []))}")


# ==================== ВСПОМОГАТЕЛЬНОЕ ====================
def now_iso():
    return datetime.now().isoformat(timespec="seconds")


def today():
    return date.today()


def parse_date(s):
    return datetime.strptime(s, "%Y-%m-%d").date()


def employee_available(emp, on=None):
    """Сотрудник доступен, если работает или его отсутствие не приходится на эту дату."""
    on = on or today()
    if emp["status"] == "работает":
        return True
    if emp["absent_from"] and emp["absent_to"]:
        return not (parse_date(emp["absent_from"]) <= on <= parse_date(emp["absent_to"]))
    return False


TASKS_SQL = """
SELECT t.*, e.name AS employee_name, i.title AS interview_title
FROM tasks t
LEFT JOIN employees e ON e.id = t.employee_id
LEFT JOIN interviews i ON i.id = t.interview_id
"""


def task_view(row):
    """Задача + вычисляемые поля: время с учётом идущего таймера, просрочка, «в срок»."""
    t = dict(row)
    spent = t["spent_seconds"]
    if t["timer_started_at"]:
        spent += int((datetime.now() - datetime.fromisoformat(t["timer_started_at"])).total_seconds())
    t["spent_live"] = max(0, spent)
    dl = parse_date(t["deadline"])
    if t["status"] == "выполнено":
        done_day = datetime.fromisoformat(t["completed_at"]).date() if t["completed_at"] else dl
        t["on_time"] = done_day <= dl
        t["overdue_days"] = max(0, (done_day - dl).days)
        t["is_overdue"] = False
    else:
        t["on_time"] = None
        t["is_overdue"] = dl < today()
        t["overdue_days"] = (today() - dl).days if t["is_overdue"] else 0
    return t


def all_tasks(db):
    return [task_view(r) for r in db.execute(TASKS_SQL + " ORDER BY t.id").fetchall()]


def open_load(db):
    """Сколько незавершённых задач у каждого сотрудника."""
    rows = db.execute(
        "SELECT employee_id, COUNT(*) AS n FROM tasks"
        " WHERE status != 'выполнено' AND employee_id IS NOT NULL GROUP BY employee_id"
    )
    return {r["employee_id"]: r["n"] for r in rows}


def pick_employee(emps, load, dept):
    """Автоназначение: доступный сотрудник отдела с наименьшей загрузкой."""
    candidates = [e for e in emps if e["department"] == dept and employee_available(e)]
    if not candidates:
        return None
    return min(candidates, key=lambda e: (load.get(e["id"], 0), e["id"]))["id"]


def resolve_employee(raw, dept, load, emps):
    raw = "" if raw is None else str(raw)
    if raw in ("", "none"):
        return None
    if raw == "auto":
        emp_id = pick_employee(emps, load, dept)
    elif raw.isdigit() and any(e["id"] == int(raw) for e in emps):
        emp_id = int(raw)
    else:
        raise ValueError("Сотрудник не найден")
    if emp_id:
        load[emp_id] = load.get(emp_id, 0) + 1
    return emp_id


def clean_task_payload(d):
    title = (d.get("title") or "").strip()
    if not title:
        raise ValueError("Укажите название задачи")
    dept = d.get("department")
    if dept not in DEPARTMENTS:
        raise ValueError("Неизвестный отдел")
    priority = d.get("priority") if d.get("priority") in PRIORITIES else "средний"
    try:
        deadline = parse_date(d.get("deadline") or "").isoformat()
    except ValueError:
        raise ValueError(f"Неверный срок у задачи «{title[:40]}»")
    try:
        hours = float(d.get("estimate_hours") or DEPT_HOURS[dept])
    except (TypeError, ValueError):
        raise ValueError("Оценка времени должна быть числом")
    if hours <= 0:
        raise ValueError("Оценка времени должна быть больше нуля")
    return title[:200], (d.get("description") or "").strip()[:1000], dept, priority, deadline, min(hours, 999)


def insert_task(db, title, description, dept, employee_id, interview_id, priority, deadline, hours):
    return db.execute(
        "INSERT INTO tasks (title, description, department, employee_id, interview_id, priority,"
        " deadline, estimate_hours, created_at) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)",
        (title, description, dept, employee_id, interview_id, priority, deadline, hours, now_iso()),
    ).lastrowid


def stop_timer(db, t):
    if t["timer_started_at"]:
        elapsed = int((datetime.now() - datetime.fromisoformat(t["timer_started_at"])).total_seconds())
        db.execute(
            "UPDATE tasks SET spent_seconds = spent_seconds + ?, timer_started_at = NULL WHERE id = ?",
            (max(0, elapsed), t["id"]),
        )


def get_task(db, task_id):
    t = db.execute("SELECT * FROM tasks WHERE id = ?", (task_id,)).fetchone()
    if not t:
        abort(404)
    return t


def ok(**kw):
    return jsonify(success=True, **kw)


def fail(message, code=400):
    return jsonify(success=False, error=message), code


# ==================== РАЗБОР ИНТЕРВЬЮ -> ЗАДАЧИ ====================
# Ключевые слова отделов (начало слова, нижний регистр, допускаются регулярные выражения)
DEPT_PATTERNS = {
    "Frontend": [
        r"интерфейс", r"экран", r"кнопк", r"форм[аыуео]\b", r"формой", r"страниц", r"дизайн",
        r"макет", r"в[её]рстк", r"мобильн", r"личн\w* кабинет", r"кабинет", r"ui\b", r"ux\b",
        r"отображ", r"показыва", r"виджет", r"меню", r"цвет", r"иконк", r"адаптив", r"телефон",
        r"сайт", r"витрин", r"карточк", r"фильтр", r"корзин",
    ],
    "Backend": [
        r"баз[аыуе]\b", r"данн", r"сервер", r"api\b", r"интеграц", r"расч[её]т", r"пересчит",
        r"считат", r"хран", r"отч[её]т", r"выгруж", r"выгруз", r"загруж", r"загруз", r"импорт",
        r"экспорт", r"1с\b", r"excel", r"обработ", r"синхрониз", r"рассылк", r"почт", r"e-?mail",
        r"скидк", r"цен[аыуе]\b", r"прайс", r"заказ", r"оплат", r"плат[её]ж", r"sql",
        r"статистик", r"алгоритм",
    ],
    "Architecture": [
        r"архитектур", r"масштаб", r"нагрузк", r"стек", r"инфраструктур", r"микросервис",
        r"облак", r"хостинг", r"разв[её]рт", r"развернут", r"отказоустойч", r"производительн",
        r"резерв", r"docker", r"кластер", r"очеред", r"websocket", r"одновременн", r"пропускн",
        r"технологи", r"платформ",
    ],
    "Security": [
        r"безопасн", r"доступ", r"парол", r"авторизац", r"аутентификац", r"рол(ь|и|ей|ям|ев)",
        r"права\b", r"прав доступа", r"персональн", r"152", r"шифр", r"защит", r"логин", r"аудит",
        r"журнал", r"утечк", r"сертификат", r"ssl", r"https", r"двухфактор", r"блокиров",
    ],
}
# При равенстве баллов выигрывает отдел, стоящий раньше
TIE_ORDER = ["Security", "Frontend", "Architecture", "Backend"]

REQUIREMENT_MARKERS = re.compile(
    r"(нужн|надо|необходим|долж(ен|н)|требу|хоч|хотел|сделать|добавить|реализова|чтобы|важно|"
    r"обязательн|нельзя|следует|предусмотр|настро|подключ|разработ|желательн|срочн|пусть)",
    re.IGNORECASE,
)
LONG_SPLIT = re.compile(
    r"\s+(?=(?:а также|также|и ещё|и еще|ещё|еще|кроме того|нужно|надо|необходимо|хочу|хотим)\b)",
    re.IGNORECASE,
)
FILLERS = [
    "и", "а", "также", "так же", "ещё", "еще", "кроме того", "плюс", "нам", "мне", "очень",
    "обязательно", "срочно", "важно", "желательно", "нужно", "надо", "необходимо", "требуется",
    "хочу", "хотим", "хотелось бы", "следует", "чтобы", "бы",
]
HIGH_PRIORITY = re.compile(r"(срочн|критичн|в первую очередь|обязательн|важн|немедленн|горит)", re.I)
LOW_PRIORITY = re.compile(
    r"(не срочн|потом|желательн|по возможност|в перспектив|втор(ой|ую) (этап|очеред)|позже)", re.I
)
NUM_WORDS = {"один": 1, "одну": 1, "одна": 1, "два": 2, "две": 2, "три": 3, "четыре": 4, "пять": 5}


def split_theses(text):
    """Протокол -> список тезисов-требований."""
    parts = []
    for line in text.splitlines():
        line = re.sub(r"^\s*(?:[-*•–—]+|\d+[.)])\s*", "", line).strip()
        if not line:
            continue
        for sentence in re.split(r"(?<=[.!?;])\s+", line):
            # Голосовой ввод часто без знаков препинания — режем длинную фразу по связкам
            pieces = LONG_SPLIT.split(sentence) if len(sentence.split()) > 15 else [sentence]
            merged = []
            for p in pieces:
                if merged and len(merged[-1].split()) < 3:
                    merged[-1] = f"{merged[-1]} {p}"
                else:
                    merged.append(p)
            for p in merged:
                p = re.sub(r"\s+и$", "", p.strip(" .;!?,\t"))
                if len(p.split()) >= 3:
                    parts.append(p)
    marked = [p for p in parts if REQUIREMENT_MARKERS.search(p)]
    return marked or parts


def classify(text):
    low = text.lower()
    scores = {
        dept: sum(1 for p in patterns if re.search(r"(?<![а-яёa-z0-9])" + p, low))
        for dept, patterns in DEPT_PATTERNS.items()
    }
    best = max(TIE_ORDER, key=lambda d: scores[d])
    if scores[best] == 0:
        return "Backend", False
    return best, True


def detect_priority(text):
    if LOW_PRIORITY.search(text):
        return "низкий"
    if HIGH_PRIORITY.search(text):
        return "высокий"
    return "средний"


def extract_deadline(text, base):
    """Срок из текста: «до 15.10», «15.10.2026», «через 2 недели», «завтра»."""
    m = re.search(r"(?<!\d)(\d{1,2})[./](\d{1,2})(?:[./](\d{2,4}))?(?!\d)", text)
    if m:
        day, month, year = int(m[1]), int(m[2]), m[3]
        y = (int(year) + 2000 if len(year) == 2 else int(year)) if year else base.year
        try:
            result = date(y, month, day)
            if not year and result < base:
                result = date(y + 1, month, day)
            return result
        except ValueError:
            pass
    m = re.search(r"через\s+(?:(\d+|\w+)\s+)?(дн|день|дня|недел|месяц)", text, re.I)
    if m:
        raw = (m[1] or "1").lower()
        n = int(raw) if raw.isdigit() else NUM_WORDS.get(raw, 1)
        unit = m[2].lower()
        days = n * (7 if unit.startswith("недел") else 30 if unit.startswith("месяц") else 1)
        return base + timedelta(days=days)
    if re.search(r"\bзавтра\b", text, re.I):
        return base + timedelta(days=1)
    return None


def make_title(sentence):
    """«Нужно срочно сделать форму…» -> «Сделать форму…»."""
    s = sentence.strip()
    changed = True
    while changed:
        changed = False
        for filler in sorted(FILLERS, key=len, reverse=True):
            m = re.match(rf"{re.escape(filler)}\b[\s,:]*", s, re.IGNORECASE)
            if m and len(s) > m.end():
                s = s[m.end():]
                changed = True
                break
    s = s.strip(" ,:")
    title = s[:1].upper() + s[1:]
    return title if len(title) <= 140 else title[:137] + "…"


# ==================== KPI И КАЧЕСТВО ====================
def kpi_rows(tasks, key):
    """
    KPI = 50% «выполнено в срок» + 30% «принято с первого раза» + 20% «план/факт по времени»,
    минус 5 баллов за каждую открытую просроченную задачу. Считается по выполненным задачам.
    """
    groups = {}
    for t in tasks:
        groups.setdefault(key(t), []).append(t)
    rows = []
    for k, ts in groups.items():
        done = [t for t in ts if t["status"] == "выполнено"]
        overdue_open = sum(1 for t in ts if t["is_overdue"])
        plan = sum(t["estimate_hours"] for t in done)
        fact = sum(t["spent_live"] for t in done) / 3600
        row = dict(
            key=k, total=len(ts), done=len(done), open=len(ts) - len(done),
            overdue_open=overdue_open, returns=sum(t["returns"] for t in ts),
            plan=round(plan, 1), fact=round(fact, 1),
            on_time_pct=None, quality_pct=None, eff_pct=None, kpi=None,
        )
        if done:
            on_time = sum(1 for t in done if t["on_time"]) / len(done)
            first_pass = sum(1 for t in done if t["returns"] == 0) / len(done)
            row["on_time_pct"] = round(100 * on_time)
            row["quality_pct"] = round(100 * first_pass)
            if fact > 0.01:
                eff = plan / fact
                row["eff_pct"] = round(100 * min(eff, 1.5))
                score = 50 * on_time + 30 * first_pass + 20 * min(eff, 1.0)
            else:
                score = (50 * on_time + 30 * first_pass) / 0.8  # таймер не использовался
            row["kpi"] = max(0, round(score) - 5 * overdue_open)
        rows.append(row)
    return sorted(rows, key=lambda r: (r["kpi"] is None, -(r["kpi"] or 0), -r["total"]))


# ==================== ШАБЛОНЫ ====================
LAYOUT = """
<!DOCTYPE html>
<html lang="ru">
<head>
    <meta charset="UTF-8">
    <meta name="viewport" content="width=device-width, initial-scale=1.0">
    <title>{% block title %}CRM аналитика{% endblock %}</title>
    <script>try { document.documentElement.dataset.theme = new URLSearchParams(location.search).get('theme') || localStorage.getItem('sa-theme') || 'classic'; } catch (e) { document.documentElement.dataset.theme = 'classic'; }</script>
    <link href="https://cdn.jsdelivr.net/npm/bootstrap@5.3.2/dist/css/bootstrap.min.css" rel="stylesheet">
    <link href="https://cdn.jsdelivr.net/npm/bootstrap-icons@1.11.1/font/bootstrap-icons.css" rel="stylesheet">
    <style>
        /* Темы оформления (общие с витриной портфолио): classic, graphite, bordeaux, violet */
        :root, :root[data-theme="classic"] {
            --page-bg: linear-gradient(135deg, #e9edf2 0%, #d9dfe7 100%);
            --head-bg: linear-gradient(135deg, #1f2a44, #33486b);
            --accent: #1f4e79; --accent-dark: #163a5a; --accent-soft: #d5dfea; --footer: #6c757d;
        }
        :root[data-theme="graphite"] {
            --page-bg: linear-gradient(135deg, #3a3f47 0%, #23272d 100%);
            --head-bg: linear-gradient(135deg, #343a40, #50575f);
            --accent: #495057; --accent-dark: #343a40; --accent-soft: #dee2e6; --footer: rgba(255,255,255,.55);
        }
        :root[data-theme="bordeaux"] {
            --page-bg: linear-gradient(135deg, #f0ebe7 0%, #e3d9d3 100%);
            --head-bg: linear-gradient(135deg, #5a1a2b, #83344a);
            --accent: #7a2a3e; --accent-dark: #5a1a2b; --accent-soft: #eddbe0; --footer: #7d6b6f;
        }
        :root[data-theme="violet"] {
            --page-bg: linear-gradient(135deg, #667eea 0%, #764ba2 100%);
            --head-bg: linear-gradient(135deg, #667eea, #764ba2);
            --accent: #5a4fcf; --accent-dark: #4a3fbf; --accent-soft: #e3e0fb; --footer: rgba(255,255,255,.55);
        }
        .btn-primary { --bs-btn-bg: var(--accent); --bs-btn-border-color: var(--accent); --bs-btn-hover-bg: var(--accent-dark);
            --bs-btn-hover-border-color: var(--accent-dark); --bs-btn-active-bg: var(--accent-dark); --bs-btn-active-border-color: var(--accent-dark); }
        .btn-outline-primary { --bs-btn-color: var(--accent); --bs-btn-border-color: var(--accent); --bs-btn-hover-bg: var(--accent);
            --bs-btn-hover-border-color: var(--accent); --bs-btn-active-bg: var(--accent); --bs-btn-active-border-color: var(--accent); }
        .themes { display: flex; justify-content: center; gap: 8px; margin-top: 12px; }
        .themes button { width: 20px; height: 20px; border-radius: 50%; border: 2px solid rgba(255,255,255,.35); padding: 0;
            background: var(--sw); cursor: pointer; transition: transform .2s; }
        .themes button:hover { transform: scale(1.2); }
        .themes button[aria-pressed="true"] { border-color: #fff; }
        .t-classic { --sw: linear-gradient(135deg, #1f3a5f 50%, #c9a86a 50%); }
        .t-graphite { --sw: linear-gradient(135deg, #3a3f47 50%, #d9dde3 50%); }
        .t-bordeaux { --sw: linear-gradient(135deg, #5a1a2b 50%, #d8b98a 50%); }
        .t-violet { --sw: linear-gradient(135deg, #764ba2 50%, #4fd1c5 50%); }
        body {
            background: var(--page-bg);
            min-height: 100vh;
            font-family: 'Segoe UI', Tahoma, sans-serif;
            padding: 20px 0;
        }
        .main-card {
            background: white;
            border-radius: 20px;
            box-shadow: 0 20px 60px rgba(0,0,0,0.3);
            padding: 30px;
            margin-bottom: 20px;
        }
        .header-title {
            background: var(--head-bg);
            color: white;
            padding: 25px;
            border-radius: 15px;
            margin-bottom: 20px;
            text-align: center;
        }
        .header-title h1 { margin: 0; font-size: 2rem; }
        .header-title p { margin: 5px 0 0 0; opacity: 0.9; }
        .nav-pills .nav-link { color: var(--accent); border: 1px solid var(--accent-soft); }
        .nav-pills .nav-link.active { background: var(--head-bg); border-color: transparent; }

        .stat-card {
            background: linear-gradient(135deg, #f5f7fa, #e8ecf3);
            border-radius: 12px;
            padding: 18px;
            text-align: center;
            border-left: 5px solid var(--accent);
            transition: transform 0.2s;
            height: 100%;
        }
        .stat-card:hover { transform: translateY(-3px); }
        .stat-card.danger { border-left-color: #dc3545; }
        .stat-card.success { border-left-color: #28a745; }
        .stat-card.warning { border-left-color: #ffc107; }
        .stat-card.info { border-left-color: #0dcaf0; }
        .stat-number { font-size: 2.2rem; font-weight: bold; color: #333; }
        .stat-label { color: #666; font-size: 0.8rem; text-transform: uppercase; }

        .task-row { transition: all 0.3s; border-left: 4px solid transparent; }
        .task-row:hover { background-color: #f8f9fa; }
        .task-row.overdue { border-left-color: #dc3545; background-color: #fff5f5; }
        .task-row.review { border-left-color: #ffc107; background-color: #fffbeb; }
        .task-row.done { border-left-color: #28a745; background-color: #f0fff4; opacity: 0.8; }

        .dept { display: inline-block; padding: 4px 10px; border-radius: 20px; font-size: 0.8rem; font-weight: 600; white-space: nowrap; }
        .dept-frontend { background: #e0f7fb; color: #087990; }
        .dept-backend { background: #efe7fb; color: #59359a; }
        .dept-architecture { background: #fff0e1; color: #b35900; }
        .dept-security { background: #fde8ea; color: #b02a37; }

        .btn-action { border-radius: 8px; padding: 5px 10px; margin: 0 1px; transition: all 0.2s; }
        .btn-action:hover { transform: scale(1.1); }
        .timer { font-family: Consolas, monospace; font-weight: 600; }
        .timer.running { color: #0d6efd; }
        .timer.running::before { content: "● "; color: #dc3545; animation: blink 1s infinite; }
        @keyframes blink { 50% { opacity: 0; } }

        .table thead { background: var(--head-bg); color: white; }
        .table > thead { --bs-table-bg: transparent; --bs-table-color: #fff; }
        .table thead th { border: none; padding: 12px; font-weight: 600; white-space: nowrap; }
        .table td { vertical-align: middle; }

        .notification {
            position: fixed; top: 20px; right: 20px; min-width: 300px; max-width: 480px;
            padding: 15px 20px; border-radius: 10px; color: white; font-weight: 500;
            z-index: 9999; box-shadow: 0 10px 30px rgba(0,0,0,0.2); animation: slideIn 0.3s ease-out;
            white-space: pre-line;
        }
        .notification.success { background: linear-gradient(135deg, #28a745, #20c997); }
        .notification.danger { background: linear-gradient(135deg, #dc3545, #fd7e14); }
        .notification.info { background: linear-gradient(135deg, #0d6efd, #6610f2); }
        @keyframes slideIn { from { transform: translateX(400px); opacity: 0; } to { transform: translateX(0); opacity: 1; } }

        .empty-state { text-align: center; padding: 50px 20px; color: #999; }
        .empty-state i { font-size: 3.5rem; margin-bottom: 10px; }
        .kpi-badge { font-size: 1rem; min-width: 52px; }
        .protocol { white-space: pre-wrap; background: #f8f9fa; border-radius: 10px; padding: 15px; }
        {% block styles %}{% endblock %}
    </style>
</head>
<body>
<div class="container-xl">
    <div class="main-card">
        <div class="header-title">
            <h1><i class="bi bi-kanban"></i> CRM системного аналитика</h1>
            <p>Интервью → задачи → исполнители → KPI • Сегодня: {{ today_str }}</p>
            <div class="themes" role="group" aria-label="Оформление">
                <button class="t-classic" data-theme="classic" title="Классика"></button>
                <button class="t-graphite" data-theme="graphite" title="Графит"></button>
                <button class="t-bordeaux" data-theme="bordeaux" title="Бордо"></button>
                <button class="t-violet" data-theme="violet" title="Фиолетовая"></button>
            </div>
        </div>
        <ul class="nav nav-pills mb-4 gap-2 flex-wrap">
            <li class="nav-item"><a class="nav-link {% if active_tab == 'tasks' %}active{% endif %}" href="{{ root }}/"><i class="bi bi-list-task"></i> Задачи</a></li>
            <li class="nav-item"><a class="nav-link {% if active_tab == 'interviews' %}active{% endif %}" href="{{ root }}/interviews"><i class="bi bi-chat-quote"></i> Интервью</a></li>
            <li class="nav-item"><a class="nav-link {% if active_tab == 'employees' %}active{% endif %}" href="{{ root }}/employees"><i class="bi bi-people"></i> Сотрудники</a></li>
            <li class="nav-item"><a class="nav-link {% if active_tab == 'kpi' %}active{% endif %}" href="{{ root }}/kpi"><i class="bi bi-graph-up-arrow"></i> KPI и качество</a></li>
        </ul>
        {% if demo_mode %}
        <div class="alert alert-info d-flex flex-wrap align-items-center justify-content-between gap-2 py-2">
            <span><i class="bi bi-info-circle"></i> <b>Демо-версия.</b> Данные общие для всех посетителей — пробуйте смело: проведите интервью, возьмите задачу в работу, отправьте на проверку.</span>
            <button class="btn btn-sm btn-outline-primary" onclick="resetDemo()"><i class="bi bi-arrow-counterclockwise"></i> Сбросить демо-данные</button>
        </div>
        {% endif %}
        {% block content %}{% endblock %}
    </div>
    <p class="text-center small" style="color: var(--footer)">CRM системного аналитика v2.0 • данные: crm.db (SQLite)</p>
</div>

<script src="https://cdn.jsdelivr.net/npm/bootstrap@5.3.2/dist/js/bootstrap.bundle.min.js"></script>
<script>
    function showNotification(message, type = 'info') {
        const notif = document.createElement('div');
        notif.className = `notification ${type}`;
        notif.textContent = message;
        document.body.appendChild(notif);
        setTimeout(() => {
            notif.style.animation = 'slideIn 0.3s reverse';
            setTimeout(() => notif.remove(), 300);
        }, 4000);
    }

    const ROOT = {{ root|tojson }};  // префикс, если CRM открыта не в корне сайта (например, /crm)

    async function api(url, data) {
        let result;
        try {
            const res = await fetch(ROOT + url, {
                method: 'POST',
                headers: {'Content-Type': 'application/json'},
                body: JSON.stringify(data || {})
            });
            result = await res.json();
        } catch (e) {
            result = {success: false, error: 'Сервер недоступен'};
        }
        if (!result.success) showNotification('⚠️ ' + (result.error || 'Ошибка'), 'danger');
        return result;
    }

    function reloadSoon() { setTimeout(() => location.reload(), 700); }

    // Тема оформления: сохраняется в браузере, общая с витриной портфолио
    function applyTheme(name) {
        document.documentElement.dataset.theme = name;
        document.querySelectorAll('.themes button').forEach(b => b.setAttribute('aria-pressed', String(b.dataset.theme === name)));
        try { localStorage.setItem('sa-theme', name); } catch (e) {}
    }
    document.querySelectorAll('.themes button').forEach(b => b.addEventListener('click', () => applyTheme(b.dataset.theme)));
    applyTheme(document.documentElement.dataset.theme || 'classic');

    async function resetDemo() {
        if (!confirm('Вернуть демо-данные к исходному состоянию?')) return;
        const r = await api('/demo/reset');
        if (r.success) { showNotification('🔄 Демо-данные восстановлены', 'success'); setTimeout(() => location.href = ROOT + '/', 700); }
    }

    function fmtDur(sec) {
        const h = Math.floor(sec / 3600), m = Math.floor(sec % 3600 / 60), s = sec % 60;
        return `${h}:${String(m).padStart(2, '0')}:${String(s).padStart(2, '0')}`;
    }

    function esc(s) {
        const d = document.createElement('div');
        d.textContent = s == null ? '' : String(s);
        return d.innerHTML;
    }
</script>
{% block scripts %}{% endblock %}
</body>
</html>
"""

INDEX = """
{% extends "layout.html" %}
{% block title %}Задачи — CRM аналитика{% endblock %}
{% block content %}
<div class="row row-cols-2 row-cols-md-5 g-3 mb-4">
    <div class="col"><div class="stat-card"><div class="stat-number">{{ stats.total }}</div><div class="stat-label"><i class="bi bi-list-task"></i> Всего задач</div></div></div>
    <div class="col"><div class="stat-card info"><div class="stat-number">{{ stats.active }}</div><div class="stat-label"><i class="bi bi-play-circle"></i> Новые и в работе</div></div></div>
    <div class="col"><div class="stat-card warning"><div class="stat-number">{{ stats.review }}</div><div class="stat-label"><i class="bi bi-search"></i> На проверке</div></div></div>
    <div class="col"><div class="stat-card danger"><div class="stat-number">{{ stats.overdue }}</div><div class="stat-label"><i class="bi bi-exclamation-triangle"></i> Просрочено</div></div></div>
    <div class="col"><div class="stat-card success"><div class="stat-number">{{ stats.done }}</div><div class="stat-label"><i class="bi bi-check-circle"></i> Выполнено</div></div></div>
</div>

<div class="d-flex flex-wrap gap-2 mb-3">
    <a href="{{ root }}/interviews" class="btn btn-primary"><i class="bi bi-chat-quote"></i> Задачи из интервью</a>
    <button class="btn btn-outline-primary" data-bs-toggle="collapse" data-bs-target="#addCard"><i class="bi bi-plus-lg"></i> Задача вручную</button>
    <button class="btn btn-warning" onclick="checkOverdue()"><i class="bi bi-search"></i> Проверить актуальность</button>
    <button class="btn btn-outline-secondary" onclick="location.reload()"><i class="bi bi-arrow-clockwise"></i> Обновить</button>
</div>

<div class="collapse mb-4" id="addCard">
    <div class="card border-0 shadow-sm"><div class="card-body">
        <h5 class="card-title"><i class="bi bi-plus-circle"></i> Новая задача</h5>
        <form id="addForm" class="row g-2 align-items-end">
            <div class="col-md-4"><label class="form-label">Задача</label>
                <input name="title" class="form-control" placeholder="Описание задачи" required></div>
            <div class="col-md-2"><label class="form-label">Отдел</label>
                <select name="department" class="form-select">{% for d in departments %}<option>{{ d }}</option>{% endfor %}</select></div>
            <div class="col-md-2"><label class="form-label">Исполнитель</label>
                <select name="employee_id" class="form-select">
                    <option value="auto">🤖 Авто</option>
                    <option value="none">— не назначен —</option>
                    {% for e in employees %}<option value="{{ e.id }}">{{ e.name }}</option>{% endfor %}
                </select></div>
            <div class="col-md-1"><label class="form-label">Приоритет</label>
                <select name="priority" class="form-select">{% for p in priorities %}<option {% if p == 'средний' %}selected{% endif %}>{{ p }}</option>{% endfor %}</select></div>
            <div class="col-md-2"><label class="form-label">Срок</label>
                <input type="date" name="deadline" class="form-control" value="{{ default_deadline }}" required></div>
            <div class="col-md-1"><label class="form-label">Часы</label>
                <input type="number" name="estimate_hours" class="form-control" value="4" min="0.5" step="0.5"></div>
            <div class="col-12"><button class="btn btn-primary"><i class="bi bi-plus-lg"></i> Добавить</button></div>
        </form>
    </div></div>
</div>

<form class="row g-2 mb-3" method="get">
    <div class="col-6 col-md-3"><select name="dept" class="form-select form-select-sm" onchange="this.form.submit()">
        <option value="">Все отделы</option>
        {% for d in departments %}<option {% if filters.dept == d %}selected{% endif %}>{{ d }}</option>{% endfor %}
    </select></div>
    <div class="col-6 col-md-3"><select name="emp" class="form-select form-select-sm" onchange="this.form.submit()">
        <option value="">Все сотрудники</option>
        <option value="none" {% if filters.emp == 'none' %}selected{% endif %}>— не назначен —</option>
        {% for e in employees %}<option value="{{ e.id }}" {% if filters.emp == e.id|string %}selected{% endif %}>{{ e.name }}</option>{% endfor %}
    </select></div>
    <div class="col-6 col-md-2"><select name="status" class="form-select form-select-sm" onchange="this.form.submit()">
        <option value="">Все статусы</option>
        {% for s in ['новая', 'в работе', 'на проверке', 'просрочено', 'выполнено'] %}<option {% if filters.status == s %}selected{% endif %}>{{ s }}</option>{% endfor %}
    </select></div>
    <div class="col-6 col-md-3"><select name="interview" class="form-select form-select-sm" onchange="this.form.submit()">
        <option value="">Все источники</option>
        {% for i in interviews %}<option value="{{ i.id }}" {% if filters.interview == i.id|string %}selected{% endif %}>{{ i.meeting_date|ru_date }} — {{ i.title }}</option>{% endfor %}
    </select></div>
    <div class="col-12 col-md-1"><a href="{{ root }}/" class="btn btn-sm btn-outline-secondary w-100">Сброс</a></div>
</form>

<div class="card border-0 shadow-sm"><div class="card-body p-0">
{% if tasks %}
<div class="table-responsive">
<table class="table table-hover mb-0">
    <thead><tr>
        <th>№</th><th>Задача</th><th>Отдел</th><th>Исполнитель</th><th>Приоритет</th>
        <th>Срок</th><th>Время</th><th>Статус</th><th class="text-end">Действия</th>
    </tr></thead>
    <tbody>
    {% for t in tasks %}
    <tr class="task-row {% if t.is_overdue %}overdue{% elif t.status == 'выполнено' %}done{% elif t.status == 'на проверке' %}review{% endif %}">
        <td><strong>#{{ t.id }}</strong></td>
        <td style="min-width: 240px">
            <div class="fw-semibold" {% if t.description %}title="{{ t.description }}"{% endif %}>{{ t.title }}</div>
            {% if t.interview_id %}<a class="small text-muted text-decoration-none" href="{{ root }}/interviews/{{ t.interview_id }}"><i class="bi bi-chat-quote"></i> {{ t.interview_title }}</a>{% endif %}
            {% if t.returns %}<span class="badge bg-danger-subtle text-danger-emphasis ms-1" title="Возвратов на доработку"><i class="bi bi-bug"></i> {{ t.returns }}</span>{% endif %}
        </td>
        <td><span class="dept dept-{{ t.department|lower }}"><i class="bi {{ dept_icons[t.department] }}"></i> {{ t.department }}</span></td>
        <td style="min-width: 170px">
            {% if t.status != 'выполнено' %}
            <select class="form-select form-select-sm" onchange="assignTask({{ t.id }}, this.value)">
                <option value="none" {% if not t.employee_id %}selected{% endif %}>— не назначен —</option>
                {% for e in employees %}<option value="{{ e.id }}" {% if e.id == t.employee_id %}selected{% endif %}>{{ e.name }} · {{ e.department }}</option>{% endfor %}
            </select>
            {% else %}<i class="bi bi-person-fill"></i> {{ t.employee_name or '—' }}{% endif %}
        </td>
        <td><span class="badge text-bg-{{ prio_colors[t.priority] }}">{{ t.priority }}</span></td>
        <td class="text-nowrap">
            <i class="bi bi-calendar-event"></i> {{ t.deadline|ru_date }}
            {% if t.is_overdue %}<br><small class="text-danger"><i class="bi bi-clock-history"></i> просрочено на {{ t.overdue_days }} дн.</small>
            {% elif t.status == 'выполнено' %}<br><small class="{{ 'text-success' if t.on_time else 'text-danger' }}">{{ 'в срок' if t.on_time else 'опоздание ' ~ t.overdue_days ~ ' дн.' }}</small>{% endif %}
        </td>
        <td class="text-nowrap">
            <span class="timer {% if t.timer_started_at %}running{% endif %}" data-spent="{{ t.spent_live }}" {% if t.timer_started_at %}data-running="1"{% endif %}>{{ t.spent_live|dur }}</span>
            <br><small class="text-muted">план {{ t.estimate_hours|round(1) }} ч</small>
        </td>
        <td class="text-nowrap">
            {% if t.status == 'новая' %}<span class="badge text-bg-secondary">Новая</span>
            {% elif t.status == 'в работе' %}<span class="badge text-bg-primary">В работе</span>
            {% elif t.status == 'на проверке' %}<span class="badge text-bg-warning">На проверке</span>
            {% else %}<span class="badge text-bg-success">Выполнено</span>{% endif %}
            {% if t.is_overdue %}<br><span class="badge text-bg-danger mt-1">Просрочено</span>{% endif %}
        </td>
        <td class="text-end text-nowrap">
            {% if t.status in ('новая', 'в работе') %}
                {% if t.timer_started_at %}
                <button class="btn btn-outline-secondary btn-sm btn-action" onclick="timer({{ t.id }}, 'stop')" title="Пауза таймера"><i class="bi bi-pause-fill"></i></button>
                {% else %}
                <button class="btn btn-outline-primary btn-sm btn-action" onclick="timer({{ t.id }}, 'start')" title="Запустить таймер"><i class="bi bi-play-fill"></i></button>
                {% endif %}
                <button class="btn btn-warning btn-sm btn-action" onclick="toReview({{ t.id }})" title="Отправить на проверку"><i class="bi bi-send-check"></i></button>
            {% elif t.status == 'на проверке' %}
                <button class="btn btn-success btn-sm btn-action" onclick="review({{ t.id }}, 'принято')" title="Принять работу"><i class="bi bi-check-lg"></i></button>
                <button class="btn btn-outline-danger btn-sm btn-action" onclick="review({{ t.id }}, 'возврат')" title="Вернуть на доработку (баг)"><i class="bi bi-bug"></i></button>
            {% endif %}
            <button class="btn btn-danger btn-sm btn-action" onclick="deleteTask({{ t.id }})" title="Удалить"><i class="bi bi-trash"></i></button>
        </td>
    </tr>
    {% endfor %}
    </tbody>
</table>
</div>
{% else %}
<div class="empty-state">
    <i class="bi bi-inbox"></i>
    <h4>Задач пока нет</h4>
    <p>Проведите интервью со стейкхолдером — задачи сформируются автоматически</p>
    <a href="{{ root }}/interviews" class="btn btn-primary"><i class="bi bi-chat-quote"></i> К интервью</a>
</div>
{% endif %}
</div></div>
{% endblock %}

{% block scripts %}
<script>
    document.getElementById('addForm').addEventListener('submit', async (e) => {
        e.preventDefault();
        const data = Object.fromEntries(new FormData(e.target));
        const r = await api('/task/add', data);
        if (r.success) { showNotification('✅ Задача добавлена!', 'success'); reloadSoon(); }
    });

    async function timer(id, action) {
        const r = await api(`/task/${id}/timer`, {action});
        if (r.success) reloadSoon();
    }

    async function toReview(id) {
        const r = await api(`/task/${id}/status`, {status: 'на проверке'});
        if (r.success) { showNotification('📨 Задача отправлена на проверку', 'info'); reloadSoon(); }
    }

    async function review(id, result) {
        let comment = '';
        if (result === 'возврат') {
            comment = prompt('Что не так? Опишите замечание (баг):');
            if (comment === null) return;
        }
        const r = await api(`/task/${id}/review`, {result, comment});
        if (r.success) {
            showNotification(result === 'принято' ? '✅ Работа принята' : '🐞 Задача возвращена на доработку',
                             result === 'принято' ? 'success' : 'danger');
            reloadSoon();
        }
    }

    async function assignTask(id, employee_id) {
        const r = await api(`/task/${id}/assign`, {employee_id});
        if (r.success) showNotification('👤 Исполнитель изменён', 'success');
    }

    async function deleteTask(id) {
        if (!confirm('Удалить эту задачу?')) return;
        const r = await api(`/task/${id}/delete`);
        if (r.success) { showNotification('🗑 Задача удалена', 'danger'); reloadSoon(); }
    }

    async function checkOverdue() {
        const res = await fetch(ROOT + '/check');
        const result = await res.json();
        if (result.overdue.length === 0) {
            showNotification('✅ Все задачи в графике! Просрочек нет.', 'success');
        } else {
            let msg = `⚠️ Найдено просрочек: ${result.overdue.length}\\n\\n`;
            result.overdue.forEach(t => {
                msg += `• №${t.id} [${t.employee}] — «${t.task_name}»\\n  срок: ${t.deadline} (просрочено на ${t.days_overdue} дн.)\\n\\n`;
            });
            alert(msg);
        }
    }

    // Живые таймеры
    setInterval(() => {
        document.querySelectorAll('.timer[data-running]').forEach(el => {
            const s = Number(el.dataset.spent) + 1;
            el.dataset.spent = s;
            el.textContent = fmtDur(s);
        });
    }, 1000);
</script>
{% endblock %}
"""

INTERVIEWS = """
{% extends "layout.html" %}
{% block title %}Интервью — CRM аналитика{% endblock %}
{% block styles %}
.voice-panel { background: #fff5f5; border: 1px dashed #f1aeb5; border-radius: 12px; padding: 15px; margin-bottom: 15px; }
.interim { color: #888; font-style: italic; min-height: 1.5em; }
.rec-on { animation: pulse 1.2s infinite; }
@keyframes pulse { 0% { box-shadow: 0 0 0 0 rgba(220,53,69,.6); } 70% { box-shadow: 0 0 0 14px rgba(220,53,69,0); } 100% { box-shadow: 0 0 0 0 rgba(220,53,69,0); } }
{% endblock %}
{% block content %}
<div class="card border-0 shadow-sm mb-4"><div class="card-body">
    <h5 class="card-title"><i class="bi bi-chat-quote"></i> Новое интервью со стейкхолдером</h5>
    <p class="text-muted small mb-3">Зафиксируйте встречу — система выделит требования, распределит их по отделам и назначит исполнителей.</p>
    <div class="row g-2 mb-3">
        <div class="col-md-5"><label class="form-label">Тема / проект</label>
            <input id="iTitle" class="form-control" placeholder="Например: Дилерская сеть лакокрасочных материалов"></div>
        <div class="col-md-4"><label class="form-label">Стейкхолдер</label>
            <input id="iStakeholder" class="form-control" placeholder="ФИО, должность"></div>
        <div class="col-md-3"><label class="form-label">Дата встречи</label>
            <input id="iDate" type="date" class="form-control" value="{{ today_iso }}"></div>
    </div>

    <div class="btn-group mb-3" role="group">
        <button type="button" class="btn btn-primary" id="modeText" onclick="setMode('text')"><i class="bi bi-keyboard"></i> Текст</button>
        <button type="button" class="btn btn-outline-primary" id="modeVoice" onclick="setMode('voice')"><i class="bi bi-mic"></i> Голос <span class="badge text-bg-warning">бета</span></button>
    </div>

    <div id="voicePanel" class="voice-panel d-none">
        <div class="d-flex align-items-center gap-3 flex-wrap">
            <button type="button" class="btn btn-danger btn-lg rounded-pill" id="recBtn" onclick="toggleRec()"><i class="bi bi-mic-fill"></i> <span>Начать запись</span></button>
            <span id="recStatus" class="text-muted">Микрофон выключен</span>
            <span id="recTime" class="fw-bold d-none">00:00</span>
        </div>
        <div id="interim" class="interim mt-2"></div>
        <div class="small text-muted mt-2">
            <i class="bi bi-info-circle"></i> <b>Бета-версия.</b> Речь распознаётся браузером (Web Speech API): работает в Chrome и Edge,
            нужен интернет и доступ к микрофону. Распознанный текст попадает в протокол ниже — его можно поправить перед разбором.
            Перед записью получите согласие стейкхолдера на аудиофиксацию встречи.
        </div>
    </div>

    <label class="form-label d-block">Протокол встречи</label>
    <textarea id="iText" class="form-control mb-2" rows="9" placeholder="Каждое требование — отдельным предложением или строкой: «Нужно…», «Должно…», «Хочу, чтобы…». Сроки можно указать прямо в тексте: «до 15.10», «через 2 недели», «срочно»."></textarea>
    <div class="d-flex flex-wrap gap-2">
        <button class="btn btn-primary" onclick="parseText()"><i class="bi bi-magic"></i> Разобрать на задачи</button>
        <button class="btn btn-outline-secondary" onclick="fillExample()"><i class="bi bi-file-earmark-text"></i> Вставить пример</button>
        {% if not emp_list %}<span class="text-danger small align-self-center"><i class="bi bi-exclamation-triangle"></i> Нет сотрудников — задачи останутся без исполнителей. <a href="{{ root }}/employees">Добавить команду</a></span>{% endif %}
    </div>
</div></div>

<div id="previewCard" class="card border-primary shadow-sm mb-4 d-none"><div class="card-body">
    <h5 class="card-title"><i class="bi bi-list-check"></i> Предпросмотр задач <span id="previewCount" class="badge text-bg-primary"></span></h5>
    <p class="text-muted small">Проверьте формулировки, отдел, исполнителя и сроки. Снимите галочку с лишних тезисов.</p>
    <div class="table-responsive">
        <table class="table table-sm align-middle">
            <thead><tr><th></th><th>Задача</th><th>Отдел</th><th>Исполнитель</th><th>Приоритет</th><th>Срок</th><th>Часы</th></tr></thead>
            <tbody id="previewBody"></tbody>
        </table>
    </div>
    <button class="btn btn-success" onclick="saveTasks()"><i class="bi bi-check2-all"></i> Сохранить интервью и создать задачи</button>
</div></div>

<h5><i class="bi bi-archive"></i> Проведённые интервью</h5>
{% if interviews %}
<div class="list-group">
    {% for i in interviews %}
    <a href="{{ root }}/interviews/{{ i.id }}" class="list-group-item list-group-item-action d-flex justify-content-between align-items-center">
        <span>
            <i class="bi {{ 'bi-mic-fill text-danger' if i.source == 'голос' else 'bi-keyboard' }}"></i>
            <b>{{ i.title }}</b> <span class="text-muted">— {{ i.stakeholder or 'стейкхолдер не указан' }}, {{ i.meeting_date|ru_date }}</span>
        </span>
        <span class="badge text-bg-primary rounded-pill">{{ i.task_count }} задач</span>
    </a>
    {% endfor %}
</div>
{% else %}
<p class="text-muted">Пока нет ни одного интервью.</p>
{% endif %}
{% endblock %}

{% block scripts %}
<script>
    const DEPTS = {{ departments|tojson }};
    const PRIOS = {{ priorities|tojson }};
    const EMPS = {{ emp_list|tojson }};
    const txt = document.getElementById('iText');
    let usedVoice = false;

    const EXAMPLE = {{ example_text|tojson }};

    function fillExample() {
        txt.value = EXAMPLE;
        document.getElementById('iTitle').value = 'Дилерская сеть лакокрасочных материалов';
        document.getElementById('iStakeholder').value = 'Руководитель отдела продаж';
    }

    function empOptions() {
        return '<option value="auto">🤖 Авто</option><option value="none">— не назначен —</option>' +
            EMPS.map(e => `<option value="${e.id}">${esc(e.name)} · ${esc(e.department)}</option>`).join('');
    }

    async function parseText() {
        const r = await api('/interviews/parse', {text: txt.value});
        if (!r.success) return;
        const tbody = document.getElementById('previewBody');
        tbody.innerHTML = '';
        r.items.forEach(it => {
            const tr = document.createElement('tr');
            tr.dataset.desc = it.description;
            tr.innerHTML = `
                <td><input type="checkbox" class="form-check-input" checked data-skip="1"></td>
                <td style="min-width: 280px"><input class="form-control form-control-sm" data-k="title">
                    <div class="small text-muted mt-1 src"></div></td>
                <td><select class="form-select form-select-sm" data-k="department">${DEPTS.map(d => `<option>${d}</option>`).join('')}</select>
                    <div class="small text-warning mt-1 unsure d-none"><i class="bi bi-question-circle"></i> проверьте отдел</div></td>
                <td style="min-width: 190px"><select class="form-select form-select-sm" data-k="employee_id">${empOptions()}</select></td>
                <td><select class="form-select form-select-sm" data-k="priority">${PRIOS.map(p => `<option>${p}</option>`).join('')}</select></td>
                <td><input type="date" class="form-control form-control-sm" data-k="deadline"></td>
                <td><input type="number" min="0.5" step="0.5" class="form-control form-control-sm" style="width: 80px" data-k="estimate_hours"></td>`;
            tr.querySelector('[data-k=title]').value = it.title;
            tr.querySelector('.src').textContent = '«' + it.description + '»';
            tr.querySelector('[data-k=department]').value = it.department;
            if (!it.confident) tr.querySelector('.unsure').classList.remove('d-none');
            tr.querySelector('[data-k=employee_id]').value = it.employee_id ? String(it.employee_id) : 'auto';
            tr.querySelector('[data-k=priority]').value = it.priority;
            tr.querySelector('[data-k=deadline]').value = it.deadline;
            tr.querySelector('[data-k=estimate_hours]').value = it.estimate_hours;
            tr.querySelector('[data-k=department]').addEventListener('change', () => {
                tr.querySelector('[data-k=employee_id]').value = 'auto';
                tr.querySelector('.unsure').classList.add('d-none');
            });
            tbody.appendChild(tr);
        });
        document.getElementById('previewCount').textContent = r.items.length;
        const card = document.getElementById('previewCard');
        card.classList.remove('d-none');
        card.scrollIntoView({behavior: 'smooth'});
    }

    async function saveTasks() {
        const tasks = [...document.querySelectorAll('#previewBody tr')]
            .filter(tr => tr.querySelector('[data-skip]').checked)
            .map(tr => {
                const o = {description: tr.dataset.desc};
                tr.querySelectorAll('[data-k]').forEach(el => o[el.dataset.k] = el.value);
                return o;
            });
        if (!tasks.length) { showNotification('Отметьте хотя бы одну задачу', 'danger'); return; }
        const r = await api('/interviews/save', {
            title: document.getElementById('iTitle').value,
            stakeholder: document.getElementById('iStakeholder').value,
            meeting_date: document.getElementById('iDate').value,
            text: txt.value,
            source: usedVoice ? 'голос' : 'текст',
            tasks
        });
        if (r.success) {
            showNotification(`✅ Интервью сохранено, создано задач: ${r.created}`, 'success');
            setTimeout(() => location.href = ROOT + '/?interview=' + r.interview_id, 900);
        }
    }

    // ---------- Голосовой ввод (бета) ----------
    const SR = window.SpeechRecognition || window.webkitSpeechRecognition;
    let rec = null, recording = false, recStart = 0, recTick = null;

    function setMode(mode) {
        const voice = mode === 'voice';
        document.getElementById('voicePanel').classList.toggle('d-none', !voice);
        document.getElementById('modeText').className = 'btn ' + (voice ? 'btn-outline-primary' : 'btn-primary');
        document.getElementById('modeVoice').className = 'btn ' + (voice ? 'btn-primary' : 'btn-outline-primary');
        if (!voice && recording) stopRec();
        if (voice && !SR) {
            document.getElementById('recStatus').textContent = '❌ Этот браузер не умеет распознавать речь — откройте CRM в Chrome или Edge';
            document.getElementById('recBtn').disabled = true;
        }
    }

    function appendText(phrase) {
        phrase = phrase.trim();
        if (!phrase) return;
        phrase = phrase[0].toUpperCase() + phrase.slice(1);
        if (!/[.!?]$/.test(phrase)) phrase += '.';
        txt.value = (txt.value.trim() ? txt.value.trim() + '\\n' : '') + phrase;
        usedVoice = true;
    }

    function setRecUI(on) {
        const btn = document.getElementById('recBtn');
        btn.querySelector('span').textContent = on ? 'Остановить' : 'Начать запись';
        btn.classList.toggle('rec-on', on);
        document.getElementById('recStatus').textContent = on ? '🎙 Идёт запись — говорите…' : 'Микрофон выключен';
        document.getElementById('recTime').classList.toggle('d-none', !on);
        if (!on) document.getElementById('interim').textContent = '';
    }

    function toggleRec() { recording ? stopRec() : startRec(); }

    function startRec() {
        if (!SR) return;
        rec = new SR();
        rec.lang = 'ru-RU';
        rec.continuous = true;
        rec.interimResults = true;
        rec.onresult = (e) => {
            let interim = '';
            for (let i = e.resultIndex; i < e.results.length; i++) {
                const res = e.results[i];
                if (res.isFinal) appendText(res[0].transcript);
                else interim += res[0].transcript;
            }
            document.getElementById('interim').textContent = interim;
        };
        rec.onerror = (e) => {
            const reasons = {'not-allowed': 'нет доступа к микрофону', 'no-speech': 'не слышно речи',
                             'network': 'нет связи с сервисом распознавания', 'audio-capture': 'микрофон не найден'};
            if (e.error === 'no-speech') return;
            showNotification('🎙 Ошибка: ' + (reasons[e.error] || e.error), 'danger');
            if (e.error !== 'network') stopRec();
        };
        rec.onend = () => {
            // Браузер сам останавливает распознавание после паузы — перезапускаем, пока запись включена
            if (recording) { try { rec.start(); } catch (_) {} }
        };
        try { rec.start(); } catch (e) { showNotification('🎙 Не удалось запустить запись', 'danger'); return; }
        recording = true;
        recStart = Date.now();
        recTick = setInterval(() => {
            const s = Math.floor((Date.now() - recStart) / 1000);
            document.getElementById('recTime').textContent = `${String(Math.floor(s / 60)).padStart(2, '0')}:${String(s % 60).padStart(2, '0')}`;
        }, 500);
        setRecUI(true);
    }

    function stopRec() {
        recording = false;
        clearInterval(recTick);
        if (rec) rec.stop();
        setRecUI(false);
    }
</script>
{% endblock %}
"""

INTERVIEW_VIEW = """
{% extends "layout.html" %}
{% block title %}{{ interview.title }} — CRM аналитика{% endblock %}
{% block content %}
<a href="{{ root }}/interviews" class="btn btn-sm btn-outline-secondary mb-3"><i class="bi bi-arrow-left"></i> Все интервью</a>
<h4><i class="bi {{ 'bi-mic-fill text-danger' if interview.source == 'голос' else 'bi-keyboard' }}"></i> {{ interview.title }}</h4>
<p class="text-muted">Стейкхолдер: {{ interview.stakeholder or 'не указан' }} • Встреча: {{ interview.meeting_date|ru_date }} • Ввод: {{ interview.source }}</p>
<h6>Протокол</h6>
<div class="protocol mb-4">{{ interview.text }}</div>
<h6>Задачи из этого интервью ({{ tasks|length }}) <a href="{{ root }}/?interview={{ interview.id }}" class="small">открыть в списке задач →</a></h6>
<ul class="list-group">
    {% for t in tasks %}
    <li class="list-group-item d-flex justify-content-between align-items-center flex-wrap gap-2">
        <span><span class="dept dept-{{ t.department|lower }}">{{ t.department }}</span> {{ t.title }}</span>
        <span class="small text-muted">{{ t.employee_name or 'не назначен' }} • до {{ t.deadline|ru_date }} • {{ t.status }}</span>
    </li>
    {% endfor %}
</ul>
{% endblock %}
"""

EMPLOYEES = """
{% extends "layout.html" %}
{% block title %}Сотрудники — CRM аналитика{% endblock %}
{% block content %}
<div class="card border-0 shadow-sm mb-4"><div class="card-body">
    <h5 class="card-title"><i class="bi bi-person-plus"></i> Добавить сотрудника</h5>
    <form id="empForm" class="row g-2 align-items-end">
        <div class="col-md-4"><label class="form-label">ФИО</label><input name="name" class="form-control" placeholder="Иванов И.И." required></div>
        <div class="col-md-3"><label class="form-label">Отдел</label>
            <select name="department" class="form-select">{% for d in departments %}<option>{{ d }}</option>{% endfor %}</select></div>
        <div class="col-md-3"><label class="form-label">Должность</label><input name="position" class="form-control" placeholder="Backend-разработчик"></div>
        <div class="col-md-2"><button class="btn btn-primary w-100"><i class="bi bi-plus-lg"></i> Добавить</button></div>
    </form>
    {% if not emps %}
    <div class="mt-3"><button class="btn btn-outline-primary btn-sm" onclick="addDemo()"><i class="bi bi-people-fill"></i> Заполнить демо-командой (8 человек)</button></div>
    {% endif %}
</div></div>

{% if emps %}
<div class="table-responsive">
<table class="table table-hover align-middle">
    <thead><tr><th>Сотрудник</th><th>Отдел</th><th>Статус</th><th>Открытых задач</th><th>KPI</th><th>Отсутствие</th><th class="text-end"></th></tr></thead>
    <tbody>
    {% for e in emps %}
    <tr>
        <td><b>{{ e.name }}</b><br><small class="text-muted">{{ e.position }}</small></td>
        <td><span class="dept dept-{{ e.department|lower }}"><i class="bi {{ dept_icons[e.department] }}"></i> {{ e.department }}</span></td>
        <td>
            {% if e.available %}<span class="badge text-bg-success">доступен</span>
            {% else %}<span class="badge text-bg-secondary">{{ e.status }}</span>{% endif %}
            {% if e.status != 'работает' and e.absent_from %}<br><small class="text-muted">{{ e.absent_from|ru_date }} – {{ e.absent_to|ru_date }}</small>{% endif %}
        </td>
        <td>{{ e.open }}</td>
        <td>{% if e.kpi is not none %}<span class="badge kpi-badge text-bg-{{ 'success' if e.kpi >= 80 else 'warning' if e.kpi >= 60 else 'danger' }}">{{ e.kpi }}</span>{% else %}<span class="text-muted">—</span>{% endif %}</td>
        <td style="min-width: 330px">
            <div class="input-group input-group-sm">
                <select class="form-select" id="st{{ e.id }}">{% for s in emp_statuses %}<option {% if s == e.status %}selected{% endif %}>{{ s }}</option>{% endfor %}</select>
                <input type="date" class="form-control" id="from{{ e.id }}" value="{{ e.absent_from or '' }}" title="с">
                <input type="date" class="form-control" id="to{{ e.id }}" value="{{ e.absent_to or '' }}" title="по">
                <button class="btn btn-outline-primary" onclick="saveStatus({{ e.id }})" title="Сохранить"><i class="bi bi-check-lg"></i></button>
            </div>
        </td>
        <td class="text-end"><button class="btn btn-danger btn-sm btn-action" onclick="delEmp({{ e.id }})" title="Удалить"><i class="bi bi-trash"></i></button></td>
    </tr>
    {% endfor %}
    </tbody>
</table>
</div>
<p class="small text-muted"><i class="bi bi-info-circle"></i> Сотрудники на больничном или в отпуске (в указанный период) не получают новые задачи при автоназначении.</p>
{% endif %}
{% endblock %}

{% block scripts %}
<script>
    document.getElementById('empForm').addEventListener('submit', async (e) => {
        e.preventDefault();
        const r = await api('/employees/add', Object.fromEntries(new FormData(e.target)));
        if (r.success) { showNotification('✅ Сотрудник добавлен', 'success'); reloadSoon(); }
    });

    async function saveStatus(id) {
        const r = await api(`/employees/${id}/status`, {
            status: document.getElementById('st' + id).value,
            absent_from: document.getElementById('from' + id).value,
            absent_to: document.getElementById('to' + id).value
        });
        if (r.success) { showNotification('✅ Статус сохранён', 'success'); reloadSoon(); }
    }

    async function delEmp(id) {
        if (!confirm('Удалить сотрудника? Его задачи останутся без исполнителя.')) return;
        const r = await api(`/employees/${id}/delete`);
        if (r.success) reloadSoon();
    }

    async function addDemo() {
        const r = await api('/employees/demo');
        if (r.success) { showNotification('👥 Демо-команда добавлена', 'success'); reloadSoon(); }
    }
</script>
{% endblock %}
"""

KPI = """
{% extends "layout.html" %}
{% block title %}KPI и качество — CRM аналитика{% endblock %}
{% macro kpi_badge(v) -%}
{% if v is not none %}<span class="badge kpi-badge text-bg-{{ 'success' if v >= 80 else 'warning' if v >= 60 else 'danger' }}">{{ v }}</span>{% else %}<span class="text-muted">—</span>{% endif %}
{%- endmacro %}
{% macro pct(v) -%}{% if v is not none %}{{ v }}%{% else %}<span class="text-muted">—</span>{% endif %}{%- endmacro %}
{% block content %}
<div class="row row-cols-2 row-cols-md-5 g-3 mb-4">
    <div class="col"><div class="stat-card"><div class="stat-number">{{ team.kpi if team and team.kpi is not none else '—' }}</div><div class="stat-label">KPI команды</div></div></div>
    <div class="col"><div class="stat-card success"><div class="stat-number">{{ team.on_time_pct ~ '%' if team and team.on_time_pct is not none else '—' }}</div><div class="stat-label">Выполнено в срок</div></div></div>
    <div class="col"><div class="stat-card info"><div class="stat-number">{{ team.quality_pct ~ '%' if team and team.quality_pct is not none else '—' }}</div><div class="stat-label">Принято с 1-го раза</div></div></div>
    <div class="col"><div class="stat-card danger"><div class="stat-number">{{ returns_total }}</div><div class="stat-label">Возвратов (багов) из {{ checks }} проверок</div></div></div>
    <div class="col"><div class="stat-card warning"><div class="stat-number" style="font-size: 1.6rem">{{ team.plan if team else 0 }} / {{ team.fact if team else 0 }}</div><div class="stat-label">Часы: план / факт</div></div></div>
</div>

<h5><i class="bi bi-person-badge"></i> KPI сотрудников</h5>
{% if by_emp %}
<div class="table-responsive mb-4">
<table class="table table-hover align-middle">
    <thead><tr><th>Сотрудник</th><th>Отдел</th><th>Задач</th><th>Выполнено</th><th>В срок</th><th>С 1-го раза</th><th>План/факт</th><th>Просрочено сейчас</th><th>KPI</th></tr></thead>
    <tbody>
    {% for r in by_emp %}
    <tr>
        <td><b>{{ r.name }}</b></td>
        <td><span class="dept dept-{{ r.dept|lower }}">{{ r.dept }}</span></td>
        <td>{{ r.total }}</td><td>{{ r.done }}</td>
        <td>{{ pct(r.on_time_pct) }}</td><td>{{ pct(r.quality_pct) }}</td>
        <td>{{ r.plan }} / {{ r.fact }} ч</td>
        <td>{% if r.overdue_open %}<span class="text-danger fw-bold">{{ r.overdue_open }}</span>{% else %}0{% endif %}</td>
        <td>{{ kpi_badge(r.kpi) }}</td>
    </tr>
    {% endfor %}
    </tbody>
</table>
</div>
{% else %}<p class="text-muted">Нет задач с назначенными исполнителями.</p>{% endif %}

<h5><i class="bi bi-diagram-3"></i> KPI отделов</h5>
{% if by_dept %}
<div class="table-responsive mb-4">
<table class="table table-hover align-middle">
    <thead><tr><th>Отдел</th><th>Задач</th><th>Открыто</th><th>Выполнено</th><th>В срок</th><th>С 1-го раза</th><th>Возвратов</th><th>KPI</th></tr></thead>
    <tbody>
    {% for r in by_dept %}
    <tr>
        <td><span class="dept dept-{{ r.key|lower }}"><i class="bi {{ dept_icons[r.key] }}"></i> {{ r.key }}</span></td>
        <td>{{ r.total }}</td><td>{{ r.open }}</td><td>{{ r.done }}</td>
        <td>{{ pct(r.on_time_pct) }}</td><td>{{ pct(r.quality_pct) }}</td><td>{{ r.returns }}</td>
        <td>{{ kpi_badge(r.kpi) }}</td>
    </tr>
    {% endfor %}
    </tbody>
</table>
</div>
{% else %}<p class="text-muted">Задач пока нет.</p>{% endif %}

<h5><i class="bi bi-bug"></i> Качество ПО: журнал проверок</h5>
{% if reviews %}
<div class="table-responsive mb-3">
<table class="table table-sm align-middle">
    <thead><tr><th>Дата</th><th>Задача</th><th>Исполнитель</th><th>Результат</th><th>Замечание</th></tr></thead>
    <tbody>
    {% for r in reviews %}
    <tr>
        <td class="text-nowrap">{{ r.created_at|ru_datetime }}</td>
        <td>#{{ r.task_id }} {{ r.title }}</td>
        <td>{{ r.employee_name or '—' }}</td>
        <td>{% if r.result == 'принято' %}<span class="badge text-bg-success">принято</span>{% else %}<span class="badge text-bg-danger">возврат</span>{% endif %}</td>
        <td>{{ r.comment }}</td>
    </tr>
    {% endfor %}
    </tbody>
</table>
</div>
{% else %}<p class="text-muted">Проверок пока не было. Отправьте задачу на проверку и примите или верните её.</p>{% endif %}

<div class="alert alert-light border small">
    <b>Как считается KPI (0–100):</b> 50% — доля задач, выполненных в срок; 30% — доля задач, принятых с первого раза (без возвратов);
    20% — соотношение плановых и фактических часов по таймеру. Минус 5 баллов за каждую открытую просроченную задачу.
    Если таймер не использовался, KPI считается по первым двум показателям.
</div>
{% endblock %}
"""

app.jinja_env.loader = DictLoader({
    "layout.html": LAYOUT,
    "index.html": INDEX,
    "interviews.html": INTERVIEWS,
    "interview_view.html": INTERVIEW_VIEW,
    "employees.html": EMPLOYEES,
    "kpi.html": KPI,
})


@app.template_filter("dur")
def fmt_duration(sec):
    sec = int(sec or 0)
    return f"{sec // 3600}:{sec % 3600 // 60:02d}:{sec % 60:02d}"


@app.template_filter("ru_date")
def ru_date(s):
    try:
        return parse_date(str(s)[:10]).strftime("%d.%m.%Y")
    except ValueError:
        return s


@app.template_filter("ru_datetime")
def ru_datetime(s):
    try:
        return datetime.fromisoformat(s).strftime("%d.%m.%Y %H:%M")
    except (TypeError, ValueError):
        return s


@app.context_processor
def inject_globals():
    return dict(
        root=request.script_root,
        demo_mode=DEMO_MODE,
        departments=DEPARTMENTS,
        dept_icons=DEPT_ICONS,
        priorities=PRIORITIES,
        prio_colors={"высокий": "danger", "средний": "warning", "низкий": "secondary"},
        emp_statuses=EMP_STATUSES,
        today_str=today().strftime("%d.%m.%Y"),
        today_iso=today().isoformat(),
        default_deadline=(today() + timedelta(days=7)).isoformat(),
    )


# ==================== МАРШРУТЫ: ЗАДАЧИ ====================
@app.route("/")
def index():
    db = get_db()
    tasks = all_tasks(db)
    stats = dict(
        total=len(tasks),
        active=sum(t["status"] in ("новая", "в работе") for t in tasks),
        review=sum(t["status"] == "на проверке" for t in tasks),
        overdue=sum(t["is_overdue"] for t in tasks),
        done=sum(t["status"] == "выполнено" for t in tasks),
    )
    filters = {k: request.args.get(k, "") for k in ("dept", "emp", "status", "interview")}
    shown = tasks
    if filters["dept"]:
        shown = [t for t in shown if t["department"] == filters["dept"]]
    if filters["emp"] == "none":
        shown = [t for t in shown if not t["employee_id"]]
    elif filters["emp"]:
        shown = [t for t in shown if str(t["employee_id"]) == filters["emp"]]
    if filters["status"] == "просрочено":
        shown = [t for t in shown if t["is_overdue"]]
    elif filters["status"]:
        shown = [t for t in shown if t["status"] == filters["status"]]
    if filters["interview"]:
        shown = [t for t in shown if str(t["interview_id"]) == filters["interview"]]
    order = {"на проверке": 0, "в работе": 1, "новая": 2, "выполнено": 3}
    shown.sort(key=lambda t: (t["status"] == "выполнено", not t["is_overdue"], order[t["status"]], t["deadline"]))
    return render_template(
        "index.html",
        tasks=shown,
        stats=stats,
        filters=filters,
        employees=db.execute("SELECT * FROM employees ORDER BY department, name").fetchall(),
        interviews=db.execute("SELECT id, title, meeting_date FROM interviews ORDER BY id DESC").fetchall(),
        active_tab="tasks",
    )


@app.route("/task/add", methods=["POST"])
def task_add():
    d = request.get_json() or {}
    db = get_db()
    if over_demo_limit(db, "tasks"):
        return fail(DEMO_LIMIT_MESSAGE)
    try:
        title, desc, dept, prio, deadline, hours = clean_task_payload(d)
        emps = db.execute("SELECT * FROM employees").fetchall()
        emp_id = resolve_employee(d.get("employee_id"), dept, open_load(db), emps)
    except ValueError as e:
        return fail(str(e))
    task_id = insert_task(db, title, desc, dept, emp_id, None, prio, deadline, hours)
    db.commit()
    return ok(task_id=task_id)


@app.route("/task/<int:task_id>/timer", methods=["POST"])
def task_timer(task_id):
    db = get_db()
    t = get_task(db, task_id)
    action = (request.get_json() or {}).get("action")
    if t["status"] not in ("новая", "в работе"):
        return fail("Таймер доступен только для задач в работе")
    if action == "start":
        if not t["timer_started_at"]:
            db.execute("UPDATE tasks SET timer_started_at = ?, status = 'в работе' WHERE id = ?", (now_iso(), task_id))
    elif action == "stop":
        stop_timer(db, t)
    else:
        return fail("Неизвестное действие")
    db.commit()
    return ok()


@app.route("/task/<int:task_id>/status", methods=["POST"])
def task_status(task_id):
    db = get_db()
    t = get_task(db, task_id)
    status = (request.get_json() or {}).get("status")
    if status != "на проверке" or t["status"] not in ("новая", "в работе"):
        return fail("Недопустимая смена статуса")
    if not t["employee_id"]:
        return fail("Сначала назначьте исполнителя")
    stop_timer(db, t)
    db.execute("UPDATE tasks SET status = 'на проверке' WHERE id = ?", (task_id,))
    db.commit()
    return ok()


@app.route("/task/<int:task_id>/review", methods=["POST"])
def task_review(task_id):
    db = get_db()
    t = get_task(db, task_id)
    d = request.get_json() or {}
    result, comment = d.get("result"), (d.get("comment") or "").strip()[:500]
    if t["status"] != "на проверке":
        return fail("Задача не на проверке")
    if result == "принято":
        db.execute("UPDATE tasks SET status = 'выполнено', completed_at = ? WHERE id = ?", (now_iso(), task_id))
    elif result == "возврат":
        if not comment:
            return fail("Опишите замечание")
        db.execute("UPDATE tasks SET status = 'в работе', returns = returns + 1 WHERE id = ?", (task_id,))
    else:
        return fail("Неизвестный результат проверки")
    db.execute(
        "INSERT INTO reviews (task_id, result, comment, created_at) VALUES (?, ?, ?, ?)",
        (task_id, result, comment, now_iso()),
    )
    db.commit()
    return ok()


@app.route("/task/<int:task_id>/assign", methods=["POST"])
def task_assign(task_id):
    db = get_db()
    get_task(db, task_id)
    raw = str((request.get_json() or {}).get("employee_id", "none"))
    emp_id = int(raw) if raw.isdigit() else None
    if emp_id and not db.execute("SELECT 1 FROM employees WHERE id = ?", (emp_id,)).fetchone():
        return fail("Сотрудник не найден")
    db.execute("UPDATE tasks SET employee_id = ? WHERE id = ?", (emp_id, task_id))
    db.commit()
    return ok()


@app.route("/task/<int:task_id>/delete", methods=["POST"])
def task_delete(task_id):
    db = get_db()
    db.execute("DELETE FROM tasks WHERE id = ?", (task_id,))
    db.commit()
    return ok()


@app.route("/check")
def check():
    tasks = all_tasks(get_db())
    overdue = [
        dict(id=t["id"], employee=t["employee_name"] or "не назначен", task_name=t["title"],
             deadline=ru_date(t["deadline"]), days_overdue=t["overdue_days"])
        for t in tasks if t["is_overdue"]
    ]
    return jsonify({"overdue": overdue})


# ==================== МАРШРУТЫ: ИНТЕРВЬЮ ====================
@app.route("/interviews")
def interviews_page():
    db = get_db()
    interviews = db.execute(
        "SELECT i.*, (SELECT COUNT(*) FROM tasks t WHERE t.interview_id = i.id) AS task_count"
        " FROM interviews i ORDER BY i.meeting_date DESC, i.id DESC"
    ).fetchall()
    emp_list = [
        dict(id=e["id"], name=e["name"], department=e["department"])
        for e in db.execute("SELECT * FROM employees ORDER BY department, name")
    ]
    return render_template(
        "interviews.html", interviews=interviews, emp_list=emp_list,
        example_text=EXAMPLE_INTERVIEW, active_tab="interviews",
    )


@app.route("/interviews/parse", methods=["POST"])
def interviews_parse():
    text = ((request.get_json() or {}).get("text") or "").strip()
    if len(text) < 10:
        return fail("Протокол слишком короткий")
    if DEMO_MODE and len(text) > DEMO_LIMITS["text"]:
        return fail(f"В демо-версии протокол не длиннее {DEMO_LIMITS['text']} символов")
    db = get_db()
    emps = db.execute("SELECT * FROM employees").fetchall()
    load = open_load(db)
    items = []
    for sentence in split_theses(text):
        dept, confident = classify(sentence)
        priority = detect_priority(sentence)
        deadline = extract_deadline(sentence, today()) or today() + timedelta(days=PRIORITY_DAYS[priority])
        emp_id = pick_employee(emps, load, dept)
        if emp_id:
            load[emp_id] = load.get(emp_id, 0) + 1
        items.append(dict(
            title=make_title(sentence), description=sentence, department=dept, confident=confident,
            priority=priority, deadline=deadline.isoformat(), estimate_hours=DEPT_HOURS[dept], employee_id=emp_id,
        ))
    if not items:
        return fail("Не нашёл требований. Сформулируйте тезисы: «Нужно…», «Должно…», «Хочу, чтобы…»")
    return ok(items=items)


@app.route("/interviews/save", methods=["POST"])
def interviews_save():
    d = request.get_json() or {}
    text = (d.get("text") or "").strip()
    if not text:
        return fail("Пустой протокол")
    try:
        meeting = parse_date(d.get("meeting_date") or today().isoformat()).isoformat()
    except ValueError:
        return fail("Неверная дата встречи")
    db = get_db()
    tasks = d.get("tasks") or []
    if DEMO_MODE and (len(text) > DEMO_LIMITS["text"] or len(tasks) > DEMO_LIMITS["tasks_per_interview"]):
        return fail("Слишком большой протокол для демо-версии")
    if over_demo_limit(db, "interviews") or over_demo_limit(db, "tasks", len(tasks)):
        return fail(DEMO_LIMIT_MESSAGE)
    emps = db.execute("SELECT * FROM employees").fetchall()
    load = open_load(db)
    try:
        cleaned = [(clean_task_payload(t), t.get("employee_id")) for t in tasks]
        interview_id = db.execute(
            "INSERT INTO interviews (title, stakeholder, meeting_date, text, source, created_at)"
            " VALUES (?, ?, ?, ?, ?, ?)",
            ((d.get("title") or "").strip()[:150] or "Интервью со стейкхолдером",
             (d.get("stakeholder") or "").strip()[:150], meeting, text,
             "голос" if d.get("source") == "голос" else "текст", now_iso()),
        ).lastrowid
        for (title, desc, dept, prio, deadline, hours), raw_emp in cleaned:
            emp_id = resolve_employee(raw_emp, dept, load, emps)
            insert_task(db, title, desc, dept, emp_id, interview_id, prio, deadline, hours)
        db.commit()
    except ValueError as e:
        db.rollback()
        return fail(str(e))
    return ok(interview_id=interview_id, created=len(cleaned))


@app.route("/interviews/<int:interview_id>")
def interview_view(interview_id):
    db = get_db()
    interview = db.execute("SELECT * FROM interviews WHERE id = ?", (interview_id,)).fetchone()
    if not interview:
        abort(404)
    tasks = db.execute(TASKS_SQL + " WHERE t.interview_id = ? ORDER BY t.id", (interview_id,)).fetchall()
    return render_template("interview_view.html", interview=interview, tasks=tasks, active_tab="interviews")


# ==================== МАРШРУТЫ: СОТРУДНИКИ ====================
DEMO_TEAM = [
    ("Ковалёва Ксения", "Frontend", "Frontend-разработчик"),
    ("Орлов Денис", "Frontend", "UI/UX-дизайнер"),
    ("Соколов Андрей", "Backend", "Backend-разработчик"),
    ("Лебедева Мария", "Backend", "Backend-разработчик"),
    ("Никитин Павел", "Architecture", "Системный архитектор"),
    ("Морозова Елена", "Architecture", "DevOps-инженер"),
    ("Фёдоров Алексей", "Security", "Специалист по ИБ"),
    ("Григорьева Ольга", "Security", "Юрист, 152-ФЗ"),
]


def seed_demo(db):
    """Пример данных для демо: команда, два интервью, задачи во всех статусах, проверки и возвраты."""
    db.executescript(
        "DELETE FROM reviews; DELETE FROM tasks; DELETE FROM interviews; DELETE FROM employees;"
        " DELETE FROM sqlite_sequence;"
    )
    base, now = today(), datetime.now()

    def day(offset, hour=11):
        return datetime.combine(base + timedelta(days=offset), datetime.min.time()).replace(hour=hour).isoformat()

    for name, dept, position in DEMO_TEAM:
        db.execute(
            "INSERT INTO employees (name, department, position, created_at) VALUES (?, ?, ?, ?)",
            (name, dept, position, day(-14)),
        )
    db.execute(
        "UPDATE employees SET status = 'больничный', absent_from = ?, absent_to = ? WHERE name = 'Лебедева Мария'",
        ((base - timedelta(days=1)).isoformat(), (base + timedelta(days=5)).isoformat()),
    )

    # Сценарии: (статус, срок относительно сегодня, завершено (дней назад), доля факта от плана, возвратов)
    scenarios = [
        ("выполнено", -3, -4, 0.9, 0),
        ("выполнено", -2, -3, 1.3, 1),
        ("на проверке", 2, None, 0.8, 0),
        ("в работе", 4, None, 0.4, 0),       # с запущенным таймером
        ("в работе", -2, None, 0.7, 0),      # просрочена
        ("выполнено", -6, -4, 1.1, 0),       # сдана с опозданием
        ("новая", 7, None, 0, 0),
        ("выполнено", -1, -1, 0.7, 0),
    ]
    load = {}
    emps = db.execute("SELECT * FROM employees").fetchall()
    for text, title, stakeholder, source, meeting in (
        (EXAMPLE_INTERVIEW, "Дилерская сеть лакокрасочных материалов", "Руководитель отдела продаж", "текст", -10),
        (DEMO_VOICE_INTERVIEW, "Требования службы ИБ", "Начальник службы безопасности", "голос", -2),
    ):
        interview_id = db.execute(
            "INSERT INTO interviews (title, stakeholder, meeting_date, text, source, created_at) VALUES (?, ?, ?, ?, ?, ?)",
            (title, stakeholder, (base + timedelta(days=meeting)).isoformat(), text, source, day(meeting, 15)),
        ).lastrowid
        for sentence in split_theses(text):
            dept, _ = classify(sentence)
            emp_id = pick_employee(emps, load, dept) or pick_employee(
                [e for e in emps if e["status"] == "работает"], load, dept)
            if emp_id:
                load[emp_id] = load.get(emp_id, 0) + 1
            task_id = insert_task(db, make_title(sentence), sentence, dept, emp_id, interview_id,
                                  detect_priority(sentence), base.isoformat(), DEPT_HOURS[dept])
            db.execute("UPDATE tasks SET created_at = ? WHERE id = ?", (day(meeting, 16), task_id))

    task_ids = [r[0] for r in db.execute("SELECT id FROM tasks ORDER BY id")]
    for task_id, (status, deadline, done_day, fact_share, returns) in zip(task_ids, scenarios):
        est = db.execute("SELECT estimate_hours FROM tasks WHERE id = ?", (task_id,)).fetchone()[0]
        db.execute(
            "UPDATE tasks SET status = ?, deadline = ?, spent_seconds = ?, returns = ?, completed_at = ?,"
            " timer_started_at = ? WHERE id = ?",
            (status, (base + timedelta(days=deadline)).isoformat(), int(est * fact_share * 3600), returns,
             day(done_day, 17) if done_day is not None else None,
             (now - timedelta(minutes=25)).isoformat(timespec="seconds") if task_id == task_ids[3] else None,
             task_id),
        )
        if returns:
            db.execute(
                "INSERT INTO reviews (task_id, result, comment, created_at) VALUES (?, 'возврат', ?, ?)",
                (task_id, "Скидка не пересчитывается при изменении суммы заказа", day(done_day - 2, 12)),
            )
        if status == "выполнено":
            db.execute(
                "INSERT INTO reviews (task_id, result, comment, created_at) VALUES (?, 'принято', '', ?)",
                (task_id, day(done_day, 17)),
            )
    # Задачи без сценария (из второго интервью) — новые, срок через неделю
    db.execute(
        "UPDATE tasks SET deadline = ? WHERE deadline = ? AND status = 'новая'",
        ((base + timedelta(days=7)).isoformat(), base.isoformat()),
    )
    db.execute("INSERT OR REPLACE INTO meta (key, value) VALUES ('seeded_on', ?)", (base.isoformat(),))
    db.commit()


@app.route("/demo/reset", methods=["POST"])
def demo_reset():
    if not DEMO_MODE:
        return fail("Сброс доступен только в демо-версии", 403)
    seed_demo(get_db())
    return ok()


@app.route("/employees")
def employees_page():
    db = get_db()
    tasks = all_tasks(db)
    kpi = {r["key"]: r for r in kpi_rows(tasks, lambda t: t["employee_id"])}
    emps = []
    for row in db.execute("SELECT * FROM employees ORDER BY department, name"):
        e = dict(row)
        e["available"] = employee_available(e)
        e["open"] = sum(1 for t in tasks if t["employee_id"] == e["id"] and t["status"] != "выполнено")
        e["kpi"] = kpi.get(e["id"], {}).get("kpi")
        emps.append(e)
    return render_template("employees.html", emps=emps, active_tab="employees")


@app.route("/employees/add", methods=["POST"])
def employees_add():
    d = request.get_json() or {}
    name = (d.get("name") or "").strip()
    if not name:
        return fail("Укажите ФИО")
    if d.get("department") not in DEPARTMENTS:
        return fail("Неизвестный отдел")
    db = get_db()
    if over_demo_limit(db, "employees"):
        return fail(DEMO_LIMIT_MESSAGE)
    db.execute(
        "INSERT INTO employees (name, department, position, created_at) VALUES (?, ?, ?, ?)",
        (name[:80], d["department"], (d.get("position") or "").strip()[:80], now_iso()),
    )
    db.commit()
    return ok()


@app.route("/employees/demo", methods=["POST"])
def employees_demo():
    db = get_db()
    for name, dept, position in DEMO_TEAM:
        if not db.execute("SELECT 1 FROM employees WHERE name = ?", (name,)).fetchone():
            db.execute(
                "INSERT INTO employees (name, department, position, created_at) VALUES (?, ?, ?, ?)",
                (name, dept, position, now_iso()),
            )
    db.commit()
    return ok()


@app.route("/employees/<int:emp_id>/status", methods=["POST"])
def employees_status(emp_id):
    d = request.get_json() or {}
    status = d.get("status")
    if status not in EMP_STATUSES:
        return fail("Неизвестный статус")
    absent_from = d.get("absent_from") or None
    absent_to = d.get("absent_to") or None
    if status == "работает":
        absent_from = absent_to = None
    elif absent_from and absent_to:
        try:
            if parse_date(absent_from) > parse_date(absent_to):
                return fail("Дата «с» позже даты «по»")
        except ValueError:
            return fail("Неверные даты отсутствия")
    elif absent_from or absent_to:
        return fail("Укажите обе даты отсутствия или ни одной")
    db = get_db()
    db.execute(
        "UPDATE employees SET status = ?, absent_from = ?, absent_to = ? WHERE id = ?",
        (status, absent_from, absent_to, emp_id),
    )
    db.commit()
    return ok()


@app.route("/employees/<int:emp_id>/delete", methods=["POST"])
def employees_delete(emp_id):
    db = get_db()
    db.execute("DELETE FROM employees WHERE id = ?", (emp_id,))
    db.commit()
    return ok()


# ==================== МАРШРУТЫ: KPI ====================
@app.route("/kpi")
def kpi_page():
    db = get_db()
    tasks = all_tasks(db)
    names = {e["id"]: e for e in db.execute("SELECT * FROM employees")}
    by_emp = kpi_rows([t for t in tasks if t["employee_id"]], lambda t: t["employee_id"])
    for r in by_emp:
        e = names.get(r["key"])
        r["name"] = e["name"] if e else "—"
        r["dept"] = e["department"] if e else ""
    team = kpi_rows(tasks, lambda t: "team")
    reviews = db.execute(
        "SELECT r.*, t.title, e.name AS employee_name FROM reviews r"
        " JOIN tasks t ON t.id = r.task_id LEFT JOIN employees e ON e.id = t.employee_id"
        " ORDER BY r.id DESC LIMIT 20"
    ).fetchall()
    return render_template(
        "kpi.html",
        by_emp=by_emp,
        by_dept=kpi_rows(tasks, lambda t: t["department"]),
        team=team[0] if team else None,
        reviews=reviews,
        checks=db.execute("SELECT COUNT(*) FROM reviews").fetchone()[0],
        returns_total=db.execute("SELECT COUNT(*) FROM reviews WHERE result = 'возврат'").fetchone()[0],
        active_tab="kpi",
    )


# ==================== ЗАПУСК ====================
def open_browser():
    """Открыть браузер автоматически после запуска сервера."""
    webbrowser.open(f"http://127.0.0.1:{PORT}")


if __name__ == "__main__":
    try:
        sys.stdout.reconfigure(encoding="utf-8", errors="replace")
    except Exception:
        pass
    init_db()
    print(f"\n{'=' * 50}")
    print("🚀 CRM системного аналитика v2.0 запущена!")
    print(f"🌐 Откройте в браузере: http://127.0.0.1:{PORT}")
    print(f"📁 База данных: {DB_FILE}")
    print(f"{'=' * 50}\n")

    if "--no-browser" not in sys.argv:
        Timer(1.5, open_browser).start()

    app.run(host="127.0.0.1", port=PORT, debug=False)
