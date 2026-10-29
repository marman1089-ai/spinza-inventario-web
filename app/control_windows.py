"""Visible activity windows combine timed samples, server accesses and historical logs."""
from datetime import datetime
from hashlib import sha256
from .inventory_analytics import build_reports, duration
from .inventory_activity import ROME


def add_control_windows(people, journal, lo, hi, selected, now):
    by_name={(p['name'],p['store']):key for key,p in people.items()}
    grouped={}
    for event in journal:
        name_key=(event['username'],event['store'])
        key=(int(event['user_id']),event['store']) if event['source']=='request' else by_name.get(name_key)
        if key is None:
            uid=-int(sha256(event['username'].encode()).hexdigest()[:12],16)
            key=(uid,event['store'])
        if key not in people:
            row={'name':event['username'],'store':event['store'],'active':False,'bars':[],'sessions':[],'current_start':'—','current_duration':'0 min','first':'—','last':'—','minutes':0}
            row.update(build_reports([],selected));people[key]=row
        by_name[name_key]=key
        grouped.setdefault(key,[]).append(event)
    for key,row in people.items():
        row['user_id']=key[0]
        records=grouped.get(key,[])
        row['access_count']=sum(e['source']=='request' and e['kind']=='access' for e in records)
        row['action_count']=sum(e['source']=='request' and e['kind']=='action' and e['status']<400 for e in records)
        row['error_count']=sum(e['source']=='request' and e['status']>=400 for e in records)
        row['log_count']=sum(e['source']=='log' for e in records)
        requests=[e for e in records if e['source']=='request']
        last_logout=max((float(e['happened']) for e in requests if e['kind']=='logout'),default=0)
        recent=lo<=now<hi and any(e['kind']!='logout' and float(e['happened'])>last_logout and 0<=now-float(e['happened'])<=30 for e in requests)
        row['active']=(row['active'] and row.get('_presence_last',0)>last_logout) or recent
        # Preserve the measured totals. Windows bridge <=90s only for navigation/detail.
        samples=[]
        # The original bars use wall-clock positions; source sessions expose their epochs below.
        for interval in row.get('measured_windows',[]): samples.append(list(interval))
        samples += [[float(e['happened']),float(e['happened'])] for e in records]
        windows=[]
        for a,b in sorted(samples):
            if windows and a-windows[-1][1]<=90: windows[-1][1]=max(b,windows[-1][1])
            else: windows.append([a,b])
        row['sessions']=[];row['bars']=[]
        for a,b in windows:
            matched=[e for e in records if a<=float(e['happened'])<=b]
            visits=sum(e['source']=='request' and e['kind']=='access' for e in matched)
            actions=sum(e['source']=='request' and e['kind']=='action' for e in matched)
            errors=sum(e['source']=='request' and e['status']>=400 for e in matched)
            logs=sum(e['source']=='log' for e in matched)
            start=datetime.fromtimestamp(a,ROME).strftime('%H:%M:%S %Z');end=datetime.fromtimestamp(b,ROME).strftime('%H:%M:%S %Z')
            label=f'{start} – {end} · {visits} accessi · {actions} operazioni · {logs} movimenti · {errors} errori. Clicca per i dettagli.'
            session={'start':start,'end':end,'duration':duration(b-a) if b>a else 'Ingresso istantaneo','from':a,'to':b,'label':label,'accesses':visits,'actions':actions,'logs':logs,'errors':errors}
            row['sessions'].append(session)
            # Split around offset changes so DST remains correctly labelled.
            chunks=[];cursor=a
            if a==b: chunks=[[a,b]]
            while cursor<b:
                stop=min(b,(int(cursor)//60+1)*60)
                if chunks and datetime.fromtimestamp(chunks[-1][0],ROME).utcoffset()==datetime.fromtimestamp(cursor,ROME).utcoffset():chunks[-1][1]=stop
                else:chunks.append([cursor,stop])
                cursor=stop
            for start_at,end_at in chunks:
                local=datetime.fromtimestamp(start_at,ROME)
                minute=local.hour*60+local.minute+local.second/60
                row['bars'].append({'left':min(99.8,minute/1440*100),'width':(end_at-start_at)/86400*100,'label':label,'from':a,'to':b,'instant':a==b,'errors':errors})
        if windows:
            row['first']=datetime.fromtimestamp(windows[0][0],ROME).strftime('%H:%M:%S')
            row['last']=datetime.fromtimestamp(windows[-1][1],ROME).strftime('%H:%M:%S')
            if row['active']:
                row['current_start']=datetime.fromtimestamp(windows[-1][0],ROME).strftime('%H:%M:%S')
