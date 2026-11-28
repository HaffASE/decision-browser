//! Project-scoped installation of the native executable.
use base64::{Engine, engine::general_purpose::STANDARD};
use serde_json::{Value, json};
use sha2::{Digest, Sha256};
use std::error::Error;
use std::fs::{self, File, OpenOptions};
use std::io::{BufRead, IsTerminal, Read, Write};
use std::path::{Component, Path, PathBuf};
use std::process::{Command, Stdio};
use std::thread;
use std::time::{Duration, Instant, SystemTime, UNIX_EPOCH};

type Result<T> = std::result::Result<T, Box<dyn Error + Send + Sync>>;
const STATE: &str = ".decision-browser-install";
const BEGIN: &str = "# BEGIN decision-browser managed MCP\n";
const END: &str = "# END decision-browser managed MCP\n";
const OWNER: &str = "decision-browser-native-v1";
const MAX_CONFIG: u64 = 4 * 1024 * 1024;
const MAX_INTENT: u64 = 64 * 1024 * 1024;
const SKILL: &str = include_str!("../assets/native-skill.md");
mod recovery;

pub fn dispatch(args: &[String]) -> Result<()> {
    let mut options = Options::parse(args)?;
    if args.first().is_some_and(|command| command == "setup") && !options.client_explicit {
        options.clients = choose_setup_client(
            std::io::stdin().is_terminal(),
            &mut std::io::stdin().lock(),
            &mut std::io::stderr(),
        )?;
    }
    if options.command == "configure" {
        return configure(&options);
    }
    let project = options.project.canonicalize()?;
    if !project.is_dir() {
        return Err("project must be an existing directory".into());
    }
    match options.command.as_str() {
        "doctor" | "install-status" => {
            let mut report = inspect(&project)?;
            if options.command == "doctor" {
                report["provider"] = crate::provider::production_provider_status();
                report["version"] = json!(env!("CARGO_PKG_VERSION"));
                report["platform"] = json!(std::env::consts::OS);
                report["architecture"] = json!(std::env::consts::ARCH);
                report["chrome_available"] = json!(crate::browser::chrome_executable().is_ok());
            }
            println!("{report}");
            Ok(())
        }
        "recover-install" => {
            let raw = if options.project.is_absolute() {
                options.project.clone()
            } else {
                std::env::current_dir()?.join(&options.project)
            };
            let barrier = if raw != project || recovery::relevant(&raw) {
                recovery::inspect(&raw, options.apply)
            } else {
                json!({"reason":"clear"})
            };
            if barrier["reason"] != "clear" {
                println!("{barrier}");
                return if barrier["status"] == "recovered" {
                    Ok(())
                } else {
                    Err("installation recovery remains blocked".into())
                };
            }
            if options.apply {
                let _lock = lock(&project)?;
                recover(&project)?;
            }
            println!("{}", inspect(&project)?);
            Ok(())
        }
        "setup" | "install" | "upgrade" | "uninstall" => {
            let _lock = lock(&project)?;
            install(
                &project,
                &std::env::current_exe()?,
                &options.clients,
                options.command == "uninstall",
            )?;
            println!("{}", inspect(&project)?);
            Ok(())
        }
        _ => Err("unsupported installation command".into()),
    }
}

struct Options {
    command: String,
    project: PathBuf,
    clients: Vec<String>,
    client_explicit: bool,
    provider: Option<String>,
    apply: bool,
}

impl Options {
    fn parse(args: &[String]) -> Result<Self> {
        let mut result = Self {
            command: args.first().ok_or("installation command required")?.clone(),
            project: std::env::current_dir()?,
            clients: vec!["codex".to_owned()],
            client_explicit: false,
            provider: None,
            apply: false,
        };
        let mut args = args.iter().skip(1);
        while let Some(arg) = args.next() {
            match arg.as_str() {
                "--project" => {
                    result.project = PathBuf::from(args.next().ok_or("--project needs a path")?)
                }
                "--client" => {
                    result.client_explicit = true;
                    result.clients = clients(args.next().ok_or("--client needs a client")?)?
                }
                "codex" | "claude" | "both" => {
                    result.client_explicit = true;
                    result.clients = clients(arg)?;
                }
                "--upgrade" => result.command = "upgrade".to_owned(),
                "--uninstall" => result.command = "uninstall".to_owned(),
                "--apply" => result.apply = true,
                "--json" => {}
                "jev" | "luna" | "gemini" | "openai" | "typesafe" | "openrouter" | "opencode"
                | "local" | "nimble"
                    if result.command == "configure" =>
                {
                    result.provider = Some(arg.clone())
                }
                _ => return Err(format!("unknown installation argument: {arg}").into()),
            }
        }
        Ok(result)
    }
}

fn choose_setup_client(
    terminal: bool,
    input: &mut impl BufRead,
    output: &mut impl Write,
) -> Result<Vec<String>> {
    if !terminal {
        return Err("setup requires a client in noninteractive mode; use setup codex, setup claude or setup both".into());
    }
    write!(
        output,
        "Choose the MCP client (codex, claude or both; empty to cancel): "
    )?;
    output.flush()?;
    let mut answer = String::new();
    if input.read_line(&mut answer)? == 0 || answer.trim().is_empty() {
        return Err("setup cancelled before installation".into());
    }
    clients(answer.trim())
}

fn clients(name: &str) -> Result<Vec<String>> {
    match name {
        "codex" | "claude" => Ok(vec![name.to_owned()]),
        "both" => Ok(vec!["codex".to_owned(), "claude".to_owned()]),
        _ => Err("client must be codex, claude or both".into()),
    }
}

fn checked(project: &Path, relative: &str) -> Result<PathBuf> {
    let relative = Path::new(relative);
    if relative
        .components()
        .any(|part| !matches!(part, Component::Normal(_)))
    {
        return Err("invalid managed path".into());
    }
    let mut path = project.to_path_buf();
    for part in relative.components() {
        path.push(part);
        match fs::symlink_metadata(&path) {
            Ok(metadata) if metadata.file_type().is_symlink() => {
                return Err("symlink in managed destination".into());
            }
            Ok(_) => {}
            Err(error) if error.kind() == std::io::ErrorKind::NotFound => {}
            Err(error) => return Err(error.into()),
        }
    }
    Ok(path)
}

fn read(path: &Path) -> Result<Option<Vec<u8>>> {
    read_bounded(path, MAX_CONFIG)
}

