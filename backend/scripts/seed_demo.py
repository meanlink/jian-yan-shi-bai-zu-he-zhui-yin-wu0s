import time, json
from sqlalchemy import select, func
from src.config.database import Session
from migrations.initial import migrate
from src.models import (User, Dataset, Job, Report, Delivery, Audit, Shift, Instrument,
                        Inspector, RecheckOrder)
from src.services.business import metadata, seed_records, analyze
from src.services.reports import deliver
from src.utils.security import hash_password
from src.utils.serialization import checksum
def seed():
    migrate()
    with Session() as db:
        for role,name in [('admin','林知 · 业务负责人'),('analyst','陈舟 · 分析员'),('viewer','周禾 · 观察员')]:
            email=role+'@demo.local'
            if not db.scalar(select(User).where(User.email==email)):
                db.add(User(email=email,name=name,role=role,password=hash_password('Demo123!')))
        db.flush()
        m=metadata()
        d=db.scalar(select(Dataset).where(Dataset.name==m['dataset_name']))
        if not d:
            rows=seed_records()
            d=Dataset(name=m['dataset_name'],source=m['short']+'-202611.csv',records=rows,checksum=checksum(rows),created_at=time.time())
            db.add(d); db.flush()
        if not db.scalar(select(Job).where(Job.name=='基线复核 · '+m['name'])):
            params={p['key']:p['default'] for p in m.get('parameters',[])}
            result=analyze(d.records,params)
            json.dumps(result,ensure_ascii=False,allow_nan=False)
            admin=db.scalar(select(User).where(User.role=='admin'))
            j=Job(name='基线复核 · '+m['name'],dataset_id=d.id,params=params,status='completed',result=result,created_by=admin.id,created_at=time.time()-3600,finished_at=time.time()-3595,approved=True)
            db.add(j);db.flush()
            r=Report(name='每日业务复核简报',job_id=j.id,destination='业务复核组 · 本地收件箱',interval_seconds=86400,next_run=time.time()+86400,enabled=True)
            db.add(r);db.flush();deliver(db,r)
            db.add(Audit(actor='seed',action='demo.baseline',detail={'dataset':d.id},at=time.time()))
        _seed_recheck(db, d)
        db.commit()
        counts={model.__tablename__:db.scalar(select(func.count()).select_from(model)) for model in [User,Dataset,Job,Report,Delivery,Audit,Shift,Instrument,Inspector,RecheckOrder]}
        print(json.dumps(counts,ensure_ascii=False))
        return counts
def _seed_recheck(db, dataset):
    from src.services.business import ITEMS
    now=time.time()
    shifts=[('早班',480,960,[0,1,2,3,4,5]),('中班',840,1320,[0,1,2,3,4]),('夜班',0,480,[0,1,2,3,4,5,6])]
    shift_by_name={}
    for name,start,end,days in shifts:
        shift=db.scalar(select(Shift).where(Shift.name==name))
        if not shift:
            shift=Shift(name=name,start_minute=start,end_minute=end,weekdays=days,active=True,created_at=now,updated_at=now)
            db.add(shift);db.flush()
        shift_by_name[name]=shift
    instruments=[
        ('HT-01','热稳定与黏度仪','早班',['heat_fail','viscosity_fail','ph_fail'],{'heat_fail':25,'viscosity_fail':15,'ph_fail':10},5,240),
        ('SP-02','灰分色差光谱仪','早班',['ash_fail','color_fail'],{'ash_fail':20,'color_fail':15},8,180),
        ('PH-03','通用理化台','中班',list(ITEMS),{'heat_fail':20,'ph_fail':10,'viscosity_fail':12,'ash_fail':25,'color_fail':10},5,300),
    ]
    for code,name,shift,cap,minutes,prep,validity in instruments:
        if not db.scalar(select(Instrument).where(Instrument.code==code)):
            db.add(Instrument(code=code,name=name,shift_id=shift_by_name[shift].id,capabilities=cap,
                              item_minutes=minutes,prep_minutes=prep,validity_minutes=validity,
                              active=True,version=1,updated_at=now))
    inspectors=[
        ('IN-01','吴珂','早班',['heat_fail','viscosity_fail','ph_fail']),
        ('IN-02','赵敏','早班',['ash_fail','color_fail','ph_fail']),
        ('IN-03','孙桐','中班',list(ITEMS)),
        ('IN-04','李澈','夜班',['heat_fail','viscosity_fail']),
    ]
    for code,name,shift,cap in inspectors:
        if not db.scalar(select(Inspector).where(Inspector.code==code)):
            db.add(Inspector(code=code,name=name,shift_id=shift_by_name[shift].id,capabilities=cap,
                             active=True,version=1,updated_at=now))
    db.flush()
    job=db.scalar(select(Job).where(Job.status=='completed').order_by(Job.id))
    if job and dataset and not db.scalar(select(RecheckOrder)):
        batch_ids=[str(r.get('batch_id')) for r in (dataset.records or [])[:6]]
        db.add(RecheckOrder(name='R2601 热稳定与黏度复检',job_id=job.id,dataset_id=dataset.id,
                            batches=batch_ids,items=['heat_fail','viscosity_fail'],status='pending',
                            block_reasons=[],created_by='seed',created_at=now,updated_at=now))


if __name__=='__main__':seed()
