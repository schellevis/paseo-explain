# Overview

## Flow

`python3 -m tasklist add "Buy milk"` runs `__main__.py`, which calls `cli.main`.
The command parser hands the title to the store, which appends a task and writes the file.

## Decisions

- Tasks live in one JSON file so the data stays readable and easy to back up.
- Identifiers are consecutive integers, never reused within a file.
