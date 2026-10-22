"""Plan §8.2 deterministic speech budget. No audio, speech text or HA calls.

Reserve immediately before playback, after synthesizing in memory and measuring
the actual audio duration. A reservation consumes budget even if playback fails;
this conservative choice prevents retries/restarts from repeating announcements.
Presence must come from a verified occupancy adapter; motion alone is insufficient.
"""
import math
import sqlite3
from datetime import datetime, timedelta,timezone
from zoneinfo import ZoneInfo


KINDS = {'new_habit', 'safety', 'explanation', 'memory_changes', 'correction', 'question',
         'clarification', 'confirmation'}
PROACTIVE = {'new_habit', 'safety'}


def reserve(store, config, *, request_id, at, room, kind, duration_seconds,
            occupied, occupancy_at, user_engaged=False, habit_id=None,execution_id=None):
    """Authorize one playback attempt; does not authorize device execution.

Arguments are internal typed observations, never unrestricted model output.
Caller invokes this for each audio buffer just before playback, not at queue time.
"""
    def deny(reason):
        return {'allowed': False, 'reason': reason, 'execution_enabled': False}

    if not isinstance(at, datetime) or at.tzinfo is None:
        return deny('invalid_time')
    if (not isinstance(request_id, str) or not request_id or len(request_id) > 200 or
            not isinstance(room, str) or room not in {s.get('room') for s in config.get('entities', {}).values()} or
            not isinstance(kind, str) or kind not in KINDS or not isinstance(user_engaged, bool)):
        return deny('invalid_request')
    if (isinstance(duration_seconds, bool) or not isinstance(duration_seconds, (int, float)) or
            not math.isfinite(duration_seconds) or not 0 < duration_seconds <= 8):
        return deny('invalid_or_overlong_audio')
    if occupied is not True:
        return deny('occupancy_not_verified')
    if not isinstance(occupancy_at, datetime) or occupancy_at.tzinfo is None:
        return deny('occupancy_not_verified')
    if not 0 <= (at - occupancy_at).total_seconds() <= 60:
        return deny('occupancy_stale_or_future')
    local = at.astimezone(ZoneInfo(config.get('timezone', 'Asia/Shanghai')))
    if local.hour >= 23 or local.hour < 7:
        return deny('night_silence')
    if (local+timedelta(seconds=duration_seconds)).hour>=23:
        return deny('audio_crosses_night_silence')
    if kind not in PROACTIVE and not user_engaged:
        return deny('user_conversation_required')
    if kind == 'new_habit' and (not isinstance(habit_id, str) or not habit_id):
        return deny('habit_required')
    if store.db.in_transaction:
        return deny('uncommitted_observations')
    # Separate connection + write lock makes daily/first-habit limits atomic
    # across processes. This operational ledger is intentionally outside rollback.
    path = store.db.execute('PRAGMA database_list').fetchone()[2]
    if not path:
        return deny('persistent_database_required')
    connection = sqlite3.connect(path, timeout=5)
    utc_at = at.astimezone(timezone.utc).isoformat()
    day = local.date().isoformat()
    try:
        connection.execute('BEGIN IMMEDIATE')
        if connection.execute('SELECT 1 FROM speech_reservations WHERE id=?', (request_id,)).fetchone():
            return deny('already_reserved')
        latest = connection.execute('SELECT MAX(at) FROM speech_reservations').fetchone()[0]
        if latest and datetime.fromisoformat(latest) > at:
            return deny('clock_moved_backwards')
        if execution_id is not None:
            execution=connection.execute('SELECT status,habit_id,issued_at FROM execution_ledger WHERE id=?',(execution_id,)).fetchone()
            if (kind not in PROACTIVE or not execution or execution[0]!='reserved' or
                    habit_id is not None and execution[1]!=habit_id or
                    not 0<=(at-datetime.fromisoformat(execution[2])).total_seconds()<=30):
                return deny('prepared_execution_not_verified')
        if kind == 'new_habit':
            # A genuine first acknowledged execution is required; a prediction
            # or forged model request cannot manufacture a first-use message.
            executions = connection.execute(
                "SELECT issued_at FROM execution_ledger WHERE habit_id=? AND status='acknowledged' ORDER BY issued_at",
                (habit_id,)).fetchall()
            verified=(len(executions)==0 if execution_id is not None else
                      len(executions)==1 and 0<=(at-datetime.fromisoformat(executions[0][0])).total_seconds()<=60)
            if not verified:
                return deny('first_execution_not_verified')
            if connection.execute("SELECT 1 FROM speech_reservations WHERE kind='new_habit' AND habit_id=?", (habit_id,)).fetchone():
                return deny('habit_already_announced')
        if kind in PROACTIVE:
            count = connection.execute(
                "SELECT COUNT(*) FROM speech_reservations WHERE local_day=? AND kind IN ('new_habit','safety','aborted_new_habit')", (day,)).fetchone()[0]
            if count >= 2:
                return deny('daily_proactive_budget')
        if kind == 'question':
            if connection.execute("SELECT 1 FROM speech_reservations WHERE local_day=? AND kind='question'", (day,)).fetchone():
                return deny('daily_question_budget')
        connection.execute('INSERT INTO speech_reservations VALUES (?,?,?,?,?,?,?)',
                           (request_id, utc_at, day, room, kind, habit_id if kind == 'new_habit' else None, duration_seconds))
        if execution_id is not None:
            connection.execute('INSERT INTO prepared_speech VALUES (?,?,?)',(request_id,execution_id,'prepared'))
        connection.commit()
        return {'allowed': True, 'reason': 'speech_reserved', 'request_id': request_id,
                'local_day': day, 'execution_enabled': False}
    except (sqlite3.Error, ValueError, TypeError):
        return deny('speech_ledger_unavailable')
    finally:
        connection.rollback()
        connection.close()


