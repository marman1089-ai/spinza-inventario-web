"""Transactional order confirmation and delivery, shared by the existing routes."""
import math
from fastapi import HTTPException
from fastapi.responses import RedirectResponse
from .db import connect, using_postgres
from .control import lock, suffix, number


def confirm_order(request,supplier,queue_ids,api):
    user=api['require_login'](request)
    if not user:
        return RedirectResponse('/',303)
    if not api['is_admin'](request):
        raise HTTPException(403,'Solo amministratore.')
    store=api['_effective_store'](request,user)
    p=api['_ph']()
    supplier=supplier.strip()
    if not supplier or not queue_ids:
        raise HTTPException(422,'Seleziona il fornitore e almeno una riga.')
    ids=sorted(set(int(i) for i in queue_ids))
    with connect() as conn:
        lock(conn)
        cur=conn.cursor()
        sql=f'SELECT * FROM order_queue WHERE id IN ({",".join([p]*len(ids))})'
        params=list(ids)
        if store!='ALL':
            sql+=f' AND store={p}'
            params.append(store)
        selected=cur.execute(sql+' ORDER BY id'+suffix(),tuple(params)).fetchall()
        if len(selected)!=len(ids):
            raise HTTPException(409,'Richieste già ordinate o non appartenenti al negozio selezionato. Ricarica il carrello.')
        batches={}
        for r in selected:
            product=cur.execute(f'SELECT * FROM products WHERE id={p} AND store={p}',(r['product_id'],r['store'])).fetchone()
            if not product or (product.get('supplier') or '').strip()!=supplier:
                raise HTTPException(409,'Prodotto eliminato o fornitore cambiato: aggiorna la preparazione ordini.')
            qty=number(r['qty_to_order'],nonnegative=True)
            if qty<=0:
                raise HTTPException(422,'Le quantità ordinate devono essere positive.')
            batches.setdefault(r['store'],[]).append((r,product,qty))
        for st,items in batches.items():
            cur.execute(f'INSERT INTO orders(store,supplier,status,created_by,kind) VALUES({p},{p},{p},{p},{p}) RETURNING id',
                        (st,supplier,'in_corso',user['username'],'ordine'))
            oid=cur.fetchone()['id']
            for r,product,qty in items:
                cur.execute(f'INSERT INTO order_lines(order_id,product_id,category,name,qty,area,unit) VALUES({",".join([p]*7)})',
                    (oid,product['id'],product['category'],product['name'],qty,product['area'],product.get('unit') or ''))
                cur.execute(f'DELETE FROM order_queue WHERE id={p}',(r['id'],))
            api['_log'](cur,store=st,username=user['username'],action='ORDER_ORDINATO',category=supplier,name=f'Ordine #{oid} ({len(items)} righe)',delta=len(items))
    return RedirectResponse('/ordinati',303)


