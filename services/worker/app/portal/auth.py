"""Cookie sessions; all writes use the portal-only database role."""
import hashlib
import hmac
import secrets
from uuid import UUID

from fastapi import APIRouter, Depends, HTTPException, Request, Response
from pydantic import BaseModel, Field

from . import passwords, store

router = APIRouter(prefix='/portal/api')
COOKIE = '__Host-tg_session'
_DUMMY = passwords.password_hash(secrets.token_urlsafe(32))


def digest(value):
    return hashlib.sha256(value.encode()).hexdigest()


def same_origin(request: Request):
    origin = request.headers.get('origin')
    if origin and origin.rstrip('/') != str(request.base_url).rstrip('/'):
        raise HTTPException(403, 'Invalid origin')


def session(request: Request):
    token = request.cookies.get(COOKIE, '')
    if not token or len(token) > 128:
        raise HTTPException(401, 'Sign in required')
    with store.connection(auth=True) as conn:
        row = conn.execute('''SELECT u.user_id,u.username,u.role,u.agent_id,u.must_change_password,
          s.csrf_token,s.token_hash FROM portal_auth.sessions s
          JOIN portal_auth.users u USING(user_id)
          WHERE s.token_hash=%s AND NOT s.revoked AND s.expires_at>now() AND u.active''',
          (digest(token),)).fetchone()
    if not row:
        raise HTTPException(401, 'Session expired')
    if request.method not in ('GET', 'HEAD', 'OPTIONS'):
        same_origin(request)
        if not hmac.compare_digest(request.headers.get('x-csrf-token', ''), row['csrf_token']):
            raise HTTPException(403, 'Invalid request token')
    return row


def principal(user=Depends(session)):
    if user['must_change_password']:
        raise HTTPException(403, 'Change your temporary password first')
    return user


def manager(user=Depends(principal)):
    if user['role'] != 'manager':
        raise HTTPException(403, 'Manager access required')
    return user


def public_user(user):
    return {k: user[k] for k in ('user_id','username','role','agent_id','must_change_password','csrf_token')}


class Login(BaseModel):
    username: str = Field(min_length=1, max_length=100)
    password: str = Field(min_length=1, max_length=256)


@router.post('/login')
def login(body: Login, request: Request, response: Response):
    same_origin(request)
    username = body.username.strip().lower()
    # Use the trusted socket peer, not a client-supplied forwarding header.
    ip_hash = digest(request.client.host if request.client else 'unknown')
    user = None
    limited = False
    with store.connection(auth=True) as conn:
        # Serialize attempts per account, including nonexistent accounts.
        conn.execute('SELECT pg_advisory_xact_lock(hashtext(%s))', ('portal-login:' + username,))
        count = conn.execute('''SELECT count(*) AS n FROM portal_auth.login_attempts
          WHERE created_at>now()-interval '15 minutes' AND NOT succeeded
          AND (username=%s OR ip_hash=%s)''', (username, ip_hash)).fetchone()['n']
        if count >= 10:
            limited = True
        else:
            candidate = conn.execute('SELECT * FROM portal_auth.users WHERE username=%s', (username,)).fetchone()
            valid = passwords.verify(body.password, candidate['password_hash'] if candidate else _DUMMY)
            if candidate and candidate['active'] and valid:
                user = candidate
            conn.execute('INSERT INTO portal_auth.login_attempts(username,ip_hash,succeeded) VALUES(%s,%s,%s)',
                         (username,ip_hash,user is not None))
            if user:
                token, csrf = secrets.token_urlsafe(32), secrets.token_urlsafe(32)
                conn.execute('''INSERT INTO portal_auth.sessions(token_hash,user_id,csrf_token,expires_at)
                   VALUES(%s,%s,%s,now()+interval '12 hours')''', (digest(token),user['user_id'],csrf))
                user['csrf_token'] = csrf
    if limited:
        raise HTTPException(429, 'Try again in 15 minutes', headers={'Retry-After':'900'})
    if not user:
        raise HTTPException(401, 'Invalid username or password')
    response.set_cookie(COOKIE, token, max_age=43200, secure=True, httponly=True, samesite='strict', path='/')
    return public_user(user)


@router.get('/me')
def me(user=Depends(session)):
    return public_user(user)


@router.post('/logout')
def logout(response: Response, user=Depends(session)):
    with store.connection(auth=True) as conn:
        conn.execute('UPDATE portal_auth.sessions SET revoked=true WHERE token_hash=%s', (user['token_hash'],))
    response.delete_cookie(COOKIE, secure=True, httponly=True, samesite='strict', path='/')
    return {'ok':True}


