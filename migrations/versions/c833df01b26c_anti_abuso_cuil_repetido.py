"""Anti-abuso: CUIL repetido en cuenta nueva (prueba gratis infinita)

Revision ID: c833df01b26c
Revises: 977821d0f9bd
Create Date: 2026-09-09 00:00:00.000000

OJO ANTES DE DESPLEGAR: esta migración asume que '977821d0f9bd' (agregar
facturado_en) es la última migración aplicada en la base de producción --
es la más nueva que había en el repo que tenía disponible. Si en el medio
se aplicó alguna otra migración que no estaba en ese repo (por ejemplo,
para las columnas de Mercado Pago o fecha_no_detectada, que existen en
models.py pero no tienen archivo de migración acá), "flask db upgrade" va
a fallar por no encontrar esa revisión como punto de partida.

Si eso pasa: correr "flask db heads" contra la base real para ver cuál es
la revisión "head" actual, y cambiar el valor de down_revision de acá
abajo por esa. Más seguro todavía: generar esta migración de cero con
"flask db migrate -m 'anti-abuso cuil repetido'" corriendo directo contra
una copia de la base de producción, que arma el down_revision solo, bien.
"""
from alembic import op
import sqlalchemy as sa


# revision identifiers, used by Alembic.
revision = 'c833df01b26c'
down_revision = '977821d0f9bd'
branch_labels = None
depends_on = None


def upgrade():
    with op.batch_alter_table('usuarios', schema=None) as batch_op:
        batch_op.add_column(sa.Column('tuvo_pago_alguna_vez', sa.Boolean(), nullable=True, server_default=sa.false()))

    op.create_table(
        'cuils_antiabuso',
        sa.Column('id', sa.Integer(), nullable=False),
        sa.Column('cuil', sa.String(length=20), nullable=False),
        sa.Column('primera_vez_en', sa.DateTime(), nullable=True),
        sa.Column('primer_usuario_id', sa.Integer(), nullable=True),
        sa.Column('primer_usuario_email', sa.String(length=200), nullable=True),
        sa.Column('primera_empresa_nombre', sa.String(length=200), nullable=True),
        sa.Column('desbloqueado', sa.Boolean(), nullable=True, server_default=sa.false()),
        sa.Column('nota_admin', sa.String(length=300), nullable=True),
        sa.PrimaryKeyConstraint('id'),
    )
    with op.batch_alter_table('cuils_antiabuso', schema=None) as batch_op:
        batch_op.create_index(batch_op.f('ix_cuils_antiabuso_cuil'), ['cuil'], unique=True)


def downgrade():
    with op.batch_alter_table('cuils_antiabuso', schema=None) as batch_op:
        batch_op.drop_index(batch_op.f('ix_cuils_antiabuso_cuil'))
    op.drop_table('cuils_antiabuso')

    with op.batch_alter_table('usuarios', schema=None) as batch_op:
        batch_op.drop_column('tuvo_pago_alguna_vez')
