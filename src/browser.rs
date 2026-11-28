use base64::Engine;
use serde_json::{Value, json};
use std::error::Error;
use std::fs;
use std::io::{self, Read, Write};
use std::net::{SocketAddr, TcpListener, TcpStream};
use std::path::PathBuf;
use std::process::{Child, Command, Stdio};
use std::sync::Arc;
use std::sync::atomic::{AtomicBool, Ordering};
use std::thread::{self, JoinHandle};
use std::time::{Duration, Instant, SystemTime, UNIX_EPOCH};
use tungstenite::{Message, WebSocket};

#[path = "matrix_browser.rs"]
mod matrix_browser;
pub use matrix_browser::{
    MatrixPage, existing_chrome_available, run_existing_task_scenario, run_scenario,
    run_task_scenario,
};

pub type BrowserResult<T> = Result<T, Box<dyn Error + Send + Sync>>;

const COUNTER_HTML: &str = include_str!("../examples/browser-control/counter.html");
const COMMAND_TIMEOUT: Duration = Duration::from_secs(5);
const RUN_TIMEOUT: Duration = Duration::from_secs(30);
const OBSERVE_COUNTER: &str = "(() => { const count = document.getElementById('count'); const button = document.getElementById('inc'); if (!count || !button || document.readyState !== 'complete') return null; const r = button.getBoundingClientRect(); return {url: location.href, count: Number(count.textContent), label: button.textContent, x: r.x + r.width / 2, y: r.y + r.height / 2, width: r.width, height: r.height}; })()";

pub struct CounterEvidence {
    pub before: u32,
    pub after: u32,
    pub png: Vec<u8>,
}

/// Exercise exactly one native click against the owned loopback fixture.
pub fn run_counter(
    choose_increment: impl FnOnce(&str, u32) -> BrowserResult<()>,
) -> BrowserResult<CounterEvidence> {
    run_counter_guarded(choose_increment, || Ok(()))
}

/// Stop cancelled work before provider calls and each native input dispatch.
pub fn run_counter_guarded(
    choose_increment: impl FnOnce(&str, u32) -> BrowserResult<()>,
    check_cancelled: impl Fn() -> BrowserResult<()>,
) -> BrowserResult<CounterEvidence> {
    check_cancelled()?;
    let fixture = CounterServer::start()?;
    check_cancelled()?;
    let mut chrome = OwnedChrome::launch()?;
    let outcome = run_counter_in_browser(&fixture.url, &chrome, choose_increment, &check_cancelled);
    let cleanup = chrome.finish();
    match (outcome, cleanup) {
        (Ok(evidence), Ok(())) => Ok(evidence),
        (Err(error), Ok(())) => Err(error),
        (Ok(_), Err(error)) => Err(error),
        (Err(error), Err(cleanup)) => Err(failure(format!(
            "{error}; browser cleanup failed: {cleanup}"
        ))),
    }
}

fn run_counter_in_browser(
    url: &str,
    chrome: &OwnedChrome,
    choose_increment: impl FnOnce(&str, u32) -> BrowserResult<()>,
    check_cancelled: &impl Fn() -> BrowserResult<()>,
) -> BrowserResult<CounterEvidence> {
    check_cancelled()?;
    let deadline = Instant::now() + RUN_TIMEOUT;
    let endpoint = chrome.debugging_endpoint(deadline)?;
    let mut cdp = Cdp::connect(&endpoint, deadline)?;
    let outcome = run_counter_on_page(url, &mut cdp, deadline, choose_increment, check_cancelled);
    // Failure and cancellation also require a graceful browser shutdown before
    // the process supervisor considers its bounded termination fallback.
    let _ = cdp.call("Browser.close", json!({}), None);
    outcome
}

