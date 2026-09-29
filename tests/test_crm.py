"""
Автотесты CRM: запуск — python tests/test_crm.py
Работают на временной базе, рабочую crm.db не трогают.
"""
import os
import sys
import tempfile

os.environ["CRM_DEMO"] = "1"
os.environ["CRM_LOG"] = os.path.join(tempfile.mkdtemp(), "test.log")
os.environ["CRM_LOG_LEVEL"] = "DEBUG"
sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
try:
    sys.stdout.reconfigure(encoding="utf-8")
except Exception:
    pass

import main  # noqa: E402
from flask import Flask  # noqa: E402
from werkzeug.middleware.dispatcher import DispatcherMiddleware  # noqa: E402

@main.app.route("/boom-test")
def boom():
    raise RuntimeError("проверочный сбой")


main.DB_FILE = os.path.join(tempfile.mkdtemp(), "test.db")
main.init_db()
root = Flask("portal")
root.wsgi_app = DispatcherMiddleware(root.wsgi_app, {"/crm": main.app})
client = root.test_client()


def post(url, data=None, expect=True):
    r = client.post("/crm" + url, json=data or {})
    body = r.get_json()
    assert bool(body and body.get("success")) == expect, (url, r.status_code, body)
    return body


def test_pages_and_prefix():
    for page in ["/", "/tasks", "/kanban", "/bpmn", "/interviews", "/employees", "/kpi", "/log", "/interviews/1",
                 "/tasks?status=просрочено", "/kanban?dept=Backend", "/check"]:
        assert client.get("/crm" + page).status_code == 200, page
    html = client.get("/crm/").get_data(as_text=True)
    assert 'href="/crm/interviews"' in html and 'const ROOT = "/crm"' in html
    assert "Демо-версия" in html


def test_demo_seed():
    db = main.sqlite3.connect(main.DB_FILE)
    db.row_factory = main.sqlite3.Row
    tasks = main.all_tasks(db)
    statuses = {t["status"] for t in tasks}
    assert {"новая", "в работе", "на проверке", "выполнено"} <= statuses
    assert any(t["is_overdue"] for t in tasks) and any(t["timer_started_at"] for t in tasks)
    team = main.kpi_rows(tasks, lambda t: "team")[0]
    assert team["kpi"] is not None


def test_parse_text():
    items = post("/interviews/parse", {"text": main.EXAMPLE_INTERVIEW})["items"]
    assert len(items) == 8
    depts = [i["department"] for i in items]
    assert depts == ["Frontend", "Backend", "Frontend", "Backend", "Security", "Security", "Architecture", "Architecture"]
    assert items[2]["priority"] == "высокий" and items[6]["priority"] == "низкий"
    assert items[7]["deadline"].endswith("-10-15")


def test_parse_voice_without_punctuation():
    text = ("нужно сделать кнопку экспорта отчёта в эксель также надо чтобы админ мог блокировать "
            "пользователей и ещё хочу чтобы страница открывалась быстро на телефоне")
    items = post("/interviews/parse", {"text": text})["items"]
    assert [i["department"] for i in items] == ["Backend", "Security", "Frontend"], items


def test_full_cycle():
    items = post("/interviews/parse", {"text": main.EXAMPLE_INTERVIEW})["items"]
    for it in items:
        it["employee_id"] = "auto"
    res = post("/interviews/save", {"title": "Тест", "text": main.EXAMPLE_INTERVIEW, "source": "голос", "tasks": items})
    assert res["created"] == 8
    db = main.sqlite3.connect(main.DB_FILE)
    tid = db.execute("SELECT id FROM tasks WHERE interview_id = ? ORDER BY id", (res["interview_id"],)).fetchone()[0]
    post(f"/task/{tid}/timer", {"action": "start"})
    post(f"/task/{tid}/timer", {"action": "stop"})
    post(f"/task/{tid}/status", {"status": "на проверке"})
    post(f"/task/{tid}/review", {"result": "возврат"}, expect=False)  # без комментария нельзя
    post(f"/task/{tid}/review", {"result": "возврат", "comment": "Баг"})
    post(f"/task/{tid}/status", {"status": "на проверке"})
    post(f"/task/{tid}/review", {"result": "принято"})
    row = db.execute("SELECT status, returns FROM tasks WHERE id = ?", (tid,)).fetchone()
    assert row == ("выполнено", 1)


def test_sick_employee_gets_no_tasks():
    db = main.sqlite3.connect(main.DB_FILE)
    sick = db.execute("SELECT id FROM employees WHERE status = 'больничный'").fetchone()[0]
    before = db.execute("SELECT COUNT(*) FROM tasks WHERE employee_id = ?", (sick,)).fetchone()[0]
    post("/task/add", {"title": "Новая задача бэкенда", "department": "Backend", "employee_id": "auto",
                       "priority": "средний", "deadline": "2030-01-01", "estimate_hours": 2})
    after = db.execute("SELECT COUNT(*) FROM tasks WHERE employee_id = ?", (sick,)).fetchone()[0]
    assert before == after


