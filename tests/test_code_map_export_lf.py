"""File-export contract; Windows text translation is simulated, not native."""

import builtins
import io
import json
import os
import subprocess
import sys
from pathlib import Path

import pytest

from kindex.config import Config
from kindex.store import Store
from kindex.vectors import PROVIDER_DEFAULTS


@pytest.mark.parametrize("platform_newline", ["\n", "\r\n"], ids=["unix", "simulated-windows"])
def test_cli_code_map_file_is_utf8_lf_and_safecrlf_compatible(
    tmp_path, monkeypatch, capsys, platform_newline
):
    """Exercise the CLI writer and inspect bytes before any universal-newline read."""
    repo = tmp_path / "repo"
    repo.mkdir()
    (repo / ".kin").mkdir()
    output = repo / ".kin" / "code-map.json"
    probe = repo / "newline-probe.txt"
    data_dir = tmp_path / "data"
    config = tmp_path / "kin.yaml"
    config.write_text("data_dir: " + json.dumps(str(data_dir)) + "\n", encoding="utf-8")
    monkeypatch.chdir(repo)
    for name in ("HOME", "XDG_CONFIG_HOME", "XDG_DATA_HOME", "XDG_STATE_HOME"):
        monkeypatch.setenv(name, str(tmp_path / name.lower()))
    monkeypatch.setenv("KIN_NO_SCHEDULER_WRITES", "1")
    monkeypatch.delenv("KIN_PROFILE", raising=False)
    for name in {
        "ANTHROPIC_API_KEY", "OPENAI_API_KEY", "GEMINI_API_KEY", "GOOGLE_API_KEY",
        *(entry["api_key_env"] for entry in PROVIDER_DEFAULTS.values()
          if entry.get("api_key_env")),
    }:
        monkeypatch.delenv(name, raising=False)

    store = Store(Config(data_dir=str(data_dir)))
    try:
        store.add_node(
            "src/café.py",
            node_id="code-mod-lf-cafe",
            node_type="artifact",
            domains=["code", "python"],
            prov_activity="code-ingest",
            extra={"relative_path": "src/café.py", "language": "Python"},
        )
    finally:
        store.close()

    # Both pathlib and built-in open delegate to this shim. Only the export
    # destination and calibration probe are affected. Explicit newline values
    # remain untouched: newline="\n" must suppress Windows CRLF translation.
    real_open = io.open

    def platform_open(file, mode="r", buffering=-1, encoding=None, errors=None,
                      newline=None, closefd=True, opener=None):
        is_target = isinstance(file, (str, bytes, os.PathLike)) and (
            Path(os.fsdecode(file)).absolute() in {output, probe}
        )
        if is_target and "b" not in mode and any(flag in mode for flag in "wax"):
            raw = real_open(file, mode.replace("t", "") + "b", buffering=buffering,
                            closefd=closefd, opener=opener)
            return io.TextIOWrapper(
                raw, encoding=encoding, errors=errors,
                newline=platform_newline if newline is None else newline,
            )
        return real_open(file, mode, buffering, encoding, errors, newline,
                         closefd, opener)

    monkeypatch.setattr(io, "open", platform_open)
    monkeypatch.setattr(builtins, "open", platform_open)
    probe.write_text("probe\n", encoding="utf-8")
    assert probe.read_bytes() == b"probe" + platform_newline.encode("ascii")

    from kindex.cli import main

    monkeypatch.setattr(sys, "argv", [
        "kin", "export", "code-map", "--format", "understand-anything",
        "--project-name", "café", "--output", str(output),
        "--config", str(config), "--data-dir", str(data_dir),
    ])
    try:
        main()
    except SystemExit as exc:
        assert exc.code in (None, 0), capsys.readouterr()

    payload = output.read_bytes()
    graph = json.loads(payload.decode("utf-8"))
    assert graph["project"]["name"] == "café"
    assert graph["nodes"][0]["id"] == "code-mod-lf-cafe"
    assert graph["nodes"][0]["filePath"] == "src/café.py"
    assert b"\n" in payload, "Export should contain real LF line endings"
    assert b"\r" not in payload, "Export must use LF bytes even under Windows translation"

    # Stage only in this disposable repository, without consulting user Git
    # configuration or hooks. This is the consumer contract for generated JSON.
    (repo / ".gitattributes").write_bytes(b".kin/*.json text eol=lf\n")
    git_env = dict(os.environ, GIT_CONFIG_NOSYSTEM="1", GIT_CONFIG_GLOBAL=os.devnull)
    for args in (
        ["init", "--quiet"],
        ["-c", "core.safecrlf=true", "-c", "core.autocrlf=false",
         "add", "--", ".kin/code-map.json"],
    ):
        result = subprocess.run(["git", *args], cwd=repo, env=git_env,
                                capture_output=True, text=True, timeout=30)
        assert result.returncode == 0, result.stderr