fn run_counter_on_page(
    url: &str,
    cdp: &mut Cdp,
    deadline: Instant,
    choose_increment: impl FnOnce(&str, u32) -> BrowserResult<()>,
    check_cancelled: &impl Fn() -> BrowserResult<()>,
) -> BrowserResult<CounterEvidence> {
    let context = cdp.call("Target.createBrowserContext", json!({}), None)?;
    let context_id = string_field(&context, "browserContextId")?;
    let target = cdp.call(
        "Target.createTarget",
        json!({"url": "about:blank", "browserContextId": context_id}),
        None,
    )?;
    let target_id = string_field(&target, "targetId")?;
    let attached = cdp.call(
        "Target.attachToTarget",
        json!({"targetId": target_id, "flatten": true}),
        None,
    )?;
    let session = string_field(&attached, "sessionId")?;
    cdp.call("Page.enable", json!({}), Some(&session))?;
    cdp.call("Runtime.enable", json!({}), Some(&session))?;
    let navigation = cdp.call("Page.navigate", json!({"url": url}), Some(&session))?;
    if let Some(error) = navigation.get("errorText") {
        return Err(failure(format!("counter navigation failed: {error}")));
    }

    // Poll this exact document so an unrelated lifecycle event cannot unlock input.
    let observation = loop {
        check_cancelled()?;
        let observed = cdp.evaluate(OBSERVE_COUNTER, &session)?;
        if !observed.is_null() {
            break observed;
        }
        if Instant::now() >= deadline {
            return Err(failure("counter fixture did not become ready"));
        }
        thread::sleep(Duration::from_millis(20));
    };
    if observation.get("url").and_then(Value::as_str) != Some(url) {
        return Err(failure("counter document URL changed before input"));
    }
    let before = counter_value(&observation["count"])?;
    if before != 0 {
        return Err(failure("counter must start at zero"));
    }
    let label = string_field(&observation, "label")?;
    check_cancelled()?;
    choose_increment(&label, before)?;
    check_cancelled()?;
    let observation = cdp.evaluate(OBSERVE_COUNTER, &session)?;
    if observation.get("url").and_then(Value::as_str) != Some(url)
        || counter_value(&observation["count"])? != before
        || observation.get("label").and_then(Value::as_str) != Some(&label)
    {
        return Err(failure("counter changed during provider selection"));
    }
    let x = number_field(&observation, "x")?;
    let y = number_field(&observation, "y")?;
    if number_field(&observation, "width")? <= 0.0 || number_field(&observation, "height")? <= 0.0 {
        return Err(failure("counter button has no clickable area"));
    }
    let hit = cdp.evaluate(
        &format!(
            "location.href === {} && Number(document.getElementById('count').textContent) === 0 && document.elementFromPoint({x}, {y}) === document.getElementById('inc')",
            serde_json::to_string(url)?,
        ),
        &session,
    )?;
    if hit != Value::Bool(true) {
        return Err(failure("counter button failed the native hit test"));
    }
    for (event, buttons) in [("mousePressed", 1), ("mouseReleased", 0)] {
        check_cancelled()?;
        cdp.call(
            "Input.dispatchMouseEvent",
            json!({"type": event, "x": x, "y": y, "button": "left", "buttons": buttons, "clickCount": 1}),
            Some(&session),
        )?;
    }
    let final_state = cdp.evaluate(
        "({url: location.href, count: Number(document.getElementById('count').textContent)})",
        &session,
    )?;
    if final_state.get("url").and_then(Value::as_str) != Some(url) {
        return Err(failure("counter document URL changed after input"));
    }
    let after = counter_value(&final_state["count"])?;
    if after != 1 {
        return Err(failure(format!(
            "expected one native click, observed {after}"
        )));
    }
    let screenshot = cdp.call(
        "Page.captureScreenshot",
        json!({"format": "png", "captureBeyondViewport": false}),
        Some(&session),
    )?;
    let png =
        base64::engine::general_purpose::STANDARD.decode(string_field(&screenshot, "data")?)?;
    if !png.starts_with(b"\x89PNG\r\n\x1a\n") {
        return Err(failure("CDP screenshot is not PNG"));
    }
    cdp.call("Target.closeTarget", json!({"targetId": target_id}), None)?;
    cdp.call(
        "Target.disposeBrowserContext",
        json!({"browserContextId": context_id}),
        None,
    )?;
    Ok(CounterEvidence { before, after, png })
}

struct Cdp {
    socket: WebSocket<TcpStream>,
    next_id: u64,
    deadline: Instant,
    origin_filter: Option<(String, String)>,
    filter_commands: std::collections::HashSet<u64>,
    document_responses: std::collections::HashMap<u64, Value>,
    pending_network: std::collections::HashSet<String>,
}

