import argparse
from pathlib import Path

from .io import read_json, write_json
from .schema import Drawing, Settings


def main(argv=None):
    import sys
    for stream in (sys.stdout, sys.stderr):
        if hasattr(stream, "reconfigure"):
            stream.reconfigure(encoding="utf-8")
    parser = argparse.ArgumentParser(description="本地图纸 → BOM → 逐面识别 → 约束装配 → 核对 → 三维浏览")
    sub = parser.add_subparsers(dest="command", required=True)
    command = sub.add_parser('process', help='完整本地流程：PDF → BOM/图元 → 拓扑 → 尺寸初审 → 三维预览')
    command.add_argument('pdf', type=Path)
    command.add_argument('--out', type=Path, required=True)
    command.add_argument('--table-regions', type=Path)
    command.add_argument('--review', type=Path)
    command.add_argument('--title')
    command.add_argument('--config', type=Path, help='完整流程配置，含诊断阈值和可选视觉复核')
    command = sub.add_parser('studio', help='启动本地图纸上传、处理进度和历史结果页面')
    command.add_argument('--root', type=Path, default=Path('runs/studio'))
    command.add_argument('--port', type=int, default=8770)
    command.add_argument('--config', type=Path)
    command = sub.add_parser('doctor', help='检查本地依赖及可选视觉服务的真实图像输入')
    command.add_argument('--config', type=Path)
    command.add_argument('--probe-vision', action='store_true')
    command.add_argument('--out', type=Path)
    command = sub.add_parser('final-check', help='汇总识别、拓扑、尺寸和三维的全流程终检')
    command.add_argument('--out', type=Path, required=True)
    command = sub.add_parser("decompose", help="分离表格并分类原生图元；不调用视觉模型")
    command.add_argument("pdf", type=Path)
    command.add_argument("--out", type=Path, required=True)
    command.add_argument("--table-regions", type=Path)
    command = sub.add_parser("topology", help="按结构面整理杆件、连接板、孔位和候选拓扑")
    command.add_argument("--out", type=Path, required=True)
    command.add_argument("--review", type=Path)
    command = sub.add_parser("audit", help="读取尺寸示意图并交叉检查结构方向、杆旁标注与 BOM")
    command.add_argument("--out", type=Path, required=True)
    command = sub.add_parser("preview", help="检查识别完整性并生成各结构面的基础三维预览")
    command.add_argument("--out", type=Path, required=True)
    command = sub.add_parser("model-audit", help="从模型几何重新测量并与原尺寸初审和 BOM 比对")
    command.add_argument("--out", type=Path, required=True)
    command.add_argument("--tolerance-mm", type=float, default=3.0)
    for name in ("prepare", "run"):
        command = sub.add_parser(name)
        command.add_argument("pdf", type=Path)
        command.add_argument("--out", type=Path, required=True)
        command.add_argument("--config", type=Path)
    command = sub.add_parser("extract")
    command.add_argument("--out", type=Path, required=True)
    command.add_argument("--config", type=Path)
    command = sub.add_parser("build")
    command.add_argument("observations", type=Path)
    command.add_argument("--out", type=Path, required=True)
    command.add_argument("--config", type=Path)
    command.add_argument("--review", type=Path)
    command = sub.add_parser("serve")
    command.add_argument("--out", type=Path, required=True)
    command.add_argument("--port", type=int, default=8766)
    command = sub.add_parser("probe")
    command.add_argument("--config", type=Path, required=True)
    command = sub.add_parser("schema")
    command.add_argument("--out", type=Path, required=True)
    command = sub.add_parser("evaluate")
    command.add_argument("model", type=Path)
    command.add_argument("reference", type=Path)
    command.add_argument("--node-map", type=Path)
    command.add_argument("--out", type=Path, required=True)
    args = parser.parse_args(argv)
    try:
        settings = Settings.model_validate(read_json(args.config)) if getattr(args, "config", None) and args.command not in {'process', 'studio', 'doctor'} else Settings()
        if args.command == 'studio':
            from .studio import serve_studio
            serve_studio(args.root, args.port, args.config)
            return 0
        if args.command == 'doctor':
            from .doctor import doctor
            import json
            result = doctor(args.config, args.probe_vision)
            if args.out:
                write_json(args.out, result)
            print(json.dumps(result, ensure_ascii=False, indent=2))
            return 0 if result['native_runtime_ready'] and result['vision_probe']['status'] in {'passed', 'not_requested'} else 2
        if args.command == 'final-check':
            from .final_audit import final_check
            final_check(args.out)
            return 2
        if args.command == 'process':
            from .workflow import process_drawing
            result = process_drawing(args.pdf, args.out, args.table_regions, args.review, args.title, args.config)
            return 1 if result['status'] == 'failed' else 2
        if args.command == "decompose":
            from .decomposition import decompose
            result = decompose(args.pdf, args.out, args.table_regions)
            return 2 if result["issues"] else 0
        if args.command == "topology":
            from .topology import topology
            result = topology(args.out, args.review)
            return 2 if result["status"] == "needs_review" else 0
        if args.command == "audit":
            from .dimension_audit import audit_dimensions
            audit_dimensions(args.out)
            return 2
        if args.command == "preview":
            from .model_preview import build_preview
            from .model_dimension_audit import audit_model_dimensions
            from .final_audit import final_check
            build_preview(args.out)
            audit_model_dimensions(args.out)
            final_check(args.out)
            return 2
        if args.command == "model-audit":
            from .model_dimension_audit import audit_model_dimensions
            audit_model_dimensions(args.out, args.tolerance_mm)
            return 2
        if args.command == "evaluate":
            from .evaluation import evaluate
            result = evaluate(read_json(args.model), read_json(args.reference), read_json(args.node_map) if args.node_map else None)
            write_json(args.out, result)
            print(f"[evaluate] pass={result['pass']}; precision={result['member_precision']:.4f}; recall={result['member_recall']:.4f}")
            return 0 if result["pass"] else 2
        if args.command == "schema":
            write_json(args.out, Drawing.model_json_schema())
            return 0
        if args.command == "serve":
            from .server import serve
            serve(args.out, args.port)
            return 0
        if args.command == "probe":
            from .vlm import VlmClient
            client = VlmClient(settings, Path("runs") / "probe-cache")
            try:
                print(client.probe())
            finally:
                client.close()
            return 0
        if args.command in {"prepare", "run"}:
            from .pdf_input import prepare
            manifest = prepare(args.pdf, args.out, settings.image_max_side)
            print(f"[prepare] {len(manifest['pages'])} pages -> {args.out}", flush=True)
            if args.command == "prepare":
                return 0
        if args.command in {"extract", "run"}:
            from .extraction import extract
            manifest = read_json(args.out / "manifest.json")
            drawing = extract(manifest, args.out, settings)
            if args.command == "extract":
                (args.out / "failure.json").unlink(missing_ok=True)
                return 0
        else:
            drawing = Drawing.model_validate(read_json(args.observations))
        from .pipeline import build
        report = build(drawing, args.out, settings, getattr(args, "review", None))
        (args.out / "failure.json").unlink(missing_ok=True)
        # Nonzero also for unreviewed candidates: suitable for automated gates.
        return 0 if report["status"] == "reviewed_frame" else 2
    except Exception as exc:
        import sys
        print(f"ERROR [{type(exc).__name__}]: {exc}", file=sys.stderr)
        if getattr(args, "out", None) and args.command not in {"schema", "evaluate", "process", "doctor"}:
            write_json(args.out / "failure.json", {"stage": args.command, "type": type(exc).__name__, "message": str(exc)})
        return 1
