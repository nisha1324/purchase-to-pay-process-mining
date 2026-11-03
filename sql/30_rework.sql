-- Working tables for the rework and automation analysis. Built on top of 10_item_facts.sql.
--
-- Only the two 3-way flows in the cohort are covered, as in the bottleneck part: consignment has
-- no invoices, and the 2-way flow is one login whose approval steps are part of its design.

-- A UTC timestamp's Dutch calendar day, as in day_events.
create macro dutch_day(ts) as timezone('Europe/Amsterdam', timezone('UTC', ts))::date;

-- What each activity is. Core steps are the work the process needs. Rework undoes or redoes
-- something already done. Approval and blocks are controls, kept apart so they aren't counted as
-- mistakes. Anything not listed here (the SRM workflow steps) is 'Other'.
create table activity_kinds as
select *
from (values
    ('Create Purchase Requisition Item',  'Core',    'Requisition'),
    ('Release Purchase Requisition',      'Core',    'Requisition'),
    ('Create Purchase Order Item',        'Core',    'PO creation'),
    ('Receive Order Confirmation',        'Core',    'Order confirmation'),
    ('Record Goods Receipt',              'Core',    'Goods receipt'),
    ('Record Service Entry Sheet',        'Core',    'Service entry sheet'),
    ('Vendor creates invoice',            'Core',    'Vendor invoice'),
    ('Record Invoice Receipt',            'Core',    'Invoice recording'),
    ('Record Subsequent Invoice',         'Core',    'Invoice recording'),
    ('Clear Invoice',                     'Core',    'Clearing'),
    ('Change Approval for Purchase Order','Control', 'PO approval'),
    ('Release Purchase Order',            'Control', 'PO approval'),
    ('Remove Payment Block',              'Control', 'Payment block'),
    ('Set Payment Block',                 'Control', 'Payment block'),
    ('Change Price',                      'Rework',  'Price change'),
    ('Change Quantity',                   'Rework',  'Quantity change'),
    ('Change Delivery Indicator',         'Rework',  'Other PO change'),
    ('Change Storage Location',           'Rework',  'Other PO change'),
    ('Change Currency',                   'Rework',  'Other PO change'),
    ('Change payment term',               'Rework',  'Other PO change'),
    ('Change Final Invoice Indicator',    'Rework',  'Other PO change'),
    ('Change Rejection Indicator',        'Rework',  'Other PO change'),
    ('Update Order Confirmation',         'Rework',  'Other PO change'),
    ('Delete Purchase Order Item',        'Rework',  'Deletion or block'),
    ('SRM: Deleted',                      'Rework',  'Deletion or block'),
    ('Block Purchase Order Item',         'Rework',  'Deletion or block'),
    ('Reactivate Purchase Order Item',    'Rework',  'Deletion or block'),
    ('Cancel Invoice Receipt',            'Rework',  'Invoice reversal'),
    ('Cancel Subsequent Invoice',         'Rework',  'Invoice reversal'),
    ('Cancel Goods Receipt',              'Rework',  'Goods reversal'),
    ('Vendor creates debit memo',         'Rework',  'Vendor debit memo')
) as t(activity, kind, step);