impl Cdp {
    fn connect(endpoint: &str, deadline: Instant) -> BrowserResult<Self> {
        let address = endpoint
            .strip_prefix("ws://127.0.0.1:")
            .and_then(|rest| rest.split('/').next())
            .ok_or_else(|| failure("Chrome returned a non-loopback CDP endpoint"))?;
        let port = address.parse::<u16>()?;
        let stream = TcpStream::connect_timeout(
            &SocketAddr::from(([127, 0, 0, 1], port)),
            Duration::from_secs(2),
        )?;
        stream.set_read_timeout(Some(COMMAND_TIMEOUT))?;
        stream.set_write_timeout(Some(Duration::from_secs(2)))?;
        let (socket, _) = tungstenite::client(endpoint, stream)?;
        socket
            .get_ref()
            .set_read_timeout(Some(Duration::from_millis(250)))?;
        Ok(Self {
            socket,
            next_id: 0,
            deadline,
            origin_filter: None,
            filter_commands: std::collections::HashSet::new(),
            document_responses: std::collections::HashMap::new(),
            pending_network: std::collections::HashSet::new(),
        })
    }

    fn restrict_origin(&mut self, session: &str, origin: &str) -> BrowserResult<()> {
        self.origin_filter = Some((session.to_owned(), origin.to_owned()));
        self.call("Network.enable", json!({}), Some(session))?;
        self.call(
            "Network.setBypassServiceWorker",
            json!({"bypass":true}),
            Some(session),
        )?;
        self.call(
            "Network.setCacheDisabled",
            json!({"cacheDisabled":true}),
            Some(session),
        )?;
        self.call(
            "Fetch.enable",
            json!({"patterns":[{"urlPattern":"*","requestStage":"Request"},{"urlPattern":"*","resourceType":"Document","requestStage":"Response"}]}),
            Some(session),
        )?;
        Ok(())
    }

    fn handle_paused_request(&mut self, event: &Value) -> BrowserResult<()> {
        let (session, origin) = self
            .origin_filter
            .as_ref()
            .ok_or_else(|| failure("unexpected paused network request"))?;
        if event["sessionId"] != *session {
            return Err(failure("network request came from an unguarded session"));
        }
        let request_id = event["params"]["requestId"]
            .as_str()
            .ok_or_else(|| failure("paused request ID missing"))?;
        let url = event["params"]["request"]["url"]
            .as_str()
            .ok_or_else(|| failure("paused request URL missing"))?;
        let allow = reqwest::Url::parse(url).ok().is_some_and(|url| {
            matches!(url.scheme(), "http" | "https")
                && url.username().is_empty()
                && url.password().is_none()
                && url.origin().ascii_serialization() == *origin
        });
        if self.filter_commands.len() >= 4096 {
            return Err(failure("network guard queue limit"));
        }
        let wait_for_completion = event["params"]["resourceType"] == "Document"
            || !matches!(
                event["params"]["request"]["method"].as_str(),
                Some("GET" | "HEAD" | "OPTIONS")
            );
        if allow
            && wait_for_completion
            && let Some(id) = event["params"]["networkId"].as_str()
        {
            self.pending_network.insert(id.to_owned());
        }
        self.next_id += 1;
        let id = self.next_id;
        let response_code = event["params"]["responseStatusCode"].as_u64();
        let (method, params) = if allow
            && response_code.is_some_and(|code| (300..400).contains(&code) || code == 204)
        {
            ("Fetch.continueResponse", json!({"requestId":request_id}))
        } else if allow && response_code.is_some() {
            let mut headers = event["params"]["responseHeaders"]
                .as_array()
                .cloned()
                .ok_or_else(|| failure("document response headers missing"))?;
            // A response policy applies before any page script runs. Limit
            // unsupported browsing contexts and transports to the owned page.
            headers.push(json!({"name":"Content-Security-Policy","value":format!("sandbox allow-scripts allow-same-origin allow-forms; base-uri 'self'; form-action {origin}; frame-src 'none'; worker-src 'none'; object-src 'none'; connect-src {origin}")}));
            // HTTP/2 has no reason phrase. Let Chrome derive the canonical
            // phrase from the status code when reconstructing the response.
            self.document_responses.insert(id,json!({"requestId":request_id,"responseCode":event["params"]["responseStatusCode"],"responseHeaders":headers}));
            ("Fetch.getResponseBody", json!({"requestId":request_id}))
        } else if allow {
            (
                "Fetch.continueRequest",
                json!({"requestId":request_id,"interceptResponse":event["params"]["resourceType"]=="Document"}),
            )
        } else {
            (
                "Fetch.failRequest",
                json!({"requestId":request_id,"errorReason":"BlockedByClient"}),
            )
        };
        self.socket.send(Message::Text(
            json!({"id":id,"method":method,"sessionId":session,"params":params})
                .to_string()
                .into(),
        ))?;
        self.filter_commands.insert(id);
        Ok(())
    }

