-- The queries behind results/COMPLIANCE.md. Each one starts with a "-- name:" line.
-- Needs the tables in 10_item_facts.sql. Everything is counted on items, never on events.

-- name: rule_totals
with counted as (
    select rule, period,
           count(*)                                   as population,
           count(*) filter (where flagged)            as flagged,
           count(*) filter (where kept)               as kept,
           count(distinct purchasing_document) filter (where kept) as documents,
           count(distinct vendor) filter (where kept) as vendors,
           sum(po_value)                              as population_value,
           coalesce(sum(po_value) filter (where kept), 0) as kept_value
    from rule_items
    where period is not null
    group by rule, period
),
by_vendor as (
    select rule, period, vendor, count(*) as n,
           row_number() over (partition by rule, period order by count(*) desc, vendor) as place
    from rule_items
    where kept and period is not null
    group by rule, period, vendor
)
select c.*, coalesce(v.n, 0) as top_vendor_items
from counted c
left join by_vendor v on v.rule = c.rule and v.period = c.period and v.place = 1
order by c.rule, c.period;

-- name: cohort_flows
select flow, count(*) as n_items, sum(po_value) as po_value
from facts
where period = 'cohort'
group by flow
order by n_items desc;

-- name: reversal_mix
-- What the items flagged by 'cleared_no_goods' really are.
select case when not r.kept and f.n_gr = 0 then 'Goods never recorded; invoice reversed too'
            when not r.kept                then 'Goods cancelled; invoice reversed too'
            when f.n_gr = 0                then 'Goods never recorded; invoice stands'
            else                                'Goods cancelled; invoice stands'
       end                                                  as kind,
       count(*)                                             as n_items,
       count(*) filter (where f.n_debit_memo > 0)           as with_debit_memo
from rule_items r
join facts f using (case_id)
where r.rule = 'cleared_no_goods' and r.period = 'cohort' and r.flagged
group by kind
order by n_items desc;

-- name: retro_gaps
-- Days from the vendor's invoice date to the PO, by flow.
select flow,
       count(*)                                                  as invoiced,
       count(*) filter (where vendor_invoice_day < po_day)       as before_po,
       count(*) filter (where vendor_invoice_day < po_day - 7)   as over_7,
       count(*) filter (where vendor_invoice_day < po_day - 30)  as over_30,
       median(po_day - vendor_invoice_day)
           filter (where vendor_invoice_day < po_day)            as median_gap
from facts
where period = 'cohort' and vendor_invoice_day is not null
group by flow
order by invoiced desc;

-- name: pair_accounts
-- How many accounts sit behind each separation-of-duties rule, and how much is one account.
with kept as (
    select r.rule, r.case_id
    from rule_items r
    where r.period = 'cohort' and r.kept and r.rule like 'sod_%'
),
per_account as (
    select k.rule, a.account, count(distinct k.case_id) as n
    from kept k
    join duty_pairs p on p.pair = k.rule
    join duties a on a.case_id = k.case_id and a.activity = p.first_duty
                 and a.account_type = 'named'
    join duties b on b.case_id = k.case_id and b.activity = p.second_duty
                 and b.account = a.account
    group by k.rule, a.account
)
select rule, count(*) as accounts, max(n) as top_account_items,
       arg_max(account, n) as top_account
from per_account
group by rule
order by rule;

-- name: role_overlap
-- Named accounts doing each duty on cohort items, and how many do both.
with roles as (
    select distinct d.activity, d.account
    from duties d
    join facts f using (case_id)
    where f.period = 'cohort' and d.account_type = 'named'
)
select p.pair,
       (select count(*) from roles where activity = p.first_duty)  as first_accounts,
       (select count(*) from roles where activity = p.second_duty) as second_accounts,
       (select count(*) from roles a join roles b using (account)
         where a.activity = p.first_duty and b.activity = p.second_duty) as in_both
from duty_pairs p
order by p.pair;

