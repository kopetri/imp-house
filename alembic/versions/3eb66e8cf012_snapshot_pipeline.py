"""replace capture videos with snapshot pipeline artifacts"""

from collections.abc import Sequence

import sqlalchemy as sa

from alembic import op

# revision identifiers, used by Alembic.
revision: str = "3eb66e8cf012"
down_revision: str | Sequence[str] | None = "10636aa1b671"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    with op.batch_alter_table("prompts") as batch_op:
        batch_op.drop_column("extra_input")
        batch_op.drop_column("prompt_input_key")
        batch_op.drop_column("video_input_key")
        batch_op.drop_column("model")

    with op.batch_alter_table("recording_settings") as batch_op:
        batch_op.drop_column("min_seconds")
        batch_op.drop_column("max_seconds")
    with op.batch_alter_table("scheduled_recordings") as batch_op:
        batch_op.drop_column("duration_seconds")

    with op.batch_alter_table("clips") as batch_op:
        batch_op.alter_column("original_path", existing_type=sa.String(length=500), nullable=True)
        batch_op.drop_column("model_input")
        batch_op.add_column(sa.Column("snapshot_path", sa.String(length=500), nullable=True))
        batch_op.add_column(sa.Column("mask_path", sa.String(length=500), nullable=True))
        batch_op.add_column(sa.Column("reference_path", sa.String(length=500), nullable=True))
        batch_op.add_column(sa.Column("scene_prompt", sa.Text(), nullable=True))
        batch_op.add_column(sa.Column("mask_bbox", sa.JSON(), nullable=True))
        batch_op.add_column(sa.Column("pipeline_stage", sa.String(length=30), nullable=True))
        batch_op.add_column(sa.Column("provider_output", sa.JSON(), nullable=True))


def downgrade() -> None:
    op.execute("UPDATE clips SET original_path = thumbnail_path WHERE original_path IS NULL")
    with op.batch_alter_table("clips") as batch_op:
        batch_op.drop_column("provider_output")
        batch_op.drop_column("pipeline_stage")
        batch_op.drop_column("mask_bbox")
        batch_op.drop_column("scene_prompt")
        batch_op.drop_column("reference_path")
        batch_op.drop_column("mask_path")
        batch_op.drop_column("snapshot_path")
        batch_op.add_column(sa.Column("model_input", sa.JSON(), nullable=True))
        batch_op.alter_column("original_path", existing_type=sa.String(length=500), nullable=False)
    with op.batch_alter_table("scheduled_recordings") as batch_op:
        batch_op.add_column(sa.Column("duration_seconds", sa.Integer(), nullable=True))
    op.execute("UPDATE scheduled_recordings SET duration_seconds = 15")
    with op.batch_alter_table("scheduled_recordings") as batch_op:
        batch_op.alter_column("duration_seconds", existing_type=sa.Integer(), nullable=False)
    with op.batch_alter_table("recording_settings") as batch_op:
        batch_op.add_column(sa.Column("max_seconds", sa.Integer(), nullable=True))
        batch_op.add_column(sa.Column("min_seconds", sa.Integer(), nullable=True))
    op.execute("UPDATE recording_settings SET min_seconds = 10, max_seconds = 20")
    with op.batch_alter_table("recording_settings") as batch_op:
        batch_op.alter_column("max_seconds", existing_type=sa.Integer(), nullable=False)
        batch_op.alter_column("min_seconds", existing_type=sa.Integer(), nullable=False)
    with op.batch_alter_table("prompts") as batch_op:
        batch_op.add_column(sa.Column("model", sa.String(length=200), nullable=True))
        batch_op.add_column(sa.Column("video_input_key", sa.String(length=50), nullable=True))
        batch_op.add_column(sa.Column("prompt_input_key", sa.String(length=50), nullable=True))
        batch_op.add_column(sa.Column("extra_input", sa.JSON(), nullable=True))
    op.execute(
        "UPDATE prompts SET model = 'cjwbw/controlvideo', video_input_key = 'video_path', "
        "prompt_input_key = 'prompt', extra_input = '{}'"
    )
    with op.batch_alter_table("prompts") as batch_op:
        batch_op.alter_column("model", existing_type=sa.String(length=200), nullable=False)
        batch_op.alter_column("video_input_key", existing_type=sa.String(length=50), nullable=False)
        batch_op.alter_column("prompt_input_key", existing_type=sa.String(length=50), nullable=False)
        batch_op.alter_column("extra_input", existing_type=sa.JSON(), nullable=False)