"""shift review_interaction score from 0-3 to 1-4

Revision ID: b8fe7cb59ea6
Revises: 1aae56295c18
Create Date: 2026-04-21 20:58:35.534574

"""
from typing import Sequence, Union

from alembic import op
import sqlalchemy as sa


# revision identifiers, used by Alembic.
revision: str = 'b8fe7cb59ea6'
down_revision: Union[str, Sequence[str], None] = '1aae56295c18'
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    """Shift existing scores +1 (0->1, 1->2, 2->3, 3->4), then narrow the
    CHECK constraint to 1-4 to match FSRS Rating values."""
    op.execute(sa.text("UPDATE review_interaction SET score = score + 1 WHERE score IS NOT NULL"))
    with op.batch_alter_table('review_interaction', schema=None) as batch_op:
        batch_op.drop_constraint('score_range', type_='check')
        batch_op.create_check_constraint('score_range', 'score >= 1 AND score <= 4')


def downgrade() -> None:
    """Restore 0-3 CHECK constraint and shift scores -1."""
    with op.batch_alter_table('review_interaction', schema=None) as batch_op:
        batch_op.drop_constraint('score_range', type_='check')
        batch_op.create_check_constraint('score_range', 'score >= 0 AND score <= 3')
    op.execute(sa.text("UPDATE review_interaction SET score = score - 1 WHERE score IS NOT NULL"))
