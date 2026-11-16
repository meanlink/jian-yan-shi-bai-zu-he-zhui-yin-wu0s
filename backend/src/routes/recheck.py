from fastapi import APIRouter, Depends, HTTPException, Query
from sqlalchemy import select
from src.config.database import get_db
from src.models import (Downtime, Inspector, Instrument, RecheckClaim, RecheckOrder,
                        RecheckPreview, RecheckRevision, RecheckSchedule, Shift)
from src.schemas.recheck import (ClaimInput, ClaimStartInput, ConfirmInput, DisposeInput,
                                 DowntimeInput, InspectorInput, InspectorPatch, InstrumentInput,
                                 InstrumentPatch, OrderInput, ShiftInput)
from src.middlewares.auth import current_user, can_write, admin_only
from src.services import scheduling
from src.services.audit import record
from src.services.business import ITEMS, NAMES
import time

router = APIRouter(prefix='/recheck')


def _enum_check(values, allowed, label):
    bad = [v for v in values if v not in allowed]
    if bad:
        raise HTTPException(422, f'{label}能力包含未知检验项：' + '、'.join(bad))


# ---------------------------------------------------------------- 元数据
@router.get('/catalog')
def catalog(db=Depends(get_db), u=Depends(current_user)):
    return {
        'timezone_offset_minutes': scheduling.RECHECK_TZ_OFFSET_MINUTES,
        'preview_ttl_seconds': scheduling.RECHECK_PREVIEW_TTL_SECONDS,
        'horizon_days': scheduling.RECHECK_HORIZON_DAYS,
        'items': [{'key': i, 'label': NAMES[i]} for i in ITEMS],
        'statuses': ['pending', 'previewed', 'scheduled', 'in_progress', 'completed',
                     'blocked', 'abnormal', 'cancelled'],
        'resource_types': ['instrument', 'inspector'],
    }


@router.get('/shifts')
def shifts(db=Depends(get_db), u=Depends(current_user)):
    return [{'id': s.id, 'name': s.name, 'start_minute': s.start_minute, 'end_minute': s.end_minute,
             'weekdays': s.weekdays or [], 'active': s.active} for s in
            db.scalars(select(Shift).order_by(Shift.start_minute, Shift.id))]


@router.post('/shifts', status_code=201)
def create_shift(data: ShiftInput, db=Depends(get_db), u=Depends(admin_only)):
    if data.end_minute <= data.start_minute:
        raise HTTPException(422, '班次结束时间必须晚于开始时间')
    if any(d < 0 or d > 6 for d in data.weekdays) or not data.weekdays:
        raise HTTPException(422, '班次至少需要一个星期取值（0=周一 … 6=周日）')
    if db.scalar(select(Shift).where(Shift.name == data.name.strip())):
        raise HTTPException(409, '班次名称已存在')
    now = time.time()
    shift = Shift(name=data.name.strip(), start_minute=data.start_minute, end_minute=data.end_minute,
                  weekdays=sorted(set(data.weekdays)), active=data.active, created_at=now, updated_at=now)
    db.add(shift); db.flush()
    record(db, u.email, 'recheck.shift.create', {'id': shift.id, 'name': shift.name})
    db.commit()
    return {'id': shift.id, 'name': shift.name, 'start_minute': shift.start_minute,
            'end_minute': shift.end_minute, 'weekdays': shift.weekdays, 'active': shift.active}


# ---------------------------------------------------------------- 资源维护
def _validate_capabilities(values):
    _enum_check(values, ITEMS, '资源')


def _resource_impact(db, resource_type, resource_id, actor):
    return scheduling.resource_impact(db, resource_type, resource_id, actor)


def _operational(changes):
    return any(k in changes for k in ('capabilities', 'shift_id', 'item_minutes',
                                      'prep_minutes', 'validity_minutes', 'active'))


def _instrument_payload(i, shifts_map):
    return {'id': i.id, 'code': i.code, 'name': i.name, 'shift_id': i.shift_id,
            'shift': shifts_map.get(i.shift_id), 'capabilities': i.capabilities or [],
            'item_labels': [NAMES.get(c, c) for c in (i.capabilities or [])],
            'item_minutes': i.item_minutes or {}, 'prep_minutes': i.prep_minutes,
            'validity_minutes': i.validity_minutes, 'active': i.active, 'version': i.version,
            'updated_at': i.updated_at}


@router.get('/instruments')
def list_instruments(db=Depends(get_db), u=Depends(current_user)):
    shifts_map = {s.id: {'id': s.id, 'name': s.name} for s in db.scalars(select(Shift))}
    return [_instrument_payload(i, shifts_map) for i in
            db.scalars(select(Instrument).order_by(Instrument.id))]


