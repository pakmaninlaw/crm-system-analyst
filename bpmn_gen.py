"""
Генератор BPMN 2.0: описание процесса на русском языке -> XML-схема с раскладкой (координатами).

Схему можно открыть в любом BPMN-редакторе (bpmn.io, Camunda Modeler, Stormbpmn) — это стандарт ISO/IEC 19510.

Язык описания («алгоритм по шагам»), одна строка — один шаг:

    Старт: Клиент оформил заказ                 — начальное событие (тонкий круг)
    Клиент: Оформить заказ                      — задача в дорожке «Клиент»
    Система: Проверить наличие                  — «Система», «CRM», «1С», «ПО» -> сервисная задача (выполняет программа)
    Если товар есть — Склад: Собрать, иначе Менеджер: Предложить замену → вернуться к Проверить наличие
                                                — исключающий шлюз XOR (ромб с «X»): ровно одна ветка
    Параллельно: Бухгалтерия: Счёт; Склад: Упаковка — параллельный шлюз AND (ромб с «+»): все ветки сразу
    Ждать оплату (3 дня)                        — промежуточное событие-таймер (двойной круг с часами)
    Подпроцесс: Доставка                        — свёрнутый подпроцесс (прямоугольник с «+»)
    Отправить уведомление / Получить ответ      — задачи отправки и получения сообщения
    ... (конец)                                 — ветка заканчивается конечным событием
    Конец: Заказ доставлен                      — конечное событие (жирный круг)

Внутри ветки шаги разделяются стрелкой «→» или словом «затем».
"""
import re
from itertools import count
from xml.sax.saxutils import quoteattr

# ---------- Размеры и сетка раскладки (в пикселях) ----------
TASK_W, TASK_H = 120, 80
EVENT_D = 36
GATEWAY_D = 50
COL_W = 170          # шаг по горизонтали между колонками
SUBROW_H = 120       # высота «подстроки» внутри дорожки (для параллельных веток)
LANE_LABEL_W = 30    # ширина подписи дорожки
POOL_LABEL_W = 30    # ширина подписи пула
LEFT = 40            # отступ до первого элемента внутри дорожки

SYSTEM_ROLES = re.compile(r"^(система|crm|срм|1с|по|сервер|сайт|приложение|бот|робот)\b", re.I)
KEYWORDS = re.compile(r"^(если|параллельно|старт|начало|конец|финиш|ждать|ожидание|таймер|подпроцесс)\b", re.I)


# =====================================================================
# 1. МОДЕЛЬ: узлы и потоки
# =====================================================================
class Node:
    def __init__(self, kind, name, lane):
        self.kind = kind      # startEvent, endEvent, task, userTask, serviceTask, sendTask, receiveTask,
        self.name = name      # subProcess, exclusiveGateway, parallelGateway, timerEvent
        self.lane = lane
        self.col = 0
        self.subrow = 0
        self.id = ""

    @property
    def size(self):
        if self.kind in ("startEvent", "endEvent", "timerEvent"):
            return EVENT_D, EVENT_D
        if self.kind in ("exclusiveGateway", "parallelGateway"):
            return GATEWAY_D, GATEWAY_D
        return TASK_W, TASK_H


class Flow:
    def __init__(self, source, target, name="", loop=False):
        self.source, self.target, self.name, self.loop = source, target, name, loop
        self.id = ""


class Model:
    def __init__(self, title):
        self.title = title
        self.nodes, self.flows, self.lanes = [], [], []

    def add(self, kind, name, lane):
        if lane not in self.lanes:
            self.lanes.append(lane)
        node = Node(kind, name, lane)
        self.nodes.append(node)
        return node

    def connect(self, a, b, name="", loop=False):
        self.flows.append(Flow(a, b, name, loop))


# =====================================================================
# 2. РАЗБОР ТЕКСТА: строки -> блоки
# =====================================================================
def split_role(text, default_role):
    """«Склад: Собрать заказ» -> ("Склад", "Собрать заказ"). Без роли — роль предыдущего шага."""
    m = re.match(r"^\s*([А-ЯЁA-Z0-9][^:]{0,40}?)\s*:\s*(.+)$", text)
    if m and not KEYWORDS.match(m.group(1)):
        return m.group(1).strip(), m.group(2).strip()
    return default_role, text.strip()


def task_kind(role, text):
    t = text.lower()
    if t.startswith(("отправить", "уведомить", "сообщить", "направить")):
        return "sendTask"
    if t.startswith(("получить", "дождаться ответа", "принять заявку")):
        return "receiveTask"
    if SYSTEM_ROLES.match(role):
        return "serviceTask"
    return "userTask"


