//! Private service observations. No shell, root credentials, or remote execution.
use serde_json::{json, Value};
use std::{fs, time::{SystemTime, UNIX_EPOCH}};
pub fn collect() -> Value {
    let now = SystemTime::now().duration_since(UNIX_EPOCH).unwrap_or_default().as_secs();
    let mut core_rss=0u64; let mut tunnel_rss=0u64;
    let mut core_count=0u64; let mut tunnel_count=0u64;
    if let Ok(entries)=fs::read_dir("/proc") {
        for item in entries.flatten() {
            if item.file_name().to_string_lossy().parse::<u32>().is_err() {continue;}
            let name=fs::read_to_string(item.path().join("comm")).unwrap_or_default();
            if !matches!(name.trim(), "sing-box" | "cloudflared") {continue;}
            let status=fs::read_to_string(item.path().join("status")).unwrap_or_default();
            let rss=status.lines().find(|l| l.starts_with("VmRSS:")).and_then(|l| l.split_whitespace().nth(1)).and_then(|v|v.parse::<u64>().ok()).unwrap_or(0)*1024;
            if name.trim()=="sing-box" {core_rss+=rss;core_count+=1;}else{tunnel_rss+=rss;tunnel_count+=1;}
        }
    }
    let tcp=format!("{}\n{}",fs::read_to_string("/proc/net/tcp").unwrap_or_default(),fs::read_to_string("/proc/net/tcp6").unwrap_or_default());
    let udp=format!("{}\n{}",fs::read_to_string("/proc/net/udp").unwrap_or_default(),fs::read_to_string("/proc/net/udp6").unwrap_or_default());
    let mut services=Vec::new();
    if let Ok(entries)=fs::read_dir("/var/lib/argo-monitor/services") {
        for item in entries.flatten().take(128) {
            let Ok(meta)=item.metadata() else {continue;};
            if !meta.is_file() || meta.len()>32768 {continue;}
            let Ok(bytes)=fs::read(item.path()) else {continue;};
            let Ok(mut value)=serde_json::from_slice::<Value>(&bytes) else {continue;};
            let Some(port)=value["port"].as_u64().filter(|p|*p>0&&*p<65536) else {continue;};
            let hy=value["protocol"]=="hysteria2";
            let table=if hy {&udp}else{&tcp};
            let listening=table.lines().any(|line|{
                let fields:Vec<_>=line.split_whitespace().collect();
                fields.len()>3 && (hy||fields[3]=="0A") && fields[1].rsplit(':').next().and_then(|p|u64::from_str_radix(p,16).ok())==Some(port)
            });
            value["process_alive"]=json!(core_count>0 && (value["protocol"]!="vless-ws-argo" || tunnel_count>0));
            value["local_listen"]=json!(listening);
            services.push(value);
        }
    }
    let probe_rss=fs::read_to_string("/proc/self/status").unwrap_or_default().lines().find(|l|l.starts_with("VmRSS:")).and_then(|l|l.split_whitespace().nth(1)).and_then(|s|s.parse::<u64>().ok()).unwrap_or(0)*1024;
    json!({"probe_rss":probe_rss,"version":1,"sampled_at":now,"singbox_rss":core_rss,"cloudflared_rss":tunnel_rss,"singbox_processes":core_count,"cloudflared_processes":tunnel_count,"services":services})
}
