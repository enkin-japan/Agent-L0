"""Launcher regression: a detached deployment needs a durable interpreter."""
import importlib.util
import os
from pathlib import Path
import shlex
import shutil
import subprocess
import sys
from unittest.mock import patch

SOURCE = Path(__file__).parent


def test_shell_uses_persistent_runtime_after_launcher_exit(tmp_path):
    directory = tmp_path / "colab"
    directory.mkdir()
    shutil.copy(SOURCE / "deploy.sh", directory / "deploy.sh")
    (directory / "compute_ctl.py").write_text("print('synthetic launcher complete')\n")
    fake_bin = tmp_path / "bin"
    fake_bin.mkdir()
    fake_uv = fake_bin / "uv"
    fake_uv.write_text("#!" + sys.executable + "\n" + r'''
import pathlib,sys
if sys.argv[1]=='venv':
 root=pathlib.Path(sys.argv[-1]); (root/'bin').mkdir(parents=True)
 target=root/'bin/python'
 target.write_text('#!/bin/sh\nexec '+%r+' "$@"\n');target.chmod(0o700)
elif sys.argv[1]=='pip': pass
else: raise SystemExit('temporary uv run environment forbidden')
''' % repr(shlex.quote(sys.executable)))
    fake_uv.chmod(0o700)
    env = dict(os.environ, PATH=str(fake_bin) + os.pathsep + os.environ["PATH"])
    result = subprocess.run(["bash", str(directory / "deploy.sh"), "prepare"],
                            env=env, capture_output=True, text=True)
    assert result.returncode == 0, result.stderr
    interpreter = tmp_path / ".colab-compute/launcher-venv/bin/python"
    child = subprocess.run([str(interpreter), "-c", "print('survived launcher')"],
                           capture_output=True, text=True)
    assert child.returncode == 0 and "survived launcher" in child.stdout


def test_rag_uses_cpu_without_changing_inference_gpu_environment():
    spec = importlib.util.spec_from_file_location("vm_runtime_test", SOURCE / "compute_vm.py")
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    env = {"PATH": "/synthetic", "CUDA_VISIBLE_DEVICES": "0"}
    with patch.object(module, "run") as run:
        module.start_compute_services(env)
    assert len(run.call_args_list) == 2
    rag, infer = run.call_args_list
    assert rag.args[-2:] == ("start", "rag-compute")
    assert rag.kwargs["env"]["CUDA_VISIBLE_DEVICES"] == ""
    assert infer.args[-2:] == ("start", "infer")
    assert infer.kwargs["env"]["CUDA_VISIBLE_DEVICES"] == "0"
    assert env["CUDA_VISIBLE_DEVICES"] == "0"
