"""Apply the small integration delta to pinned MIT Monitor source trees."""
from pathlib import Path
import sys
hub, agent = map(Path, sys.argv[1:3])
p=hub/'src/auth.rs';s=p.read_text();s=s.replace('pub fn authed(app: &App, headers: &HeaderMap) -> bool {','''pub fn service_authed(headers: &HeaderMap) -> bool {
    let Ok(expected) = std::env::var("ARGO_MONITOR_SERVICE_TOKEN") else { return false; };
    if expected.len() < 32 { return false; }
    let Some(actual) = headers.get("x-argo-service").and_then(|v| v.to_str().ok()) else { return false; };
    // Compare fixed length hashes without a data-dependent early exit.
    let a = Sha256::digest(expected.as_bytes());
    let b = Sha256::digest(actual.as_bytes());
    a.iter().zip(b.iter()).fold(0u8, |diff,(a,b)| diff | (a ^ b)) == 0
}
pub fn authed(app: &App, headers: &HeaderMap) -> bool {
    if service_authed(headers) { return true; }''');p.write_text(s)
p=hub/'src/api.rs';s=p.read_text().replace('if authed(app, &parts.headers) {','''if crate::auth::service_authed(&parts.headers) {
            let path = parts.uri.path();
            let tail = path.strip_prefix("/api/nodes/").unwrap_or("");
            let allowed = path == "/api/nodes" || {
                let mut pieces = tail.split('/');
                pieces.next().is_some_and(|id| id.parse::<u64>().is_ok()) &&
                matches!(pieces.next(), None | Some("token") | Some("metrics")) && pieces.next().is_none()
            };
            if !allowed { return Err(answer(StatusCode::FORBIDDEN, "service scope denied")); }
            Ok(Admin)
        } else if authed(app, &parts.headers) {''',1);p.write_text(s)
p=hub/'src/main.rs';s=p.read_text().replace('pub fn public_page(&self) -> bool {','pub fn public_page(&self) -> bool {\n        if std::env::var_os("ARGO_MONITOR_SERVICE_TOKEN").is_some() { return false; }');p.write_text(s)
p=agent/'src/main.rs';s=p.read_text().replace('mod collect;','mod collect;\nmod argo;');s=s.replace('let result = loop {\n        tokio::select!', 'let mut argo_at = Instant::now() - Duration::from_secs(60);\n    let result = loop {\n        tokio::select!',1);needle='if let Err(e) = send(&mut ws, notify("report", m), remaining(last_frame)).await { break Err(e); }';s=s.replace(needle,needle+'''
                if argo_at.elapsed() >= Duration::from_secs(30) {
                    argo_at = Instant::now();
                    if let Err(e) = send(&mut ws, notify("service.report", argo::collect()), remaining(last_frame)).await { break Err(e); }
                }''');p.write_text(s)
(agent/'src/argo.rs').write_text((Path(__file__).parent/'argo.rs').read_text())
