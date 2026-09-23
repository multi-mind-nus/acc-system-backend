"""Add B6 AI integration baseline

Revision ID: 0009_b6_ai_baseline
Revises: 0008_submission_notes
Create Date: 2026-09-22 11:12:27.415228
"""

from collections.abc import Sequence

from alembic import op
import sqlalchemy as sa
from sqlalchemy.dialects import postgresql

revision: str = '0009_b6_ai_baseline'
down_revision: str | None = '0008_submission_notes'
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    op.create_unique_constraint('uq_submissions_firm_request_id', 'submissions', ['firm_id', 'request_id', 'id'])
    op.create_table('notification_outbox',
    sa.Column('id', sa.Uuid(), nullable=False),
    sa.Column('firm_id', sa.Uuid(), nullable=False),
    sa.Column('request_id', sa.Uuid(), nullable=False),
    sa.Column('channel', sa.String(length=16), server_default='EMAIL', nullable=False),
    sa.Column('recipient', sa.String(length=320), nullable=False),
    sa.Column('template', sa.String(length=64), nullable=False),
    sa.Column('payload', postgresql.JSONB(astext_type=sa.Text()), server_default=sa.text("'{}'::jsonb"), nullable=False),
    sa.Column('dedupe_key', sa.String(length=200), nullable=False),
    sa.Column('status', sa.String(length=16), server_default='SUPPRESSED', nullable=False),
    sa.Column('attempts', sa.Integer(), server_default='0', nullable=False),
    sa.Column('next_attempt_at', sa.DateTime(timezone=True), nullable=True),
    sa.Column('locked_by', sa.String(length=100), nullable=True),
    sa.Column('locked_until', sa.DateTime(timezone=True), nullable=True),
    sa.Column('last_error', sa.String(length=64), server_default='PROVIDER_DISABLED', nullable=False),
    sa.Column('sent_at', sa.DateTime(timezone=True), nullable=True),
    sa.Column('created_at', sa.DateTime(timezone=True), server_default=sa.text('now()'), nullable=False),
    sa.CheckConstraint("status = 'SUPPRESSED' AND last_error = 'PROVIDER_DISABLED' AND sent_at IS NULL", name='ck_outbox_delivery_disabled'),
    sa.ForeignKeyConstraint(['firm_id', 'request_id'], ['collection_requests.firm_id', 'collection_requests.id'], ),
    sa.ForeignKeyConstraint(['firm_id'], ['firms.id'], ),
    sa.PrimaryKeyConstraint('id'),
    sa.UniqueConstraint('firm_id', 'dedupe_key', name='uq_notification_outbox_dedupe')
    )
    op.create_table('ai_runs',
    sa.Column('id', sa.Uuid(), nullable=False),
    sa.Column('firm_id', sa.Uuid(), nullable=False),
    sa.Column('request_id', sa.Uuid(), nullable=False),
    sa.Column('submission_id', sa.Uuid(), nullable=True),
    sa.Column('purpose', sa.String(length=16), nullable=False),
    sa.Column('status', sa.String(length=16), server_default='DRAFT', nullable=False),
    sa.Column('model_version', sa.String(length=200), nullable=True),
    sa.Column('input_snapshot', postgresql.JSONB(astext_type=sa.Text()), server_default=sa.text("'{}'::jsonb"), nullable=False),
    sa.Column('output', postgresql.JSONB(astext_type=sa.Text()), nullable=True),
    sa.Column('error', sa.String(length=64), nullable=True),
    sa.Column('requested_by', sa.Uuid(), nullable=True),
    sa.Column('attempts', sa.Integer(), server_default='0', nullable=False),
    sa.Column('next_attempt_at', sa.DateTime(timezone=True), nullable=True),
    sa.Column('locked_by', sa.String(length=100), nullable=True),
    sa.Column('locked_until', sa.DateTime(timezone=True), nullable=True),
    sa.Column('created_at', sa.DateTime(timezone=True), server_default=sa.text('now()'), nullable=False),
    sa.Column('finished_at', sa.DateTime(timezone=True), nullable=True),
    sa.CheckConstraint("purpose <> 'REVIEW' OR submission_id IS NOT NULL", name='ck_ai_runs_review_submission'),
    sa.CheckConstraint("purpose IN ('CLASSIFY', 'REVIEW')", name='ck_ai_runs_purpose'),
    sa.CheckConstraint("status IN ('DRAFT', 'QUEUED', 'PROCESSING', 'SUCCEEDED', 'FAILED', 'CANCELLED')", name='ck_ai_runs_status'),
    sa.CheckConstraint('attempts >= 0', name='ck_ai_runs_attempts'),
    sa.ForeignKeyConstraint(['firm_id', 'request_id', 'submission_id'], ['submissions.firm_id', 'submissions.request_id', 'submissions.id'], ),
    sa.ForeignKeyConstraint(['firm_id', 'request_id'], ['collection_requests.firm_id', 'collection_requests.id'], ),
    sa.ForeignKeyConstraint(['firm_id', 'requested_by'], ['users.firm_id', 'users.id'], ),
    sa.ForeignKeyConstraint(['firm_id'], ['firms.id'], ),
    sa.PrimaryKeyConstraint('id'),
    sa.UniqueConstraint('firm_id', 'submission_id', 'id', name='uq_ai_runs_firm_submission_id')
    )
    op.create_index('ix_ai_runs_queue', 'ai_runs', ['status', 'next_attempt_at'], unique=False)
    op.create_index('ix_ai_runs_request_created', 'ai_runs', ['request_id', 'created_at'], unique=False)
    op.add_column('collection_requests', sa.Column('ai_mode', sa.String(length=16), server_default='AUTO_REVIEW', nullable=False))
    op.execute("UPDATE collection_requests SET ai_mode = 'SUGGEST'")
    op.add_column('collection_requests', sa.Column('ai_satisfy_threshold', sa.Numeric(precision=4, scale=3), server_default='0.980', nullable=False))
    op.add_column('collection_requests', sa.Column('ai_request_action_threshold', sa.Numeric(precision=4, scale=3), server_default='0.980', nullable=False))
    op.create_check_constraint('ck_collection_ai_mode', 'collection_requests', "ai_mode IN ('OFF', 'SUGGEST', 'AUTO_REVIEW')")
    op.create_check_constraint('ck_collection_ai_thresholds', 'collection_requests', 'ai_satisfy_threshold BETWEEN 0.500 AND 1.000 AND ai_request_action_threshold BETWEEN 0.500 AND 1.000')
    op.add_column('documents', sa.Column('document_type', sa.String(length=64), nullable=True))
    op.add_column('documents', sa.Column('entity_name', sa.String(length=200), nullable=True))
    op.add_column('documents', sa.Column('period', sa.Date(), nullable=True))
    op.add_column('documents', sa.Column('extracted_data', postgresql.JSONB(astext_type=sa.Text()), nullable=True))
    op.create_index('ix_documents_analysis_search', 'documents', ['firm_id', 'client_id', 'period', 'document_type', 'created_at'], unique=False)
    op.add_column('requirements', sa.Column('analysis_type', sa.String(length=40), server_default='DOCUMENT_REQUIREMENT_VALIDATION', nullable=False))
    op.create_check_constraint('ck_requirement_analysis_type', 'requirements', "analysis_type IN ('DOCUMENT_REQUIREMENT_VALIDATION', 'BANK_TRANSACTION_RECONCILIATION')")
    op.add_column('review_decisions', sa.Column('source', sa.String(length=16), server_default='HUMAN', nullable=False))
    op.add_column('review_decisions', sa.Column('ai_run_id', sa.Uuid(), nullable=True))
    op.alter_column('review_decisions', 'created_by',
               existing_type=sa.UUID(),
               nullable=True)
    op.create_index('uq_review_ai_run_requirement', 'review_decisions', ['ai_run_id', 'requirement_id'], unique=True, postgresql_where=sa.text("source = 'AI'"))
    op.create_foreign_key('fk_review_requirement_firm', 'review_decisions', 'requirements', ['firm_id', 'requirement_id'], ['firm_id', 'id'])
    op.create_foreign_key('fk_review_ai_run_submission', 'review_decisions', 'ai_runs', ['firm_id', 'submission_id', 'ai_run_id'], ['firm_id', 'submission_id', 'id'])
    op.create_check_constraint('ck_review_decision_source', 'review_decisions', "(source = 'HUMAN' AND created_by IS NOT NULL AND ai_run_id IS NULL) OR (source = 'AI' AND created_by IS NULL AND ai_run_id IS NOT NULL AND decision IN ('SATISFY', 'REQUEST_ACTION'))")
    op.add_column('workflow_events', sa.Column('actor_type', sa.String(length=16), server_default='USER', nullable=False))
    op.alter_column('workflow_events', 'actor_id',
               existing_type=sa.UUID(),
               nullable=True)
    op.create_check_constraint('ck_workflow_actor', 'workflow_events', "(actor_type = 'USER' AND actor_id IS NOT NULL) OR (actor_type = 'SYSTEM' AND actor_id IS NULL)")


