-- Load the two snapshot files into typed tables, and build one row per purchase item.
-- Nothing is filtered or corrected here. Blank source values become NULL.

create table cases as
select *
from read_csv($cases, header = true, all_varchar = true);

create table events as
select case_id,
       seq::integer             as seq,
       activity,
       ts::timestamp            as ts,          -- UTC, to the minute
       "user"                   as account,
       net_worth_eur::double    as event_value,
       case when "user" like 'batch%' then 'batch'
            when "user" like 'user%'  then 'named'
            else 'none' end     as account_type
from read_csv($events, header = true, all_varchar = true);

-- Every item has exactly one 'Create Purchase Order Item' event, so its value gives one
-- figure per item. It's used as the item's value (see 3.1 in the source profile).
create table items as
select c.*,
       f.po_ts,
       f.po_value,
       f.n_events,
       f.n_clear,
       f.first_clear_ts,
       f.n_deleted
from cases c
join (
    select case_id,
           min(ts)          filter (where activity = 'Create Purchase Order Item') as po_ts,
           min(event_value) filter (where activity = 'Create Purchase Order Item') as po_value,
           count(*)                                                                as n_events,
           count(*)         filter (where activity = 'Clear Invoice')              as n_clear,
           min(ts)          filter (where activity = 'Clear Invoice')              as first_clear_ts,
           count(*)         filter (where activity in ('Delete Purchase Order Item',
                                                       'SRM: Deleted'))            as n_deleted
    from events
    group by case_id
) f using (case_id);
