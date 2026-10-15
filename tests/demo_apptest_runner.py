"""Renders the read-only demo (app/demo_app.py) with Streamlit's AppTest and prints a JSON
summary. Run in its own process by tests/test_demo_mode.py, so demo-mode environment
variables never leak into other tests."""
import json
import sys
import tempfile
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))

import os  # noqa: E402

os.environ["TMP"] = os.environ["TEMP"] = tempfile.mkdtemp(prefix="aaba-demo-test-")   # fresh demo database
tempfile.tempdir = None

from streamlit.testing.v1 import AppTest  # noqa: E402

os.environ["AABA_DASHBOARD_PAGE"] = sys.argv[1] if len(sys.argv) > 1 else "overview"
at = AppTest.from_file(str(ROOT / "app" / "demo_app.py"), default_timeout=120).run()


def texts(a):
    out = []
    for kind in ("markdown", "title", "caption", "info", "warning", "error", "subheader", "header"):
        out += [str(getattr(x, "value", "")) for x in getattr(a, kind, [])]
    for df in a.dataframe:
        out.append(df.value.to_csv(index=False))
    return " ".join(out)


print(json.dumps({
    "exception": [str(e.value)[:300] for e in at.exception],
    "text": texts(at),
    "buttons": [b.label for b in at.button],
    "text_inputs": [t.label for t in at.text_input],
    "env_demo": os.environ.get("AABA_DEMO_MODE"),
    "db": os.environ.get("AABA_DB_PATH"),
}))
