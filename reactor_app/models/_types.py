from __future__ import annotations

from sqlalchemy.dialects import mysql

from ..extensions import db


def unsigned_bigint():
    return db.BigInteger().with_variant(mysql.BIGINT(unsigned=True), "mysql")
