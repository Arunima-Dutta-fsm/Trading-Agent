"""Public read-only demo of the AABA dashboard (Streamlit Community Cloud main file).

    streamlit run app/demo_app.py

Before anything from aaba is imported, this file switches on demo mode: simulated prices,
the simulated broker, no .env, no secrets, and a throwaway database in the temp folder.
Every action that could trade, approve, change settings or start a run is refused on the
server side (aaba/demo.py). It never touches the real trading database or a real account.
"""
from __future__ import annotations

import os
import re
import runpy
import sys
import tempfile
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
BASE = Path(tempfile.gettempdir()) / "aaba-demo"

# no real credential can be used by the demo, even if one is set on the host
_SECRETISH = re.compile(r"(API_KEY|SECRET|TOKEN|_PIN$|PASSWORD|ACCOUNT_NUMBER|^LLM_|^GROQ_|^GEMINI_|^GOOGLE_"
                        r"|^OLLAMA_|^OPENAI_|^OPENROUTER_|^FINNHUB_)", re.I)
for _k in [k for k in os.environ if _SECRETISH.search(k)]:
    os.environ.pop(_k, None)

os.environ.update({
    "AABA_DEMO_MODE": "1",
    "AABA_SKIP_DOTENV": "1",
    "AABA_DATA_PROVIDER": "synthetic",
    "AABA_BROKER": "fake",
    "ENABLE_PAPER_ORDERS": "false",
    "AABA_SYNTHETIC_MARKET_OPEN": "true",
    "AABA_DATA_DIR": str(BASE / "data"),
    "AABA_DB_PATH": str(BASE / "data" / "aaba.db"),
    "AABA_MODEL_RUN_DIR": str(BASE / "model-run"),
    "AABA_LOG_DIR": str(BASE / "logs"),
    "AABA_REPORT_DIR": str(BASE / "reports"),
    "CREWAI_TRACING_ENABLED": "false",
    "CREWAI_DISABLE_TELEMETRY": "true",
    "OTEL_SDK_DISABLED": "true",
})
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from app.demo_seed import DEMO_DAY, ensure_seeded  # noqa: E402

os.environ["AABA_SYNTHETIC_NOW"] = DEMO_DAY
ensure_seeded()
runpy.run_path(str(ROOT / "app" / "streamlit_app.py"), run_name="__main__")
