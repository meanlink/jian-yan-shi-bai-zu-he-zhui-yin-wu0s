"""复检排程：班次/能力/时长/制样有效期约束下的双资源预约。

资源区间一律使用半开区间 [start, end)；时间戳为 epoch 秒。
班次按业务时区（RECHECK_TZ_OFFSET_MINUTES）的自然日解释。
"""
from __future__ import annotations

import secrets
import threading
import time
from datetime import date, datetime, time as dtime, timedelta, timezone

from fastapi import HTTPException
from sqlalchemy import select
from sqlalchemy.exc import IntegrityError

from src.config.settings import (RECHECK_DEFAULT_ITEM_MINUTES, RECHECK_HORIZON_DAYS,
                                 RECHECK_PREVIEW_TTL_SECONDS, RECHECK_TZ_OFFSET_MINUTES)
from src.models import (Dataset, Downtime, Inspector, Instrument, Job, RecheckClaim,
                        RecheckConfirmation, RecheckOccupancy, RecheckOrder, RecheckPreview,
                        RecheckRevision, RecheckSchedule, Shift)
from src.services.audit import record
from src.services.business import ITEMS, NAMES
from src.utils.serialization import checksum

_TZ = timezone(timedelta(minutes=RECHECK_TZ_OFFSET_MINUTES))
# 单进程内的排程互斥：SQLite 测试与单容器部署下也能保证确认不重叠。
# 多进程部署另由 MySQL 行锁（资源 → 工单，固定顺序）兜底。
SCHEDULER_LOCK = threading.RLock()
ACTIVE_SCHEDULE_STATUSES = ('scheduled', 'in_progress')
OCCUPYING_SCHEDULE_STATUSES = ('scheduled', 'in_progress', 'abnormal')
RESOURCE_LABEL = {'instrument': '仪器', 'inspector': '检验员'}


# ---------------------------------------------------------------- 时间与区间
def local_iso(ts: float | None) -> str | None:
    if ts is None:
        return None
    return datetime.fromtimestamp(ts, _TZ).isoformat(timespec='seconds')


def _local_date(ts: float) -> date:
    return datetime.fromtimestamp(ts, _TZ).date()


def _at(day: date, minute: int) -> float:
    return (datetime.combine(day, dtime.min, tzinfo=_TZ) + timedelta(minutes=minute)).timestamp()


def shift_windows(shift: Shift, start_ts: float, end_ts: float) -> list[tuple[float, float]]:
    """把每日班次展开为 [start_ts, end_ts) 内的可用窗口（尚未扣除占用）。"""
    if not shift or not shift.active or shift.start_minute >= shift.end_minute:
        return []
    weekdays = set(shift.weekdays or [])
    if not weekdays:
        return []
    windows: list[tuple[float, float]] = []
    first = _local_date(start_ts) - timedelta(days=1)
    last = _local_date(end_ts) + timedelta(days=1)
    day = first
    while day <= last:
        if day.weekday() in weekdays:
            s = _at(day, shift.start_minute)
            e = _at(day, shift.end_minute)
            s, e = max(s, start_ts), min(e, end_ts)
            if e > s:
                windows.append((s, e))
        day += timedelta(days=1)
    return merge_intervals(windows)


def merge_intervals(intervals):
    ordered = sorted((float(s), float(e)) for s, e in intervals if e > s)
    merged: list[tuple[float, float]] = []
    for s, e in ordered:
        if merged and s <= merged[-1][1]:
            merged[-1] = (merged[-1][0], max(merged[-1][1], e))
        else:
            merged.append((s, e))
    return merged


def subtract_intervals(windows, busy):
    blocked = merge_intervals(busy)
    result: list[tuple[float, float]] = []
    for ws, we in merge_intervals(windows):
        cursor = ws
        for bs, be in blocked:
            if be <= cursor or bs >= we:
                continue
            if bs > cursor:
                result.append((cursor, min(bs, we)))
            cursor = max(cursor, be)
            if cursor >= we:
                break
        if cursor < we:
            result.append((cursor, we))
    return result


def intersect_intervals(a, b):
    a, b = merge_intervals(a), merge_intervals(b)
    out, i, j = [], 0, 0
    while i < len(a) and j < len(b):
        s, e = max(a[i][0], b[j][0]), min(a[i][1], b[j][1])
        if e > s:
            out.append((s, e))
        if a[i][1] <= b[j][1]:
            i += 1
        else:
            j += 1
    return out


def _container(windows, start, end):
    return any(s <= start and end <= e for s, e in windows)


# ---------------------------------------------------------------- 资源日历
def _downtime_rows(db, instrument_id: int, start: float, end: float):
    return list(db.scalars(select(Downtime).where(
        Downtime.instrument_id == instrument_id,
        Downtime.start_at < end,
        Downtime.end_at > start)))


def effective_downtime(d: Downtime) -> tuple[float, float]:
    """已提前恢复的停机只占用到恢复时刻。"""
    end = d.end_at
    start = d.start_at
    if d.resolved_at is not None:
        # 提前恢复：停机视为在“恢复时刻”结束，之前已过去的时段仍算占用。
        end = min(d.end_at, max(d.resolved_at, start))
    else:
        end = d.end_at
    return start, end


def busy_intervals(db, resource_type: str, resource_id: int, start: float, end: float,
                   exclude_schedule_id: int | None = None):
    rows = db.scalars(select(RecheckOccupancy).where(
        RecheckOccupancy.resource_type == resource_type,
        RecheckOccupancy.resource_id == resource_id,
        RecheckOccupancy.active == True,  # noqa: E712
        RecheckOccupancy.start_at < end,
        RecheckOccupancy.end_at > start))
    busy = [(r.start_at, r.end_at) for r in rows if r.schedule_id != exclude_schedule_id]
    if resource_type == 'instrument':
        for d in _downtime_rows(db, resource_id, start, end):
            ds, de = effective_downtime(d)
            if de > ds:
                busy.append((ds, de))
    return merge_intervals(busy)


def free_windows(db, shift: Shift, resource_type: str, resource_id: int,
                 start: float, end: float, exclude_schedule_id: int | None = None):
    windows = shift_windows(shift, start, end)
    busy = busy_intervals(db, resource_type, resource_id, start, end, exclude_schedule_id)
    return subtract_intervals(windows, busy)


def _shift_of(db, model, obj):
    return db.get(Shift, obj.shift_id) if obj else None


# ---------------------------------------------------------------- 时长与可行性
def order_duration(instrument: Instrument, order: RecheckOrder):
    batches = max(1, len(order.batches or []))
    prep = int(instrument.prep_minutes) * batches
    per_item = instrument.item_minutes or {}
    test = 0
    for item in order.items or []:
        test += int(per_item.get(item, RECHECK_DEFAULT_ITEM_MINUTES))
    test *= batches
    validity = int(instrument.validity_minutes)
    return prep, test, validity


