use super::{BrowserResult, Cdp, OwnedChrome, failure, number_field, string_field};
use serde_json::{Value, json};
use std::thread;
use std::time::{Duration, Instant};

const OBSERVE: &str = include_str!("../assets/matrix_observation.js");
const CHECK_PUBLIC: &str = include_str!("../assets/functional_observation.js");
const VIEWPORT: &str = "({width:innerWidth,height:innerHeight})";

pub struct MatrixPage {
    cdp: Cdp,
    session: String,
    origin: String,
    world: Option<i64>,
    observed_url: String,
    observed_choices: Vec<Value>,
    task_scope: bool,
}

pub fn run_scenario<T>(
    url: &str,
    timeout: Duration,
    run: impl FnOnce(&mut MatrixPage) -> BrowserResult<T>,
) -> BrowserResult<T> {
    run_page(url, timeout, false, &|| Ok(()), run)
}

/// Execute an admitted HTTP(S) task in an owned browser on its starting origin.
pub fn run_task_scenario<T>(
    url: &str,
    timeout: Duration,
    guard: &dyn Fn() -> BrowserResult<()>,
    run: impl FnOnce(&mut MatrixPage) -> BrowserResult<T>,
) -> BrowserResult<T> {
    run_page(url, timeout, true, guard, run)
}

/// Attach to an explicitly selected local Chrome and own only an acknowledged tab.
pub fn run_existing_task_scenario<T>(
    url: &str,
    timeout: Duration,
    guard: &dyn Fn() -> BrowserResult<()>,
    run: impl FnOnce(&mut MatrixPage) -> BrowserResult<T>,
) -> BrowserResult<T> {
    let path = selected_devtools_file()?;
    let endpoint = read_devtools_file(&path)?;
    run_existing_endpoint(&endpoint, url, timeout, guard, run)
}

pub fn existing_chrome_available() -> bool {
    selected_devtools_file()
        .and_then(|path| read_devtools_file(&path))
        .is_ok()
}

pub(crate) fn explicit_existing_chrome_available() -> bool {
    explicit_devtools_file()
        .is_some_and(|path| path.and_then(|path| read_devtools_file(&path)).is_ok())
}

fn explicit_devtools_file() -> Option<BrowserResult<std::path::PathBuf>> {
    std::env::var_os("DECISION_BROWSER_DEVTOOLS_ACTIVE_PORT").map(|path| {
        let path = std::path::PathBuf::from(path);
        if !path.is_absolute() {
            return Err(failure("invalid_devtools_path"));
        }
        Ok(path)
    })
}

fn selected_devtools_file() -> BrowserResult<std::path::PathBuf> {
    use std::path::PathBuf;
    if let Some(path) = explicit_devtools_file() {
        return path;
    }
    let home = std::env::var_os("HOME")
        .map(PathBuf::from)
        .ok_or_else(|| failure("devtools_port_unreadable"))?;
    let root = if cfg!(target_os = "macos") {
        home.join("Library/Application Support/Google/Chrome")
    } else if cfg!(target_os = "linux") {
        let config = std::env::var_os("CHROME_CONFIG_HOME")
            .or_else(|| std::env::var_os("XDG_CONFIG_HOME"))
            .map(PathBuf::from)
            .filter(|path| path.is_absolute())
            .unwrap_or_else(|| home.join(".config"));
        config.join("google-chrome")
    } else if cfg!(target_os = "windows") {
        std::env::var_os("LOCALAPPDATA")
            .map(PathBuf::from)
            .filter(|path| path.is_absolute())
            .unwrap_or_else(|| home.join("AppData/Local"))
            .join("Google/Chrome/User Data")
    } else {
        return Err(failure("existing_chrome_unsupported"));
    };
    Ok(root.join("DevToolsActivePort"))
}

fn parse_devtools_file(bytes: &[u8]) -> BrowserResult<String> {
    let text = std::str::from_utf8(bytes).map_err(|_| failure("devtools_port_malformed"))?;
    if bytes.len() > 4096 || !text.is_ascii() || text.contains('\r') {
        return Err(failure("devtools_port_malformed"));
    }
    let lines = text
        .strip_suffix('\n')
        .unwrap_or(text)
        .split('\n')
        .collect::<Vec<_>>();
    if lines.len() != 2 {
        return Err(failure("devtools_port_malformed"));
    }
    let port = lines[0]
        .parse::<u16>()
        .map_err(|_| failure("devtools_port_malformed"))?;
    let token = lines[1]
        .strip_prefix("/devtools/browser/")
        .filter(|value| {
            !value.is_empty()
                && value.len() <= 128
                && value
                    .bytes()
                    .all(|b| b.is_ascii_alphanumeric() || b == b'-')
        })
        .ok_or_else(|| failure("endpoint_rejected"))?;
    if port == 0 || lines[0] != port.to_string() {
        return Err(failure("devtools_port_malformed"));
    }
    Ok(format!("ws://127.0.0.1:{port}/devtools/browser/{token}"))
}

fn read_devtools_file(path: &std::path::Path) -> BrowserResult<String> {
    use std::io::Read;
    let before =
        std::fs::symlink_metadata(path).map_err(|_| failure("devtools_port_unreadable"))?;
    if !before.is_file() || before.file_type().is_symlink() || before.len() > 4096 {
        return Err(failure("devtools_port_unreadable"));
    }
    #[cfg(unix)]
    {
        use std::os::unix::fs::MetadataExt;
        if before.nlink() != 1 || before.mode() & 0o022 != 0 {
            return Err(failure("devtools_port_unreadable"));
        }
    }
    let mut options = std::fs::OpenOptions::new();
    options.read(true);
    #[cfg(unix)]
    {
        use std::os::unix::fs::OpenOptionsExt;
        options.custom_flags(libc::O_NOFOLLOW | libc::O_NONBLOCK);
    }
    let file = options
        .open(path)
        .map_err(|_| failure("devtools_port_unreadable"))?;
    let opened = file.metadata()?;
    let after = std::fs::symlink_metadata(path)?;
    if after.file_type().is_symlink() || !opened.is_file() {
        return Err(failure("devtools_port_unreadable"));
    }
    #[cfg(unix)]
    {
        use std::os::unix::fs::MetadataExt;

        if before.dev() != opened.dev()
            || before.ino() != opened.ino()
            || after.dev() != opened.dev()
            || after.ino() != opened.ino()
            || opened.uid() != unsafe { libc::getuid() }
            || opened.nlink() != 1
            || opened.mode() & 0o022 != 0
        {
            return Err(failure("devtools_port_unreadable"));
        }
    }
    let mut bytes = Vec::new();
    file.take(4097).read_to_end(&mut bytes)?;
    parse_devtools_file(&bytes)
}

