import json
from concurrent.futures import ThreadPoolExecutor
from datetime import datetime, timedelta, timezone
from decimal import Decimal
from uuid import UUID, uuid4

import jwt
import pytest
import yaml
from openapi_spec_validator import validate
from sqlalchemy import delete, select, update

from app.db import engine, table
from app.main import spec_path
from app.security import ISSUER, SECRET


def order(client, headers, product, quantity=1, **extra):
    return client.post('/orders', headers=headers, json={'items': [{'product_id': product['id'], 'quantity': quantity}], **extra})


def stock(client, seller, product):
    return client.get('/products/' + product['id'], headers=seller).json()['stock']


def clear_operations(user, operation=None):
    claims = jwt.decode(user['Authorization'].split()[1], SECRET, algorithms=['HS256'], issuer=ISSUER)
    operations = table('user_operations')
    with engine.begin() as conn:
        query = delete(operations).where(operations.c.user_id == UUID(claims['sub']))
        if operation:
            query = query.where(operations.c.operation_type == operation)
        conn.execute(query)


def promo(client, seller, **extra):
    now = datetime.now(timezone.utc)
    body = {'code': 'P_' + uuid4().hex[:12].upper(), 'discount_type': 'PERCENTAGE', 'discount_value': 20, 'min_order_amount': 100, 'max_uses': 1, 'valid_from': (now-timedelta(days=1)).isoformat(), 'valid_until': (now+timedelta(days=1)).isoformat(), **extra}
    response = client.post('/promo-codes', headers=seller, json=body)
    assert response.status_code == 201, response.text
    return response.json()


def uses(code):
    promos = table('promo_codes')
    with engine.connect() as conn:
        return conn.execute(select(promos.c.current_uses).where(promos.c.code == code)).scalar_one()


def test_contract():
    validate(yaml.safe_load(spec_path.read_text()))


def test_crud_pagination_and_soft_delete(client, seller, product):
    p = product()
    page = client.get('/products', headers=seller, params={'category': p['category'], 'status': 'ACTIVE', 'size': 1}).json()
    assert page['totalElements'] == 1 and page['page'] == 0 and page['size'] == 1
    assert page['items'][0]['id'] == p['id']
    assert client.get('/products', headers=seller, params={'category': p['category'], 'page': 1, 'size': 1}).json()['items'] == []
    body = {'name': 'Updated', 'price': 150, 'stock': 3, 'category': p['category'], 'status': 'INACTIVE'}
    result = client.put('/products/'+p['id'], headers=seller, json=body)
    assert result.status_code == 200 and result.json()['updated_at'] != p['updated_at']
    assert client.delete('/products/'+p['id'], headers=seller).status_code == 204
    assert client.get('/products/'+p['id'], headers=seller).json()['status'] == 'ARCHIVED'
    with engine.connect() as conn:
        assert conn.execute(select(table('products').c.status).where(table('products').c.id == UUID(p['id']))).scalar_one() == 'ARCHIVED'


@pytest.mark.parametrize('field,value', [('name',''),('name','x'*256),('description','x'*4001),('price',0),('price',0.001),('stock',-1),('stock',1.5),('category',''),('category','x'*101),('status','UNKNOWN')])
def test_product_validation(client, seller, field, value):
    body = {'name':'P','price':1,'stock':0,'category':'C','status':'ACTIVE',field:value}
    response = client.post('/products', headers=seller, json=body)
    assert response.status_code == 400, response.text
    assert response.json()['error_code'] == 'VALIDATION_ERROR'
    assert any(field in item['field'] for item in response.json()['details']['fields'])


def test_auth_and_roles(client, user, seller, account, product, admin):
    p = product()
    assert client.get('/products').json()['error_code'] == 'TOKEN_INVALID'
    assert client.get('/products', headers={'Authorization':'Bearer broken'}).status_code == 401
    assert client.post('/products', headers=user, json={'name':'P','price':1,'stock':0,'category':'C','status':'ACTIVE'}).status_code == 403
    assert client.delete('/products/'+p['id'], headers=account('SELLER')).status_code == 403
    assert order(client, seller, p).status_code == 403
    assert client.delete('/products/'+p['id'], headers=admin).status_code == 204
    assert client.post('/auth/register', json={'username':str(uuid4()),'password':'password1','role':'ADMIN'}).status_code == 400


