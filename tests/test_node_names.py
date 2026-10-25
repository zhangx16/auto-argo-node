import unittest
from app import node_names


class NodeNamesTests(unittest.TestCase):
    def test_protocol_suffixes_are_canonical_and_not_repeated(self):
        for kind,suffix in node_names.SUFFIXES.items():
            self.assertEqual(node_names.format_name('KR Demo',kind),'KR Demo-'+suffix)
            self.assertEqual(node_names.format_name('KR Demo-'+suffix.upper(),kind),'KR Demo-'+suffix)
            self.assertEqual(node_names.format_name('KR Demo-'+suffix+'-'+suffix,kind),'KR Demo-'+suffix)
        self.assertEqual(node_names.format_name('名称'*80,'reality')[-6:],'-Vless')
        self.assertLessEqual(len(node_names.format_name('名称'*80,'reality')),80)

    def test_protocol_inference_only_labels_supported_managed_protocols(self):
        cases=[('vless://u@host:443?type=ws&security=tls','vless-ws-argo'),('vless://u@host:33003?type=tcp&security=reality','reality'),('hysteria2://p@host:443','hysteria2'),('hy2://p@host:443','hysteria2'),('ss://fixture','')]
        for link,kind in cases:self.assertEqual(node_names.protocol({'share_link':link}),kind)
