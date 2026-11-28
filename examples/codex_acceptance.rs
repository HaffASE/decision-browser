use base64::{Engine, engine::general_purpose::STANDARD};
use serde_json::{Value, json};
use std::error::Error;
use std::fs::{self, File};
use std::io::{BufRead, BufReader, Write};
use std::path::{Path, PathBuf};
use std::process::{Child, ChildStdin, Command, Stdio};
use std::sync::mpsc::{self, Receiver};
use std::thread;
use std::time::{Duration, Instant, SystemTime, UNIX_EPOCH};

type TestResult<T> = Result<T, Box<dyn Error + Send + Sync>>;
const SERVER_NAME: &str = "rust_cdp_counter";
const REQUEST_TIMEOUT: Duration = Duration::from_secs(60);
const PUBLIC_START: &str = "https://www.python.org/";
const PUBLIC_ABOUT: &str = "https://www.python.org/about/";

#[derive(Clone, Copy, PartialEq, Eq)]
enum Surface {
    Regression,
    Counter,
    Existing,
    Public,
}

struct FormScope {
    origin: String,
    start: String,
    mode: &'static str,
    public: bool,
}

struct AppServer {
    child: Child,
    input: ChildStdin,
    messages: Receiver<Result<Value, String>>,
    next_id: u64,
    forms: Vec<Value>,
    form_scope: Option<FormScope>,
}

impl AppServer {
    fn start(
        binary: &Path,
        scratch: &Path,
        output: &Path,
        provider: &str,
        product: bool,
    ) -> TestResult<Self> {
        // Codex merges configuration tables, so disable inherited entries explicitly.
        let inventory = Command::new("codex")
            .args([
                "--disable",
                "apps",
                "--disable",
                "plugins",
                "--disable",
                "memories",
            ])
            .args(["mcp", "list", "--json"])
            .current_dir(scratch)
            .output()?;
        if !inventory.status.success() {
            return Err("could not discover inherited Codex MCP servers".into());
        }
        let inventory: Value = serde_json::from_slice(&inventory.stdout)?;
        let inherited = inventory
            .as_array()
            .ok_or("unexpected Codex MCP inventory")?;
        let arguments = if product {
            r#"["serve", "--admission", "standard_form"]"#
        } else {
            "[]"
        };
        let config = format!(
            "mcp_servers = {{ {SERVER_NAME} = {{ command = {}, args = {arguments}, startup_timeout_sec = 15, tool_timeout_sec = 60, env_vars = [\"DECISION_BROWSER_CHROME\", \"DECISION_BROWSER_DEVTOOLS_ACTIVE_PORT\"], env = {{ DECISION_BROWSER_PROVIDER = {} }} }} }}",
            serde_json::to_string(&binary.to_string_lossy())?,
            serde_json::to_string(provider)?
        );
        let mut command = Command::new("codex");
        command.args([
            "app-server",
            "--listen",
            "stdio://",
            "--disable",
            "apps",
            "--disable",
            "plugins",
            "--disable",
            "memories",
            "-c",
            &config,
        ]);
        for server in inherited {
            let name = server["name"]
                .as_str()
                .ok_or("MCP inventory entry has no name")?;
            if name != SERVER_NAME {
                if !name
                    .bytes()
                    .all(|byte| byte.is_ascii_alphanumeric() || matches!(byte, b'_' | b'-'))
                {
                    return Err("cannot safely override an inherited MCP server name".into());
                }
                command
                    .arg("-c")
                    .arg(format!("mcp_servers.{name}.enabled = false"));
            }
        }
        let mut child = command
            .current_dir(scratch)
            .stdin(Stdio::piped())
            .stdout(Stdio::piped())
            .stderr(Stdio::from(File::create(output.join("codex-stderr.log"))?))
            .spawn()?;
        let input = child.stdin.take().ok_or("app-server stdin unavailable")?;
        let stdout = child.stdout.take().ok_or("app-server stdout unavailable")?;
        let (sender, messages) = mpsc::channel();
        thread::spawn(move || {
            for line in BufReader::new(stdout).lines() {
                let message = match line {
                    Ok(line) => serde_json::from_str(&line)
                        .map_err(|error| format!("app-server emitted invalid JSON: {error}")),
                    Err(error) => Err(format!("read app-server output: {error}")),
                };
                if sender.send(message).is_err() {
                    break;
                }
            }
        });
        Ok(Self {
            child,
            input,
            messages,
            next_id: 0,
            forms: Vec::new(),
            form_scope: None,
        })
    }