def cap(s):
    s = s.strip().rstrip(".")
    return s[:1].upper() + s[1:]


def parse_step(text, role):
    """Один шаг ветки -> описание: задача / конец / возврат к шагу."""
    text = text.strip().rstrip(".")
    m = re.match(r"^(вернуться|возврат|повторить)\s+(к|на)?\s*(.+)$", text, re.I)
    if m:
        return {"type": "loop", "target": m.group(3).strip()}, role
    m = re.match(r"^(конец|финиш|завершить процесс)\s*:?\s*(.*)$", text, re.I)
    if m:
        return {"type": "end", "name": cap(m.group(2) or "Процесс завершён")}, role
    role, body = split_role(text, role)
    ends = bool(re.search(r"\((конец|завершение)\)\s*$", body, re.I))
    body = re.sub(r"\s*\((конец|завершение)\)\s*$", "", body, flags=re.I)
    if re.match(r"^(ждать|ожидание|таймер)\b", body, re.I):
        return {"type": "timer", "name": cap(re.sub(r"^(таймер\s*:)\s*", "", body, flags=re.I)), "role": role, "ends": ends}, role
    if re.match(r"^подпроцесс\s*:", body, re.I):
        return {"type": "sub", "name": cap(body.split(":", 1)[1]), "role": role, "ends": ends}, role
    return {"type": "task", "name": cap(body), "role": role, "ends": ends}, role


def parse_branch(text, role):
    steps, r = [], role
    for part in re.split(r"\s*(?:→|->|\bзатем\b)\s*", text):
        if part.strip():
            step, r = parse_step(part, r)
            steps.append(step)
    return steps


def parse_text(text, title="Процесс"):
    """Текст -> список блоков: ("start", имя) / ("step", шаг) / ("xor", условие, [ветки]) / ("and", [ветки]) / ("end", имя)."""
    blocks, role = [], "Процесс"
    start_name, end_name = None, None
    for raw in text.splitlines():
        line = raw.strip().strip("•-–—*").strip()
        if not line or line.startswith("#"):
            continue
        m = re.match(r"^(старт|начало)\s*:\s*(.+)$", line, re.I)
        if m:
            start_name = cap(m.group(2))
            continue
        m = re.match(r"^(конец|финиш)\s*:\s*(.+)$", line, re.I)
        if m:
            end_name = cap(m.group(2))
            continue
        m = re.match(r"^если\s+(.+?)(?:,?\s+то\s+|\s*[—–]\s*|\s+-\s+|\s*:\s*)(.+?)(?:[,;]?\s*иначе\s*[:—–-]?\s*(.+))?$", line, re.I)
        if m:
            cond = cap(m.group(1))
            cond = cond if cond.endswith("?") else cond + "?"
            yes = parse_branch(m.group(2), role)
            no = parse_branch(m.group(3), role) if m.group(3) else []
            blocks.append(("xor", cond, [("Да", yes), ("Нет", no)]))
            continue
        m = re.match(r"^(параллельно|одновременно)\s*:?\s*(.+)$", line, re.I)
        if m:
            branches = [("", parse_branch(part, role)) for part in re.split(r"\s*;\s*", m.group(2)) if part.strip()]
            blocks.append(("and", branches))
            continue
        step, role = parse_step(line, role)
        blocks.append(("step", step))
    return {"title": title, "start": start_name or "Начало", "end": end_name or "Конец", "blocks": blocks}


