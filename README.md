# Wind turbine O&M reliability from status and alarm logs

[![tests](https://github.com/kwwaleszczynski-del/wind-om-reliability/actions/workflows/ci.yml/badge.svg)](https://github.com/kwwaleszczynski-del/wind-om-reliability/actions/workflows/ci.yml)

**Interactive dashboard - Pareto by code and component, failure timeline, Crow-AMSAA, jack-knife, repeats, logs
against SCADA: [kwwaleszczynski-del.github.io/wind-om-reliability](https://kwwaleszczynski-del.github.io/wind-om-reliability/)**
- example monthly KPI pack: [HTML](https://kwwaleszczynski-del.github.io/wind-om-reliability/examples/kpi_penmanshiel_2020-11.html),
[Excel](docs/examples/kpi_penmanshiel_2020-11.xlsx).

The monthly O&M report and the meeting with the service provider keep coming back to the same questions:
*which faults cost us the most*, *is this turbine worse than the others*, *is the fleet getting better or worse*,
*why does the same alarm come back three days after every reset*, *how long does it take until someone is on
site*. The answers are in the turbines' own status and alarm logs. This project turns such a log - any
manufacturer, any export layout - into those numbers, and checks itself on three open wind farms:

| Farm | Turbines | Logs used | What the log holds |
|---|---|---|---|
| Kelmarsh (UK) | 6 x Senvion MM92, 2.05 MW | 2016 (from COD) - 2021 | Greenbyte status log: Stop / Warning / Informational, code, text, the operator's IEC 61400-26 category |
| Penmanshiel (UK) | 14 x Senvion MM82, 2.05 MW | 2016 - 2024 | the same (2024 only for turbines 11-15, which is all the open data has) |
| Hill of Towie (UK) | 21 x Siemens SWT-2.3-VS-82, 2.3 MW | 2016 - April 2026 | the controller's alarm log: code, time on, time off - nothing else; 12 of about 400 codes are described |

What it computes, per farm, turbine, component, code and month:

1. **Pareto** of stop hours, stops, forced-outage episodes and lost energy by status code and by component, with
   overlapping
   stops attributed once (to the *first-up* alarm). Code-to-component mappings are plain CSV files, one per
   manufacturer.
2. **Reliability**: MTBF with confidence intervals, MTTR and the distribution of fault times; Weibull of the
   times between failures; **Crow-AMSAA** on repeat chains with turbine-bootstrap intervals (is reliability
   improving or deteriorating?); **bad actors** - turbines and code x turbine pairs above the fleet rate, and the
   jack-knife diagram of codes; how much every count depends on the counting rule.
3. **Compound events**: repeat failures (same code within 7 days of the restart) against the rate expected by
   chance; alarm cascades (primary against secondary alarms); automatic resets against remote and on-site
   interventions; the time until a technician is on site.
4. **Lost energy** of the logged stops and the **check of the log against 10-minute SCADA** (stop hours,
   clock / daylight-saving errors), using the methods of the earlier project
   [wind-downtime-availability](https://github.com/kwwaleszczynski-del/wind-downtime-availability) as a
   dependency, not a copy.
5. An illustrative **spare-parts stock** from the expected failures per component and year.
6. A **monthly KPI pack** in Excel (headers, conditional formatting) and HTML.

## Key results

What this README calls a *failure* is a **forced-outage stop episode**: the stops of one turbine less than 10
minutes apart, in which a forced-outage stop was at some moment the reason the turbine stood still, unless half of
the turbines stopped within 5 minutes (a common cause). It is a counting rule, not a law of nature: the next
section shows how much the counts move under other reasonable rules.

| | Kelmarsh | Penmanshiel | Hill of Towie³ |
|---|---|---|---|
| Turbine-years of logs (gaps removed) | 32.7 | 104.3 | 214.8 |
| Forced-outage stop episodes (not fleet-wide)² | 267 | 2,013 | 1,611 |
| ... of which another turbine failed within 10 minutes (coincident) | 45 (17 %) | 441 (22 %) | 186 (12 %) |
| Repeat chains (a code repeating within 7 days counts once) | 220 | 1,149 | 1,084 |
| Episodes per turbine-year | 8.2 | 19.3 | 7.5 |
| MTBF, operating hours (turbine bootstrap 90 %) | 1,051 (908-1,232) | 443 (373-533) | 1,107 (853-1,413) |
| Time down for the fault: median / mean (MTTR) | 16 min / 4.4 h | 14 min / 5.8 h | 43 min / 13.1 h |
| Episode, first stop to restart: median / mean | 16 min / 8.2 h | 28 min / 6.9 h | 61 min / 23.1 h |
| Codes making 80 % of all stop hours (of codes with downtime) | 7 of 69 | 16 of 99 | 21 of 101 |
| Repeat failures within 7 days (expected by chance) | 18 % (1.6 %) | 43 % (3.5 %) | 33 % (1.6 %) |
| Weibull shape of times between failures (turbine bootstrap 90 %) | 0.40 (0.33-0.52) | 0.37 (0.35-0.41) | 0.44 (0.41-0.51) |
| Crow-AMSAA beta on repeat chains (turbine bootstrap 90 %)¹ | 0.81 (0.76-0.91) | 0.95 (0.89-1.01) | 0.64 (0.42-0.87) |
| Crow-AMSAA beta on every episode (interval assumes independent events) | 0.82 (0.74-0.91) | 1.12 (1.08-1.16) | 0.44 (0.34-0.55) |
| Trend (bootstrap interval of the repeat-chain beta below / above 1) | **improving** | **no clear trend** | **not assessed** (code dictionary from 2021 only) |
| Cleared by automatic reset / technician on site | 52 % / 19 % | 40 % / 16 % | 34 % / no on-site codes |
| Time to technician on site: median / 90th percentile (failures it rests on) | 15.7 h / 72 h (23) | 12.9 h / 45 h (159) | not in the log |
| Bad-actor turbines (repeat chains, Poisson test, FDR 5 %) | none | T02 | T06 |
| Logged vs SCADA-detected stops, F1 of 10-min intervals (weighted by stop hours) | 0.82 (2016-2021) | 0.86 (2018, 2020) | 0.88 (2021) |
| Clock check: best shift of the log against SCADA | 0 h | 0 h | 0 h |

¹ Crow-AMSAA fits only failures that start inside the observation windows; a few start during a data-link
outage of 6 hours or more, which is cut out of the observation time while the controller kept its own log
(`results/<farm>/crow_amsaa.csv` has the counts). The bootstrap resamples whole turbines, so it keeps the
dependence between a turbine's failures that the profile-likelihood and chi-square intervals ignore.

² Episodes per turbine-year, MTBF and the trend depend on the counting rule; see the next section.

³ **A different definition.** At Kelmarsh and Penmanshiel the operator's platform gives every event an IEC
category and a stop flag. Hill of Towie's alarm log has neither: for 134 undocumented codes both were inferred
from 2021 SCADA (see finding 8), so its column counts something similar, not the same thing, and the code
dictionary does not reach codes that appear only in other years.

### What the counts depend on

| Counting rule (`results/<farm>/sensitivity.csv`) | Kelmarsh | Penmanshiel | Hill of Towie |
|---|---|---|---|
| published: stops < 10 min apart are one episode; fleet-wide = half the turbines within 5 min | 267 (8.2 per turbine-year, MTBF 1,051 h) | 2,013 (19.3, 443 h) | 1,611 (7.5, 1,107 h) |
| stops < 1 h apart are one episode | 229 (-14 %) | 1,534 (-24 %) | 1,443 (-10 %) |
| stops < 24 h apart are one episode | 224 (-16 %) | 1,342 (-33 %) | 1,294 (-20 %) |
| coincident failures removed (another turbine within 10 minutes) | 222 (-17 %) | 1,572 (-22 %) | 1,425 (-12 %) |
| 1 h gap and coincident removed | 192 (5.9, 1,461 h) | 1,123 (10.8, 793 h) | 1,273 (5.9, 1,400 h) |
| repeat chains only | 220 (-18 %) | 1,149 (-43 %) | 1,084 (-33 %) |
| without tower-oscillation, remote manual and external stops | 116 (3.6, 2,419 h) | 1,262 (12.1, 707 h) | no such codes |

The fleet-wide rule needs half of the turbines within 5 minutes, so a common cause that stops two or three
turbines stays as two or three turbine failures: at Penmanshiel the coincident episodes are mostly tower
oscillation in storms, remote manual stops (91) and "externally stopped" (50) - operator actions and weather
rather than faults. Depending on the rule, Kelmarsh has 3.6 to 8.2 failures per turbine-year and Penmanshiel 10.8
to 19.3. Compare such numbers between farms or with a contract only under the same rule; the tool flags the
coincident episodes (`coincident`) and takes the merge gap as an option.

**1. A handful of codes carries the downtime, and they are rarely the frequent ones.** At Kelmarsh 7 of 69
codes make 80 % of all stop hours: on-site manual stops (28 %), a single 82-day "emergency stop top box" (14 %),
overvoltage, a 68-day "stop for manual reboot", park master stops and icing. The *failures* are another list:
tower and nacelle stops (mostly tower oscillation) make 128 of the 267 failures but only 76 hours of fault time,
while 12 converter failures cost 390 hours (MTTR 33 h). Drivetrain failures look long by episode (46 h on
average) but the fault itself lasts 7.7 h: the rest is maintenance done in the same stop, which is why MTTR here
counts only the time the forced-outage stops were the reason the turbine stood still (Kelmarsh: 1,185 of 2,189
episode hours; 990 are scheduled-maintenance stops inside the same episodes). Even so, the five longest faults
make 42 % of Kelmarsh's fault hours: a mean over 267 episodes this skewed says little, the median (16 minutes)
and the jack-knife diagram say more. At Penmanshiel four codes are acute *and* chronic: remote manual stops
logged as forced outages, the pitch charging circuit, generator brush service and the frequency converter error.

**2. Failures come in bursts: resets without a fix.** The Weibull shape of the times between failures is
0.37-0.40 at both Senvion farms - far from the 1.0 of random failures - and 43 % of Penmanshiel's failures repeat
the same code within 7 days of the restart, against 3.5 % if each code simply recurred at its own rate. The worst:
*anemometer defect* - 401 failures, 88 % repeats (7 % by chance), in only 50 repeat chains: T12's 153 anemometer
stops are 5 chains, one of them 147 resets long - one sensor left unrepaired, not a weak turbine; *missing gear
oil (high rpm)* 48 % repeats, a quarter of its 240 failures on T11. Counted as chains, as the bad-actor test does,
T12, T11 and T08 are not out of line; T02 is (122 chains against 79 expected). At Kelmarsh "high rotor speed
nacelle" occurs only on T1 (7 chains of 7, 46 % repeats) and "tower oscillation X level 1" mostly on T6 (17 of 32
chains). These are the code x turbine pairs the test flags (2 at Kelmarsh, 9 at Penmanshiel): one turbine's sensor
or hydraulics problem, not a fleet issue - and the agenda for the next meeting with the service provider.

**3. Kelmarsh got better; at Penmanshiel three turbines' repeating alarms did not make the fleet worse.**
Kelmarsh: beta 0.81 on repeat chains, 0.76-0.91 when whole turbines are resampled, the same on every episode
(0.82); failures per turbine-year fell from 9.2 (2016-2018) to 5.3 (2021). Penmanshiel looks like it deteriorated
- beta 1.12 on every episode, 11 failures per turbine-year in 2017-2019 against 26-30 in 2021-2022 - but that is
the T08/T11/T12 anemometer and gear-oil repeats. On repeat chains beta is 0.95 (p = 0.09); with one failure per
turbine-day 1.02; without T08, T11 and T12 0.94; without the anemometer, remote-stop and external-stop codes 0.98.
Resampling turbines gives 0.89-1.01 on chains and 0.98-1.24 on every episode, against the 1.08-1.16 an
independence assumption would claim: turbine-year counts have a variance 20 times their mean. Only 4 of the 14
turbines deteriorate on their own. Its commissioning year 2016 shows the opposite pattern (30 per turbine-year
in four months).

**4. Half of the failures clear themselves; the few that need a visit wait half a day.** The Senvion controller
restarts by itself after a fixed delay: the restore-time histogram has a sharp peak at 15-16 minutes, so 20
minutes separates automatic resets from the rest. 52 % (Kelmarsh) and 40 % (Penmanshiel) of the failures end
that way. A response time can be measured for 23 failures at Kelmarsh and 159 at Penmanshiel (9 % and 8 % of all
failures): the first on-site event (manual stop on site, manual yaw, manual operation) follows the failure after
a median of 15.7 h and 12.9 h, with 10 % taking more than 72 h and 45 h. Another 29 and 155 failures happened
while someone was already on site (the on-site event came within 15 minutes) and are left out - they would make
the response look like minutes. With 23 cases, Kelmarsh's figures are indicative only.
**5. Cascades say which alarm to look at.** "Charging circuit pitch" brings two or three "battery charge cycle
axis" errors along in 89 % of its episodes (lift 260): only the first is the fault. "Icing (stop)" brings
"icing (deviation of electrical power)" in 93 %, an anemometer defect the 4-20 mA anemometer / vane alarms.

**6. The logs agree with SCADA, and the clock is right.** Interval-level F1 of logged stops against stops found
in the 10-minute signals by the parked-rotor signature: 0.70-0.93 at Kelmarsh in 2017-2021 (0.53 in its commissioning
year, when logged stops do not show in the signals) and 0.85-0.86 at Penmanshiel; monthly stop hours per turbine
correlate at 0.99 and 0.97. Shifting the log against SCADA in 10-minute steps peaks exactly at zero - a log read
in the wrong time zone, or with daylight saving mishandled, peaks at -1 or -2 hours (see the test below).

**7. Lost energy agrees with the operator's platform where SCADA exists.** Over all logged stops, the
running-neighbour reference prices 3.43 GWh at Kelmarsh (2016-2021) and 4.38 GWh at Penmanshiel (2018, 2020).
Greenbyte's own lost production to downtime is 5.61 and 9.04 GWh, but 2.33 and 5.07 GWh of it lies in stops
without any SCADA row - at Penmanshiel 4.6 GWh in 2020 alone, most of it the 15-day park master stop of
September-October.
Where SCADA exists the two differ by +5 % (Kelmarsh) and +10 % (Penmanshiel; this tool also prices the
requested and environmental stops). The tool reports the stop hours it
cannot price instead of guessing them. Kelmarsh's lost energy has other scopes in two related projects:
[wind-power-curve-kelmarsh](https://github.com/kwwaleszczynski-del/wind-power-curve-kelmarsh) prices the
non-normal *SCADA states* from commissioning (stops, grid and park stops, curtailment, icing) at 3.1 GWh, and
[wind-downtime-availability](https://github.com/kwwaleszczynski-del/wind-downtime-availability) only the logged
stops of its test years 2020-2021 at 1.85-1.96 GWh; none of the three prices the stops without SCADA rows.

**8. Hill of Towie: a log without a dictionary.** Only 12 alarm codes are described (low and high wind, icing,
cable untwisting, pitch lubrication). From one year of SCADA (2021) the tool infers which of the other codes
stop the turbine (the rotor is parked in at least 70 % of the code's active intervals: 102 of 134 codes with
enough data) and which are faults (the documented "turbine error active" timer runs during at least half of that
time: 49). On that basis: 1,611 failures in 215 turbine-years. **T06 is the bad actor** - 313 failures, 91 repeat
chains against 48 expected; 249 of the failures are one code (3220), 20 times its share of the fleet, repeating
within 7 days in 77 % of the cases (2.6 % by chance): one turbine's chronic fault that a fleet-level Pareto would
show as a fleet problem. **No trend is claimed**, because a dictionary built from 2021 cannot see codes that
are rare in 2021: the share of active alarm hours from codes outside the dictionary is 0.2 % in 2021, 4-14 % in 2016-2020 and 17-32 % in 2022-2025 (45,300 hours in 2022 alone; `results/hill_of_towie/yearly.csv`). Codes that first appear in 2022 (73033 and 73030, 61,000 active hours together;
3235, 31 events with a median of 253 hours in 2024-2026) can never count as failures, so the low rates after 2021
(3.8-4.8 per turbine-year in 2022-2024) and the beta below 1 are partly an artefact; the study refuses to call it
a trend (`trend: not assessed`). The same caveat covers the longer fault times from 2021 (MTTR 2-8 h until 2020,
19-43 h after). The 2018 peak (14 per turbine-year) is mostly code 13325 (120 failures), not T06's 3220, which
peaks in 2019 (153). Calibrating the dictionary on every year would need the SCADA of every year. The logged stops
match SCADA (F1 0.88) and contain 95 % of the shutdown time the operator computed from SCADA and its own logs;
the log has 34 % more stop hours, mostly undocumented 1000-series stop codes without the error timer (by analogy
with the published 1005 "availability - low wind", availability states), which stay uncategorised and out of
the failure counts. Without code texts the Pareto by component is 92 % "unmapped" - the honest result for a log
without a dictionary.

## Validation

**Synthetic logs with known parameters** (`wind_om.synthetic`, tests in `tests/`): failures drawn on each
turbine's operating clock, restore times lognormal, codes from a known mix; then the full pipeline.

| Synthetic fleet | Parameter | True | Estimated |
|---|---|---|---|
| constant rate, 30 turbines x 5 years | MTBF (h) | 600 | 614 (90 % interval contains 600) |
| | MTTR (h) | 9.9 | 10.3 |
| | restore time median (h) / log-sd | 6 / 1.0 | 6.1 / 1.02 |
| | Weibull shape / Crow-AMSAA beta | 1 / 1 | 1.01 / 0.98 (no trend found) |
| power law, observation starting at age 2 years | Crow-AMSAA beta | 0.6 / 1.4 | 0.63 / 1.37 (improving / deteriorating) |
| power law with 12 logging gaps of 30 days | Crow-AMSAA beta | 1.4 | 1.44 |
| Weibull renewal process | shape | 0.7 / 2.0 | 0.72 / 1.97 |
| Weibull renewal process, heavy random censoring | shape / scale | 1.6 / 300 h | within 5 % |
| one turbine with 2.5 x the failure rate | bad actors found | that turbine | that turbine only |
| 30 % of failures repeat within 2 days | repeats found | all | > 97 %; rate without repeats = chance rate |
| constant rate, half of the failures repeating within 2 days | Crow-AMSAA beta on chains (turbine bootstrap) | 1 | 1.01 (0.97-1.05), no clear trend |
| the same, on every event | interval: profile likelihood / turbine bootstrap | - | bootstrap > 1.1 x wider |
| one turbine whose extra failures are all repeats | bad actors on failures / on chains | - / none | that turbine / none |
| technician on site 6 h after each failure | median response | 6 h | 6.0 h |
| every code 1001 drags 7101 along | cascade confidence | 100 % | 100 % |

**Hard cases**, each a test: overlapping stops (union hours, first-up attribution, a fully nested stop gets
zero hours, a failure followed by a technician's manual stop stays a failure); open events (closed at the next
stop of the turbine); a stop crossing midnight at the end of March in Polish time (split 1 h / 2 h between the
months, counted as a failure in March; March has 743 hours, October 745); logging gaps (exposure without them,
no time-between-failures interval across a gap); weeks of silence in a log that normally talks every day (taken
as missing files); grid losses on all turbines at once (one fleet-wide event, not 10 failures); a 10-minute
SCADA farm with painted stops (lost energy within 8 %, clock check finds a 2-hour shift).

**Daylight saving on real logs.** Each farm's full log written as naive UK local time and read back
(`python -m wind_om dst-check`): Kelmarsh 311,989 events, 10 starts in the repeated autumn hour, 6 placed one
hour wrong; Penmanshiel 1,056,488 events, 51 in the repeated hour, 22 wrong. These are lone events: a turbine
that logs only one event between 01:00 and 02:00 on the night the clock goes back gives no way to tell which
pass it belongs to. Every other event, including all spring-gap times, comes back to the second. The same logic
handles Europe/Warsaw (synthetic round trip through both 2026 clock changes is exact). If you can, export in
UTC or with the offset in the timestamp.

## Use it on your own logs

The input is one table, one row per event:

| column | meaning | required |
|---|---|---|
| `turbine_id` | any text | yes |
| `start` | event start | yes |
| `end` | event end (or a duration column) | no - open stops run to the next stop |
| `code` | status / alarm code | yes |
| `description` | the controller's text | no |
| `category` | the log's own reason category | no - or from the code mapping |

**A YAML file** says which column of *your* export is which, the time zone of the timestamps and how the raw
values translate (examples for several manufacturers in [`src/wind_om/configs`](src/wind_om/configs) - their
column names are placeholders to rename, not OEM specifications):

```yaml
format: csv
read: {sep: ";", encoding: utf-8-sig}
columns: {turbine_id: Unit, start: Detected, end: Reset, code: Alarm code, description: Alarm text, category: Alarm type}
timezone: Europe/Warsaw            # naive local times: both clock changes handled
ambiguous: infer                   # the repeated hour in October, from the file order
datetime_format: "%d.%m.%Y %H:%M:%S"
stop: {column: Alarm type, values: [Stop, Fault]}
category_map: {Fault: forced_outage, Service: scheduled_maintenance, Weather: environmental, Grid: grid}
```

**A mapping CSV** per manufacturer gives each code a component, an optional role (`onsite`: someone at the
turbine - manual stop on site, manual operation; `remote`: remote stop or reset), and, where the log has none, a
category and a stopping flag. `python -m wind_om template` lists every code of your log, most frequent first, to
fill in. Shipped: [`senvion_mm.csv`](src/wind_om/mappings/senvion_mm.csv) (268 codes, written from the open
logs' code texts) and [`siemens_swt_hot.csv`](src/wind_om/mappings/siemens_swt_hot.csv) (the 12 published
Hill of Towie codes plus codes inferred from SCADA).

```bash
pip install "wind-om-reliability[scada] @ git+https://github.com/kwwaleszczynski-del/wind-om-reliability"
python -m wind_om template alarms.csv --config my_oem.yaml --out my_codes.csv
python -m wind_om report alarms.csv --config my_oem.yaml --mapping my_codes.csv --month 2026-09 \
    --tz Europe/Warsaw --farm "Farm A" --out kpi/
python -m wind_om report alarms.csv --config my_oem.yaml --mapping my_codes.csv --month 2026-09 \
    --scada scada.parquet --rated-kw 3600 --out kpi/        # adds lost MWh (SCADA in the long format of
                                                            # wind-downtime-availability)
python -m wind_om analyse alarms.csv --config my_oem.yaml --mapping my_codes.csv --out tables/
python -m wind_om demo --out demo/                          # a synthetic farm in Polish time, no data needed
```

`report` writes `kpi_YYYY-MM.xlsx` and `kpi_YYYY-MM.html`: summary KPIs, Pareto by code and component for the
month, MTBF / MTTR per turbine and component and bad actors over the last 12 months, the month's repeat failures,
repeat rates against chance, cascades, restoration and response time, a 24-month trend and every failure of the
month. Months are calendar months in `--tz`. Useful options: `--coverage` (periods with valid logging),
`--cod` (commissioning dates: the age origin of Crow-AMSAA), `--max-silence 7D` (logs that carry routine
messages), `--repeat-days`, `--auto-max`, `--merge-gap`. Paths come from arguments or environment variables
only; nothing is sent anywhere.

## Method

**Episodes and attribution.** Stops of one turbine that overlap or follow within 10 minutes form one episode
(one repair, first stop to final restart). Each moment of downtime belongs to the earliest-starting stop still
active (the first-up; ties: the one listed first). An episode is a **failure** (a forced-outage stop episode) when
a stop of a failure category (forced outage) was at some moment the reason the turbine stood still, and it did not
start within 5 minutes on at least half of the turbines (then it is fleet-wide: grid loss, park controller, storm).
The failure's code is the failure-category stop with most attributed hours. Failures that start within 10 minutes
of a failure on another turbine are flagged `coincident` (a partial common cause); `study.sensitivity` recounts
everything with merge gaps of 1 h and 24 h, without coincident failures, on repeat chains and without chosen codes.

**MTBF and MTTR.** With $`n`$ failures in $`T`$ operating hours (observation minus all stop time):

```math
\widehat{\mathrm{MTBF}} = \frac{T}{n}, \qquad
\mathrm{MTBF} \in \left[\frac{2T}{\chi^2_{1-\alpha/2,\;2n+2}},\; \frac{2T}{\chi^2_{\alpha/2,\;2n}}\right]
```

MTTR is the mean time down for the fault: the attributed hours of the episode's forced-outage stops (waiting,
travel and logistics included). Maintenance done in the same stop after the fault and uncategorised stops are in
the episode duration, which is reported next to it. Fault times are summarised by median, 90th percentile and a
lognormal fit. The chi-square interval assumes independent failures; the published MTBF interval resamples whole
turbines instead.

**Weibull** of the operating times $`t_i`$ between failures of each turbine, the last interval of every
observation window right-censored ($`\delta_i = 0`$), the first dropped:

```math
\ell(k,\eta) = \sum_i \delta_i \left[\ln k - k\ln\eta + (k-1)\ln t_i\right] - \sum_i \left(\frac{t_i}{\eta}\right)^k
```

**Crow-AMSAA** (power-law non-homogeneous Poisson process) with intensity $`u(t) = \lambda\beta t^{\beta-1}`$ in
turbine age $`t`$ since commissioning, every turbine $`q`$ observed in windows $`(S_{qj}, T_{qj}]`$ (logging gaps
and late starts handled exactly):

```math
\ell(\lambda,\beta) = n\ln\lambda + n\ln\beta + (\beta-1)\sum_{i}\ln t_i - \lambda\sum_{q,j}\left(T_{qj}^{\beta}-S_{qj}^{\beta}\right)
```

$`\beta`$ by maximum likelihood on the first failure of every repeat chain (a code repeating on the same
turbine within 7 days of the restart is one event, not several), its 90 % interval from resampling whole turbines
500 times. The trend is *improving* or *deteriorating* only when that interval lies below or above 1. The fit on
every episode and its profile-likelihood interval are reported too; they assume independent events, which repeat
failures are not. Second opinion: the Laplace test on each turbine's observed-time clock (windows joined end to
end), which does not depend on the age origin:

```math
U = \frac{\sum_i \left(\tau_i - L_{q(i)}/2\right)}{\sqrt{\sum_i L_{q(i)}^2/12}} \sim N(0,1)
```

Operating time, MTBF and MTTR with their interval, Crow-AMSAA, the Laplace test and the interval operations
behind the first-up attribution are computed by
[res-asset-kpi](https://github.com/kwwaleszczynski-del/res-asset-kpi) (`res_asset_kpi.wind`,
`res_asset_kpi.intervals`), the KPI library extracted from this and five related projects.

**Bad actors.** Turbine $`j`$ with $`n_j`$ repeat chains in $`T_j`$ operating hours against the fleet rate
$`r = \sum n / \sum T`$: one-sided p-value $`P(N \ge n_j)`$ for $`N \sim \mathrm{Poisson}(r T_j)`$, adjusted by
Benjamini-Hochberg across turbines; the same per code and turbine. Counting every repeat instead would make the
Poisson test far too confident (turbine-year counts of failures have a variance 2 to 23 times their mean).
Jack-knife diagram (Knights, 2001): failures against mean fault time per code, limits at the mean failures per code
and the overall MTTR.

**Repeats against chance.** A code $`c`$ that fails $`n_c`$ times in $`T`$ turbine operating hours would recur
within $`d`$ days by chance with probability

```math
p_c = 1 - \exp\!\left(-\frac{n_c}{T}\,24d\right)
```

**Lost energy** of a 10-minute interval $`k`$ covered for a fraction $`c_k`$ by logged stops, with expected power
$`\hat P_k`$ from the running neighbours' wind and the turbine's own power curve (wind-downtime-availability):

```math
E_k = \min\left(\max(\hat P_k - P_k, 0),\; c_k \hat P_k\right)\Delta t
```

shared among the stops of the interval by overlap. **Spare parts** (illustrative): with $`\mu`$ the expected
failures of a component in the fleet during one replenishment lead time, the stock is the smallest $`s`$ with
$`P(\mathrm{Poisson}(\mu) \le s) \ge`$ the service level; it assumes a constant rate and one part per failure.

## Data and licences

- *Kelmarsh wind farm data*, C. Plumley and R. Takeuchi, Cubico Sustainable Investments,
  [doi:10.5281/zenodo.16807551](https://doi.org/10.5281/zenodo.16807551), and *Penmanshiel wind farm data*,
  C. Plumley, Cubico Sustainable Investments, [doi:10.5281/zenodo.16807304](https://doi.org/10.5281/zenodo.16807304);
  both CC BY 4.0. Kelmarsh logs are read from the data folder of
  [wind-power-curve-kelmarsh](https://github.com/kwwaleszczynski-del/wind-power-curve-kelmarsh); for Penmanshiel
  only the `Status_*` files are downloaded from the yearly archives (about 8 MB instead of 7 GB, byte ranges).
- *Hill of Towie wind farm open dataset* v2.1.0, RES,
  [doi:10.5281/zenodo.22662930](https://doi.org/10.5281/zenodo.22662930), CC BY 4.0 (licence checked in the
  record's metadata): the monthly
  `tblAlarmLog` files of every year (about 32 MB instead of 15 GB), the turbine metadata and the alarm
  description table. The 10-minute SCADA used for the checks comes from the caches of
  wind-downtime-availability.
- `python -m wind_om fetch` downloads all of it (each file in one exact byte-range request, CRC-checked,
  stops above 60 MB); `python -m wind_om study --scada-dir <wind-downtime-availability>/data` reproduces every
  number here (KELMARSH_DATA_DIR points at the Kelmarsh data).

## Limitations

- **Mappings are working assumptions.** The Senvion mapping was written from the code texts of the open logs;
  the Hill of Towie one is mostly inferred from one year of SCADA (stopping: the turbine is parked in at least
  70 % of the code's active intervals; forced outage: the documented "turbine error active" timer runs during at
  least half of that time). Neither is an OEM document. Codes can mean different things on different firmware
  (code 111 is an emergency stop at Kelmarsh and grid curtailment at Penmanshiel).
- **What counts as a failure follows the log's categories.** Greenbyte files remote manual stops as forced
  outages (203 failures at Penmanshiel, component "service"). Contracts define failures, response clocks and
  exclusions their own way; the definitions here are examples to be checked against the actual contract.
- **Restoration needs role codes.** Automatic reset, remote and on-site are told apart only by codes with a
  role and by the restore time; a remote reset that leaves no trace in the log ends up as "not logged". Hill of
  Towie's alarm log has no on-site codes at all.
- **Response time is time to the first on-site event**, not to arrival: a technician who works without putting
  the turbine into manual mode is invisible. It rests on few failures (23 at Kelmarsh, 159 at Penmanshiel).
- **Trends are fleet averages.** Crow-AMSAA assumes one power law over the whole life; Penmanshiel's
  commissioning burst in 2016 followed by a rise from 2020 is not a power law. The yearly rates show the shape.
  Per turbine the law can fail outright: for 5 of the 21 Hill of Towie turbines, observed only from four years
  after commissioning with their failures bunched in 2018-2019, the likelihood has no maximum and the table says
  "no power-law fit".
- **A code dictionary built from one year** classifies only the codes active in that year. At Hill of Towie
  that hides the codes of other years from the failure counts, so its rates are not comparable across years and
  its trend is not assessed (`results/hill_of_towie/yearly.csv` has the unclassified hours per year).
- **Failures are counted, not given.** The merge gap, the fleet-wide rule and repeat chains change the counts by
  10 to 57 %; MTBF and MTTR intervals that assume independent failures are too narrow on this kind of data, which
  is why the published ones resample whole turbines.
- **Lone events in the repeated autumn hour** of a local-time log cannot be placed (see Validation).
- **Lost energy** exists only where SCADA has rows; stops without them are reported as unpriced hours.
- **Spare parts** are an illustration: the logs do not say which repairs used a part.

## Repository

```
src/wind_om/
  schema.py       generic log, YAML column mapping, time zones and DST
  mapping.py      code -> component / role / category / stopping
  events.py       open events, episodes, first-up attribution, fleet-wide events, exposure, periods
  pareto.py       Pareto by code / component / category for any period
  reliability.py  lognormal, Weibull, repeat chains, turbine bootstrap, trend, bad actors, jack-knife;
                  MTBF / MTTR, Crow-AMSAA from res-asset-kpi
  compound.py     repeats, cascades, restoration and response time
  spares.py       expected failures per component, Poisson stock
  scada.py        lost energy, log-vs-SCADA agreement, clock check, code catalogue (uses wind_downtime)
  report.py       monthly KPI pack: Excel and HTML
  sources.py      adapters of the three open farms (through the same YAML files)
  fetch.py        exact byte-range download of the open logs
  study.py        the study, sensitivity of the counts, code-dictionary coverage, dashboard data, demo, DST check
  synthetic.py    synthetic logs with known parameters
  configs/, mappings/
tests/            synthetic recovery, hard cases, SCADA checks, report files, CLI
docs/             dashboard (GitHub Pages) and example KPI packs
results/          the study's tables per farm
```

MIT licence. Python 3.11+.