    fn send(&mut self, message: Value) -> TestResult<()> {
        serde_json::to_writer(&mut self.input, &message)?;
        self.input.write_all(b"\n")?;
        self.input.flush()?;
        Ok(())
    }

    fn request(&mut self, method: &str, params: Value) -> TestResult<Value> {
        self.request_with_form(method, params, None)
    }

    fn request_with_form(
        &mut self,
        method: &str,
        params: Value,
        form_action: Option<(&str, &str)>,
    ) -> TestResult<Value> {
        self.next_id += 1;
        let id = self.next_id;
        self.send(json!({"id": id, "method": method, "params": params}))?;
        let deadline = Instant::now() + REQUEST_TIMEOUT;
        let mut form_seen = false;
        loop {
            let remaining = deadline.saturating_duration_since(Instant::now());
            let message = self.messages.recv_timeout(remaining)??;
            if message.get("method").is_some() && message.get("id").is_some() {
                let (action, thread_id) = form_action.ok_or("unexpected app-server request")?;
                if form_seen {
                    return Err("multiple confirmation forms for one counter call".into());
                }
                validate_form(&message, thread_id, self.form_scope.as_ref())?;
                form_seen = true;
                self.forms.push(message["params"].clone());
                let result = match action {
                    "accept" => json!({"action": "accept", "content": {"reviewed": true}}),
                    "decline" => json!({"action": "decline"}),
                    _ => return Err("invalid local test form action".into()),
                };
                self.send(json!({"id": message["id"], "result": result}))?;
                continue;
            }
            if message.get("id").and_then(Value::as_u64) == Some(id) {
                if let Some(error) = message.get("error") {
                    return Err(format!("{method} failed: {error}").into());
                }
                if form_action.is_some() && !form_seen {
                    return Err(format!(
                        "{method} returned before requesting the expected form: {}",
                        message["result"]
                    )
                    .into());
                }
                return message
                    .get("result")
                    .cloned()
                    .ok_or_else(|| "missing app-server result".into());
            }
        }
    }
}

impl Drop for AppServer {
    fn drop(&mut self) {
        let _ = self.child.kill();
        let _ = self.child.wait();
    }
}

fn validate_form(message: &Value, thread_id: &str, scope: Option<&FormScope>) -> TestResult<()> {
    let params = &message["params"];
    if message["method"] != "mcpServer/elicitation/request"
        || params["serverName"] != SERVER_NAME
        || params["threadId"] != thread_id
        || params["mode"] != "form"
    {
        return Err("unexpected server, thread or confirmation method".into());
    }
    let schema = &params["requestedSchema"];
    let properties = schema["properties"]
        .as_object()
        .ok_or("form properties missing")?;
    if schema["type"] != "object"
        || schema["required"] != json!(["reviewed"])
        || properties.len() != 1
        || properties
            .get("reviewed")
            .is_none_or(|field| field["type"] != "boolean")
    {
        return Err("counter confirmation schema changed".into());
    }
    let text = params["message"].as_str().ok_or("form message missing")?;
    let valid = if let Some(scope) = scope {
        let scope_offset = text.find('{').ok_or("product form scope missing")?;
        let proposal: Value = serde_json::from_str(&text[scope_offset..])?;
        if proposal["kind"] == "alpha" {
            proposal["origin"] == scope.origin
                && proposal["start_url"] == scope.start
                && proposal["mode"] == scope.mode
        } else if proposal["kind"] == "beta" && proposal["document_origin"] == scope.origin {
            proposal["destination_origin"] == scope.origin
                && match proposal["operation"].as_str() {
                    Some("wait" | "scroll") => true,
                    Some("click") if scope.public => proposal["destination_url"] == PUBLIC_ABOUT,
                    Some("click") => proposal["description"] == "Click \"Increase\"",
                    _ => false,
                }
        } else {
            false
        }
    } else {
        text.contains("one click") && text.contains("local test counter")
    };
    if !valid {
        return Err("counter confirmation scope changed".into());
    }
    Ok(())
}

