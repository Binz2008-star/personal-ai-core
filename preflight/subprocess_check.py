import json
import subprocess
import sys

pytest_path, cwd = sys.argv[1], sys.argv[2]
p = subprocess.run(
    [pytest_path, "-q", "-p", "no:cacheprovider", "test_ok.py"],
    cwd=cwd, capture_output=True, text=True,
)
print(json.dumps({"returncode": p.returncode, "stdout": p.stdout, "stderr": p.stderr}))