fn target_inventory(cdp: &mut Cdp) -> BrowserResult<Vec<String>> {
    let result = cdp.call("Target.getTargets", json!({}), None)?;
    result["targetInfos"]
        .as_array()
        .ok_or_else(|| failure("target_inventory_failed"))?
        .iter()
        .filter(|info| info["type"] == "page")
        .map(|info| string_field(info, "targetId"))
        .collect()
}

fn close_existing_target(endpoint: &str, target: &str, before: &[String]) -> BrowserResult<()> {
    let deadline = Instant::now() + Duration::from_secs(3);
    let mut cleanup = Cdp::connect(endpoint, deadline).map_err(|_| failure("cleanup_unknown"))?;
    let result = cleanup
        .call("Target.closeTarget", json!({"targetId":target}), None)
        .map_err(|_| failure("cleanup_unknown"))?;
    if result["success"] != true {
        return Err(failure("cleanup_unknown"));
    }
    loop {
        let after = target_inventory(&mut cleanup).map_err(|_| failure("cleanup_unknown"))?;
        if !after.iter().any(|id| id == target) {
            // An unowned new target may be a popup. Its ownership is unresolved.
            if after.iter().any(|id| !before.contains(id)) {
                return Err(failure("cleanup_unknown"));
            }
            return Ok(());
        }
        if Instant::now() >= deadline {
            return Err(failure("cleanup_unknown"));
        }
        thread::sleep(Duration::from_millis(20));
    }
}

fn run_existing_endpoint<T>(
    endpoint: &str,
    url: &str,
    timeout: Duration,
    guard: &dyn Fn() -> BrowserResult<()>,
    run: impl FnOnce(&mut MatrixPage) -> BrowserResult<T>,
) -> BrowserResult<T> {
    guard()?;
    // Parse again so every reconnect stays bound to a full browser endpoint.
    let parsed_endpoint = reqwest::Url::parse(endpoint)?;
    if parsed_endpoint.scheme() != "ws"
        || parsed_endpoint.host_str() != Some("127.0.0.1")
        || parsed_endpoint.port().is_none()
        || !parsed_endpoint.username().is_empty()
        || parsed_endpoint.password().is_some()
        || parsed_endpoint.query().is_some()
        || parsed_endpoint.fragment().is_some()
        || !parsed_endpoint.path().starts_with("/devtools/browser/")
    {
        return Err(failure("endpoint_rejected"));
    }
    let parsed = reqwest::Url::parse(url)?;
    if !matches!(parsed.scheme(), "http" | "https")
        || !parsed.username().is_empty()
        || parsed.password().is_some()
    {
        return Err(failure("invalid_start_url"));
    }
    let deadline = Instant::now() + timeout;
    let mut cdp = Cdp::connect(endpoint, deadline)?;
    let before = target_inventory(&mut cdp)?;
    guard()?;
    let created = cdp
        .call("Target.createTarget", json!({"url":"about:blank"}), None)
        .map_err(|_| failure("cleanup_unknown"))?;
    let target = string_field(&created, "targetId").map_err(|_| failure("cleanup_unknown"))?;
    if before.contains(&target) {
        return Err(failure("cleanup_unknown"));
    }
    let result = (|| {
        let attached = cdp.call(
            "Target.attachToTarget",
            json!({"targetId":target,"flatten":true}),
            None,
        )?;
        let session = string_field(&attached, "sessionId")?;
        cdp.call("Page.enable", json!({}), Some(&session))?;
        cdp.call("Runtime.enable", json!({}), Some(&session))?;
        cdp.restrict_origin(&session, &parsed.origin().ascii_serialization())?;
        let mut page = MatrixPage {
            cdp,
            session,
            origin: parsed.origin().ascii_serialization(),
            world: None,
            observed_url: String::new(),
            observed_choices: Vec::new(),
            task_scope: true,
        };
        guard()?;
        let navigation = page
            .cdp
            .call("Page.navigate", json!({"url":url}), Some(&page.session))?;
        if navigation.get("errorText").is_some() {
            return Err(failure("initial navigation failed"));
        }
        page.ready()?;
        guard()?;
        run(&mut page)
    })();
    let cleanup = close_existing_target(endpoint, &target, &before);
    match (result, cleanup) {
        (Ok(value), Ok(())) => Ok(value),
        (_, Err(_)) => Err(failure("cleanup_unknown")),
        (Err(error), Ok(())) => Err(error),
    }
}

fn run_page<T>(
    url: &str,
    timeout: Duration,
    task_scope: bool,
    guard: &dyn Fn() -> BrowserResult<()>,
    run: impl FnOnce(&mut MatrixPage) -> BrowserResult<T>,
) -> BrowserResult<T> {
    guard()?;
    let parsed = reqwest::Url::parse(url)?;
    if !matches!(parsed.scheme(), "http" | "https")
        || !parsed.username().is_empty()
        || parsed.password().is_some()
        || parsed.host_str().is_none()
    {
        return Err(failure("invalid_start_url"));
    }
    let mut chrome = OwnedChrome::launch()?;
    let outcome = (|| {
        let deadline = Instant::now() + timeout;
        let endpoint = chrome.debugging_endpoint(deadline)?;
        let mut cdp = Cdp::connect(&endpoint, deadline)?;
        let context = cdp.call("Target.createBrowserContext", json!({}), None)?;
        let context_id = string_field(&context, "browserContextId")?;
        let target = cdp.call(
            "Target.createTarget",
            json!({"url":"about:blank", "browserContextId":context_id}),
            None,
        )?;
        let target_id = string_field(&target, "targetId")?;
        let attached = cdp.call(
            "Target.attachToTarget",
            json!({"targetId":target_id,"flatten":true}),
            None,
        )?;
        let session = string_field(&attached, "sessionId")?;
        cdp.call("Page.enable", json!({}), Some(&session))?;
        cdp.call("Runtime.enable", json!({}), Some(&session))?;
        if !task_scope
            && !(parsed.scheme() == "http" && parsed.host_str() == Some("127.0.0.1")
                || url == "https://www.python.org/")
        {
            return Err(failure("matrix URL is outside the fixture boundary"));
        }
        if task_scope {
            cdp.restrict_origin(&session, &parsed.origin().ascii_serialization())?;
        }
        let mut page = MatrixPage {
            cdp,
            session,
            origin: parsed.origin().ascii_serialization(),
            world: None,
            observed_url: String::new(),
            observed_choices: Vec::new(),
            task_scope,
        };
        let result = (|| {
            guard()?;
            let navigation =
                page.cdp
                    .call("Page.navigate", json!({"url":url}), Some(&page.session))?;
            if navigation.get("errorText").is_some() {
                return Err(failure("initial navigation failed"));
            }
            page.ready()?;
            guard()?;
            run(&mut page)
        })();
        let _ = page.cdp.call("Browser.close", json!({}), None);
        result
    })();
    let cleanup = chrome.finish();
    match (outcome, cleanup) {
        (Ok(value), Ok(())) => Ok(value),
        (Err(error), Ok(())) => Err(error),
        (Ok(_), Err(cleanup)) => Err(failure(format!("browser cleanup failed: {cleanup}"))),
        (Err(error), Err(cleanup)) => Err(failure(format!("{error}; cleanup failed: {cleanup}"))),
    }
}