    fn process_network_event(&mut self, event: &Value) -> BrowserResult<bool> {
        if let Some(mut response) = event["id"]
            .as_u64()
            .and_then(|id| self.document_responses.remove(&id))
        {
            use base64::Engine;
            self.filter_commands.remove(&event["id"].as_u64().unwrap());
            if event.get("error").is_some() {
                return Err(failure("document response inspection failed"));
            }
            let body = event["result"]["body"]
                .as_str()
                .ok_or_else(|| failure("document body missing"))?;
            if body.len() > 16 * 1024 * 1024 {
                return Err(failure("document body exceeds transport limit"));
            }
            let bytes = if event["result"]["base64Encoded"] == true {
                base64::engine::general_purpose::STANDARD.decode(body)?
            } else {
                body.as_bytes().to_vec()
            };
            let headers = response["responseHeaders"]
                .as_array_mut()
                .ok_or_else(|| failure("document response headers missing"))?;
            headers.retain(|header| {
                !header["name"].as_str().is_some_and(|name| {
                    ["content-encoding", "content-length", "transfer-encoding"]
                        .iter()
                        .any(|excluded| name.eq_ignore_ascii_case(excluded))
                })
            });
            headers.push(json!({"name":"Content-Length","value":bytes.len().to_string()}));
            response["body"] = json!(base64::engine::general_purpose::STANDARD.encode(bytes));
            self.next_id += 1;
            let id = self.next_id;
            self.socket.send(Message::Text(json!({"id":id,"method":"Fetch.fulfillRequest","params":response,"sessionId":self.origin_filter.as_ref().unwrap().0}).to_string().into()))?;
            self.filter_commands.insert(id);
            return Ok(true);
        }
        if event["method"] == "Fetch.requestPaused" {
            self.handle_paused_request(event)?;
            return Ok(true);
        }
        if matches!(
            event["method"].as_str(),
            Some("Network.loadingFinished" | "Network.loadingFailed")
        ) {
            if self
                .origin_filter
                .as_ref()
                .is_some_and(|(session, _)| event["sessionId"] == *session)
                && let Some(id) = event["params"]["requestId"].as_str()
            {
                self.pending_network.remove(id);
            }
            return Ok(true);
        }
        if event["id"]
            .as_u64()
            .is_some_and(|id| self.filter_commands.remove(&id))
        {
            if event.get("error").is_some() {
                return Err(failure("network guard command failed"));
            }
            return Ok(true);
        }
        Ok(false)
    }

    /// Drain request-stage pauses and wait for their network completions before
    /// observing the next action. Pending commands are never replayed.
    fn settle_network(&mut self) -> BrowserResult<()> {
        if self.origin_filter.is_none() {
            return Ok(());
        }
        let deadline = self.deadline.min(Instant::now() + Duration::from_secs(3));
        let mut quiet_since = Instant::now();
        loop {
            if Instant::now() >= deadline {
                return Err(failure("network settlement deadline exceeded"));
            }
            let message = match self.socket.read() {
                Ok(message) => message,
                Err(tungstenite::Error::Io(error))
                    if matches!(
                        error.kind(),
                        io::ErrorKind::TimedOut | io::ErrorKind::WouldBlock
                    ) =>
                {
                    if self.pending_network.is_empty()
                        && self.filter_commands.is_empty()
                        && quiet_since.elapsed() >= Duration::from_millis(150)
                    {
                        return Ok(());
                    }
                    continue;
                }
                Err(error) => return Err(error.into()),
            };
            if let Message::Text(text) = message {
                let event: Value = serde_json::from_str(&text)?;
                let completing = event["params"]["requestId"]
                    .as_str()
                    .is_some_and(|id| self.pending_network.contains(id));
                let pausing_effect = event["method"] == "Fetch.requestPaused"
                    && (event["params"]["resourceType"] == "Document"
                        || !matches!(
                            event["params"]["request"]["method"].as_str(),
                            Some("GET" | "HEAD" | "OPTIONS")
                        ));
                self.process_network_event(&event)?;
                if completing || pausing_effect {
                    quiet_since = Instant::now();
                }
                if self.pending_network.is_empty()
                    && self.filter_commands.is_empty()
                    && quiet_since.elapsed() >= Duration::from_millis(150)
                {
                    return Ok(());
                }
            }
        }
    }

