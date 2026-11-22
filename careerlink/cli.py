"""命令行入口：python3 -m careerlink <command> ...

所有输出为 JSON（UTF-8），便于就业老师查看或接入其他工具。
"""
from __future__ import annotations

import argparse
import json
import sys
from pathlib import Path

from .models import norm_ts
from .service import CareerService


def _emit(obj) -> None:
    print(json.dumps(obj, ensure_ascii=False, indent=2))


def _load_existing(path: str) -> CareerService:
    if not Path(path).exists():
        raise SystemExit(f"存储文件不存在: {path}（先用 ingest 采集数据）")
    return CareerService.load(path)


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(
        prog="careerlink",
        description="高校就业服务：岗位采集、技能归类与培养跟进（合成数据）",
    )
    sub = parser.add_subparsers(dest="command", required=True)

    p = sub.add_parser("ingest", help="采集一个或多个来源文件")
    p.add_argument("store")
    p.add_argument("files", nargs="+")

    p = sub.add_parser("postings", help="按来源/状态/用工性质/有效期查看岗位")
    p.add_argument("store")
    p.add_argument("--source")
    p.add_argument("--state", choices=["open", "closed", "converted", "filled", "expired"])
    p.add_argument("--kind", dest="employment_kind")
    p.add_argument("--employer")
    p.add_argument("--as-of", dest="as_of")
    p.add_argument("--now")

    p = sub.add_parser("detail", help="查看岗位详情（技能、关联培养活动、待核实项）")
    p.add_argument("store")
    p.add_argument("canonical_id")
    p.add_argument("--now")

    p = sub.add_parser("verify", help="列出仍需向企业核实的变化")
    p.add_argument("store")
    p.add_argument("--now")

    p = sub.add_parser("recommend", help="生成一条学生推送（快照冻结当前依据）")
    p.add_argument("store")
    p.add_argument("canonical_id")
    p.add_argument("--course", dest="course_ref")
    p.add_argument("--student", dest="student_ref")
    p.add_argument("--now")

    p = sub.add_parser("push", help="回看已发推送的当时依据")
    p.add_argument("store")
    p.add_argument("push_id")
    p.add_argument("--now")

    p = sub.add_parser("course", help="查看课程及其关联岗位与反馈")
    p.add_argument("store")
    p.add_argument("course_ref")
    p.add_argument("--now")

    p = sub.add_parser("course-explain", help="解释课程调整来自哪些仍有效的企业需求")
    p.add_argument("store")
    p.add_argument("course_ref")
    p.add_argument("--now")

    p = sub.add_parser("summary", help="汇总统计（按用工性质分开计数）")
    p.add_argument("store")
    p.add_argument("--now")

    args = parser.parse_args(argv)
    now = norm_ts(args.now) if getattr(args, "now", None) else None

    if args.command == "ingest":
        service = CareerService.load_or_create(args.store)
        results = {}
        for path in args.files:
            results[path] = service.ingest_file(path)
        service.save(args.store)
        _emit({"store": args.store, "results": results})
        return 0

    service = _load_existing(args.store)
    try:
        if args.command == "postings":
            as_of = norm_ts(args.as_of) if args.as_of else None
            _emit(service.list_postings(
                source=args.source,
                state=args.state,
                employment_kind=args.employment_kind,
                employer=args.employer,
                as_of=as_of,
                now=now,
            ))
        elif args.command == "detail":
            _emit(service.posting_detail(args.canonical_id, now=now))
        elif args.command == "verify":
            _emit(service.pending_verifications(now=now))
        elif args.command == "recommend":
            result = service.recommend(
                args.canonical_id,
                course_ref=args.course_ref,
                student_ref=args.student_ref,
                now=now,
            )
            service.save(args.store)
            _emit(result)
        elif args.command == "push":
            _emit(service.review_push(args.push_id, now=now))
        elif args.command == "course":
            _emit(service.course_detail(args.course_ref, now=now))
        elif args.command == "course-explain":
            _emit(service.explain_course_adjustment(args.course_ref, now=now))
        elif args.command == "summary":
            _emit(service.summary(now=now))
    except KeyError as exc:
        print(str(exc), file=sys.stderr)
        return 1
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