impl MatrixPage {
    fn ready(&mut self) -> BrowserResult<()> {
        loop {
            let state = if self.task_scope {
                "document.readyState !== 'loading' && location.href !== 'about:blank'"
            } else {
                "document.readyState === 'complete' && location.href !== 'about:blank'"
            };
            if self.cdp.evaluate(state, &self.session)? == true {
                return Ok(());
            }
            thread::sleep(Duration::from_millis(20));
        }
    }

    fn enter_world(&mut self) -> BrowserResult<()> {
        let tree = self
            .cdp
            .call("Page.getFrameTree", json!({}), Some(&self.session))?;
        let frame = tree
            .pointer("/frameTree/frame/id")
            .and_then(Value::as_str)
            .ok_or_else(|| failure("main frame missing"))?;
        let created = self.cdp.call(
            "Page.createIsolatedWorld",
            json!({"frameId":frame,"worldName":"decision-browser-matrix"}),
            Some(&self.session),
        )?;
        self.world = created["executionContextId"].as_i64();
        if self.world.is_none() {
            return Err(failure("isolated world missing"));
        }
        Ok(())
    }

    fn eval(&mut self, expression: &str) -> BrowserResult<Value> {
        let context = self
            .world
            .ok_or_else(|| failure("snapshot required before action"))?;
        let result = self.cdp.call(
            "Runtime.evaluate",
            json!({"expression":expression,"contextId":context,"returnByValue":true}),
            Some(&self.session),
        )?;
        if result.get("exceptionDetails").is_some() {
            return Err(failure("matrix evaluation failed"));
        }
        result
            .pointer("/result/value")
            .cloned()
            .ok_or_else(|| failure("matrix evaluation returned no value"))
    }

    pub fn snapshot(&mut self) -> BrowserResult<Value> {
        self.cdp.settle_network()?;
        self.ready()?;
        self.enter_world()?;
        let state = self.eval(OBSERVE)?;
        let url = string_field(&state, "url")?;
        self.check_url(&url)?;
        self.observed_url = url;
        self.observed_choices = state["choices"]
            .as_array()
            .ok_or_else(|| failure("snapshot choices missing"))?
            .clone();
        Ok(state)
    }

    fn check_url(&self, url: &str) -> BrowserResult<()> {
        let parsed = reqwest::Url::parse(url)?;
        if parsed.origin().ascii_serialization() != self.origin {
            return Err(failure("navigation escaped matrix origin"));
        }
        if !self.task_scope
            && self.origin == "https://www.python.org"
            && !["/", "/about/"].contains(&parsed.path())
        {
            return Err(failure("public navigation escaped About scope"));
        }
        Ok(())
    }

    fn element_expression(&self, target: &str) -> BrowserResult<String> {
        Ok(format!(
            "globalThis.__matrixNodes?.get({})",
            serde_json::to_string(target)?
        ))
    }

    fn geometry(&mut self, target: &str) -> BrowserResult<Value> {
        let element = self.element_expression(target)?;
        let expected_url = serde_json::to_string(&self.observed_url)?;
        self.eval(&format!("(() => {{if(location.href!=={expected_url})return null; const e={element}; if(!e||!e.isConnected||e.disabled||globalThis.__matrixPrivateControl(e))return null; const r=e.getBoundingClientRect(); const x=r.x+r.width/2,y=r.y+r.height/2; const h=document.elementFromPoint(x,y); if(r.width<=0||r.height<=0||x<0||y<0||x>=innerWidth||y>=innerHeight||!(h===e||e.contains(h)))return null; return {{x,y,label:globalThis.__matrixLabel(e),value:e.value,current:e.type==='checkbox'||e.type==='radio'?String(e.checked):e.value,tag:e.tagName,type:e.type||'',effect:globalThis.__matrixEffect(e)}}; }})()"))
    }

    fn focus(&mut self, target: &str) -> BrowserResult<()> {
        let context = self.world.ok_or_else(|| failure("snapshot required"))?;
        let result = self.cdp.call(
            "Runtime.evaluate",
            json!({"expression":self.element_expression(target)?,"contextId":context}),
            Some(&self.session),
        )?;
        let object = result
            .pointer("/result/objectId")
            .and_then(Value::as_str)
            .ok_or_else(|| failure("stale focus target"))?;
        let node = self.cdp.call(
            "DOM.describeNode",
            json!({"objectId":object}),
            Some(&self.session),
        )?;
        let backend = node
            .pointer("/node/backendNodeId")
            .and_then(Value::as_u64)
            .ok_or_else(|| failure("focus node missing"))?;
        self.cdp.call(
            "DOM.focus",
            json!({"backendNodeId":backend}),
            Some(&self.session),
        )?;
        self.cdp.call(
            "Runtime.releaseObject",
            json!({"objectId":object}),
            Some(&self.session),
        )?;
        Ok(())
    }

    pub fn act(&mut self, operation: &str, target: &str, value: Option<&str>) -> BrowserResult<()> {
        self.act_guarded(operation, target, value, &|| Ok(()))
    }