    fn call(&mut self, method: &str, params: Value, session: Option<&str>) -> BrowserResult<Value> {
        let deadline = self.deadline.min(Instant::now() + COMMAND_TIMEOUT);
        if Instant::now() >= deadline {
            return Err(failure("CDP run deadline exceeded"));
        }
        self.next_id += 1;
        let id = self.next_id;
        let mut request = json!({"id": id, "method": method, "params": params});
        if let Some(session_id) = session {
            request["sessionId"] = Value::String(session_id.to_owned());
        }
        self.socket
            .send(Message::Text(request.to_string().into()))?;
        loop {
            if Instant::now() >= deadline {
                return Err(failure(format!("CDP command timed out: {method}")));
            }
            let message = match self.socket.read() {
                Ok(message) => message,
                Err(tungstenite::Error::Io(error))
                    if matches!(
                        error.kind(),
                        io::ErrorKind::WouldBlock | io::ErrorKind::TimedOut
                    ) =>
                {
                    continue;
                }
                Err(error) => return Err(error.into()),
            };
            let Message::Text(text) = message else {
                continue;
            };
            let response: Value = serde_json::from_str(&text)?;
            if self.process_network_event(&response)? {
                continue;
            }

            if response.get("id").and_then(Value::as_u64) != Some(id) {
                continue;
            }
            if let Some(error) = response.get("error") {
                return Err(failure(format!("CDP {method} failed: {error}")));
            }
            return response
                .get("result")
                .cloned()
                .ok_or_else(|| failure("CDP response has no result"));
        }
    }

    fn evaluate(&mut self, expression: &str, session: &str) -> BrowserResult<Value> {
        let result = self.call(
            "Runtime.evaluate",
            json!({"expression": expression, "returnByValue": true}),
            Some(session),
        )?;
        if result.get("exceptionDetails").is_some() {
            return Err(failure("counter observation raised a JavaScript exception"));
        }
        result
            .pointer("/result/value")
            .cloned()
            .ok_or_else(|| failure("CDP observation has no value"))
    }
}

struct OwnedChrome {
    child: Option<Child>,
    profile: PathBuf,
}

impl OwnedChrome {
    fn launch() -> BrowserResult<Self> {
        let executable = chrome_executable()?;
        let profile = unique_profile()?;
        let mut command = Command::new(executable);
        #[cfg(unix)]
        {
            use std::os::unix::process::CommandExt;
            command.process_group(0);
        }
        let child = command
            .arg("--headless=new")
            .arg("--remote-debugging-port=0")
            .arg("--remote-debugging-address=127.0.0.1")
            .arg(format!("--user-data-dir={}", profile.display()))
            .args([
                "--no-first-run",
                "--no-default-browser-check",
                "--disable-background-networking",
                "--disable-component-update",
                "--disable-sync",
                "--metrics-recording-only",
                "--window-size=900,700",
                "about:blank",
            ])
            .stdin(Stdio::null())
            .stdout(Stdio::null())
            .stderr(Stdio::null())
            .spawn();
        match child {
            Ok(child) => Ok(Self {
                child: Some(child),
                profile,
            }),
            Err(error) => {
                fs::remove_dir_all(&profile)?;
                Err(error.into())
            }
        }
    }

    fn debugging_endpoint(&self, deadline: Instant) -> BrowserResult<String> {
        let deadline = deadline.min(Instant::now() + Duration::from_secs(10));
        let port_file = self.profile.join("DevToolsActivePort");
        loop {
            if let Ok(contents) = fs::read_to_string(&port_file) {
                let mut lines = contents.lines();
                if let (Some(port), Some(path)) = (lines.next(), lines.next()) {
                    let port = port.parse::<u16>()?;
                    if path.starts_with("/devtools/browser/") {
                        return Ok(format!("ws://127.0.0.1:{port}{path}"));
                    }
                }
            }
            if Instant::now() >= deadline {
                return Err(failure(
                    "Chrome did not publish its CDP endpoint within 10 seconds",
                ));
            }
            thread::sleep(Duration::from_millis(25));
        }
    }

