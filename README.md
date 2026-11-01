# Purchase-to-pay process mining: where invoices stall and rules break

> **Status: in progress.** Part 1 of 5 is done (source, snapshot and profile). See the [plan](#plan).

A company's purchase-to-pay process is meant to be simple: raise an order, receive the goods, match
the invoice, pay. In a real ERP system it rarely is. This project uses **process mining** on a real
event log of 1.6 million events, using DuckDB SQL and Python, to answer the questions a procurement
or accounts-payable lead and an internal auditor would ask:

1. **Compliance:** are invoices paid only after the checks their flow requires?
2. **Bottlenecks:** where do items wait, and is that wait in the process or in the payment calendar?
3. **Rework:** how often are orders and invoices changed, cancelled or redone?
4. **Automation:** which steps, vendors and spend areas would repay fixing first?

It ties to business analysis and digital transformation: instead of asking people how a process
works, it measures what the system recorded.

## Data

| | |
|---|---|
| Dataset | [BPI Challenge 2019](https://doi.org/10.4121/uuid:d06aff4b-79f0-45e6-8ec8-e19730c248f1), from 4TU.ResearchData |
| What it is | Purchase-order handling at a large multinational in coatings and paints, based in the Netherlands |
| Size | 251,734 purchase items, 76,349 purchase documents, 1,595,923 events, 42 activities |
| Period | Mainly 1 Jan 2018 to 18 Jan 2019 |
| Licence | CC BY 4.0. Citation: van Dongen, Boudewijn (2019). BPI Challenge 2019. 4TU.ResearchData. Dataset. |
| In this repo | `data/events.csv.gz` and `data/cases.csv.gz` (11.8 MB), parsed from the raw log with nothing filtered or corrected |

**Getting the raw log:** `scripts/download_data.py` tries 4TU.ResearchData, then figshare, then a
public GitHub copy, and keeps a file only if its contents match the MD5 published with the
dataset. When this was built, 4TU timed out and figshare returned errors from the build machine,
so the raw log came from the [GitHub copy](https://github.com/Gength/generalization_analysis). Its
MD5 matches exactly, as do all 5 counts in the dataset's description. The Internet Archive's
capture was rejected, as it holds only the first 1 MiB. You don't need the raw log to run the
analysis.

## Part 1: what the log can and can't tell us

Full results, with every rule for the later parts: [`results/SOURCE_PROFILE.md`](results/SOURCE_PROFILE.md).

Before asking what the process does, this part checks what the data means. That turned out to
matter: **seven plausible readings of this log are wrong**, and several would pass unnoticed.

| Trap | What the data shows | Rule from here on |
|---|---|---|
| **The value field isn't real money, and doesn't add up** | Values are anonymised, so only shares hold. Summing the field gives 28.55 billion, 28.6 times the 0.997 billion you get counting each item once. 19 of the 25 common activities, including `Clear Invoice` and even `Change Price`, just repeat the same value. | Count each item once. Report shares. |
| **Some dates are wrong, and vendor dates aren't times** | 327 events fall outside the main span, as early as 1948. Vendor events all sit at 23:59 Dutch time, switching exactly with daylight saving. | Treat them as dates, not times. |
| **Requisition records jump** | 136-229 a month to August, then 11,422 in September, while POs hold steady. The log doesn't say why. | No "PO without requisition" trend across the break. |
| **The log just stops** | Cleared share falls from 92-94% in early 2018 to 5% for December, because the median PO takes 79.1 days to reach first clearing. | Measure on items with enough follow-up. |
| **A named account isn't always a person** | `user_002` clears 85.6% of invoices on 94 days, and 97.0% of named clearing happens in bursts of 50+ events in one minute. | "Named" doesn't mean "manual". |
| **Ties and repeats inflate paths** | 11,973 paths as logged. Collapsing immediate repeats leaves 7,246. | Treat path counts as a guide. |
| **A few items flood event counts** | 164,975 service entry sheet events sit on 5,604 items. | Count items, not events. |

![Requisition records by month](results/charts/01_requisitions_by_month.png)

![Cleared share by PO month](results/charts/02_clearing_by_po_month.png)

**The process has a clear core and a long tail.** The top 10 paths cover 59.5% of items, the top 100
cover 86.2%, and 9,030 paths were followed by a single item.

![Path coverage](results/charts/03_path_coverage.png)

**Two leads for the next part,** on items with at least 6.5 months of follow-up. The share with at
least one cleared invoice differs a lot by flow: 94.0% for 3-way invoice-before-receipt (the
main flow, 87.8% of items), 78.5% for 3-way invoice-after-receipt and 28.6% for 2-way match (88 of
308 items). These are not yet explained.

### So what for the business?

- **Trust this log for counts and shares, not for euros.** It can show which rules were followed,
  where items wait and how often work is redone. It can't size the money at stake in real
  terms, and its clearing events don't show what was actually paid. Any savings figure would need
  real amounts from finance.
- **A dashboard built on the raw fields would have misled.** It would have shown spend 28 times too
  high, a slowdown at year-end that's really the log stopping, and one person doing most of the
  invoice clearing.
- **Ask the process owner one question first:** what changed with requisitions in September 2018?
  The answer decides whether later compliance figures can cover the whole year.

## Plan

1. ✅ **Source, snapshot and profile.** Verified download, strict parser, data-quality traps.
2. **Compliance.** Rule checks per flow in DuckDB SQL: clearing before goods receipt, invoices dated
   before the PO, and approvals and payment blocks, each sized as a share of items.
3. **Bottlenecks.** A process map, waiting times that handle unfinished items, and how much of the
   wait comes from the payment-run calendar.
4. **Rework and automation.** Changes, cancellations and repeat work by vendor and spend area,
   turned into a ranked list of what to fix first.
5. **CI and write-up.** Automated checks, TL;DR, ranked recommendations and limitations.

## How to run

```bash
uv venv .venv && uv pip install -r requirements-lock.txt   # or: pip install -r requirements.txt
source .venv/bin/activate

python -m scripts.profile_log     # rebuilds results/ from the committed snapshot (about 25 seconds)
pytest                            # about 3 minutes

# Optional, to rebuild the snapshot from the source:
python -m scripts.download_data   # 17-730 MB depending on the host, verified by MD5
python -m scripts.build_snapshot  # about 3 minutes
```

## Tests

36 tests. The headline numbers are recounted in plain pandas, with none of the project's SQL. The
parser and downloader are tested on small hand-written files, including truncated and wrong
copies. A rebuild from the raw log must match the committed files byte for byte, and the report
and charts must match a fresh run. To check that the tests can fail, 21 bugs were planted one at a
time, such as a wrong follow-up window or a bad MD5 check, and all 21 were caught.

## Layout

```
data/              snapshot (events, cases) and SNAPSHOT.json with its hashes
p2p/               source details, XES parser, DuckDB helper
scripts/           download, snapshot and profile steps
sql/               table definitions and every query behind the report
results/           generated report and charts
tests/             pytest suite
```

## Credit

Data: van Dongen, Boudewijn (2019). *BPI Challenge 2019*. 4TU.ResearchData. Dataset.
https://doi.org/10.4121/uuid:d06aff4b-79f0-45e6-8ec8-e19730c248f1, licensed under
[CC BY 4.0](https://creativecommons.org/licenses/by/4.0/). The snapshot in `data/` is a CSV
conversion of that log with the `org:resource` field dropped, because it always equals `User`.