def _capability_reasons(db, order, items):
    reasons = []
    instruments = list(db.scalars(select(Instrument).where(Instrument.active == True).order_by(Instrument.id)))
    inspectors = list(db.scalars(select(Inspector).where(Inspector.active == True).order_by(Inspector.id)))
    eligible_ins, eligible_insp = [], []
    for ins in instruments:
        missing = [i for i in items if i not in (ins.capabilities or [])]
        if missing:
            reasons.append({'code': 'instrument_capability', 'resource_type': 'instrument',
                            'resource_id': ins.id, 'resource': ins.name,
                            'message': f'仪器 {ins.name} 缺少检验项能力：' + '、'.join(NAMES.get(i, i) for i in missing)})
            continue
        if not _shift_of(db, Instrument, ins):
            reasons.append({'code': 'instrument_shift', 'resource_type': 'instrument', 'resource_id': ins.id,
                            'resource': ins.name, 'message': f'仪器 {ins.name} 未绑定有效班次'})
            continue
        eligible_ins.append(ins)
    for insp in inspectors:
        missing = [i for i in items if i not in (insp.capabilities or [])]
        if missing:
            reasons.append({'code': 'inspector_capability', 'resource_type': 'inspector',
                            'resource_id': insp.id, 'resource': insp.name,
                            'message': f'检验员 {insp.name} 缺少检验项能力：' + '、'.join(NAMES.get(i, i) for i in missing)})
            continue
        if not _shift_of(db, Inspector, insp):
            reasons.append({'code': 'inspector_shift', 'resource_type': 'inspector', 'resource_id': insp.id,
                            'resource': insp.name, 'message': f'检验员 {insp.name} 未绑定有效班次'})
            continue
        eligible_insp.append(insp)
    return eligible_ins, eligible_insp, reasons


def compute_plan(db, order: RecheckOrder, now: float | None = None, not_before: float | None = None):
    """返回 (plan, reasons)。plan 为最早可行整单排程；无时段时 reasons 说明阻塞原因。"""
    now = float(now if now is not None else time.time())
    earliest = max(now, float(not_before or now))
    horizon_end = earliest + RECHECK_HORIZON_DAYS * 86400
    items = list(order.items or [])
    eligible_ins, eligible_insp, reasons = _capability_reasons(db, order, items)
    if not eligible_ins:
        reasons.append({'code': 'no_instrument', 'message': '没有同时具备所选检验项能力且启用的仪器'})
    if not eligible_insp:
        reasons.append({'code': 'no_inspector', 'message': '没有同时具备所选检验项能力且启用的检验员'})
    if not eligible_ins or not eligible_insp:
        return None, reasons

    candidates = []
    details = []
    for ins in eligible_ins:
        prep, test, validity = order_duration(ins, order)
        if test <= 0 or prep <= 0:
            reasons.append({'code': 'invalid_duration', 'resource_type': 'instrument', 'resource_id': ins.id,
                            'message': f'仪器 {ins.name} 的制样或检测时长为 0'})
            continue
        if test > validity:
            reasons.append({'code': 'validity_too_short', 'resource_type': 'instrument', 'resource_id': ins.id,
                            'message': (f'仪器 {ins.name} 的检测需 {test} 分钟，但制样后有效期仅 {validity} 分钟，'
                                        '检测无法全程落在有效期内')})
            continue
        ins_shift = _shift_of(db, Instrument, ins)
        ins_windows = free_windows(db, ins_shift, 'instrument', ins.id, earliest, horizon_end)
        if not ins_windows:
            details.append(f'{ins.name} 在 {RECHECK_HORIZON_DAYS} 天内没有空闲班次（含停机与已占用）')
        total = prep + test
        for insp in eligible_insp:
            insp_shift = _shift_of(db, Inspector, insp)
            insp_windows = free_windows(db, insp_shift, 'inspector', insp.id, earliest, horizon_end)
            common = intersect_intervals(ins_windows, insp_windows)
            for ws, we in common:
                if we - ws >= total:
                    candidates.append((ws, ins.id, insp.id, ins, insp, ins_shift, insp_shift,
                                       prep, test, validity))
                    break
    if not candidates:
        if not details:
            for ins in eligible_ins:
                prep, test, _ = order_duration(ins, order)
                ins_windows = free_windows(db, _shift_of(db, Instrument, ins), 'instrument', ins.id,
                                           earliest, horizon_end)
                details.append(f'{ins.name} 空闲窗口不足 {prep + test} 分钟')
            for insp in eligible_insp:
                insp_windows = free_windows(db, _shift_of(db, Inspector, insp), 'inspector', insp.id,
                                            earliest, horizon_end)
                details.append(f'{insp.name} 空闲窗口不足')
        reasons.append({
            'code': 'no_feasible_window',
            'message': (f'未来 {RECHECK_HORIZON_DAYS} 天内没有同时满足仪器能力、检验员班次与整单占用时长的时段。'),
            'detail': '；'.join(details[:8]),
            'horizon_end': horizon_end,
        })
        return None, reasons

    start, ins_id, insp_id, ins, insp, ins_shift, insp_shift, prep, test, validity = min(
        candidates, key=lambda c: (c[0], c[1], c[2]))
    prep_end = start + prep * 60
    test_end = prep_end + test * 60
    plan = {
        'instrument_id': ins.id, 'inspector_id': insp.id,
        'prep_start': start, 'prep_end': prep_end,
        'test_start': prep_end, 'test_end': test_end,
        'validity_end': prep_end + validity * 60,
        'prep_minutes': prep, 'test_minutes': test, 'validity_minutes': validity,
        'batches': list(order.batches or []), 'items': list(order.items or []),
        'instrument_shift_id': ins.shift_id, 'inspector_shift_id': insp.shift_id,
    }
    return plan, reasons


# ---------------------------------------------------------------- 依据快照
def _basis_snapshot(db, order: RecheckOrder, plan: dict):
    ins = db.get(Instrument, plan['instrument_id'])
    insp = db.get(Inspector, plan['inspector_id'])
    shifts = {}
    for shift_id in {plan.get('instrument_shift_id'), plan.get('inspector_shift_id')}:
        shift = db.get(Shift, shift_id) if shift_id else None
        if shift:
            shifts[shift.id] = {'id': shift.id, 'name': shift.name, 'start_minute': shift.start_minute,
                                'end_minute': shift.end_minute, 'weekdays': shift.weekdays,
                                'active': shift.active}
    start, end = plan['prep_start'], plan['test_end']
    occupancies = [{'id': r.id, 'schedule_id': r.schedule_id, 'type': r.resource_type,
                    'resource_id': r.resource_id, 'start': r.start_at, 'end': r.end_at}
                   for r in db.scalars(select(RecheckOccupancy).where(
                       RecheckOccupancy.active == True,  # noqa: E712
                       RecheckOccupancy.start_at < end, RecheckOccupancy.end_at > start).order_by(RecheckOccupancy.id))]
    downtimes = [{'id': d.id, 'start': d.start_at, 'end': d.end_at, 'resolved_at': d.resolved_at}
                 for d in _downtime_rows(db, plan['instrument_id'], start, end)]
    snapshot = {
        'order': {'id': order.id, 'items': list(order.items or []), 'batches': list(order.batches or []),
                  'dataset_id': order.dataset_id},
        'plan': {k: plan[k] for k in ('instrument_id', 'inspector_id', 'prep_start', 'prep_end',
                                      'test_start', 'test_end', 'prep_minutes', 'test_minutes', 'validity_minutes')}
                | {k: plan.get(k) for k in ('instrument_shift_id', 'inspector_shift_id')},
        'instrument': ({'id': ins.id, 'version': ins.version, 'active': ins.active, 'shift_id': ins.shift_id,
                        'capabilities': ins.capabilities, 'item_minutes': ins.item_minutes,
                        'prep_minutes': ins.prep_minutes, 'validity_minutes': ins.validity_minutes}
                       if ins else None),
        'inspector': ({'id': insp.id, 'version': insp.version, 'active': insp.active, 'shift_id': insp.shift_id,
                       'capabilities': insp.capabilities} if insp else None),
        'shifts': shifts,
        'occupancies': occupancies,
        'downtimes': downtimes,
    }
    snapshot['hash'] = checksum(snapshot)
    return snapshot


