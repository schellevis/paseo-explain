"""Load and save tasks in a JSON file."""

import json
from pathlib import Path

DEFAULT_PATH = Path("tasks.json")


def load(path=DEFAULT_PATH):
    path = Path(path)
    if not path.exists():
        return []
    with open(path, encoding="utf-8") as handle:
        return json.load(handle)


def save(tasks, path=DEFAULT_PATH):
    with open(path, "w", encoding="utf-8") as handle:
        json.dump(tasks, handle, indent=2)


def add(title, path=DEFAULT_PATH):
    tasks = load(path)
    next_id = max((task["id"] for task in tasks), default=0) + 1
    task = {"id": next_id, "title": title, "done": False}
    tasks.append(task)
    save(tasks, path)
    return task


def mark_done(task_id, path=DEFAULT_PATH):
    tasks = load(path)
    for task in tasks:
        if task["id"] == task_id:
            task["done"] = True
            save(tasks, path)
            return task
    return None
