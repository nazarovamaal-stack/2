import os
from datetime import datetime, timedelta, timezone
from decimal import Decimal, ROUND_HALF_UP
from uuid import uuid4

from sqlalchemy import delete, func, select, text, update
from sqlalchemy.exc import IntegrityError

from app.db import engine, table
from app.errors import ApiError
from app.security import decode, hash_password, tokens, verify_password

INTERVAL = timedelta(minutes=float(os.environ.get('ORDER_INTERVAL_MINUTES', '1')))
CENT = Decimal('0.01')
MAX_AMOUNT = Decimal('9999999999.99')


def authorize(request, roles):
    if roles and request.state.identity['role'] not in roles:
        raise ApiError(403, 'ACCESS_DENIED', 'Insufficient permissions')


def identity(request):
    return request.state.identity


def register(request, body):
    users = table('users')
    try:
        with engine.begin() as conn:
            return dict(conn.execute(users.insert().values(id=uuid4(), username=body.username, password_hash=hash_password(body.password), role=body.role).returning(users.c.id, users.c.username, users.c.role)).mappings().one())
    except IntegrityError:
        raise ApiError(409, 'USERNAME_TAKEN', 'Username is already registered')


def login(request, body):
    users = table('users')
    with engine.connect() as conn:
        user = conn.execute(select(users).where(users.c.username == body.username)).mappings().first()
    if not user or not verify_password(body.password, user['password_hash']):
        raise ApiError(401, 'TOKEN_INVALID', 'Invalid credentials')
    return tokens(user)


def refresh(request, body):
    claims = decode(body.refresh_token, 'refresh')
    users = table('users')
    with engine.connect() as conn:
        user = conn.execute(select(users).where(users.c.id == claims['user_id'])).mappings().first()
    if not user:
        raise ApiError(401, 'REFRESH_TOKEN_INVALID', 'User does not exist')
    return tokens(user)


def find_product(conn, id, lock=False):
    products = table('products')
    query = select(products).where(products.c.id == id)
    if lock:
        query = query.with_for_update()
    product = conn.execute(query).mappings().first()
    if not product:
        raise ApiError(404, 'PRODUCT_NOT_FOUND', 'Product not found')
    return dict(product)


def product_owner(request, product):
    if identity(request)['role'] != 'ADMIN' and product['seller_id'] != identity(request)['user_id']:
        raise ApiError(403, 'ACCESS_DENIED', 'Product belongs to another seller')


def create_product(request, body):
    products = table('products')
    with engine.begin() as conn:
        return dict(conn.execute(products.insert().values(id=uuid4(), seller_id=identity(request)['user_id'], **body.model_dump()).returning(products)).mappings().one())


def get_product(request, id):
    with engine.connect() as conn:
        return find_product(conn, id)


def list_products(request, page=0, size=20, status=None, category=None):
    products = table('products')
    filters = []
    if status is not None:
        filters.append(products.c.status == status)
    if category is not None:
        filters.append(products.c.category == category)
    with engine.connect().execution_options(isolation_level='REPEATABLE READ') as conn:
        with conn.begin():
            total = conn.execute(select(func.count()).select_from(products).where(*filters)).scalar_one()
            rows = conn.execute(select(products).where(*filters).order_by(products.c.created_at, products.c.id).offset(page * size).limit(size)).mappings().all()
    return {'items': [dict(row) for row in rows], 'totalElements': total, 'page': page, 'size': size}


def update_product(request, id, body):
    products = table('products')
    with engine.begin() as conn:
        product_owner(request, find_product(conn, id, lock=True))
        return dict(conn.execute(update(products).where(products.c.id == id).values(**body.model_dump()).returning(products)).mappings().one())


def delete_product(request, id):
    products = table('products')
    with engine.begin() as conn:
        product_owner(request, find_product(conn, id, lock=True))
        conn.execute(update(products).where(products.c.id == id).values(status='ARCHIVED'))


def create_promo(request, body):
    if body.valid_until < body.valid_from:
        raise ApiError(400, 'VALIDATION_ERROR', 'Invalid validity interval', {'fields': [{'field': 'valid_until', 'message': 'Must be greater than or equal to valid_from'}]})
    promo = table('promo_codes')
    try:
        with engine.begin() as conn:
            return dict(conn.execute(promo.insert().values(id=uuid4(), **body.model_dump()).returning(promo)).mappings().one())
    except IntegrityError:
        raise ApiError(409, 'PROMO_CODE_EXISTS', 'Promo code already exists')


def lock_user(conn, user_id):
    conn.execute(text('SELECT pg_advisory_xact_lock(hashtextextended(:user_id, 0))'), {'user_id': str(user_id)})


