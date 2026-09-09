"""Agregar leads_contacto (contactos del formulario de WhatsApp de la landing)

Revision ID: ab2c20c6792c
Revises: c833df01b26c
Create Date: 2026-09-09 00:10:00.000000

OJO ANTES DE DESPLEGAR: mismo aviso que en la migración anterior
(c833df01b26c) -- esta encadena justo después de esa, así que si esa otra
todavía no se aplicó, aplicar primero esa y recién después esta.

upgrade() chequea si la tabla ya existe antes de crearla -- por las dudas
dos deploys hayan corrido "flask db upgrade" casi al mismo tiempo (dos
subidas de archivos seguidas, por ejemplo) y ya se haya creado en uno de
ellos antes de que este otro llegue a esta migración.
"""
from alembic import op
import sqlalchemy as sa


# revision identifiers, used by Alembic.
revision = 'ab2c20c6792c'
down_revision = 'c833df01b26c'
branch_labels = None
depends_on = None


def upgrade():
    bind = op.get_bind()
    inspector = sa.inspect(bind)
    if 'leads_contacto' in inspector.get_table_names():
        return  # ya existe (ver aviso arriba) -- no hay nada más que hacer acá

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