fn write_json(path: &Path, value: &Value) -> TestResult<()> {
    let mut file = File::create(path)?;
    serde_json::to_writer_pretty(&mut file, value)?;
    file.write_all(b"\n")?;
    Ok(())
}

fn main() -> TestResult<()> {
    let mut args = std::env::args_os().skip(1);
    let usage = "usage: codex_acceptance MCP_BINARY [EVIDENCE_DIRECTORY] [fixture|jev|luna|gemini] [--product|--product-existing|--product-public]";
    let binary = args.next().ok_or(usage)?;
    let binary = PathBuf::from(binary).canonicalize()?;
    let nonce = SystemTime::now().duration_since(UNIX_EPOCH)?.as_nanos();
    let scratch =
        std::env::temp_dir().join(format!("rust-cdp-codex-{}-{nonce}", std::process::id()));
    fs::create_dir(&scratch)?;
    let output = args
        .next()
        .map(PathBuf::from)
        .unwrap_or_else(|| scratch.join("evidence"));
    let provider = args.next().unwrap_or_else(|| "fixture".into());
    let provider = provider.to_str().ok_or(usage)?;
    let surface = match args.next().as_deref().and_then(std::ffi::OsStr::to_str) {
        None => Surface::Regression,
        Some("--product") => Surface::Counter,
        Some("--product-existing") => Surface::Existing,
        Some("--product-public") => Surface::Public,
        _ => return Err(usage.into()),
    };
    if !["fixture", "jev", "luna", "gemini"].contains(&provider) || args.next().is_some() {
        return Err(usage.into());
    }
    if surface == Surface::Existing
        && std::env::var_os("DECISION_BROWSER_DEVTOOLS_ACTIVE_PORT").is_none()
    {
        return Err("--product-existing requires an explicitly selected disposable Chrome DevToolsActivePort".into());
    }
    if surface == Surface::Public && provider == "fixture" {
        return Err("--product-public requires a live decision provider".into());
    }
    fs::create_dir_all(&output)?;
    if fs::read_dir(&output)?.next().is_some() {
        return Err("evidence directory must be empty".into());
    }
    let output = output.canonicalize()?;
    let result = run(&binary, &scratch, &output, provider, surface);
    let _ = fs::remove_dir(&scratch);
    result
}

