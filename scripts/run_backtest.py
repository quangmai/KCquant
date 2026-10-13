#!/usr/bin/env python3
"""Backtest carry, trend and KC/RC spread signals on KC and RC (point in time).

Uses the cached Databento data (no downloads). Writes to output/backtest/:
  metrics.csv, subperiods.csv, annual_returns.csv, robustness.csv,
  latest_signals.csv, daily_<sleeve>.csv, backtest_overview.png

    python scripts/run_backtest.py
    python scripts/run_backtest.py --ticks 2 --target-vol 0.10
"""
import argparse
import logging
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "src"))

import numpy as np  # noqa: E402
import pandas as pd  # noqa: E402

from kcquant import signals  # noqa: E402
from kcquant.backtest import BacktestConfig, annual_returns, combine, metrics, run_sleeve  # noqa: E402
from kcquant.contracts import KC, RC  # noqa: E402
from kcquant.data import SnapshotStatisticsLoader  # noqa: E402
from kcquant.features import curve_features  # noqa: E402
from kcquant.roll import FixedDaysBeforeFND, RollConfig, build_continuous  # noqa: E402

ROLL_DAYS = {"KC": 10, "RC": 5}


def load_market(spec, start, end):
    loader = SnapshotStatisticsLoader(spec, cache_dir=ROOT / "data" / "raw", client=object())
    panel = loader.load(start, end, with_definitions=False, offline=True)
    res = build_continuous(panel, spec, RollConfig(rule=FixedDaysBeforeFND(ROLL_DAYS[spec.root])))
    return res.daily, curve_features(panel, spec)


def build_sleeves(mk, cfg, carry_full=0.10, trend_lb=(63, 126, 252), spread_win=252):
    (d_kc, f_kc), (d_rc, f_rc) = mk["KC"], mk["RC"]
    sp = signals.spread_zscore(f_kc, f_rc, window=spread_win)
    sleeves = {
        "carry_KC": run_sleeve(d_kc, signals.carry(f_kc, carry_full), KC, cfg),
        "carry_RC": run_sleeve(d_rc, signals.carry(f_rc, carry_full), RC, cfg),
        "trend_KC": run_sleeve(d_kc, signals.trend(d_kc, trend_lb), KC, cfg),
        "trend_RC": run_sleeve(d_rc, signals.trend(d_rc, trend_lb), RC, cfg),
    }
    sleeves["spread_KC_RC"] = pair_sleeve(d_kc, d_rc, sp, cfg)
    bench = {"long_KC": run_sleeve(d_kc, signals.constant(d_kc), KC, cfg),
             "long_RC": run_sleeve(d_rc, signals.constant(d_rc), RC, cfg)}
    # exploratory, added after first results; reported separately, not in the portfolio
    explore = {"xs_carry_KC_RC": pair_sleeve(d_kc, d_rc, signals.relative_carry(f_kc, f_rc, carry_full), cfg)}
    return sleeves, bench, sp, explore


def pair_sleeve(d_kc, d_rc, sig, cfg):
    """Long KC / short RC when sig > 0, each leg at half the sleeve's vol target.
    w_held / pct_long refer to the KC leg (long = long arabica, short robusta)."""
    leg_kc = run_sleeve(d_kc, sig, KC, cfg, scale=0.5)
    leg_rc = run_sleeve(d_rc, sig.assign(signal=-sig["signal"]), RC, cfg, scale=0.5)
    pair = pd.concat([leg_kc, leg_rc], axis=1, keys=["kc", "rc"]).fillna(0.0)
    return pd.DataFrame({
        "net": pair["kc"]["net"] + pair["rc"]["net"], "cost": pair["kc"]["cost"] + pair["rc"]["cost"],
        "traded": pair["kc"]["traded"] + pair["rc"]["traded"], "w_held": pair["kc"]["w_held"],
        "weight_KC": pair["kc"]["weight"], "weight_RC": pair["rc"]["weight"],
        "signal_used": pair["kc"]["signal_used"]})


