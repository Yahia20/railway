"""Every object lookup applies the authenticated employee scope in SQL."""
from datetime import date, timedelta
from pathlib import Path
from uuid import UUID

from fastapi import APIRouter, Depends, HTTPException, Query
from fastapi.responses import FileResponse

from . import store
from .auth import principal

router = APIRouter(prefix='/portal')
STATIC = Path(__file__).parent / 'static'
# Every call query starts here: calls long enough to be a conversation (the
# archive's own 30 s rule), the person resolved live from the approved mapping
# (v_call_owner, migration 036), never from call_log.extension.
CALLS = '''FROM call_log l JOIN v_call_owner o USING(uniqueid)
      LEFT JOIN call_processing_jobs j USING(uniqueid) LEFT JOIN call_grades g USING(uniqueid)'''
CONVERSATION = 'coalesce(l.duration_seconds,0) >= 30'


def scope(user, requested=None):
    if user['role'] == 'agent':
        if not user['agent_id'] or (requested and str(requested) != str(user['agent_id'])):
            raise HTTPException(403, 'Employee access denied')
        return str(user['agent_id'])
    return str(requested) if requested else None


def window(start: date | None = None, end: date | None = None):
    end = end or date.today()
    start = start or end - timedelta(days=27)
    if end < start or (end-start).days > 366:
        raise HTTPException(422, 'Choose a date range of up to one year')
    return start,end+timedelta(days=1)


@router.get('', include_in_schema=False)
@router.get('/', include_in_schema=False)
def page():
    return FileResponse(STATIC/'index.html', headers={
        'Cache-Control':'no-store','X-Frame-Options':'DENY','Referrer-Policy':'no-referrer',
        'Content-Security-Policy':"default-src 'self'; script-src 'self'; style-src 'self'; img-src 'self' data:; media-src https:; connect-src 'self'; frame-ancestors 'none'; base-uri 'none'; form-action 'self'"})


@router.get('/api/agents')
def agents(user=Depends(principal)):
    employee=scope(user)
    return store.rows('''SELECT agent_id,full_name,team,qa_excluded FROM agents
      WHERE is_active AND NOT is_bot AND (%s::uuid IS NULL OR agent_id=%s::uuid)
      ORDER BY full_name''',(employee,employee))


@router.get('/api/summary')
def summary(start: date | None=None,end: date | None=None,agent_id: UUID | None=None,user=Depends(principal)):
    employee=scope(user,agent_id)
    lo,hi=window(start,end)
    chats=store.rows('''SELECT count(*) AS total,
      count(*) FILTER(WHERE g.status='scored') AS graded,
      count(*) FILTER(WHERE g.status='scored' AND g.attribution='single' AND g.score IS NOT NULL) AS numeric,
      count(*) FILTER(WHERE g.attribution IN ('shared','uncertain')) AS shared
      FROM interactions i LEFT JOIN qa_grades_v6 g USING(interaction_id)
      WHERE i.external_source='bitrix_chat_api' AND i.started_at >= %s::date AT TIME ZONE 'Asia/Riyadh'
      AND i.started_at < %s::date AT TIME ZONE 'Asia/Riyadh'
      AND (%s::uuid IS NULL OR coalesce(g.agent_id,i.agent_id)=%s::uuid)''',(lo,hi,employee,employee))[0]
    calls=store.rows(f'''SELECT count(*) AS total,count(g.uniqueid) AS graded,
      count(*) FILTER(WHERE o.agent_id IS NULL) AS unattributed,
      count(*) FILTER(WHERE j.status='failed') AS failed,
      round(coalesce(sum(l.duration_seconds),0)/60.0) AS minutes
      {CALLS} WHERE {CONVERSATION} AND l.calldate_local >= %s AND l.calldate_local < %s
      AND (%s::uuid IS NULL OR o.agent_id=%s::uuid)''',(lo,hi,employee,employee))[0]
    weekly=store.rows('''SELECT date_trunc('week',i.started_at AT TIME ZONE 'Asia/Riyadh')::date AS week,
      round(avg(g.score),1) AS score,count(g.score) AS count
      FROM qa_grades_v6 g JOIN interactions i USING(interaction_id)
      WHERE g.status='scored' AND g.attribution='single'
      AND i.started_at >= %s::date AT TIME ZONE 'Asia/Riyadh' AND i.started_at < %s::date AT TIME ZONE 'Asia/Riyadh'
      AND (%s::uuid IS NULL OR g.agent_id=%s::uuid) GROUP BY 1 ORDER BY 1''',(lo,hi,employee,employee))
    call_weekly=store.rows(f'''SELECT date_trunc('week',l.calldate_local)::date AS week,
      round(avg(g.score),1) AS score,count(g.score) AS count
      {CALLS} WHERE {CONVERSATION} AND o.agent_id IS NOT NULL AND l.calldate_local >= %s AND l.calldate_local < %s
      AND (%s::uuid IS NULL OR o.agent_id=%s::uuid) GROUP BY 1 ORDER BY 1''',(lo,hi,employee,employee))
    return {'chats':chats,'calls':calls,'weekly_chats':weekly,'weekly_calls':call_weekly,
            'start':lo,'end':hi-timedelta(days=1)}