def test_tokens(client):
    username = str(uuid4())
    client.post('/auth/register', json={'username':username,'password':'password1'})
    pair = client.post('/auth/login', json={'username':username,'password':'password1'}).json()
    claims = jwt.decode(pair['access_token'], SECRET, algorithms=['HS256'], issuer=ISSUER)
    assert claims['role'] == 'USER' and claims['exp']-claims['iat'] == 1200
    assert client.post('/auth/refresh', json={'refresh_token':pair['refresh_token']}).status_code == 200
    assert client.get('/products', headers={'Authorization':'Bearer '+pair['refresh_token']}).json()['error_code'] == 'TOKEN_INVALID'
    assert client.post('/auth/refresh', json={'refresh_token':pair['access_token']}).json()['error_code'] == 'REFRESH_TOKEN_INVALID'
    expired = jwt.encode({**claims, 'exp':datetime.now(timezone.utc)-timedelta(seconds=1)}, SECRET, algorithm='HS256')
    assert client.get('/products', headers={'Authorization':'Bearer '+expired}).json()['error_code'] == 'TOKEN_EXPIRED'
    assert client.post('/auth/login', json={'username':username,'password':'wrong'}).status_code == 401


def test_reservation_snapshot_cancel(client, user, seller, product):
    p = product()
    response = order(client, user, p, 3)
    assert response.status_code == 201, response.text
    o = response.json()
    assert Decimal(o['total_amount']) == 300 and stock(client,seller,p) == 7
    body = {'name':'P','price':999,'stock':7,'category':p['category'],'status':'ACTIVE'}
    client.put('/products/'+p['id'], headers=seller, json=body)
    stored = client.get('/orders/'+o['id'], headers=user).json()
    assert Decimal(stored['items'][0]['price_at_order']) == 100
    result = client.post('/orders/'+o['id']+'/cancel', headers=user)
    assert result.status_code == 200 and result.json()['status'] == 'CANCELED'
    assert stock(client,seller,p) == 10
    assert client.post('/orders/'+o['id']+'/cancel', headers=user).json()['error_code'] == 'INVALID_STATE_TRANSITION'
    assert stock(client,seller,p) == 10


def test_catalog_errors_and_rollback(client, user, seller, product):
    p = product(stock=2)
    response = order(client,user,p,3)
    assert response.json()['error_code'] == 'INSUFFICIENT_STOCK'
    assert response.json()['details']['products'][0]['available_stock'] == 2
    assert stock(client,seller,p) == 2
    assert order(client,user,{'id':str(uuid4())}).json()['error_code'] == 'PRODUCT_NOT_FOUND'
    assert order(client,user,product(status='INACTIVE')).json()['error_code'] == 'PRODUCT_INACTIVE'
    assert order(client,user,p,promo_code='MISSING').json()['error_code'] == 'PROMO_CODE_INVALID'
    assert stock(client,seller,p) == 2
    o = order(client,user,p,2).json()
    response = client.put('/orders/'+o['id'], headers=user, json={'items':[{'product_id':p['id'],'quantity':3}]})
    assert response.json()['error_code'] == 'INSUFFICIENT_STOCK'
    assert stock(client,seller,p) == 0
    assert client.get('/orders/'+o['id'], headers=user).json()['items'][0]['quantity'] == 2


def test_limits_and_ownership(client, user, account, product):
    p = product()
    o = order(client,user,p).json()
    assert order(client,user,p).json()['error_code'] == 'ORDER_LIMIT_EXCEEDED'
    clear_operations(user)
    assert order(client,user,p).json()['error_code'] == 'ORDER_HAS_ACTIVE'
    another = account()
    assert client.get('/orders/'+o['id'], headers=another).json()['error_code'] == 'ORDER_OWNERSHIP_VIOLATION'
    assert client.put('/orders/'+o['id'], headers=another, json={'items':[{'product_id':p['id'],'quantity':1}]}).status_code == 403
    assert client.post('/orders/'+o['id']+'/cancel', headers=another).status_code == 403
    assert client.get('/orders/'+str(uuid4()), headers=user).json()['error_code'] == 'ORDER_NOT_FOUND'
    body = {'items':[{'product_id':p['id'],'quantity':2}]}
    assert client.put('/orders/'+o['id'], headers=user, json=body).status_code == 200
    assert client.put('/orders/'+o['id'], headers=user, json=body).json()['error_code'] == 'ORDER_LIMIT_EXCEEDED'