-- name: segments
-- Where the items that never cleared sit. A vendor is in a "never" group only if it is in
-- quiet_vendors, so those groups are 0% by construction (see the note on that table).
with labelled as (
    select f.*,
           q.vendor is not null as quiet,
           case when f.flow = '2-way match' and f.sub_spend_area = 'Government payments'
                    then '2-way: government payments'
                when f.flow = '2-way match'
                    then '2-way: everything else'
                when q.vendor is not null and f.item_type = 'Service'
                    then 'Invoice after GR: services, vendors never invoiced'
                when q.vendor is not null and f.flow like '%after GR'
                    then 'Invoice after GR: subcontractors never cleared'
                when q.vendor is not null
                    then 'Main flow: subcontractors never cleared'
                when f.flow like '%after GR' and f.item_type = 'Service'
                    then 'Invoice after GR: other services'
                when f.flow like '%after GR'
                    then 'Invoice after GR: goods'
                else 'Main flow: everything else'
           end as segment
    from facts f
    left join quiet_vendors q
           on q.vendor = f.vendor and q.item_type = f.item_type
          and q.document_type = f.document_type
    where f.period = 'cohort' and f.flow <> 'Consignment'
)
select segment,
       count(*)                                                     as n_items,
       count(distinct vendor)                                       as vendors,
       sum(po_value)                                                as value,
       count(*) filter (where n_clear > 0)                          as cleared,
       count(*) filter (where n_clear = 0 and n_deleted > 0)        as deleted,
       count(*) filter (where n_clear = 0 and n_deleted = 0
                          and net_invoices > 0)                     as invoice_open,
       count(*) filter (where n_clear = 0 and n_deleted = 0
                          and net_invoices <= 0)                    as not_invoiced
from labelled
group by segment
order by segment;

-- name: flow_gap
-- The clearing gap before and after setting the quiet vendors and government payments aside.
-- sub_spend_area can be blank, hence "is distinct from".
select f.flow,
       count(*)                                    as n_items,
       count(*) filter (where f.n_clear > 0)       as cleared,
       count(*) filter (where q.vendor is null
                          and f.sub_spend_area is distinct from 'Government payments') as rest,
       count(*) filter (where q.vendor is null and f.n_clear > 0
                          and f.sub_spend_area is distinct from 'Government payments') as rest_cleared
from facts f
left join quiet_vendors q
       on q.vendor = f.vendor and q.item_type = f.item_type
      and q.document_type = f.document_type
where f.period = 'cohort' and f.flow <> 'Consignment'
group by f.flow
order by n_items desc;

-- name: quiet_vendor_groups
select item_type, document_type,
       count(*)              as vendors,
       sum(cohort_items)     as cohort_items
from quiet_vendors
group by item_type, document_type
order by cohort_items desc;

-- name: quiet_invoice_accounts
-- Who records the invoices on subcontractors that never clear.
select e.account_type,
       count(distinct e.case_id) as n_items
from facts f
join quiet_vendors q
  on q.vendor = f.vendor and q.item_type = f.item_type and q.document_type = f.document_type
join events e using (case_id)
where f.period = 'cohort' and f.item_type = 'Subcontracting'
  and e.activity = 'Record Invoice Receipt'
group by e.account_type
order by n_items desc;

-- name: quiet_service_shape
-- Service items that were received but never invoiced, at vendors that never invoice.
with sheets as (
    select case_id, count(*) as n_sheets,
           date_trunc('month', min(ts)) as first_month,
           date_trunc('month', max(ts)) as last_month
    from events
    where activity = 'Record Service Entry Sheet'
    group by case_id
)
select count(*)                                             as n_items,
       count(s.case_id)                                     as with_sheets,
       median(s.n_sheets)                                   as median_sheets,
       count(*) filter (where s.first_month = s.last_month) as one_month,
       count(*) filter (where f.spend_area = 'Logistics')   as logistics
from facts f
join quiet_vendors q
  on q.vendor = f.vendor and q.item_type = f.item_type and q.document_type = f.document_type
left join sheets s using (case_id)
where f.period = 'cohort' and f.item_type = 'Service';

-- name: government
-- 2-way items in the cohort: government payments against the rest.
with po as (
    select case_id, account from events where activity = 'Create Purchase Order Item'
)
select f.sub_spend_area = 'Government payments'                  as government,
       count(*)                                                  as n_items,
       count(distinct f.vendor)                                  as vendors,
       count(distinct f.purchasing_document)                     as documents,
       sum(f.po_value)                                           as value,
       count(*) filter (where f.n_clear > 0)                     as cleared,
       count(*) filter (where f.n_clear = 0 and f.net_invoices > 0) as invoice_open,
       count(distinct p.account)                                 as accounts,
       max(po_day)                                               as last_po_day,
       mode(p.account)                                           as top_account,
       count(*) filter (where p.account = (select mode(account) from po join facts g using (case_id)
                                            where g.flow = '2-way match' and g.period = 'cohort'
                                              and g.sub_spend_area = 'Government payments')) as top_account_items,
       count(*) filter (where f.ir_day between date '2018-03-01' and date '2018-04-30') as invoiced_mar_apr,
       count(*) filter (where f.vendor_invoice_day < f.po_day)   as dated_before_po,
       count(*) filter (where f.vendor_invoice_day is not null)  as with_vendor_date