@router.get('/api/chats')
def chats(start:date|None=None,end:date|None=None,agent_id:UUID|None=None,
          offset:int=Query(0,ge=0,le=100000),user=Depends(principal)):
    employee=scope(user,agent_id);lo,hi=window(start,end)
    return store.rows('''SELECT i.interaction_id,i.started_at,g.status,g.score,g.attribution,g.title,
      g.chat_type,g.reviews_open,g.loss_reason,coalesce(g.agent_id,i.agent_id) AS agent_id
      FROM interactions i LEFT JOIN qa_grades_v6 g USING(interaction_id)
      WHERE i.external_source='bitrix_chat_api' AND i.started_at >= %s::date AT TIME ZONE 'Asia/Riyadh'
      AND i.started_at < %s::date AT TIME ZONE 'Asia/Riyadh'
      AND (%s::uuid IS NULL OR coalesce(g.agent_id,i.agent_id)=%s::uuid)
      ORDER BY i.started_at DESC,i.interaction_id LIMIT 50 OFFSET %s''',(lo,hi,employee,employee,offset))


@router.get('/api/chats/{interaction_id}')
def chat(interaction_id:UUID,user=Depends(principal)):
    employee=scope(user)
    rows=store.rows('''SELECT g.interaction_id,g.status,g.score,g.attribution,g.result,g.transcript,
      g.title,g.reviews_open,g.evaluated_at,g.prompt_version,g.engine_version
      FROM qa_grades_v6 g WHERE g.interaction_id=%s AND (%s::uuid IS NULL OR g.agent_id=%s::uuid)''',
      (interaction_id,employee,employee))
    if not rows: raise HTTPException(404,'Chat not found')
    return rows[0]


@router.get('/api/calls')
def calls(start:date|None=None,end:date|None=None,agent_id:UUID|None=None,unattributed:bool=False,
          offset:int=Query(0,ge=0,le=100000),user=Depends(principal)):
    employee=scope(user,agent_id);lo,hi=window(start,end)
    if unattributed and user['role']=='agent': raise HTTPException(403,'Team access required')
    return store.rows(f'''SELECT l.uniqueid,l.calldate_local,l.call_type,l.duration_seconds,j.status,
      o.agent_id,a.full_name AS employee,o.reason AS attribution_reason,j.audio_seconds,
      g.score,g.assessed_weight,g.critical_errors
      {CALLS} LEFT JOIN agents a ON a.agent_id=o.agent_id
      WHERE {CONVERSATION} AND l.calldate_local >= %s AND l.calldate_local < %s
      AND (%s::uuid IS NULL OR o.agent_id=%s::uuid) AND (NOT %s OR o.agent_id IS NULL)
      ORDER BY l.calldate_local DESC,l.uniqueid LIMIT 50 OFFSET %s''',(lo,hi,employee,employee,unattributed,offset))


def call_record(uniqueid,user):
    employee=scope(user)
    rows=store.rows('''SELECT j.uniqueid,o.agent_id,j.status,o.reason AS attribution_reason,j.audio_seconds,
      j.stereo_key,j.transcript,j.metrics,j.model_versions,j.content_purged_at,g.score,g.assessed_weight,
      g.reviews_open,g.items,g.critical_errors,g.rubric_version,g.evaluated_at
      FROM call_processing_jobs j JOIN v_call_owner o USING(uniqueid) LEFT JOIN call_grades g USING(uniqueid)
      WHERE j.uniqueid=%s AND (%s::uuid IS NULL OR o.agent_id=%s::uuid)''',(uniqueid,employee,employee))
    if not rows: raise HTTPException(404,'Call not found')
    return rows[0]


