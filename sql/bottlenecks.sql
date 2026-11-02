-- Every number behind the bottleneck report. Run after 10_item_facts.sql and 20_waits.sql.
-- All times are whole Dutch calendar days. Each query is named so the report can call it.

-- name: durations
-- One row per 3-way item created in the log's span. A blank "end" means it hadn't happened when
-- the log stopped, so the time from the start to the log's last day is a lower bound.
select flow,
       month(po_day)                                    as po_month,
       gr_day - po_day                                  as po_to_goods,
       vendor_invoice_day - po_day                      as po_to_invoice_date,
       ir_day - vendor_invoice_day                      as invoice_date_to_recorded,
       clear_day - ir_day                               as recorded_to_clear,
       clear_day - gr_day                               as goods_to_clear,
       clear_day - po_day                               as po_to_clear,
       $log_end::date - po_day                          as open_from_po,
       $log_end::date - gr_day                          as open_from_goods,
       $log_end::date - vendor_invoice_day              as open_from_invoice_date,
       $log_end::date - ir_day                          as open_from_recorded
from facts
where flow in ('3-way match, invoice before GR', '3-way match, invoice after GR')
  and po_ts >= $window_start::timestamp and po_ts < $window_end::timestamp
order by case_id;

-- name: map_nodes
select activity, count(*) as n_items
from map_steps
group by activity
order by n_items desc;

-- name: map_edges
select a.activity                             as from_step,
       b.activity                             as to_step,
       count(*)                               as n_items,
       median(b.day - a.day)                  as median_days,
       quantile_cont(b.day - a.day, 0.9)      as p90_days
from map_steps a
join map_steps b on b.case_id = a.case_id and b.step = a.step + 1
group by a.activity, b.activity
order by n_items desc, from_step, to_step;

-- name: map_ends
-- Where each item's path stops. Anything but Clear Invoice is an item with nothing cleared.
with ends as (
    select case_id, arg_max(activity, step) as last_step
    from map_steps
    group by case_id
)
select last_step, count(*) as n_items
from ends
group by last_step
order by n_items desc, last_step;

-- name: clearing_bands
select case when n_events >= $main_run_floor::integer then 'Main runs'
            when n_events >= $run_floor::integer      then 'Smaller runs'
            else 'Other days' end                   as band,
       count(*)                                     as n_days,
       sum(n_events)                                as n_events
from clearing_days
group by band
order by n_events desc;

-- name: first_clearing_on_main_runs
-- Items, not events: where each 3-way cohort item first cleared.
select count(*)                                     as n_items,
       count(*) filter (where d.is_main_run)        as on_main_run,
       count(*) filter (where d.is_run)             as on_any_run,
       count(*) filter (where dayname(f.clear_day) = 'Thursday') as on_thursday
from facts f
join clearing_days d on d.day = f.clear_day
where f.period = 'cohort'
  and f.flow in ('3-way match, invoice before GR', '3-way match, invoice after GR')
  and f.n_clear > 0;

-- name: run_calendar
select day, dayname(day) as weekday, n_events, is_main_run, main_run
from clearing_days
where is_run
order by day;

-- name: main_run_gaps
with runs as (
    select day, day - lag(day) over (order by day) as gap
    from clearing_days
    where is_main_run
)
select count(*) as n_runs, min(gap) as shortest, max(gap) as longest,
       arg_max(day, gap) as longest_ended, arg_max(day - gap::integer, gap) as longest_began
from runs;

-- name: vendor_run_ties
-- Does a vendor belong to one of the two main runs? Measured on cohort items that cleared on one.
with mix as (
    select f.vendor,
           count(*)                                     as n_items,
           count(*) filter (where d.main_run = 'early') as early
    from facts f
    join clearing_days d on d.day = f.clear_day
    where f.period = 'cohort' and d.is_main_run
      and f.flow in ('3-way match, invoice before GR', '3-way match, invoice after GR')
    group by f.vendor
    having count(*) >= $tie_min::integer
)
select case when early >= n_items * $tie_share::double then 'Tied to the early run'
            when early <= n_items * (1 - $tie_share::double) then 'Tied to the late run'
            else 'Both runs' end  as tie,
       count(*)                   as n_vendors,
       sum(n_items)               as n_items
from mix
group by tie
order by n_vendors desc;

