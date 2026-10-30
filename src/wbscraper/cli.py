"""Command-line interface: run | resume | status | export | rescore | sources | terms | report | check."""
from __future__ import annotations

import argparse
import logging
import logging.handlers
import os
import sys
import time
from pathlib import Path

from rich.console import Console
from rich.table import Table

from . import __version__, queries
from .app import REDDIT_SOURCE_NAMES, SOURCE_NAMES, App, build_app
from .config import Settings, deep_merge, load_dotenv, load_settings
from .export import export_csv, write_manifest
from .progress import Dashboard, print_summary
from .report import build_report
from .rescore import rescore_all
from .runner import Runner

console = Console()


def _settings(args) -> Settings:
    load_dotenv()
    overrides: dict = {}
    if getattr(args, "data_dir", None):
        overrides.setdefault("paths", {})["data_dir"] = args.data_dir
    if getattr(args, "since", None):
        overrides.setdefault("date_range", {})["since"] = args.since
    if getattr(args, "until", None):
        overrides.setdefault("date_range", {})["until"] = args.until
    only = getattr(args, "sources", None)
    if only:
        wanted = {s.strip() for s in only.split(",") if s.strip()}
        unknown = wanted - set(SOURCE_NAMES)
        if unknown:
            raise SystemExit(f"Unknown source(s): {', '.join(sorted(unknown))}. Available: {', '.join(SOURCE_NAMES)}")
        overrides["sources"] = {n: {"enabled": n in wanted} for n in SOURCE_NAMES}
    cfg_dir = Path(args.config_dir) if getattr(args, "config_dir", None) else None
    return load_settings(cfg_dir, overrides)


def _setup_logging(settings: Settings, level: str = "INFO") -> Path:
    settings.log_dir.mkdir(parents=True, exist_ok=True)
    path = settings.log_dir / f"run-{time.strftime('%Y%m%d-%H%M%S')}.log"
    handler = logging.handlers.RotatingFileHandler(path, maxBytes=5_000_000, backupCount=5, encoding="utf-8")
    handler.setFormatter(logging.Formatter("%(asctime)s %(levelname)s %(name)s: %(message)s"))
    root = logging.getLogger("wbscraper")
    root.handlers[:] = [handler]
    root.setLevel(getattr(logging, level.upper(), logging.INFO))
    root.propagate = False
    return path


def _do_export(app: App, include_borderline: bool | None = None, excel: bool = False, out: str | None = None):
    ib = app.settings.get("export.include_borderline", False) if include_borderline is None else include_borderline
    path = Path(out) if out else app.settings.csv_path
    res = export_csv(app.db, path, include_borderline=bool(ib),
                     min_chars=int(app.settings.get("export.min_text_chars", 12)), excel=excel)
    write_manifest(app.db, app.settings, res, app.term_files())
    return res


# ---------------------------------------------------------------------------------------------- commands
def cmd_run(args, resume: bool = False) -> int:
    settings = _settings(args)
    if args.fresh and settings.db_path.exists():
        backup = settings.db_path.with_name(f"collection-{time.strftime('%Y%m%d-%H%M%S')}.sqlite3.bak")
        settings.db_path.rename(backup)
        for ext in ("-wal", "-shm"):
            side = Path(str(settings.db_path) + ext)
            if side.exists():
                side.unlink()
        console.print(f"Archived previous state to {backup}")
    log_path = _setup_logging(settings, args.log_level)
    min_m = args.min_minutes if args.min_minutes is not None else settings.get("runtime.min_minutes", 120)
    max_m = args.max_minutes if args.max_minutes is not None else settings.get("runtime.max_minutes", 480)
    if max_m < min_m:
        raise SystemExit("--max-minutes must be >= --min-minutes")

    app = build_app(settings)
    try:
        since = None
        if args.refresh:
            last = app.db.get_meta("last_run_started")
            since = int(float(last)) if last else None
            console.print(f"Refresh mode: searching only items newer than {time.strftime('%Y-%m-%d', time.gmtime(since))}"
                          if since else "Refresh mode: no previous run found, collecting everything")
        added = queries.seed(app.db, app.lib, settings, since=since)
        app.db.set_meta("last_run_started", str(int(time.time())))
        pending = app.db.pending_count()
        console.print(f"{'Resuming' if resume or pending > added else 'Starting'}: {pending} task(s) queued ({added} new). "
                      f"Log file: {log_path}")
        if not app.providers:
            raise SystemExit("No sources enabled.")
        dash = Dashboard(app.stats, app.db, min_minutes=min_m, max_minutes=max_m, plain=args.no_dashboard or None)
        exp = lambda: _do_export(app)
        runner = Runner(settings, app.db, app.http, app.handlers, app.stats, progress=dash, exporter=exp)
        dash.start()
        try:
            result = runner.run(min_m, max_m, config_snapshot=settings.raw)
        finally:
            dash.stop()
        res = _do_export(app)
        print_summary(console, app.db, app.stats, result.stop_reason, result.elapsed_seconds, str(res.path), res.rows,
                      bool(settings.get("export.include_borderline", False)))
        return 0
    finally:
        app.close()


