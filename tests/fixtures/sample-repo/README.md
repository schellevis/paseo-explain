# tasklist

A small to-do list command-line tool. It keeps tasks in a JSON file and offers three commands: add, list and done.

## Run

```bash
python3 -m tasklist add "Buy milk"
python3 -m tasklist list
python3 -m tasklist done 1
```

Tasks are stored in `tasks.json` in the current directory.

## Test

```bash
python3 -m unittest
```

## Layout

- `src/tasklist/cli.py`: argument parsing and command dispatch.
- `src/tasklist/store.py`: loading and saving the task file.
- `docs/overview.md`: design notes.
