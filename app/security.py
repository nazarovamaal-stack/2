import hashlib
import hmac
import os
from datetime import datetime, timedelta, timezone
from uuid import UUID, uuid4

import jwt
from sqlalchemy import select

from app.db import engine, table
from app.errors import ApiError

SECRET = os.environ.get('JWT_SECRET', 'marketplace-local-development-secret-32-characters')
ISSUER = 'marketplace'


def hash_password(password):
    salt = os.urandom(16)
    digest = hashlib.scrypt(password.encode(), salt=salt, n=16384, r=8, p=1)
    return salt.hex() + ':' + digest.hex()


def verify_password(password, stored):
    salt, digest = stored.split(':')
    actual = hashlib.scrypt(password.encode(), salt=bytes.fromhex(salt), n=16384, r=8, p=1)
    return hmac.compare_digest(actual.hex(), digest)


def tokens(user):
    now = datetime.now(timezone.utc)
    claims = {'sub': str(user['id']), 'role': user['role'], 'iat': now, 'iss': ISSUER}
    access = jwt.encode({**claims, 'type': 'access', 'exp': now + timedelta(minutes=20), 'jti': str(uuid4())}, SECRET, algorithm='HS256')
    refresh = jwt.encode({**claims, 'type': 'refresh', 'exp': now + timedelta(days=14), 'jti': str(uuid4())}, SECRET, algorithm='HS256')
    return {'access_token': access, 'refresh_token': refresh, 'token_type': 'bearer', 'expires_in': 1200}


def decode(token, kind):
    try:
        claims = jwt.decode(token, SECRET, algorithms=['HS256'], issuer=ISSUER, options={'require': ['sub', 'role', 'iat', 'exp', 'type', 'jti']})
        if claims['type'] != kind or claims['role'] not in ('USER', 'SELLER', 'ADMIN'):
            raise jwt.InvalidTokenError()
        claims['user_id'] = UUID(claims['sub'])
        return claims
    except jwt.ExpiredSignatureError:
        code = 'TOKEN_EXPIRED' if kind == 'access' else 'REFRESH_TOKEN_INVALID'
    except (jwt.InvalidTokenError, ValueError, TypeError):
        code = 'TOKEN_INVALID' if kind == 'access' else 'REFRESH_TOKEN_INVALID'
    raise ApiError(401, code, 'Invalid or expired token')


def bootstrap_admin():
    username = os.environ.get('BOOTSTRAP_ADMIN_USERNAME')
    password = os.environ.get('BOOTSTRAP_ADMIN_PASSWORD')
    if not username or not password:
        return
    users = table('users')
    with engine.begin() as conn:
        if not conn.execute(select(users.c.id).where(users.c.username == username)).first():
            conn.execute(users.insert().values(id=uuid4(), username=username, password_hash=hash_password(password), role='ADMIN'))