fn run(
    binary: &Path,
    scratch: &Path,
    output: &Path,
    provider: &str,
    surface: Surface,
) -> TestResult<()> {
    let product = surface != Surface::Regression;
    let mut app = AppServer::start(binary, scratch, output, provider, product)?;
    app.request("initialize", json!({
        "clientInfo": {"name": "rust_cdp_acceptance", "title": "Rust CDP acceptance test", "version": "0.1.0"},
        "capabilities": {"experimentalApi": true}
    }))?;
    app.send(json!({"method": "initialized", "params": {}}))?;
    let started = app.request(
        "thread/start",
        json!({
            "cwd": scratch,
            "ephemeral": true,
            "approvalPolicy": "on-request",
            "approvalsReviewer": "user",
            "sandbox": "workspace-write"
        }),
    )?;
    let thread_id = started["thread"]["id"]
        .as_str()
        .ok_or("thread/start returned no thread ID")?
        .to_owned();
    let servers = app.request(
        "mcpServerStatus/list",
        json!({
            "threadId": thread_id, "detail": "toolsAndAuthOnly"
        }),
    )?;
    write_json(&output.join("mcp-status.json"), &servers)?;
    for server in servers["data"]
        .as_array()
        .ok_or("MCP status list missing")?
    {
        if server["name"] != SERVER_NAME
            && (server["tools"]
                .as_object()
                .is_some_and(|tools| !tools.is_empty())
                || (!server["runtimeStatus"].is_null()
                    && server["runtimeStatus"] != "disabled"
                    && server["runtimeStatus"] != "notStarted"))
        {
            return Err("an inherited MCP server started during the isolated test".into());
        }
    }
    if product {
        return product_task(&mut app, &thread_id, provider, output, surface);
    }
    let arguments = json!({"threadId": thread_id, "server": SERVER_NAME, "tool": "run_counter", "arguments": {}});
    let declined = app.request_with_form(
        "mcpServer/tool/call",
        arguments.clone(),
        Some(("decline", &thread_id)),
    )?;
    write_json(&output.join("declined.json"), &declined)?;
    write_json(&output.join("forms.json"), &json!(app.forms))?;
    if declined["structuredContent"]["status"] != "declined" {
        return Err("declined form did not produce the declined status".into());
    }
    let accepted_started = Instant::now();
    let mut accepted = app.request_with_form(
        "mcpServer/tool/call",
        arguments,
        Some(("accept", &thread_id)),
    )?;
    let accepted_call_ms = accepted_started.elapsed().as_millis() as u64;
    let contents = accepted["content"]
        .as_array_mut()
        .ok_or("accepted result has no content")?;
    let mut png_bytes = 0;
    for item in contents {
        if item["type"] == "image" {
            if item["mimeType"] != "image/png" || png_bytes != 0 {
                return Err("counter result has an unexpected image".into());
            }
            let png = STANDARD.decode(item["data"].as_str().ok_or("PNG data missing")?)?;
            if !png.starts_with(b"\x89PNG\r\n\x1a\n") {
                return Err("counter result contains invalid PNG data".into());
            }
            png_bytes = png.len();
            fs::write(output.join("counter.png"), png)?;
            item.as_object_mut()
                .ok_or("image content malformed")?
                .remove("data");
            item["savedTo"] = json!("counter.png");
        }
    }
    write_json(&output.join("accepted.json"), &accepted)?;
    write_json(&output.join("forms.json"), &json!(app.forms))?;
    let evidence = &accepted["structuredContent"];
    if evidence["status"] != "verified"
        || evidence["before"] != 0
        || evidence["after"] != 1
        || evidence["provider"] != provider
        || accepted["isError"] == true
        || png_bytes == 0
    {
        return Err("accepted form did not verify the counter and PNG".into());
    }
    let mut summary = json!({
        "status": "verified",
        "host": "Codex app-server",
        "approvalSource": "programmatic acceptance test",
        "nativeUiAcceptance": "not exercised",
        "provider": provider,
        "modelRequests": if provider == "fixture" {0} else {1},
        "declined": true,
        "before": 0,
        "after": 1,
        "pngBytes": png_bytes,
        "acceptedCallMs": accepted_call_ms
    });
    for field in ["model", "inputTokens", "outputTokens"] {
        if let Some(value) = evidence.get(field) {
            summary[field] = value.clone();
        }
    }
    write_json(&output.join("summary.json"), &summary)?;
    println!("{}", serde_json::to_string_pretty(&summary)?);
    println!("Evidence saved in {}", output.display());
    Ok(())
}