def test_logging():
    db = main.sqlite3.connect(main.DB_FILE)
    db.row_factory = main.sqlite3.Row
    before = db.execute("SELECT MAX(id) FROM events").fetchone()[0] or 0
    post("/task/add", {"title": "Проверка журнала", "department": "Frontend", "employee_id": "auto",
                       "priority": "низкий", "deadline": "2030-01-01", "estimate_hours": 1})
    post("/task/add", {"title": "", "department": "Frontend"}, expect=False)
    new = db.execute("SELECT level, action FROM events WHERE id > ? ORDER BY id", (before,)).fetchall()
    assert [tuple(r) for r in new] == [("INFO", "Задача создана вручную"), ("WARNING", "Отказ")], [tuple(r) for r in new]
    html = client.get("/crm/log?level=WARNING").get_data(as_text=True)
    assert "Отказ" in html and "Задача создана вручную" not in html
    csv_text = client.get("/crm/log.csv?q=журнала").get_data(as_text=True)
    assert "Проверка журнала" in csv_text
    with open(main.LOG_FILE, encoding="utf-8") as f:
        text = f.read()
    assert "INFO    | Задача создана вручную" in text and "WARNING | Отказ" in text and "DEBUG   | Разбор протокола" in text


def test_error_is_logged():
    r = client.get("/crm/boom-test")
    assert r.status_code == 500
    db = main.sqlite3.connect(main.DB_FILE)
    assert db.execute("SELECT COUNT(*) FROM events WHERE level = 'ERROR' AND message LIKE '%проверочный сбой%'").fetchone()[0] == 1
    with open(main.LOG_FILE, encoding="utf-8") as f:
        assert "Traceback" in f.read()


def test_back_to_projects_button():
    assert 'class="back" href="/"' in client.get("/crm/kpi").get_data(as_text=True)


def test_dashboard_and_kanban():
    html = client.get("/crm/").get_data(as_text=True)
    assert "Динамика" in html and "chart.umd.min.js" in html and "KPI команды" in html
    db = main.sqlite3.connect(main.DB_FILE)
    new_id = db.execute("SELECT id FROM tasks WHERE status = 'новая' AND employee_id IS NOT NULL LIMIT 1").fetchone()[0]
    post(f"/task/{new_id}/take")
    assert db.execute("SELECT status FROM tasks WHERE id = ?", (new_id,)).fetchone()[0] == "в работе"
    post(f"/task/{new_id}/take", expect=False)  # повторно нельзя
    assert "kcard" in client.get("/crm/kanban").get_data(as_text=True)


CALC_TEXT = """Старт: Ввод
Пользователь: Ввести числа
Если делитель равен нулю — Система: Показать ошибку (конец), иначе Система: Посчитать
Конец: Результат"""


def test_bpmn():
    import xml.dom.minidom
    db = main.sqlite3.connect(main.DB_FILE)
    assert db.execute("SELECT COUNT(*) FROM diagrams").fetchone()[0] >= 4  # демо: 2 интервью + шаблон + пример
    for kind in ("lifecycle", "empty"):
        post("/bpmn/generate", {"kind": kind})
    r = post("/bpmn/generate", {"kind": "text", "title": "Калькулятор",
                                "text": CALC_TEXT})
    xml_text = db.execute("SELECT xml FROM diagrams WHERE id = ?", (r["id"],)).fetchone()[0]
    xml.dom.minidom.parseString(xml_text.encode())
    assert "exclusiveGateway" in xml_text and "BPMNEdge" in xml_text
    post("/bpmn/generate", {"kind": "text", "text": ""}, expect=False)
    iid = db.execute("SELECT id FROM interviews LIMIT 1").fetchone()[0]
    r2 = post("/bpmn/generate", {"kind": "interview", "interview_id": iid})
    post(f"/bpmn/{r2['id']}/save", {"xml": xml_text, "title": "Правка"})
    post(f"/bpmn/{r2['id']}/save", {"xml": "<html/>"}, expect=False)
    assert client.get(f"/crm/bpmn/{r2['id']}.bpmn").status_code == 200
    assert "bpmn-modeler" in client.get(f"/crm/bpmn?id={r2['id']}").get_data(as_text=True)
    post(f"/bpmn/{r2['id']}/delete")
    # Сохранение интервью автоматически строит схему
    items = post("/interviews/parse", {"text": main.EXAMPLE_INTERVIEW})["items"]
    res = post("/interviews/save", {"title": "Авто-схема", "text": main.EXAMPLE_INTERVIEW, "tasks": items})
    assert res["diagram_id"]


def test_demo_limits_and_daily_reset():
    for i in range(main.DEMO_LIMITS["employees"]):
        body = client.post("/crm/employees/add", json={"name": f"Бот {i}", "department": "Backend"}).get_json()
        if not body["success"]:
            break
    assert not body["success"] and "лимит" in body["error"]
    db = main.sqlite3.connect(main.DB_FILE)
    db.execute("UPDATE meta SET value = '2000-01-01' WHERE key = 'seeded_on'")
    db.commit()
    client.get("/crm/")  # первый запрос нового дня возвращает демо-данные
    assert db.execute("SELECT COUNT(*) FROM employees").fetchone()[0] == len(main.DEMO_TEAM)


def test_demo_reset():
    post("/demo/reset")
    db = main.sqlite3.connect(main.DB_FILE)
    assert db.execute("SELECT COUNT(*) FROM interviews").fetchone()[0] == 2


if __name__ == "__main__":
    tests = [v for k, v in list(globals().items()) if k.startswith("test_")]
    for t in tests:
        t()
        print(f"✔ {t.__name__}")
    print(f"Все тесты пройдены: {len(tests)}")