@router.post('/instruments', status_code=201)
def create_instrument(data: InstrumentInput, db=Depends(get_db), u=Depends(admin_only)):
    _validate_capabilities(data.capabilities)
    if not db.get(Shift, data.shift_id):
        raise HTTPException(422, '班次不存在')
    if db.scalar(select(Instrument).where(Instrument.code == data.code.strip())):
        raise HTTPException(409, '仪器编号已存在')
    unknown = set(data.item_minutes) - set(ITEMS)
    if unknown:
        raise HTTPException(422, '检测时长包含未知检验项：' + '、'.join(sorted(unknown)))
    now = time.time()
    ins = Instrument(code=data.code.strip(), name=data.name.strip(), shift_id=data.shift_id,
                     capabilities=list(dict.fromkeys(data.capabilities)),
                     item_minutes={k: int(v) for k, v in data.item_minutes.items()},
                     prep_minutes=data.prep_minutes, validity_minutes=data.validity_minutes,
                     active=data.active, version=1, updated_at=now)
    db.add(ins); db.flush()
    record(db, u.email, 'recheck.instrument.create', {'id': ins.id, 'code': ins.code})
    db.commit()
    shifts_map = {s.id: {'id': s.id, 'name': s.name} for s in db.scalars(select(Shift))}
    return _instrument_payload(ins, shifts_map)


@router.patch('/instruments/{id}')
def patch_instrument(id: int, data: InstrumentPatch, db=Depends(get_db), u=Depends(admin_only)):
    with scheduling.SCHEDULER_LOCK:
        db.rollback()
        ins = db.scalar(select(Instrument).where(Instrument.id == id).with_for_update())
        if not ins:
            raise HTTPException(404, '仪器不存在')
        changes = data.model_dump(exclude_unset=True)
        if 'capabilities' in changes and changes['capabilities'] is not None:
            _validate_capabilities(changes['capabilities'])
            changes['capabilities'] = list(dict.fromkeys(changes['capabilities']))
        if 'shift_id' in changes and changes['shift_id'] is not None and not db.get(Shift, changes['shift_id']):
            raise HTTPException(422, '班次不存在')
        if 'item_minutes' in changes and changes['item_minutes'] is not None:
            unknown = set(changes['item_minutes']) - set(ITEMS)
            if unknown:
                raise HTTPException(422, '检测时长包含未知检验项：' + '、'.join(sorted(unknown)))
            changes['item_minutes'] = {k: int(v) for k, v in changes['item_minutes'].items()}
        if 'name' in changes and changes['name'] is not None:
            changes['name'] = changes['name'].strip()
        for key, value in changes.items():
            if value is not None:
                setattr(ins, key, value)
        ins.version += 1
        ins.updated_at = time.time()
        impact = _resource_impact(db, 'instrument', ins.id, u.email) if _operational(changes) else []
        record(db, u.email, 'recheck.instrument.update', {'id': ins.id, 'changes': list(changes), 'impact': impact})
        db.commit()
        shifts_map = {s.id: {'id': s.id, 'name': s.name} for s in db.scalars(select(Shift))}
        return {**_instrument_payload(ins, shifts_map), 'impact': impact}


def _inspector_payload(p, shifts_map):
    return {'id': p.id, 'code': p.code, 'name': p.name, 'shift_id': p.shift_id,
            'shift': shifts_map.get(p.shift_id), 'capabilities': p.capabilities or [],
            'item_labels': [NAMES.get(c, c) for c in (p.capabilities or [])],
            'active': p.active, 'version': p.version, 'updated_at': p.updated_at}


@router.get('/inspectors')
def list_inspectors(db=Depends(get_db), u=Depends(current_user)):
    shifts_map = {s.id: {'id': s.id, 'name': s.name} for s in db.scalars(select(Shift))}
    return [_inspector_payload(p, shifts_map) for p in
            db.scalars(select(Inspector).order_by(Inspector.id))]


@router.post('/inspectors', status_code=201)
def create_inspector(data: InspectorInput, db=Depends(get_db), u=Depends(admin_only)):
    _validate_capabilities(data.capabilities)
    if not db.get(Shift, data.shift_id):
        raise HTTPException(422, '班次不存在')
    if db.scalar(select(Inspector).where(Inspector.code == data.code.strip())):
        raise HTTPException(409, '检验员编号已存在')
    now = time.time()
    insp = Inspector(code=data.code.strip(), name=data.name.strip(), shift_id=data.shift_id,
                     capabilities=list(dict.fromkeys(data.capabilities)), active=data.active,
                     version=1, updated_at=now)
    db.add(insp); db.flush()
    record(db, u.email, 'recheck.inspector.create', {'id': insp.id, 'code': insp.code})
    db.commit()
    shifts_map = {s.id: {'id': s.id, 'name': s.name} for s in db.scalars(select(Shift))}
    return _inspector_payload(insp, shifts_map)