-- name: coverage
select (select count(*) from facts where period = 'cohort')         as all_cohort_items,
       count(*)                                                     as cohort_items,
       count(*) filter (where n_clear > 0)                          as cleared,
       (select count(*) from timed_items)                           as timed,
       (select count(*) from waits)                                 as measured,
       (select count(*) from vendor_terms)                          as n_vendors,
       count(*) filter (where n_clear > 0 and corrected)            as corrected,
       count(*) filter (where n_clear > 0 and not corrected
                          and vendor_invoice_day is null)           as no_invoice_date,
       count(*) filter (where n_clear > 0 and not corrected
                          and clear_day < vendor_invoice_day)       as cleared_before_date
from facts
where period = 'cohort'
  and flow in ('3-way match, invoice before GR', '3-way match, invoice after GR');

-- name: wait_parts
-- Mean days per item, which add up, unlike medians. The value-weighted row counts each item by
-- its share of value, which holds even though the amounts are anonymised.
select 'All 3-way items' as view_name, count(*) as n_items,
       avg(total) as total, avg(to_invoice) as to_invoice, avg(term) as term,
       avg(overrun) as overrun, avg(run_wait) as run_wait, avg(run_wait_weekly) as run_wait_weekly
from waits
union all
select flow, count(*), avg(total), avg(to_invoice), avg(term), avg(overrun), avg(run_wait),
       avg(run_wait_weekly)
from waits
group by flow
union all
select 'Weighted by value', count(*),
       sum(po_value * total) / sum(po_value), sum(po_value * to_invoice) / sum(po_value),
       sum(po_value * term) / sum(po_value), sum(po_value * overrun) / sum(po_value),
       sum(po_value * run_wait) / sum(po_value), sum(po_value * run_wait_weekly) / sum(po_value)
from waits
order by view_name;

-- name: wait_medians
select median(total) as total, median(to_invoice) as to_invoice, median(term) as term,
       median(run_wait) as run_wait, median(days_to_clear) as days_to_clear,
       quantile_cont(days_to_clear, 0.9) as p90_days_to_clear
from waits;

-- name: term_bands
-- Implied terms, in the weekly steps that would show a 30- or 60-day pattern.
select case when term_days < 28 then 'Under 28'
            when term_days < 35 then '28-34'
            when term_days < 42 then '35-41'
            when term_days < 56 then '42-55'
            when term_days < 63 then '56-62'
            when term_days < 84 then '63-83'
            else '84 and over' end    as band,
       min(term_days)                 as first_day,
       count(*)                       as n_vendors,
       sum(n_items)                   as n_items
from vendor_terms
group by band
order by first_day;

-- name: term_holdout
-- Set each vendor's term on invoices dated January to March, then look at April to June. A real
-- term should hold steady, and about one invoice in twenty should beat it.
with split as (
    select *, month(vendor_invoice_day) <= 3 as early_half
    from timed_items
    where vendor_invoice_day >= date '2018-01-01' and vendor_invoice_day < date '2018-07-01'
),
ranked as (
    select vendor, early_half, days_to_clear,
           row_number() over (partition by vendor, early_half order by days_to_clear) as position,
           count(*)     over (partition by vendor, early_half)                        as n_items
    from split
),
terms as (
    select vendor, early_half, days_to_clear as term_days
    from ranked
    where n_items >= $term_min::integer
      and position = ceil(n_items * $term_q::double)
),
both_halves as (
    select a.vendor, a.term_days as first_term, b.term_days as second_term
    from terms a
    join terms b on b.vendor = a.vendor and a.early_half and not b.early_half
),
later as (
    select s.*, b.first_term
    from split s
    join both_halves b using (vendor)
    where not s.early_half
)
select (select count(*) from both_halves)                                   as n_vendors,
       (select median(abs(first_term - second_term)) from both_halves)      as median_gap,
       (select count(*) filter (where abs(first_term - second_term) <= 7)
          from both_halves)                                                 as within_a_week,
       (select count(*) from later)                                         as later_items,
       (select count(*) filter (where days_to_clear < first_term) from later) as beat_term;