fn read_bounded(path: &Path, limit: u64) -> Result<Option<Vec<u8>>> {
    let mut options = OpenOptions::new();
    options.read(true);
    #[cfg(unix)]
    {
        use std::os::unix::fs::OpenOptionsExt;
        options.custom_flags(libc::O_NOFOLLOW | libc::O_NONBLOCK);
    }
    match options.open(path) {
        Ok(mut file) => {
            let before = file.metadata()?;
            if !before.is_file() || before.len() > limit {
                return Err("unsafe managed configuration".into());
            }
            #[cfg(unix)]
            {
                use std::os::unix::fs::MetadataExt;
                if before.nlink() != 1
                    || before.uid() != unsafe { libc::geteuid() }
                    || before.mode() & 0o022 != 0
                {
                    return Err("unsafe managed configuration".into());
                }
            }
            let mut bytes = Vec::new();
            (&mut file).take(limit + 1).read_to_end(&mut bytes)?;
            if bytes.len() as u64 > limit {
                return Err("managed configuration exceeds its size limit".into());
            }
            #[cfg(unix)]
            {
                use std::os::unix::fs::MetadataExt;
                let identity = |info: &fs::Metadata| {
                    (
                        info.dev(),
                        info.ino(),
                        info.mode(),
                        info.nlink(),
                        info.uid(),
                        info.len(),
                        info.mtime(),
                        info.mtime_nsec(),
                        info.ctime(),
                        info.ctime_nsec(),
                    )
                };
                if identity(&before) != identity(&file.metadata()?)
                    || identity(&before) != identity(&fs::symlink_metadata(path)?)
                {
                    return Err("managed configuration changed during read".into());
                }
            }
            Ok(Some(bytes))
        }
        Err(error) if error.kind() == std::io::ErrorKind::NotFound => Ok(None),
        Err(error) => Err(error.into()),
    }
}

fn sync_directory(path: &Path) -> Result<()> {
    #[cfg(unix)]
    File::open(path)?.sync_all()?;
    #[cfg(not(unix))]
    let _ = path;
    Ok(())
}

fn private_dir(path: &Path) -> Result<()> {
    if path.is_dir() {
        return Ok(());
    }
    fs::create_dir_all(path)?;
    #[cfg(unix)]
    {
        use std::os::unix::fs::PermissionsExt;
        fs::set_permissions(path, fs::Permissions::from_mode(0o700))?;
    }
    Ok(())
}

fn write_atomic(path: &Path, content: Option<&[u8]>) -> Result<()> {
    let parent = path.parent().ok_or("missing parent directory")?;
    private_dir(parent)?;
    if let Some(content) = content {
        let temp = parent.join(format!(".decision-browser-write-{}", nonce()?));
        let mut options = OpenOptions::new();
        options.write(true).create_new(true);
        #[cfg(unix)]
        {
            use std::os::unix::fs::OpenOptionsExt;
            options.mode(0o600);
        }
        let mut file = options.open(&temp)?;
        file.write_all(content)?;
        file.sync_all()?;
        fs::rename(&temp, path)?;
    } else if path.exists() {
        fs::remove_file(path)?;
    }
    sync_directory(parent)
}

fn checksum(bytes: &[u8]) -> String {
    Sha256::digest(bytes)
        .iter()
        .map(|byte| format!("{byte:02x}"))
        .collect()
}
fn nonce() -> Result<String> {
    static NEXT: std::sync::atomic::AtomicU64 = std::sync::atomic::AtomicU64::new(0);
    Ok(format!(
        "{}-{}-{}",
        std::process::id(),
        SystemTime::now().duration_since(UNIX_EPOCH)?.as_nanos(),
        NEXT.fetch_add(1, std::sync::atomic::Ordering::Relaxed)
    ))
}

struct Lease(Vec<File>);
impl Drop for Lease {
    fn drop(&mut self) {
        for file in &self.0 {
            let _ = file.unlock();
        }
    }
}

fn open_lease(path: &Path, create: bool) -> Result<File> {
    let mut options = OpenOptions::new();
    options
        .read(true)
        .write(true)
        .create(create)
        .truncate(false);
    #[cfg(unix)]
    {
        use std::os::unix::fs::OpenOptionsExt;
        options
            .mode(0o600)
            .custom_flags(libc::O_NOFOLLOW | libc::O_NONBLOCK);
    }
    let file = options.open(path)?;
    let info = file.metadata()?;
    if !info.is_file() || info.len() > 128 {
        return Err("unsafe installation lease".into());
    }
    #[cfg(unix)]
    {
        use std::os::unix::fs::MetadataExt;
        if info.nlink() != 1 || info.uid() != unsafe { libc::geteuid() } || info.mode() & 0o077 != 0
        {
            return Err("unsafe installation lease".into());
        }
    }
    Ok(file)
}

fn lock(project: &Path) -> Result<Lease> {
    let path = checked(project, ".decision-browser-install.native.lease")?;
    let file = open_lease(&path, true)?;
    file.try_lock()
        .map_err(|_| "another native installer is active")?;
    let mut files = vec![file];
    for relative in [
        ".decision-browser-install.lease",
        ".decision-browser-stage.lease",
    ] {
        let old = checked(project, relative)?;
        let create = relative == ".decision-browser-install.lease";
        if create || old.exists() {
            let mut file = open_lease(&old, create)?;
            file.try_lock()
                .map_err(|_| "a legacy installer is active")?;
            let mut body = Vec::new();
            (&mut file).take(129).read_to_end(&mut body)?;
            if body != b"{\"owner\":\"decision-browser-lease-v1\"}\n" && !body.is_empty() {
                return Err(
                    "legacy interrupted stage needs recovery before native installation".into(),
                );
            }
            if create && body.is_empty() {
                file.write_all(b"{\"owner\":\"decision-browser-lease-v1\"}\n")?;
                file.sync_all()?;
                sync_directory(project)?;
            }
            files.push(file);
        }
    }
    Ok(Lease(files))
}

fn manifest(project: &Path) -> Result<Value> {
    let path = checked(project, &format!("{STATE}/native-manifest.json"))?;
    match read(&path)? {
        None => legacy_manifest(project),
        Some(bytes) => {
            let value: Value = serde_json::from_slice(&bytes)?;
            if value["owner"] != OWNER || !value["clients"].is_object() {
                return Err("invalid native installation manifest".into());
            }
            Ok(value)
        }
    }
}