# ---------------------------------------------------------------- 预览
def _plan_for_response(db, plan: dict):
    if not plan:
        return None
    ins = db.get(Instrument, plan['instrument_id'])
    insp = db.get(Inspector, plan['inspector_id'])
    result = dict(plan)
    result.update({
        'instrument': {'id': ins.id, 'code': ins.code, 'name': ins.name} if ins else None,
        'inspector': {'id': insp.id, 'code': insp.code, 'name': insp.name} if insp else None,
        'prep_start_local': local_iso(plan['prep_start']), 'prep_end_local': local_iso(plan['prep_end']),
        'test_start_local': local_iso(plan['test_start']), 'test_end_local': local_iso(plan['test_end']),
        'validity_end_local': local_iso(plan['validity_end']),
        'item_labels': [NAMES.get(i, i) for i in plan.get('items', [])],
    })
    return result


def preview_dict(db, preview: RecheckPreview):
    return {
        'id': preview.id, 'order_id': preview.order_id, 'token': preview.token,
        'feasible': preview.feasible, 'plan': _plan_for_response(db, preview.plan),
        'reasons': preview.reasons or [], 'created_at': preview.created_at,
        'created_at_local': local_iso(preview.created_at), 'expires_at': preview.expires_at,
        'expires_at_local': local_iso(preview.expires_at),
        'consumed_schedule_id': preview.consumed_schedule_id, 'actor': preview.actor,
        'expired': preview.expires_at <= time.time(),
    }


def create_preview(db, order: RecheckOrder, actor: str, now: float | None = None,
                   persist: bool = True, not_before: float | None = None):
    now = float(now if now is not None else time.time())
    plan, reasons = compute_plan(db, order, now=now, not_before=not_before)
    basis = _basis_snapshot(db, order, plan) if plan else {'hash': None, 'order_id': order.id}
    preview = RecheckPreview(
        order_id=order.id, token=secrets.token_urlsafe(24), feasible=bool(plan),
        plan=plan, reasons=reasons, basis=basis, created_at=now,
        expires_at=now + RECHECK_PREVIEW_TTL_SECONDS, actor=actor,
    )
    if persist:
        db.add(preview)
        db.flush()
        if order.status in ('pending', 'previewed', 'blocked'):
            order.status = 'previewed' if plan else 'blocked'
            order.block_reasons = [] if plan else reasons
            order.updated_at = now
    return preview


def _raise_replan(db, order_id, actor, now, code, message):
    order = db.get(RecheckOrder, order_id)
    fresh = create_preview(db, order, actor, now=now, persist=True)
    payload = preview_dict(db, fresh)
    db.commit()
    raise HTTPException(409, {'code': code, 'message': message, 'replan': True,
                              'preview': payload, 'order_id': order.id})


# ---------------------------------------------------------------- 确认与占位
def _active_schedule(db, order):
    if not order.current_schedule_id:
        return None
    schedule = db.get(RecheckSchedule, order.current_schedule_id)
    if schedule and schedule.status in OCCUPYING_SCHEDULE_STATUSES:
        return schedule
    return None


def _lock_resource(db, model, resource_id):
    return db.scalar(select(model).where(model.id == resource_id).with_for_update())


def _has_conflict(db, resource_type, resource_id, start, end, exclude_schedule_id=None):
    return bool(busy_intervals(db, resource_type, resource_id, start, end, exclude_schedule_id))


def _create_schedule(db, order, plan, basis, version, reason, actor, now, parent_id=None):
    schedule = RecheckSchedule(
        order_id=order.id, instrument_id=plan['instrument_id'], inspector_id=plan['inspector_id'],
        prep_start=plan['prep_start'], prep_end=plan['prep_end'], test_start=plan['test_start'],
        test_end=plan['test_end'], validity_end=plan['validity_end'],
        prep_minutes=plan['prep_minutes'], test_minutes=plan['test_minutes'],
        validity_minutes=plan['validity_minutes'], status='scheduled', plan_version=version,
        basis=basis, created_at=now, updated_at=now,
    )
    db.add(schedule)
    db.flush()
    for resource_type, resource_id in (('instrument', schedule.instrument_id),
                                       ('inspector', schedule.inspector_id)):
        db.add(RecheckOccupancy(schedule_id=schedule.id, resource_type=resource_type,
                                resource_id=resource_id, start_at=plan['prep_start'],
                                end_at=plan['test_end'], active=True, created_at=now))
    schedule.basis = {**basis, 'plan_version': version, 'created_reason': reason,
                      'parent_schedule_id': parent_id}
    db.add(RecheckRevision(schedule_id=schedule.id, order_id=order.id, version=version,
                           reason=reason, plan=plan, basis=basis, actor=actor, at=now))
    return schedule


def _latest_version(db, order_id):
    value = db.scalar(select(RecheckSchedule.plan_version).where(
        RecheckSchedule.order_id == order_id).order_by(RecheckSchedule.plan_version.desc()).limit(1))
    return int(value or 0)


