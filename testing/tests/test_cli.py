from pathlib import Path
import pytest
from market_env.__main__ import main


@pytest.mark.parametrize("command", ["download", "follow"])
def test_missing_credentials_clean_exit(tmp_path, command, capsys):
    args = ["--project", str(tmp_path), command]
    if command == "download":
        args += ["--start", "2026-09-01T00:00Z", "--end", "2026-09-05T00:00Z"]
    assert main(args) == 2
    assert "Missing ALPACA_API_KEY" in capsys.readouterr().err


def test_demo_console_replay_export_and_zero_startup_orders(tmp_path, monkeypatch, capsys):
    commands = iter(["prices", "status", "portfolio", "orders", "buy KO 5", "cancel o000001",
                     "buy KO 2", "next", "sell KO 1", "next", "orders", "export", "quit"])
    monkeypatch.setattr("builtins.input", lambda _: next(commands))
    assert main(["--project", str(tmp_path), "demo"]) == 0
    output = capsys.readouterr().out
    assert "SYNTHETIC" in output and '"filled"' in output and '"canceled": true' in output
    assert len(list((tmp_path / "runs").iterdir())) == 2
    commands = iter(["orders", "quit"])
    monkeypatch.setattr("builtins.input", lambda _: next(commands))
    assert main(["--project", str(tmp_path), "replay", "--data", "data/SYNTHETIC.csv"]) == 0
    assert "[]" in capsys.readouterr().out


def test_cli_rejects_paths_outside_project(tmp_path, capsys):
    (tmp_path / "config.toml").write_text('data_dir = "../outside"\n')
    assert main(["--project", str(tmp_path), "demo"]) == 2
    assert "inside the project" in capsys.readouterr().err