def cmd_status(args) -> int:
    settings = _settings(args)
    if not settings.db_path.exists():
        console.print("No collection database yet. Start one with: wbscraper run")
        return 0
    app = build_app(settings)
    try:
        db = app.db
        tiers = db.tier_counts()
        tc = db.task_counts()
        threads = db.conn.execute(
            "SELECT COUNT(DISTINCT thread_uid) FROM items WHERE tier='relevant' AND thread_uid IS NOT NULL").fetchone()[0]
        console.print(f"[bold]Items[/]: relevant {tiers.get('relevant', 0)}, borderline {tiers.get('borderline', 0)}, "
                      f"other {tiers.get('irrelevant', 0)}; unique threads with relevant items: {threads}")
        console.print(f"[bold]Tasks[/]: " + ", ".join(f"{k}={v}" for k, v in sorted(tc.items())))
        console.print(f"[bold]Total runtime so far[/]: {db.total_runtime_seconds() / 60:.1f} minutes over "
                      f"{db.conn.execute('SELECT COUNT(*) FROM sessions').fetchone()[0]} session(s)")
        last = db.conn.execute("SELECT * FROM sessions ORDER BY id DESC LIMIT 1").fetchone()
        if last:
            console.print(f"[bold]Last session[/]: stop reason = {last['stop_reason'] or 'still running or crashed'}")
        t = Table(title="Pending work by type")
        t.add_column("Type"); t.add_column("Status"); t.add_column("Count", justify="right")
        for r in db.task_counts_by_type():
            t.add_row(r["type"], r["status"], str(r["n"]))
        console.print(t)
        from .report import COUNTER_LABELS
        for key, val in sorted(db.counters().items()):
            if key in COUNTER_LABELS and val:
                console.print(f"  {COUNTER_LABELS[key]}: {val}")
        console.print(f"CSV (last export): {settings.csv_path}  exists={settings.csv_path.exists()}")
    finally:
        app.close()
    return 0


def cmd_export(args) -> int:
    settings = _settings(args)
    app = build_app(settings)
    try:
        res = _do_export(app, include_borderline=args.include_borderline or None, excel=args.excel, out=args.out)
        console.print(f"Wrote {res.rows} rows to [bold]{res.path}[/] (tiers: {', '.join(res.included_tiers)})")
    finally:
        app.close()
    return 0


def cmd_rescore(args) -> int:
    settings = _settings(args)
    app = build_app(settings)
    try:
        console.print(rescore_all(app.db, app.scorer))
    finally:
        app.close()
    return 0


def cmd_sources(args) -> int:
    settings = _settings(args)
    app = build_app(settings)
    try:
        t = Table(title="Source adapters")
        for c in ("Adapter", "Enabled", "Active", "Domain", "Role", "Credentials"):
            t.add_column(c)
        info = {
            "pullpush": ("reddit_pullpush", "Global Reddit keyword search (posts, comments)", "none"),
            "arcticshift": ("reddit_arcticshift", "Subreddit search, link posts, full threads", "none"),
            "discovery": ("reddit_discovery", "Search-engine discovery of Reddit threads (ddgs)", "none"),
            "praw": ("reddit_praw", "Official Reddit API fallback", "REDDIT_CLIENT_ID / REDDIT_CLIENT_SECRET (optional)"),
        }
        for key, (cfgname, role, creds) in info.items():
            prov = app.providers.get(key)
            enabled = settings.source_enabled(cfgname)
            active = bool(prov and prov.available())
            t.add_row(key, "yes" if enabled else "no", "[green]yes[/]" if active else "[yellow]no[/]",
                      prov.domain if prov else "-", role, creds)
        from .sources.registry import EXTRA_SOURCES
        for spec in EXTRA_SOURCES:
            enabled = settings.source_enabled(spec.name)
            t.add_row(spec.name, "yes" if enabled else "no", "[green]yes[/]" if enabled else "[yellow]no[/]",
                      spec.domains, spec.role, spec.credentials)
        console.print(t)
        console.print("Platform-by-platform status, including restricted ones: docs/SOURCE_COVERAGE.md")
    finally:
        app.close()
    return 0


def cmd_terms(args) -> int:
    settings = _settings(args)
    from .terms import TermLibrary
    lib = TermLibrary.load(settings.config_dir)
    st = queries.plan_stats(lib, settings)
    console.print("Planned tasks by family: " + ", ".join(f"{k}={v}" for k, v in st.items()))
    console.print(f"Entities: {len(lib.entities)}; subreddits: {len(queries.subreddit_tiers(lib))}; "
                  f"PullPush tokens: {len(queries.pullpush_tokens(lib))}")
    tasks = queries.plan_all(lib, settings)
    if args.dump:
        with open(args.dump, "w", encoding="utf-8") as fh:
            fh.write("priority\tfamily\ttype\tquery\tsubreddit\n")
            for ttype, p, pri, fam in sorted(tasks, key=lambda x: -x[2]):
                fh.write(f"{pri}\t{fam}\t{ttype}\t{p.get('q') or p.get('prefix') or p.get('url') or p.get('org') or ''}\t{p.get('subreddit') or p.get('site') or ''}\n")
        console.print(f"Wrote {len(tasks)} planned queries to {args.dump}")
    else:
        for fam in sorted({t[3] for t in tasks}):
            sample = [t for t in tasks if t[3] == fam][: args.sample]
            console.print(f"\n[bold]{fam}[/] (first {len(sample)}):")
            for ttype, p, pri, _ in sample:
                console.print(f"  [{pri}] {p.get('kind', '')} {p.get('q') or p.get('prefix') or p.get('url') or p.get('org') or ''}"
                              + (f"  (r/{p['subreddit']})" if p.get("subreddit") else ""))
    return 0