    pub fn act_guarded(
        &mut self,
        operation: &str,
        target: &str,
        value: Option<&str>,
        guard: &dyn Fn() -> BrowserResult<()>,
    ) -> BrowserResult<()> {
        guard()?;
        let url = self.cdp.evaluate("location.href", &self.session)?;
        self.check_url(url.as_str().ok_or_else(|| failure("page URL missing"))?)?;
        match operation {
            "wait" => thread::sleep(Duration::from_millis(300)),
            "scroll" => {
                let delta = if value == Some("up") || target == "up" {
                    -500
                } else {
                    500
                };
                guard()?;
                self.cdp.call(
                    "Input.dispatchMouseEvent",
                    json!({"type":"mouseWheel","x":450,"y":300,"deltaX":0,"deltaY":delta}),
                    Some(&self.session),
                )?;
            }
            "click" | "submit" | "fill" | "select" | "enter" => {
                let geometry = self.geometry(target)?;
                if geometry.is_null() {
                    return Err(failure("target became stale, obscured or offscreen"));
                }
                let choice = self
                    .observed_choices
                    .iter()
                    .find(|choice| choice["id"] == target)
                    .ok_or_else(|| failure("target was not offered in the snapshot"))?;
                let offered = choice["operation"].as_str();
                let compatible = offered == Some(operation)
                    || operation == "submit" && offered == Some("click")
                    || operation == "enter" && offered == Some("fill");
                if !compatible
                    || choice["label"] != geometry["label"]
                    || choice["tag"] != geometry["tag"]
                    || choice["inputType"] != geometry["type"]
                    || choice["effect"] != geometry["effect"]
                    || choice
                        .get("current")
                        .is_some_and(|current| current != &geometry["current"])
                {
                    return Err(failure("target semantics changed after snapshot"));
                }
                if let Some(href) = geometry["effect"]["href"].as_str() {
                    self.check_url(href)?;
                }
                if self.task_scope {
                    if let Some(action) = geometry["effect"]["destination"][0].as_str() {
                        self.check_url(action)?;
                    }
                    for target in [
                        &geometry["effect"]["linkTarget"],
                        &geometry["effect"]["destination"][2],
                    ] {
                        if target.as_str().is_some_and(|name| {
                            !name.is_empty() && !name.eq_ignore_ascii_case("_self")
                        }) {
                            return Err(failure("target would navigate another browsing context"));
                        }
                    }
                }
                match operation {
                    "fill" => {
                        if geometry["tag"] != "INPUT" && geometry["tag"] != "TEXTAREA" {
                            return Err(failure("fill target is not editable"));
                        }
                        self.focus(target)?;
                        guard()?;
                        self.cdp.call("Input.dispatchKeyEvent", json!({"type":"keyDown","key":"a","code":"KeyA","modifiers":if cfg!(target_os="macos"){4}else{2},"commands":["selectAll"]}), Some(&self.session))?;
                        guard()?;
                        self.cdp.call(
                            "Input.dispatchKeyEvent",
                            json!({"type":"keyUp","key":"a","code":"KeyA"}),
                            Some(&self.session),
                        )?;
                        guard()?;
                        self.cdp.call(
                            "Input.insertText",
                            json!({"text":value.ok_or_else(|| failure("fill value missing"))?}),
                            Some(&self.session),
                        )?;
                    }
                    "select" => {
                        if geometry["tag"] != "SELECT" {
                            return Err(failure("select target is not a select"));
                        }
                        let element = self.element_expression(target)?;
                        let desired = serde_json::to_string(
                            value.ok_or_else(|| failure("select value missing"))?,
                        )?;
                        guard()?;
                        if self.eval(&format!("(() => {{const e={element};const v={desired};const matches=Array.from(e.options).filter(o=>!o.disabled&&(o.value===v||o.textContent.trim()===v));if(matches.length!==1)return false;Reflect.apply(Object.getOwnPropertyDescriptor(HTMLSelectElement.prototype,'value').set,e,[matches[0].value]);e.dispatchEvent(new Event('input',{{bubbles:true}}));e.dispatchEvent(new Event('change',{{bubbles:true}}));return true;}})()"))? != true {
                            return Err(failure("select option is absent or ambiguous"));
                        }
                    }
                    "enter" => {
                        self.focus(target)?;
                        for kind in ["keyDown", "keyUp"] {
                            guard()?;
                            let mut event = json!({"type":kind,"key":"Enter","code":"Enter","windowsVirtualKeyCode":13,"nativeVirtualKeyCode":13});
                            if kind == "keyDown" {
                                event["text"] = json!("\r");
                                event["unmodifiedText"] = json!("\r");
                            }
                            self.cdp
                                .call("Input.dispatchKeyEvent", event, Some(&self.session))?;
                        }
                    }
                    _ => {
                        let x = number_field(&geometry, "x")?;
                        let y = number_field(&geometry, "y")?;
                        guard()?;
                        self.cdp.call(
                            "Input.dispatchMouseEvent",
                            json!({"type":"mouseMoved","x":x,"y":y}),
                            Some(&self.session),
                        )?;
                        if self.geometry(target)? != geometry {
                            return Err(failure("target changed after hover"));
                        }
                        for (kind, buttons) in [("mousePressed", 1), ("mouseReleased", 0)] {
                            guard()?;
                            self.cdp.call("Input.dispatchMouseEvent", json!({"type":kind,"x":x,"y":y,"button":"left","buttons":buttons,"clickCount":1}), Some(&self.session))?;
                        }
                    }
                }
            }
            _ => return Err(failure("unsupported matrix operation")),
        }
        thread::sleep(Duration::from_millis(200));
        self.cdp.settle_network()?;
        Ok(())
    }

    /// Record bounded local geometry without sending browser pixels to a model.
    pub fn inspect_geometry(&mut self, width: u64, height: u64) -> BrowserResult<Value> {
        if !(100..=4096).contains(&width) || !(100..=4096).contains(&height) {
            return Err(failure("viewport_invalid"));
        }
        self.ready()?;
        self.enter_world()?;
        let original = self.eval(VIEWPORT)?;
        let requested = json!({"width":width,"height":height});
        let changed = self.cdp.call(
            "Emulation.setDeviceMetricsOverride",
            json!({"width":width,"height":height,"deviceScaleFactor":1,"mobile":false}),
            Some(&self.session),
        );
        let result = changed.and_then(|_| {
            self.wait_for_viewport(&requested)?;
            self.eval(include_str!("../assets/geometry_observation.js"))
        });
        let restored = self.cdp.call(
            "Emulation.clearDeviceMetricsOverride",
            json!({}),
            Some(&self.session),
        );
        if restored
            .and_then(|_| self.wait_for_viewport(&original))
            .is_err()
        {
            return Err(failure("viewport_restore_failed"));
        }
        let mut report = result?;
        report["viewport"] = requested;
        Ok(report)
    }

