"""
Adds the suno_account_creation_queue table to the Odysseus database.
Works seamlessly with both SQLite (default) and PostgreSQL/Neon.
"""
import os
import sys
from datetime import datetime
from sqlalchemy import Column, String, Boolean, DateTime, JSON, Text, Index

# Ensure the Odysseus root is in the path
sys.path.insert(0, '/Users/growthgod/Desktop/VAN/odysseus')

# Import the existing SQLAlchemy engine and Base from Odysseus
try:
    from core.database import engine, Base
except ImportError:
    print("❌ Could not import Odysseus database config. Are you in the correct directory?")
    sys.exit(1)

class SunoAccountCreationQueue(Base):
    __tablename__ = "suno_account_creation_queue"
    
    id = Column(String, primary_key=True, index=True)
    email = Column(String, unique=True, nullable=False)
    password = Column(String, nullable=False)
    assigned_device_id = Column(String, nullable=False)  # Links to vmos_devices.pad_code
    status = Column(String, default='pending')           # 'pending', 'in_progress', 'completed', 'failed'
    suno_handle = Column(String, nullable=True)          # Populated upon successful creation
    session_cookies = Column(JSON, nullable=True)        # Harvested cookies upon success
    ig_connected = Column(Boolean, default=False)        # Track if Instagram link step succeeded
    error_message = Column(Text, nullable=True)
    created_at = Column(DateTime, default=datetime.utcnow)
    updated_at = Column(DateTime, default=datetime.utcnow, onupdate=datetime.utcnow)

def migrate():
    db_url = os.getenv("DATABASE_URL", "sqlite:///./data/app.db")
    print(f"🔌 Connecting to database: {db_url}")
    
    try:
        # Create the table if it doesn't exist (safe for repeated runs)
        SunoAccountCreationQueue.__table__.create(engine, checkfirst=True)
        print("✅ Table 'suno_account_creation_queue' created successfully!")
        
        # Create the status index for faster queue polling
        with engine.begin() as conn:
            idx = Index('idx_suno_queue_status', SunoAccountCreationQueue.status)
            idx.create(engine, checkfirst=True)
        print("✅ Index 'idx_suno_queue_status' created successfully!")
        
        print("\n🎉 Database schema is ready. You can now run the ingestion script.")
        
    except Exception as e:
        print(f"❌ Error creating table: {e}")

if __name__ == "__main__":
    migrate()
