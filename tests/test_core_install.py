"""Exercise binary integrity, bounded backup and config-preserving rollback."""
import hashlib,json,subprocess,tempfile,unittest
from pathlib import Path
class CoreInstallTests(unittest.TestCase):
    def test_verified_swap_backup_and_guarded_rollback(self):
        with tempfile.TemporaryDirectory() as d:
            root=Path(d);(root/'state').mkdir();old=root/'sing-box';old.write_text('#!/bin/sh\necho original\n');old.chmod(0o755)
            new=root/'candidate';new.write_text('#!/bin/sh\necho "sing-box version 1.14.3"\n');new.chmod(0o755)
            manifest={'core_version':'1.14.3','files':{'sing-box-minimal-amd64':{'sha256':hashlib.sha256(new.read_bytes()).hexdigest()}}}
            (root/'manifest').write_text(json.dumps(manifest))
            (root/'state/config.json').write_text('{"configuration":"preserve"}')
            (root/'state/relay.json').write_text('{}')
            script=r'''
set -eu
ROOT=$1
export ARGO_PANEL=https://fixture.example ARGO_CORE_CHANNEL=minimal
SINGBOX_DIR="$ROOT/state";SINGBOX_BIN="$ROOT/sing-box";CONFIG_FILE="$SINGBOX_DIR/config.json";RELAY_CONFIG_FILE="$SINGBOX_DIR/relay.json"
curl() { case "$*" in *manifest.json*) cp "$ROOT/manifest" "${@: -1}";; *) cp "$ROOT/candidate" "${@: -1}";;esac; }
uname() { echo x86_64; }
_check_combined_config_files() { [ ! -f "$ROOT/reject" ]; }
_manage_service() { return 0; }
source "$2"
_panel_install_core
_panel_commit_core
[ "$("$SINGBOX_BIN" version)" = 'sing-box version 1.14.3' ]
[ -f "$SINGBOX_DIR/core-rollback/sing-box" ]
touch "$ROOT/reject"
if _panel_rollback_core;then exit 2;fi
[ "$("$SINGBOX_BIN" version)" = 'sing-box version 1.14.3' ]
rm "$ROOT/reject"
_panel_rollback_core
[ "$("$SINGBOX_BIN")" = original ]
grep -q preserve "$CONFIG_FILE"
printf tampered > "$ROOT/candidate"
if _panel_install_core;then exit 3;fi
[ "$("$SINGBOX_BIN")" = original ]
'''
            result=subprocess.run(['bash','-c',script,'fixture',d,str(Path(__file__).resolve().parents[1]/'app/node-core.sh')],capture_output=True,text=True)
            self.assertEqual(result.returncode,0,result.stdout+result.stderr)
    def test_locked_release_selects_same_version_and_job_has_valid_shell(self):
        from app import monitor
        config={'node_port':18080,'core_channel':'minimal'}
        script=monitor.job_script({'node_id':1},{'operation':'core','config_json':json.dumps(config)})
        self.assertIn('_singbox_core_is_locked',script)
        self.assertIn('_do_update_singbox "$SINGBOX_FIXED_VERSION"',script)
        subprocess.run(['sh','-n'],input=script.encode(),check=True)
        inner=next(line[len("bash -c '"):-1] for line in script.splitlines() if line.startswith("bash -c '"))
        subprocess.run(['bash','-n'],input=inner.encode(),check=True)
        with tempfile.TemporaryDirectory() as d:
            root=Path(d);(root/'candidate').write_text('#!/bin/sh\necho "sing-box version 1.13.21"\n')
            digest=hashlib.sha256((root/'candidate').read_bytes()).hexdigest()
            (root/'manifest').write_text(json.dumps({'core_version':'1.14.3','files':{'sing-box-minimal-1.13.21-amd64':{'version':'1.13.21','sha256':digest}}}))
            shell=r'''
set -eu
ROOT=$1
export ARGO_PANEL=https://fixture.example ARGO_CORE_CHANNEL=minimal
SINGBOX_DIR="$ROOT/state";SINGBOX_BIN="$ROOT/sing-box";CONFIG_FILE="$SINGBOX_DIR/config.json";RELAY_CONFIG_FILE="$SINGBOX_DIR/relay.json"
curl() { case "$*" in *manifest.json*) cp "$ROOT/manifest" "${@: -1}";; *sing-box-minimal-1.13.21-amd64*) cp "$ROOT/candidate" "${@: -1}";; *) return 1;;esac; }
uname() { echo x86_64; }
source "$2"
_panel_install_core 1.13.21
_panel_commit_core
[ "$("$SINGBOX_BIN" version)" = 'sing-box version 1.13.21' ]
'''
            result=subprocess.run(['bash','-c',shell,'fixture',d,str(Path(__file__).resolve().parents[1]/'app/node-core.sh')],capture_output=True,text=True)
            self.assertEqual(result.returncode,0,result.stdout+result.stderr)