def downgrade() -> None:
    # Do not discard AI audit history or attribute SYSTEM actions to a human.
    if op.get_bind().execute(sa.text("SELECT EXISTS (SELECT 1 FROM workflow_events WHERE actor_type = 'SYSTEM') OR EXISTS (SELECT 1 FROM ai_runs) OR EXISTS (SELECT 1 FROM notification_outbox)")).scalar():
        raise RuntimeError('Downgrade would discard AI audit data; retain the schema and roll back the application instead')
    op.drop_constraint('ck_workflow_actor', 'workflow_events', type_='check')
    op.drop_constraint('ck_review_decision_source', 'review_decisions', type_='check')
    op.drop_constraint('ck_requirement_analysis_type', 'requirements', type_='check')
    op.drop_constraint('ck_collection_ai_thresholds', 'collection_requests', type_='check')
    op.drop_constraint('ck_collection_ai_mode', 'collection_requests', type_='check')
    op.alter_column('workflow_events', 'actor_id',
               existing_type=sa.UUID(),
               nullable=False)
    op.drop_column('workflow_events', 'actor_type')
    op.drop_constraint('fk_review_ai_run_submission', 'review_decisions', type_='foreignkey')
    op.drop_constraint('fk_review_requirement_firm', 'review_decisions', type_='foreignkey')
    op.drop_index('uq_review_ai_run_requirement', table_name='review_decisions', postgresql_where=sa.text("source = 'AI'"))
    op.alter_column('review_decisions', 'created_by',
               existing_type=sa.UUID(),
               nullable=False)
    op.drop_column('review_decisions', 'ai_run_id')
    op.drop_column('review_decisions', 'source')
    op.drop_column('requirements', 'analysis_type')
    op.drop_index('ix_documents_analysis_search', table_name='documents')
    op.drop_column('documents', 'extracted_data')
    op.drop_column('documents', 'period')
    op.drop_column('documents', 'entity_name')
    op.drop_column('documents', 'document_type')
    op.drop_column('collection_requests', 'ai_request_action_threshold')
    op.drop_column('collection_requests', 'ai_satisfy_threshold')
    op.drop_column('collection_requests', 'ai_mode')
    op.drop_index('ix_ai_runs_request_created', table_name='ai_runs')
    op.drop_index('ix_ai_runs_queue', table_name='ai_runs')
    op.drop_table('ai_runs')
    op.drop_constraint('uq_submissions_firm_request_id', 'submissions', type_='unique')
    op.drop_table('notification_outbox')