fn product_task(
    app: &mut AppServer,
    thread: &str,
    provider: &str,
    output: &Path,
    surface: Surface,
) -> TestResult<()> {
    let fixture = (surface != Surface::Public)
        .then(|| decision_browser_rust_cdp::matrix_fixtures::start("counter"))
        .transpose()?;
    let mode = if surface == Surface::Existing {
        "existing_chrome"
    } else {
        "isolated"
    };
    let start_url = fixture
        .as_ref()
        .map_or(PUBLIC_START, |fixture| &fixture.url);
    app.form_scope = Some(FormScope {
        origin: reqwest::Url::parse(start_url)?
            .origin()
            .ascii_serialization(),
        start: start_url.to_owned(),
        mode,
        public: surface == Surface::Public,
    });
    let call = |tool: &str, arguments: Value| json!({"threadId":thread,"server":SERVER_NAME,"tool":tool,"arguments":arguments});
    let capabilities = app.request("mcpServer/tool/call", call("capabilities", json!({})))?;
    write_json(&output.join("capabilities.json"), &capabilities)?;
    let (goal, checks) = match &fixture {
        Some(fixture) => (
            fixture.goal.as_str(),
            json!([{"kind":"text","target":"#status","expected":"Count: 3"}]),
        ),
        None => (
            "Open the About page via the About link. Verify the final About page URL.",
            json!([{"kind":"route","target":"","expected":PUBLIC_ABOUT}]),
        ),
    };
    let request = json!({"goal":goal,"start_url":start_url,"mode":mode,"values":{},"checks":checks,
        "privacy":{},"limits":{"max_steps":8,"max_provider_calls":12,"max_seconds":55.0}});
    let started_at = Instant::now();
    let started = app.request(
        "mcpServer/tool/call",
        call("start_task", json!({"request":request})),
    )?;
    write_json(&output.join("started.json"), &started)?;
    let run_id = started
        .pointer("/structuredContent/result/run_id")
        .and_then(Value::as_str)
        .ok_or("start_task returned no run ID")?;
    let mut previous = Value::Null;
    let terminal = loop {
        if started_at.elapsed() > REQUEST_TIMEOUT {
            return Err("product run did not reach a terminal result".into());
        }
        let result = app.request(
            "mcpServer/tool/call",
            call("get_task", json!({"run_id":run_id})),
        )?;
        let snapshot = &result["structuredContent"];
        let proposal = &snapshot["pending_proposal"];
        if !proposal.is_null() && *proposal != previous {
            let approval = json!({
                "run_id":run_id,"proposal_id":proposal["proposal_id"],"digest":proposal["digest"]
            });
            app.request_with_form(
                "mcpServer/tool/call",
                call("present_admission", approval),
                Some(("accept", thread)),
            )?;
            previous = proposal.clone();
            write_json(&output.join("forms.json"), &json!(app.forms))?;
        }
        match snapshot["result"]["terminal"].as_str() {
            Some("queued" | "running" | "awaiting_human_admission") => {}
            Some(_) => break snapshot["result"].clone(),
            None => return Err("get_task returned no task result".into()),
        }
        thread::sleep(Duration::from_millis(75));
    };
    write_json(&output.join("terminal.json"), &terminal)?;
    let forms_verified = if surface == Surface::Public {
        app.forms.len() == 1
    } else {
        app.forms.len() >= 2
    };
    if terminal["terminal"] != "verified"
        || terminal["functional"] != "verified"
        || fixture
            .as_ref()
            .map(|fixture| fixture.verify())
            .transpose()?
            == Some(false)
        || !forms_verified
    {
        return Err("product did not verify the declared checks and admission".into());
    }
    let artifact_id = terminal
        .pointer("/artifact_ids/0")
        .and_then(Value::as_str)
        .ok_or("product run has no evidence artifact")?;
    let artifact = app.request(
        "mcpServer/tool/call",
        call(
            "read_artifact",
            json!({
                "run_id":run_id,"artifact_id":artifact_id,"channel":"structured"
            }),
        ),
    )?;
    let chunk = &artifact["structuredContent"];
    let report = STANDARD.decode(
        chunk["content"]
            .as_str()
            .ok_or("artifact content missing")?,
    )?;
    if chunk["eof"] != true
        || chunk["media_type"] != "application/json"
        || serde_json::from_slice::<Value>(&report)? != json!({"checks_passed":1})
    {
        return Err("unexpected product check evidence".into());
    }
    fs::write(output.join("checks.json"), &report)?;
    let mut summary = json!({
        "status":"verified", "host":"Codex app-server", "provider":provider,
        "approvalSource":"programmatic acceptance test", "forms":app.forms.len(),
        "elapsedMs":started_at.elapsed().as_millis() as u64, "mode":mode,
        "artifactBytes":report.len(), "artifactType":"application/json", "usage":terminal["usage"]
    });
    if fixture.is_some() {
        summary["counter"] = json!(3);
    } else {
        summary["verifiedUrl"] = json!(PUBLIC_ABOUT);
    }
    write_json(&output.join("summary.json"), &summary)?;
    println!("{}", serde_json::to_string_pretty(&summary)?);
    Ok(())
}