-- Every event on a 3-way cohort item, with what it is and how it was done:
--   System         a batch account
--   Vendor or none no account: the vendor's own date, or an unattributed step
--   Bulk           a named account in a burst: 50+ of the same thing in one minute (Part 1's rule)
--   One at a time  any other named-account event
create table work_events as
select e.case_id, e.seq, e.activity, e.ts, e.day, e.account, f.purchasing_document, f.vendor,
       f.spend_area, f.flow, f.po_value, f.first_ir_ts,
       coalesce(k.kind, 'Other')                      as kind,
       coalesce(k.step, 'Other')                      as step,
       case when e.account_type = 'batch' then 'System'
            when e.account_type = 'none'  then 'Vendor or none'
            when b.account is not null    then 'Bulk'
            else 'One at a time' end                  as mode
from day_events e
join facts f using (case_id)
left join activity_kinds k using (activity)
left join bursts b using (activity, account, ts)
where f.period = 'cohort'
  and f.flow in ('3-way match, invoice before GR', '3-way match, invoice after GR');

-- One-at-a-time work as touches: one account handling one document in one minute counts once,
-- however many lines it holds, because a 10-line PO entered at once is one piece of work. A touch
-- belongs to the spend area of its lowest-numbered line (a document can mix spend areas; its vendor
-- is always one).
create table touches as
select activity, account, purchasing_document, ts,
       any_value(kind)                 as kind,
       any_value(step)                 as step,
       any_value(vendor)               as vendor,
       arg_min(spend_area, case_id)    as spend_area,
       count(*)                        as n_lines
from work_events
where mode = 'One at a time'
group by activity, account, purchasing_document, ts;

-- One row per 3-way cohort item: which kinds of rework it had, and what they led to.
create table item_rework as
with flags as (
    select case_id,
           bool_or(step = 'Price change')                                     as price_change,
           bool_or(step = 'Price change' and ts >= first_ir_ts)               as price_after_invoice,
           bool_or(step = 'Quantity change')                                  as quantity_change,
           bool_or(step = 'Quantity change' and ts >= first_ir_ts)            as quantity_after_invoice,
           bool_or(step = 'Other PO change')                                  as other_change,
           bool_or(step = 'Deletion or block')                                as deletion,
           bool_or(step = 'Invoice reversal')                                 as invoice_reversal,
           bool_or(step = 'Goods reversal')                                   as goods_reversal,
           bool_or(step = 'Vendor debit memo')                                as debit_memo,
           bool_or(kind = 'Rework')                                           as any_rework,
           min(ts) filter (where step = 'Price change' and ts >= first_ir_ts) as first_late_price_ts,
           min(ts) filter (where activity = 'Cancel Invoice Receipt')         as first_ir_cancel_ts,
           min(ts) filter (where activity = 'Cancel Goods Receipt')           as first_gr_cancel_ts,
           min(day) filter (where activity in ('Delete Purchase Order Item', 'SRM: Deleted'))
                                                                              as delete_day,
           count(*) filter (where kind = 'Rework' and mode = 'One at a time') as manual_rework_events
    from work_events
    group by case_id
),
after_marks as (
    -- What happened after the first late price change and the first cancellations.
    select w.case_id,
           min(w.ts) filter (where w.activity = 'Remove Payment Block'
                               and w.ts >= g.first_late_price_ts)            as block_after_price_ts,
           bool_or(w.activity = 'Record Invoice Receipt' and w.ts > g.first_ir_cancel_ts)
                                                                              as invoice_reentered,
           bool_or(w.activity = 'Record Goods Receipt' and w.ts > g.first_gr_cancel_ts)
                                                                              as goods_reentered
    from work_events w
    join flags g using (case_id)
    group by w.case_id
)
select f.case_id, f.flow, f.vendor, f.spend_area, f.po_value, f.po_day, f.n_gr, f.n_ir, f.n_clear,
       f.n_block_removed, f.corrected,
       g.price_change, coalesce(g.price_after_invoice, false)       as price_after_invoice,
       g.quantity_change, coalesce(g.quantity_after_invoice, false) as quantity_after_invoice,
       g.other_change, g.deletion, g.invoice_reversal, g.goods_reversal, g.debit_memo, g.any_rework,
       g.first_late_price_ts, a.block_after_price_ts,
       g.first_ir_cancel_ts is not null                             as invoice_cancelled,
       coalesce(a.invoice_reentered, false)                         as invoice_reentered,
       g.first_gr_cancel_ts is not null                             as goods_cancelled,
       coalesce(a.goods_reentered, false)                           as goods_reentered,
       g.delete_day,
       g.manual_rework_events
from facts f
join flags g using (case_id)
join after_marks a using (case_id)
where f.period = 'cohort'
  and f.flow in ('3-way match, invoice before GR', '3-way match, invoice after GR');