# =====================================================================
# 3. ПОСТРОЕНИЕ ГРАФА И РАСКЛАДКА ПО СЕТКЕ
# =====================================================================
def build(spec):
    model = Model(spec["title"])
    first_role = next((b[1].get("role") for b in spec["blocks"] if b[0] == "step" and b[1].get("role")), "Процесс")
    start = model.add("startEvent", spec["start"], first_role)
    start.col = 0
    tail = [start]          # узлы, от которых идёт поток к следующему шагу
    tail_labels = [""]      # подписи этих потоков («Да»/«Нет»)
    col = 1
    loops = []              # (узел-источник, имя целевого шага)
    last_role = first_role

    def place(node, c, sub=0):
        node.col, node.subrow = c, sub
        return node

    def attach(node):
        for t, label in zip(tail, tail_labels):
            model.connect(t, node, label)

    def make_step(step, c, sub):
        """Создать узел шага. Возвращает (узел, закончилась_ли_ветка)."""
        nonlocal last_role
        if step["type"] == "end":
            return place(model.add("endEvent", step["name"], last_role), c, sub), True
        kind = {"timer": "timerEvent", "sub": "subProcess"}.get(step["type"]) or task_kind(step["role"], step["name"])
        last_role = step["role"]
        node = place(model.add(kind, step["name"], step["role"]), c, sub)
        if step.get("ends"):
            return node, "end-after"
        return node, False

    for block in spec["blocks"]:
        if block[0] == "step":
            step = block[1]
            if step["type"] == "loop":
                loops.extend((t, step["target"]) for t in tail)
                tail, tail_labels = [], []
                continue
            node, ended = make_step(step, col, 0)
            attach(node)
            col += 1
            if ended is True:
                tail, tail_labels = [], []
            elif ended == "end-after":
                end = place(model.add("endEvent", "Процесс завершён", node.lane), col, 0)
                model.connect(node, end)
                col += 1
                tail, tail_labels = [], []
            else:
                tail, tail_labels = [node], [""]
            continue

        # ----- Шлюзы: XOR (исключающее ИЛИ) и AND (параллельно) -----
        is_xor = block[0] == "xor"
        branches = block[2] if is_xor else block[1]
        gw_lane = tail[0].lane if tail else last_role
        split = place(model.add("exclusiveGateway" if is_xor else "parallelGateway",
                                block[1] if is_xor else "", gw_lane), col, 0)
        attach(split)
        col += 1
        branch_tails = []   # (узел, подпись) — что сходится в объединяющий шлюз
        max_len = 0
        lane_usage = {}     # ветки в одной дорожке раскладываем по «подстрокам», в разных — на одной линии
        for label, steps in branches:
            first_lane = next((s.get("role") for s in steps if s.get("role")), gw_lane)
            i = lane_usage.get(first_lane, 0)
            lane_usage[first_lane] = i + 1
            prev, prev_label, c, open_end = split, label, col, True
            if not steps:   # пустая ветка «иначе» — сразу к объединению
                branch_tails.append((split, label))
                continue
            for step in steps:
                if step["type"] == "loop":
                    loops.append((prev, step["target"], prev_label))
                    open_end = False
                    break
                node, ended = make_step(step, c, i)
                model.connect(prev, node, prev_label)
                prev, prev_label, c = node, "", c + 1
                if ended is True:
                    open_end = False
                    break
                if ended == "end-after":
                    end = place(model.add("endEvent", "Процесс завершён", node.lane), c, i)
                    model.connect(node, end)
                    c += 1
                    open_end = False
                    break
            max_len = max(max_len, c - col)
            if open_end:
                branch_tails.append((prev, prev_label))
        col += max(max_len, 1)
        if len(branch_tails) > 1 or (not is_xor and branch_tails):
            join = place(model.add("exclusiveGateway" if is_xor else "parallelGateway", "", gw_lane), col, 0)
            for node, label in branch_tails:
                model.connect(node, join, label)
            col += 1
            tail, tail_labels = [join], [""]
        elif branch_tails:
            tail, tail_labels = [branch_tails[0][0]], [branch_tails[0][1]]
        else:
            tail, tail_labels = [], []

    if tail:
        end = place(model.add("endEvent", spec["end"], tail[0].lane), col, 0)
        attach(end)

    # Возвраты «вернуться к …»: ищем шаг по началу названия
    for item in loops:
        src, target_name = item[0], item[1]
        label = item[2] if len(item) > 2 else ""
        target = next((n for n in model.nodes if n.name.lower().startswith(target_name.lower().strip(" .«»\"")[:25])), None)
        if target:
            model.connect(src, target, label, loop=True)
    return model


