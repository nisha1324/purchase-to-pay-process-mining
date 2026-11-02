-- One row per purchase item with everything the compliance rules need.
-- Built on top of 00_load.sql. Nothing is filtered: every item in the log is here.
--
-- Days are Dutch calendar days. The log's vendor dates and no-account clearing dates are
-- date-only, stamped 23:59 Dutch time (see 3.2 in the source profile), so times of day can't be
-- compared across them. A rule says "before" only when the day is earlier.

create table day_events as
select *,
       timezone('Europe/Amsterdam', timezone('UTC', ts))::date as day
from events;

-- A named account doing 50+ of the same thing in one minute is a bulk run, not one person's
-- decision, so the separation-of-duties rules mark it.
create table bursts as
select activity, account, ts
from events
where account_type = 'named'
group by activity, account, ts
having count(*) >= $burst_min::integer;

-- True when the last delete-or-reactivate before a clearing was a delete.
create table cleared_while_deleted as
with marks as (
    select case_id, activity,
           lag(activity) over (partition by case_id order by ts, seq) as previous
    from events
    where activity in ('Delete Purchase Order Item', 'SRM: Deleted',
                       'Reactivate Purchase Order Item', 'Clear Invoice')
)
select distinct case_id
from marks
where activity = 'Clear Invoice'
  and previous in ('Delete Purchase Order Item', 'SRM: Deleted');

create table facts as
select i.case_id,
       i.purchasing_document,
       i.item_category                                                  as flow,
       i.item_type,
       i.document_type,
       i.spend_area,
       i.sub_spend_area,
       i.vendor,
       i.po_value,
       i.po_ts,
       case when i.po_ts >= $follow_start::timestamp and i.po_ts < $follow_end::timestamp
                then 'cohort'
            when i.po_ts >= $check_start::timestamp and i.po_ts < $check_end::timestamp
                then 'check'
       end                                                              as period,
       i.n_clear,
       i.n_deleted,
       n.n_gr, n.n_gr_cancel, n.n_ir, n.n_ir_cancel, n.n_sub, n.n_sub_cancel,
       n.n_vendor_invoice, n.n_debit_memo, n.n_block_removed, n.n_approval,
       n.po_day, n.gr_day, n.ir_day, n.clear_day, n.block_day,
       n.vendor_invoice_day, n.first_ir_ts, n.first_approval_ts,
       n.n_gr - n.n_gr_cancel                                           as net_goods,
       n.n_ir + n.n_sub - n.n_ir_cancel - n.n_sub_cancel                as net_invoices,
       n.n_ir_cancel + n.n_sub_cancel + n.n_debit_memo > 0              as corrected,
       d.case_id is not null                                            as cleared_after_delete
from items i
join (
    select case_id,
           count(*) filter (where activity = 'Record Goods Receipt')           as n_gr,
           count(*) filter (where activity = 'Cancel Goods Receipt')           as n_gr_cancel,
           count(*) filter (where activity = 'Record Invoice Receipt')         as n_ir,
           count(*) filter (where activity = 'Cancel Invoice Receipt')         as n_ir_cancel,
           count(*) filter (where activity = 'Record Subsequent Invoice')      as n_sub,
           count(*) filter (where activity = 'Cancel Subsequent Invoice')      as n_sub_cancel,
           count(*) filter (where activity = 'Vendor creates invoice')         as n_vendor_invoice,
           count(*) filter (where activity = 'Vendor creates debit memo')      as n_debit_memo,
           count(*) filter (where activity = 'Remove Payment Block')           as n_block_removed,
           count(*) filter (where activity = 'Change Approval for Purchase Order') as n_approval,
           min(day) filter (where activity = 'Create Purchase Order Item')     as po_day,
           min(day) filter (where activity = 'Record Goods Receipt')           as gr_day,
           min(day) filter (where activity = 'Record Invoice Receipt')         as ir_day,
           min(day) filter (where activity = 'Clear Invoice')                  as clear_day,
           min(day) filter (where activity = 'Remove Payment Block')           as block_day,
           min(day) filter (where activity = 'Vendor creates invoice'
                              and ts >= $vendor_floor::timestamp)              as vendor_invoice_day,
           min(ts)  filter (where activity = 'Record Invoice Receipt')         as first_ir_ts,
           min(ts)  filter (where activity = 'Change Approval for Purchase Order') as first_approval_ts
    from day_events
    group by case_id
) n using (case_id)
left join cleared_while_deleted d using (case_id);

-- Vendors whose items of one type on one document type never cleared in the cohort, with at
-- least $cluster_min of them. Choosing vendors by outcome makes these groups 0% in the cohort by
-- construction, so they show where uncleared items sit, not why. They're chosen on the cohort
-- alone so that later items at the same vendors are a fair check.
create table quiet_vendors as
select vendor, item_type, document_type,
       count(*) as cohort_items
from facts
where period = 'cohort' and item_type in ('Service', 'Subcontracting')
group by vendor, item_type, document_type
having count(*) >= $cluster_min::integer
   and count(*) filter (where n_clear > 0) = 0;

-- Items where one named account did both duties.
create table duties as
select e.case_id, e.activity, e.account, e.account_type,
       b.account is not null as in_burst
from events e
left join bursts b using (activity, account, ts)
where e.activity in ('Create Purchase Order Item', 'Record Goods Receipt',
                     'Record Invoice Receipt', 'Clear Invoice', 'Remove Payment Block');