def confirm(db, order_id: int, preview_token: str, idempotency_key: str | None, actor: str):
    """确认预排：整单占用仪器与检验员，返回 (schedule, created)。

    并发安全：进程内排程互斥 + 数据库行锁（资源行 → 工单行，固定顺序）。
    同一幂等键或同一预览的重试不会重复占位。
    """
    now = time.time()
    with SCHEDULER_LOCK:
        db.rollback()  # 丢弃进入前的读快照，确保锁内看到最新占位
        preview = db.scalar(select(RecheckPreview).where(
            RecheckPreview.order_id == order_id, RecheckPreview.token == preview_token))
        if not preview:
            raise HTTPException(404, '排程预览不存在，请重新预排')
        plan = preview.plan or {}

        # 先锁资源（仪器 → 检验员），与停机登记/恢复保持同一顺序，避免死锁。
        ins = insp = None
        if plan:
            ins = _lock_resource(db, Instrument, plan['instrument_id'])
            insp = _lock_resource(db, Inspector, plan['inspector_id'])
            if not ins or not insp or not ins.active or not insp.active:
                _raise_replan(db, order_id, actor, now, 'resource_unavailable',
                              '排程所需仪器或检验员已停用，已重新预排。')

        order = db.scalar(select(RecheckOrder).where(RecheckOrder.id == order_id).with_for_update())
        if not order:
            raise HTTPException(404, '复检工单不存在')

        if idempotency_key:
            existing = db.scalar(select(RecheckConfirmation).where(
                RecheckConfirmation.idempotency_key == idempotency_key))
            if existing:
                if existing.order_id != order.id:
                    raise HTTPException(409, '该幂等键已被其他工单使用')
                return db.get(RecheckSchedule, existing.schedule_id), False

        if preview.consumed_schedule_id:
            schedule = db.get(RecheckSchedule, preview.consumed_schedule_id)
            if schedule:
                return schedule, False  # 同一预览的重试不再占位

        active = _active_schedule(db, order)
        if active:
            raise HTTPException(409, {'code': 'order_already_scheduled',
                                      'message': f'该工单已排程（版本 {active.plan_version}），请先取消或等待完工。',
                                      'order_id': order.id, 'schedule_id': active.id})
        if not preview.feasible or not plan:
            raise HTTPException(409, {'code': 'preview_infeasible', 'message': '该预览没有可行时段，不能确认。',
                                      'reasons': preview.reasons or []})
        if preview.expires_at <= now:
            _raise_replan(db, order.id, actor, now, 'preview_expired',
                          '预览已过期，已按当前资源重新预排，请确认新方案。')
        if preview.order_id != order.id:
            raise HTTPException(409, {'code': 'preview_order_mismatch', 'message': '预览不属于该工单。'})

        current_basis = _basis_snapshot(db, order, plan)
        if preview.basis.get('hash') != current_basis.get('hash'):
            _raise_replan(db, order.id, actor, now, 'preview_stale',
                          '资源能力、班次、停机或占用已变化，原预览不再适用，已重新预排，请确认新方案。')
        if _has_conflict(db, 'instrument', ins.id, plan['prep_start'], plan['test_end']):
            _raise_replan(db, order.id, actor, now, 'resource_conflict', '仪器时段已被占用，已重新预排。')
        if _has_conflict(db, 'inspector', insp.id, plan['prep_start'], plan['test_end']):
            _raise_replan(db, order.id, actor, now, 'resource_conflict', '检验员时段已被占用，已重新预排。')
        prep, test, validity = order_duration(ins, order)
        if (prep, test, validity) != (plan['prep_minutes'], plan['test_minutes'], plan['validity_minutes']):
            _raise_replan(db, order.id, actor, now, 'duration_changed',
                          '制样或检测时长配置已变化，已重新预排。')

        version = _latest_version(db, order.id) + 1
        basis = {**current_basis, 'preview_token': preview.token, 'confirmed_at': now}
        try:
            schedule = _create_schedule(db, order, plan, basis, version, 'confirmed', actor, now)
            if idempotency_key:
                db.add(RecheckConfirmation(order_id=order.id, idempotency_key=idempotency_key,
                                           schedule_id=schedule.id, preview_id=preview.id, created_at=now))
            preview.consumed_schedule_id = schedule.id
            order.status = 'scheduled'
            order.current_schedule_id = schedule.id
            order.block_reasons = []
            order.updated_at = now
            record(db, actor, 'recheck.confirm', {'order_id': order.id, 'schedule_id': schedule.id,
                                                  'version': version, 'instrument_id': ins.id,
                                                  'inspector_id': insp.id,
                                                  'idempotency_key': idempotency_key})
            # 在互斥区内提交：并发确认在锁外等待时能看到已消费的预览与新增占位，
            # 从而得到“已处理”而不是重复建单或误报冲突。
            db.commit()
        except IntegrityError:
            db.rollback()
            if idempotency_key:
                existing = db.scalar(select(RecheckConfirmation).where(
                    RecheckConfirmation.idempotency_key == idempotency_key))
                if existing:
                    return db.get(RecheckSchedule, existing.schedule_id), False
            raise HTTPException(409, {'code': 'confirm_conflict', 'message': '并发确认冲突，请刷新后重试。'})
    return schedule, True


# ---------------------------------------------------------------- 工单与停机
def validate_order_input(db, job_id, batches, items):
    job = db.get(Job, job_id)
    if not job:
        raise HTTPException(404, '分析任务不存在')
    if job.status != 'completed':
        raise HTTPException(409, '只能从已完成的分析中选择批次与检验项')
    dataset = db.get(Dataset, job.dataset_id)
    if not dataset:
        raise HTTPException(404, '分析任务的数据批次不存在')
    known_batches = {str(r.get('batch_id')) for r in dataset.records if r.get('batch_id') is not None}
    clean_batches = []
    for batch in batches:
        text = str(batch).strip()
        if text not in known_batches:
            raise HTTPException(422, f'批次 {text} 不属于该分析使用的数据批次')
        if text not in clean_batches:
            clean_batches.append(text)
    clean_items = []
    for item in items:
        if item not in ITEMS:
            raise HTTPException(422, f'未知检验项：{item}')
        if item not in clean_items:
            clean_items.append(item)
    if not clean_batches:
        raise HTTPException(422, '请至少选择一个复检批次')
    if not clean_items:
        raise HTTPException(422, '请至少选择一个检验项')
    return job, dataset, clean_batches, clean_items


def _claims_for_schedule(db, schedule_id):
    return list(db.scalars(select(RecheckClaim).where(RecheckClaim.schedule_id == schedule_id)))


def _invalidate_claims(db, schedule, status_for_issued='voided', status_for_started='interrupted', note=''):
    for claim in _claims_for_schedule(db, schedule.id):
        if claim.status == 'issued':
            claim.status = status_for_issued
            claim.note = (note or '排程已变更，领工记录失效')[:300]
        elif claim.status == 'started' and status_for_started:
            claim.status = status_for_started
            claim.note = (note or '班次中断，待处置')[:300]


def _deactivate_occupancies(db, schedule):
    for row in db.scalars(select(RecheckOccupancy).where(
            RecheckOccupancy.schedule_id == schedule.id, RecheckOccupancy.active == True)):  # noqa: E712
        row.active = False


def _activate_occupancies(db, schedule):
    for row in db.scalars(select(RecheckOccupancy).where(RecheckOccupancy.schedule_id == schedule.id)):
        row.active = True


def _plan_of_schedule(schedule):
    basis_plan = (schedule.basis or {}).get('plan', {}) or {}
    return {
        'instrument_id': schedule.instrument_id, 'inspector_id': schedule.inspector_id,
        'prep_start': schedule.prep_start, 'prep_end': schedule.prep_end,
        'test_start': schedule.test_start, 'test_end': schedule.test_end,
        'validity_end': schedule.validity_end, 'prep_minutes': schedule.prep_minutes,
        'test_minutes': schedule.test_minutes, 'validity_minutes': schedule.validity_minutes,
        'instrument_shift_id': basis_plan.get('instrument_shift_id'),
        'inspector_shift_id': basis_plan.get('inspector_shift_id'),
    }


def _instrument_down_now(db, instrument_id, at=None):
    at = time.time() if at is None else at
    for d in db.scalars(select(Downtime).where(
            Downtime.instrument_id == instrument_id,
            Downtime.start_at <= at, Downtime.end_at > at)):
        ds, de = effective_downtime(d)
        if ds <= at < de:
            return d
    return None


def _replan_from(db, order, actor, now, not_before, reason, parent_id, downtime_id=None):
    """内部重排；成功返回新排程，失败将工单标为阻塞并返回 (None, reasons)。"""
    plan, reasons = compute_plan(db, order, now=now, not_before=not_before)
    if not plan:
        order.status = 'blocked'
        order.block_reasons = reasons
        order.current_schedule_id = None
        order.updated_at = now
        return None, reasons
    basis = _basis_snapshot(db, order, plan)
    version = _latest_version(db, order.id) + 1
    schedule = _create_schedule(db, order, plan, {**basis, 'downtime_id': downtime_id},
                                version, reason, actor, now, parent_id=parent_id)
    order.status = 'scheduled'
    order.current_schedule_id = schedule.id
    order.block_reasons = []
    order.updated_at = now
    return schedule, reasons


def register_downtime(db, data, actor: str):
    with SCHEDULER_LOCK:
        return _register_downtime(db, data, actor)


