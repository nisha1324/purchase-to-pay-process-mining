-- The queries behind results/SOURCE_PROFILE.md. Each one starts with a "-- name:" line.
-- $window_start, $window_end, $follow_start, $follow_end and $burst_min are set in
-- p2p/source.py.

-- name: totals
select (select count(*) from cases)                                   as n_items,
       (select count(distinct purchasing_document) from cases)        as documents,
       (select count(*) from events)                                  as n_events,
       (select count(distinct activity) from events)                  as activities,
       (select count(distinct account) from events
         where account_type = 'named')                                as named_accounts,
       (select count(distinct account) from events
         where account_type = 'batch')                                as batch_accounts,
       (select count(distinct vendor) from cases)                     as vendors,
       (select count(distinct company) from cases)                    as companies,
       (select count(*) from cases
         where company = (select mode(company) from cases))           as items_top_company,
       (select sum(po_value) from items)                              as po_value,
       (select sum(event_value) from events)                          as naive_value_sum,
       (select median(n_events) from items)                           as median_events,
       (select count(*) from items where n_events > 100)              as long_items,
       (select sum(n_events) from items where n_events > 100)         as long_item_events,
       (select max(n_events) from items)                              as max_events;

-- name: flows
-- Clearing and deletion are measured on items with a long enough follow-up.
select item_category                                                  as flow,
       count(*)                                                       as n_items,
       sum(po_value)                                                  as po_value,
       median(po_value)                                               as median_value,
       median(n_events)                                               as median_events,
       count(*) filter (where po_ts >= $follow_start::timestamp
                          and po_ts <  $follow_end::timestamp)        as cohort,
       count(*) filter (where po_ts >= $follow_start::timestamp
                          and po_ts <  $follow_end::timestamp
                          and n_clear > 0)                            as cohort_cleared
from items
group by item_category
order by n_items desc;

-- name: value_copies
-- How often an event's value simply repeats the value on the item's PO creation event.
select e.activity,
       count(*)                                                       as n_events,
       count(*) filter (where abs(e.event_value - i.po_value) < 0.005) as n_same
from events e
join items i using (case_id)
group by e.activity
having count(*) >= 1000
order by n_same * 1.0 / count(*) desc, n_events desc;

-- name: invoice_sums
-- The publisher says invoices on one item should add up to the item's worth.
with receipts as (
    select case_id, count(*) as n, sum(event_value) as total, min(event_value) as lo,
           max(event_value) as hi
    from events
    where activity = 'Record Invoice Receipt'
    group by case_id
)
select count(*)                                                       as invoiced_items,
       count(*) filter (where abs(r.total - i.po_value) < 0.5)        as sum_matches,
       count(*) filter (where r.n > 1)                                as multi_invoice,
       count(*) filter (where r.n > 1 and r.lo = r.hi)                as multi_same_value
from receipts r
join items i using (case_id);

-- name: zero_value
select count(*) filter (where po_value = 0)                           as zero_items,
       count(*) filter (where po_value = 0
                          and item_category = 'Consignment')          as zero_consignment,
       (select count(*) from cases where item_category = 'Consignment') as consignment
from items;

-- name: out_of_window
select count(*)                                                       as n_events,
       count(*) filter (where ts <  $window_start::timestamp)         as before_start,
       count(*) filter (where ts >= $window_end::timestamp)           as after_end,
       count(*) filter (where activity in ('Vendor creates invoice',
                                           'Vendor creates debit memo')) as vendor_dated,
       count(*) filter (where activity = 'Create Purchase Requisition Item'
                          and year(ts) = 2017)                        as requisitions_2017,
       count(*) filter (where ts < '2017-01-01'::timestamp)           as before_2017,
       min(ts)                                                        as earliest,
       max(ts)                                                        as latest,
       count(distinct case_id)                                        as affected_items
from events
where ts < $window_start::timestamp or ts >= $window_end::timestamp;

-- name: window_edges
select min(ts) as first_ts, max(ts) as last_ts
from events
where ts >= $window_start::timestamp and ts < $window_end::timestamp;

-- name: date_only
-- Vendor dates carry no time of day: 23:59 in Amsterdam, shown here in UTC.
select strftime(ts, '%H:%M')                                          as utc_time,
       count(*)                                                       as n_events,
       min(ts)::date                                                  as first_day,
       max(ts)::date                                                  as last_day
from events
where activity in ('Vendor creates invoice', 'Vendor creates debit memo')
  and ts >= $window_start::timestamp and ts < $window_end::timestamp
group by utc_time
order by n_events desc;

-- name: date_only_clearing
-- Clearing with no account has the same shape as the vendor dates.
select count(*)                                                       as n_events,
       count(*) filter (where strftime(ts, '%H:%M') in ('21:59', '22:59')) as at_2359_local,
       count(distinct ts::date)                                       as n_days