create table duty_pairs as
select *
from (values ('sod_po_goods',     'Create Purchase Order Item', 'Record Goods Receipt'),
             ('sod_po_invoice',   'Create Purchase Order Item', 'Record Invoice Receipt'),
             ('sod_goods_invoice','Record Goods Receipt',       'Record Invoice Receipt'),
             ('sod_po_clear',     'Create Purchase Order Item', 'Clear Invoice'),
             ('sod_invoice_clear','Record Invoice Receipt',     'Clear Invoice'),
             ('sod_invoice_block','Record Invoice Receipt',     'Remove Payment Block'))
     as t(pair, first_duty, second_duty);

create table pair_items as
with both_duties as (
    select p.pair, a.case_id
    from duty_pairs p
    join (select distinct case_id, activity from duties) a on a.activity = p.first_duty
    join (select distinct case_id, activity from duties) b
      on b.activity = p.second_duty and b.case_id = a.case_id
),
same_account as (
    select p.pair, a.case_id, bool_or(a.in_burst or b.in_burst) as in_burst
    from duty_pairs p
    join duties a on a.activity = p.first_duty and a.account_type = 'named'
    join duties b on b.activity = p.second_duty and b.case_id = a.case_id
                 and b.account = a.account
    group by p.pair, a.case_id
)
select d.pair, d.case_id,
       s.case_id is not null        as same_account,
       coalesce(s.in_burst, false)  as in_burst
from both_duties d
left join same_account s using (pair, case_id);

-- Every rule as rows: one row per item the rule applies to.
--   flagged: the plain reading of the rule says this item fails
--   kept:    it still fails after removing what the rule's note in scripts/compliance.py
--            explains (usually an invoice that was reversed, so nothing stands to be paid)
create table rule_items as
with main_flow as (select * from facts where flow = '3-way match, invoice before GR'),
     after_flow as (select * from facts where flow = '3-way match, invoice after GR'),
     two_way as (select * from facts where flow = '2-way match')

-- Rules that should never fail if the system's controls work.
select 'after_invoice_before_goods' as rule, period, case_id, po_value, vendor, purchasing_document,
       ir_day < gr_day as flagged, ir_day < gr_day as kept
from after_flow where n_ir > 0 and n_gr > 0
union all
select 'after_cleared_no_goods', period, case_id, po_value, vendor, purchasing_document,
       net_goods <= 0 or clear_day < gr_day, net_goods <= 0 or clear_day < gr_day
from after_flow where n_clear > 0
union all
select 'goods_before_po', period, case_id, po_value, vendor, purchasing_document,
       gr_day < po_day, gr_day < po_day
from facts where n_gr > 0
union all
select 'invoice_recorded_before_po', period, case_id, po_value, vendor, purchasing_document,
       ir_day < po_day, ir_day < po_day
from facts where n_ir > 0
union all
select 'consignment_invoiced', period, case_id, po_value, vendor, purchasing_document,
       n_ir + n_sub + n_vendor_invoice + n_debit_memo + n_clear > 0,
       n_ir + n_sub + n_vendor_invoice + n_debit_memo + n_clear > 0
from facts where flow = 'Consignment'
union all
select 'two_way_goods', period, case_id, po_value, vendor, purchasing_document,
       n_gr > 0, n_gr > 0
from two_way
union all
select 'two_way_approved_after_invoice', period, case_id, po_value, vendor, purchasing_document,
       first_approval_ts > first_ir_ts, first_approval_ts > first_ir_ts
from two_way where n_ir > 0 and n_approval > 0

-- Rules that fail on some items.
union all
select 'cleared_no_goods', period, case_id, po_value, vendor, purchasing_document,
       net_goods <= 0, net_goods <= 0 and net_invoices > 0
from main_flow where n_clear > 0
union all
select 'cleared_before_goods', period, case_id, po_value, vendor, purchasing_document,
       clear_day < gr_day, clear_day < gr_day and net_invoices > 0
from main_flow where n_clear > 0 and n_gr > 0
union all
select 'block_removed_before_goods', period, case_id, po_value, vendor, purchasing_document,
       gr_day is null or block_day < gr_day,
       (gr_day is null or block_day < gr_day) and net_invoices > 0
from main_flow where n_block_removed > 0
union all
select 'cleared_before_invoice', period, case_id, po_value, vendor, purchasing_document,
       clear_day < ir_day, clear_day < ir_day and net_invoices > 0
from facts where n_clear > 0 and n_ir > 0
union all
select 'cleared_after_delete', period, case_id, po_value, vendor, purchasing_document,
       cleared_after_delete, cleared_after_delete and net_invoices > 0
from facts where n_clear > 0
union all
select 'extra_clearing', period, case_id, po_value, vendor, purchasing_document,
       n_clear > n_ir + n_sub, n_clear > n_ir + n_sub and not corrected
from facts where n_clear > 0
union all
select 'retro_po_three_way', period, case_id, po_value, vendor, purchasing_document,
       vendor_invoice_day < po_day, vendor_invoice_day < po_day - $retro_days::integer
from facts
where flow in ('3-way match, invoice before GR', '3-way match, invoice after GR')
  and vendor_invoice_day is not null
union all
select 'retro_po_two_way', period, case_id, po_value, vendor, purchasing_document,
       vendor_invoice_day < po_day, vendor_invoice_day < po_day - $retro_days::integer
from two_way where vendor_invoice_day is not null

-- Separation of duties. A bulk run isn't one person's decision, and an item with a cancellation
-- or debit memo may just be a correction, so the kept column drops those where it matters.
union all
select p.pair, f.period, f.case_id, f.po_value, f.vendor, f.purchasing_document,
       p.same_account,
       case p.pair
            when 'sod_po_goods'      then p.same_account and not p.in_burst
            when 'sod_invoice_clear' then p.same_account and not f.corrected
            when 'sod_invoice_block' then p.same_account and not f.corrected and not p.in_burst
            else p.same_account
       end
from pair_items p
join facts f using (case_id);
