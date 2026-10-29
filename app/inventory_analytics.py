"""Calendar comparisons use Rome days and unioned activity intervals, including zero days."""
from datetime import timedelta, datetime
from .inventory_activity import day_bounds, merge_intervals, ROME

def duration(seconds):
    if 0 < seconds < 60: return '<1 min'
    minutes=int(seconds//60)
    return (f'{minutes//60} h {minutes%60:02d} min' if minutes>=60 else f'{minutes} min')

def delta(current, baseline):
    return round((current-baseline)/baseline*100,1) if baseline>0 else None

def build_reports(intervals, selected):
    intervals=merge_intervals(intervals)
    def seconds(start,end):
        lo=day_bounds(start)[0];hi=day_bounds(end)[1]
        return sum(max(0,min(b,hi)-max(a,lo)) for a,b in intervals)
    def daily(day):
        value=seconds(day,day)
        average=sum(seconds(day-timedelta(days=i),day-timedelta(days=i)) for i in range(1,8))/7
        return {'from':day.isoformat(),'to':day.isoformat(),'seconds':value,'duration':duration(value),
                'base':average,'base_duration':duration(average),'delta':delta(value,average)}
    days=[daily(selected-timedelta(days=i)) for i in range(7)]
    monday=selected-timedelta(days=selected.weekday())
    weeks=[]
    for i in range(4):
        start=monday-timedelta(weeks=i)
        end=selected if i==0 else start+timedelta(days=6)
        value=seconds(start,end)
        previous=seconds(start-timedelta(days=7),end-timedelta(days=7))
        weeks.append({'from':start.isoformat(),'to':end.isoformat(),'seconds':value,'duration':duration(value),
                      'base':previous,'base_duration':duration(previous),'delta':delta(value,previous)})
    return {'daily':days[0],'week':weeks[0],'days':days,'weeks':weeks}
