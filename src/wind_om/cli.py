"""Command line interface.

Usage:
    python -m wind_om report alarms.csv --config my_oem.yaml --mapping my_codes.csv --month 2026-09 --out kpi/
    python -m wind_om report alarms.csv --config my_oem.yaml --mapping my_codes.csv --month 2026-09 \\
        --scada scada.parquet --rated-kw 3600 --tz Europe/Warsaw --out kpi/     # with lost energy
    python -m wind_om analyse alarms.csv --config my_oem.yaml --mapping my_codes.csv --out tables/
    python -m wind_om template alarms.csv --config my_oem.yaml --out my_codes.csv   # mapping to fill in
    python -m wind_om demo --out demo/                   # synthetic log, mapping and a KPI pack
    python -m wind_om fetch                              # event logs of the open farms (about 40 MB)
    python -m wind_om study                              # the study on Kelmarsh, Penmanshiel, Hill of Towie
    python -m wind_om export penmanshiel --tz Europe/London --out demo/   # an open farm as a vendor-style CSV
    python -m wind_om dst-check kelmarsh --tz Europe/London   # local-time round trip of a real log

Paths come from arguments or environment variables only: WIND_OM_DATA_DIR (default ./data),
KELMARSH_DATA_DIR (Kelmarsh archives) and WIND_DOWNTIME_DATA_DIR (SCADA caches of wind-downtime-availability).
"""
from __future__ import annotations

import argparse
import json
import logging
import os
import sys
from pathlib import Path

import pandas as pd

log = logging.getLogger("wind_om")


def _settings(args):
    from wind_om.analysis import Settings

    return Settings(merge_gap=args.merge_gap, repeat_days=args.repeat_days, cascade_window=args.cascade_window,
                    auto_max=args.auto_max, min_failure_h=args.min_failure_h, max_silence=args.max_silence)


def _common(p):
    p.add_argument("log", type=Path, help="event log export (CSV, Excel or Parquet)")
    p.add_argument("--config", type=Path, help="YAML column mapping (default: the canonical format)")
    p.add_argument("--mapping", help="code mapping CSV (or a shipped one: senvion_mm, siemens_swt_hot)")
    p.add_argument("--coverage", type=Path, help="CSV turbine_id,start,end of periods with valid logging (UTC)")
    p.add_argument("--cod", type=Path, help="CSV turbine_id,cod: commissioning dates (age origin of Crow-AMSAA)")
    p.add_argument("--start", help="analysis start (UTC date), default: first event")
    p.add_argument("--end", help="analysis end (UTC date), default: last event")
    p.add_argument("--merge-gap", default="10min", help="stops closer than this form one episode")
    p.add_argument("--repeat-days", type=float, default=7.0)
    p.add_argument("--cascade-window", default="2min")
    p.add_argument("--auto-max", default="15min", help="longest stop still taken as an automatic reset")
    p.add_argument("--min-failure-h", type=float, default=0.0)
    p.add_argument("--max-silence", help="e.g. 7D: no event at all for longer = not logged (logs with routine "
                                         "messages only)")
    p.add_argument("--scada", type=Path, help="10-minute SCADA in the wind-downtime-availability long format")
    p.add_argument("--rated-kw", type=float)
    p.add_argument("--out", type=Path, required=True)


def _load(args):
    from wind_om.analysis import analyse
    from wind_om.mapping import read_mapping
    from wind_om.schema import read_log

    ev = read_log(args.log, args.config)
    mapping = read_mapping(args.mapping) if args.mapping else None
    cov = None
    if args.coverage:
        cov = pd.read_csv(args.coverage, dtype={"turbine_id": str})
        cov["start"], cov["end"] = pd.to_datetime(cov["start"], utc=True), pd.to_datetime(cov["end"], utc=True)
    origin = None
    if args.cod:
        c = pd.read_csv(args.cod, dtype={"turbine_id": str})
        origin = dict(zip(c["turbine_id"], pd.to_datetime(c["cod"], utc=True)))
    a = analyse(ev, mapping, args.start, args.end, cov, _settings(args), origin)
    if args.scada:
        from wind_om.scada import SCADA, attribute_lost, scada_fleet

        sc = SCADA.build(scada_fleet(args.scada, args.rated_kw))
        a.seg = attribute_lost(a.seg, sc)
    return a


