"""Approved observation scale. Models supply observations, never numeric scores."""
from collections import Counter
import json
import math
from pathlib import Path

RUBRIC=json.loads((Path(__file__).parent/'rubric.json').read_text(encoding='utf-8'))
CRITICAL={'misinformation','unauthorized_promise','financial_error','privacy_breach','procedure_violation'}
REFERENCE_ITEMS={'accuracy','procedure'}
NA_ALLOWED={'greeting','understanding','listening','probing','empathy','customer_name','call_control',
            'language','accuracy','procedure','solution','conditions','objections','expectations','next_step','closing'}


def evidence_valid(evidence,segments,*,agent_only=False):
    if not isinstance(evidence,list) or not evidence:return False
    by_id={str(s['id']):s for s in segments}
    for ev in evidence:
        if not isinstance(ev,dict):return False
        segment=by_id.get(str(ev.get('segment_id')))
        quote=ev.get('quote')
        if not segment or not isinstance(quote,str) or not quote.strip():return False
        if ev.get('speaker')!=segment['speaker'] or quote not in segment['text']:return False
        if agent_only and segment['speaker']!='agent':return False
    return True


def observation(obs,segments,complete):
    if not isinstance(obs,dict):return 'review'
    value=obs.get('value')
    if value not in ('yes','no','na','unobservable'):return 'review'
    if value=='unobservable':return 'review'
    evidence=obs.get('evidence',[])
    # Even unused quotes must be valid; never silently retain invented evidence.
    if evidence and not evidence_valid(evidence,segments):return 'review'
    if value=='na':
        return 'na' if obs.get('reason') and complete and (evidence or obs.get('basis')=='absence') else 'review'
    if obs.get('basis')=='absence':
        return value if complete and obs.get('reason') and obs.get('reviewed_full_context') is True else 'review'
    return value if evidence_valid(evidence,segments,agent_only=True) else 'review'


def score(transcript,readings,*,reference_verified=False):
    segments=transcript.get('segments',[])
    complete=transcript.get('complete') is True and not transcript.get('quality_flags')
    items=[];earned=0.0;assessed=0;flags=[]
    for rule in RUBRIC['items']:
        resolved=[];lines=set()
        for index in range(5):
            votes=[]
            for reading in readings[:3]:
                observations=reading.get('items',{}).get(rule['id'],[])
                obs=observations[index] if isinstance(observations,list) and len(observations)>index else None
                value=observation(obs,segments,complete)
                votes.append(value)
                if value!='review' and isinstance(obs,dict):
                    lines.update(str(e['segment_id']) for e in obs.get('evidence',[]))
            counts=Counter(votes)
            winners=[v for v,n in counts.items() if n>=2 and v!='review']
            resolved.append(winners[0] if winners else 'review')
        if rule['id'] in REFERENCE_ITEMS and not reference_verified:resolved=['review']*5
        # A diarization overlap is not proof of an interruption on mono audio.
        if rule['id']=='listening' and transcript.get('overlap_uncertain'):resolved[3]='review'
        applicable=[v for v in resolved if v!='na']
        if 'review' in resolved:
            value=None;status='review'
        elif not applicable:
            value=None;status='na'
        else:
            value=math.floor(5*applicable.count('yes')/len(applicable)+0.5);status='scored'
            assessed+=rule['weight'];earned+=rule['weight']*value/5
        items.append({'id':rule['id'],'label':rule['label'],'weight':rule['weight'],
                      'score':value,'status':status,'checks':resolved,'lines':sorted(lines)})
    for reading in readings[:3]:
        for flag in reading.get('critical_errors',[]):
            if not isinstance(flag,dict) or flag.get('kind') not in CRITICAL:continue
            if not evidence_valid(flag.get('evidence'),segments,agent_only=True):continue
            # These are management review flags, never an automatic zero.
            key=(flag['kind'],tuple(str(e['segment_id']) for e in flag['evidence']))
            if any((f['kind'],tuple(f['lines']))==key for f in flags):continue
            flags.append({'kind':flag['kind'],'reason':flag.get('reason',''),
                          'lines':list(key[1]),'status':'review',
                          'reference_required':flag['kind'] in {'misinformation','financial_error','procedure_violation'}})
    return {'score':round(100*earned/assessed,1) if assessed else None,'assessed_weight':assessed,
            'items':items,'critical_errors':flags,'rubric_version':RUBRIC['version'],
            'reviews_open':sum(i['status']=='review' for i in items)}