    fn finish(&mut self) -> BrowserResult<()> {
        if let Some(child) = self.child.as_mut() {
            let deadline = Instant::now() + Duration::from_secs(2);
            let mut exited = child.try_wait()?.is_some();
            while !exited && Instant::now() < deadline {
                thread::sleep(Duration::from_millis(25));
                exited = child.try_wait()?.is_some();
            }
            #[cfg(unix)]
            {
                let group = i32::try_from(child.id())?;
                // The live child still owns the process group. Once reaped, only
                // probe the group so a recycled ID cannot authorize a signal.
                unsafe extern "C" {
                    fn kill(pid: i32, signal: i32) -> i32;
                }
                if !exited && unsafe { kill(-group, 9) } != 0 {
                    let error = io::Error::last_os_error();
                    if error.raw_os_error() != Some(3) {
                        return Err(failure(format!(
                            "owned Chrome group termination failed: {error}"
                        )));
                    }
                }
                child.wait()?;
                let deadline = Instant::now() + Duration::from_secs(2);
                loop {
                    if unsafe { kill(-group, 0) } != 0 {
                        let error = io::Error::last_os_error();
                        if error.raw_os_error() == Some(libc::ESRCH) {
                            break;
                        }
                        if error.raw_os_error() == Some(libc::EPERM) {
                            if !process_group_in_inventory(group, deadline)? {
                                break;
                            }
                        } else {
                            return Err(failure(format!(
                                "owned Chrome group liveness unverified: {error}"
                            )));
                        }
                    }
                    if Instant::now() >= deadline {
                        return Err(failure("owned Chrome process group is still present"));
                    }
                    thread::sleep(Duration::from_millis(25));
                }
            }
            #[cfg(not(unix))]
            {
                if !exited {
                    child.kill()?;
                }
                child.wait()?;
            }
            self.child = None;
        }
        if self.profile.exists() {
            fs::remove_dir_all(&self.profile).map_err(|error| {
                failure(format!("owned Chrome profile cleanup failed: {error}"))
            })?;
        }
        Ok(())
    }
}

#[cfg(unix)]
fn inventory_contains_group(bytes: &[u8], group: i32, owner: u32) -> BrowserResult<bool> {
    let text = std::str::from_utf8(bytes).map_err(|_| failure("process inventory is not UTF-8"))?;
    let mut rows = 0;
    let mut present = false;
    for line in text.lines() {
        let fields = line.split_ascii_whitespace().collect::<Vec<_>>();
        if fields.len() != 3 {
            return Err(failure("process inventory has an invalid row"));
        }
        let observed_group = fields[0]
            .parse::<i32>()
            .map_err(|_| failure("process inventory has an invalid group"))?;
        if observed_group < -1 {
            return Err(failure("process inventory has an invalid group"));
        }
        rows += 1;
        // System processes can have no group and use status flags that vary
        // by host. Their ownership and liveness do not affect our group.
        if observed_group != group {
            continue;
        }
        let observed_owner = fields[1]
            .parse::<i64>()
            .map_err(|_| failure("process inventory has an invalid owner"))?;
        let state = fields[2];
        if !(i64::from(i32::MIN)..=i64::from(u32::MAX)).contains(&observed_owner)
            || !state.bytes().all(|byte| byte.is_ascii_graphic())
        {
            return Err(failure("process inventory has invalid process state"));
        }
        // Darwin ps can render system UIDs as signed values.
        if observed_owner as u32 != owner {
            return Err(failure("process group identity changed"));
        }
        // Zombies have no executable work or open resources. Treat any other
        // state, including an unknown status, as potentially live.
        present |= !state.starts_with('Z');
    }
    if rows == 0 {
        return Err(failure("process inventory is empty"));
    }
    Ok(present)
}

#[cfg(unix)]
fn process_group_in_inventory(group: i32, deadline: Instant) -> BrowserResult<bool> {
    use std::os::fd::AsRawFd;
    let mut process = Command::new("/bin/ps")
        .args(["-A", "-o", "pgid=,uid=,stat="])
        .env_clear()
        .env("LC_ALL", "C")
        .stdin(Stdio::null())
        .stdout(Stdio::piped())
        .stderr(Stdio::null())
        .spawn()?;
    let outcome = (|| {
        let mut stdout = process
            .stdout
            .take()
            .ok_or_else(|| failure("process inventory output missing"))?;
        let flags = unsafe { libc::fcntl(stdout.as_raw_fd(), libc::F_GETFL) };
        if flags < 0
            || unsafe { libc::fcntl(stdout.as_raw_fd(), libc::F_SETFL, flags | libc::O_NONBLOCK) }
                < 0
        {
            return Err(io::Error::last_os_error().into());
        }
        let mut bytes = Vec::new();
        let mut buffer = [0_u8; 8192];
        loop {
            if Instant::now() >= deadline {
                return Err(failure("process inventory timed out"));
            }
            match stdout.read(&mut buffer) {
                Ok(0) => break,
                Ok(count) => {
                    if bytes.len() + count > 4 * 1024 * 1024 {
                        return Err(failure("process inventory exceeds limit"));
                    }
                    bytes.extend_from_slice(&buffer[..count]);
                }
                Err(error) if error.kind() == io::ErrorKind::WouldBlock => {
                    thread::sleep(Duration::from_millis(5))
                }
                Err(error) => return Err(error.into()),
            }
        }
        loop {
            if let Some(status) = process.try_wait()? {
                if !status.success() {
                    return Err(failure("process inventory failed"));
                }
                return inventory_contains_group(&bytes, group, unsafe { libc::geteuid() });
            }
            if Instant::now() >= deadline {
                return Err(failure("process inventory timed out"));
            }
            thread::sleep(Duration::from_millis(5));
        }
    })();
    if process.try_wait()?.is_none() {
        let _ = process.kill();
        let _ = process.wait();
    }
    outcome
}

