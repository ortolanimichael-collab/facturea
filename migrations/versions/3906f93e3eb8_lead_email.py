"""Agregar columna email a leads_contacto (para el lead del demo interactivo)

Revision ID: 3906f93e3eb8
Revises: ab2c20c6792c
Create Date: 2026-09-09 01:00:00.000000
"""
from alembic import op
import sqlalchemy as sa


revision = '3906f93e3eb8'
down_revision = 'ab2c20c6792c'
branch_labels = None
depends_on = None


def upgrade():
    bind = op.get_bind()
    inspector = sa.inspect(bind)
    columnas = [c['name'] for c in inspector.get_columns('leads_contacto')]
    if 'email' in columnas:
        return  # ya existe -- ver el mismo motivo que en la migración anterior (deploys casi simultáneos)

    with op.batch_alter_table('leads_contacto', schema=None) as batch_op:
        batch_op.add_column(sa.Column('email', sa.String(length=200), nullable=True))


def downgrade():
    with op.batch_alter_table('leads_contacto', schema=None) as batch_op:
        batch_op.drop_column('email')