from facts f
join po p using (case_id)
where f.flow = '2-way match' and f.period = 'cohort'
group by government
order by government desc;

-- name: oldest_open_two_way
select count(*)                                                  as n_items,
       min(first_ir_ts)                                          as first_invoice,
       max(first_ir_ts)                                          as last_invoice,
       median(date_diff('day', first_ir_ts, $window_end::timestamp)) as median_days_open
from facts
where flow = '2-way match' and period = 'cohort'
  and n_clear = 0 and n_deleted = 0 and net_invoices > 0;

-- name: quiet_out_of_sample
-- The same vendors on later items. Period NULL is October onwards, which is too recent to read.
select f.item_type,
       f.period,
       count(distinct f.vendor)              as vendors,
       count(*)                              as n_items,
       count(*) filter (where f.n_ir > 0)    as invoiced,
       count(*) filter (where f.n_clear > 0) as cleared
from facts f
join quiet_vendors q
  on q.vendor = f.vendor and q.item_type = f.item_type and q.document_type = f.document_type
where f.period is not null
group by f.item_type, f.period
order by f.item_type, f.period;

-- name: subcontract_timing
-- Is "never cleared" just the log stopping? Compare these invoices' age with how long other
-- subcontracting invoices took to clear.
with quiet as (
    select f.*
    from facts f
    join quiet_vendors q
      on q.vendor = f.vendor and q.item_type = f.item_type and q.document_type = f.document_type
    where f.period = 'cohort' and f.item_type = 'Subcontracting' and f.n_ir > 0
),
others as (
    select date_diff('day', f.first_ir_ts, f.clear_day::timestamp) as days
    from facts f
    left join quiet_vendors q
      on q.vendor = f.vendor and q.item_type = f.item_type and q.document_type = f.document_type
    where f.period = 'cohort' and f.item_type = 'Subcontracting' and q.vendor is null
      and f.n_clear > 0 and f.n_ir > 0
)
select (select count(*) from quiet)                                            as invoiced,
       (select count(*) from quiet where n_clear > 0)                          as cleared,
       (select median(date_diff('day', po_ts, first_ir_ts)) from quiet)        as median_po_to_invoice,
       (select quantile_cont(date_diff('day', po_ts, first_ir_ts), 0.05) from quiet) as p05_po_to_invoice,
       (select quantile_cont(date_diff('day', po_ts, first_ir_ts), 0.95) from quiet) as p95_po_to_invoice,
       (select count(*) from others)                                           as other_cleared,
       (select median(days) from others)                                       as other_median,
       (select quantile_cont(days, 0.9) from others)                           as other_p90,
       (select count(*) from quiet
         where date_diff('day', first_ir_ts, $window_end::timestamp)
               > (select quantile_cont(days, 0.9) from others))                as older_than_p90;

-- name: delete_mix
-- Which items were cleared after a deletion flag, and how far apart the two were.
with kept as (
    select f.*
    from rule_items r
    join facts f using (case_id)
    where r.rule = 'cleared_after_delete' and r.period = 'cohort' and r.kept
)
select document_type,
       count(*)      as n_items,
       sum(po_value) as value
from kept
group by document_type
order by value desc;

-- name: two_way_log
-- The 2-way flow across the whole log, not just the cohort.
select (select count(*) from facts where flow = '2-way match')                 as n_items,
       (select count(*) from items
         where item_category = '2-way match' and company = 'companyID_0003')   as in_company,
       (select count(*) from items where company = 'companyID_0003')           as company_items,
       (select count(distinct e.account)
          from events e join facts f using (case_id)
         where f.flow = '2-way match'
           and e.activity = 'Create Purchase Order Item')                      as po_accounts,
       (select count(*) from facts where flow = '2-way match'
           and n_approval > 0)                                                 as with_approval;

-- name: review_list
-- Every cohort item any review rule still flags, once, with how many rules it fails.
select case_id, purchasing_document, vendor, po_value,
       count(*)                       as rules_failed,
       string_agg(rule, ' ' order by rule) as rules
from rule_items
where period = 'cohort' and kept
group by case_id, purchasing_document, vendor, po_value
order by rules_failed desc, po_value desc, case_id;

-- name: cohort_value
select count(*) as n_items, sum(po_value) as value from facts where period = 'cohort';
