"""Build-time pinned DB-IP country database; runtime lookups stay local."""
import gzip,hashlib,subprocess
from pathlib import Path
import maxminddb
URL='https://download.db-ip.com/free/dbip-country-lite-2026-10.mmdb.gz'
SHA256='4cd44536ddfd40fec7eca94a233f7d19f38d470e6522e653cb8d27001f5fe177'
root=Path(__file__).resolve().parent.parent
data=subprocess.run(['curl','--proto','=https','--tlsv1.2','-fsSL','--retry','3','--connect-timeout','15','--max-time','90','--max-filesize',str(32*1024*1024),URL],capture_output=True,check=True).stdout
if len(data)>32*1024*1024 or hashlib.sha256(data).hexdigest()!=SHA256:raise RuntimeError('GeoIP download integrity check failed')
raw=gzip.decompress(data)
if len(raw)>64*1024*1024:raise RuntimeError('GeoIP database too large')
dest=root/'app/vendor/country.mmdb';dest.parent.mkdir(parents=True,exist_ok=True);dest.write_bytes(raw)
with maxminddb.open_database(str(dest)) as reader:
 if not reader.metadata().database_type:raise RuntimeError('GeoIP database invalid')
print('Verified DB-IP country database 2026-10 installed')