fn legacy_manifest(project: &Path) -> Result<Value> {
    let mut migrated = json!({"owner":OWNER,"clients":{}});
    let Some(raw) = read(&checked(project, &format!("{STATE}/manifest.json"))?)? else {
        return Ok(migrated);
    };
    let owner = read(&checked(project, &format!("{STATE}/owner.json"))?)?;
    if owner.as_deref() != Some(b"{\"owner\":\"decision-browser-installer-v1\"}\n") {
        return Err("legacy installation owner cannot be established".into());
    }
    let legacy: Value = serde_json::from_slice(&raw)?;
    if legacy["schema"] != 1 || legacy["project"].as_str() != project.to_str() {
        return Err("legacy installation manifest belongs to a different project".into());
    }
    for (client, record) in legacy["clients"]
        .as_object()
        .ok_or("invalid legacy clients")?
    {
        if !matches!(client.as_str(), "codex" | "claude") {
            return Err("unknown legacy client".into());
        }
        let config = record
            .get("config")
            .filter(|v| v.is_object())
            .ok_or("invalid legacy configuration")?;
        let skill = record["files"]
            .get(skill_path(client))
            .and_then(Value::as_str)
            .ok_or("legacy skill ownership cannot be established")?;
        migrated["clients"][client] = json!({"config":config,"skill_sha256":skill});
    }
    Ok(migrated)
}

fn registration(
    client: &str,
    binary: &Path,
    project: &Path,
    environment: &std::collections::BTreeMap<String, String>,
) -> Value {
    let mut config = json!({"command":binary,"args":["serve","--admission","standard_form"]});
    if client == "codex" {
        config["cwd"] = json!(project);
        config["startup_timeout_sec"] = json!(30);
        config["tool_timeout_sec"] = json!(180);
        config["env_vars"] = json!([
            "DECISION_BROWSER_PROVIDER",
            "DECISION_BROWSER_CHROME",
            "DECISION_BROWSER_CONFIG_FILE",
            "DECISION_BROWSER_DEVTOOLS_ACTIVE_PORT",
            "OPENAI_API_KEY",
            "TYPESAFE_API_KEY",
            "OPENROUTER_API_KEY",
            "OPENCODE_API_KEY"
        ]);
    } else {
        config["type"] = json!("stdio");
    }
    for name in [
        "DECISION_BROWSER_CONFIG_FILE",
        "DECISION_BROWSER_CHROME",
        "DECISION_BROWSER_DEVTOOLS_ACTIVE_PORT",
    ] {
        if let Some(value) = environment.get(name) {
            if !Path::new(value).is_absolute() || value.len() > 4096 {
                continue;
            }
            if config.get("env").is_none() {
                config["env"] = json!({});
            }
            config["env"][name] = json!(value);
        }
    }
    config
}

fn change_config(
    client: &str,
    before: Option<&[u8]>,
    old: Option<&Value>,
    desired: Option<&Value>,
) -> Result<Option<Vec<u8>>> {
    let text = std::str::from_utf8(before.unwrap_or_default())?;
    if client == "codex" {
        let parsed: toml::Value = toml::from_str(text)?;
        let current = parsed
            .get("mcp_servers")
            .and_then(|table| table.get("decision_browser"));
        let current = current.map(serde_json::to_value).transpose()?;
        if current.as_ref() != old && current.is_some() {
            return Err("conflicting or edited decision_browser registration".into());
        }
        let mut stripped = text.to_owned();
        let count = text.matches(BEGIN).count();
        if count > 1 || text.matches(END).count() != count {
            return Err("ambiguous managed MCP markers".into());
        }
        if count == 1 {
            let start = text.find(BEGIN).unwrap();
            let end = text.find(END).unwrap() + END.len();
            if end < start {
                return Err("invalid managed MCP markers".into());
            }
            let block: toml::Value = toml::from_str(&text[start..end])?;
            let expected = json!({"mcp_servers":{"decision_browser":current}});
            if serde_json::to_value(block)? != expected {
                return Err("managed block contains unrelated settings".into());
            }
            stripped.replace_range(start..end, "");
        } else if current.is_some() {
            return Err("registration has no managed ownership markers".into());
        }
        if let Some(config) = desired {
            if !stripped.is_empty() && !stripped.ends_with('\n') {
                stripped.push('\n');
            }
            stripped.push_str(BEGIN);
            stripped.push_str("[mcp_servers.decision_browser]\n");
            for (key, value) in config.as_object().ok_or("invalid registration")? {
                if key != "env" {
                    stripped.push_str(&format!("{key} = {value}\n"));
                }
            }
            if let Some(env) = config.get("env").and_then(Value::as_object) {
                stripped.push_str("[mcp_servers.decision_browser.env]\n");
                for (key, value) in env {
                    stripped.push_str(&format!("{} = {value}\n", serde_json::to_string(key)?));
                }
            }
            stripped.push_str(END);
        }
        let _: toml::Value = toml::from_str(&stripped)?;
        return Ok(if stripped.is_empty() && before.is_none() {
            None
        } else {
            Some(stripped.into_bytes())
        });
    }
    let mut parsed: Value = if text.trim().is_empty() {
        json!({})
    } else {
        serde_json::from_str(text)?
    };
    let root = parsed
        .as_object_mut()
        .ok_or("Claude configuration must be an object")?;
    let servers = root
        .entry("mcpServers")
        .or_insert_with(|| json!({}))
        .as_object_mut()
        .ok_or("invalid mcpServers table")?;
    if servers.get("decision_browser") != old && servers.contains_key("decision_browser") {
        return Err("conflicting or edited decision_browser registration".into());
    }
    if let Some(desired) = desired {
        servers.insert("decision_browser".to_owned(), desired.clone());
    } else {
        servers.remove("decision_browser");
    }
    Ok(Some(serde_json::to_vec_pretty(&parsed)?))
}

fn configuration_path(client: &str) -> &'static str {
    if client == "codex" {
        ".codex/config.toml"
    } else {
        ".mcp.json"
    }
}
fn skill_path(client: &str) -> &'static str {
    if client == "codex" {
        ".agents/skills/decision-browser/SKILL.md"
    } else {
        ".claude/skills/decision-browser/SKILL.md"
    }
}

fn transaction(project: &Path, changes: Vec<Value>) -> Result<()> {
    let intent = checked(project, &format!("{STATE}/native-intent.json"))?;
    if intent.exists() {
        return Err(
            "incomplete native installation; inspect install-status and recover-install".into(),
        );
    }
    let body = json!({"owner":OWNER,"changes":changes});
    let bytes = serde_json::to_vec(&body)?;
    if bytes.len() as u64 > MAX_INTENT {
        return Err("native installation journal exceeds its size limit".into());
    }
    for item in body["changes"]
        .as_array()
        .ok_or("invalid installation intent")?
    {
        for field in ["before", "after"] {
            if decode(&item[field])?
                .as_ref()
                .is_some_and(|bytes| bytes.len() as u64 > MAX_CONFIG)
            {
                return Err("managed configuration exceeds its size limit".into());
            }
        }
    }
    write_atomic(&intent, Some(&bytes))?;
    for change in body["changes"]
        .as_array()
        .ok_or("invalid installation intent")?
    {
        let path = checked(
            project,
            change["path"].as_str().ok_or("invalid intent path")?,
        )?;
        let before = decode(&change["before"])?;
        if read(&path)? != before {
            return Err("configuration changed during installation; recovery retained".into());
        }
        let after = decode(&change["after"])?;
        write_atomic(&path, after.as_deref())?;
    }
    write_atomic(&intent, None)
}

