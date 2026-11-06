import argparse
import json
from pathlib import Path
import sys

def main():
    parser = argparse.ArgumentParser(description="Offline Markdown to Word desktop app and verification tools")
    from . import __version__
    parser.add_argument("--version", action="version", version="Markdown 转 Word " + __version__)
    sub = parser.add_subparsers(dest="command")
    gui = sub.add_parser("gui", help="Open the full offline editor and Word export interface")
    gui.add_argument("--verify-output", type=Path, help="Opt-in fixed-sample UI verification; requires a new directory")
    gui.add_argument("--verify-local-output", type=Path, help="Opt-in isolated preview-selection formatting verification")
    gui.add_argument("--verify-state-output", type=Path, help="Opt-in isolated fixed-sample state verification")
    gui.add_argument("--verify-state-worker", choices=("seed", "activate", "recover", "review5", "review6"), help=argparse.SUPPRESS)
    gui.add_argument("--verify-product-output", type=Path, help=argparse.SUPPRESS)
    for name in ("smoke", "math-probe"):
        p = sub.add_parser(name)
        p.add_argument("--output-dir", type=Path, required=True)
    p = sub.add_parser("preview")
    p.add_argument("--verify-report", type=Path)
    p.add_argument("--screenshot", type=Path)
    p = sub.add_parser("inspect", help="Parse/check input without exporting DOCX")
    p.add_argument("input", type=Path)
    p.add_argument("--mode", choices=["markdown", "plain"], required=True)
    p = sub.add_parser("convert-temp", help="Create a validated temporary DOCX, no user-target commit")
    p.add_argument("input", type=Path)
    p.add_argument("--mode", choices=["markdown", "plain"], required=True)
    p.add_argument("--work-root", type=Path)
    p = sub.add_parser("convert-styled", help="Create a styled temporary lecture DOCX")
    p.add_argument("input", type=Path)
    p.add_argument("--mode", choices=["markdown", "plain"], required=True)
    p.add_argument("--work-root", type=Path)
    p.add_argument("--style-file", type=Path)
    p = sub.add_parser("export", help="Background export with validated no-clobber output commit")
    p.add_argument("input", type=Path)
    p.add_argument("--mode", choices=["markdown", "plain"], required=True)
    p.add_argument("--output", type=Path, required=True)
    p.add_argument("--overwrite", action="store_true", help="Recheck existing target; unsupported replacement fails safely")
    p.add_argument("--timeout", type=float, default=60.0)
    p.add_argument("--work-root", type=Path)
    args = parser.parse_args()
    if args.command in (None, "gui"):
        if getattr(args, "verify_product_output", None):
            from .ui.product_verification import verify_product
            return verify_product(args.verify_product_output)
        if getattr(args,"verify_local_output",None):
            from .ui.local_verification import verify_local_gui
            return verify_local_gui(args.verify_local_output)
        if getattr(args, "verify_state_output", None):
            if not args.verify_state_worker:
                parser.error("--verify-state-worker is required for isolated state verification")
            from .ui.state_verification import verify_state_worker
            return verify_state_worker(args.verify_state_output, args.verify_state_worker)
        if getattr(args, "verify_output", None):
            from .ui.verification import verify_gui
            return verify_gui(args.verify_output)
        from .ui.main_window import run_app
        return run_app()
    if args.command == "preview":
        from .preview import show_preview
        return show_preview(getattr(args, "verify_report", None),
                            getattr(args, "screenshot", None))
    try:
        if args.command == "export":
            from .input import read_input
            from .domain.models import InputMode
            from .templates.service import resolve_style
            from .exporting.models import ExportSnapshot, TaskState
            from .exporting.output import OutputIntent
            from .exporting.service import ExportService
            from .exporting.scheduler import Scheduler
            session = read_input(args.input, InputMode(args.mode))
            snapshot = ExportSnapshot.capture(session, resolve_style())
            intent = OutputIntent.capture(args.output, overwrite=args.overwrite)
            scheduler = Scheduler(ExportService(work_root=args.work_root))
            handle = scheduler.submit(snapshot, intent, timeout=args.timeout)
            try:
                try:
                    result = handle.result()
                except KeyboardInterrupt:
                    handle.cancel()
                    result = handle.result()
                print(json.dumps({
                    "stage": "05", "state": result.state.value,
                    "source_revision": result.snapshot.input.revision,
                    "style_revision": result.snapshot.style_revision,
                    "snapshot_id": result.snapshot.snapshot_id,
                    "output": str(result.output_path) if result.output_path else None,
                    "sha256": result.sha256,
                    "failure": result.failure.code if result.failure else None,
                    "elapsed_seconds": result.elapsed_seconds,
                    "issues": [{"code": i.code, "severity": i.severity.value} for i in result.issues],
                    "states": [e.state.value for e in handle.context.drain_events()],
                }, ensure_ascii=True, indent=2))
                return 0 if result.state == TaskState.SUCCEEDED else 2
            finally:
                scheduler.dispose()
        if args.command in {"convert-temp", "convert-styled"}:
            from .input import read_input
            from .domain.models import InputMode
            from .conversion.service import ConversionService
            style = None
            if args.command == "convert-styled":
                from .templates.service import resolve_style
                from .domain.style_models import StyleConfig
                personal = None
                if args.style_file:
                    from .domain.models import failure
                    try:
                        with args.style_file.open("rb") as stream:
                            raw = stream.read(128 * 1024 + 1)
                        if len(raw) > 128 * 1024:
                            raise ValueError("Oversized config")
                        personal = StyleConfig.parse_config(json.loads(raw.decode("utf-8-sig")))
                    except (OSError, ValueError):
                        raise failure("STYLE_READ_FAILED", "无法读取所选样式配置。", "检查 UTF-8 JSON 文件。", "style") from None
                style = resolve_style(personal=personal)
            result = ConversionService().convert_temp(
                read_input(args.input, InputMode(args.mode)).snapshot(), work_root=args.work_root, style=style)
            print(json.dumps({
                "stage": "04" if style is not None else "03", "temporary_docx": str(result.docx_path),
                "style_hash": style.resolved_hash if style is not None else None,
                "user_target_committed": False, "validated": result.validation.passed,
                "math_objects": result.validation.math_objects,
                "math_fallbacks": len(result.checked.math_fallbacks),
                "elapsed_seconds": result.elapsed_seconds,
                "issues": [{"code": i.code, "severity": i.severity.value} for i in result.checked.issues],
            }, ensure_ascii=True, indent=2))
            return 0
        if args.command == "inspect":
            from .input import read_input
            from .parsing import Parser
            from .domain.models import InputMode
            from .domain.normalizer import normalize
            from .domain.repairs import suggest_outer_fence
            session = read_input(args.input, InputMode(args.mode))
            snapshot = session.snapshot()
            result = normalize(Parser().parse(snapshot))
            print(json.dumps({
                "stage": "02", "export_performed": False,
                "ready_for_next_stage": result.exportable,
                "nodes": len(result.node_index), "manifest_entries": len(result.manifest.entries),
                "fallbacks": len(result.fallback_records),
                "repair_available": suggest_outer_fence(snapshot).suggestion is not None,
                "issues": [{"code": i.code, "severity": i.severity.value} for i in result.issues],
            }, ensure_ascii=False, indent=2))
            return 0 if result.exportable else 2
        from .spike import smoke, math_probe
        if args.command == "smoke":
            report = smoke(args.output_dir.resolve())
            print(json.dumps({k: v for k, v in report.items() if k != "text"},
                             ensure_ascii=False, indent=2))
        else:
            report = math_probe(args.output_dir.resolve())
            print(json.dumps([{"ordinal": x["ordinal"], "status": x["status"]}
                              for x in report["singles"]]))
        return 0
    except Exception as exc:
        print(f"{getattr(exc, 'code', type(exc).__name__)}: {exc}", file=sys.stderr)
        return 1

if __name__ == "__main__":
    raise SystemExit(main())