def claim_prepared(store,config,request_id,at,*,occupied,occupancy_at):
    """Claim one playback only after an observed execution, with fresh silence/occupancy checks."""
    if store.db.in_transaction:
        raise ValueError('Commit before claiming prepared speech')
    store.db.execute('BEGIN IMMEDIATE')
    try:
        row=store.db.execute('SELECT p.execution_id,p.status,s.at,s.duration_seconds FROM prepared_speech p JOIN speech_reservations s ON s.id=p.id WHERE p.id=?',(request_id,)).fetchone()
        if not row or row[1]!='prepared':
            store.db.rollback()
            return {'allowed':False,'reason':'not_prepared_or_already_claimed'}
        local=at.astimezone(ZoneInfo(config.get('timezone','Asia/Shanghai')))
        hour=local.hour
        reason=('night_silence' if hour>=23 or hour<7 else 'audio_crosses_night_silence'
                if (local+timedelta(seconds=row[3])).hour>=23 else 'prepared_speech_expired'
                if not 0<=(at-datetime.fromisoformat(row[2])).total_seconds()<=60 else 'occupancy_not_verified'
                if occupied is not True or not isinstance(occupancy_at,datetime) or occupancy_at.tzinfo is None or
                   not 0<=(at-occupancy_at).total_seconds()<=60 else None)
        execution=store.db.execute('SELECT status FROM execution_ledger WHERE id=?',(row[0],)).fetchone()
        if reason:
            store.db.execute("UPDATE prepared_speech SET status='abandoned' WHERE id=?",(request_id,))
            store.db.commit()
            return dict(allowed=False,reason=reason)
        if not execution or execution[0]!='acknowledged':
            store.db.rollback()
            return {'allowed':False,'reason':'execution_not_observed'}
        store.db.execute("UPDATE prepared_speech SET status='claimed' WHERE id=?",(request_id,))
        store.db.commit()
        return {'allowed':True,'reason':'playback_claimed'}
    except Exception:
        store.db.rollback()
        raise


def abort_prepared(store,request_id):
    """Release the first-habit slot only when HA proves nothing was dispatched."""
    with store.db:
        row=store.db.execute('SELECT e.status FROM prepared_speech p JOIN execution_ledger e ON e.id=p.execution_id WHERE p.id=?',(request_id,)).fetchone()
        if not row or not row[0].startswith('not_dispatched'):
            raise ValueError('Cannot release speech for an uncertain physical outcome')
        store.db.execute("UPDATE prepared_speech SET status='aborted' WHERE id=?",(request_id,))
        store.db.execute("UPDATE speech_reservations SET kind='aborted_new_habit' WHERE id=? AND kind='new_habit'",(request_id,))
