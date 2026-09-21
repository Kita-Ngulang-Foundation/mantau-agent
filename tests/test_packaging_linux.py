from pathlib import Path


ROOT = Path(__file__).resolve().parents[1]


def test_systemd_unit_runs_unprivileged_with_restart_and_safe_paths():
    unit = (ROOT / "packaging/systemd/mantau-agent.service").read_text(encoding="utf-8")
    assert "User=mantau-agent" in unit
    assert "Restart=on-failure" in unit
    assert "MANTAU_CONFIG_PATH" not in unit  # supplied explicitly as a CLI argument
    assert "/etc/mantau-agent/config.json run" in unit
    assert "MANTAU_SPOOL_PATH=/var/lib/mantau-agent/spool.db" in unit
    assert "ProtectSystem=strict" in unit
    assert "UMask=0077" in unit


def test_installer_supports_x86_64_and_arm64_and_does_not_claim_android():
    script = (ROOT / "packaging/install.sh").read_text(encoding="utf-8")
    assert "x86_64|amd64" in script
    assert "aarch64|arm64" in script
    assert "mantau-agent-linux-arm64" in script
    assert "Android" not in script and "Termux" not in script
    assert "systemctl enable --now" in script
    assert "runuser -u" in script
    assert "--status-path $DATA_DIR/status.json status --json" in script


def test_uninstall_preserves_state_unless_purge_is_explicit():
    script = (ROOT / "packaging/uninstall.sh").read_text(encoding="utf-8")
    assert 'if [ "$PURGE" -eq 1 ]' in script
    assert "Default uninstall preserves" in script
    assert "/etc/mantau-agent /var/lib/mantau-agent" in script


def test_build_script_targets_both_linux_architectures():
    script = (ROOT / "packaging/build-linux.sh").read_text(encoding="utf-8")
    assert "linux/amd64" in script
    assert "linux/arm64" in script
    assert "mantau-agent-linux-x64" in script
    assert "mantau-agent-linux-arm64" in script


def test_release_notes_only_present_supported_linux_profiles():
    notes = (ROOT / "packaging/release-notes-v0.1.0.md").read_text(encoding="utf-8")
    assert "Linux x86_64" in notes
    assert "Raspberry Pi" in notes
    assert "Termux" not in notes
    assert "Windows PC" not in notes