fn decode(value: &Value) -> Result<Option<Vec<u8>>> {
    if value.is_null() {
        return Ok(None);
    }
    let encoded = value.as_str().ok_or("invalid installation intent bytes")?;
    if encoded.len() as u64 > MAX_CONFIG.div_ceil(3) * 4 {
        return Err("managed configuration exceeds its size limit".into());
    }
    let bytes = STANDARD.decode(encoded)?;
    if bytes.len() as u64 > MAX_CONFIG {
        return Err("managed configuration exceeds its size limit".into());
    }
    Ok(Some(bytes))
}
fn change(path: &str, before: Option<Vec<u8>>, after: Option<Vec<u8>>) -> Value {
    json!({"path":path,"before":before.map(|b|STANDARD.encode(b)),"after":after.map(|b|STANDARD.encode(b))})
}

fn recover(project: &Path) -> Result<()> {
    let path = checked(project, &format!("{STATE}/native-intent.json"))?;
    let Some(bytes) = read_bounded(&path, MAX_INTENT)? else {
        return Ok(());
    };
    let body: Value = serde_json::from_slice(&bytes)?;
    if body["owner"] != OWNER {
        return Err("unsupported installation recovery record".into());
    }
    let changes = body["changes"]
        .as_array()
        .filter(|c| c.len() <= 8)
        .ok_or("invalid recovery record")?;
    let mut targets = Vec::new();
    let mut seen = std::collections::BTreeSet::new();
    for item in changes.iter().rev() {
        let relative = item["path"].as_str().ok_or("invalid recovery path")?;
        if !seen.insert(relative) || item.as_object().is_none_or(|object| object.len() != 3) {
            return Err("invalid recovery record".into());
        }
        if !matches!(
            relative,
            ".codex/config.toml"
                | ".mcp.json"
                | ".agents/skills/decision-browser/SKILL.md"
                | ".claude/skills/decision-browser/SKILL.md"
                | ".decision-browser-install/native-manifest.json"
        ) {
            return Err("unexpected recovery target".into());
        }
        let target = checked(project, relative)?;
        let before = decode(&item["before"])?;
        let after = decode(&item["after"])?;
        let current = read(&target)?;
        if current != before && current != after {
            return Err("user-edited file prevents automatic recovery".into());
        }
        targets.push((relative, current, before));
    }
    for (relative, expected, before) in targets {
        let target = checked(project, relative)?;
        if read(&target)? != expected || read_bounded(&path, MAX_INTENT)?.as_ref() != Some(&bytes) {
            return Err("configuration changed during recovery; journal retained".into());
        }
        write_atomic(&target, before.as_deref())?;
    }
    let archive = checked(
        project,
        &format!("{STATE}/native-recovered-{}.json", nonce()?),
    )?;
    fs::rename(&path, archive)?;
    sync_directory(path.parent().unwrap())
}

fn install(project: &Path, source: &Path, clients: &[String], uninstall: bool) -> Result<()> {
    if checked(project, &format!("{STATE}/native-intent.json"))?.exists() {
        return Err("incomplete installation requires recover-install".into());
    }
    for residual in [
        ".decision-browser-stage-cleanup-unknown.json",
        ".decision-browser-install.stage-unknown",
    ] {
        if checked(project, residual)?.exists() {
            return Err(
                "legacy installation state requires legacy recovery before native installation"
                    .into(),
            );
        }
    }
    let mut manifest = manifest(project)?;
    let old = manifest.clone();
    let runtime = (!uninstall)
        .then(|| checked(project, &format!("{STATE}/native-runtimes/{}", nonce()?)))
        .transpose()?;
    let binary = runtime
        .as_ref()
        .map(|runtime| {
            runtime.join(if cfg!(windows) {
                "decision-browser.exe"
            } else {
                "decision-browser"
            })
        })
        .unwrap_or_default();
    let mut changes = Vec::new();
    let environment = std::env::vars()
        .filter(|(key, _)| {
            matches!(
                key.as_str(),
                "DECISION_BROWSER_CONFIG_FILE"
                    | "DECISION_BROWSER_CHROME"
                    | "DECISION_BROWSER_DEVTOOLS_ACTIVE_PORT"
            )
        })
        .collect();
    for client in clients {
        let config_path = configuration_path(client);
        let before = read(&checked(project, config_path)?)?;
        let previous = old["clients"].get(client).and_then(|c| c.get("config"));
        let mut desired =
            (!uninstall).then(|| registration(client, &binary, project, &environment));
        if let (Some(desired), Some(env)) = (
            desired.as_mut(),
            previous
                .and_then(|old| old.get("env"))
                .and_then(Value::as_object),
        ) {
            for (key, value) in env {
                if !key.ends_with("API_KEY") && value.is_string() {
                    if desired.get("env").is_none() {
                        desired["env"] = json!({});
                    }
                    desired["env"]
                        .as_object_mut()
                        .unwrap()
                        .entry(key.clone())
                        .or_insert_with(|| value.clone());
                }
            }
        }
        let after = change_config(client, before.as_deref(), previous, desired.as_ref())?;
        changes.push(change(config_path, before, after));
        let relative = skill_path(client);
        let current = read(&checked(project, relative)?)?;
        let expected = old["clients"][client]["skill_sha256"].as_str();
        if current
            .as_ref()
            .is_some_and(|bytes| Some(checksum(bytes).as_str()) != expected)
        {
            return Err("conflicting or edited managed skill".into());
        }
        changes.push(change(
            relative,
            current,
            (!uninstall).then(|| SKILL.as_bytes().to_vec()),
        ));
        if let Some(desired) = desired {
            manifest["clients"][client] =
                json!({"config":desired,"skill_sha256":checksum(SKILL.as_bytes())});
        } else {
            manifest["clients"].as_object_mut().unwrap().remove(client);
        }
    }
    let relative = format!("{STATE}/native-manifest.json");
    changes.push(change(
        &relative,
        read(&checked(project, &relative)?)?,
        Some(serde_json::to_vec(&manifest)?),
    ));
    // All registrations, skill hashes and paths have passed preflight before an
    // executable candidate is created. A candidate is retained once its intent
    // could have activated a client, so recovery can preserve running callers.
    let mut staged = runtime
        .map(|runtime| StagedRuntime::prepare(runtime, source))
        .transpose()?;
    let result = transaction(project, changes);
    let intent = checked(project, &format!("{STATE}/native-intent.json"))?;
    if (result.is_ok() || intent.exists())
        && let Some(staged) = staged.as_mut()
    {
        staged.retained = true;
    }
    result
}