@router.get('/api/calls/{uniqueid}')
def call(uniqueid:str,user=Depends(principal)):
    row=call_record(uniqueid,user)
    row['audio_available']=bool(row.pop('stereo_key')) and not row['content_purged_at']
    return row


@router.get('/api/calls/{uniqueid}/audio')
def audio(uniqueid:str,user=Depends(principal)):
    row=call_record(uniqueid,user)
    if not row['stereo_key'] or row['content_purged_at']: raise HTTPException(404,'Recording unavailable')
    from ..calls_archive.api import storage
    return {'url':storage().presign(row['stereo_key'],expires=120),'expires_in':120}


@router.get('/api/rubrics')
def rubrics(user=Depends(principal)):
    import json
    from ..qa.v6.scorecard import RUBRIC
    return {'chat':RUBRIC,'call':json.loads((Path(__file__).parents[1]/'call_qa'/'rubric.json').read_text(encoding='utf-8'))}


def team_access(user):
    if user['role']=='agent':raise HTTPException(403,'Team access required')


@router.get('/api/team')
def team(start:date|None=None,end:date|None=None,user=Depends(principal)):
    team_access(user);lo,hi=window(start,end)
    return store.rows(f'''WITH chats AS (
      SELECT g.agent_id,round(avg(g.score) FILTER(WHERE g.attribution='single'),1) AS chat_score,
      count(*) AS chat_count,count(g.score) FILTER(WHERE g.attribution='single') AS chat_numeric
      FROM qa_grades_v6 g JOIN interactions i USING(interaction_id)
      WHERE g.status='scored' AND i.started_at >= %s::date AT TIME ZONE 'Asia/Riyadh'
      AND i.started_at < %s::date AT TIME ZONE 'Asia/Riyadh' GROUP BY g.agent_id),
      calls AS (SELECT o.agent_id,round(avg(g.score),1) AS call_score,count(*) AS call_count,
      count(g.score) AS call_graded,round(coalesce(sum(l.duration_seconds),0)/60.0) AS call_minutes,
      count(*) FILTER(WHERE jsonb_array_length(coalesce(g.critical_errors,'[]'::jsonb))>0) AS critical_count
      {CALLS} WHERE {CONVERSATION} AND o.agent_id IS NOT NULL AND l.calldate_local >= %s AND l.calldate_local < %s GROUP BY o.agent_id)
      SELECT a.agent_id,a.full_name,a.team,a.qa_excluded,c.chat_score,coalesce(c.chat_count,0) AS chat_count,
      coalesce(c.chat_numeric,0) AS chat_numeric,k.call_score,coalesce(k.call_count,0) AS call_count,
      coalesce(k.call_graded,0) AS call_graded,coalesce(k.call_minutes,0) AS call_minutes,
      coalesce(k.critical_count,0) AS critical_count FROM agents a
      LEFT JOIN chats c USING(agent_id) LEFT JOIN calls k USING(agent_id)
      WHERE a.is_active AND NOT a.is_bot ORDER BY a.full_name''',(lo,hi,lo,hi))


@router.get('/api/health')
def health(user=Depends(principal)):
    team_access(user)
    return {'recordings':store.rows('SELECT status,count(*) AS count FROM call_recording_jobs GROUP BY status ORDER BY 2 DESC'),
      'journeys':store.rows("SELECT coalesce(verdict,status) AS status,count(*) AS count FROM call_journeys GROUP BY 1 ORDER BY 2 DESC"),
      'owners':store.rows("SELECT reason AS status,count(*) AS count FROM v_call_owner GROUP BY 1 ORDER BY 2 DESC"),
      'processing':store.rows('SELECT status,count(*) AS count FROM call_processing_jobs GROUP BY status ORDER BY 2 DESC'),
      'spend':store.rows('SELECT * FROM v_call_lane_spend')[0],
      'config':store.rows("SELECT key,value FROM call_pipeline_config WHERE key LIKE '%%mode'")}
