"""Command-line interface for paseo-explain."""

import argparse
import json
import os
import sys

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))

from explainlib import __version__


def true_false(value: str) -> bool:
    if value == "true":
        return True
    if value == "false":
        return False
    raise argparse.ArgumentTypeError("expected true or false")


def _role_path(value: str):
    if "=" not in value:
        raise argparse.ArgumentTypeError("expected ROLE=PATH")
    role, path = value.split("=", 1)
    if role not in ("spec", "plan", "brief", "resolution", "other"):
        raise argparse.ArgumentTypeError("invalid role")
    return (role, path)


def _check_question(value: str):
    if "::" not in value:
        raise argparse.ArgumentTypeError("expected Q::EXPECTED")
    question, expected = value.split("::", 1)
    return {"q": question, "expected": expected}


def format_success(result):
    if isinstance(result, str):
        return result
    if result is None:
        return ""
    return json.dumps(result, ensure_ascii=False, indent=2) + "\n"


def format_error(err) -> str:
    payload = {"ok": False, "errors": err.errors, **(err.extra or {})}
    return json.dumps(payload, ensure_ascii=False, indent=2) + "\n"


def _add_session(parser: argparse.ArgumentParser) -> None:
    parser.add_argument("--session", required=True)


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(prog="explain.py")
    parser.add_argument("--version", action="version", version=f"paseo-explain {__version__}")
    sub = parser.add_subparsers(dest="cmd", required=True)

    init = sub.add_parser("init")
    identity = init.add_mutually_exclusive_group(required=True)
    identity.add_argument("--slug")
    identity.add_argument("--autopilot")
    init.add_argument("--doc", choices=("spec", "plan"))
    init.add_argument("--kind", choices=("plan", "idea"))
    init.add_argument("--out")
    init.add_argument("--mode", choices=("quick", "standard", "deep"))
    init.add_argument("--level", type=int)
    init.add_argument("--levels", type=int, choices=(3, 5))
    init.add_argument("--lang")

    frame = sub.add_parser("frame")
    _add_session(frame)
    frame.add_argument("--audience")
    frame.add_argument("--question")
    frame.add_argument("--out-of-scope", action="append", dest="out_of_scope")
    frame.add_argument("--check-question", action="append", dest="check_questions", type=_check_question)
    frame.add_argument("--orchestrator-model")

    ingest = sub.add_parser("ingest")
    _add_session(ingest)
    ingest.add_argument("--autopilot")
    ingest.add_argument("--doc", choices=("spec", "plan"))
    ingest.add_argument("--file", action="append", dest="files", type=_role_path)
    ingest.add_argument("--text-file", dest="text_file")

    validate = sub.add_parser("validate")
    _add_session(validate)

    check_prepare = sub.add_parser("check-prepare")
    _add_session(check_prepare)
    check_prepare.add_argument("--kind", required=True, choices=("factcheck", "reader"))

    check_report = sub.add_parser("check-report")
    _add_session(check_report)
    check_report.add_argument("--kind", required=True, choices=("factcheck", "reader"))
    check_report.add_argument("--file")

    apply_corrections = sub.add_parser("apply-corrections")
    _add_session(apply_corrections)
    apply_corrections.add_argument("--remove", action="append", dest="remove")

    skip = sub.add_parser("skip")
    _add_session(skip)
    skip.add_argument("--step", required=True, choices=("fact_check", "reader_test"))
    skip.add_argument("--reason", required=True)

    grade = sub.add_parser("grade")
    _add_session(grade)
    grade.add_argument("--correct", required=True, type=int)

    md = sub.add_parser("md")
    _add_session(md)
    md.add_argument("--level", required=True, type=int)
    md.add_argument("--to")

    render = sub.add_parser("render")
    _add_session(render)

    serve = sub.add_parser("serve")
    serve.add_argument("--root")
    serve.add_argument("--port", type=int)
    serve.add_argument("--host", default="127.0.0.1")

    show = sub.add_parser("show")
    _add_session(show)
    show.add_argument("--no-start", action="store_true")

    sub.add_parser("stop")

    inject = sub.add_parser("inject")
    _add_session(inject)
    inject.add_argument("--max-bytes", dest="max_bytes", type=int, default=400000)

    tab = sub.add_parser("tab")
    _add_session(tab)
    tab.add_argument("--opened", required=True, type=true_false)

    result = sub.add_parser("result")
    _add_session(result)

    sub.add_parser("config")
    sub.add_parser("lint")

    leaks = sub.add_parser("leaks")
    leaks.add_argument("--repo", action="store_true")
    leaks.add_argument("--path", action="append", dest="path")
    return parser