    fn wait_for_viewport(&mut self, expected: &Value) -> BrowserResult<()> {
        // CDP acknowledges the metrics command before the renderer has
        // necessarily applied the resize.
        let deadline = self
            .cdp
            .deadline
            .min(Instant::now() + Duration::from_secs(2));
        loop {
            if self.eval(VIEWPORT)? == *expected {
                return Ok(());
            }
            if Instant::now() >= deadline {
                return Err(failure("viewport_change_unverified"));
            }
            thread::sleep(Duration::from_millis(20));
        }
    }

    pub fn screenshot(&mut self) -> BrowserResult<Vec<u8>> {
        use base64::Engine;
        let shot = self.cdp.call(
            "Page.captureScreenshot",
            json!({"format":"png"}),
            Some(&self.session),
        )?;
        let png = base64::engine::general_purpose::STANDARD.decode(string_field(&shot, "data")?)?;
        if !png.starts_with(b"\x89PNG\r\n\x1a\n") {
            return Err(failure("invalid browser PNG"));
        }
        Ok(png)
    }

    /// Evaluate the entire public check list in one synchronous document observation.
    pub fn read_public_checks(&mut self, checks: &[Value]) -> BrowserResult<bool> {
        self.cdp.settle_network()?;
        self.ready()?;
        let encoded = serde_json::to_string(checks)?;
        // Keep the execution context from the approved snapshot. A replaced
        // document invalidates it, even when the replacement keeps the URL.
        let raw = self.eval(&format!("{CHECK_PUBLIC}({encoded})"))?;
        if raw["url"].as_str() != Some(&self.observed_url) {
            return Err(failure("document changed before final checks"));
        }
        let results = raw["results"]
            .as_array()
            .ok_or_else(|| failure("check observation failed"))?;
        Ok(!checks.is_empty()
            && results.len() == checks.len()
            && results.iter().all(|value| value == true))
    }

    pub fn read_check(&mut self, check: &Value) -> BrowserResult<bool> {
        self.ready()?;
        self.enter_world()?;
        let url = self.eval("location.href")?;
        self.check_url(url.as_str().ok_or_else(|| failure("page URL missing"))?)?;
        let kind = check["type"]
            .as_str()
            .ok_or_else(|| failure("check type missing"))?;
        let observed = if kind == "url_equals" {
            self.eval("location.href")?
        } else {
            let selector = serde_json::to_string(
                check["target"]
                    .as_str()
                    .ok_or_else(|| failure("check target missing"))?,
            )?;
            match kind {
                "text_equals" => self.eval(&format!(
                    "document.querySelector({selector})?.textContent?.trim() ?? null"
                ))?,
                "value_equals" => self.eval(&format!(
                    "document.querySelector({selector})?.value ?? null"
                ))?,
"count_equals" => self.eval(&format!("document.querySelectorAll({selector}).length"))?,
"visible_equals" => self.eval(&format!("(() => {{const e=document.querySelector({selector}); if(!e)return false;const r=e.getBoundingClientRect(),s=getComputedStyle(e);return s.visibility!=='hidden'&&s.display!=='none'&&r.width>0&&r.height>0;}})()"))?,
_ => return Err(failure("unsupported matrix check")),
            }
        };
        Ok(observed == check["value"])
    }
}

#[cfg(test)]
mod tests {
    use super::*;
    use crate::browser::CounterServer;

    #[test]
    #[ignore = "requires an installed sandboxed Chrome"]
    fn hides_private_controls_and_refuses_a_retargeted_form() -> BrowserResult<()> {
        let fixture = crate::matrix_fixtures::start("search")?;
        run_task_scenario(&fixture.url, Duration::from_secs(15), &|| Ok(()), |page| {
            page.snapshot()?;
            page.eval("(() => {const e=document.createElement('input');e.type='password';e.setAttribute('value','secret-test-value');document.body.prepend(e);return true})()")?;
            assert!(!page.read_public_checks(&[json!({"kind":"value","target":"input[type=password]","expected":"secret-test-value"})])?);
            assert!(!page.read_public_checks(&[
                json!({"kind":"count","target":"input[type=password]","expected":1})
            ])?);
            assert!(page.read_public_checks(&[json!({"kind":"count","target":"body:has(input[value='secret-test-value'])","expected":0})])?);
            let state = page.snapshot()?;
            assert!(!state.to_string().contains("secret-test-value"));
            assert!(
                state["choices"]
                    .as_array()
                    .unwrap()
                    .iter()
                    .all(|choice| choice["inputType"] != "password")
            );
            let button = state["choices"]
                .as_array()
                .unwrap()
                .iter()
                .find(|choice| choice["label"] == "Search")
                .unwrap();
            page.eval("(() => {document.querySelector('form').action='/changed-after-observation';return true})()")?;
            let error = page
                .act("click", &string_field(button, "id")?, None)
                .unwrap_err();
            assert_eq!(error.to_string(), "target semantics changed after snapshot");
            assert!(!fixture.verify()?);
            page.eval(
                "(() => {history.pushState({}, '', '/same-origin-after-snapshot');return true})()",
            )?;
            assert_eq!(
                page.read_public_checks(&[json!({"kind":"count","target":"form","expected":1})])
                    .unwrap_err()
                    .to_string(),
                "document changed before final checks"
            );
            Ok(())
        })
    }