async def receive_order(request,order_id,api):
    user=api['require_login'](request)
    if not user:
        return RedirectResponse('/',303)
    store=api['_effective_store'](request,user)
    p=api['_ph']()
    form=await request.form()
    try:
        missing={int(x) for x in form.getlist('missing_ids')}
    except ValueError:
        raise HTTPException(422,'Righe mancanti non valide.')
    with connect() as conn:
        lock(conn)
        cur=conn.cursor()
        sql=f"SELECT * FROM orders WHERE id={p} AND status='in_corso'"
        params=[order_id]
        if store!='ALL':
            sql+=f' AND store={p}'
            params.append(store)
        order=cur.execute(sql+suffix(),tuple(params)).fetchone()
        if not order:
            raise HTTPException(409,'Ordine già ricevuto o non disponibile nel negozio selezionato.')
        real_store=order['store']
        transfer=(order.get('kind') or 'ordine') in ('transfer','scambio')
        lines=cur.execute(f'SELECT * FROM order_lines WHERE order_id={p} ORDER BY id',(order_id,)).fetchall()
        if not lines:
            raise HTTPException(422,'Ordine senza righe: correggilo prima di confermare.')
        plans=[]
        reserved={}
        for line in lines:
            base=number(line['qty'],nonnegative=True)
            received=0.0 if line['id'] in missing else number(form.get(f'received_{line["id"]}'),nonnegative=True)
            if received>base+1e-8:
                raise HTTPException(422,f'{line["name"]}: ricevuto superiore all’ordinato. Verifica la quantità.')
            product=None
            if line.get('product_id'):
                product=cur.execute(f'SELECT * FROM products WHERE id={p} AND store={p}'+suffix(),(line['product_id'],real_store)).fetchone()
            area=line.get('area') or (product['area'] if product else 'prodotti')
            unit=line.get('unit') or (product.get('unit') if product else '') or ''
            src=None
            if transfer and received:
                src_store=order.get('from_store')
                if src_store not in api['STORES'] or real_store!=order.get('to_store') or src_store==real_store:
                    raise HTTPException(409,'Negozi dello scambio non validi.')
                if line.get('source_product_id'):
                    sources=cur.execute(f'SELECT * FROM products WHERE id={p} AND store={p}'+suffix(),(line['source_product_id'],src_store)).fetchall()
                else:
                    sources=cur.execute(f'SELECT * FROM products WHERE store={p} AND area={p} AND category={p} AND name={p} ORDER BY id'+suffix(),
                        (src_store,area,line['category'],line['name'])).fetchall()
                if len(sources)!=1:
                    raise HTTPException(409,f'{line["name"]}: posizione sorgente non univoca o eliminata. Modifica lo scambio e scegli il prodotto corretto.')
                src=sources[0]
                reserved[src['id']]=reserved.get(src['id'],0)+received
                if reserved[src['id']]>float(src['qty'])+1e-8:
                    raise HTTPException(409,f'{line["name"]}: giacenza insufficiente nel negozio mittente. Nessuna merce caricata.')
            if not product:
                product=cur.execute(f"SELECT * FROM products WHERE store={p} AND area={p} AND category={p} AND name={p} AND location='MAGAZZINO'"+suffix(),
                    (real_store,area,line['category'],line['name'])).fetchone()
            plans.append((line,base,received,product,area,unit,src))
        # Conditional claim prevents duplicate delivery, including concurrent submissions.
        cur.execute(f"UPDATE orders SET status='ricezione' WHERE id={p} AND status='in_corso'",(order_id,))
        if cur.rowcount!=1:
            raise HTTPException(409,'Consegna già confermata.')
        now=api['_now']()
        for line,base,received,product,area,unit,src in plans:
            remainder=max(0.0,base-received)
            if not product and (received or not transfer):
                cur.execute(f'''INSERT INTO products(store,category,name,area,location,unit,qty,min_qty,supplier)
                    VALUES({','.join([p]*9)}) ON CONFLICT(store,area,category,name,location)
                    DO UPDATE SET name=excluded.name RETURNING id''',
                    (real_store,line['category'],line['name'],area,'MAGAZZINO',unit,0,0,'' if transfer else order['supplier']))
                pid=cur.fetchone()['id']
                product=cur.execute(f'SELECT * FROM products WHERE id={p}',(pid,)).fetchone()
            if product:
                cur.execute(f'UPDATE order_lines SET product_id={p},area={p},unit={p} WHERE id={p}',(product['id'],area,unit,line['id']))
            cur.execute(f'UPDATE order_lines SET received_qty={p},is_missing={p} WHERE id={p}',(received,1 if remainder>1e-8 else 0,line['id']))
            if received and product:
                cur.execute(f'UPDATE products SET qty=qty+{p},updated_at={now} WHERE id={p}',(received,product['id']))
                api['_log'](cur,store=real_store,username=user['username'],action='TRANSFER_CARICATO_INVENTARIO' if transfer else 'ORDER_CARICATO_INVENTARIO',
                    category=line['category'],name=f'{line["name"]} | {product["location"]} | ordine #{order_id}',delta=received)
            if src and received:
                cur.execute(f'UPDATE products SET qty=qty-{p},updated_at={now} WHERE id={p} AND qty>={p}',(received,src['id'],received))
                if cur.rowcount!=1:
                    raise HTTPException(409,'Giacenza del mittente cambiata: riprova.')
                api['_log'](cur,store=src['store'],username=user['username'],action='TRANSFER_OUT_CONFERMATO',category=line['category'],
                    name=f'{line["name"]} | {src["location"]} → {real_store} | ordine #{order_id}',delta=-received)
            if remainder>1e-8:
                if not transfer and product:
                    cur.execute(f'UPDATE products SET missing_order_date={p},missing_delivery_date={p},missing_qty={p} WHERE id={p}',
                        (str(order.get('ts') or '')[:10],api['_today_str'](),remainder,product['id']))
                    queue=cur.execute(f'SELECT id FROM order_queue WHERE store={p} AND product_id={p} ORDER BY id'+suffix(),(real_store,product['id'])).fetchone()
                    if queue:
                        cur.execute(f'UPDATE order_queue SET qty_to_order=qty_to_order+{p},added_by={p},ts={now} WHERE id={p}',(remainder,user['username'],queue['id']))
                    else:
                        cur.execute(f'INSERT INTO order_queue(store,product_id,category,name,qty_to_order,added_by) VALUES({",".join([p]*6)})',
                            (real_store,product['id'],line['category'],line['name'],remainder,user['username']))
                api['_log'](cur,store=real_store,username=user['username'],action='TRANSFER_MANCANTE' if transfer else 'ORDER_MANCANTE',
                    category=line['category'],name=f'{line["name"]} | ordine #{order_id}: ricevuti {received:g} di {base:g}',delta=remainder)
            elif product and not transfer:
                cur.execute(f'UPDATE products SET missing_qty=0,missing_order_date=NULL,missing_delivery_date=NULL WHERE id={p}',(product['id'],))
        cur.execute(f"UPDATE orders SET status='chiuso',closed_at={now} WHERE id={p}",(order_id,))
        api['_log'](cur,store=real_store,username=user['username'],action='TRANSFER_ARRIVATO' if transfer else 'ORDER_ARRIVATO',
                    category=order['supplier'],name=f'Ordine #{order_id} ricevuto e archiviato',delta=0)
    return RedirectResponse('/ordinati',303)
