import json
import logging
import time
from contextlib import asynccontextmanager
from datetime import datetime, timezone
from pathlib import Path
from uuid import uuid4

import yaml
from fastapi import FastAPI, Request
from fastapi.exceptions import RequestValidationError
from fastapi.responses import JSONResponse
from starlette.exceptions import HTTPException

from app.db import initialize
from app.errors import ApiError
from app.security import bootstrap_admin, decode

logger = logging.getLogger('marketplace.requests')
logger.setLevel(logging.INFO)
logger.propagate = False
if not logger.handlers:
    logger.addHandler(logging.StreamHandler())


@asynccontextmanager
async def lifespan(app):
    initialize()
    bootstrap_admin()
    yield


app = FastAPI(title='Marketplace API', lifespan=lifespan)


def redact(value):
    if isinstance(value, dict):
        return {key: '***' if any(word in key.lower() for word in ('password', 'token', 'secret')) else redact(item) for key, item in value.items()}
    if isinstance(value, list):
        return [redact(item) for item in value]
    return value


def reject_json_constant(value):
    raise ValueError('Invalid JSON constant')


@app.middleware('http')
async def request_middleware(request: Request, call_next):
    start = time.perf_counter()
    request_id = str(uuid4())
    request.state.identity = None
    timestamp = datetime.now(timezone.utc).isoformat()
    body = None
    if request.method in ('POST', 'PUT', 'DELETE'):
        raw = await request.body()
        if raw:
            try:
                body = redact(json.loads(raw, parse_constant=reject_json_constant))
            except (ValueError, UnicodeDecodeError):
                body = '[invalid JSON]'
    try:
        public = request.url.path in ('/auth/register', '/auth/login', '/auth/refresh', '/docs', '/docs/oauth2-redirect', '/redoc', '/openapi.json')
        if not public:
            scheme, _, token = request.headers.get('Authorization', '').partition(' ')
            if scheme.lower() != 'bearer' or not token:
                raise ApiError(401, 'TOKEN_INVALID', 'Bearer access token is required')
            request.state.identity = decode(token, 'access')
        response = await call_next(request)
    except ApiError as exc:
        response = JSONResponse(exc.payload(), status_code=exc.status)
    except Exception:
        response = JSONResponse({'error_code': 'INTERNAL_ERROR', 'message': 'Internal server error'}, status_code=500)
    response.headers['X-Request-Id'] = request_id
    record = {'request_id': request_id, 'method': request.method, 'endpoint': request.url.path, 'status_code': response.status_code, 'duration_ms': round((time.perf_counter() - start) * 1000, 3), 'user_id': str(request.state.identity['user_id']) if request.state.identity else None, 'timestamp': timestamp}
    if request.method in ('POST', 'PUT', 'DELETE'):
        record['request_body'] = body
    logger.info(json.dumps(record, ensure_ascii=False))
    return response


@app.exception_handler(ApiError)
async def api_error(request, exc):
    return JSONResponse(exc.payload(), status_code=exc.status)


@app.exception_handler(RequestValidationError)
async def validation_error(request, exc):
    fields = [{'field': '.'.join(str(part) for part in error['loc']), 'message': error['msg']} for error in exc.errors()]
    return JSONResponse({'error_code': 'VALIDATION_ERROR', 'message': 'Request validation failed', 'details': {'fields': fields}}, status_code=400)


@app.exception_handler(HTTPException)
async def http_error(request, exc):
    code = {404: 'NOT_FOUND', 405: 'METHOD_NOT_ALLOWED'}.get(exc.status_code, 'VALIDATION_ERROR')
    payload = {'error_code': code, 'message': str(exc.detail)}
    if exc.status_code == 400:
        payload['details'] = {'fields': [{'field': 'body', 'message': str(exc.detail)}]}
    return JSONResponse(payload, status_code=exc.status_code)


from app.generated.routes import router

app.include_router(router)
spec_path = Path(__file__).resolve().parents[1] / 'src/main/resources/openapi/marketplace.yaml'
app.openapi = lambda: yaml.safe_load(spec_path.read_text())
