"""add_last_active_to_users

Revision ID: bd0ce0cc1130
Revises: 06cdc0c24d9d
Create Date: 2025-07-28 04:16:44.974263

"""
from typing import Sequence, Union

from alembic import op
import sqlalchemy as sa


# revision identifiers, used by Alembic.
revision: str = 'bd0ce0cc1130'
down_revision: Union[str, Sequence[str], None] = '06cdc0c24d9d'
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade():
    op.add_column('users', sa.Column('last_active', sa.DateTime(), nullable=True))

def downgrade():
    op.drop_column('users', 'last_active')