# =====================================================================
# 4. XML BPMN 2.0 + DI (координаты для отрисовки)
# =====================================================================
def to_xml(model):
    ids = count(1)
    for n in model.nodes:
        n.id = f"{n.kind[0].upper()}{n.kind[1:]}_{next(ids)}"
    for f in model.flows:
        f.id = f"Flow_{next(ids)}"

    # Высота дорожек: сколько «подстрок» использует каждая
    subrows = {lane: 1 for lane in model.lanes}
    for n in model.nodes:
        subrows[n.lane] = max(subrows[n.lane], n.subrow + 1)
    lane_y, y = {}, 0
    for lane in model.lanes:
        lane_y[lane] = y
        y += subrows[lane] * SUBROW_H + 20
    total_h = y
    max_col = max((n.col for n in model.nodes), default=0)
    x0 = POOL_LABEL_W + LANE_LABEL_W + LEFT
    total_w = x0 + (max_col + 1) * COL_W + 20

    pos = {}
    for n in model.nodes:
        w, h = n.size
        cx = x0 + n.col * COL_W + TASK_W / 2
        cy = lane_y[n.lane] + 10 + n.subrow * SUBROW_H + SUBROW_H / 2
        pos[n] = (cx - w / 2, cy - h / 2, w, h)

    def waypoints(f):
        sx, sy, sw, sh = pos[f.source]
        tx, ty, tw, th = pos[f.target]
        scx, scy, tcx, tcy = sx + sw / 2, sy + sh / 2, tx + tw / 2, ty + th / 2
        if f.loop:   # возврат назад — по нижнему краю нижней из двух дорожек, не пересекая остальные
            lower = max((f.source.lane, f.target.lane), key=lambda ln: lane_y[ln])
            bottom = lane_y[lower] + subrows[lower] * SUBROW_H + 20 - 10
            return [(scx, sy + sh), (scx, bottom), (tcx, bottom), (tcx, ty + th)]
        if abs(scy - tcy) < 1:
            return [(sx + sw, scy), (tx, tcy)]
        if f.source.kind.endswith("Gateway"):    # из шлюза: вверх/вниз, затем вправо
            return [(scx, sy + sh if tcy > scy else sy), (scx, tcy), (tx, tcy)]
        if f.target.kind.endswith("Gateway"):    # в шлюз: вправо, затем вверх/вниз
            return [(sx + sw, scy), (tcx, scy), (tcx, ty + th if scy > tcy else ty)]
        mid = (sx + sw + tx) / 2
        return [(sx + sw, scy), (mid, scy), (mid, tcy), (tx, tcy)]

    tag = {
        "startEvent": "startEvent", "endEvent": "endEvent", "task": "task", "userTask": "userTask",
        "serviceTask": "serviceTask", "sendTask": "sendTask", "receiveTask": "receiveTask",
        "subProcess": "subProcess", "exclusiveGateway": "exclusiveGateway",
        "parallelGateway": "parallelGateway", "timerEvent": "intermediateCatchEvent",
    }
    out = [
        '<?xml version="1.0" encoding="UTF-8"?>',
        '<bpmn:definitions xmlns:bpmn="http://www.omg.org/spec/BPMN/20100524/MODEL" '
        'xmlns:bpmndi="http://www.omg.org/spec/BPMN/20100524/DI" '
        'xmlns:dc="http://www.omg.org/spec/DD/20100524/DC" '
        'xmlns:di="http://www.omg.org/spec/DD/20100524/DI" '
        'id="Definitions_1" targetNamespace="http://bpmn.io/schema/bpmn" exporter="CRM аналитика">',
        '  <bpmn:collaboration id="Collaboration_1">',
        f'    <bpmn:participant id="Pool_1" name={quoteattr(model.title)} processRef="Process_1" />',
        '  </bpmn:collaboration>',
        '  <bpmn:process id="Process_1" isExecutable="false">',
        '    <bpmn:laneSet id="LaneSet_1">',
    ]
    for i, lane in enumerate(model.lanes, 1):
        out.append(f'      <bpmn:lane id="Lane_{i}" name={quoteattr(lane)}>')
        out += [f"        <bpmn:flowNodeRef>{n.id}</bpmn:flowNodeRef>" for n in model.nodes if n.lane == lane]
        out.append("      </bpmn:lane>")
    out.append("    </bpmn:laneSet>")
    for n in model.nodes:
        inc = [f"      <bpmn:incoming>{f.id}</bpmn:incoming>" for f in model.flows if f.target is n]
        outg = [f"      <bpmn:outgoing>{f.id}</bpmn:outgoing>" for f in model.flows if f.source is n]
        name = f" name={quoteattr(n.name)}" if n.name else ""
        body = inc + outg
        if n.kind == "timerEvent":
            body.append(f'      <bpmn:timerEventDefinition id="Timer_{n.id}" />')
        out.append(f"    <bpmn:{tag[n.kind]} id=\"{n.id}\"{name}>")
        out += body
        out.append(f"    </bpmn:{tag[n.kind]}>")
    for f in model.flows:
        name = f" name={quoteattr(f.name)}" if f.name else ""
        out.append(f'    <bpmn:sequenceFlow id="{f.id}"{name} sourceRef="{f.source.id}" targetRef="{f.target.id}" />')
    out.append("  </bpmn:process>")

    # ----- Диаграмма (где что нарисовать) -----
    out += ['  <bpmndi:BPMNDiagram id="Diagram_1">', '    <bpmndi:BPMNPlane id="Plane_1" bpmnElement="Collaboration_1">',
            '      <bpmndi:BPMNShape id="Pool_1_di" bpmnElement="Pool_1" isHorizontal="true">',
            f'        <dc:Bounds x="0" y="0" width="{total_w:.0f}" height="{total_h:.0f}" />',
            "      </bpmndi:BPMNShape>"]
    for i, lane in enumerate(model.lanes, 1):
        h = subrows[lane] * SUBROW_H + 20
        out += [f'      <bpmndi:BPMNShape id="Lane_{i}_di" bpmnElement="Lane_{i}" isHorizontal="true">',
                f'        <dc:Bounds x="{POOL_LABEL_W}" y="{lane_y[lane]:.0f}" width="{total_w - POOL_LABEL_W:.0f}" height="{h:.0f}" />',
                "      </bpmndi:BPMNShape>"]
    for n in model.nodes:
        x, y, w, h = pos[n]
        extra = ' isExpanded="false"' if n.kind == "subProcess" else ""
        extra += ' isMarkerVisible="true"' if n.kind == "exclusiveGateway" else ""
        out += [f'      <bpmndi:BPMNShape id="{n.id}_di" bpmnElement="{n.id}"{extra}>',
                f'        <dc:Bounds x="{x:.0f}" y="{y:.0f}" width="{w:.0f}" height="{h:.0f}" />']
        if n.name and n.kind in ("startEvent", "endEvent", "timerEvent", "exclusiveGateway", "parallelGateway"):
            out += ["        <bpmndi:BPMNLabel>",
                    f'          <dc:Bounds x="{x + w / 2 - 55:.0f}" y="{y + h + 6:.0f}" width="110" height="28" />',
                    "        </bpmndi:BPMNLabel>"]
        out.append("      </bpmndi:BPMNShape>")
    for f in model.flows:
        out.append(f'      <bpmndi:BPMNEdge id="{f.id}_di" bpmnElement="{f.id}">')
        out += [f'        <di:waypoint x="{x:.0f}" y="{y:.0f}" />' for x, y in waypoints(f)]
        out.append("      </bpmndi:BPMNEdge>")
    out += ["    </bpmndi:BPMNPlane>", "  </bpmndi:BPMNDiagram>", "</bpmn:definitions>"]
    return "\n".join(out)