def _register_downtime(db, data, actor: str):
    now = time.time()
    db.rollback()
    instrument = db.scalar(select(Instrument).where(Instrument.id == data.instrument_id).with_for_update())
    if not instrument:
        raise HTTPException(404, '仪器不存在')
    start_at = float(data.start_at if data.start_at is not None else now)
    end_at = float(data.end_at)
    if end_at <= start_at:
        raise HTTPException(422, '停机结束时间必须晚于开始时间')
    if not (data.reason or '').strip() or len(data.reason.strip()) < 2:
        raise HTTPException(422, '请填写停机原因（至少 2 个字符）')
    downtime = Downtime(instrument_id=instrument.id, start_at=start_at, end_at=end_at,
                        reason=data.reason.strip()[:300], created_by=actor, created_at=now, impact=[])
    db.add(downtime)
    db.flush()
    affected = list(db.scalars(select(RecheckSchedule).where(
        RecheckSchedule.instrument_id == instrument.id,
        RecheckSchedule.status.in_(('scheduled', 'in_progress')),
        RecheckSchedule.prep_start < end_at,
        RecheckSchedule.test_end > start_at).order_by(RecheckSchedule.id)))
    impact = []
    for schedule in affected:
        order = db.scalar(select(RecheckOrder).where(RecheckOrder.id == schedule.order_id).with_for_update())
        if not order:
            continue
        started = schedule.status == 'in_progress' or schedule.started_at is not None
        if started:
            schedule.status = 'abnormal'
            schedule.updated_at = now
            schedule.basis = {**(schedule.basis or {}), 'abnormal_downtime_id': downtime.id,
                              'abnormal_at': now}
            order.status = 'abnormal'
            order.block_reasons = [{'code': 'instrument_downtime', 'downtime_id': downtime.id,
                                    'message': f'仪器 {instrument.name} 停机时该工单已开工，检测转异常，等待处置。'}]
            order.updated_at = now
            _invalidate_claims(db, schedule, note='仪器停机，已开工检测转异常待处置')
            impact.append({'schedule_id': schedule.id, 'order_id': order.id, 'action': 'abnormal',
                           'order_name': order.name, 'started': True,
                           'original_plan': _plan_of_schedule(schedule)})
            record(db, actor, 'recheck.downtime_abnormal', {'order_id': order.id, 'schedule_id': schedule.id,
                                                            'downtime_id': downtime.id})
        else:
            original_plan = _plan_of_schedule(schedule)
            _invalidate_claims(db, schedule, note='仪器停机，原排程已重排')
            _deactivate_occupancies(db, schedule)
            schedule.status = 'cancelled'
            schedule.updated_at = now
            schedule.basis = {**(schedule.basis or {}), 'cancelled_reason': 'downtime',
                              'downtime_id': downtime.id, 'cancelled_at': now}
            new_schedule, reasons = _replan_from(
                db, order, actor, now, max(now, end_at), 'downtime_replan', schedule.id, downtime.id)
            impact.append({'schedule_id': schedule.id, 'order_id': order.id,
                           'action': 'replanned' if new_schedule else 'blocked',
                           'order_name': order.name, 'started': False,
                           'original_plan': original_plan,
                           'new_schedule_id': new_schedule.id if new_schedule else None,
                           'reasons': reasons if not new_schedule else []})
            record(db, actor, 'recheck.downtime_replanned', {'order_id': order.id, 'downtime_id': downtime.id,
                                                             'old_schedule_id': schedule.id,
                                                             'new_schedule_id': new_schedule.id if new_schedule else None})
    downtime.impact = impact
    record(db, actor, 'recheck.downtime', {'instrument_id': instrument.id, 'start_at': start_at,
                                           'end_at': end_at, 'affected': len(impact)})
    db.flush()
    return downtime


def _original_slot_restorable(db, order, schedule, original_plan, now):
    if not original_plan or original_plan['prep_start'] <= now:
        return False
    instrument = db.get(Instrument, original_plan['instrument_id'])
    inspector = db.get(Inspector, original_plan['inspector_id'])
    if not instrument or not inspector or not instrument.active or not inspector.active:
        return False
    if _instrument_down_now(db, instrument.id, now):
        return False
    ins_shift = db.get(Shift, instrument.shift_id)
    insp_shift = db.get(Shift, inspector.shift_id)
    if not _container(shift_windows(ins_shift, now, original_plan['test_end']),
                      original_plan['prep_start'], original_plan['test_end']):
        return False
    if not _container(shift_windows(insp_shift, now, original_plan['test_end']),
                      original_plan['prep_start'], original_plan['test_end']):
        return False
    if _has_conflict(db, 'instrument', instrument.id, original_plan['prep_start'],
                     original_plan['test_end'], exclude_schedule_id=schedule.id):
        return False
    if _has_conflict(db, 'inspector', inspector.id, original_plan['prep_start'],
                     original_plan['test_end'], exclude_schedule_id=schedule.id):
        return False
    return True


def _restore_original(db, order, old_schedule, original_plan, actor, now, reason):
    old_schedule.status = 'scheduled'
    old_schedule.updated_at = now
    old_schedule.basis = {**(old_schedule.basis or {}), 'restored_at': now, 'restored_reason': reason}
    _activate_occupancies(db, old_schedule)
    order.status = 'scheduled'
    order.current_schedule_id = old_schedule.id
    order.block_reasons = []
    order.updated_at = now
    db.add(RecheckRevision(schedule_id=old_schedule.id, order_id=order.id,
                           version=old_schedule.plan_version, reason=reason,
                           plan=original_plan, basis=old_schedule.basis or {}, actor=actor, at=now))
    record(db, actor, 'recheck.restore', {'order_id': order.id, 'schedule_id': old_schedule.id,
                                          'reason': reason})


def resolve_downtime(db, downtime_id: int, actor: str, restore_original: bool = True):
    with SCHEDULER_LOCK:
        return _resolve_downtime(db, downtime_id, actor, restore_original)


def _resolve_downtime(db, downtime_id: int, actor: str, restore_original: bool = True):
    now = time.time()
    db.rollback()
    downtime = db.scalar(select(Downtime).where(Downtime.id == downtime_id).with_for_update())
    if not downtime:
        raise HTTPException(404, '停机记录不存在')
    if downtime.resolved_at is not None:
        return downtime, 0
    downtime.resolved_at = now
    downtime.resolved_by = actor
    restored = 0
    impact = list(downtime.impact or [])
    for entry in impact:
        if not restore_original or entry.get('action') != 'replanned':
            continue
        order = db.scalar(select(RecheckOrder).where(RecheckOrder.id == entry['order_id']).with_for_update())
        old_schedule = db.get(RecheckSchedule, entry['schedule_id'])
        new_schedule = db.get(RecheckSchedule, entry.get('new_schedule_id')) if entry.get('new_schedule_id') else None
        if not order or not old_schedule or old_schedule.status != 'cancelled':
            continue
        if new_schedule and (new_schedule.status != 'scheduled' or new_schedule.started_at):
            continue
        if order.current_schedule_id != (new_schedule.id if new_schedule else None):
            continue
        if not _original_slot_restorable(db, order, old_schedule, entry.get('original_plan'), now):
            continue
        if new_schedule:
            _deactivate_occupancies(db, new_schedule)
            new_schedule.status = 'cancelled'
            new_schedule.updated_at = now
            new_schedule.basis = {**(new_schedule.basis or {}), 'cancelled_reason': 'downtime_restored',
                                  'cancelled_at': now}
            _invalidate_claims(db, new_schedule, note='停机提前恢复，原排程已恢复')
        _restore_original(db, order, old_schedule, entry['original_plan'], actor, now, 'downtime_restored')
        entry['action'] = 'restored'
        entry['restored_schedule_id'] = old_schedule.id
        restored += 1
    downtime.impact = impact
    record(db, actor, 'recheck.downtime_resolved', {'downtime_id': downtime.id, 'restored': restored})
    db.flush()
    return downtime, restored


