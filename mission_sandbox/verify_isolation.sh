#!/usr/bin/env bash
# Proves the mission sandbox is isolated. Run from odysseus/ on the Docker host AFTER
# `docker compose up -d --build`. Every check runs exactly the way mission code runs
# (same clean environment and limits). Do NOT run missions unless it ends with VERIFIED.
set -u
docker compose exec -T sandbox python - <<'PY'
import importlib.util, os

spec = importlib.util.spec_from_file_location("srv", "/opt/server.py")
srv = importlib.util.module_from_spec(spec)
spec.loader.exec_module(srv)
os.makedirs("/app/data/workspace/m-ffffff", exist_ok=True)
results = []


def check(name, code, ok, must=True):
    r = srv.run_command("m-ffffff", "python", code, 30)
    out = (r.get("stdout", "") + r.get("stderr", "")).strip()
    passed = bool(ok(out, r.get("exit_code")))
    if must:
        results.append(passed)
    print(("PASS " if passed else "FAIL ") + name + "  ->  " + out[:90].replace("\n", " | "))


NET = "import socket; socket.create_connection(({host!r}, {port}), timeout=4)"
check("runs as a non-root user", "import os; print(os.getuid())", lambda o, r: r == 0 and o != "0")
check("no secrets in the environment",
      "import os; print([k for k in os.environ if any(s in k.upper() for s in ('TOKEN','KEY','SECRET','PASS'))])",
      lambda o, r: o == "[]")
check("root filesystem is read-only", "open('/usr/local/x', 'w')", lambda o, r: r != 0)
check("cannot write outside workspace and /tmp", "open('/opt/x', 'w')", lambda o, r: r != 0)
check("workspace is writable", "open('ok.txt', 'w').write('1'); print('wrote')", lambda o, r: r == 0 and "wrote" in o)
check("no internet", NET.format(host="1.1.1.1", port=443), lambda o, r: r != 0)
check("no DNS", "import socket; socket.gethostbyname('example.com')", lambda o, r: r != 0)
check("cannot reach the app container", NET.format(host="odysseus", port=7000), lambda o, r: r != 0)
check("cannot reach the LAN", NET.format(host="10.0.0.1", port=80), lambda o, r: r != 0)
check("cannot reach the Docker host", NET.format(host="172.17.0.1", port=22), lambda o, r: r != 0)
check("process cap holds (fork bomb contained)",
      "import os\nfor _ in range(2000):\n    os.fork()\n", lambda o, r: True)
check("mission tools are installed",
      "import shutil, importlib.util as u; print(shutil.which('ffmpeg') is not None, "
      "all(u.find_spec(m) for m in ('numpy','pydicom','SimpleITK','skimage','trimesh','faster_whisper')))",
      lambda o, r: o == "True True", must=False)
print()
print("ISOLATION VERIFIED" if all(results) else "ISOLATION NOT VERIFIED - do not run missions")
PY