@pytest.mark.parametrize('items,promo_code', [([],None),([{'product_id':str(uuid4()),'quantity':0}],None),([{'product_id':str(uuid4()),'quantity':1000}],None),([{'product_id':str(uuid4()),'quantity':1}]*51,None),([{'product_id':str(uuid4()),'quantity':1}],'bad')])
def test_order_validation(client,user,items,promo_code):
    response = client.post('/orders',headers=user,json={'items':items,'promo_code':promo_code})
    assert response.status_code == 400 and response.json()['error_code'] == 'VALIDATION_ERROR'


def test_promo_update_removal_and_cancel(client,user,seller,product):
    p = product()
    code = promo(client,seller,min_order_amount=200)
    o = order(client,user,p,3,promo_code=code['code']).json()
    assert Decimal(o['total_amount']) == 240 and uses(code['code']) == 1
    response = client.put('/orders/'+o['id'],headers=user,json={'items':[{'product_id':p['id'],'quantity':2}]})
    assert response.status_code == 200 and uses(code['code']) == 1
    assert Decimal(response.json()['total_amount']) == 160
    clear_operations(user)
    response = client.put('/orders/'+o['id'],headers=user,json={'items':[{'product_id':p['id'],'quantity':1}]})
    assert response.status_code == 200 and response.json()['promo_code_id'] is None
    assert Decimal(response.json()['discount_amount']) == 0 and uses(code['code']) == 0
    client.post('/orders/'+o['id']+'/cancel',headers=user)
    assert uses(code['code']) == 0


@pytest.mark.parametrize('discount_type,value,expected', [('PERCENTAGE',70,30),('PERCENTAGE',80,100),('FIXED_AMOUNT',150,0)])
def test_promo_discount_rules(client,user,seller,product,discount_type,value,expected):
    p = product()
    code = promo(client,seller,discount_type=discount_type,discount_value=value)
    o = order(client,user,p,promo_code=code['code']).json()
    assert Decimal(o['total_amount']) == expected
    client.post('/orders/'+o['id']+'/cancel',headers=user)
    assert uses(code['code']) == 0


def test_promo_invalid_minimum_and_expiration(client,user,seller,product):
    p = product()
    code = promo(client,seller,min_order_amount=200)
    assert order(client,user,p,promo_code=code['code']).json()['error_code'] == 'PROMO_CODE_MIN_AMOUNT'
    assert stock(client,seller,p) == 10 and uses(code['code']) == 0
    expired = promo(client,seller,valid_until=(datetime.now(timezone.utc)-timedelta(hours=1)).isoformat())
    assert order(client,user,p,promo_code=expired['code']).json()['error_code'] == 'PROMO_CODE_INVALID'
    assert stock(client,seller,p) == 10


def test_state_machine(client,user,admin,product):
    p = product()
    o = order(client,user,p).json()
    endpoint = '/orders/'+o['id']+'/status'
    assert client.post(endpoint,headers=admin,json={'status':'PAID'}).json()['error_code'] == 'INVALID_STATE_TRANSITION'
    assert client.post(endpoint,headers=admin,json={'status':'PAYMENT_PENDING'}).status_code == 200
    assert client.put('/orders/'+o['id'],headers=user,json={'items':[{'product_id':p['id'],'quantity':1}]}).json()['error_code'] == 'INVALID_STATE_TRANSITION'
    for status in ['PAID','SHIPPED','COMPLETED']:
        assert client.post(endpoint,headers=admin,json={'status':status}).json()['status'] == status
    assert client.post('/orders/'+o['id']+'/cancel',headers=user).json()['error_code'] == 'INVALID_STATE_TRANSITION'