from events
where activity = 'Clear Invoice' and account_type = 'none';

-- name: monthly
select date_trunc('month', ts)::date                                  as month,
       count(*) filter (where activity = 'Create Purchase Order Item') as po_items,
       count(*) filter (where activity = 'Create Purchase Requisition Item') as requisitions
from events
where ts >= '2018-01-01'::timestamp and ts < '2019-01-01'::timestamp
group by month
order by month;

-- name: cleared_by_month
select date_trunc('month', po_ts)::date                               as month,
       count(*)                                                       as n_items,
       count(*) filter (where n_clear > 0)                            as cleared
from items
where item_category like '3-way%' and po_ts < '2019-01-01'::timestamp
group by month
order by month;

-- name: clearing_time
-- PO creation to first clearing, in days, on 3-way items with a long enough follow-up.
select median(date_diff('minute', po_ts, first_clear_ts) / 1440.0)    as median_days,
       quantile_cont(date_diff('minute', po_ts, first_clear_ts) / 1440.0, 0.9) as p90_days,
       count(*)                                                       as n_items
from items
where item_category like '3-way%'
  and first_clear_ts is not null
  and po_ts >= $follow_start::timestamp and po_ts < $follow_end::timestamp;

-- name: accounts_by_activity
-- Who does the work, as the log labels it.
select activity,
       count(*)                                                       as n_events,
       count(*) filter (where account_type = 'named')                 as named,
       count(*) filter (where account_type = 'batch')                 as batch,
       count(*) filter (where account_type = 'none')                  as no_account
from events
where activity in ('Create Purchase Order Item', 'Record Goods Receipt',
                   'Record Invoice Receipt', 'Remove Payment Block', 'Clear Invoice')
group by activity
order by n_events desc;

-- name: bursts
-- Events that share an activity, an account and a minute with many others.
with minutes as (
    select activity, account, date_trunc('minute', ts) as minute, count(*) as n
    from events
    where account_type = 'named'
      and activity in ('Create Purchase Order Item', 'Record Goods Receipt',
                       'Record Invoice Receipt', 'Remove Payment Block', 'Clear Invoice')
    group by activity, account, minute
)
select activity,
       sum(n)                                                         as named_events,
       coalesce(sum(n) filter (where n >= $burst_min), 0)             as in_bursts
from minutes
group by activity
order by activity;

-- name: clearing_accounts
select account,
       count(*)                                                       as n_events,
       count(distinct ts::date)                                       as active_days,
       sum(count(*)) over ()                                          as all_clearing
from events
where activity = 'Clear Invoice'
group by account
order by n_events desc
limit 3;

-- name: repeats
-- Rows identical on item, activity, minute and value.
with groups_ as (
    select case_id, activity, ts, event_value, count(*) as n
    from events
    group by all
    having count(*) > 1
)
select count(*)                                                       as repeat_groups,
       sum(n - 1)                                                     as extra_rows,
       coalesce(sum(n - 1) filter (where activity = 'Record Service Entry Sheet'), 0)
                                                                      as extra_service_rows
from groups_;

-- name: service_sheets
select count(*)                                                       as n_events,
       count(distinct case_id)                                        as n_items
from events
where activity = 'Record Service Entry Sheet';

-- name: ties
with steps as (
    select ts, lag(ts) over (partition by case_id order by seq) as previous_ts
    from events
)
select count(*) filter (where ts = previous_ts)                       as tied_steps,
       count(previous_ts)                                             as all_steps
from steps;

-- name: variants
-- Three counts of "how many paths": as logged; with same-minute events put in a fixed
-- order, so an arbitrary tie can't create a path; and also with immediate repeats
-- collapsed, so 3 goods receipts in a row count like 1.
with ordered as (
    select case_id, activity,
           row_number() over (partition by case_id order by ts, activity, seq) as step
    from events
),
marked as (
    select *, lag(activity) over (partition by case_id order by step) as previous_activity
    from ordered
),
paths as (
    select case_id,
           (select string_agg(activity, ' > ' order by seq) from events e
             where e.case_id = m.case_id)                             as as_logged,
           string_agg(activity, ' > ' order by step)                  as tie_fixed,
           string_agg(activity, ' > ' order by step)
               filter (where previous_activity is distinct from activity) as collapsed
    from marked m
    group by case_id
)
select count(distinct as_logged)                                      as as_logged,
       count(distinct tie_fixed)                                      as tie_fixed,
       count(distinct collapsed)                                      as collapsed
from paths;

-- name: variant_sizes
-- Items per path, largest first, using paths exactly as logged.
with paths as (
    select case_id, string_agg(activity, ' > ' order by seq) as path
    from events
    group by case_id
)
select path, count(*) as n_items
from paths
group by path
order by n_items desc, path;