struct StagedRuntime {
    directory: PathBuf,
    identity: fs::Metadata,
    binary: PathBuf,
    hash: String,
    retained: bool,
}

impl StagedRuntime {
    fn prepare(directory: PathBuf, source: &Path) -> Result<Self> {
        // Open the source before creating the directory so missing sources
        // cannot leave an empty runtime behind.
        let mut input = File::open(source)?;
        private_dir(directory.parent().ok_or("runtime parent missing")?)?;
        fs::create_dir(&directory)?;
        #[cfg(unix)]
        {
            use std::os::unix::fs::PermissionsExt;
            fs::set_permissions(&directory, fs::Permissions::from_mode(0o700))?;
        }
        let binary = directory.join(if cfg!(windows) {
            "decision-browser.exe"
        } else {
            "decision-browser"
        });
        let identity = fs::symlink_metadata(&directory)?;
        let mut staged = Self {
            directory,
            identity,
            binary,
            hash: String::new(),
            retained: false,
        };
        let prepared = (|| -> Result<()> {
            let mut options = OpenOptions::new();
            options.write(true).create_new(true);
            #[cfg(unix)]
            {
                use std::os::unix::fs::OpenOptionsExt;
                options.mode(0o700);
            }
            let mut output = options.open(&staged.binary)?;
            std::io::copy(&mut input, &mut output)?;
            output.sync_all()?;
            sync_directory(&staged.directory)?;
            Ok(())
        })();
        // Remember the exact candidate bytes even when the copy or fsync fails.
        // A modified binary or additional user file prevents automatic removal.
        if let Ok(hash) = file_checksum(&staged.binary) {
            staged.hash = hash;
        }
        prepared?;
        probe(&staged.binary)?;
        Ok(staged)
    }
}

impl Drop for StagedRuntime {
    fn drop(&mut self) {
        if self.retained {
            return;
        }
        let Ok(identity) = fs::symlink_metadata(&self.directory) else {
            return;
        };
        if !identity.is_dir() || identity.file_type().is_symlink() {
            return;
        }
        #[cfg(unix)]
        {
            use std::os::unix::fs::MetadataExt;
            if identity.dev() != self.identity.dev() || identity.ino() != self.identity.ino() {
                return;
            }
        }
        #[cfg(not(unix))]
        if identity.created().ok() != self.identity.created().ok() {
            return;
        }
        let Ok(entries) = fs::read_dir(&self.directory) else {
            return;
        };
        let entries = entries.collect::<std::io::Result<Vec<_>>>();
        let Ok(entries) = entries else {
            return;
        };
        if entries.iter().any(|entry| entry.path() != self.binary) {
            return;
        }
        if self.binary.exists() {
            if self.hash.is_empty()
                || file_checksum(&self.binary).ok().as_deref() != Some(&self.hash)
            {
                return;
            }
            if fs::remove_file(&self.binary).is_err() {
                return;
            }
        }
        if fs::remove_dir(&self.directory).is_ok()
            && let Some(parent) = self.directory.parent()
        {
            let _ = sync_directory(parent);
        }
    }
}

fn file_checksum(path: &Path) -> Result<String> {
    let metadata = fs::symlink_metadata(path)?;
    if !metadata.is_file() || metadata.file_type().is_symlink() {
        return Err("runtime candidate is not a regular file".into());
    }
    let mut file = File::open(path)?;
    let mut digest = Sha256::new();
    let mut bytes = [0u8; 65536];
    loop {
        let length = file.read(&mut bytes)?;
        if length == 0 {
            break;
        }
        digest.update(&bytes[..length]);
    }
    Ok(digest
        .finalize()
        .iter()
        .map(|byte| format!("{byte:02x}"))
        .collect())
}

fn probe(binary: &Path) -> Result<()> {
    let mut child = Command::new(binary)
        .arg("--version")
        .stdin(Stdio::null())
        .stdout(Stdio::null())
        .stderr(Stdio::null())
        .spawn()?;
    let deadline = Instant::now() + Duration::from_secs(10);
    loop {
        if let Some(status) = child.try_wait()? {
            return if status.success() {
                Ok(())
            } else {
                Err("native executable probe failed".into())
            };
        }
        if Instant::now() >= deadline {
            let _ = child.kill();
            let _ = child.wait();
            return Err("native executable probe timed out".into());
        }
        thread::sleep(Duration::from_millis(20));
    }
}

fn inspect(project: &Path) -> Result<Value> {
    if recovery::relevant(project) {
        let barrier = recovery::inspect(project, false);
        if barrier["status"] == "blocked" {
            let mut report = barrier;
            report["runtime"] = json!("native");
            report["readiness_scope"] = json!("on_disk_prerequisites");
            return Ok(report);
        }
    }
    let intent = checked(project, &format!("{STATE}/native-intent.json"))?.exists();
    let mut legacy_blocked =
        checked(project, ".decision-browser-stage-cleanup-unknown.json")?.exists();
    for relative in [
        ".decision-browser-install.lease",
        ".decision-browser-stage.lease",
    ] {
        if let Some(bytes) = read(&checked(project, relative)?)? {
            legacy_blocked |=
                !bytes.is_empty() && bytes != b"{\"owner\":\"decision-browser-lease-v1\"}\n";
        }
    }
    let intent = intent || legacy_blocked;
    let manifest = manifest(project)?;
    let mut clients = json!({});
    for client in ["codex", "claude"] {
        let record = &manifest["clients"][client];
        let registration = if let Some(config) = record.get("config") {
            let before = read(&checked(project, configuration_path(client))?)?;
            before
                .as_deref()
                .and_then(|bytes| std::str::from_utf8(bytes).ok())
                .is_some_and(|text| {
                    let actual = if client == "codex" {
                        toml::from_str::<toml::Value>(text)
                            .ok()
                            .and_then(|v| {
                                v.get("mcp_servers")
                                    .and_then(|t| t.get("decision_browser"))
                                    .cloned()
                            })
                            .and_then(|v| serde_json::to_value(v).ok())
                    } else {
                        serde_json::from_str::<Value>(text)
                            .ok()
                            .and_then(|v| v["mcpServers"].get("decision_browser").cloned())
                    };
                    actual.as_ref() == Some(config)
                })
        } else {
            false
        };
        let executable = record["config"]["command"]
            .as_str()
            .is_some_and(|p| Path::new(p).is_file());
        clients[client] =
            json!({"registration_matches":registration,"executable_present":executable});
    }
    Ok(
        json!({"status":if intent{"blocked"}else{"clear"},"reason":if legacy_blocked{Some("legacy_recovery_required")}else if intent{Some("incomplete_native_transaction")}else{None},"recovery":if legacy_blocked{Some("legacy_boot_witness_recovery_unavailable_in_native_cli")}else if intent{Some("recover-install --apply")}else{None},"clients":clients,"readiness_scope":"on_disk_prerequisites","runtime":"native"}),
    )
}