def test_concurrent_stock(client,account,seller,product):
    p = product(stock=1)
    users = [account(),account()]
    with ThreadPoolExecutor(max_workers=2) as executor:
        results = list(executor.map(lambda headers: order(client,headers,p),users))
    assert sorted(result.status_code for result in results) == [201,409]
    assert stock(client,seller,p) == 0


def test_concurrent_same_user(client,user,product):
    p = product()
    with ThreadPoolExecutor(max_workers=2) as executor:
        results = list(executor.map(lambda _: order(client,user,p), range(2)))
    assert sorted(result.status_code for result in results) == [201,429]


def test_concurrent_promo(client,account,seller,product):
    products = [product(), product()]
    code = promo(client,seller)
    users = [account(),account()]
    with ThreadPoolExecutor(max_workers=2) as executor:
        results = list(executor.map(lambda pair: order(client,pair[0],pair[1],promo_code=code['code']), zip(users,products)))
    assert sorted(result.status_code for result in results) == [201,422]
    assert uses(code['code']) == 1 and sum(stock(client,seller,p) for p in products) == 19


def test_json_logging(client):
    from app.main import logger
    import logging
    from io import StringIO
    output = StringIO()
    handler = logging.StreamHandler(output)
    logger.addHandler(handler)
    try:
        response = client.post('/auth/login',json={'username':'missing','password':'secret123'})
    finally:
        logger.removeHandler(handler)
    record = json.loads(output.getvalue())
    assert set(['request_id','method','endpoint','status_code','duration_ms','user_id','timestamp']).issubset(record)
    assert record['request_body']['password'] == '***'
    assert record['request_id'] == response.headers['X-Request-Id']
    UUID(record['request_id'])
    assert record['user_id'] is None


def test_update_replaces_items_and_reserves_atomically(client,user,seller,product):
    first, second = product(), product(price=250,stock=4)
    o = order(client,user,first,2).json()
    response = client.put('/orders/'+o['id'],headers=user,json={'items':[{'product_id':second['id'],'quantity':3}]})
    assert response.status_code == 200 and Decimal(str(response.json()['total_amount'])) == 750
    assert stock(client,seller,first) == 10 and stock(client,seller,second) == 1
    stored = client.get('/orders/'+o['id'],headers=user).json()
    assert len(stored['items']) == 1 and stored['items'][0]['product_id'] == second['id']


def test_multiple_shortages_and_catalog_check_priority(client,user,seller,product):
    first, second = product(stock=0), product(stock=1)
    response = client.post('/orders',headers=user,json={'items':[{'product_id':first['id'],'quantity':1},{'product_id':second['id'],'quantity':2}]})
    assert response.json()['error_code'] == 'INSUFFICIENT_STOCK'
    assert len(response.json()['details']['products']) == 2
    response = client.post('/orders',headers=user,json={'items':[{'product_id':first['id'],'quantity':1},{'product_id':str(uuid4()),'quantity':2}]})
    assert response.json()['error_code'] == 'PRODUCT_NOT_FOUND'
    assert stock(client,seller,first) == 0 and stock(client,seller,second) == 1


def test_payment_pending_cancel(client,user,admin,seller,product):
    p = product()
    o = order(client,user,p,2).json()
    assert client.post('/orders/'+o['id']+'/status',headers=admin,json={'status':'PAYMENT_PENDING'}).status_code == 200
    assert client.post('/orders/'+o['id']+'/cancel',headers=user).status_code == 200
    assert stock(client,seller,p) == 10


def test_promo_invalid_on_update_rolls_back(client,user,seller,product):
    p = product()
    code = promo(client,seller)
    o = order(client,user,p,2,promo_code=code['code']).json()
    promos = table('promo_codes')
    with engine.begin() as conn:
        conn.execute(update(promos).where(promos.c.id == UUID(code['id'])).values(active=False))
    response = client.put('/orders/'+o['id'],headers=user,json={'items':[{'product_id':p['id'],'quantity':3}]})
    assert response.json()['error_code'] == 'PROMO_CODE_INVALID'
    assert stock(client,seller,p) == 8 and uses(code['code']) == 1
    assert client.get('/orders/'+o['id'],headers=user).json()['items'][0]['quantity'] == 2


