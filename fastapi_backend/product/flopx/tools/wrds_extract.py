"""Extract FLOPX's equity history from WRDS into plain CSV files (run on your own machine).

Why it runs locally: the WRDS database is a Postgres login (wrds-pgdata.wharton.upenn.edu:9737),
which the build sandbox cannot reach, and a login never needs to leave your machine. You upload the
output folder (data only, no credentials); the backend loads it into its archive with provenance
"licensed: WRDS".

Usage:
    pip install wrds pandas
    python wrds_extract.py --out flopx_wrds            # asks for your WRDS username and password
    python wrds_extract.py --out flopx_wrds --options  # also OptionMetrics implied vol, if subscribed

Output (in --out):
    crsp_daily.csv      date, ticker, permno, close, ret, volume, shares_out, cfacpr
    comp_quarterly.csv  datadate, ticker, gvkey, revenue_q, capex_ytd, capex_q, total_assets, report_date
    options_iv.csv      date, ticker, days, delta, impl_volatility (only with --options)
    coverage.json       per table: rows, first and last date per ticker, which source table answered

Exit codes: 0 on success (even if a ticker had no rows: see coverage.json), 1 when the connection
fails. Known blind spot: CRSP holds what your institution's subscription updates; many update
annually, so the last date may be months behind. coverage.json says exactly where each series ends.
"""

from __future__ import annotations

import argparse
import json
import sys
from pathlib import Path

TICKERS = ["NVDA", "CRWV", "NBIS", "IREN", "APLD", "CORZ", "VRT", "SMCI"]
START = "2023-01-01"


def _connect():
    try:
        import wrds  # type: ignore
    except ImportError:
        sys.exit("pip install wrds pandas first")
    try:
        return wrds.Connection()  # prompts for username and password; offers to save a .pgpass
    except Exception as exc:  # noqa: BLE001 - report and stop
        print(f"WRDS connection failed: {exc}", file=sys.stderr)
        sys.exit(1)


def _crsp(db) -> tuple["object", str]:
    tick = ",".join(f"'{t}'" for t in TICKERS)
    # The CIZ (v2) tables first; the legacy SIZ tables if this subscription predates them.
    queries = {
        "crsp.dsf_v2": f"""
            select d.dlycaldt as date, n.ticker, d.permno, d.dlyprc as close, d.dlyret as ret,
                   d.dlyvol as volume, d.shrout as shares_out, d.dlycumfacpr as cfacpr
            from crsp.dsf_v2 d
            join crsp.stocknames_v2 n on n.permno = d.permno and d.dlycaldt between n.namedt and n.nameenddt
            where n.ticker in ({tick}) and d.dlycaldt >= '{START}'""",
        "crsp.dsf": f"""
            select d.date, n.ticker, d.permno, abs(d.prc) as close, d.ret, d.vol as volume,
                   d.shrout as shares_out, d.cfacpr
            from crsp.dsf d
            join crsp.stocknames n on n.permno = d.permno and d.date between n.namedt and n.nameenddt
            where n.ticker in ({tick}) and d.date >= '{START}'""",
    }
    last_error = None
    for table, sql in queries.items():
        try:
            return db.raw_sql(sql, date_cols=["date"]), table
        except Exception as exc:  # noqa: BLE001 - try the next table
            last_error = exc
    raise RuntimeError(f"no CRSP daily table answered: {last_error}")


def _compustat(db):
    tick = ",".join(f"'{t}'" for t in TICKERS)
    sql = f"""
        select f.datadate, f.tic as ticker, f.gvkey, f.revtq as revenue_q, f.capxy as capex_ytd,
               f.atq as total_assets, f.rdq as report_date, f.fqtr
        from comp.fundq f
        where f.tic in ({tick}) and f.datadate >= '{START}'
          and f.indfmt = 'INDL' and f.datafmt = 'STD' and f.popsrc = 'D' and f.consol = 'C'"""
    df = db.raw_sql(sql, date_cols=["datadate", "report_date"])
    # capxy is year to date; the quarter's own capex is the difference within a fiscal year.
    df = df.sort_values(["ticker", "datadate"])
    df["capex_q"] = df.groupby(["ticker", df["datadate"].dt.year])["capex_ytd"].diff().fillna(df["capex_ytd"])
    df.loc[df["fqtr"] == 1, "capex_q"] = df["capex_ytd"]
    return df.drop(columns=["fqtr"])


def _options(db, crsp):
    permnos = sorted({int(p) for p in crsp["permno"].dropna()})
    links = db.raw_sql(f"select secid, permno from wrdsapps.opcrsphist where permno in ({','.join(map(str, permnos))})")
    if links.empty:
        return None
    secids = ",".join(str(int(s)) for s in links["secid"].unique())
    sql = f"""
        select v.date, v.secid, v.days, v.delta, v.impl_volatility
        from optionm.vsurfd v
        where v.secid in ({secids}) and v.days in (30, 91) and v.delta in (50, -50) and v.date >= '{START}'"""
    iv = db.raw_sql(sql, date_cols=["date"])
    ticker_by_permno = crsp.drop_duplicates("permno").set_index("permno")["ticker"]
    links["ticker"] = links["permno"].map(ticker_by_permno)
    return iv.merge(links[["secid", "ticker"]].drop_duplicates("secid"), on="secid").drop(columns=["secid"])


def _coverage(df, date_col: str) -> dict:
    out = {}
    for ticker, rows in df.groupby("ticker"):
        out[ticker] = {"rows": int(len(rows)), "first": str(rows[date_col].min().date()), "last": str(rows[date_col].max().date())}
    out["_missing"] = [t for t in TICKERS if t not in out]
    return out


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    parser.add_argument("--out", default="flopx_wrds", help="output folder (created)")
    parser.add_argument("--options", action="store_true", help="also extract OptionMetrics 30/91-day ATM implied vol")
    args = parser.parse_args()
    out = Path(args.out)
    out.mkdir(parents=True, exist_ok=True)
    db = _connect()
    coverage: dict = {"tickers": TICKERS, "start": START}

    crsp, table = _crsp(db)
    crsp.to_csv(out / "crsp_daily.csv", index=False)
    coverage["crsp_daily"] = {"table": table, **_coverage(crsp, "date")}
    print(f"CRSP daily: {len(crsp)} rows from {table}")

    try:
        comp = _compustat(db)
        comp.to_csv(out / "comp_quarterly.csv", index=False)
        coverage["comp_quarterly"] = {"table": "comp.fundq", **_coverage(comp, "datadate")}
        print(f"Compustat quarterly: {len(comp)} rows")
    except Exception as exc:  # noqa: BLE001 - optional table
        coverage["comp_quarterly"] = {"error": str(exc)}
        print(f"Compustat skipped: {exc}")

    if args.options:
        try:
            iv = _options(db, crsp)
            if iv is not None:
                iv.to_csv(out / "options_iv.csv", index=False)
                coverage["options_iv"] = {"table": "optionm.vsurfd", **_coverage(iv, "date")}
                print(f"OptionMetrics implied vol: {len(iv)} rows")
        except Exception as exc:  # noqa: BLE001 - optional table
            coverage["options_iv"] = {"error": str(exc)}
            print(f"OptionMetrics skipped: {exc}")

    (out / "coverage.json").write_text(json.dumps(coverage, indent=2))
    db.close()
    print(f"Done. Zip the folder '{out}' and upload it; it holds data only, no credentials.")


if __name__ == "__main__":
    main()
