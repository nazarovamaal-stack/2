import re
from datetime import datetime
from decimal import Decimal
from typing import Annotated

from pydantic import AwareDatetime, BeforeValidator, PlainSerializer


def json_number(value):
    if isinstance(value, bool) or not isinstance(value, (int, float, Decimal)):
        raise ValueError('Expected a JSON number')
    return value


def date_time(value):
    if isinstance(value, datetime):
        return value
    if not isinstance(value, str) or not re.fullmatch(r'\d{4}-\d{2}-\d{2}[Tt]\d{2}:\d{2}:\d{2}(?:\.\d+)?(?:[Zz]|[+-]\d{2}:\d{2})', value):
        raise ValueError('Expected an RFC 3339 date-time with timezone')
    return value.upper()


JsonInteger = Annotated[int, BeforeValidator(json_number)]
JsonDecimal = Annotated[Decimal, BeforeValidator(json_number), PlainSerializer(float, return_type=float, when_used='json')]
DateTime = Annotated[AwareDatetime, BeforeValidator(date_time)]
