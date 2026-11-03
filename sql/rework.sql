-- Queries behind the rework and automation report. Tables come from sql/30_rework.sql, which needs
-- 10_item_facts.sql and 20_waits.sql first.

-- name: scope
select count(*)                                  as n_items,
       sum(po_value)                             as total_value,
       count(*) filter (where any_rework)        as rework_items,
       sum(po_value) filter (where any_rework)   as rework_value
from item_rework;

-- name: modes
-- How each step is done, by event, and how many one-at-a-time touches it takes.
with events as (
    select kind, step,
           count(*)                                           as n_events,
           count(*) filter (where mode = 'System')            as system_events,
           count(*) filter (where mode = 'Vendor or none')    as vendor_events,
           count(*) filter (where mode = 'Bulk')              as bulk_events,
           count(*) filter (where mode = 'One at a time')     as single_events,
           count(distinct case_id)                            as n_items
    from work_events
    group by kind, step
),
touched as (
    select step, count(*) as n_touches, count(distinct account) as n_accounts
    from touches
    group by step
)
select e.*, coalesce(t.n_touches, 0) as n_touches, coalesce(t.n_accounts, 0) as n_accounts
from events e
left join touched t using (step)
order by n_touches desc, n_events desc;

-- name: touch_totals
select kind, count(*) as n_touches, sum(n_lines) as n_lines
from touches
group by kind
order by kind;

-- name: rework_rates
-- Items with each kind of rework, as shares of items and of value.
with kinds as (
    select 'Price change' as step, price_change as hit, po_value from item_rework
    union all select 'Quantity change', quantity_change, po_value from item_rework
    union all select 'Other PO change', other_change, po_value from item_rework
    union all select 'Deletion or block', deletion, po_value from item_rework
    union all select 'Invoice reversal', invoice_reversal, po_value from item_rework
    union all select 'Goods reversal', goods_reversal, po_value from item_rework
    union all select 'Vendor debit memo', debit_memo, po_value from item_rework
    union all select 'Any rework', any_rework, po_value from item_rework
)
select step,
       count(*) filter (where hit)              as n_items,
       count(*)                                 as of_items,
       sum(po_value) filter (where hit)         as hit_value,
       sum(po_value)                            as of_value
from kinds
group by step
order by step = 'Any rework', n_items desc;

-- name: value_bands
-- Rework rate by item value, in five equal-sized bands, to see why value share runs above item share.
with banded as (
    select *, ntile(5) over (order by po_value, case_id) as band
    from item_rework
)
select band, count(*) as n_items, min(po_value) as low, max(po_value) as high,
       avg(any_rework::integer) as rework_rate
from banded
group by band
order by band;

-- name: change_timing
-- When price and quantity changes happen, against the item's first recorded invoice.
select activity,
       count(*)                                                    as n_events,
       count(*) filter (where first_ir_ts is null)                 as no_invoice,
       count(*) filter (where ts < first_ir_ts)                    as before_invoice,
       count(*) filter (where ts >= first_ir_ts)                   as after_invoice
from work_events
where activity in ('Change Price', 'Change Quantity')
group by activity
order by activity;

-- name: price_loop
-- A price changed after the invoice is recorded: does a payment block come off after it? Gaps are in
-- Dutch calendar days, like every other day count in this project.
select count(*)                                                          as n_items,
       count(block_after_price_ts)                                       as block_after,
       count(*) filter (where n_block_removed > 0)                       as any_block,
       count(*) filter (where dutch_day(block_after_price_ts) = dutch_day(first_late_price_ts))
                                                                         as same_day,
       count(*) filter (where dutch_day(block_after_price_ts) - dutch_day(first_late_price_ts) <= 7)
                                                                         as within_week,
       sum(po_value)                                                     as loop_value
from item_rework
where price_after_invoice;

-- name: price_loop_base
-- The block rate on main-flow items with a recorded invoice and no price change, for comparison.
select count(*) as n_items, count(*) filter (where n_block_removed > 0) as blocked
from item_rework
where flow = '3-way match, invoice before GR' and n_ir > 0 and not price_change;

-- name: change_extra_days
-- Days from invoice date to first clearing, against items with no rework at all at the same vendor
-- with an invoice dated the same month. Only items the wait breakdown measures (not corrected), so
-- reversals and debit memos can't be timed here. Each item takes the first group that fits.
with timed as (
    select w.case_id, w.vendor, w.days_to_clear, w.overrun,
           date_trunc('month', w.vendor_invoice_day) as invoice_month,
           r.n_block_removed > 0 as blocked,
           case when r.price_after_invoice then 'Price changed after the invoice'
                when r.price_change        then 'Price changed before the invoice'
                when r.quantity_change     then 'Quantity changed'
                when r.other_change        then 'Other PO change'
                when r.any_rework          then 'Other rework'
                else 'No rework' end       as rework
    from waits w
    join item_rework r using (case_id)
),
baseline as (
    select vendor, invoice_month, avg(days_to_clear) as usual_days, avg(overrun) as usual_overrun
    from timed
    where rework = 'No rework'
    group by vendor, invoice_month
    having count(*) >= 5
)
select rework,
       case when month(invoice_month) <= 3 then 'Jan-Mar' else 'Apr-Jun' end as invoices,
       count(*)                                 as n_items,
       avg(days_to_clear - usual_days)          as extra_days,
       avg(overrun - usual_overrun)             as extra_overrun,
       avg(blocked::integer)                    as block_rate