@router.patch('/inspectors/{id}')
def patch_inspector(id: int, data: InspectorPatch, db=Depends(get_db), u=Depends(admin_only)):
    with scheduling.SCHEDULER_LOCK:
        db.rollback()
        insp = db.scalar(select(Inspector).where(Inspector.id == id).with_for_update())
        if not insp:
            raise HTTPException(404, '检验员不存在')
        changes = data.model_dump(exclude_unset=True)
        if 'capabilities' in changes and changes['capabilities'] is not None:
            _validate_capabilities(changes['capabilities'])
            changes['capabilities'] = list(dict.fromkeys(changes['capabilities']))
        if 'shift_id' in changes and changes['shift_id'] is not None and not db.get(Shift, changes['shift_id']):
            raise HTTPException(422, '班次不存在')
        if 'name' in changes and changes['name'] is not None:
            changes['name'] = changes['name'].strip()
        for key, value in changes.items():
            if value is not None:
                setattr(insp, key, value)
        insp.version += 1
        insp.updated_at = time.time()
        impact = _resource_impact(db, 'inspector', insp.id, u.email) if _operational(changes) else []
        record(db, u.email, 'recheck.inspector.update', {'id': insp.id, 'changes': list(changes), 'impact': impact})
        db.commit()
        shifts_map = {s.id: {'id': s.id, 'name': s.name} for s in db.scalars(select(Shift))}
        return {**_inspector_payload(insp, shifts_map), 'impact': impact}


# ---------------------------------------------------------------- 工单与预排
@router.get('/orders')
def list_orders(status: str | None = Query(default=None), db=Depends(get_db), u=Depends(current_user)):
    stmt = select(RecheckOrder).order_by(RecheckOrder.id.desc()).limit(200)
    if status:
        stmt = stmt.where(RecheckOrder.status == status)
    return [scheduling.order_dict(db, o) for o in db.scalars(stmt)]


@router.post('/orders', status_code=201)
def create_order(data: OrderInput, db=Depends(get_db), u=Depends(can_write)):
    job, dataset, batches, items = scheduling.validate_order_input(db, data.job_id, data.batches, data.items)
    now = time.time()
    order = RecheckOrder(name=data.name.strip(), job_id=job.id, dataset_id=dataset.id,
                         batches=batches, items=items, status='pending', block_reasons=[],
                         created_by=u.email, created_at=now, updated_at=now)
    db.add(order); db.flush()
    record(db, u.email, 'recheck.order.create', {'id': order.id, 'job_id': job.id,
                                                 'batches': len(batches), 'items': items})
    db.commit()
    return scheduling.order_dict(db, order, detail=True)


@router.get('/orders/{id}')
def order_detail(id: int, db=Depends(get_db), u=Depends(current_user)):
    order = db.get(RecheckOrder, id)
    if not order:
        raise HTTPException(404, '复检工单不存在')
    return scheduling.order_dict(db, order, detail=True)


@router.post('/orders/{id}/preview')
def preview_order(id: int, db=Depends(get_db), u=Depends(can_write)):
    order = db.get(RecheckOrder, id)
    if not order:
        raise HTTPException(404, '复检工单不存在')
    if order.status in ('completed', 'cancelled'):
        raise HTTPException(409, '已完工或已取消的工单不能预排')
    if order.status in ('scheduled', 'in_progress', 'abnormal'):
        raise HTTPException(409, {'code': 'order_active',
                                  'message': '该工单已有有效排程，请先取消或处理异常后再预排。',
                                  'order_id': order.id})
    preview = scheduling.create_preview(db, order, u.email)
    record(db, u.email, 'recheck.preview', {'order_id': order.id, 'preview_id': preview.id,
                                            'feasible': preview.feasible})
    db.commit()
    return scheduling.preview_dict(db, preview)


@router.get('/previews/{token}')
def get_preview(token: str, db=Depends(get_db), u=Depends(current_user)):
    preview = db.scalar(select(RecheckPreview).where(RecheckPreview.token == token))
    if not preview:
        raise HTTPException(404, '预览不存在')
    return scheduling.preview_dict(db, preview)


@router.post('/orders/{id}/confirm')
def confirm_order(id: int, data: ConfirmInput, db=Depends(get_db), u=Depends(can_write)):
    schedule, created = scheduling.confirm(db, id, data.preview_token, data.idempotency_key, u.email)
    db.commit()
    order = db.get(RecheckOrder, schedule.order_id)
    return {'created': created, 'schedule': scheduling.schedule_dict(db, schedule),
            'order': scheduling.order_dict(db, order)}


