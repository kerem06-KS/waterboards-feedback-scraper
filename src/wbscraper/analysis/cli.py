"""`wbscraper analyze` and `wbscraper analyze-eval` (registered from wbscraper.cli)."""
from __future__ import annotations

import time
from pathlib import Path

from rich.console import Console

from ..config import find_config_dir, load_dotenv
from ..terms import TermLibrary
from .config import load_analysis_config

console = Console()


def _load(args):
    load_dotenv()
    config_dir = Path(args.config_dir) if getattr(args, "config_dir", None) else find_config_dir()
    data_dir = Path(args.data_dir) if getattr(args, "data_dir", None) else Path("data")
    return load_analysis_config(config_dir, data_dir), TermLibrary.load(config_dir)


def cmd_analyze(args) -> int:
    from .run import analyze
    cfg, lib = _load(args)
    inp = Path(args.input) if args.input else (cfg.path("borderline_input") if args.include_borderline else cfg.path("input"))
    if not inp.exists():
        console.print(f"[red]Input not found:[/] {inp}. Run `wbscraper export` first.")
        return 2
    res = analyze(cfg, lib, input_path=inp, out_path=Path(args.out) if args.out else None, tag=args.tag, retag=args.retag,
                  dry_run=args.dry_run)
    if args.dry_run:
        t = res.tagging
        console.print(f"[bold]Dry run[/] (no API calls): {t['total']} posts, {t['cached']} already tagged, {t['to_tag']} would be sent "
                      f"(~{t['approx_input_tokens']:,} input tokens, roughly ${t['approx_cost_usd']:.2f}).")
        return 0
    c = res.counts
    console.print(f"Scored {res.n} posts: [red]{c.get('negative', 0)} negative[/], {c.get('neutral', 0)} neutral, "
                  f"[green]{c.get('positive', 0)} positive[/].")
    console.print(f"  scored CSV : {res.paths['scored']}\n  summary    : {res.paths['summary']}\n"
                  f"  review file: {res.paths['review']}  ({res.flagged} rows flagged for a look; {res.reviewed} with your overrides, "
                  f"{res.changed} label changes)")
    if res.tagging and not res.tagging.get("dry_run"):
        t = res.tagging
        console.print(f"  tagging    : {t['calls']} API calls, {t['refusals']} refusals, {t['errors']} errors, "
                      f"{t['input_tokens']:,} in / {t['output_tokens']:,} out tokens, about ${t['cost_usd']:.2f}")
    for w in res.warnings:
        console.print(f"[yellow]{w}[/]")
    return 0


def cmd_analyze_eval(args) -> int:
    from . import tune
    cfg, lib = _load(args)
    try:
        relevant = tune.load_gold(cfg.path("gold_relevant"))
        tuning = tune.load_gold(cfg.path("gold_tuning"))
        try:
            tuning += tune.load_gold(cfg.path("gold_pool"))
        except (FileNotFoundError, KeyError):
            pass
    except FileNotFoundError as e:
        console.print(f"[red]Hand-labelled file missing:[/] {e.filename}. See docs/SENTIMENT.md for the format.")
        return 2
    res = tune.evaluate(cfg, lib, relevant, tuning, ablate=not args.no_ablation)
    icwsm = None
    if args.icwsm:
        icwsm = tune.icwsm_check(cfg, lib, Path(args.icwsm))
    md = tune.render_report(res, icwsm, time.strftime("%Y-%m-%d"))
    out = cfg.path("eval_report")
    out.parent.mkdir(parents=True, exist_ok=True)
    out.write_text(md, encoding="utf-8")
    tune.save_results(res, out.with_suffix(".json"))
    console.print(md)
    L = res["locked"]
    if L:
        cur = cfg.thresholds
        if tuple(L["thresholds"]) != tuple(cur):
            console.print(f"[yellow]Note:[/] this run picked cutoffs {L['thresholds']} but config/analysis/settings.yaml has {cur}. "
                          f"Settings are changed by hand, never automatically.")
    console.print(f"Report written to {out}")
    return 0


def register(sub, common) -> None:
    a = sub.add_parser("analyze", parents=[common],
                       help="sentiment score + label for each exported post; optional task/pain-point tags; review file; summary")
    a.add_argument("--in", dest="input", help="input CSV (default data/output/waterboards_feedback.csv)")
    a.add_argument("--out", help="scored CSV path (default data/output/waterboards_feedback_scored.csv)")
    a.add_argument("--include-borderline", action="store_true", help="score data/output/borderline_review.csv instead")
    a.add_argument("--tag", action="store_true", help="call the Claude API for task and pain-point tags (costs money; cached)")
    a.add_argument("--retag", action="store_true", help="with --tag: ignore cached tags")
    a.add_argument("--dry-run", action="store_true", help="show how many posts would be tagged and the estimated cost; no API calls")
    a.set_defaults(func=cmd_analyze)
    e = sub.add_parser("analyze-eval", parents=[common],
                       help="accuracy of the sentiment scorer against the hand-labelled posts; tuning report")
    e.add_argument("--icwsm", help="folder with the VADER paper's *_GroundTruth.txt files for the general-text check")
    e.add_argument("--no-ablation", action="store_true", help="skip the what-each-rule-adds table")
    e.set_defaults(func=cmd_analyze_eval)