def from_text(text, title="Процесс"):
    """Главная функция: описание на русском -> BPMN XML."""
    spec = parse_text(text, title)
    if not spec["blocks"]:
        raise ValueError("В описании нет ни одного шага")
    return to_xml(build(spec))


# =====================================================================
# 5. ГОТОВЫЕ СЦЕНАРИИ ДЛЯ CRM
# =====================================================================
LIFECYCLE_TEXT = """Старт: Требование получено
Аналитик: Зафиксировать требование в CRM
CRM: Определить отдел и исполнителя
Исполнитель: Выполнить задачу
Исполнитель: Отправить на проверку
Аналитик: Проверить результат
Если работа принята — CRM: Закрыть задачу и учесть в KPI, иначе Аналитик: Описать замечание → вернуться к Выполнить задачу
Конец: Задача выполнена"""

EXAMPLE_TEXT = """Старт: Клиент оформил заказ
Клиент: Оформить заказ на сайте
Система: Проверить наличие товара
Если товар в наличии — Система: Зарезервировать товар, иначе Менеджер: Предложить замену → вернуться к Проверить наличие товара
Параллельно: Бухгалтерия: Выставить счёт; Склад: Собрать заказ
Ждать оплату (до 3 дней)
Если оплата получена — Склад: Отгрузить заказ, иначе Менеджер: Отменить заказ (конец)
Конец: Заказ доставлен"""


def interview_text(interview_title, stakeholder, dept_counts):
    """Процесс реализации требований из интервью: стейкхолдер -> аналитик -> отделы параллельно -> приёмка."""
    who = "Стейкхолдер"
    lines = [
        "Старт: Потребность бизнеса",
        f"{who}: Сформулировать потребность",
        "Аналитик: Провести интервью",
        "CRM: Сформировать задачи по отделам",
    ]
    active = [(d, n) for d, n in dept_counts if n]
    if len(active) > 1:
        lines.append("Параллельно: " + "; ".join(f"{d}: Подпроцесс: Задачи {d} ({n})" for d, n in active))
    elif active:
        d, n = active[0]
        lines.append(f"{d}: Подпроцесс: Задачи {d} ({n})")
    lines += [
        "Аналитик: Проверить результат",
        f"Если требования выполнены — {who}: Принять результат, иначе Аналитик: Вернуть на доработку → вернуться к CRM: Сформировать",
        "Конец: Требования реализованы",
    ]
    # «вернуться к …» ищет шаг по началу названия — упростим цель
    text = "\n".join(lines).replace("вернуться к CRM: Сформировать", "вернуться к Сформировать задачи")
    return text