    #[test]
    #[ignore = "requires an installed sandboxed Chrome"]
    fn matrix_fixture_labels_and_native_form_actions() -> BrowserResult<()> {
        for case in ["search", "hotel", "profile_form"] {
            let fixture = crate::matrix_fixtures::start(case)?;
            run_scenario(&fixture.url, Duration::from_secs(15), |page| {
                let values = fixture
                    .values
                    .as_object()
                    .ok_or_else(|| failure("values missing"))?;
                for (label, value) in values {
                    let state = page.snapshot()?;
                    let choice = state["choices"]
                        .as_array()
                        .unwrap()
                        .iter()
                        .find(|choice| choice["operation"] == "fill" && choice["label"] == *label)
                        .ok_or_else(|| failure(format!("{case}: no fill choice for {label}")))?;
                    page.act("fill", &string_field(choice, "id")?, value.as_str())?;
                    let after = page.snapshot()?;
                    assert!(
                        after["choices"]
                            .as_array()
                            .unwrap()
                            .iter()
                            .any(|choice| choice["label"] == *label && choice["current"] == *value)
                    );
                }
                if case == "profile_form" {
                    let state = page.snapshot()?;
                    let select = state["choices"]
                        .as_array()
                        .unwrap()
                        .iter()
                        .find(|choice| choice["label"] == "Delivery")
                        .unwrap();
                    page.act("select", &string_field(select, "id")?, Some("Express"))?;
                }
                if case != "hotel" {
                    let state = page.snapshot()?;
                    let label = if case == "search" {
                        "Search"
                    } else {
                        "Confirm profile"
                    };
                    let button = state["choices"]
                        .as_array()
                        .unwrap()
                        .iter()
                        .find(|choice| choice["label"] == label)
                        .unwrap();
                    page.act("click", &string_field(button, "id")?, None)?;
                    let deadline = Instant::now() + Duration::from_secs(2);
                    loop {
                        let checks = fixture
                            .checks
                            .as_array()
                            .unwrap()
                            .iter()
                            .map(|check| page.read_check(check))
                            .collect::<BrowserResult<Vec<_>>>()?;
                        if checks.into_iter().all(|passed| passed) && fixture.verify()? {
                            break;
                        }
                        assert!(
                            Instant::now() < deadline,
                            "{case}: fixture did not acknowledge input before deadline"
                        );
                        thread::sleep(Duration::from_millis(20));
                    }
                }
                Ok(())
            })?;
        }
        Ok(())
    }

    #[test]
    #[ignore = "requires an installed sandboxed Chrome"]
    fn matrix_snapshot_input_and_checks_use_native_cdp() -> BrowserResult<()> {
        let fixture = CounterServer::start()?;
        run_scenario(&fixture.url, Duration::from_secs(15), |page| {
            let state = page.snapshot()?;
            let button = state["choices"]
                .as_array()
                .and_then(|choices| choices.iter().find(|choice| choice["label"] == "inc"))
                .ok_or_else(|| failure("counter button missing from snapshot"))?;
            let target = string_field(button, "id")?;
            assert!(page.act("click", "absent", None).is_err());
            assert!(page.read_check(&json!({"type":"text_equals","target":"#count","value":"0"}))?);
            page.snapshot()?;
            page.act("click", &target, None)?;
            assert!(page.read_check(&json!({"type":"text_equals","target":"#count","value":"1"}))?);
            assert!(page.read_check(&json!({"type":"url_equals","value":fixture.url}))?);
            assert!(page.read_public_checks(&[
                json!({"kind":"text","target":"#count","expected":"1"}),
                json!({"kind":"count","target":"#count,#inc","expected":2})
            ])?);
            assert!(!page.read_public_checks(&[
                json!({"kind":"text","target":"#count,#inc","expected":"1"})
            ])?);
            let original = page.eval("({width:innerWidth,height:innerHeight})")?;
            let geometry = page.inspect_geometry(390, 844)?;
            assert_eq!(geometry["viewport"], json!({"width":390,"height":844}));
            assert!(geometry["nodes"].as_array().unwrap().len() <= 256);
            assert_eq!(
                page.eval("({width:innerWidth,height:innerHeight})")?,
                original
            );
            assert!(page.check_url("https://example.org/").is_err());
            Ok(())
        })
    }
    #[test]
    fn devtools_file_parser_rejects_remote_or_ambiguous_endpoints() {
        assert_eq!(
            parse_devtools_file(b"9222\n/devtools/browser/abc-123\n").unwrap(),
            "ws://127.0.0.1:9222/devtools/browser/abc-123"
        );
        for bytes in [
            b"0\n/devtools/browser/abc".as_slice(),
            b"09222\n/devtools/browser/abc",
            b"9222\n/devtools/page/abc",
            b"9222\n/devtools/browser/abc?x=1",
            b"9222\n/devtools/browser/abc\n\n",
            b"9222\r\n/devtools/browser/abc",
        ] {
            assert!(parse_devtools_file(bytes).is_err());
        }
    }

