# Marketplace: OpenAPI + CRUD

Python 3.12, FastAPI, PostgreSQL 17, Flyway, JWT.

## Запуск

```sh
docker compose up --build -d
docker compose logs -f api
```

API: http://localhost:8000. Swagger UI: http://localhost:8000/docs.

Миграции Flyway выполняются перед запуском API. Модели и обработчики генерируются из `src/main/resources/openapi/marketplace.yaml` при сборке Docker-образа. Сгенерированный каталог `app/generated/` исключён из Git.

Отдельная генерация:

```sh
python3 -m venv .venv
.venv/bin/pip install -r requirements.txt
make generate PYTHON=.venv/bin/python
```

## Авторизация

`POST /auth/register` принимает `username`, `password` и необязательную роль `USER` или `SELLER`. Администратор создаётся при старте из `BOOTSTRAP_ADMIN_USERNAME` и `BOOTSTRAP_ADMIN_PASSWORD`; в Compose по умолчанию `admin` / `admin-password`.

`POST /auth/login` возвращает access token на 20 минут и refresh token на 14 дней. `POST /auth/refresh` принимает `refresh_token`. Для защищённых запросов передаётся `Authorization: Bearer <access_token>`.

`JWT_SECRET`, `ORDER_INTERVAL_MINUTES`, `BOOTSTRAP_ADMIN_USERNAME`, `BOOTSTRAP_ADMIN_PASSWORD` можно задать через окружение или `.env`. Интервал создания и обновления заказов по умолчанию — 1 минута, каждый тип операции учитывается отдельно.

Продавец управляет своими товарами и создаёт промокоды. Пользователь оформляет и изменяет свои заказы. Администратор управляет любыми товарами и заказами. `POST /orders/{id}/status` доступен администратору для последовательных переходов `CREATED → PAYMENT_PENDING → PAID → SHIPPED → COMPLETED`. Отмена выполняется через `POST /orders/{id}/cancel` из `CREATED` и `PAYMENT_PENDING`.

Процентная скидка свыше 70% не применяется: согласно формулировке задания «в ином случае цена 100%». При обновлении заказа сохранённые позиции сохраняют `price_at_order`, новые получают текущую цену. Повторные `product_id` учитываются суммарно при проверке и резервировании остатков. При повторной проверке уже применённого промокода его собственное использование не считается исчерпанием лимита.

## Проверка

Полный пайплайн: сборка и запуск, автоматические тесты, проверка воспроизводимости генерации, HTTP E2E, SELECT в PostgreSQL и проверка JSON-логов. Требуются Docker и Bash.

```sh
make check
```

Или `bash scripts/check.sh`. При ошибке выполнение останавливается с ненулевым кодом. Пайплайн создаёт пользователей с уникальными именами, товары, промокоды и заказы; записи остаются для просмотра на защите. Используются настройки администратора из Compose.

Только автоматические тесты:

```sh
docker compose exec api pytest -q
```

Тесты выполняют реальные запросы к приложению с PostgreSQL и проверяют валидацию, роли, JWT, переходы состояний, снапшоты цен, промокоды, откат транзакций и конкурентное резервирование. Тестовые записи остаются в БД.

## E2E

```sh
curl -s http://localhost:8000/auth/register -H 'Content-Type: application/json' -d '{"username":"seller","password":"password123","role":"SELLER"}'
curl -s http://localhost:8000/auth/register -H 'Content-Type: application/json' -d '{"username":"buyer","password":"password123","role":"USER"}'
curl -s http://localhost:8000/auth/login -H 'Content-Type: application/json' -d '{"username":"seller","password":"password123"}'
curl -s http://localhost:8000/auth/login -H 'Content-Type: application/json' -d '{"username":"buyer","password":"password123"}'
```

Сохраните полученные access token в переменные `SELLER_TOKEN` и `USER_TOKEN`.

```sh
curl -s http://localhost:8000/products -H "Authorization: Bearer $SELLER_TOKEN" -H 'Content-Type: application/json' -d '{"name":"Keyboard","price":1500,"stock":10,"category":"electronics","status":"ACTIVE"}'
```

Сохраните ID созданного товара в `PRODUCT_ID`.

```sh
curl -s http://localhost:8000/orders -H "Authorization: Bearer $USER_TOKEN" -H 'Content-Type: application/json' -d "{\"items\":[{\"product_id\":\"$PRODUCT_ID\",\"quantity\":2}]}"
curl -s 'http://localhost:8000/products?page=0&size=20&status=ACTIVE&category=electronics' -H "Authorization: Bearer $USER_TOKEN"
```

Сохраните ID заказа в `ORDER_ID`.

```sh
curl -s "http://localhost:8000/orders/$ORDER_ID" -H "Authorization: Bearer $USER_TOKEN"
curl -s -X POST "http://localhost:8000/orders/$ORDER_ID/cancel" -H "Authorization: Bearer $USER_TOKEN"
docker compose exec db psql -U marketplace -d marketplace -c 'SELECT * FROM products; SELECT * FROM orders; SELECT * FROM order_items; SELECT * FROM promo_codes; SELECT * FROM user_operations;'
```
