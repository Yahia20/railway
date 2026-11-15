"""API isolation tests: scope is enforced before returning data or signing audio."""
from uuid import uuid4

import pytest
from fastapi import FastAPI
from fastapi.testclient import TestClient

from app.portal import api, auth, passwords


@pytest.fixture
def portal(monkeypatch):
    app=FastAPI();app.include_router(auth.router);app.include_router(api.router)
    employee,other=str(uuid4()),str(uuid4())
    user={'user_id':str(uuid4()),'agent_id':employee,'role':'agent','must_change_password':False}
    app.dependency_overrides[auth.principal]=lambda:user
    queries=[]
    def rows(sql,params=()):
        queries.append((sql,params))
        return []
    monkeypatch.setattr(api.store,'rows',rows)
    return TestClient(app),user,other,queries


@pytest.mark.parametrize('route',['chats','calls','summary'])
def test_employee_cannot_request_another_employee(portal,route):
    client,user,other,queries=portal
    assert client.get('/portal/api/'+route,params={'agent_id':other}).status_code==403
    assert not queries


@pytest.mark.parametrize('route',['team','health','users','calls?unattributed=true'])
def test_employee_cannot_open_team_admin_routes(portal,route):
    client,_,_,queries=portal
    assert client.get('/portal/api/'+route).status_code==403
    assert not queries


@pytest.mark.parametrize('route',['chats/00000000-0000-0000-0000-000000000001','calls/123.45','calls/123.45/audio'])
def test_object_endpoints_scope_query_and_hide_missing(portal,route):
    client,user,_,queries=portal
    assert client.get('/portal/api/'+route).status_code==404
    assert queries and queries[-1][1][-2:]==(user['agent_id'],user['agent_id'])
    assert 'agent_id=%s::uuid' in queries[-1][0]


def test_unattributed_calls_cannot_match_agent(portal):
    client,user,_,queries=portal
    assert client.get('/portal/api/calls').status_code==200
    sql,params=queries[-1]
    # The person comes from v_call_owner (approved mapping, journey for inbound),
    # whose agent_id is NULL for anything unproven, and NULL never equals an id.
    assert 'o.agent_id=%s::uuid' in sql and user['agent_id'] in params
    assert 'v_call_owner' in sql


@pytest.mark.parametrize('route',['calls','summary','team'])
def test_no_call_query_trusts_the_first_ringing_extension(portal,route,monkeypatch):
    client,user,_,queries=portal;user['role']='manager'
    def rows(sql,params=()):
        queries.append((sql,params))
        return [{}]
    monkeypatch.setattr(api.store,'rows',rows)
    assert client.get('/portal/api/'+route).status_code==200
    assert queries
    for sql,_ in queries:
        assert 'l.extension' not in sql and 'j.agent_id' not in sql


def test_leader_can_view_all_but_cannot_create_users(portal):
    client,user,_,queries=portal;user['role']='leader'
    assert client.get('/portal/api/team').status_code==200
    assert client.post('/portal/api/users',json={'username':'sample','role':'manager'}).status_code==403


def test_expired_or_absent_cookie_rejected():
    app=FastAPI();app.include_router(api.router)
    assert TestClient(app).get('/portal/api/chats').status_code==401


def test_password_hash_salted_and_verified():
    a=passwords.password_hash('example-password-123')
    assert a!=passwords.password_hash('example-password-123')
    assert passwords.verify('example-password-123',a)
    assert not passwords.verify('wrong',a)
    assert not passwords.verify('example-password-123',a.replace('16384','1073741824'))


def test_wrong_origin_rejected_before_database():
    app=FastAPI();app.include_router(auth.router)
    r=TestClient(app).post('/portal/api/login',json={'username':'demo','password':'wrong'},headers={'Origin':'https://evil.invalid'})
    assert r.status_code==403


def test_date_window_is_bounded(portal):
    client,_,_,queries=portal
    assert client.get('/portal/api/chats?start=2020-01-01&end=2026-01-01').status_code==422
    assert not queries