fn configure(options: &Options) -> Result<()> {
    let provider = options
        .provider
        .as_deref()
        .ok_or("configure requires openai, typesafe, gemini, openrouter, opencode or local")?;
    let path = if let Some(path) = std::env::var_os("DECISION_BROWSER_CONFIG_FILE") {
        PathBuf::from(path)
    } else {
        PathBuf::from(std::env::var_os("HOME").ok_or("HOME is unavailable")?)
            .join(".config/decision-browser/provider.json")
    };
    if !path.is_absolute() {
        return Err("provider profile path must be absolute".into());
    }
    let mut ancestor = Some(path.as_path());
    while let Some(current) = ancestor {
        if fs::symlink_metadata(current).is_ok_and(|m| m.file_type().is_symlink()) {
            return Err("symlink in provider profile path".into());
        }
        ancestor = current.parent();
    }
    #[cfg(unix)]
    if path.exists() {
        use std::os::unix::fs::MetadataExt;
        let metadata = fs::metadata(&path)?;
        if metadata.mode() & 0o077 != 0 || metadata.uid() != unsafe { libc::geteuid() } {
            return Err("provider profile must be private and owned by the current user".into());
        }
    }
    let canonical = match provider {
        "jev" => "typesafe",
        "luna" => "openai",
        "nimble" => "local",
        name => name,
    };
    if canonical == "fixture" {
        return Err("fixture configuration is limited to explicit development runs".into());
    }
    let mut profile: Value = match read(&path)? {
        Some(bytes) => serde_json::from_slice(&bytes)?,
        None => json!({"version":1}),
    };
    let object = profile.as_object_mut().ok_or("invalid provider profile")?;
    let previous = object.get("provider").and_then(Value::as_str).unwrap_or("");
    let previous = match previous {
        "jev" => "typesafe",
        "luna" => "openai",
        name => name,
    };
    if previous != canonical {
        for key in ["api_key", "endpoint", "model", "max_choices"] {
            object.remove(key);
        }
    }
    object.insert("version".to_owned(), json!(1));
    object.insert("provider".to_owned(), json!(canonical));
    write_atomic(&path, Some(&serde_json::to_vec(&profile)?))?;
    println!(
        "{}",
        json!({"status":"configured","provider":provider,"profile":path,"credential_source":"macos_keychain"})
    );
    Ok(())
}

#[cfg(test)]
mod tests {
    use super::*;
    struct Project(PathBuf);
    impl Project {
        fn new() -> Self {
            let path = std::env::temp_dir()
                .canonicalize()
                .unwrap()
                .join(format!("native-install-test-{}", nonce().unwrap()));
            fs::create_dir(&path).unwrap();
            Self(path)
        }
    }
    impl Drop for Project {
        fn drop(&mut self) {
            let _ = fs::remove_dir_all(&self.0);
        }
    }