class PasswordChange(BaseModel):
    current_password: str = Field(max_length=256)
    new_password: str = Field(min_length=12, max_length=256)


@router.post('/password')
def change_password(body: PasswordChange, response: Response, user=Depends(session)):
    with store.connection(auth=True) as conn:
        old = conn.execute('SELECT password_hash FROM portal_auth.users WHERE user_id=%s FOR UPDATE',
                           (user['user_id'],)).fetchone()
        if not passwords.verify(body.current_password, old['password_hash']):
            raise HTTPException(403, 'Current password is incorrect')
        conn.execute('UPDATE portal_auth.users SET password_hash=%s,must_change_password=false WHERE user_id=%s',
                     (passwords.password_hash(body.new_password), user['user_id']))
        conn.execute('UPDATE portal_auth.sessions SET revoked=true WHERE user_id=%s', (user['user_id'],))
        conn.execute("INSERT INTO portal_auth.audit(actor_id,action,subject_id) VALUES(%s,'password_change',%s)",
                     (user['user_id'],user['user_id']))
    response.delete_cookie(COOKIE, secure=True, httponly=True, samesite='strict', path='/')
    return {'ok':True,'sign_in_again':True}


class NewUser(BaseModel):
    username: str = Field(pattern=r'^[a-z0-9][a-z0-9._-]{2,99}$')
    role: str = Field(pattern=r'^(agent|leader|manager)$')
    agent_id: UUID | None = None


@router.get('/users')
def users(user=Depends(manager)):
    with store.connection(auth=True) as conn:
        return conn.execute('''SELECT user_id,username,role,agent_id,active,must_change_password
          FROM portal_auth.users ORDER BY username''').fetchall()


@router.post('/users')
def create_user(body: NewUser, user=Depends(manager)):
    if body.role == 'agent' and body.agent_id is None:
        raise HTTPException(422, 'An agent account needs an employee')
    if body.agent_id and not store.rows('SELECT agent_id FROM agents WHERE agent_id=%s AND is_active AND NOT is_bot', (body.agent_id,)):
        raise HTTPException(422, 'Employee not found')
    temporary = secrets.token_urlsafe(18)
    with store.connection(auth=True) as conn:
        row = conn.execute('''INSERT INTO portal_auth.users(username,password_hash,role,agent_id)
          VALUES(%s,%s,%s,%s) ON CONFLICT(username) DO NOTHING RETURNING user_id''',
          (body.username,passwords.password_hash(temporary),body.role,body.agent_id)).fetchone()
        if not row:
            raise HTTPException(409, 'Username already exists')
        conn.execute("INSERT INTO portal_auth.audit(actor_id,action,subject_id) VALUES(%s,'create_user',%s)",
                     (user['user_id'],row['user_id']))
    return {'user_id':row['user_id'],'temporary_password':temporary}


@router.post('/users/{user_id}/disable')
def disable_user(user_id: UUID, user=Depends(manager)):
    if str(user_id) == str(user['user_id']):
        raise HTTPException(409, 'Cannot disable your own account')
    with store.connection(auth=True) as conn:
        conn.execute('UPDATE portal_auth.users SET active=false WHERE user_id=%s', (user_id,))
        conn.execute('UPDATE portal_auth.sessions SET revoked=true WHERE user_id=%s', (user_id,))
        conn.execute("INSERT INTO portal_auth.audit(actor_id,action,subject_id) VALUES(%s,'disable_user',%s)",
                     (user['user_id'],user_id))
    return {'ok':True}


@router.post('/users/{user_id}/reset')
def reset_user(user_id: UUID, user=Depends(manager)):
    temporary = secrets.token_urlsafe(18)
    with store.connection(auth=True) as conn:
        row = conn.execute('''UPDATE portal_auth.users SET password_hash=%s,must_change_password=true
          WHERE user_id=%s RETURNING user_id''', (passwords.password_hash(temporary),user_id)).fetchone()
        if not row:
            raise HTTPException(404, 'Account not found')
        conn.execute('UPDATE portal_auth.sessions SET revoked=true WHERE user_id=%s', (user_id,))
        conn.execute("INSERT INTO portal_auth.audit(actor_id,action,subject_id) VALUES(%s,'reset_password',%s)",
                     (user['user_id'],user_id))
    return {'temporary_password':temporary}