def cmd_report(args) -> int:
    settings = _settings(args)
    if not settings.db_path.exists():
        console.print("No collection database yet.")
        return 0
    app = build_app(settings)
    try:
        text = build_report(app.db)
        out = settings.output_dir / "coverage_report.md"
        out.parent.mkdir(parents=True, exist_ok=True)
        out.write_text(text, encoding="utf-8")
        console.print(text)
        console.print(f"Saved to {out}")
    finally:
        app.close()
    return 0


def cmd_check(args) -> int:
    """One lightweight request per provider so you can confirm connectivity. Run by the user, not by CI."""
    settings = _settings(args)
    _setup_logging(settings)
    app = build_app(settings)
    rc = 0
    try:
        for name, prov in app.providers.items():
            if not prov.available():
                console.print(f"[yellow]SKIP[/] {name}: not available (missing credentials or library)")
                continue
            try:
                console.print(f"[green]OK[/]   {name}: {prov.ping(app.http)}")
            except Exception as exc:
                rc = 1
                console.print(f"[red]FAIL[/] {name}: {exc}")
    finally:
        app.close()
    return rc


# ---------------------------------------------------------------------------------------------- parser
def build_parser() -> argparse.ArgumentParser:
    p = argparse.ArgumentParser(prog="wbscraper", description="California Water Boards public feedback scraper")
    p.add_argument("--version", action="version", version=f"%(prog)s {__version__}")
    common = argparse.ArgumentParser(add_help=False)
    common.add_argument("--config-dir", help="directory containing settings.yaml and terms/ (default: ./config)")
    common.add_argument("--data-dir", help="where the database, logs and CSV are kept (default: ./data)")
    sub = p.add_subparsers(dest="cmd", required=True)

    def run_args(sp):
        sp.add_argument("--min-minutes", type=float, help="retry window for deferred work (default 120)")
        sp.add_argument("--max-minutes", type=float, help="hard stop for this session (default 480)")
        sp.add_argument("--sources", help=f"comma list to enable only these: {', '.join(SOURCE_NAMES)}")
        sp.add_argument("--since", help="earliest date to collect, e.g. 2020-01-01 (default: all history)")
        sp.add_argument("--until", help="latest date to collect")
        sp.add_argument("--refresh", action="store_true", help="incremental: only content newer than the last run")
        sp.add_argument("--fresh", action="store_true", help="archive the old state and start from scratch")
        sp.add_argument("--no-dashboard", action="store_true", help="print a status line every 30s instead of a live panel")
        sp.add_argument("--log-level", default="INFO")

    r = sub.add_parser("run", parents=[common], help="start (or continue) a collection")
    run_args(r)
    r.set_defaults(func=lambda a: cmd_run(a, resume=False))
    rs = sub.add_parser("resume", parents=[common], help="continue an interrupted collection")
    run_args(rs)
    rs.set_defaults(func=lambda a: cmd_run(a, resume=True))
    sub.add_parser("status", parents=[common], help="show progress from the database").set_defaults(func=cmd_status)
    e = sub.add_parser("export", parents=[common], help="write the CSV (text,date,source_url)")
    e.add_argument("--out", help="output path (default data/output/waterboards_feedback.csv)")
    e.add_argument("--include-borderline", action="store_true", help="also export borderline items")
    e.add_argument("--excel", action="store_true", help="UTF-8 with BOM so Excel opens it correctly")
    e.set_defaults(func=cmd_export)
    sub.add_parser("rescore", parents=[common], help="re-apply relevance rules to stored items").set_defaults(func=cmd_rescore)
    sub.add_parser("sources", parents=[common], help="list source adapters").set_defaults(func=cmd_sources)
    t = sub.add_parser("terms", parents=[common], help="preview the generated search queries (offline)")
    t.add_argument("--sample", type=int, default=8, help="queries to show per family")
    t.add_argument("--dump", help="write every planned query to this TSV file")
    t.set_defaults(func=cmd_terms)
    sub.add_parser("report", parents=[common], help="coverage report from the database").set_defaults(func=cmd_report)
    sub.add_parser("check", parents=[common], help="one small request per provider (run it yourself)").set_defaults(func=cmd_check)
    return p


def main(argv: list[str] | None = None) -> int:
    args = build_parser().parse_args(argv)
    try:
        return int(args.func(args) or 0)
    except KeyboardInterrupt:
        console.print("\nInterrupted.")
        return 130


if __name__ == "__main__":
    sys.exit(main())