def main(argv=None) -> int:
    p = argparse.ArgumentParser(prog="wind_om", description=__doc__.splitlines()[0])
    p.add_argument("-v", "--verbose", action="store_true")
    sub = p.add_subparsers(dest="cmd", required=True)

    r = sub.add_parser("report", help="monthly KPI pack (Excel + HTML) from an event log")
    _common(r)
    r.add_argument("--month", required=True, help="YYYY-MM in --tz")
    r.add_argument("--tz", default="Europe/Warsaw", help="time zone of the reporting months")
    r.add_argument("--farm", default="farm", help="name printed on the report")

    a = sub.add_parser("analyse", help="all tables for the whole period (CSV)")
    _common(a)

    t = sub.add_parser("template", help="empty code mapping for every code of a log")
    t.add_argument("log", type=Path)
    t.add_argument("--config", type=Path)
    t.add_argument("--out", type=Path, required=True)

    d = sub.add_parser("demo", help="synthetic log + mapping + KPI pack, no data needed")
    d.add_argument("--out", type=Path, default=Path("demo"))

    f = sub.add_parser("fetch", help="download the open farms' event logs (about 40 MB)")
    f.add_argument("--data-dir", type=Path, default=Path(os.environ.get("WIND_OM_DATA_DIR", "data")))

    s = sub.add_parser("study", help="run the study on the three open farms")
    s.add_argument("--data-dir", type=Path, default=Path(os.environ.get("WIND_OM_DATA_DIR", "data")))
    s.add_argument("--scada-dir", type=Path, default=Path(os.environ.get("WIND_DOWNTIME_DATA_DIR", "")) or None,
                   help="data folder of wind-downtime-availability (SCADA caches for lost energy)")
    s.add_argument("--results", type=Path, default=Path("results"))
    s.add_argument("--docs", type=Path, default=Path("docs"))
    s.add_argument("--farms", nargs="+", default=["kelmarsh", "penmanshiel", "hill_of_towie"])

    e = sub.add_parser("export", help="an open farm's log as a vendor-style CSV + YAML + mapping")
    e.add_argument("farm", choices=["kelmarsh", "penmanshiel", "hill_of_towie"])
    e.add_argument("--data-dir", type=Path, default=Path(os.environ.get("WIND_OM_DATA_DIR", "data")))
    e.add_argument("--tz", default="UTC", help="write naive local times in this zone (tests the DST handling)")
    e.add_argument("--out", type=Path, required=True)

    c = sub.add_parser("dst-check", help="write an open farm's log in local time, read it back, compare")
    c.add_argument("farm", choices=["kelmarsh", "penmanshiel", "hill_of_towie"])
    c.add_argument("--tz", default="Europe/London")
    c.add_argument("--data-dir", type=Path, default=Path(os.environ.get("WIND_OM_DATA_DIR", "data")))
    c.add_argument("--results", type=Path, default=Path("results"))

    args = p.parse_args(argv)
    logging.basicConfig(level=logging.DEBUG if args.verbose else logging.INFO,
                        format="%(asctime)s %(levelname)s %(name)s: %(message)s", stream=sys.stderr)

    if args.cmd == "report":
        from wind_om.report import kpi_pack, write_excel, write_html

        an = _load(args)
        pack = kpi_pack(an, args.month, args.tz, args.farm)
        args.out.mkdir(parents=True, exist_ok=True)
        write_excel(pack, args.out / f"kpi_{args.month}.xlsx")
        write_html(pack, args.out / f"kpi_{args.month}.html")
        for k, v in pack.summary.items():
            log.info("%-42s %s", k, v)
    elif args.cmd == "analyse":
        from wind_om.study import full_tables

        an = _load(args)
        args.out.mkdir(parents=True, exist_ok=True)
        tables, summary = full_tables(an)
        for name, df in tables.items():
            df.to_csv(args.out / f"{name}.csv", index=False)
        (args.out / "summary.json").write_text(json.dumps(summary, indent=2, default=str), encoding="utf-8")
        log.info("%d tables written to %s", len(tables), args.out)
    elif args.cmd == "template":
        from wind_om.mapping import mapping_template
        from wind_om.schema import read_log

        tpl = mapping_template(read_log(args.log, args.config))
        args.out.parent.mkdir(parents=True, exist_ok=True)
        tpl.to_csv(args.out, index=False)
        log.info("%d codes written to %s", len(tpl), args.out)
    elif args.cmd == "demo":
        from wind_om.study import demo

        demo(args.out)
    elif args.cmd == "fetch":
        from wind_om.fetch import METER, fetch

        fetch(args.data_dir)
        log.info("received %.1f MB", METER.bytes / 1e6)
    elif args.cmd == "study":
        from wind_om.study import run_study

        run_study(args.data_dir, args.scada_dir, args.results, args.docs, args.farms)
    elif args.cmd == "export":
        from wind_om.study import export_farm

        export_farm(args.farm, args.data_dir, args.out, args.tz)
    elif args.cmd == "dst-check":
        from wind_om.study import dst_roundtrip

        dst_roundtrip(args.farm, args.data_dir, args.tz, args.results)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