impl Drop for OwnedChrome {
    fn drop(&mut self) {
        let _ = self.finish();
    }
}

struct CounterServer {
    url: String,
    stop: Arc<AtomicBool>,
    worker: Option<JoinHandle<()>>,
}

impl CounterServer {
    fn start() -> BrowserResult<Self> {
        let listener = TcpListener::bind(("127.0.0.1", 0))?;
        listener.set_nonblocking(true)?;
        let address = listener.local_addr()?;
        let path = format!("/counter-{}.html", unique_suffix()?);
        let url = format!("http://{address}{path}");
        let stop = Arc::new(AtomicBool::new(false));
        let worker_stop = stop.clone();
        let worker = thread::spawn(move || {
            while !worker_stop.load(Ordering::Acquire) {
                match listener.accept() {
                    Ok((mut stream, _)) => {
                        let _ = stream.set_read_timeout(Some(Duration::from_millis(500)));
                        let _ = stream.set_write_timeout(Some(Duration::from_secs(1)));
                        let mut request = [0_u8; 8192];
                        if let Ok(length) = stream.read(&mut request) {
                            let expected = format!("GET {path} HTTP/1.1\r\n");
                            let (status, body) =
                                if request[..length].starts_with(expected.as_bytes()) {
                                    ("200 OK", COUNTER_HTML)
                                } else {
                                    ("404 Not Found", "Not found")
                                };
                            let response = format!(
                                "HTTP/1.1 {status}\r\nContent-Type: text/html; charset=utf-8\r\nContent-Length: {}\r\nCache-Control: no-store\r\nConnection: close\r\n\r\n{body}",
                                body.len()
                            );
                            let _ = stream.write_all(response.as_bytes());
                        }
                    }
                    Err(error) if error.kind() == io::ErrorKind::WouldBlock => {
                        thread::sleep(Duration::from_millis(10));
                    }
                    Err(_) => break,
                }
            }
        });
        Ok(Self {
            url,
            stop,
            worker: Some(worker),
        })
    }
}

impl Drop for CounterServer {
    fn drop(&mut self) {
        self.stop.store(true, Ordering::Release);
        if let Some(worker) = self.worker.take() {
            let _ = worker.join();
        }
    }
}

pub(crate) fn chrome_executable() -> BrowserResult<PathBuf> {
    if let Some(path) = std::env::var_os("DECISION_BROWSER_CHROME") {
        let path = PathBuf::from(path);
        if path.is_file() {
            return Ok(path);
        }
        return Err(failure(
            "DECISION_BROWSER_CHROME must name a browser executable",
        ));
    }
    let candidates = [
        "/Applications/Google Chrome.app/Contents/MacOS/Google Chrome",
        "/Applications/Chromium.app/Contents/MacOS/Chromium",
        "/usr/bin/chromium",
        "/usr/bin/chromium-browser",
        "/usr/bin/google-chrome",
        "C:\\Program Files\\Google\\Chrome\\Application\\chrome.exe",
        "C:\\Program Files (x86)\\Google\\Chrome\\Application\\chrome.exe",
    ];
    candidates
        .into_iter()
        .map(PathBuf::from)
        .find(|path| path.is_file())
        .ok_or_else(|| failure("Chrome not found; set DECISION_BROWSER_CHROME to its executable"))
}

