"""Помощники для записи в базу без гонок.

Запросы к базе выполняются параллельно в потоках (см. db.py), поэтому шаблон
«прочитать → изменить в Python → записать» небезопасен: два запроса читают одно и то же
и оба пишут. Для всего ценного (GRAM, предметы, сферы, лоты) используем один UPDATE/DELETE
с условием и смотрим, сколько строк он затронул.
"""
from sqlalchemy.dialects.postgresql import insert as pg_insert
from sqlalchemy.dialects.sqlite import insert as sqlite_insert

import db


def insert_ignore(table, **values):
    """INSERT, который ничего не делает, если запись с таким ключом уже есть (PostgreSQL и SQLite)."""
    ins = pg_insert(table) if db.IS_PG else sqlite_insert(table)
    return ins.values(**values).on_conflict_do_nothing()
