"""Agregar leads_contacto (contactos del formulario de WhatsApp de la landing)

Revision ID: ab2c20c6792c
Revises: c833df01b26c
Create Date: 2026-09-09 00:10:00.000000

OJO ANTES DE DESPLEGAR: mismo aviso que en la migración anterior
(c833df01b26c) -- esta encadena justo después de esa, así que si esa otra
todavía no se aplicó, aplicar primero esa y recién después esta.
"""
from alembic import op
import sqlalchemy as sa


# revision identifiers, used by Alembic.
revision = 'ab2c20c6792c'
down_revision = 'c833df01b26c'
branch_labels = None
depends_on = None


def upgrade():
    op.create_table(
        'leads_contacto',
        sa.Column('id', sa.Integer(), nullable=False),
        sa.Column('nombre', sa.String(length=200), nullable=True),
        sa.Column('telefono', sa.String(length=60), nullable=True),
        sa.Column('origen', sa.String(length=50), nullable=True),
        sa.Column('creado_en', sa.DateTime(), nullable=True),
        sa.PrimaryKeyConstraint('id'),
    )


def downgrade():
    op.drop_table('leads_contacto')
