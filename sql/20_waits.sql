-- Working tables for the bottleneck analysis. Built on top of 10_item_facts.sql.
--
-- Only the two 3-way flows are timed here: consignment has no invoices, and the 2-way flow is one
-- login with invoices dated before the PO (see the compliance report). Everything is in Dutch
-- calendar days, because vendor dates have no time of day.

-- Days with clearing, and which of them are payment runs.
create table clearing_days as
select day,
       count(*)                                       as n_events,
       count(*) >= $run_floor::integer                as is_run,
       count(*) >= $main_run_floor::integer           as is_main_run,
       case when count(*) >= $main_run_floor::integer
            then case when day(day) <= $early_last_day::integer then 'early' else 'late' end
       end                                            as main_run
from day_events
where activity = 'Clear Invoice'
group by day;

-- What had happened by the day an item first cleared.
create table steps_before_clear as
select e.case_id,
       max(e.day) filter (where e.activity = 'Record Goods Receipt')   as last_goods_day,
       max(e.day) filter (where e.activity = 'Record Invoice Receipt') as last_invoice_day,
       max(e.day) filter (where e.activity = 'Remove Payment Block')   as last_block_day
from day_events e
join facts f using (case_id)
where e.day <= f.clear_day
group by e.case_id;

-- Items the wait breakdown can be measured on: created in the cohort, cleared, with a vendor
-- invoice date not after the clearing. Corrected items are left out, because a clearing there can
-- be a reversal being settled, not an invoice being paid.
create table timed_items as
select f.case_id, f.flow, f.vendor, f.spend_area, f.po_value,
       f.po_day, f.vendor_invoice_day, f.ir_day, f.gr_day, f.clear_day, f.n_block_removed,
       f.clear_day - f.vendor_invoice_day as days_to_clear,
       coalesce(greatest(s.last_goods_day, s.last_invoice_day, s.last_block_day),
                f.vendor_invoice_day)     as ready_day,
       s.last_goods_day, s.last_invoice_day, s.last_block_day
from facts f
join steps_before_clear s using (case_id)
where f.period = 'cohort'
  and f.flow in ('3-way match, invoice before GR', '3-way match, invoice after GR')
  and f.n_clear > 0
  and f.vendor_invoice_day is not null
  and f.clear_day >= f.vendor_invoice_day
  and not f.corrected;

-- A vendor's implied term: the rank-th fastest of its invoices, where rank = ceil(n * q).
create table vendor_terms as
with ranked as (
    select vendor, days_to_clear,
           row_number() over (partition by vendor order by days_to_clear) as position,
           count(*)     over (partition by vendor)                        as n_items
    from timed_items
)
select vendor, n_items, days_to_clear::integer as term_days
from ranked
where n_items >= $term_min::integer
  and position = ceil(n_items * $term_q::double);

-- One row per timed item: PO to clearing, cut into four parts that add up exactly.
--   to_invoice  PO to the vendor's invoice date (negative when the invoice is older than the PO)
--   term        invoice date to the implied due day, or to clearing if that came first
--   overrun     due day to the day the last step finished, if that was later
--   run_wait    ready and due, waiting for a payment run
create table waits as
with dated as (
    select t.*, v.term_days,
           least(t.vendor_invoice_day + v.term_days, t.clear_day) as due_day
    from timed_items t
    join vendor_terms v using (vendor)
),
moved as (
    select *, greatest(due_day, ready_day) as free_day
    from dated
)
select *,
       vendor_invoice_day - po_day as to_invoice,
       due_day - vendor_invoice_day as term,
       free_day - due_day          as overrun,
       clear_day - free_day        as run_wait,
       clear_day - po_day          as total,
       case when ready_day <= due_day            then 'On time'
            when ready_day = last_block_day      then 'Payment block'
            when ready_day = last_invoice_day    then 'Invoice recording'
            else 'Goods receipt' end             as held_by,
       least(clear_day - free_day, (11 - dayofweek(free_day)) % 7) as run_wait_weekly
from moved;

-- Each item's first time at each main step. A vendor's date goes first among same-day events,
-- because the invoice exists before anyone records it.
create table map_steps as
with first_steps as (
    select e.case_id, e.activity, min(e.day) as day, min(e.ts) as ts, min(e.seq) as seq
    from day_events e
    join facts f using (case_id)
    where f.period = 'cohort'
      and f.flow in ('3-way match, invoice before GR', '3-way match, invoice after GR')
      and e.activity in ('Create Purchase Order Item', 'Vendor creates invoice',
                         'Record Goods Receipt', 'Record Invoice Receipt',
                         'Remove Payment Block', 'Clear Invoice')
      and (e.activity <> 'Vendor creates invoice' or e.ts >= $vendor_floor::timestamp)
    group by e.case_id, e.activity
)
select *,
       row_number() over (partition by case_id
                          order by day, activity = 'Vendor creates invoice' desc, ts, seq) as step
from first_steps;

-- Main-flow items with a recorded invoice, sorted by what could have set a payment block. The log
-- rarely shows a block being set, so this reads what's visible when one is removed. Each item
-- takes the first row that applies.
create table block_items as
with signals as (
    select e.case_id,
           min(e.ts) filter (where e.activity = 'Record Goods Receipt')   as first_goods_ts,
           max(e.ts) filter (where e.activity = 'Record Goods Receipt')   as last_goods_ts,
           min(e.ts) filter (where e.activity = 'Record Invoice Receipt') as first_invoice_ts,
           bool_or(e.activity in ('Change Price', 'Change Quantity'))     as po_changed
    from day_events e
    join facts f using (case_id)
    where f.period = 'cohort' and f.flow = '3-way match, invoice before GR' and f.n_ir > 0
    group by e.case_id
)
select f.case_id, f.vendor, f.spend_area, f.po_value, f.ir_day, f.gr_day, f.block_day,
       f.n_block_removed > 0 as blocked,
       case when s.first_goods_ts is null or s.first_invoice_ts < s.first_goods_ts
                then 'Invoice recorded before the goods'
            when s.first_invoice_ts < s.last_goods_ts
                then 'Invoice recorded between deliveries'
            when f.n_ir > 1   then 'Several invoices on the item'
            when s.po_changed then 'PO price or quantity changed'
            else 'No reason visible in the log'
       end as reason
from facts f
join signals s using (case_id);
