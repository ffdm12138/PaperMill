"""Quick syntax check for pack_repo.py and agent_acceptance.py."""
import ast
import sys

ok = True
for f in ["scripts/pack_repo.py", "scripts/agent_acceptance.py"]:
    try:
        ast.parse(open(f, encoding="utf-8").read())
        print(f"  {f}: Syntax OK")
    except SyntaxError as e:
        print(f"  {f}: FAIL - {e}")
        ok = False

sys.exit(0 if ok else 1)