def _validate_args(parser: argparse.ArgumentParser, args) -> None:
    if args.cmd == "init":
        if args.autopilot and not args.doc:
            parser.error("--autopilot requires --doc")
        if args.slug and not args.kind:
            parser.error("--kind is required unless --autopilot is given")
    elif args.cmd == "ingest":
        modes = sum(bool(item) for item in (args.autopilot or args.doc, args.files, args.text_file))
        if modes != 1:
            parser.error("ingest requires exactly one of --autopilot/--doc, --file, or --text-file")
        if bool(args.autopilot) != bool(args.doc):
            parser.error("--autopilot and --doc are both required")
    elif args.cmd == "serve" and args.port is None:
        raw = os.environ.get("PASEO_PORT")
        if raw is None or raw == "":
            parser.error("serve requires --port or PASEO_PORT")
        try:
            int(raw)
        except ValueError:
            parser.error("PASEO_PORT must be an integer")
    elif args.cmd == "leaks":
        if args.repo == bool(args.path):
            parser.error("leaks requires exactly one of --repo or --path")


def cmd_init(args):
    from explainlib.common import init_session

    return init_session(
        kind=args.kind,
        slug=args.slug,
        autopilot=args.autopilot,
        doc=args.doc,
        out=args.out,
        mode=args.mode,
        level=args.level,
        levels=args.levels,
        lang=args.lang,
    )


def cmd_frame(args):
    from explainlib import validate

    return validate.frame(
        args.session,
        audience=args.audience,
        question=args.question,
        out_of_scope=args.out_of_scope,
        check_questions=args.check_questions,
        orchestrator_model=args.orchestrator_model,
    )


def cmd_ingest(args):
    from explainlib import ingest

    return ingest.ingest(
        args.session,
        autopilot=args.autopilot,
        doc=args.doc,
        files=args.files,
        text_file=args.text_file,
    )


def cmd_validate(args):
    from explainlib import validate

    return validate.run_validate(args.session)


def cmd_check_prepare(args):
    from explainlib import reports

    return reports.prepare(args.session, args.kind)


def cmd_check_report(args):
    from explainlib import reports

    return reports.check_report(args.session, args.kind, args.file)


def cmd_apply_corrections(args):
    from explainlib import reports

    return reports.apply_corrections(args.session, args.remove or [])


def cmd_skip(args):
    from explainlib import reports

    return reports.skip(args.session, args.step, args.reason)


def cmd_grade(args):
    from explainlib import reports

    return reports.grade(args.session, args.correct)


def cmd_md(args):
    from explainlib import render

    return render.markdown_command(args.session, args.level, args.to)


def cmd_render(args):
    from explainlib import render

    return render.render(args.session)


def cmd_serve(args):
    from explainlib.common import ExplainError, sessions_root
    from explainlib import serve as serve_mod

    port = args.port if args.port is not None else int(os.environ["PASEO_PORT"])
    root = args.root if args.root is not None else str(sessions_root())
    try:
        serve_mod.serve(root, args.host, port)
    except ValueError as exc:
        raise ExplainError(2, str(exc)) from exc
    return None


def cmd_show(args):
    from explainlib import show

    return show.show(args.session, start=not args.no_start)


def cmd_stop(args):
    from explainlib import show

    return show.stop()


def cmd_inject(args):
    from explainlib import render

    return render.inject(args.session, max_bytes=args.max_bytes)


def cmd_tab(args):
    from explainlib import result

    return result.set_tab(args.session, args.opened)


def cmd_result(args):
    from explainlib import result

    return result.write_result(args.session)


def cmd_config(args):
    from explainlib.config import config_report

    return config_report()


def cmd_lint(args):
    from explainlib import lint

    return lint.lint()


def cmd_leaks(args):
    from explainlib import leaks

    if args.repo:
        return leaks.leaks_repo()
    return leaks.leaks_paths(args.path)


_DISPATCH = {
    "init": cmd_init,
    "frame": cmd_frame,
    "ingest": cmd_ingest,
    "validate": cmd_validate,
    "check-prepare": cmd_check_prepare,
    "check-report": cmd_check_report,
    "apply-corrections": cmd_apply_corrections,
    "skip": cmd_skip,
    "grade": cmd_grade,
    "md": cmd_md,
    "render": cmd_render,
    "serve": cmd_serve,
    "show": cmd_show,
    "stop": cmd_stop,
    "inject": cmd_inject,
    "tab": cmd_tab,
    "result": cmd_result,
    "config": cmd_config,
    "lint": cmd_lint,
    "leaks": cmd_leaks,
}


def main(argv=None) -> int:
    parser = build_parser()
    args = parser.parse_args(argv)
    _validate_args(parser, args)
    from explainlib.common import ExplainError

    try:
        result = _DISPATCH[args.cmd](args)
    except ExplainError as err:
        sys.stdout.write(format_error(err))
        return err.code
    if isinstance(result, str):
        sys.stdout.write(result)
    elif isinstance(result, dict):
        sys.stdout.write(format_success(result))
    return 0


if __name__ == "__main__":
    sys.exit(main())