def test_repeated_items_and_missing_product(client,user,seller,product):
    p = product()
    item = {'product_id':p['id'],'quantity':1}
    response = client.post('/orders',headers=user,json={'items':[item,item]})
    assert response.status_code == 201 and stock(client,seller,p) == 8
    assert len(response.json()['items']) == 2
    assert Decimal(str(response.json()['total_amount'])) == 200
    assert client.post('/orders/'+response.json()['id']+'/cancel',headers=user).status_code == 200
    assert stock(client,seller,p) == 10
    assert client.get('/products/'+str(uuid4()),headers=seller).json()['error_code'] == 'PRODUCT_NOT_FOUND'
    assert client.get('/products',headers=seller,params={'size':0}).status_code == 400
    assert client.get('/products',headers=seller,params={'page':-1}).status_code == 400


@pytest.mark.parametrize('field,value', [('price','10'),('price',True),('stock',True),('stock','1')])
def test_product_json_types(client,seller,field,value):
    body = {'name':'P','price':1,'stock':0,'category':'C','status':'ACTIVE',field:value}
    response = client.post('/products',headers=seller,json=body)
    assert response.status_code == 400 and response.json()['error_code'] == 'VALIDATION_ERROR'
    assert any(field in item['field'] for item in response.json()['details']['fields'])


@pytest.mark.parametrize('quantity', [True,'1'])
def test_order_json_types(client,user,product,quantity):
    p = product()
    response = order(client,user,p,quantity)
    assert response.status_code == 400 and response.json()['error_code'] == 'VALIDATION_ERROR'
    assert any('quantity' in item['field'] for item in response.json()['details']['fields'])


@pytest.mark.parametrize('field,value', [('active','false'),('max_uses',True),('discount_value','10'),('valid_from',1700000000),('valid_from','1700000000'),('valid_until','2026-10-19')])
def test_promo_json_types(client,seller,field,value):
    now = datetime.now(timezone.utc)
    body = {'code':'P_'+uuid4().hex[:12].upper(),'discount_type':'PERCENTAGE','discount_value':20,'min_order_amount':0,'max_uses':1,'valid_from':(now-timedelta(days=1)).isoformat(),'valid_until':(now+timedelta(days=1)).isoformat(),field:value}
    response = client.post('/promo-codes',headers=seller,json=body)
    assert response.status_code == 400 and response.json()['error_code'] == 'VALIDATION_ERROR'
    assert any(field in item['field'] for item in response.json()['details']['fields'])


def test_repeated_items_sum_stock_and_update(client,user,seller,product):
    p = product(stock=1500)
    items = [{'product_id':p['id'],'quantity':999},{'product_id':p['id'],'quantity':502}]
    response = client.post('/orders',headers=user,json={'items':items})
    assert response.json()['error_code'] == 'INSUFFICIENT_STOCK'
    assert response.json()['details']['products'][0]['requested_quantity'] == 1501
    assert stock(client,seller,p) == 1500
    items[1]['quantity'] = 501
    response = client.post('/orders',headers=user,json={'items':items})
    assert response.status_code == 201 and stock(client,seller,p) == 0
    o = response.json()
    assert len(o['items']) == 2 and sum(item['quantity'] for item in o['items']) == 1500
    assert Decimal(str(o['total_amount'])) == 150000
    response = client.put('/orders/'+o['id'],headers=user,json={'items':[{'product_id':p['id'],'quantity':3},{'product_id':p['id'],'quantity':2}]})
    assert response.status_code == 200 and stock(client,seller,p) == 1495
    assert Decimal(str(response.json()['total_amount'])) == 500
    assert client.post('/orders/'+o['id']+'/cancel',headers=user).status_code == 200
    assert stock(client,seller,p) == 1500


