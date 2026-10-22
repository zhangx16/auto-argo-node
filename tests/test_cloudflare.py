import os
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

from app import db, cloudflare, deploy, fixed_jobs


class CloudflareTests(unittest.TestCase):
    def test_hostname_and_link(self):
        with patch.object(cloudflare,'settings',return_value={'zone_name':'example.com'}):
            self.assertEqual(cloudflare.valid_hostname('KR01.example.com.'),'kr01.example.com')
            for host in ['example.com','other.net','a.b.example.com','-x.example.com','x.example.com;id']:
                with self.assertRaises(cloudflare.CFError):cloudflare.valid_hostname(host)
        body=deploy.NodeEditBody(remark='fixed',node_port=33001,client_port=443,client_address='')
        link=deploy.make_link({'domain':'kr.example.com','uuid':'341cad6d-9bf5-4b09-9c96-a7879ebc8fe1','path':'/ws'},body)
        self.assertIn('sni=kr.example.com',link)

    def test_credentials_encrypted_and_no_token_in_script(self):
        with tempfile.TemporaryDirectory() as temp, patch.object(db,'DATA_DIR',Path(temp)):
            Path(temp,'secret.key').write_text('isolated-test-secret')
            ciphertext=cloudflare.encrypt('runtime-secret')
            self.assertNotIn('runtime-secret',ciphertext)
            self.assertEqual(cloudflare.decrypt(ciphertext),'runtime-secret')
        import subprocess
        script=fixed_jobs.migrate(7,{'node_port':33001,'uuid':'341cad6d-9bf5-4b09-9c96-a7879ebc8fe1','ws_path':'/ws'})
        subprocess.run(['sh','-n'],input=script.encode(),check=True)
        subprocess.run(['bash','-n'],input=script.split("exec bash <<'FIXED_MIGRATION'\n",1)[1].rsplit('\nFIXED_MIGRATION',1)[0].encode(),check=True)
        self.assertNotIn('runtime-secret',script)
        self.assertIn('fixed_wait',script)

    def test_direct_fixed_install_script(self):
        import subprocess
        script=fixed_jobs.install(7,{'node_port':33001,'uuid':'341cad6d-9bf5-4b09-9c96-a7879ebc8fe1','ws_path':'/ws'},'Fixed QA')
        subprocess.run(['sh','-n'],input=script.encode(),check=True)
        subprocess.run(['bash','-n'],input=script.split("exec bash <<'FIXED_INSTALL'\n",1)[1].rsplit('\nFIXED_INSTALL',1)[0].encode(),check=True)
        self.assertNotIn('_start_argo_tunnel',script)
        self.assertIn('rollback_install',script)

    def test_cleanup_refuses_modified_record(self):
        b={'hostname':'x.example.com','dns_id':'owned','tunnel_id':'tunnel'}
        with patch.object(cloudflare,'binding',return_value=b),patch.object(cloudflare,'settings',return_value={'zone_id':'zone'}),patch.object(cloudflare,'api',return_value={'name':'other.example.com','type':'CNAME','content':'tunnel.cfargotunnel.com'}) as api:
            with self.assertRaises(cloudflare.CFError):cloudflare.cleanup(1)
            self.assertEqual(api.call_count,1)

    def test_watchdog_excludes_only_panel_fixed_nodes(self):
        text=deploy.installer_text()
        self.assertIn('select(.value.manager != "argo-deploy-fixed")',text)
        self.assertNotIn('select(.value.type != "fixed")',text)
