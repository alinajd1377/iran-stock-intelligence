"""Add normalized Codal report sheet and table archive tables."""
from alembic import op
from app import models

revision = "0002_codal_report_tables"
down_revision = "0001_initial"
branch_labels = None
depends_on = None

def upgrade():
    bind = op.get_bind()
    models.CodalReportSheet.__table__.create(bind=bind, checkfirst=True)
    models.CodalReportTable.__table__.create(bind=bind, checkfirst=True)

def downgrade():
    bind = op.get_bind()
    models.CodalReportTable.__table__.drop(bind=bind, checkfirst=True)
    models.CodalReportSheet.__table__.drop(bind=bind, checkfirst=True)