def test_remaining_role_restrictions(client,user,seller,product,admin,account):
    p = product()
    o = order(client,user,p).json()
    body = {'name':'P','price':1,'stock':0,'category':'C','status':'ACTIVE'}
    assert client.put('/products/'+p['id'],headers=user,json=body).json()['error_code'] == 'ACCESS_DENIED'
    assert client.delete('/products/'+p['id'],headers=user).json()['error_code'] == 'ACCESS_DENIED'
    assert client.put('/products/'+p['id'],headers=account('SELLER'),json=body).json()['error_code'] == 'ACCESS_DENIED'
    assert client.get('/orders/'+o['id'],headers=seller).json()['error_code'] == 'ACCESS_DENIED'
    assert client.put('/orders/'+o['id'],headers=seller,json={'items':[{'product_id':p['id'],'quantity':1}]}).json()['error_code'] == 'ACCESS_DENIED'
    assert client.post('/orders/'+o['id']+'/cancel',headers=seller).json()['error_code'] == 'ACCESS_DENIED'
    now = datetime.now(timezone.utc)
    promo_body = {'code':'P_'+uuid4().hex[:12].upper(),'discount_type':'PERCENTAGE','discount_value':20,'min_order_amount':0,'max_uses':1,'valid_from':(now-timedelta(days=1)).isoformat(),'valid_until':(now+timedelta(days=1)).isoformat()}
    assert client.post('/promo-codes',headers=user,json=promo_body).json()['error_code'] == 'ACCESS_DENIED'
    assert client.get('/orders/'+o['id'],headers=admin).status_code == 200
    assert client.put('/orders/'+o['id'],headers=admin,json={'items':[{'product_id':p['id'],'quantity':2}]}).status_code == 200
    assert client.post('/orders/'+o['id']+'/cancel',headers=admin).status_code == 200
    assert client.post('/promo-codes',headers=admin,json=promo_body).status_code == 201
    assert order(client,admin,p).status_code == 201


def test_update_preserves_existing_snapshot(client,user,seller,product):
    p = product(price=100)
    o = order(client,user,p).json()
    body = {'name':'P','price':250,'stock':9,'category':p['category'],'status':'ACTIVE'}
    assert client.put('/products/'+p['id'],headers=seller,json=body).status_code == 200
    response = client.put('/orders/'+o['id'],headers=user,json={'items':[{'product_id':p['id'],'quantity':2}]})
    assert response.status_code == 200
    assert Decimal(str(response.json()['total_amount'])) == 200
    assert Decimal(str(response.json()['items'][0]['price_at_order'])) == 100


def test_valid_contract_boundaries(client,user,seller,product):
    p = product(name='N'*255,description='D'*4000,category='C'*68+uuid4().hex.upper(),price=0.01,stock=49950)
    response = client.post('/orders',headers=user,json={'items':[{'product_id':p['id'],'quantity':999} for _ in range(50)]})
    assert response.status_code == 201, response.text
    assert len(response.json()['items']) == 50
    assert Decimal(str(response.json()['total_amount'])) == Decimal('499.50')
    assert stock(client,seller,p) == 0
    assert client.post('/orders/'+response.json()['id']+'/cancel',headers=user).status_code == 200
    assert stock(client,seller,p) == 49950
    page = client.get('/products',headers=seller,params={'category':p['category']}).json()
    assert page['size'] == 20 and page['page'] == 0 and page['totalElements'] == 1
    assert client.get('/products',headers=seller,params={'category':p['category'].lower()}).json()['totalElements'] == 0


@pytest.mark.parametrize('raw', [b'{', b'\xff'])
def test_malformed_body_error_details(client,seller,raw):
    response = client.post('/products',headers={**seller,'Content-Type':'application/json'},content=raw)
    assert response.status_code == 400 and response.json()['error_code'] == 'VALIDATION_ERROR'
    assert response.json()['details']['fields']
    assert response.headers['X-Request-Id']


def test_database_requirements(client):
    from sqlalchemy import text
    with engine.connect() as conn:
        assert conn.execute(text("SELECT count(*) FROM flyway_schema_history WHERE success")).scalar_one() >= 2
        assert conn.execute(text("SELECT count(*) FROM pg_indexes WHERE tablename = 'products' AND indexdef LIKE '%(status)%'")).scalar_one() >= 1
        assert conn.execute(text("SELECT count(*) FROM information_schema.triggers WHERE event_object_table IN ('products','orders') AND event_manipulation = 'UPDATE'")).scalar_one() >= 2
