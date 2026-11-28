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

pub type BrowserResult<T> = Result<T, Box<dyn Error + Send + Sync>>;

const COUNTER_HTML: &str = include_str!("../../../examples/browser-control/counter.html");
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
    let fixture = CounterServer::start()?;
    let mut chrome = OwnedChrome::launch()?;
    let outcome = run_counter_in_browser(&fixture.url, &chrome, choose_increment);
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
) -> BrowserResult<CounterEvidence> {
    let deadline = Instant::now() + RUN_TIMEOUT;
    let endpoint = chrome.debugging_endpoint(deadline)?;
    let mut cdp = Cdp::connect(&endpoint, deadline)?;
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
    choose_increment(&label, before)?;
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
    // Chrome may close the socket before replying to Browser.close.
    let _ = cdp.call("Browser.close", json!({}), None);
    Ok(CounterEvidence { before, after, png })
}

struct Cdp {
    socket: WebSocket<TcpStream>,
    next_id: u64,
    deadline: Instant,
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
        })
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
                        return Err(error.into());
                    }
                }
                child.wait()?;
                let deadline = Instant::now() + Duration::from_secs(2);
                loop {
                    if unsafe { kill(-group, 0) } != 0 {
                        let error = io::Error::last_os_error();
                        if error.raw_os_error() != Some(3) {
                            return Err(error.into());
                        }
                        break;
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
            fs::remove_dir_all(&self.profile)?;
        }
        Ok(())
    }
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

fn chrome_executable() -> BrowserResult<PathBuf> {
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