    #[cfg(unix)]
    #[test]
    fn both_clients_install_repeat_uninstall_preserves_user_configuration() -> Result<()> {
        let project = Project::new();
        write_atomic(
            &project.0.join(".codex/config.toml"),
            Some(b"model = \"example\"\n"),
        )?;
        write_atomic(
            &project.0.join(".mcp.json"),
            Some(br#"{"mcpServers":{"other":{"command":"other"}},"user":true}"#),
        )?;
        let names = clients("both")?;
        let source = Path::new("/usr/bin/true");
        install(&project.0, source, &names, false)?;
        let first = manifest(&project.0)?;
        install(&project.0, source, &names, false)?;
        let second = manifest(&project.0)?;
        assert_ne!(
            first["clients"]["codex"]["config"]["command"],
            second["clients"]["codex"]["config"]["command"]
        );
        let state = inspect(&project.0)?;
        assert_eq!(state["clients"]["codex"]["registration_matches"], true);
        install(&project.0, source, &names, true)?;
        assert_eq!(
            fs::read_to_string(project.0.join(".codex/config.toml"))?,
            "model = \"example\"\n"
        );
        let claude: Value = serde_json::from_slice(&fs::read(project.0.join(".mcp.json"))?)?;
        assert_eq!(claude["user"], true);
        assert_eq!(claude["mcpServers"]["other"]["command"], "other");
        assert!(claude["mcpServers"].get("decision_browser").is_none());
        Ok(())
    }

    #[cfg(unix)]
    #[test]
    fn edited_config_and_skill_are_preserved() -> Result<()> {
        let project = Project::new();
        let source = Path::new("/usr/bin/true");
        let names = clients("codex")?;
        install(&project.0, source, &names, false)?;
        let target = project.0.join(".codex/config.toml");
        let edited = fs::read_to_string(&target)?
            .replace("tool_timeout_sec = 180", "tool_timeout_sec = 181");
        fs::write(&target, &edited)?;
        assert!(install(&project.0, source, &names, true).is_err());
        assert_eq!(fs::read_to_string(target)?, edited);
        Ok(())
    }

    #[test]
    fn recovery_rolls_back_only_exact_managed_changes() -> Result<()> {
        let project = Project::new();
        let path = project.0.join(".mcp.json");
        fs::write(&path, b"after")?;
        let body = json!({"owner":OWNER,"changes":[change(".mcp.json",Some(b"before".to_vec()),Some(b"after".to_vec()))]});
        let intent = project.0.join(STATE).join("native-intent.json");
        write_atomic(&intent, Some(&serde_json::to_vec(&body)?))?;
        fs::write(&path, b"user edit")?;
        assert!(recover(&project.0).is_err());
        assert_eq!(fs::read(&path)?, b"user edit");
        fs::write(&path, b"after")?;
        recover(&project.0)?;
        assert_eq!(fs::read(&path)?, b"before");
        assert!(!intent.exists());
        Ok(())
    }

    #[cfg(unix)]
    #[test]
    fn symlink_destinations_and_concurrent_installs_are_refused() -> Result<()> {
        let project = Project::new();
        let outside = Project::new();
        std::os::unix::fs::symlink(&outside.0, project.0.join(".codex"))?;
        assert!(checked(&project.0, ".codex/config.toml").is_err());
        let _lease = lock(&project.0)?;
        assert!(lock(&project.0).is_err());
        Ok(())
    }
    #[cfg(unix)]
    #[test]
    fn legacy_installation_migrates_owned_registrations() -> Result<()> {
        let project = Project::new();
        let legacy_config = json!({"command":"/legacy/bin/python","args":["-I","-m","decision_browser.cli","serve"],"env":{}});
        let text = format!(
            "{BEGIN}[mcp_servers.decision_browser]\ncommand = \"/legacy/bin/python\"\nargs = [\"-I\",\"-m\",\"decision_browser.cli\",\"serve\"]\n[mcp_servers.decision_browser.env]\n{END}"
        );
        write_atomic(&project.0.join(".codex/config.toml"), Some(text.as_bytes()))?;
        write_atomic(&project.0.join(skill_path("codex")), Some(b"old skill"))?;
        let legacy = json!({"schema":1,"project":project.0,"source_id":"0123456789abcdef0123","clients":{"codex":{"config":legacy_config,"files":{skill_path("codex"):checksum(b"old skill")}}}});
        write_atomic(
            &project.0.join(STATE).join("owner.json"),
            Some(b"{\"owner\":\"decision-browser-installer-v1\"}\n"),
        )?;
        write_atomic(
            &project.0.join(STATE).join("manifest.json"),
            Some(&serde_json::to_vec(&legacy)?),
        )?;
        {
            let _lease = lock(&project.0)?;
            install(
                &project.0,
                Path::new("/usr/bin/true"),
                &clients("codex")?,
                false,
            )?;
        }
        let native = manifest(&project.0)?;
        assert_ne!(
            native["clients"]["codex"]["config"]["command"],
            legacy_config["command"]
        );
        assert_eq!(
            inspect(&project.0)?["clients"]["codex"]["registration_matches"],
            true
        );
        assert!(project.0.join(STATE).join("manifest.json").is_file());
        Ok(())
    }

    #[test]
    fn registration_preserves_explicit_browser_selection_for_both_clients() {
        let selected = std::collections::BTreeMap::from([
            (
                "DECISION_BROWSER_DEVTOOLS_ACTIVE_PORT".to_owned(),
                "/selected/profile/DevToolsActivePort".to_owned(),
            ),
            (
                "DECISION_BROWSER_CHROME".to_owned(),
                "/selected/Chrome".to_owned(),
            ),
            (
                "DECISION_BROWSER_CONFIG_FILE".to_owned(),
                "/selected/provider.json".to_owned(),
            ),
        ]);
        for client in ["codex", "claude"] {
            let registration = registration(
                client,
                Path::new("/native/decision-browser"),
                Path::new("/project"),
                &selected,
            );
            assert_eq!(
                registration["env"]["DECISION_BROWSER_DEVTOOLS_ACTIVE_PORT"],
                "/selected/profile/DevToolsActivePort"
            );
            assert_eq!(
                registration["env"]["DECISION_BROWSER_CHROME"],
                "/selected/Chrome"
            );
            if client == "codex" {
                assert!(
                    registration["env_vars"]
                        .as_array()
                        .unwrap()
                        .contains(&json!("DECISION_BROWSER_DEVTOOLS_ACTIVE_PORT"))
                );
            }
        }
    }

    #[test]
    fn native_recovery_reads_a_large_but_bounded_intent() -> Result<()> {
        let project = Project::new();
        let bytes = vec![b'x'; 2 * 1024 * 1024];
        let after = vec![b'y'; 2 * 1024 * 1024];
        let target = project.0.join(".mcp.json");
        write_atomic(&target, Some(&after))?;
        let intent = project.0.join(STATE).join("native-intent.json");
        let body =
            json!({"owner":OWNER,"changes":[change(".mcp.json",Some(bytes.clone()),Some(after))]});
        let record = serde_json::to_vec(&body)?;
        assert!(record.len() as u64 > MAX_CONFIG);
        write_atomic(&intent, Some(&record))?;
        recover(&project.0)?;
        assert_eq!(fs::read(target)?, bytes);
        assert!(!intent.exists());
        Ok(())
    }

    #[test]
    fn overlarge_configuration_is_rejected_before_journal_creation() -> Result<()> {
        let project = Project::new();
        let changes = vec![change(
            ".mcp.json",
            None,
            Some(vec![b'x'; MAX_CONFIG as usize + 1]),
        )];
        assert!(transaction(&project.0, changes).is_err());
        assert!(!project.0.join(STATE).join("native-intent.json").exists());
        assert!(!project.0.join(".mcp.json").exists());
        Ok(())
    }

    #[test]
    fn legacy_active_intent_keeps_the_installation_blocked() -> Result<()> {
        let project = Project::new();
        fs::write(
            project.0.join(".decision-browser-stage.lease"),
            b"{\"active\":\"stage-lifecycle-v1\"}\n",
        )?;
        assert!(lock(&project.0).is_err());
        assert!(!project.0.join(STATE).exists());
        Ok(())
    }
    #[test]
    fn configure_accepts_live_routes_and_rejects_fixture() -> Result<()> {
        for provider in [
            "jev",
            "luna",
            "gemini",
            "openai",
            "typesafe",
            "openrouter",
            "opencode",
            "local",
            "nimble",
        ] {
            let options = Options::parse(&["configure".to_owned(), provider.to_owned()])?;
            assert_eq!(options.provider.as_deref(), Some(provider));
        }
        assert!(Options::parse(&["configure".to_owned(), "fixture".to_owned()]).is_err());
        Ok(())
    }

    #[test]
    fn setup_requires_explicit_or_interactive_client_and_respects_cancellation() -> Result<()> {
        let explicit = Options::parse(&[
            "setup".to_owned(),
            "--client".to_owned(),
            "claude".to_owned(),
        ])?;
        assert!(explicit.client_explicit);
        assert_eq!(explicit.clients, vec!["claude"]);
        let implicit = Options::parse(&["setup".to_owned()])?;
        assert!(!implicit.client_explicit);
        let mut output = Vec::new();
        assert!(
            choose_setup_client(false, &mut std::io::Cursor::new(b"both\n"), &mut output).is_err()
        );
        assert!(output.is_empty());
        assert!(choose_setup_client(true, &mut std::io::Cursor::new(b"\n"), &mut output).is_err());
        assert_eq!(
            choose_setup_client(true, &mut std::io::Cursor::new(b"both\n"), &mut output)?,
            vec!["codex", "claude"]
        );
        Ok(())
    }
    #[cfg(unix)]
    #[test]
    fn preflight_conflict_cannot_stage_a_new_runtime() -> Result<()> {
        let project = Project::new();
        write_atomic(
            &project.0.join(".mcp.json"),
            Some(br#"{"mcpServers":{"decision_browser":{"command":"user-owned"}}}"#),
        )?;
        assert!(
            install(
                &project.0,
                Path::new("/usr/bin/true"),
                &clients("both")?,
                false
            )
            .is_err()
        );
        assert!(!project.0.join(STATE).join("native-runtimes").exists());
        assert!(!project.0.join(".codex/config.toml").exists());
        Ok(())
    }

    #[cfg(unix)]
    #[test]
    fn failed_candidate_probe_keeps_the_previous_installation_and_removes_its_candidate()
    -> Result<()> {
        let project = Project::new();
        let names = clients("codex")?;
        install(&project.0, Path::new("/usr/bin/true"), &names, false)?;
        let config = fs::read(project.0.join(".codex/config.toml"))?;
        let manifest = fs::read(project.0.join(STATE).join("native-manifest.json"))?;
        let runtimes = project.0.join(STATE).join("native-runtimes");
        let before = fs::read_dir(&runtimes)?.count();
        assert!(install(&project.0, Path::new("/usr/bin/false"), &names, false).is_err());
        assert_eq!(fs::read(project.0.join(".codex/config.toml"))?, config);
        assert_eq!(
            fs::read(project.0.join(STATE).join("native-manifest.json"))?,
            manifest
        );
        assert_eq!(fs::read_dir(&runtimes)?.count(), before);
        assert!(!project.0.join(STATE).join("native-intent.json").exists());
        Ok(())
    }

    #[cfg(unix)]
    #[test]
    fn candidate_cleanup_preserves_added_files_and_modified_executables() -> Result<()> {
        let project = Project::new();
        let directory = project.0.join("candidate");
        let staged = StagedRuntime::prepare(directory.clone(), Path::new("/usr/bin/true"))?;
        fs::write(directory.join("user-note"), b"preserve")?;
        let binary = staged.binary.clone();
        drop(staged);
        assert_eq!(fs::read(directory.join("user-note"))?, b"preserve");
        assert!(binary.exists());
        let directory = project.0.join("modified");
        let staged = StagedRuntime::prepare(directory.clone(), Path::new("/usr/bin/true"))?;
        let binary = staged.binary.clone();
        fs::write(&binary, b"modified by user")?;
        drop(staged);
        assert_eq!(fs::read(binary)?, b"modified by user");
        Ok(())
    }

    #[test]
    fn real_legacy_installer_lease_is_locked_and_active_state_is_refused() -> Result<()> {
        let project = Project::new();
        let path = project.0.join(".decision-browser-install.lease");
        write_atomic(&path, Some(b"{\"owner\":\"decision-browser-lease-v1\"}\n"))?;
        let legacy = OpenOptions::new().read(true).write(true).open(&path)?;
        legacy.try_lock()?;
        assert!(lock(&project.0).is_err());
        legacy.unlock()?;
        fs::write(&path, b"{\"active\":\"stage-lifecycle-v1\"}\n")?;
        assert!(lock(&project.0).is_err());
        assert_eq!(inspect(&project.0)?["status"], "blocked");
        assert!(!project.0.join(STATE).exists());
        Ok(())
    }
    #[cfg(unix)]
    #[test]
    fn lease_validation_cannot_write_through_a_hardlink() -> Result<()> {
        let project = Project::new();
        let unrelated = project.0.join("unrelated");
        write_atomic(&unrelated, Some(b""))?;
        let lease = project.0.join(".decision-browser-install.lease");
        fs::hard_link(&unrelated, &lease)?;
        assert!(lock(&project.0).is_err());
        assert!(fs::read(&unrelated)?.is_empty());
        Ok(())
    }
    #[cfg(unix)]
    #[test]
    fn candidate_cleanup_refuses_a_replaced_directory() -> Result<()> {
        let project = Project::new();
        let directory = project.0.join("candidate");
        let staged = StagedRuntime::prepare(directory.clone(), Path::new("/usr/bin/true"))?;
        let saved = project.0.join("saved");
        fs::rename(&directory, &saved)?;
        let replacement = project.0.join("replacement");
        fs::create_dir(&replacement)?;
        fs::copy(
            saved.join("decision-browser"),
            replacement.join("decision-browser"),
        )?;
        std::os::unix::fs::symlink(&replacement, &directory)?;
        drop(staged);
        assert!(replacement.join("decision-browser").is_file());
        assert!(saved.join("decision-browser").is_file());
        Ok(())
    }
    #[test]
    fn legacy_contender_observes_native_lock_on_a_fresh_project() -> Result<()> {
        const PROJECT: &str = "DECISION_BROWSER_NATIVE_LEASE_TEST_PROJECT";
        const EXPECTED: &str = "DECISION_BROWSER_NATIVE_LEASE_TEST_EXPECTED";
        if let Some(project) = std::env::var_os(PROJECT) {
            let path = PathBuf::from(project).join(".decision-browser-install.lease");
            let contender = OpenOptions::new()
                .read(true)
                .write(true)
                .create(true)
                .truncate(false)
                .open(path)?;
            let acquired = contender.try_lock().is_ok();
            assert_eq!(acquired, std::env::var(EXPECTED)?.as_str() == "available");
            return Ok(());
        }
        let project = Project::new();
        let lease_path = project.0.join(".decision-browser-install.lease");
        assert!(!lease_path.exists());
        let lease = lock(&project.0)?;
        assert_eq!(
            fs::read(&lease_path)?,
            b"{\"owner\":\"decision-browser-lease-v1\"}\n"
        );
        assert!(!project.0.join(".decision-browser-stage.lease").exists());
        let child = |expected: &str| -> Result<()> {
            let output = Command::new(std::env::current_exe()?)
                .arg("legacy_contender_observes_native_lock_on_a_fresh_project")
                .env(PROJECT, &project.0)
                .env(EXPECTED, expected)
                .output()?;
            if !output.status.success() {
                return Err(format!(
                    "lease contender test failed: {}",
                    String::from_utf8_lossy(&output.stdout)
                )
                .into());
            }
            Ok(())
        };
        child("locked")?;
        drop(lease);
        child("available")?;
        assert!(!project.0.join(".decision-browser-stage.lease").exists());
        Ok(())
    }
}
