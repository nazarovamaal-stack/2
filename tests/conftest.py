from uuid import uuid4

import pytest
from fastapi.testclient import TestClient

from app.db import engine, table
from app.main import app
from app.security import hash_password


@pytest.fixture(scope='session')
def client():
    with TestClient(app) as client:
        yield client


@pytest.fixture
def account(client):
    def create(role='USER'):
        username = str(uuid4())
        password = 'test-password'
        if role == 'ADMIN':
            with engine.begin() as conn:
                conn.execute(table('users').insert().values(id=uuid4(), username=username, password_hash=hash_password(password), role=role))
        else:
            response = client.post('/auth/register', json={'username': username, 'password': password, 'role': role})
            assert response.status_code == 201, response.text
        response = client.post('/auth/login', json={'username': username, 'password': password})
        assert response.status_code == 200, response.text
        return {'Authorization': 'Bearer ' + response.json()['access_token']}
    return create


@pytest.fixture
def seller(account):
    return account('SELLER')


@pytest.fixture
def user(account):
    return account()


@pytest.fixture
def admin(account):
    return account('ADMIN')


@pytest.fixture
def product(client, seller):
    def create(**values):
        body = {'name': 'Product', 'description': None, 'price': 100, 'stock': 10, 'category': str(uuid4()), 'status': 'ACTIVE', **values}
        response = client.post('/products', json=body, headers=seller)
        assert response.status_code == 201, response.text
        return response.json()
    return create
