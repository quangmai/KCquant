# Fundamental signal hypotheses — registered 2026-10-05, BEFORE any backtest

Written before loading any fundamental data, so the test design can't be tuned to results.
Each hypothesis has one pre-set signal definition. Variants are reported only as robustness,
never used to pick a "best" version. Everything is evaluated with `backtest.py` (point-in-time,
1 tick/side + $2.50/contract, 10% vol target), on 2019-01 .. 2026-10, with sub-periods.

| # | Hypothesis | Data (availability) | Signal (fixed) | Market |
|---|---|---|---|---|
| H1 | Speculative crowding reverses: when managed money is extremely long (short) relative to its own history, forward returns are lower (higher). | CFTC disaggregated COT, Coffee C (Tue positions, released Fri 15:30 NY; shutdown/holiday delays applied) | −z / 2, clipped to ±1; z = 3-year z-score of managed-money net position as % of open interest | KC |
| H2 | Same as H1 for robusta. | ICE Futures Europe COT, Robusta (Tue positions, released Fri 18:30 London) | same as H1 | RC |
| H3 | A stronger real raises arabica prices (Brazilian growers sell less when local-currency prices fall). | BCB PTAX USD/BRL (published ~13:10 São Paulo, same day) | −(20-day log change in USD/BRL) / 2%, clipped to ±1 | KC |
| H4 | Weather shocks in Brazilian arabica regions (frost May–Sep, flowering-season drought Sep–Nov) are under-priced at first. | Open-Meteo ERA5 daily min temperature & rainfall for Sul de Minas, Cerrado, Mogiana (usable 7 days after the date) | Long only. +1 for 20 sessions after any day with min temp ≤ 2 °C; otherwise, in Sep–Nov, +(1 − rain_60d / climatology_60d) clipped to [0, 1] | KC |
| H5 | Drought in Vietnam's Central Highlands lifts robusta. | Open-Meteo ERA5 daily rainfall for Đắk Lắk, Gia Lai, Đắk Nông (usable 7 days after the date) | Long only. +(1 − rain_90d / climatology_90d), clipped to [0, 1] | RC |

Climatology uses only years before the current one (expanding window from 2000), so it never
contains future weather. ERA5 values can be revised slightly in the first ~3 months; that small
look-ahead is accepted and noted.

~~Pass bar for further work: Sharpe > 0 in at least 2 of 3 sub-periods AND full-period Sharpe more than
1 standard error above zero, after costs.~~

**Amended 2026-10-05, before any real fundamental data was loaded.** A dry run on randomly generated
fundamental data showed the original bar lets a long-only signal (H4, H5) "pass" just by being long a
rising market. New bar: the sleeve's return **beyond buy-and-hold of the same market** — alpha from
regressing its daily net returns on the long-only sleeve's — must have an information ratio more than
1 standard error above zero, AND that alpha must be positive in at least 2 of the 3 sub-periods
(2019–21, 2022–23, 2024–26), after costs. Anything else is reported as "no evidence".

**Second amendment, 2026-10-05, still before any real fundamental data.** In the same dry run on
random data, H5 cleared the 1-standard-error bar by chance. With five hypotheses tested, a 1 s.e.
bar (about a 16% false-pass chance each) makes a false discovery likely. Verdicts are therefore:
**PASS** = alpha IR above 2 standard errors (about 2% false-pass chance each, roughly 11% across all
five) and positive in at least 2 of 3 sub-periods; **weak** = between 1 and 2 standard errors;
otherwise **no evidence**. Only a PASS justifies paper trading.

---

# Warehouse stock hypotheses — registered 2026-10-05, BEFORE any backtest

**What had been seen when this was written:** 42 daily ICE Coffee "C" certified stock reports
(5 Aug – 2 Oct 2026), looked at only to work out the file layout (certified ≈ 253–261k bags, pending
grading ≈ 12–21k). No stock figure had been compared with prices or returns. To avoid choosing
thresholds from that glimpse, both signals use **ranks against the series' own trailing history**
rather than fixed levels. (H7 is reserved for robusta stocks, if an ICE Europe source is found.)

| # | Hypothesis | Data (availability) | Signal (fixed) | Market |
|---|---|---|---|---|
| H6 | Falling certified stocks mean deliverable supply is tightening, which lifts arabica prices. | ICE Coffee "C" certified stock report, total bags certified (stamped ~13:15–13:25 NY; usable from that time + 15 min, so from the next session's 13:00 decision) | x = 20-report log change in total certified bags; p = percentile rank of x among the previous 250 reports (at least 120 needed); signal = −(2p − 1), so the steepest falls give +1 (long) and the steepest builds give −1 (short) | KC |
| H8 | A large pile of bags waiting for grading is supply about to become deliverable, which weighs on arabica prices. | Same reports: bags pending grading ÷ total certified | r = pending ÷ certified; p = percentile rank of r among the previous 250 reports (at least 120 needed); signal = −(2p − 1): an unusually large pending pile is short, an unusually small one long | KC |

**Data rules.** A change or rank is computed only over reports actually published. If the 20-report
window spans a gap of more than 5 trading days with no report (missing files), the signal is blank for
that day rather than bridging the gap. Reports are never forward-filled beyond the next report.

**Test.** Same backtest, costs and vol targeting as H1–H5, and the same amended bar: **PASS** if the
alpha against buy-and-hold KC has an information ratio above 2 standard errors and is positive in at
least 2 of 3 sub-periods; **weak** at 1–2 standard errors; otherwise **no evidence**. Because these
reports may only reach back to late 2023, the sub-periods are the usable sample split into three
equal parts, not the 2019–21 / 2022–23 / 2024–26 split.

**Known limits, stated in advance.**
* Power: after the 120-report warm-up, about 2 years of signal remain. The standard error of the
  information ratio is then about 0.7, so a PASS needs an information ratio above roughly 1.4.
  A real but modest effect will most likely show "no evidence" or "weak". Either result means
  *keep collecting*, not *proven useless*.
* Structural change: about 78% of certified bags are "transition" bags that take a discount from
  2027 (ICE notice), which may drive stocks for reasons unrelated to supply tightness. As a
  robustness check only (no verdict), H6 is also reported using non-transition bags
  (certified − transition).
* Tests now total seven (H1–H6, H8). The 2-standard-error bar is kept for that reason.