fn unique_profile() -> BrowserResult<PathBuf> {
    let path = std::env::temp_dir().join(format!("decision-browser-rust-cdp-{}", unique_suffix()?));
    let mut builder = fs::DirBuilder::new();
    #[cfg(unix)]
    {
        use std::os::unix::fs::DirBuilderExt;
        builder.mode(0o700);
    }
    builder.create(&path)?;
    Ok(path)
}

fn unique_suffix() -> BrowserResult<String> {
    Ok(format!(
        "{}-{}",
        std::process::id(),
        SystemTime::now().duration_since(UNIX_EPOCH)?.as_nanos()
    ))
}

fn string_field(value: &Value, name: &str) -> BrowserResult<String> {
    value
        .get(name)
        .and_then(Value::as_str)
        .map(str::to_owned)
        .ok_or_else(|| failure(format!("CDP result is missing string field {name}")))
}

fn number_field(value: &Value, name: &str) -> BrowserResult<f64> {
    value
        .get(name)
        .and_then(Value::as_f64)
        .filter(|number| number.is_finite())
        .ok_or_else(|| failure(format!("CDP result is missing finite number field {name}")))
}

fn counter_value(value: &Value) -> BrowserResult<u32> {
    value
        .as_u64()
        .and_then(|count| u32::try_from(count).ok())
        .ok_or_else(|| failure("counter observation is not an unsigned integer"))
}

fn failure(message: impl Into<String>) -> Box<dyn Error + Send + Sync> {
    io::Error::other(message.into()).into()
}

#[cfg(test)]
mod cancellation_tests {
    use super::*;
    use std::cell::Cell;

    #[cfg(unix)]
    #[test]
    fn process_inventory_requires_complete_unambiguous_liveness() {
        assert!(inventory_contains_group(b"0 0 Ss\n2 -2 Ss\n123 501 S+\n", 123, 501).unwrap());
        assert!(inventory_contains_group(b"0 0 Ss\n123 501 ?\n", 123, 501).unwrap());
        assert!(!inventory_contains_group(b"0 0 Ss\n123 501 Z+\n987 501 R<\n", 123, 501).unwrap());
        assert!(!inventory_contains_group(b"0 0 Ss\n-1 -2 ?\n987 - R>!\n", 123, 501).unwrap());
        for invalid in [
            b"".as_slice(),
            b"123 501\n",
            b"123 501 S extra\n",
            b"-2 0 S\n",
            b"123 999 S\n",
            b"oops 501 S\n",
            b"123 uid S\n",
            b"123 501 \x01\n",
        ] {
            assert!(inventory_contains_group(invalid, 123, 501).is_err());
        }
    }

    #[cfg(unix)]
    #[test]
    #[ignore = "requires permission to inspect native process inventory"]
    fn native_process_inventory_distinguishes_a_live_and_reaped_owned_group() -> BrowserResult<()> {
        use std::os::unix::process::CommandExt;
        let mut child = Command::new("/bin/sleep")
            .arg("20")
            .process_group(0)
            .stdin(Stdio::null())
            .stdout(Stdio::null())
            .stderr(Stdio::null())
            .spawn()?;
        let group = i32::try_from(child.id())?;
        let result = (|| {
            assert!(process_group_in_inventory(
                group,
                Instant::now() + Duration::from_secs(2)
            )?);
            child.kill()?;
            child.wait()?;
            assert!(!process_group_in_inventory(
                group,
                Instant::now() + Duration::from_secs(2)
            )?);
            Ok(())
        })();
        if child.try_wait()?.is_none() {
            let _ = child.kill();
            let _ = child.wait();
        }
        result
    }

    #[test]
    fn an_already_cancelled_counter_never_reaches_the_provider() {
        let invoked = Cell::new(false);
        let result = run_counter_guarded(
            |_, _| {
                invoked.set(true);
                Ok(())
            },
            || Err(failure("cancelled")),
        );
        assert!(!invoked.get());
        assert_eq!(result.err().unwrap().to_string(), "cancelled");
    }

    #[test]
    #[ignore = "requires an installed sandboxed Chrome"]
    fn cancellation_during_provider_selection_stops_before_input_and_cleans_up() {
        let cancelled = Cell::new(false);
        let result = run_counter_guarded(
            |_, _| {
                cancelled.set(true);
                Ok(())
            },
            || {
                if cancelled.get() {
                    Err(failure("cancelled"))
                } else {
                    Ok(())
                }
            },
        );
        assert!(cancelled.get());
        assert_eq!(result.err().unwrap().to_string(), "cancelled");
    }
}