def dispose_abnormal(db, order_id: int, action: str, actor: str):
    with SCHEDULER_LOCK:
        return _dispose_abnormal(db, order_id, action, actor)


def _dispose_abnormal(db, order_id: int, action: str, actor: str):
    now = time.time()
    db.rollback()
    order = db.scalar(select(RecheckOrder).where(RecheckOrder.id == order_id).with_for_update())
    if not order:
        raise HTTPException(404, '复检工单不存在')
    schedule = db.get(RecheckSchedule, order.current_schedule_id) if order.current_schedule_id else None
    if not schedule:
        schedule = db.scalar(select(RecheckSchedule).where(
            RecheckSchedule.order_id == order.id, RecheckSchedule.status == 'abnormal'
        ).order_by(RecheckSchedule.id.desc()).limit(1))
    if not schedule or schedule.status != 'abnormal':
        raise HTTPException(409, {'code': 'not_abnormal', 'message': '该工单没有待处置的异常排程'})
    if action == 'resume':
        if now >= schedule.test_end:
            raise HTTPException(409, {'code': 'schedule_elapsed', 'message': '原检测时段已结束，无法恢复，请选择重排。'})
        if _instrument_down_now(db, schedule.instrument_id, now):
            raise HTTPException(409, {'code': 'instrument_down', 'message': '仪器仍在停机，暂不能恢复检测。'})
        instrument = db.get(Instrument, schedule.instrument_id)
        inspector = db.get(Inspector, schedule.inspector_id)
        resume_start = max(now, schedule.prep_start)
        if not instrument or not inspector:
            raise HTTPException(409, {'code': 'resource_missing', 'message': '排程所需的仪器或检验员已不存在。'})
        if not _container(shift_windows(db.get(Shift, instrument.shift_id), now, schedule.test_end),
                          resume_start, schedule.test_end):
            raise HTTPException(409, {'code': 'outside_shift', 'message': '当前时刻不在仪器班次内，请选择重排。'})
        if not _container(shift_windows(db.get(Shift, inspector.shift_id), now, schedule.test_end),
                          resume_start, schedule.test_end):
            raise HTTPException(409, {'code': 'outside_shift', 'message': '当前时刻不在检验员班次内，请选择重排。'})
        _activate_occupancies(db, schedule)
        schedule.status = 'in_progress'
        schedule.updated_at = now
        schedule.basis = {**(schedule.basis or {}), 'resumed_at': now}
        order.status = 'in_progress'
        order.block_reasons = []
        order.updated_at = now
        db.add(RecheckRevision(schedule_id=schedule.id, order_id=order.id,
                               version=schedule.plan_version, reason='resume_after_downtime',
                               plan=_plan_of_schedule(schedule), basis=schedule.basis or {},
                               actor=actor, at=now))
        record(db, actor, 'recheck.resume', {'order_id': order.id, 'schedule_id': schedule.id})
        db.flush()
        return order, schedule
    if action == 'cancel':
        _deactivate_occupancies(db, schedule)
        schedule.status = 'cancelled'
        schedule.updated_at = now
        schedule.basis = {**(schedule.basis or {}), 'cancelled_reason': 'abnormal_disposed',
                          'cancelled_at': now}
        order.status = 'cancelled'
        order.current_schedule_id = None
        order.block_reasons = []
        order.updated_at = now
        record(db, actor, 'recheck.cancel', {'order_id': order.id, 'schedule_id': schedule.id})
        db.flush()
        return order, schedule
    if action == 'replan':
        _invalidate_claims(db, schedule, note='异常处置：重排')
        _deactivate_occupancies(db, schedule)
        schedule.status = 'cancelled'
        schedule.updated_at = now
        schedule.basis = {**(schedule.basis or {}), 'cancelled_reason': 'abnormal_replanned',
                          'cancelled_at': now}
        new_schedule, reasons = _replan_from(db, order, actor, now, now, 'abnormal_replanned', schedule.id)
        db.flush()
        return order, new_schedule
    raise HTTPException(422, '处置动作仅支持 resume、replan 或 cancel')


# ---------------------------------------------------------------- 领工与完工
def issue_claim(db, order_id: int, inspector_id: int | None, note: str, actor: str):
    with SCHEDULER_LOCK:
        return _issue_claim(db, order_id, inspector_id, note, actor)


def _issue_claim(db, order_id: int, inspector_id: int | None, note: str, actor: str):
    now = time.time()
    db.rollback()
    order = db.scalar(select(RecheckOrder).where(RecheckOrder.id == order_id).with_for_update())
    if not order:
        raise HTTPException(404, '复检工单不存在')
    schedule = _active_schedule(db, order)
    if not schedule or schedule.status not in ('scheduled', 'in_progress'):
        raise HTTPException(409, {'code': 'not_scheduled', 'message': '工单当前没有可领工的排程'})
    if inspector_id and inspector_id != schedule.inspector_id:
        raise HTTPException(409, {'code': 'inspector_mismatch',
                                  'message': '领工检验员须与已占用排程的检验员一致，如需更换请重新预排。'})
    if now > schedule.test_end:
        raise HTTPException(409, {'code': 'schedule_elapsed', 'message': '排程检测时段已结束，请重新预排。'})
    if schedule.status == 'in_progress':
        raise HTTPException(409, {'code': 'order_started',
                                  'message': '该排程已开工，不能重复领工；完工或异常处置后再操作。'})
    existing = [c for c in _claims_for_schedule(db, schedule.id) if c.status == 'issued']
    if existing:
        return existing[0]
    claim = RecheckClaim(order_id=order.id, schedule_id=schedule.id,
                         schedule_version=schedule.plan_version,
                         inspector_id=schedule.inspector_id, token=secrets.token_urlsafe(20),
                         status='issued', issued_by=actor, issued_at=now, note=(note or '')[:300])
    db.add(claim)
    record(db, actor, 'recheck.claim_issued', {'order_id': order.id, 'schedule_id': schedule.id,
                                               'claim_id': claim.id, 'version': schedule.plan_version})
    db.flush()
    return claim


def _claim_schedule(db, claim):
    schedule = db.get(RecheckSchedule, claim.schedule_id)
    order = db.get(RecheckOrder, claim.order_id)
    return claim, schedule, order


def start_claim(db, claim_id: int, token: str | None, actor: str):
    with SCHEDULER_LOCK:
        return _start_claim(db, claim_id, token, actor)


def _start_claim(db, claim_id: int, token: str | None, actor: str):
    now = time.time()
    db.rollback()
    claim = db.scalar(select(RecheckClaim).where(RecheckClaim.id == claim_id).with_for_update())
    if not claim:
        raise HTTPException(404, '领工记录不存在')
    claim, schedule, order = _claim_schedule(db, claim)
    if token and token != claim.token:
        raise HTTPException(409, {'code': 'claim_invalid', 'message': '领工凭证不匹配，请重新领工。'})
    if claim.status != 'issued':
        raise HTTPException(409, {'code': 'claim_invalid', 'message': f'领工记录已{claim.status}，不能开工。'})
    if not schedule or not order:
        raise HTTPException(409, {'code': 'claim_invalid', 'message': '领工记录对应的排程已不存在。'})
    if (schedule.status not in ('scheduled', 'in_progress') or order.current_schedule_id != schedule.id
            or claim.schedule_version != schedule.plan_version):
        raise HTTPException(409, {'code': 'claim_stale', 'message': '领工记录已失效：排程已变更或作废，请重新领工。'})
    if _instrument_down_now(db, schedule.instrument_id, now):
        raise HTTPException(409, {'code': 'instrument_down', 'message': '仪器正在停机，不能开工。'})
    if now > schedule.test_end:
        raise HTTPException(409, {'code': 'claim_stale', 'message': '排程检测时段已结束，领工记录失效。'})
    if now < schedule.prep_start - 30 * 60:
        raise HTTPException(409, {'code': 'too_early', 'too_early': True,
                                  'message': f'未到排程开始时间（{local_iso(schedule.prep_start)}），暂不能开工。'})
    claim.status = 'started'
    claim.started_at = now
    schedule.status = 'in_progress'
    schedule.started_at = now
    schedule.updated_at = now
    order.status = 'in_progress'
    order.updated_at = now
    record(db, actor, 'recheck.claim_started', {'order_id': order.id, 'schedule_id': schedule.id,
                                                'claim_id': claim.id})
    db.flush()
    return claim