    #[test]
    #[ignore = "requires installed sandboxed Chrome"]
    fn existing_mode_preserves_foreign_tab_and_browser_after_owned_click() -> BrowserResult<()> {
        let fixture = CounterServer::start()?;
        let mut chrome = OwnedChrome::launch()?;
        let result = (|| {
            let endpoint = chrome.debugging_endpoint(Instant::now() + Duration::from_secs(15))?;
            assert_eq!(
                read_devtools_file(&chrome.profile.join("DevToolsActivePort"))?,
                endpoint
            );
            let mut foreign = Cdp::connect(&endpoint, Instant::now() + Duration::from_secs(30))?;
            let target = foreign.call("Target.createTarget", json!({"url":"about:blank"}), None)?;
            let foreign_id = string_field(&target, "targetId")?;
            let attached = foreign.call(
                "Target.attachToTarget",
                json!({"targetId":foreign_id,"flatten":true}),
                None,
            )?;
            let session = string_field(&attached, "sessionId")?;
            foreign.evaluate("document.body.innerHTML='<p id=sentinel>foreign marker</p>'; document.title='untouched'",&session)?;
            assert_eq!(foreign.call("Network.setCookie",json!({"url":fixture.url,"name":"db_existing_sentinel","value":"retained","sameSite":"Lax"}),Some(&session))?["success"],true);
            foreign.call("Network.setCookie", json!({"name":"owned_test_session","value":"existing-context-marker","url":fixture.url}), Some(&session))?;
            let before = target_inventory(&mut foreign)?;
            run_existing_endpoint(
                &endpoint,
                &fixture.url,
                Duration::from_secs(15),
                &|| Ok(()),
                |page| {
                    let snapshot = page.snapshot()?;
                    assert_eq!(
                        page.cdp.evaluate(
                            "document.cookie.includes('db_existing_sentinel=retained')",
                            &page.session
                        )?,
                        true
                    );
                    assert_eq!(
                        page.eval(
                            "document.cookie.includes('owned_test_session=existing-context-marker')"
                        )?,
                        true
                    );
                    let button = snapshot["choices"]
                        .as_array()
                        .unwrap()
                        .iter()
                        .find(|item| item["label"] == "inc")
                        .ok_or_else(|| failure("counter missing"))?;
                    page.act_guarded("click", button["id"].as_str().unwrap(), None, &|| Ok(()))?;
                    assert!(page.read_check(
                        &json!({"type":"text_equals","target":"#count","value":"1"})
                    )?);
                    Ok(())
                },
            )?;
            assert!(foreign.evaluate("location.href==='about:blank' && document.title==='untouched' && document.querySelector('#sentinel').textContent==='foreign marker'",&session)?==true);
            let cookies = foreign.call(
                "Network.getCookies",
                json!({"urls":[fixture.url]}),
                Some(&session),
            )?;
            assert!(
                cookies["cookies"]
                    .as_array()
                    .unwrap()
                    .iter()
                    .any(|cookie| cookie["name"] == "db_existing_sentinel"
                        && cookie["value"] == "retained")
            );
            let mut after = target_inventory(&mut foreign)?;
            let mut before = before;
            before.sort();
            after.sort();
            assert_eq!(before, after);
            // Browser.close is issued by the disposable test owner only.
            let _ = foreign.call("Browser.close", json!({}), None);
            Ok(())
        })();
        let cleanup = chrome.finish();
        result.and(cleanup)
    }
    struct HttpProbe {
        url: String,
        hits: std::sync::Arc<std::sync::atomic::AtomicUsize>,
        stop: std::sync::Arc<std::sync::atomic::AtomicBool>,
        worker: Option<std::thread::JoinHandle<()>>,
    }
    impl HttpProbe {
        fn start(foreign: Option<String>) -> BrowserResult<Self> {
            use std::io::{Read, Write};
            use std::sync::{
                Arc,
                atomic::{AtomicBool, AtomicUsize, Ordering},
            };
            let listener = std::net::TcpListener::bind(("127.0.0.1", 0))?;
            listener.set_nonblocking(true)?;
            let url = format!("http://{}", listener.local_addr()?);
            let hits = Arc::new(AtomicUsize::new(0));
            let stop = Arc::new(AtomicBool::new(false));
            let counted = hits.clone();
            let stopped = stop.clone();
            let worker = thread::spawn(move || {
                let mut streams = Vec::new();
                while !stopped.load(Ordering::Acquire) {
                    match listener.accept() {
                        Ok((mut socket, _)) => {
                            let _ = socket.set_nonblocking(false);
                            let _ = socket.set_read_timeout(Some(Duration::from_millis(300)));
                            let mut buffer = [0; 16384];
                            let Ok(count) = socket.read(&mut buffer) else {
                                continue;
                            };
                            if count == 0 {
                                continue;
                            }
                            counted.fetch_add(1, Ordering::AcqRel);
                            let request = String::from_utf8_lossy(&buffer[..count]);
                            if request.starts_with("GET /events ")
                                || request.starts_with("GET /slow-image ")
                            {
                                let is_event = request.starts_with("GET /events ");
                                let _=socket.write_all(if is_event {b"HTTP/1.1 200 OK\r\nContent-Type: text/event-stream\r\nConnection: close\r\n\r\ndata: active\n\n"}else{b"HTTP/1.1 200 OK\r\nContent-Type: image/png\r\nContent-Length: 999999\r\nConnection: close\r\n\r\n"});
                                let stop = stopped.clone();
                                streams.push(thread::spawn(move || {
                                    let _socket = socket;
                                    while !stop.load(Ordering::Acquire) {
                                        thread::sleep(Duration::from_millis(10));
                                    }
                                }));
                                continue;
                            }
                            let response = if request.starts_with("GET /redirect ") {
                                format!(
                                    "HTTP/1.1 302 Found\r\nLocation: {}/redirected\r\nContent-Length: 0\r\nConnection: close\r\n\r\n",
                                    foreign.as_deref().unwrap()
                                )
                            } else {
                                let body = if request.starts_with("GET /streaming ") {
                                    "<!doctype html><p id=ready>ready</p><img src='/slow-image'><script>new EventSource('/events')</script>".to_owned()
                                } else if request.starts_with("GET /protected ") {
                                    let foreign = foreign.as_deref().unwrap();
                                    let websocket = foreign.replace("http://", "ws://");
                                    format!(
                                        "<!doctype html><p id=ready>ready</p><button id=probe>Run probes</button><script>document.querySelector('#probe').onclick=()=>{{window.popupBlocked=window.open('/popup')===null;try{{const w=new Worker('/worker.js');w.onerror=()=>window.workerBlocked=true;}}catch(e){{window.workerBlocked=true;}}try{{const s=new WebSocket('{websocket}/socket');s.onerror=()=>window.socketBlocked=true;}}catch(e){{window.socketBlocked=true;}};fetch('{foreign}/fetch').catch(()=>{{}});}}</script>"
                                    )
                                } else if request.starts_with("GET /fixture ") {
                                    let foreign = foreign.as_deref().unwrap();
                                    format!(
                                        "<!doctype html><p id=ready>ready</p><img src='{foreign}/image'><iframe src='{foreign}/frame'></iframe><form action='{foreign}/posted' method='post'><button>Submit outside</button></form><script>Promise.allSettled([fetch('/allowed'),fetch('{foreign}/fetch')]).then(()=>document.body.dataset.probeDone='true')</script>"
                                    )
                                } else {
                                    "ok".to_owned()
                                };
                                format!(
                                    "HTTP/1.1 200 OK\r\nContent-Type: text/html\r\nContent-Length: {}\r\nConnection: close\r\n\r\n{body}",
                                    body.len()
                                )
                            };
                            let _ = socket.write_all(response.as_bytes());
                        }
                        Err(error) if error.kind() == std::io::ErrorKind::WouldBlock => {
                            thread::sleep(Duration::from_millis(5))
                        }
                        Err(_) => break,
                    }
                }
                for stream in streams {
                    let _ = stream.join();
                }
            });
            Ok(Self {
                url,
                hits,
                stop,
                worker: Some(worker),
            })
        }
    }
    impl Drop for HttpProbe {
        fn drop(&mut self) {
            self.stop.store(true, std::sync::atomic::Ordering::Release);
            if let Some(worker) = self.worker.take() {
                let _ = worker.join();
            }
        }
    }

