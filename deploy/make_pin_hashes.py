"""Write the dashboard's approval settings: salted PIN hashes, never the PINs.

    sudo /opt/aaba/.venv/bin/python /opt/aaba/deploy/make_pin_hashes.py

Reads the students' approval PINs from /etc/aaba/engine.env, refuses weak PINs (roll numbers,
IDs, common or sequential PINs), and writes /etc/aaba/dashboard-approvals.env (root:aabaweb 640)
with STUDENTn_APPROVAL_PIN_HASH=pbkdf2_sha256:... and AABA_APPROVALS_ON_DASHBOARD=1. Re-run it after
changing a PIN, then: sudo systemctl restart aaba-dashboard. Delete the file to switch dashboard
approvals off again.
"""
import grp
import os
import sys

sys.path.insert(0, "/opt/aaba")
from dotenv import dotenv_values  # noqa: E402

SRC, DST = "/etc/aaba/engine.env", "/etc/aaba/dashboard-approvals.env"
env = dotenv_values(SRC)
for k, v in env.items():                     # pin_problem() needs the student list only
    if v is not None and k.startswith("STUDENT"):
        os.environ.setdefault(k, v)
from aaba.approvals import hash_pin, pin_problem  # noqa: E402
from aaba.settings import load_students  # noqa: E402

lines = ["# written by deploy/make_pin_hashes.py: salted PIN hashes for dashboard approvals (no PINs)",
         "AABA_APPROVALS_ON_DASHBOARD=1"]
for st in load_students().students:
    pin = (env.get(f"{st.env_prefix}_APPROVAL_PIN") or "").strip()
    problem = pin_problem(pin)
    if problem:
        sys.exit(f"{st.student_id}: approval PIN not usable ({problem}); nothing written")
    lines.append(f"{st.env_prefix}_APPROVAL_PIN_HASH={hash_pin(pin)}")
tmp = DST + ".new"
with open(tmp, "w", encoding="utf-8") as f:
    f.write("\n".join(lines) + "\n")
os.chown(tmp, 0, grp.getgrnam("aabaweb").gr_gid)
os.chmod(tmp, 0o640)
os.replace(tmp, DST)
print(f"wrote {DST}: {len(lines) - 2} PIN hashes, dashboard approvals ON (restart aaba-dashboard)")
