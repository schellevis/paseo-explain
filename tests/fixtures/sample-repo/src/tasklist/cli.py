"""Command-line interface."""

import argparse

from tasklist import store


def build_parser():
    parser = argparse.ArgumentParser(prog="tasklist")
    sub = parser.add_subparsers(dest="command", required=True)
    add = sub.add_parser("add", help="add a task")
    add.add_argument("title")
    sub.add_parser("list", help="show all tasks")
    done = sub.add_parser("done", help="mark a task as done")
    done.add_argument("id", type=int)
    return parser


def main(argv=None):
    args = build_parser().parse_args(argv)
    if args.command == "add":
        task = store.add(args.title)
        print(f"added {task['id']}: {task['title']}")
    elif args.command == "list":
        for task in store.load():
            mark = "x" if task["done"] else " "
            print(f"[{mark}] {task['id']} {task['title']}")
    else:
        task = store.mark_done(args.id)
        if task is None:
            print("no such task")
            return 1
        print(f"done {task['id']}")
    return 0