# ---------------------------------------------------------------- 领工、开工、完工
@router.post('/orders/{id}/claims', status_code=201)
def issue_claim(id: int, data: ClaimInput, db=Depends(get_db), u=Depends(can_write)):
    claim = scheduling.issue_claim(db, id, data.inspector_id, data.note, u.email)
    db.commit()
    schedule = db.get(RecheckSchedule, claim.schedule_id)
    return scheduling.claim_dict(claim, {schedule.id: schedule}, include_token=True)


@router.get('/claims')
def list_claims(order_id: int | None = Query(default=None), db=Depends(get_db), u=Depends(current_user)):
    stmt = select(RecheckClaim).order_by(RecheckClaim.id.desc()).limit(200)
    if order_id:
        stmt = stmt.where(RecheckClaim.order_id == order_id)
    claims = list(db.scalars(stmt))
    schedules = {s.id: s for s in db.scalars(select(RecheckSchedule).where(
        RecheckSchedule.id.in_([c.schedule_id for c in claims])))}
    return [scheduling.claim_dict(c, schedules) for c in claims]


@router.post('/claims/{id}/start')
def start_claim(id: int, data: ClaimStartInput = ClaimStartInput(), db=Depends(get_db), u=Depends(can_write)):
    claim = scheduling.start_claim(db, id, data.token, u.email)
    db.commit()
    schedule = db.get(RecheckSchedule, claim.schedule_id)
    return scheduling.claim_dict(claim, {schedule.id: schedule}, include_token=True)


@router.post('/claims/{id}/complete')
def complete_claim(id: int, db=Depends(get_db), u=Depends(can_write)):
    claim = scheduling.complete_claim(db, id, u.email)
    db.commit()
    schedule = db.get(RecheckSchedule, claim.schedule_id)
    return scheduling.claim_dict(claim, {schedule.id: schedule}, include_token=True)


@router.post('/orders/{id}/cancel')
def cancel_order(id: int, db=Depends(get_db), u=Depends(can_write)):
    order = scheduling.cancel_order(db, id, u.email)
    return {'order': scheduling.order_dict(db, order, detail=True)}


@router.post('/orders/{id}/dispose')
def dispose_order(id: int, data: DisposeInput, db=Depends(get_db), u=Depends(can_write)):
    order, result = scheduling.dispose_abnormal(db, id, data.action, u.email)
    if data.note:
        record(db, u.email, 'recheck.dispose_note', {'order_id': id, 'note': data.note})
    db.commit()
    return {'order': scheduling.order_dict(db, order, detail=True),
            'schedule': scheduling.schedule_dict(db, result) if isinstance(result, RecheckSchedule) else None}


# ---------------------------------------------------------------- 停机
@router.get('/downtimes')
def list_downtimes(db=Depends(get_db), u=Depends(current_user)):
    return [scheduling.downtime_dict(db, d) for d in
            db.scalars(select(Downtime).order_by(Downtime.id.desc()).limit(100))]


@router.post('/downtimes', status_code=201)
def create_downtime(data: DowntimeInput, db=Depends(get_db), u=Depends(admin_only)):
    downtime = scheduling.register_downtime(db, data, u.email)
    db.commit()
    return scheduling.downtime_dict(db, downtime)


@router.post('/downtimes/{id}/resolve')
def resolve_downtime(id: int, restore_original: bool = True, db=Depends(get_db), u=Depends(admin_only)):
    downtime, restored = scheduling.resolve_downtime(db, id, u.email, restore_original)
    db.commit()
    return {**scheduling.downtime_dict(db, downtime), 'restored_count': restored}


# ---------------------------------------------------------------- 历史排程依据
@router.get('/schedules')
def list_schedules(order_id: int | None = Query(default=None), db=Depends(get_db), u=Depends(current_user)):
    stmt = select(RecheckSchedule).order_by(RecheckSchedule.id.desc()).limit(200)
    if order_id:
        stmt = stmt.where(RecheckSchedule.order_id == order_id)
    return [scheduling.schedule_dict(db, s) for s in db.scalars(stmt)]


@router.get('/revisions')
def list_revisions(order_id: int | None = Query(default=None), db=Depends(get_db), u=Depends(current_user)):
    stmt = select(RecheckRevision).order_by(RecheckRevision.id.desc()).limit(200)
    if order_id:
        stmt = stmt.where(RecheckRevision.order_id == order_id)
    return [{'id': r.id, 'schedule_id': r.schedule_id, 'order_id': r.order_id, 'version': r.version,
             'reason': r.reason, 'plan': r.plan, 'basis': r.basis, 'actor': r.actor, 'at': r.at,
             'at_local': scheduling.local_iso(r.at)} for r in db.scalars(stmt)]


@router.post('/recover')
def recover(db=Depends(get_db), u=Depends(admin_only)):
    counts = scheduling.recover(db)
    db.commit()
    return counts