def check_limit(conn, user_id, operation):
    operations = table('user_operations')
    last = conn.execute(select(operations.c.created_at).where(operations.c.user_id == user_id, operations.c.operation_type == operation).order_by(operations.c.created_at.desc()).limit(1)).scalar_one_or_none()
    if last and datetime.now(timezone.utc) - last < INTERVAL:
        raise ApiError(429, 'ORDER_LIMIT_EXCEEDED', 'Order operation frequency limit exceeded')


def record_operation(conn, user_id, operation):
    conn.execute(table('user_operations').insert().values(id=uuid4(), user_id=user_id, operation_type=operation))


def item_quantities(items):
    result = {}
    for item in items:
        result[item.product_id] = result.get(item.product_id, 0) + item.quantity
    return result


def lock_products(conn, ids):
    products = table('products')
    rows = conn.execute(select(products).where(products.c.id.in_(ids)).order_by(products.c.id).with_for_update()).mappings().all()
    return {row['id']: dict(row) for row in rows}


def validate_catalog(quantities, catalog):
    for product_id in quantities:
        if product_id not in catalog:
            raise ApiError(404, 'PRODUCT_NOT_FOUND', 'Product not found', {'product_id': str(product_id)})
        if catalog[product_id]['status'] != 'ACTIVE':
            raise ApiError(409, 'PRODUCT_INACTIVE', 'Product is not active', {'product_id': str(product_id)})


def reserve(conn, quantities, catalog):
    shortage = [{'product_id': str(pid), 'requested_quantity': quantity, 'available_stock': catalog[pid]['stock']} for pid, quantity in quantities.items() if catalog[pid]['stock'] < quantity]
    if shortage:
        raise ApiError(409, 'INSUFFICIENT_STOCK', 'Insufficient product stock', {'products': shortage})
    products = table('products')
    for pid, quantity in quantities.items():
        conn.execute(update(products).where(products.c.id == pid).values(stock=products.c.stock - quantity))


def subtotal(quantities, catalog):
    amount = sum((catalog[pid]['price'] * quantity for pid, quantity in quantities.items()), Decimal(0))
    if amount > MAX_AMOUNT:
        raise ApiError(400, 'VALIDATION_ERROR', 'Order amount exceeds supported maximum', {'fields': [{'field': 'items', 'message': 'Order amount must not exceed 9999999999.99'}]})
    return amount


def promo_valid(promo, existing=False):
    now = datetime.now(timezone.utc)
    if not promo or not promo['active'] or not promo['valid_from'] <= now <= promo['valid_until'] or promo['current_uses'] - int(existing) >= promo['max_uses']:
        raise ApiError(422, 'PROMO_CODE_INVALID', 'Promo code is invalid, expired or exhausted')


def discount_for(promo, amount):
    if promo['discount_type'] == 'PERCENTAGE':
        value = promo['discount_value']
        discount = amount * value / Decimal(100) if value <= 70 else Decimal(0)
    else:
        discount = min(promo['discount_value'], amount)
    return discount.quantize(CENT, rounding=ROUND_HALF_UP)


def add_items(conn, order_id, items, catalog, previous=None):
    previous = previous or {}
    conn.execute(table('order_items').insert(), [{'id': uuid4(), 'order_id': order_id, 'product_id': item.product_id, 'quantity': item.quantity, 'price_at_order': previous.get(item.product_id, catalog[item.product_id]['price'])} for item in items])


def order_response(conn, order):
    items = table('order_items')
    result = dict(order)
    result['items'] = [dict(item) for item in conn.execute(select(items.c.id, items.c.product_id, items.c.quantity, items.c.price_at_order).where(items.c.order_id == result['id']).order_by(items.c.id)).mappings()]
    return result


def create_order(request, body):
    user_id = identity(request)['user_id']
    orders, promos = table('orders'), table('promo_codes')
    with engine.begin() as conn:
        lock_user(conn, user_id)
        check_limit(conn, user_id, 'CREATE_ORDER')
        if conn.execute(select(orders.c.id).where(orders.c.user_id == user_id, orders.c.status.in_(['CREATED', 'PAYMENT_PENDING']))).first():
            raise ApiError(409, 'ORDER_HAS_ACTIVE', 'User already has an active order')
        quantities = item_quantities(body.items)
        catalog = lock_products(conn, quantities)
        validate_catalog(quantities, catalog)
        reserve(conn, quantities, catalog)
        amount = subtotal(quantities, catalog)
        discount, promo_id = Decimal(0), None
        if body.promo_code is not None:
            promo = conn.execute(select(promos).where(promos.c.code == body.promo_code).with_for_update()).mappings().first()
            promo_valid(promo)
            if amount < promo['min_order_amount']:
                raise ApiError(422, 'PROMO_CODE_MIN_AMOUNT', 'Order amount is below promo code minimum')
            discount, promo_id = discount_for(promo, amount), promo['id']
            conn.execute(update(promos).where(promos.c.id == promo_id).values(current_uses=promos.c.current_uses + 1))
        order = conn.execute(orders.insert().values(id=uuid4(), user_id=user_id, status='CREATED', promo_code_id=promo_id, total_amount=amount-discount, discount_amount=discount).returning(orders)).mappings().one()
        add_items(conn, order['id'], body.items, catalog)
        record_operation(conn, user_id, 'CREATE_ORDER')
        return order_response(conn, order)


