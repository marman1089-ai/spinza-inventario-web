"""Reconcile each store's cash since its preceding physical count, in integer cents."""
import json
from bisect import bisect_left
from collections import defaultdict
from datetime import date,timedelta


def collection_windows(reports,receipts):
    daily=defaultdict(lambda:defaultdict(lambda:dict(cash=0,spent=0,authors=set())))
    for report in reports:
        lines=json.loads(report['lines_json'])
        if not any(line['name']=='Cash' for line in lines):continue
        day=str(report['closure_date'])[:10]
        row=daily[report['store']][day]
        row['cash']+=sum(int(line['cents']) for line in lines if line['name']=='Cash')
        row['spent']+=sum(int(line['cents']) for line in json.loads(report['expenses_json'] or '[]') if line.get('mode')!='cash_fund')
        if report.get('created_by'):row['authors'].add(report['created_by'])
    counts=defaultdict(dict)
    for receipt in receipts:counts[receipt['store']][str(receipt['day'])[:10]]=dict(receipt)
    result={}
    for store,store_counts in counts.items():
        dates=sorted(daily[store]);previous=None
        for day,receipt in sorted(store_counts.items()):
            end=(date.fromisoformat(day)-timedelta(days=1)).isoformat()
            start=previous or (dates[0] if dates and dates[0]<=end else end)
            first,last=bisect_left(dates,start),bisect_left(dates,day)
            covered=dates[first:last]
            cash=sum(daily[store][d]['cash'] for d in covered)
            spent=sum(daily[store][d]['spent'] for d in covered)
            expected=cash-spent if covered else None
            missing=(date.fromisoformat(end)-date.fromisoformat(start)).days+1-len(covered)
            found=receipt.get('amount_cents')
            authors=set().union(*(daily[store][d]['authors'] for d in covered)) if covered else set()
            result[(store,day)]=dict(receipt,day=day,report_start=start,report_end=end,report_day=end,
                report_days=len(covered),period_days=(date.fromisoformat(end)-date.fromisoformat(start)).days+1,
                missing_days=missing,declared=expected,cash=cash if covered else None,spent=spent if covered else None,
                difference=found-expected if found is not None and expected is not None and missing==0 else None,
                authors=', '.join(sorted(authors)))
            previous=day  # A recorded zero is a real boundary; an absent count is not.
    return result


class CollectionIndex:
    def __init__(self,windows):
        self.rows=defaultdict(list)
        for (store,day),row in sorted(windows.items()):self.rows[store].append(row)
        self.ends={store:[r['report_end'] for r in rows] for store,rows in self.rows.items()}
    def for_report(self,store,day):
        rows=self.rows[store];index=bisect_left(self.ends.get(store,[]),day)
        if index<len(rows) and rows[index]['report_start']<=day:return rows[index]
        return None
