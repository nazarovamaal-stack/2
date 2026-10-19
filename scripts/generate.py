from pathlib import Path
import yaml

ROOT = Path(__file__).resolve().parents[1]
spec = yaml.safe_load((ROOT / 'src/main/resources/openapi/marketplace.yaml').read_text())
out = ROOT / 'app/generated'
out.mkdir(parents=True, exist_ok=True)
(out / '__init__.py').write_text('')


def type_of(schema, query=False):
    if '$ref' in schema:
        result = schema['$ref'].rsplit('/', 1)[1]
    elif 'enum' in schema:
        result = 'Literal[' + ', '.join(repr(v) for v in schema['enum']) + ']'
    elif schema['type'] == 'array':
        result = 'list[' + type_of(schema['items']) + ']'
    elif schema.get('format') in ('uuid', 'date-time', 'decimal'):
        result = {'uuid': 'UUID', 'date-time': 'DateTime', 'decimal': 'JsonDecimal'}[schema['format']]
    else:
        result = {'string': 'str', 'integer': 'int' if query else 'JsonInteger', 'number': 'float', 'boolean': 'StrictBool', 'object': 'dict[str, Any]'}[schema['type']]
    return result + ' | None' if schema.get('nullable') else result


def constraints(schema):
    names = {'minimum': 'ge', 'maximum': 'le', 'minLength': 'min_length', 'maxLength': 'max_length', 'minItems': 'min_length', 'maxItems': 'max_length', 'pattern': 'pattern', 'multipleOf': 'multiple_of'}
    return [f'{target}={schema[source]!r}' for source, target in names.items() if source in schema]


lines = ['from __future__ import annotations', 'from typing import Any, Literal', 'from decimal import Decimal', 'from uuid import UUID', 'from pydantic import BaseModel, Field, ConfigDict, StrictBool', 'from app.validation import JsonInteger, JsonDecimal, DateTime', '']
for name, schema in spec['components']['schemas'].items():
    lines += [f'class {name}(BaseModel):', '    model_config = ConfigDict(extra="forbid")']
    for field, prop in schema['properties'].items():
        default = '...' if field in schema.get('required', []) else repr(prop.get('default'))
        args = [default, *constraints(prop)]
        lines.append(f'    {field}: {type_of(prop)} = Field({", ".join(args)})')
    lines.append('')
for name in spec['components']['schemas']:
    lines.append(f'{name}.model_rebuild()')
(out / 'models.py').write_text('\n'.join(lines) + '\n')
lines = ['from fastapi import APIRouter, Request, Response, Body, Path, Query', 'from app.generated.models import *', 'from app import services', '', 'router = APIRouter()', '']
for path, operations in spec['paths'].items():
    for method, op in operations.items():
        code = next(c for c in op['responses'] if c.startswith('2'))
        response = op['responses'][code].get('content', {}).get('application/json', {}).get('schema')
        model = type_of(response) if response else 'None'
        lines.append(f'@router.{method}({path!r}, status_code={code}, response_model={model})')
        args, forwarded = ['request: Request'], []
        for param in op.get('parameters', []):
            schema = param['schema']
            required = param.get('required', False)
            typ = type_of(schema, query=param['in'] == 'query')
            if not required and 'default' not in schema:
                typ += ' | None'
            default = '...' if required else repr(schema.get('default'))
            value = ', '.join([default, *constraints(schema)])
            kind = 'Path' if param['in'] == 'path' else 'Query'
            args.append(f'{param["name"]}: {typ} = {kind}({value})')
            forwarded.append(f'{param["name"]}={param["name"]}')
        if 'requestBody' in op:
            schema = op['requestBody']['content']['application/json']['schema']
            args.append(f'body: {type_of(schema)} = Body(...)')
            forwarded.append('body=body')
        lines.append(f'def {op["operationId"]}({", ".join(args)}):')
        lines.append(f'    services.authorize(request, {op["x-roles"]!r})')
        lines.append(f'    result = services.{op["operationId"]}(request{", " if forwarded else ""}{", ".join(forwarded)})')
        lines.append('    return Response(status_code=204)' if code == '204' else '    return result')
        lines.append('')
(out / 'routes.py').write_text('\n'.join(lines) + '\n')
