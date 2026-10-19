set -euo pipefail
cd "$(dirname "$0")/.."

printf '\n1. Build and start\n'
docker compose up --build -d

printf '\n2. Contract, CRUD, transactions, authorization and concurrency tests\n'
docker compose exec -T api pytest -q

printf '\n3. Generated code reproducibility\n'
docker compose exec -T api python - <<'PY'
import hashlib
import subprocess
import sys
from pathlib import Path


def snapshot():
    return {p.name: hashlib.sha256(p.read_bytes()).hexdigest() for p in Path('app/generated').glob('*.py')}


before = snapshot()
subprocess.run([sys.executable, 'scripts/generate.py'], check=True)
assert before == snapshot(), 'Generated code is not reproducible'
print('Generated models and handlers are reproducible')
PY

printf '\n4. HTTP E2E and database SELECTs\n'
docker compose exec -T api python - <<'PY'
import json
import os
import time
from datetime import datetime, timedelta, timezone
from uuid import UUID, uuid4

import httpx
from sqlalchemy import text

from app.db import engine

run_id = uuid4().hex[:12]
client = httpx.Client(base_url='http://localhost:8000', timeout=15)
for attempt in range(30):
    try:
        if client.get('/openapi.json').status_code == 200:
            break
    except httpx.TransportError:
        pass
    time.sleep(1)
else:
    raise RuntimeError('API did not become ready')


def call(method, path, expected=200, headers=None, error=None, **kwargs):
    response = client.request(method, path, headers=headers, **kwargs)
    assert response.status_code == expected, f'{method} {path}: {response.status_code}, {response.text}'
    UUID(response.headers['X-Request-Id'])
    result = response.json() if response.content else None
    if error:
        assert result['error_code'] == error, result
    print(f'{method} {path} -> {expected}' + (f' {error}' if error else ''))
    return result


def account(role, suffix):
    credentials = {'username': f'pipeline_{run_id}_{suffix}', 'password': 'pipeline-password'}
    user = call('POST', '/auth/register', 201, json={**credentials, 'role': role})
    pair = call('POST', '/auth/login', json=credentials)
    return user, {'Authorization': 'Bearer ' + pair['access_token']}, pair


def query(sql, parameters=None):
    with engine.connect() as conn:
        rows = [dict(row) for row in conn.execute(text(sql), parameters or {}).mappings()]
    print(sql)
    print(json.dumps(rows, default=str, ensure_ascii=False))
    return rows


seller, seller_headers, _ = account('SELLER', 'seller')
buyer, buyer_headers, pair = account('USER', 'buyer')
other, other_headers, _ = account('USER', 'other')
admin_pair = call('POST', '/auth/login', json={'username': os.environ['BOOTSTRAP_ADMIN_USERNAME'], 'password': os.environ['BOOTSTRAP_ADMIN_PASSWORD']})
admin_headers = {'Authorization': 'Bearer ' + admin_pair['access_token']}

call('GET', '/products', 401, error='TOKEN_INVALID')
call('GET', '/products', 401, headers={'Authorization': 'Bearer invalid'}, error='TOKEN_INVALID')
call('POST', '/auth/refresh', json={'refresh_token': pair['refresh_token']})
call('POST', '/auth/refresh', 401, json={'refresh_token': pair['access_token']}, error='REFRESH_TOKEN_INVALID')

product_body = {'name': 'Pipeline keyboard', 'description': None, 'price': 1000, 'stock': 10, 'category': 'pipeline_' + run_id, 'status': 'ACTIVE'}
call('POST', '/products', 403, headers=buyer_headers, json=product_body, error='ACCESS_DENIED')
call('POST', '/products', 400, headers=seller_headers, json={**product_body, 'name': ''}, error='VALIDATION_ERROR')
product = call('POST', '/products', 201, headers=seller_headers, json=product_body)
product_path = '/products/' + product['id']
page = call('GET', '/products', headers=buyer_headers, params={'category': product_body['category'], 'status': 'ACTIVE', 'page': 0, 'size': 20})
assert page['totalElements'] == 1 and page['items'][0]['id'] == product['id']

now = datetime.now(timezone.utc)
promo_body = {'code': 'PIPE_' + run_id.upper(), 'discount_type': 'PERCENTAGE', 'discount_value': 20, 'min_order_amount': 2000, 'max_uses': 5, 'valid_from': (now-timedelta(days=1)).isoformat(), 'valid_until': (now+timedelta(days=1)).isoformat()}
promo = call('POST', '/promo-codes', 201, headers=seller_headers, json=promo_body)
items = [{'product_id': product['id'], 'quantity': 3}]
call('POST', '/orders', 403, headers=seller_headers, json={'items': items}, error='ACCESS_DENIED')
call('POST', '/orders', 409, headers=other_headers, json={'items': [{'product_id': product['id'], 'quantity': 999}]}, error='INSUFFICIENT_STOCK')
call('POST', '/orders', 422, headers=other_headers, json={'items': items, 'promo_code': 'MISSING'}, error='PROMO_CODE_INVALID')
assert call('GET', product_path, headers=buyer_headers)['stock'] == 10