from timed t
join baseline b using (vendor, invoice_month)
where invoice_month >= date '2018-01-01' and invoice_month < date '2018-07-01'
group by grouping sets ((rework), (rework, invoices))
order by rework, invoices nulls first;

-- name: deletions
-- Items deleted, and how many were deleted before anything was received or invoiced.
select count(*)                                                          as n_deleted,
       count(*) filter (where n_gr = 0 and n_ir = 0)                     as unused,
       median(delete_day - po_day) filter (where n_gr = 0 and n_ir = 0)  as median_days,
       count(*) filter (where n_gr = 0 and n_ir = 0 and delete_day - po_day <= 7)
                                                                         as within_week,
       sum(po_value) filter (where n_gr = 0 and n_ir = 0)                as unused_value
from item_rework
where delete_day is not null;

-- name: deleted_by_creator
-- Of unused deleted items, how many were deleted by the account that raised the PO.
with made as (
    select case_id, account as creator
    from work_events
    where activity = 'Create Purchase Order Item' and mode = 'One at a time'
),
gone as (
    select case_id, account as deleter
    from work_events
    where activity = 'Delete Purchase Order Item'
)
select count(distinct r.case_id)                                      as n_items,
       count(distinct r.case_id) filter (where g.deleter = m.creator) as same_account
from item_rework r
join made m using (case_id)
join gone g using (case_id)
where r.n_gr = 0 and r.n_ir = 0;

-- name: reentry
select 'Invoice' as what,
       count(*) filter (where invoice_cancelled)                       as cancelled,
       count(*) filter (where invoice_reentered)                       as reentered
from item_rework
union all
select 'Goods',
       count(*) filter (where goods_cancelled),
       count(*) filter (where goods_reentered)
from item_rework;

-- name: by_area
-- Rework by spend area, for areas with at least 1,000 items.
with items as (
    select coalesce(spend_area, '(blank)') as spend_area, count(*) as n_items,
           count(*) filter (where any_rework)           as rework_items,
           count(*) filter (where price_after_invoice)  as price_after_items
    from item_rework
    group by 1
),
work as (
    select coalesce(spend_area, '(blank)') as spend_area,
           count(*) filter (where kind = 'Rework') as rework_touches,
           count(*)                                as all_touches
    from touches
    group by 1
)
select i.*, coalesce(w.rework_touches, 0) as rework_touches, coalesce(w.all_touches, 0) as all_touches
from items i
left join work w using (spend_area)
where i.n_items >= 1000
order by n_items desc;

-- name: area_totals
select count(*) as n_items,
       (select count(*) from touches where kind = 'Rework') as rework_touches,
       (select count(*) from touches) as all_touches
from item_rework;

-- name: vendor_rework
-- One row per vendor: items, rework, and its one-at-a-time rework touches.
with items as (
    select vendor, count(*) as n_items, count(*) filter (where any_rework) as rework_items
    from item_rework
    group by vendor
),
work as (
    select vendor, count(*) as rework_touches
    from touches
    where kind = 'Rework'
    group by vendor
)
select i.vendor, i.n_items, i.rework_items, coalesce(w.rework_touches, 0) as rework_touches
from items i
left join work w using (vendor)
order by rework_touches desc, i.vendor;

-- name: candidates
-- Each step's one-at-a-time touches, who does them, and how concentrated they are by vendor.
with ranked as (
    select step, kind, vendor, count(*) as n_touches,
           row_number() over (partition by step order by count(*) desc, vendor) as position
    from touches
    group by step, kind, vendor
)
select step, any_value(kind) as kind,
       sum(n_touches)                                      as n_touches,
       sum(n_touches) filter (where position <= 10)        as top10_touches,
       count(*)                                            as n_vendors,
       (select count(distinct account) from touches t where t.step = r.step) as n_accounts,
       (select count(distinct purchasing_document) from touches t where t.step = r.step) as n_documents
from ranked r
group by step
order by n_touches desc;

-- name: bulk_sensitivity
-- How many touches stay one at a time if a smaller bulk rule is used: an account handling
-- this many different documents of the same step in one minute counts as bulk.
with per_minute as (
    select step, kind, account, ts, count(*) as n_docs
    from touches
    group by step, kind, account, ts
)
select step, kind,
       sum(n_docs)                                  as touches,
       sum(n_docs) filter (where n_docs < 5)        as under_5_docs,
       sum(n_docs) filter (where n_docs < 2)        as single_doc
from per_minute
group by step, kind
order by touches desc;

-- name: loop_areas
select coalesce(spend_area, '(blank)') as spend_area, count(*) as n_items
from item_rework
where price_after_invoice
group by 1
order by n_items desc, spend_area;

-- name: loop_vendors
select vendor, count(*) as n_items
from item_rework
where price_after_invoice
group by vendor
order by n_items desc, vendor;

-- name: invoice_accounts
select account, count(*) as n_touches
from touches
where step = 'Invoice recording'
group by account
order by n_touches desc, account;