def find_order(conn, request, id, lock=False):
    orders = table('orders')
    query = select(orders).where(orders.c.id == id)
    if lock:
        query = query.with_for_update()
    order = conn.execute(query).mappings().first()
    if not order:
        raise ApiError(404, 'ORDER_NOT_FOUND', 'Order not found')
    if identity(request)['role'] != 'ADMIN' and order['user_id'] != identity(request)['user_id']:
        raise ApiError(403, 'ORDER_OWNERSHIP_VIOLATION', 'Order belongs to another user')
    return dict(order)


def get_order(request, id):
    with engine.connect().execution_options(isolation_level='REPEATABLE READ') as conn:
        with conn.begin():
            return order_response(conn, find_order(conn, request, id))


def require_state(order, states):
    if order['status'] not in states:
        raise ApiError(409, 'INVALID_STATE_TRANSITION', 'Operation is not allowed in current order state')


def return_stock(conn, items):
    products = table('products')
    for item in items:
        conn.execute(update(products).where(products.c.id == item['product_id']).values(stock=products.c.stock + item['quantity']))


def update_order(request, id, body):
    orders, items, products, promos = table('orders'), table('order_items'), table('products'), table('promo_codes')
    with engine.begin() as conn:
        lock_user(conn, identity(request)['user_id'])
        order = find_order(conn, request, id, lock=True)
        require_state(order, ['CREATED'])
        check_limit(conn, identity(request)['user_id'], 'UPDATE_ORDER')
        old_items = conn.execute(select(items).where(items.c.order_id == id)).mappings().all()
        quantities = item_quantities(body.items)
        catalog = lock_products(conn, set(quantities) | {item['product_id'] for item in old_items})
        return_stock(conn, old_items)
        for item in old_items:
            catalog[item['product_id']]['stock'] += item['quantity']
        validate_catalog(quantities, catalog)
        reserve(conn, quantities, catalog)
        previous = {item['product_id']: item['price_at_order'] for item in old_items}
        price_catalog = {pid: {**p, 'price': previous.get(pid, p['price'])} for pid, p in catalog.items()}
        amount = subtotal(quantities, price_catalog)
        discount, promo_id = Decimal(0), order['promo_code_id']
        if promo_id:
            promo = conn.execute(select(promos).where(promos.c.id == promo_id).with_for_update()).mappings().one()
            if amount < promo['min_order_amount']:
                conn.execute(update(promos).where(promos.c.id == promo_id).values(current_uses=promos.c.current_uses - 1))
                promo_id = None
            else:
                promo_valid(promo, existing=True)
                discount = discount_for(promo, amount)
        conn.execute(delete(items).where(items.c.order_id == id))
        add_items(conn, id, body.items, catalog, previous)
        order = conn.execute(update(orders).where(orders.c.id == id).values(total_amount=amount-discount, discount_amount=discount, promo_code_id=promo_id).returning(orders)).mappings().one()
        record_operation(conn, identity(request)['user_id'], 'UPDATE_ORDER')
        return order_response(conn, order)


def cancel_order(request, id):
    orders, promos, items = table('orders'), table('promo_codes'), table('order_items')
    with engine.begin() as conn:
        lock_user(conn, identity(request)['user_id'])
        order = find_order(conn, request, id, lock=True)
        require_state(order, ['CREATED', 'PAYMENT_PENDING'])
        old_items = conn.execute(select(items).where(items.c.order_id == id)).mappings().all()
        lock_products(conn, [item['product_id'] for item in old_items])
        return_stock(conn, old_items)
        if order['promo_code_id']:
            conn.execute(update(promos).where(promos.c.id == order['promo_code_id']).values(current_uses=promos.c.current_uses - 1))
        order = conn.execute(update(orders).where(orders.c.id == id).values(status='CANCELED').returning(orders)).mappings().one()
        return order_response(conn, order)


def transition_order(request, id, body):
    transitions = {'CREATED': 'PAYMENT_PENDING', 'PAYMENT_PENDING': 'PAID', 'PAID': 'SHIPPED', 'SHIPPED': 'COMPLETED'}
    orders = table('orders')
    with engine.begin() as conn:
        order = find_order(conn, request, id, lock=True)
        if transitions.get(order['status']) != body.status:
            raise ApiError(409, 'INVALID_STATE_TRANSITION', 'Invalid order state transition')
        order = conn.execute(update(orders).where(orders.c.id == id).values(status=body.status).returning(orders)).mappings().one()
        return order_response(conn, order)
