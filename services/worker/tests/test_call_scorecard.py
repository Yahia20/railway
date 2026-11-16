from copy import deepcopy
from app.call_qa.scorecard import RUBRIC,evidence_valid,score

TRANSCRIPT={'complete':True,'segments':[{'id':'1','speaker':'agent','text':'Hello, how can I help?'},
                                      {'id':'2','speaker':'customer','text':'I need a flight.'}]}


def reading(value='yes'):
    observation={'value':value,'basis':'quote','reason':'fixture','evidence':[
        {'segment_id':'1','speaker':'agent','quote':'how can I help?'}]}
    return {'items':{r['id']:[deepcopy(observation) for _ in range(5)] for r in RUBRIC['items']}}


def test_customer_quote_cannot_prove_agent_behavior():
    obs={'segment_id':'2','speaker':'customer','quote':'I need a flight.'}
    assert evidence_valid([obs],TRANSCRIPT['segments'])
    assert not evidence_valid([obs],TRANSCRIPT['segments'],agent_only=True)
    obs['speaker']='agent'
    assert not evidence_valid([obs],TRANSCRIPT['segments'])


def test_score_computed_from_observations_not_model_number():
    r=reading();r['score']=0
    result=score(TRANSCRIPT,[r,r,r],reference_verified=True)
    assert result['score']==100 and result['assessed_weight']==100


def test_majority_and_zero_are_real():
    result=score(TRANSCRIPT,[reading('no'),reading('no'),reading('yes')],reference_verified=True)
    assert result['score']==0 and result['assessed_weight']==100


def test_null_not_zero_and_renormalize():
    r=reading()
    for obs in r['items']['greeting']:obs['value']='na'
    result=score(TRANSCRIPT,[r,r,r],reference_verified=True)
    assert result['items'][0]['score'] is None
    assert result['assessed_weight']==92 and result['score']==100


def test_all_inapplicable_has_no_score():
    r=reading('na')
    result=score(TRANSCRIPT,[r,r,r],reference_verified=True)
    assert result['score'] is None and result['assessed_weight']==0


def test_unverified_reference_items_go_to_review():
    result=score(TRANSCRIPT,[reading()]*3)
    assert all(i['status']=='review' for i in result['items'] if i['id'] in ('accuracy','procedure'))


def test_invented_quote_rejected():
    r=reading()
    r['items']['greeting'][0]['evidence'][0]['quote']='not in the transcript'
    result=score(TRANSCRIPT,[r]*3,reference_verified=True)
    assert result['items'][0]['status']=='review'


def test_no_majority_is_review():
    result=score(TRANSCRIPT,[reading('yes'),reading('no'),reading('unobservable')],reference_verified=True)
    assert result['score'] is None


def test_incomplete_audio_cannot_prove_absence():
    r=reading()
    r['items']['greeting'][0]={'value':'no','basis':'absence','reason':'missing greeting','reviewed_full_context':True}
    t={**TRANSCRIPT,'complete':False}
    assert score(t,[r]*3,reference_verified=True)['items'][0]['status']=='review'


def test_critical_flag_does_not_zero_score():
    r=reading();r['critical_errors']=[{'kind':'privacy_breach','evidence':r['items']['greeting'][0]['evidence']}]
    result=score(TRANSCRIPT,[r]*3,reference_verified=True)
    assert result['score']==100 and len(result['critical_errors'])==1


def test_reference_items_are_not_asked_and_still_go_to_review():
    from app.call_qa.engine import rubric_text
    text=rubric_text()
    assert '- accuracy:' not in text and '- procedure:' not in text
    assert '- greeting:' in text
    r=reading()
    for item in ('accuracy','procedure'):
        del r['items'][item]                      # the model was never asked
    result=score(TRANSCRIPT,[r,r,r])
    status={i['id']:i['status'] for i in result['items']}
    assert status['accuracy']=='review' and status['procedure']=='review'
    assert result['assessed_weight']==82