order = call('POST', '/orders', 201, headers=buyer_headers, json={'items': items, 'promo_code': promo['code']})
order_path = '/orders/' + order['id']
assert order['total_amount'] == 2400 and order['discount_amount'] == 600
assert call('GET', product_path, headers=buyer_headers)['stock'] == 7
call('GET', order_path, 403, headers=other_headers, error='ORDER_OWNERSHIP_VIOLATION')
call('GET', order_path, headers=admin_headers)

updated = call('PUT', order_path, headers=buyer_headers, json={'items': [{'product_id': product['id'], 'quantity': 2}]})
assert updated['total_amount'] == 1600 and updated['discount_amount'] == 400
assert call('GET', product_path, headers=buyer_headers)['stock'] == 8
call('PUT', product_path, headers=seller_headers, json={**product_body, 'price': 1500, 'stock': 8})
snapshot = call('GET', order_path, headers=buyer_headers)
assert snapshot['items'][0]['price_at_order'] == 1000 and snapshot['total_amount'] == 1600

query('SELECT id, username, role FROM users WHERE id IN (:seller, :buyer)', {'seller': UUID(seller['id']), 'buyer': UUID(buyer['id'])})
rows = query('SELECT id, price, stock, status, created_at, updated_at FROM products WHERE id = :id', {'id': UUID(product['id'])})
assert rows[0]['stock'] == 8
query('SELECT * FROM orders WHERE id = :id', {'id': UUID(order['id'])})
query('SELECT * FROM order_items WHERE order_id = :id', {'id': UUID(order['id'])})
rows = query('SELECT * FROM promo_codes WHERE id = :id', {'id': UUID(promo['id'])})
assert rows[0]['current_uses'] == 1
rows = query('SELECT * FROM user_operations WHERE user_id = :id ORDER BY created_at', {'id': UUID(buyer['id'])})
assert {row['operation_type'] for row in rows} == {'CREATE_ORDER', 'UPDATE_ORDER'}

canceled = call('POST', order_path + '/cancel', headers=buyer_headers)
assert canceled['status'] == 'CANCELED'
assert call('GET', product_path, headers=buyer_headers)['stock'] == 10
rows = query('SELECT status FROM orders WHERE id = :id', {'id': UUID(order['id'])})
assert rows[0]['status'] == 'CANCELED'
rows = query('SELECT current_uses FROM promo_codes WHERE id = :id', {'id': UUID(promo['id'])})
assert rows[0]['current_uses'] == 0
call('POST', order_path + '/cancel', 409, headers=buyer_headers, error='INVALID_STATE_TRANSITION')

flow = call('POST', '/orders', 201, headers=other_headers, json={'items': [{'product_id': product['id'], 'quantity': 1}]})
flow_path = '/orders/' + flow['id']
call('POST', flow_path + '/status', 409, headers=admin_headers, json={'status': 'PAID'}, error='INVALID_STATE_TRANSITION')
for status in ['PAYMENT_PENDING', 'PAID', 'SHIPPED', 'COMPLETED']:
    assert call('POST', flow_path + '/status', headers=admin_headers, json={'status': status})['status'] == status
call('POST', flow_path + '/cancel', 409, headers=other_headers, error='INVALID_STATE_TRANSITION')
call('DELETE', product_path, 204, headers=seller_headers)
rows = query('SELECT id, stock, status FROM products WHERE id = :id', {'id': UUID(product['id'])})
assert len(rows) == 1 and rows[0]['status'] == 'ARCHIVED' and rows[0]['stock'] == 9
query('SELECT version, description, success FROM flyway_schema_history ORDER BY installed_rank')
query("SELECT indexname, indexdef FROM pg_indexes WHERE tablename = 'products'")
client.close()
print('HTTP E2E and PostgreSQL checks passed')
PY

printf '\n5. JSON request logs\n'
docker compose logs --no-color --no-log-prefix api | docker compose exec -T api python -c '
import json
import sys
from uuid import UUID

required = {"request_id", "method", "endpoint", "status_code", "duration_ms", "user_id", "timestamp"}
records = []
for line in sys.stdin:
    if not line.lstrip().startswith("{"):
        continue
    record = json.loads(line)
    assert required.issubset(record), record
    UUID(record["request_id"])
    if record["method"] in ("POST", "PUT", "DELETE"):
        assert "request_body" in record
    body = record.get("request_body")
    if isinstance(body, dict):
        for key in ("password", "refresh_token"):
            if key in body:
                assert body[key] == "***"
    records.append(record)
assert records, "No API request logs found"
for record in records[-10:]:
    print(json.dumps(record, ensure_ascii=False))
print("JSON logs and sensitive field masking verified")
'

printf '\nAll checks passed. Swagger: http://localhost:8000/docs\n'
