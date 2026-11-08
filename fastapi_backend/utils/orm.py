from typing import Any, Dict

from sqlalchemy.inspection import inspect as sa_inspect


def orm_to_dict(instance: Any) -> Dict[str, Any]:
    """Converts a SQLAlchemy ORM instance to a dictionary of column values"""
    mapper = sa_inspect(instance).mapper
    return {attr.key: getattr(instance, attr.key) for attr in mapper.column_attrs}


