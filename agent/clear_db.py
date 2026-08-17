#!/usr/bin/env python3
"""清空 agent 数据库所有表的数据（保留表结构）。

用法：
    cd /home/lyr/Main/Softer/软工三/projects/agent
    source venv/bin/activate
    python3 clear_db.py
"""

import os
import sys

# ensure we can import backend
sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))

from backend.db.session import SessionLocal
from backend.db.models import (
    AnalysisSession, ClaimRecord, EventRecord,
    ToolCallRecord, SummaryRecord, ChatMessage, SkillRecord,
)

TABLES = [
    ("event_record", EventRecord),
    ("tool_call_record", ToolCallRecord),
    ("claim_record", ClaimRecord),
    ("summary_record", SummaryRecord),
    ("chat_message", ChatMessage),
    ("skill_record", SkillRecord),
    ("analysis_session", AnalysisSession),
]


def clear_all():
    db = SessionLocal()
    try:
        for name, model in TABLES:
            count = db.query(model).delete()
            print(f"  {name}: 删除了 {count} 条记录")
        db.commit()
        print("\n✅ 所有数据已清空，表结构保留。")
    except Exception as e:
        db.rollback()
        print(f"\n❌ 清空失败: {e}")
        sys.exit(1)
    finally:
        db.close()


if __name__ == "__main__":
    print("清空数据库所有表...\n")
    clear_all()
