"""Calendar calculations shared by operational contacts and draft previews."""

import copy
from datetime import date, timedelta


def schedule_configuration(content):
    if "schedule" in content:
        return copy.deepcopy(content["schedule"])
    # Compatibility adapter: old revisions keep their original representation.
    return {
        "contacts": {slug: dict(rule, active=True, label="Contato com cliente", description="")
                     for slug, rule in content["contact_rules"].items()},
        "inss": [{"key": key, "label": label, "description": "", "active": True,
                  "delay_business_days": 0, "depends_on": content["inss_tasks"][index - 1][0] if index else None}
                 for index, (key, label, _) in enumerate(content["inss_tasks"])],
    }


def evenly_spaced(dates, count):
    if not count or len(dates) <= count:
        return dates
    if count == 1:
        return dates[:1]
    return sorted({dates[round(i * (len(dates) - 1) / (count - 1))] for i in range(count)})


def contact_calendar(unlock_date, started_on, rules):
    rows = []
    for slug, rule in rules.items():
        if not rule.get("active", True):
            continue
        start = max(started_on, unlock_date - timedelta(days=rule["window_ceiling_days"]))
        end = unlock_date - timedelta(days=rule["window_floor_days"])
        weeks = {}
        day = start
        while day <= end:
            if day.weekday() < 5:
                weeks.setdefault(day.isocalendar()[:2], []).append(day)
            day += timedelta(days=1)
        for days in weeks.values():
            for day in evenly_spaced(days, rule["contacts_per_week"]):
                rows.append({"key": f"unlock-contact:{slug}:{day.isoformat()}", "stage_slug": slug,
                             "date": day, "label": rule.get("label", "Contato com cliente"), "kind": "contact"})
    return sorted(rows, key=lambda row: (row["date"], row["key"]))


def business_deadline(released_on, days):
    result = released_on
    while days:
        result += timedelta(days=1)
        if result.weekday() < 5:
            days -= 1
    return result


def parse_day(value, label):
    try:
        if not isinstance(value, str) or len(value) != 10:
            raise ValueError
        parsed = date.fromisoformat(value)
        if not 1900 <= parsed.year <= 2200:
            raise ValueError
        return parsed
    except (ValueError, TypeError):
        raise ValueError(f"Informe uma data valida para {label} (1900 a 2200).") from None


def simulate_schedule(content, concession, started_on):
    unlock_date = concession + timedelta(days=90)
    rules = schedule_configuration(content)
    rows = contact_calendar(unlock_date, started_on, rules["contacts"])
    deadline = started_on
    if started_on < unlock_date:
        for rule in rules["inss"]:
            if not rule["active"]:
                continue
            deadline = business_deadline(deadline, rule["delay_business_days"])
            rows.append({"key": rule["key"], "label": rule["label"], "date": deadline,
                         "stage_slug": None, "kind": "inss", "estimated": True,
                         "depends_on": rule["depends_on"]})
    rows.sort(key=lambda row: (row["date"], 0 if row["kind"] == "inss" else 1))
    warnings = []
    if not any(rule["active"] for rule in rules["contacts"].values()):
        warnings.append("Todos os contatos estao inativos nesta versao.")
    if any(not rule["active"] for rule in rules["inss"]):
        warnings.append("A sequencia INSS possui tarefas inativas nesta versao.")
    if started_on >= unlock_date:
        warnings.append("Entrada no dia ou depois do desbloqueio: nenhuma nova agenda de fidelizacao.")
    if any(row["kind"] == "inss" and row["date"] >= unlock_date for row in rows):
        warnings.append("Um prazo estimado do INSS alcanca ou ultrapassa o desbloqueio.")
    return {"unlock_date": unlock_date.isoformat(), "days_left": (unlock_date - started_on).days,
            "rows": [dict(row, date=row["date"].isoformat()) for row in rows], "warnings": warnings,
            "counts": {kind: sum(row["kind"] == kind for row in rows) for kind in ("contact", "inss")}}


def preview_replan(content, concession, started_on, existing, today, effective_from):
    if effective_from <= today:
        raise ValueError("Replanejamento deve comecar depois de hoje; tarefas de hoje e atrasadas sao preservadas.")
    if effective_from > today + timedelta(days=91):
        raise ValueError("Escolha uma data de inicio nos proximos 91 dias.")
    rules = schedule_configuration(content)
    desired = {row["key"]: row for row in contact_calendar(concession + timedelta(days=90), started_on, rules["contacts"])
               if row["date"] >= effective_from}
    rows = []
    occupied = {task.task_key: task for task in existing}
    for task in existing:
        is_contact = task.task_key.startswith("unlock-contact:")
        replacement = desired.get(task.task_key)
        if task.completed_at:
            action, reason = "preserve", "Tarefa concluida"
        elif task.note or task.completed_by_id:
            action, reason = "preserve", "Possui registro de atuacao"
        elif not is_contact:
            action, reason = "preserve", "INSS ou tarefa adicional fora do replanejamento de contatos"
        elif not task.active:
            action, reason = "preserve", "Tarefa inativa; reativacao automatica bloqueada"
        elif task.scheduled_for < effective_from:
            action, reason = "preserve", "Prazo anterior ao inicio; inclui hoje e atrasos"
        elif replacement:
            action, reason = "preserve", "Data mantida; nome e configuracao originais preservados"
        else:
            action, reason = "deactivate", "Contato futuro fora da nova agenda"
        rows.append({"task_id": task.id, "key": task.task_key, "label": task.label,
                     "date": task.scheduled_for.isoformat(), "action": action, "reason": reason})
    # A completed/inactive occurrence occupies its key and is never recreated.
    for key, row in desired.items():
        if key not in occupied:
            rows.append({"task_id": None, "key": key, "label": row["label"], "date": row["date"].isoformat(),
                         "action": "create", "reason": "Novo contato futuro"})
    rows.sort(key=lambda row: (row["date"], row["key"]))
    return {"rows": rows, "effective_from": effective_from.isoformat(),
            "counts": {action: sum(row["action"] == action for row in rows) for action in ("preserve", "deactivate", "create")},
            "preview_only": True}