def complete_claim(db, claim_id: int, actor: str):
    with SCHEDULER_LOCK:
        return _complete_claim(db, claim_id, actor)


def _complete_claim(db, claim_id: int, actor: str):
    now = time.time()
    db.rollback()
    claim = db.scalar(select(RecheckClaim).where(RecheckClaim.id == claim_id).with_for_update())
    if not claim:
        raise HTTPException(404, '领工记录不存在')
    claim, schedule, order = _claim_schedule(db, claim)
    if claim.status != 'started':
        raise HTTPException(409, {'code': 'claim_invalid', 'message': '仅已开工的领工记录可以完工。'})
    if not schedule or schedule.status != 'in_progress':
        raise HTTPException(409, {'code': 'claim_stale', 'message': '排程已中断或变更，不能直接完工，请先处置异常。'})
    claim.status = 'completed'
    claim.completed_at = now
    schedule.status = 'completed'
    schedule.finished_at = now
    schedule.updated_at = now
    _deactivate_occupancies(db, schedule)
    order.status = 'completed'
    order.current_schedule_id = schedule.id
    order.updated_at = now
    record(db, actor, 'recheck.completed', {'order_id': order.id, 'schedule_id': schedule.id,
                                            'claim_id': claim.id})
    db.flush()
    return claim


# ---------------------------------------------------------------- 服务恢复
def recover(db):
    """进程中断后的恢复：一致性校验，不重复占位，不丢原排程依据。

    - 已失效排程上的未开工领工记录作废；
    - 当前排程指针为空但工单标记已排程的，转阻塞并记录原因；
    - 预览凭证保持原样（过期由确认时重排）。
    """
    now = time.time()
    counts = {'claims_voided': 0, 'orders_blocked': 0, 'orders_restored': 0}
    for claim in db.scalars(select(RecheckClaim).where(RecheckClaim.status == 'issued')):
        schedule = db.get(RecheckSchedule, claim.schedule_id)
        order = db.get(RecheckOrder, claim.order_id)
        if (not schedule or schedule.status != 'scheduled' or not order
                or order.current_schedule_id != schedule.id
                or claim.schedule_version != schedule.plan_version):
            claim.status = 'voided'
            claim.note = '服务恢复时发现排程已失效，领工记录作废'
            counts['claims_voided'] += 1
    for order in db.scalars(select(RecheckOrder).where(
            RecheckOrder.status.in_(('scheduled', 'in_progress')))):
        schedule = db.get(RecheckSchedule, order.current_schedule_id) if order.current_schedule_id else None
        if not schedule or schedule.status not in OCCUPYING_SCHEDULE_STATUSES:
            # 中断可能丢失指针，但原排程与占位仍有效：优先恢复原排程，不重建、不重复占位。
            schedule = db.scalar(select(RecheckSchedule).where(
                RecheckSchedule.order_id == order.id,
                RecheckSchedule.status.in_(OCCUPYING_SCHEDULE_STATUSES)
            ).order_by(RecheckSchedule.plan_version.desc(), RecheckSchedule.id.desc()).limit(1))
        if schedule and schedule.status in OCCUPYING_SCHEDULE_STATUSES:
            order.current_schedule_id = schedule.id
            order.status = 'in_progress' if schedule.status == 'in_progress' else 'scheduled'
            order.block_reasons = []
            order.updated_at = now
            counts['orders_restored'] = counts.get('orders_restored', 0) + 1
        else:
            order.status = 'blocked'
            order.block_reasons = [{'code': 'schedule_missing',
                                    'message': '服务恢复时未找到有效排程，请重新预排确认。'}]
            order.current_schedule_id = None
            order.updated_at = now
            counts['orders_blocked'] += 1
    if any(counts.values()):
        record(db, 'system', 'recheck.recovered', counts)
    db.flush()
    return counts


# ---------------------------------------------------------------- 序列化
def _resource_dicts(db):
    instruments = {i.id: i for i in db.scalars(select(Instrument))}
    inspectors = {p.id: p for p in db.scalars(select(Inspector))}
    return instruments, inspectors


def schedule_dict(db, schedule: RecheckSchedule, instruments=None, inspectors=None):
    if not schedule:
        return None
    instruments = instruments or {i.id: i for i in db.scalars(select(Instrument))}
    inspectors = inspectors or {p.id: p for p in db.scalars(select(Inspector))}
    ins, insp = instruments.get(schedule.instrument_id), inspectors.get(schedule.inspector_id)
    return {
        'id': schedule.id, 'order_id': schedule.order_id, 'status': schedule.status,
        'plan_version': schedule.plan_version,
        'instrument_id': schedule.instrument_id,
        'instrument': {'id': ins.id, 'code': ins.code, 'name': ins.name} if ins else None,
        'inspector_id': schedule.inspector_id,
        'inspector': {'id': insp.id, 'code': insp.code, 'name': insp.name} if insp else None,
        'prep_start': schedule.prep_start, 'prep_end': schedule.prep_end,
        'test_start': schedule.test_start, 'test_end': schedule.test_end,
        'validity_end': schedule.validity_end,
        'prep_start_local': local_iso(schedule.prep_start), 'prep_end_local': local_iso(schedule.prep_end),
        'test_start_local': local_iso(schedule.test_start), 'test_end_local': local_iso(schedule.test_end),
        'validity_end_local': local_iso(schedule.validity_end),
        'prep_minutes': schedule.prep_minutes, 'test_minutes': schedule.test_minutes,
        'validity_minutes': schedule.validity_minutes,
        'started_at': schedule.started_at, 'started_at_local': local_iso(schedule.started_at),
        'finished_at': schedule.finished_at, 'finished_at_local': local_iso(schedule.finished_at),
        'created_at': schedule.created_at, 'updated_at': schedule.updated_at,
        'basis': schedule.basis or {},
        'validity_ok': schedule.test_end <= schedule.validity_end,
    }


def claim_dict(claim: RecheckClaim, schedules=None, include_token: bool = False):
    schedules = schedules or {}
    schedule = schedules.get(claim.schedule_id)
    data = {
        'id': claim.id, 'order_id': claim.order_id, 'schedule_id': claim.schedule_id,
        'schedule_version': claim.schedule_version, 'inspector_id': claim.inspector_id,
        'status': claim.status, 'issued_by': claim.issued_by,
        'issued_at': claim.issued_at, 'issued_at_local': local_iso(claim.issued_at),
        'started_at': claim.started_at, 'started_at_local': local_iso(claim.started_at),
        'completed_at': claim.completed_at, 'completed_at_local': local_iso(claim.completed_at),
        'note': claim.note,
        'schedule_status': schedule.status if schedule else None,
        'schedule_version_current': schedule.plan_version if schedule else None,
    }
    if include_token:
        data['token'] = claim.token  # 领工凭证只在发放时返回一次
    return data