-- name: term_sensitivity
-- The same breakdown under other ways of setting the term.
with settings(sort_key, label, q, by_area) as (
    values (1, 'Fastest 1%', 0.01, false),
           (2, 'Fastest 5% (used)', 0.05, false),
           (3, 'Fastest 10%', 0.10, false),
           (4, 'Fastest 5%, per vendor and spend area', 0.05, true)
),
ranked as (
    select s.sort_key, s.label, s.q, t.*,
           row_number() over (partition by s.label, t.vendor,
                              case when s.by_area then t.spend_area end
                              order by t.days_to_clear) as position,
           count(*)     over (partition by s.label, t.vendor,
                              case when s.by_area then t.spend_area end) as n_items,
           case when s.by_area then t.spend_area end as area_key
    from timed_items t
    cross join settings s
),
terms as (
    select label, vendor, area_key, days_to_clear::integer as term_days
    from ranked
    where n_items >= $term_min::integer and position = ceil(n_items * q)
),
parted as (
    select r.sort_key, r.label, r.ready_day, r.vendor_invoice_day, r.clear_day,
           least(r.vendor_invoice_day + t.term_days, r.clear_day) as due_day
    from ranked r
    join terms t on t.label = r.label and t.vendor = r.vendor
                and t.area_key is not distinct from r.area_key
)
select label, count(*) as n_items,
       avg(due_day - vendor_invoice_day)             as term,
       avg(greatest(due_day, ready_day) - due_day)   as overrun,
       avg(clear_day - greatest(due_day, ready_day)) as run_wait
from parted
group by sort_key, label
order by sort_key;

-- name: held_by
select held_by, count(*) as n_items, sum(overrun) as overrun_days, avg(overrun) as mean_overrun,
       median(overrun) as median_overrun
from waits
group by held_by
order by overrun_days desc, held_by;

-- name: overrun_by_area
select spend_area, count(*) as n_items,
       count(*) filter (where overrun > 0) as n_held,
       sum(overrun)                        as overrun_days,
       count(*) filter (where held_by = 'Payment block')     as held_by_block,
       count(*) filter (where held_by = 'Invoice recording') as held_by_invoice
from waits
group by spend_area
order by overrun_days desc, spend_area;

-- name: overrun_concentration
with vendors as (
    select vendor, sum(overrun) as overrun_days,
           row_number() over (order by sum(overrun) desc, vendor) as position
    from waits
    group by vendor
)
select count(*) filter (where overrun_days > 0)                     as n_vendors_held,
       sum(overrun_days) filter (where position <= 10)              as top_ten_days,
       sum(overrun_days)                                            as all_days
from vendors;

-- name: block_reasons
select reason, count(*) as n_items, count(*) filter (where blocked) as n_blocked
from block_items
group by reason
order by n_blocked desc;

-- name: block_totals
select count(*)                                    as main_items,
       count(*) filter (where n_block_removed > 0) as blocked,
       count(*) filter (where n_ir > 0)            as with_invoice,
       count(*) filter (where ir_day < gr_day)     as invoice_earlier_day
from facts
where period = 'cohort' and flow = '3-way match, invoice before GR';

-- name: block_extra_days
-- A blocked item against unblocked items at the same vendor with an invoice dated the same month.
-- Same term and same run, so what's left is more likely the block, though nothing here proves it.
with timed as (
    select w.case_id, w.vendor, w.days_to_clear, w.held_by, b.reason, b.blocked,
           date_trunc('month', w.vendor_invoice_day) as invoice_month
    from waits w
    join block_items b using (case_id)
),
baseline as (
    select vendor, invoice_month, avg(days_to_clear) as usual_days
    from timed
    where not blocked
    group by vendor, invoice_month
    having count(*) >= 5
)
select reason,
       case when month(invoice_month) <= 3 then 'Jan-Mar' else 'Apr-Jun' end as invoices,
       count(*)                                  as n_items,
       avg(days_to_clear - usual_days)           as extra_days,
       count(*) filter (where held_by = 'Payment block') as held_by_block
from timed t
join baseline b using (vendor, invoice_month)
where blocked
  and invoice_month >= date '2018-01-01' and invoice_month < date '2018-07-01'
group by grouping sets ((reason), (reason, invoices), (invoices), ())
order by reason nulls first, invoices nulls first;

-- name: block_vendors
-- How much the unexplained blocks depend on the vendor.
with rates as (
    select vendor, count(*) as n_items, avg(blocked::integer) as rate
    from block_items
    where reason = 'No reason visible in the log'
    group by vendor
    having count(*) >= $tie_min::integer
)
select count(*)                                as n_vendors,
       median(rate)                            as median_rate,
       quantile_cont(rate, 0.1)                as p10_rate,
       quantile_cont(rate, 0.9)                as p90_rate
from rates;

-- name: block_accounts
-- Who lifts the block on main-flow items, and how long after the invoice was recorded.
with lifted as (
    select e.case_id, e.account_type, e.day, f.ir_day
    from day_events e
    join facts f using (case_id)
    where e.activity = 'Remove Payment Block'
      and f.period = 'cohort' and f.flow = '3-way match, invoice before GR'
      and e.day >= f.ir_day
)
select account_type, count(*) as n_events, median(day - ir_day) as median_days
from lifted
group by account_type
order by n_events desc;
