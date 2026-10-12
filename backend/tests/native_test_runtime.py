"""Run CLI Swift test binaries in uniquely identified test-only app preference domains."""
from pathlib import Path
import os
import plistlib
import shutil
import subprocess
import uuid

def run_native_case(binary, args, work, *, domain=None, require_fresh=True, timeout=20):
    domain=domain or "com.agent.codex.native-tests."+uuid.uuid4().hex
    assert domain.startswith("com.agent.codex.native-tests.")
    app=Path(work)/(domain+".app")/"Contents"
    (app/"MacOS").mkdir(parents=True,exist_ok=True)
    exe=app/"MacOS"/"native-test"
    shutil.copy2(binary,exe)
    (app/"Info.plist").write_bytes(plistlib.dumps({"CFBundleIdentifier":domain,
        "CFBundleExecutable":"native-test","CFBundlePackageType":"APPL"}))
    env={**os.environ,"CODEX_NATIVE_TEST_DOMAIN":domain,
         "CODEX_NATIVE_TEST_FRESH":"1" if require_fresh else "0"}
    # No production preference domain is read, cleared, or modified by this helper.
    return subprocess.run([str(exe),*map(str,args)],capture_output=True,text=True,timeout=timeout,env=env)