def downtime_dict(db, downtime: Downtime):
    instruments = {i.id: i for i in db.scalars(select(Instrument))}
    ins = instruments.get(downtime.instrument_id)
    return {
        'id': downtime.id, 'instrument_id': downtime.instrument_id,
        'instrument': {'id': ins.id, 'code': ins.code, 'name': ins.name} if ins else None,
        'start_at': downtime.start_at, 'end_at': downtime.end_at,
        'start_at_local': local_iso(downtime.start_at), 'end_at_local': local_iso(downtime.end_at),
        'reason': downtime.reason, 'created_by': downtime.created_by,
        'created_at': downtime.created_at, 'created_at_local': local_iso(downtime.created_at),
        'resolved_at': downtime.resolved_at, 'resolved_at_local': local_iso(downtime.resolved_at),
        'resolved_by': downtime.resolved_by, 'impact': downtime.impact or [],
    }


def order_dict(db, order: RecheckOrder, detail: bool = False):
    instruments, inspectors = _resource_dicts(db)
    schedule = db.get(RecheckSchedule, order.current_schedule_id) if order.current_schedule_id else None
    data = {
        'id': order.id, 'name': order.name, 'job_id': order.job_id, 'dataset_id': order.dataset_id,
        'batches': order.batches or [], 'items': order.items or [],
        'item_labels': [NAMES.get(i, i) for i in (order.items or [])],
        'status': order.status, 'block_reasons': order.block_reasons or [],
        'current_schedule_id': order.current_schedule_id,
        'current_schedule': schedule_dict(db, schedule, instruments, inspectors) if schedule else None,
        'created_by': order.created_by, 'created_at': order.created_at,
        'created_at_local': local_iso(order.created_at), 'updated_at': order.updated_at,
    }
    if detail:
        schedules = list(db.scalars(select(RecheckSchedule).where(
            RecheckSchedule.order_id == order.id).order_by(RecheckSchedule.plan_version, RecheckSchedule.id)))
        claims = list(db.scalars(select(RecheckClaim).where(
            RecheckClaim.order_id == order.id).order_by(RecheckClaim.id.desc())))
        revisions = list(db.scalars(select(RecheckRevision).where(
            RecheckRevision.order_id == order.id).order_by(RecheckRevision.id.desc())))
        previews = list(db.scalars(select(RecheckPreview).where(
            RecheckPreview.order_id == order.id).order_by(RecheckPreview.id.desc()).limit(20)))
        schedule_map = {s.id: s for s in schedules}
        data.update({
            'schedules': [schedule_dict(db, s, instruments, inspectors) for s in schedules],
            'claims': [claim_dict(c, schedule_map) for c in claims],
            'revisions': [{'id': r.id, 'schedule_id': r.schedule_id, 'version': r.version,
                           'reason': r.reason, 'plan': _plan_for_response(db, r.plan),
                           'basis': r.basis or {}, 'actor': r.actor, 'at': r.at,
                           'at_local': local_iso(r.at)} for r in revisions],
            'previews': [preview_dict(db, p) for p in previews],
        })
    return data


def resource_impact(db, resource_type: str, resource_id: int, actor: str):
    """资源能力/班次/时长/启停变更后处理相关排程。

    - 已开工排程转异常待处置，领工记录中断；
    - 未开工排程取消并自动重排（无可行时段则置阻塞），保留原方案依据。
    """
    with SCHEDULER_LOCK:
        now = time.time()
        db.rollback()
        column = (RecheckSchedule.instrument_id if resource_type == 'instrument'
                  else RecheckSchedule.inspector_id)
        schedules = list(db.scalars(select(RecheckSchedule).where(
            column == resource_id,
            RecheckSchedule.status.in_(('scheduled', 'in_progress'))).order_by(RecheckSchedule.id)))
        touched = []
        for schedule in schedules:
            order = db.scalar(select(RecheckOrder).where(RecheckOrder.id == schedule.order_id).with_for_update())
            if not order or order.current_schedule_id != schedule.id:
                continue
            label = RESOURCE_LABEL.get(resource_type, '资源')
            if schedule.status == 'in_progress' or schedule.started_at:
                schedule.status = 'abnormal'
                schedule.updated_at = now
                schedule.basis = {**(schedule.basis or {}), 'abnormal_reason': 'resource_changed',
                                  'abnormal_at': now}
                order.status = 'abnormal'
                order.block_reasons = [{'code': 'resource_changed',
                                        'message': f'{label}配置变更，已开工检测转异常待处置。'}]
                order.updated_at = now
                _invalidate_claims(db, schedule, note='资源变更，已开工检测转异常待处置')
                touched.append({'order_id': order.id, 'schedule_id': schedule.id, 'action': 'abnormal'})
                record(db, actor, 'recheck.resource_abnormal', {'order_id': order.id,
                                                                'schedule_id': schedule.id,
                                                                'resource_type': resource_type})
            else:
                original = _plan_of_schedule(schedule)
                _invalidate_claims(db, schedule, note='资源变更，原排程已重排')
                _deactivate_occupancies(db, schedule)
                schedule.status = 'cancelled'
                schedule.updated_at = now
                schedule.basis = {**(schedule.basis or {}), 'cancelled_reason': 'resource_changed',
                                  'cancelled_at': now}
                new_schedule, reasons = _replan_from(
                    db, order, actor, now, now, 'resource_changed', schedule.id)
                touched.append({'order_id': order.id, 'schedule_id': schedule.id,
                                'action': 'replanned' if new_schedule else 'blocked',
                                'new_schedule_id': new_schedule.id if new_schedule else None,
                                'original_plan': original,
                                'reasons': reasons if not new_schedule else []})
                record(db, actor, 'recheck.resource_replanned', {
                    'order_id': order.id, 'old_schedule_id': schedule.id,
                    'new_schedule_id': new_schedule.id if new_schedule else None})
        db.flush()
        return touched


def cancel_order(db, order_id: int, actor: str, note: str = ''):
    """取消尚未开工的工单：释放占用、作废领工记录，保留取消依据。"""
    with SCHEDULER_LOCK:
        now = time.time()
        db.rollback()
        order = db.scalar(select(RecheckOrder).where(RecheckOrder.id == order_id).with_for_update())
        if not order:
            raise HTTPException(404, '复检工单不存在')
        if order.status == 'completed':
            raise HTTPException(409, {'code': 'order_completed', 'message': '已完工工单不能取消。'})
        schedule = _active_schedule(db, order)
        if schedule and (schedule.status == 'in_progress' or schedule.started_at):
            raise HTTPException(409, {'code': 'order_started',
                                      'message': '该工单已开工，请先处置为异常或等待完工。'})
        if schedule:
            _invalidate_claims(db, schedule, note='工单取消')
            _deactivate_occupancies(db, schedule)
            schedule.status = 'cancelled'
            schedule.updated_at = now
            schedule.basis = {**(schedule.basis or {}), 'cancelled_reason': 'order_cancelled',
                              'cancelled_at': now, 'cancelled_by': actor, 'note': note}
        order.status = 'cancelled'
        order.current_schedule_id = None
        order.block_reasons = []
        order.updated_at = now
        record(db, actor, 'recheck.order_cancelled', {'order_id': order.id,
                                                      'schedule_id': schedule.id if schedule else None,
                                                      'note': note})
        db.commit()
        return order