    #[test]
    #[ignore = "requires installed sandboxed Chrome"]
    fn ordinary_observation_does_not_wait_for_sse_or_slow_images() -> BrowserResult<()> {
        use std::sync::atomic::Ordering;
        let server = HttpProbe::start(None)?;
        let started = Instant::now();
        run_task_scenario(
            &format!("{}/streaming", server.url),
            Duration::from_secs(10),
            &|| Ok(()),
            |page| {
                page.snapshot()?;
                assert!(page.read_public_checks(&[
                    json!({"kind":"text","target":"#ready","expected":"ready"})
                ])?);
                Ok(())
            },
        )?;
        assert!(started.elapsed() < Duration::from_secs(4));
        assert!(server.hits.load(Ordering::Acquire) >= 3);
        Ok(())
    }

    #[test]
    #[ignore = "requires installed sandboxed Chrome"]
    fn isolated_product_filters_foreign_http_before_network() -> BrowserResult<()> {
        use std::sync::atomic::Ordering;
        let foreign = HttpProbe::start(None)?;
        let origin = HttpProbe::start(Some(foreign.url.clone()))?;
        run_task_scenario(
            &format!("{}/fixture", origin.url),
            Duration::from_secs(15),
            &|| Ok(()),
            |page| {
                let deadline = Instant::now() + Duration::from_secs(3);
                loop {
                    page.snapshot()?;
                    if page.eval("document.body.dataset.probeDone==='true'")? == true {
                        // Invoke native form submission from the test page world to
                        // exercise Fetch independently of semantic action prechecks.
                        let _ = page
                            .cdp
                            .evaluate("document.forms[0].submit(); true", &page.session);
                        for _ in 0..3 {
                            let _ = page.cdp.evaluate("location.href", &page.session);
                        }
                        break;
                    }
                    assert!(
                        Instant::now() < deadline,
                        "same-origin fetch failed to settle"
                    );
                }
                Ok(())
            },
        )?;
        assert!(
            origin.hits.load(Ordering::Acquire) >= 2,
            "same-origin page and fetch must reach origin"
        );
        assert_eq!(
            foreign.hits.load(Ordering::Acquire),
            0,
            "foreign image/frame/fetch/form contacted upstream"
        );
        let result = run_task_scenario(
            &format!("{}/redirect", origin.url),
            Duration::from_secs(15),
            &|| Ok(()),
            |_| Ok(()),
        );
        assert!(result.is_err(), "off-origin redirect was accepted");
        assert_eq!(
            foreign.hits.load(Ordering::Acquire),
            0,
            "foreign redirect contacted upstream"
        );
        Ok(())
    }

    #[test]
    #[ignore = "requires installed sandboxed Chrome"]
    fn product_response_policy_blocks_workers_popups_and_foreign_sockets() -> BrowserResult<()> {
        use std::sync::atomic::Ordering;
        let foreign = HttpProbe::start(None)?;
        let origin = HttpProbe::start(Some(foreign.url.clone()))?;
        run_task_scenario(
            &format!("{}/protected", origin.url),
            Duration::from_secs(15),
            &|| Ok(()),
            |page| {
                let state = page.snapshot()?;
                let button = state["choices"]
                    .as_array()
                    .unwrap()
                    .iter()
                    .find(|item| item["label"] == "Run probes")
                    .unwrap();
                let mut before = target_inventory(&mut page.cdp)?;
                page.act("click", button["id"].as_str().unwrap(), None)?;
                page.snapshot()?;
                assert_eq!(page.cdp.evaluate("window.popupBlocked===true && window.workerBlocked===true && window.socketBlocked===true", &page.session)?,true);
                let mut after = target_inventory(&mut page.cdp)?;
                before.sort();
                after.sort();
                assert_eq!(before, after);
                Ok(())
            },
        )?;
        // Only the document and a possible favicon may reach the origin. In
        // particular, neither a worker script nor popup navigation is loaded.
        assert!(origin.hits.load(Ordering::Acquire) <= 2);
        assert_eq!(foreign.hits.load(Ordering::Acquire), 0);
        Ok(())
    }

    #[test]
    #[ignore = "requires installed sandboxed Chrome"]
    fn existing_product_filters_foreign_requests_on_its_owned_tab() -> BrowserResult<()> {
        use std::sync::atomic::Ordering;
        let foreign = HttpProbe::start(None)?;
        let origin = HttpProbe::start(Some(foreign.url.clone()))?;
        let mut chrome = OwnedChrome::launch()?;
        let result = (|| {
            let endpoint = chrome.debugging_endpoint(Instant::now() + Duration::from_secs(15))?;
            run_existing_endpoint(
                &endpoint,
                &format!("{}/fixture", origin.url),
                Duration::from_secs(15),
                &|| Ok(()),
                |page| {
                    page.snapshot()?;
                    assert_eq!(page.eval("document.body.dataset.probeDone==='true'")?, true);
                    Ok(())
                },
            )?;
            assert_eq!(foreign.hits.load(Ordering::Acquire), 0);
            let mut owner = Cdp::connect(&endpoint, Instant::now() + Duration::from_secs(3))?;
            let _ = owner.call("Browser.close", json!({}), None);
            Ok(())
        })();
        let cleanup = chrome.finish();
        result.and(cleanup)
    }
    struct AcceptLocalCounter;
    impl crate::task_runner::RunControl for AcceptLocalCounter {
        fn cancelled(&self) -> bool {
            false
        }
        fn approve(&self, _: Value) -> Result<(), &'static str> {
            Ok(())
        }
    }

    #[test]
    #[ignore = "requires installed sandboxed Chrome"]
    fn product_fixture_counter_settles_exactly_three_server_effects() -> BrowserResult<()> {
        let fixture = crate::matrix_fixtures::start("counter")?;
        let checks = fixture
            .checks
            .as_array()
            .unwrap()
            .iter()
            .map(|check| {
                json!({
                    "kind":if check["type"]=="url_equals" {"route"}else{"text"},
                    "target":check["target"].as_str().unwrap_or(""),"expected":check["value"]
                })
            })
            .collect::<Vec<_>>();
        let request = json!({"goal":fixture.goal,"start_url":fixture.url,"values":fixture.values,
            "checks":checks,"limits":{"max_seconds":30.0},"privacy":{}});
        let result = crate::task_runner::execute(
            &request,
            crate::provider::Provider::Fixture,
            &AcceptLocalCounter,
        );
        assert_eq!(result.terminal, "verified", "{:?}", result.reason);
        assert_eq!(result.usage["steps"], 3);
        let oracle = fixture.oracle()?;
        assert_eq!(
            oracle["counter"], 3,
            "page completion lag must not authorize a fourth click"
        );
        assert_eq!(oracle["success"], true);
        Ok(())
    }
}