def fundamental_sleeves(mk, cfg):
    """One sleeve per registered hypothesis whose data has been downloaded."""
    from kcquant import fundamentals as fu
    data = fu.load_all(ROOT / "data" / "fundamentals")
    (d_kc, _), (d_rc, _) = mk["KC"], mk["RC"]
    out = {}
    if "cot_kc" in data:
        out["H1_cot_KC"] = run_sleeve(d_kc, signals.cot_positioning(data["cot_kc"]), KC, cfg)
    if "cot_rc" in data:
        out["H2_cot_RC"] = run_sleeve(d_rc, signals.cot_positioning(data["cot_rc"]), RC, cfg)
    if "ptax" in data:
        out["H3_brl_KC"] = run_sleeve(d_kc, signals.brl_move(data["ptax"]), KC, cfg)
    if "weather" in data:
        wx = data["weather"]
        if (wx["region"] == "BR_arabica").any():
            br = fu.regional_weather(wx, "BR_arabica")
            out["H4_weather_KC"] = run_sleeve(d_kc, signals.weather_brazil(br, fu.rain_anomaly(br, 60)), KC, cfg)
        if (wx["region"] == "VN_robusta").any():
            vn = fu.regional_weather(wx, "VN_robusta")
            out["H5_weather_RC"] = run_sleeve(d_rc, signals.weather_vietnam(vn, fu.rain_anomaly(vn, 90)), RC, cfg)
    if "cert_kc" in data:
        cert = data["cert_kc"]
        out["H6_cert_KC"] = run_sleeve(d_kc, signals.cert_stock_change(cert, KC.tcal), KC, cfg)
        out["H8_pending_KC"] = run_sleeve(d_kc, signals.pending_grading(cert), KC, cfg)
        # robustness only (no verdict): H6 on non-transition bags
        nt = cert.assign(non_transition=cert["certified"] - cert["transition"].fillna(0))
        out["H6_nontransition_KC (robustness)"] = run_sleeve(
            d_kc, signals.cert_stock_change(nt, KC.tcal, column="non_transition"), KC, cfg)
    # a hypothesis whose data never produced a usable signal is skipped, not reported as a result
    for k in [k for k, v in out.items() if v["signal_used"].notna().sum() == 0]:
        print(f"skipping {k}: no usable signal (data missing?)")
        del out[k]
    return out


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--start", default="2019-01-01")
    ap.add_argument("--end", default=pd.Timestamp.today().strftime("%Y-%m-%d"))
    ap.add_argument("--target-vol", type=float, default=0.10)
    ap.add_argument("--ticks", type=float, default=1.0, help="slippage ticks per side")
    a = ap.parse_args()
    logging.basicConfig(level=logging.WARNING)
    out = ROOT / "output" / "backtest"
    out.mkdir(parents=True, exist_ok=True)
    pd.set_option("display.width", 200, "display.max_columns", 30)

    mk = {s.root: load_market(s, a.start, a.end) for s in (KC, RC)}
    cfg = BacktestConfig(target_vol=a.target_vol, ticks_per_side=a.ticks)
    sleeves, bench, sp, explore = build_sleeves(mk, cfg)
    port = combine(sleeves)
    port_df = pd.DataFrame({"net": port})

    # ---- headline metrics ----
    rows = {k: metrics(v["net"], v) for k, v in sleeves.items()}
    rows["PORTFOLIO (equal risk)"] = metrics(port)
    rows.update({k: metrics(v["net"], v) for k, v in bench.items()})
    rows.update({f"{k} (exploratory)": metrics(v["net"], v) for k, v in explore.items()})
    m = pd.DataFrame(rows).T
    m.to_csv(out / "metrics.csv")
    show = m[["start", "years", "ann_return", "ann_vol", "sharpe", "sharpe_se", "max_drawdown",
              "hit_rate", "turnover_per_yr", "cost_per_yr", "pct_long", "pct_short"]].copy()
    for c in ["ann_return", "ann_vol", "max_drawdown", "hit_rate", "cost_per_yr", "pct_long", "pct_short"]:
        show[c] = show[c].map(lambda x: f"{x:.1%}" if pd.notna(x) else "")
    for c in ["sharpe", "sharpe_se", "turnover_per_yr"]:
        show[c] = show[c].map(lambda x: f"{x:.2f}" if pd.notna(x) else "")
    print("== Net of costs (1 tick/side + $2.50/contract), 10% vol target per sleeve ==")
    print(show.to_string())

    # ---- correlations with buy-and-hold ----
    nets = pd.concat({**{k: v["net"] for k, v in sleeves.items()}, "PORTFOLIO": port,
                      **{k: v["net"] for k, v in bench.items()},
                      **{k: v["net"] for k, v in explore.items()}}, axis=1).fillna(0.0)
    print("\n== Daily correlation ==")
    print(nets.corr().round(2).to_string())

    # ---- sub-periods (regimes) ----
    periods = {"2019-21 (contango)": ("2019", "2021"), "2022-23": ("2022", "2023"),
               "2024-26 (backwardation)": ("2024", "2026")}
    sub = pd.DataFrame({p: {k: metrics(nets[k].loc[s:e]).get("sharpe", np.nan) for k in nets}
                        for p, (s, e) in periods.items()})
    sub.to_csv(out / "subperiods.csv")
    print("\n== Sharpe by sub-period ==")
    print(sub.round(2).to_string())

    ann = nets.apply(annual_returns)
    ann.to_csv(out / "annual_returns.csv")
    print("\n== Annual net returns ==")
    print((ann * 100).round(1).to_string())

    # ---- robustness: parameters around the defaults, and cost stress ----
    grid = []
    for cf in [0.05, 0.10, 0.20]:
        s, _, _, _ = build_sleeves(mk, cfg, carry_full=cf)
        grid += [("carry full_at", cf, k, metrics(s[k]["net"])["sharpe"]) for k in ["carry_KC", "carry_RC"]]
    for lb in [(21, 63, 126), (63, 126, 252), (126, 252)]:
        s, _, _, _ = build_sleeves(mk, cfg, trend_lb=lb)
        grid += [("trend lookbacks", str(lb), k, metrics(s[k]["net"])["sharpe"]) for k in ["trend_KC", "trend_RC"]]
    for w in [126, 252, 504]:
        s, _, _, _ = build_sleeves(mk, cfg, spread_win=w)
        grid += [("spread window", w, "spread_KC_RC", metrics(s["spread_KC_RC"]["net"])["sharpe"])]
    for t in [1, 2, 4]:
        s, _, _, _ = build_sleeves(mk, BacktestConfig(target_vol=a.target_vol, ticks_per_side=t))
        grid += [("cost ticks/side", t, "PORTFOLIO", metrics(combine(s))["sharpe"])]
    rob = pd.DataFrame(grid, columns=["parameter", "value", "sleeve", "sharpe"])
    rob.to_csv(out / "robustness.csv", index=False)
    print("\n== Robustness (Sharpe; defaults: carry 0.10, trend (63,126,252), spread 252, 1 tick) ==")
    print(rob.pivot_table(index=["parameter", "sleeve"], columns="value", values="sharpe",
                          aggfunc="first").round(2).to_string())

    # ---- latest signals (what the team would trade at the next decision) ----
    latest = []
    for k, v in sleeves.items():
        last = v.dropna(subset=["signal_used"]).iloc[-1]
        row = {"sleeve": k, "as_of_decision": v.index[-1].date(), "signal": round(last["signal_used"], 3)}
        if k.startswith("spread"):
            row.update(weight_KC=round(last["weight_KC"], 3), weight_RC=round(last["weight_RC"], 3),
                       zscore=round(sp["zscore"].iloc[-1], 2))
        else:
            row["weight"] = round(v["weight"].iloc[-1], 3)
        latest.append(row)
    lat = pd.DataFrame(latest)
    lat.to_csv(out / "latest_signals.csv", index=False)
    print("\n== Latest signals (weights = notional / capital at 10% vol target) ==")
    print(lat.to_string(index=False))

    # ---- fundamental hypotheses (docs/fundamental_hypotheses.md) ----
    fund = fundamental_sleeves(mk, cfg)
    if fund:
        fnets = pd.concat({k: v["net"] for k, v in fund.items()}, axis=1).fillna(0.0)
        fm = pd.DataFrame({k: metrics(v["net"], v) for k, v in fund.items()}).T
        fsub = pd.DataFrame({p: {k: metrics(fnets[k].loc[s:e]).get("sharpe", np.nan) for k in fnets}
                             for p, (s, e) in periods.items()})
        # Pass bar (amended before real data, see docs): return BEYOND buy-and-hold of the same
        # market. alpha = sleeve - beta * long-only, beta by OLS; IR = annualised alpha / tracking vol.
        bench_of = {k: ("long_KC" if k.endswith("KC") else "long_RC") for k in fund}

        def alpha_ir(y, x):
            df = pd.concat([y, x], axis=1).dropna()
            df = df[(df.iloc[:, 0] != 0)]
            if len(df) < 60:
                return np.nan, np.nan
            beta = np.cov(df.iloc[:, 0], df.iloc[:, 1])[0, 1] / df.iloc[:, 1].var()
            resid = df.iloc[:, 0] - beta * df.iloc[:, 1]
            return resid.mean() / resid.std() * np.sqrt(252), beta

        def thirds(k):
            """H6/H8 have a short sample: split their own signal period into 3 equal parts."""
            live = fund[k]["signal_used"].dropna().index
            if len(live) < 30:
                return periods
            cuts = [live[0], live[len(live) // 3], live[2 * len(live) // 3], live[-1]]
            return {f"third {i + 1}": (cuts[i], cuts[i + 1]) for i in range(3)}

        verdict, alpha_sub = {}, {}
        for k in fund:
            ir, beta = alpha_ir(fnets[k], nets[bench_of[k]])
            yrs = fm.loc[k, "years"]
            se = np.sqrt((1 + 0.5 * ir ** 2) / yrs) if np.isfinite(ir) else np.nan
            per = thirds(k) if k.startswith(("H6", "H8")) else periods
            sub_ir = {p: alpha_ir(fnets[k].loc[a:b], nets[bench_of[k]].loc[a:b])[0] for p, (a, b) in per.items()}
            alpha_sub[k] = sub_ir if per is periods else {}
            if per is not periods:
                fm.loc[k, "thirds_alpha_IR"] = ", ".join(f"{v:+.2f}" for v in sub_ir.values())
            pos = sum(1 for v in sub_ir.values() if np.isfinite(v) and v > 0)
            fm.loc[k, "beta_to_long"], fm.loc[k, "alpha_IR"], fm.loc[k, "alpha_IR_se"] = beta, ir, se
            fm.loc[k, "alpha_periods_positive"] = pos
            verdict[k] = ("(robustness - no verdict)" if "robustness" in k else
                          "PASS" if (ir > 2 * se and pos >= 2) else
                          "weak (1-2 s.e.)" if (ir > se and pos >= 2) else "no evidence")
        fm["verdict"] = pd.Series(verdict)
        fsub = pd.concat({"sharpe": fsub, "alpha_IR": pd.DataFrame(alpha_sub).T}, axis=1)
        fm.to_csv(out / "fundamental_metrics.csv")
        fsub.to_csv(out / "fundamental_subperiods.csv")
        cols = ["start", "years", "ann_return", "sharpe", "beta_to_long", "alpha_IR", "alpha_IR_se",
                "max_drawdown", "turnover_per_yr", "pct_long", "pct_short", "alpha_periods_positive", "verdict"]
        if "thirds_alpha_IR" in fm:
            cols.insert(-1, "thirds_alpha_IR")
        print("\n== Fundamental hypotheses (PASS: alpha vs buy-and-hold > 2 s.e. AND positive in >= 2 of 3 periods) ==")
        print(fm[cols].to_string(float_format=lambda x: f"{x:.2f}"))
        print("\nSharpe and alpha IR by sub-period:")
        print(fsub.round(2).to_string())
        print("\nCorrelation of fundamental sleeves with the technical portfolio and buy-and-hold:")
        allnets = pd.concat([fnets, nets[["PORTFOLIO", "long_KC", "long_RC"]]], axis=1).fillna(0.0)
        print(allnets.corr().loc[list(fund), ["PORTFOLIO", "long_KC", "long_RC"]].round(2).to_string())
        combo = combine({**sleeves, **{k: v for k, v in fund.items() if "robustness" not in k}})
        print(f"\nPortfolio, technical only: Sharpe {metrics(port)['sharpe']:.2f}   "
              f"technical + fundamental (equal risk): Sharpe {metrics(combo)['sharpe']:.2f} "
              f"± {metrics(combo)['sharpe_se']:.2f}")
        for k, v in fund.items():
            v.to_csv(out / f"daily_{k}.csv")
        flat = pd.DataFrame([{"sleeve": k, "as_of_decision": v.index[-1].date(),
                              "signal": round(v["signal_used"].dropna().iloc[-1], 3),
                              "weight": round(v["weight"].iloc[-1], 3)} for k, v in fund.items()])
        print("\nLatest fundamental signals:")
        print(flat.to_string(index=False))
        flat.to_csv(out / "latest_fundamental_signals.csv", index=False)

    for k, v in {**sleeves, **bench}.items():
        v.to_csv(out / f"daily_{k}.csv")
    port_df.to_csv(out / "daily_PORTFOLIO.csv")

    from kcquant.plotting import plot_backtest
    plot_backtest(nets, str(out / "backtest_overview.png"),
                  subtitle=f"net of costs · {a.ticks:g} tick/side · {a.target_vol:.0%} vol target per sleeve · "
                           f"{nets.index[0]:%b %Y} – {nets.index[-1]:%b %Y}")
    print("\nwrote", out)


if __name__ == "__main__":
    main()
